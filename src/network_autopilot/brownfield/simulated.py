"""Deterministic simulated network for Stage 4 tests and the benchmark.

This is not a mock of the diagnosis engine: it is a small network state model
built by the REAL compiler from a `NetworkSpec` (`compile_spec`), which then
renders a running configuration, applies IOS-shaped repair commands to that
state, and answers the importer/tracer/verifier through the same adapter
surface the live Packet Tracer adapter uses.

Why it exists: the fault-injection benchmark must execute the engine on
network after network, deterministically, on a laptop with no Packet Tracer
running. Live evidence is collected separately and is never replaced by this
file; every simulated result says `mode: simulated`.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from ..adapter.base import AdapterInfo
from ..compiler.compiler import compile_spec
from ..compiler.dag import OperationDag
from ..core.actual_state import ActualNetworkState, DeviceObservation, PortObservation
from ..core.ids import new_id, utc_now_iso
from ..core.network_spec import (
    DeviceRole,
    FlowTestSpec,
    NetworkSpec,
    default_model_for_role,
)
from ..core.observability import Observable
from ..core.receipts import CheckResult, OperationReceipt, OperationResult
from ..core.requirements import FlowOutcome, FlowResult
from ..ipam.planner import AddressPlan, plan_addressing
from ..pt.catalog import is_logical_port, resolve_model
from .config_reader import ConfigCorpus
from .importer import media_for_interface
from .models import (
    BrownfieldInventory,
    ConfigExtract,
    ConfigStatus,
    DeviceInventory,
    HostInventory,
    InterfaceInventory,
    LinkInventory,
    Platform,
    platform_for_category,
    role_for_category,
)
from .parser import IosConfigParser

_ROLE_TO_CATEGORY = {
    "router": "router",
    "switch": "switch",
    "l3_switch": "switch",
    "pc": "pc",
    "server": "server",
    "laptop": "laptop",
    "access_point": "accesspoint",
    "cloud": "cloud",
    "firewall": "firewall",
    "wireless_router": "wireless_router",
    "modem": "modem",
    "iot": "iot",
    "printer": "printer",
    "ip_phone": "ip_phone",
    "tablet": "tablet",
    "smartphone": "smartphone",
    "wlc": "wlc",
    "other": "end_device",
}


@dataclass
class SimInterface:
    name: str
    ip: str | None = None
    mask: str | None = None
    shutdown: bool = False
    description: str | None = None
    switchport_mode: str | None = None
    access_vlan: int | None = None
    voice_vlan: int | None = None
    trunk_native: int | None = None
    trunk_allowed: list[int] | None = None
    encapsulation_vlan: int | None = None
    helpers: list[str] = field(default_factory=list)
    acl_in: str | None = None
    acl_out: str | None = None
    nameif: str | None = None
    security_level: int | None = None
    standby: list[dict[str, Any]] = field(default_factory=list)
    linked: bool = False
    oper_up: bool = True
    peers: list[str] = field(default_factory=list)
    peer_ports: list[str] = field(default_factory=list)
    link_class: str | None = None


@dataclass
class SimDevice:
    name: str
    category: str
    model: str
    role: str
    interfaces: dict[str, SimInterface] = field(default_factory=dict)
    host: dict[str, Any] | None = None
    vlans: dict[int, str] = field(default_factory=dict)
    routes: list[dict[str, Any]] = field(default_factory=list)
    static_routes: list[dict[str, Any]] = field(default_factory=list)
    acls: dict[str, list[str]] = field(default_factory=dict)
    acl_bindings: list[dict[str, Any]] = field(default_factory=list)
    dhcp_pools: dict[str, dict[str, Any]] = field(default_factory=dict)
    ip_routing: bool = False
    default_gateway: str | None = None
    stp: list[dict[str, Any]] = field(default_factory=list)
    position: tuple[int, int] = (0, 0)

    @property
    def is_host(self) -> bool:
        return self.category in (
            "pc", "laptop", "server", "printer", "ip_phone",
            "end_device", "tablet", "smartphone",
        )


class SimulatedNetwork:
    """A compilable network plus the IOS-shaped command interpreter."""

    def __init__(self, spec: NetworkSpec):
        self.spec = spec
        self.plan: AddressPlan = plan_addressing(spec)
        self.compiled = compile_spec(spec, self.plan)
        self.devices: dict[str, SimDevice] = {}
        self.links: list[dict[str, Any]] = []
        self._build()
        self._baseline_commands = {
            name: list(self._commands_for(name)) for name in self.devices
        }

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        for index, device in enumerate(self.spec.devices):
            role = device.role.value
            category = _ROLE_TO_CATEGORY.get(role, "end_device")
            model_name = device.model_hint or default_model_for_role(device.role)
            model = resolve_model(model_name)
            interfaces: dict[str, SimInterface] = {}
            if model is not None:
                for port in model.ports:
                    interfaces[port.full_name] = SimInterface(name=port.full_name)
            if not interfaces:
                interfaces["FastEthernet0"] = SimInterface(name="FastEthernet0")
            self.devices[device.id] = SimDevice(
                name=device.id,
                category=category,
                model=model_name,
                role=role,
                interfaces=interfaces,
                position=(100 + (index % 6) * 120, 100 + (index // 6) * 120),
            )
        # Links: the compiler solved concrete ports; that is the real topology.
        for node in self.compiled.dag.nodes.values():
            op = node.operation
            if op.operation_type != "create_link":
                continue
            a_dev = str(op.inputs["a_device"])
            a_port = str(op.inputs["a_port"])
            b_dev = str(op.inputs["b_device"])
            b_port = str(op.inputs["b_port"])
            if a_dev not in self.devices or b_dev not in self.devices:
                continue
            self.devices[a_dev].interfaces.setdefault(a_port, SimInterface(a_port))
            self.devices[b_dev].interfaces.setdefault(b_port, SimInterface(b_port))
            self.devices[a_dev].interfaces[a_port].linked = True
            self.devices[b_dev].interfaces[b_port].linked = True
            self.devices[a_dev].interfaces[a_port].peers = [b_dev]
            self.devices[b_dev].interfaces[b_port].peers = [a_dev]
            self.devices[a_dev].interfaces[a_port].peer_ports = [b_port]
            self.devices[b_dev].interfaces[b_port].peer_ports = [a_port]
            self.devices[a_dev].interfaces[a_port].link_class = "Cable"
            self.devices[b_dev].interfaces[b_port].link_class = "Cable"
            self.links.append(
                {
                    "a_device": a_dev, "a_port": a_port,
                    "b_device": b_dev, "b_port": b_port,
                }
            )
        # Configuration: apply the compiler's own commands through the same
        # interpreter a repair uses, so the baseline is self-consistent.
        for node in self.compiled.dag.nodes.values():
            op = node.operation
            if op.operation_type != "configure_ios":
                continue
            device = str(op.inputs.get("device") or "")
            if device in self.devices:
                self.apply_ios(device, [str(c) for c in op.inputs.get("commands") or []])
        # Host addressing from the plan (the compiler emits configure_host ops).
        used_ips = {
            str(a.ip) for a in self.plan.assignments if a.ip
        }
        dhcp_next: dict[str, int] = {}
        for assignment in self.plan.assignments:
            device = self.devices.get(assignment.device)
            if device is None or not device.is_host:
                continue
            allocation = self.plan.allocation_by_subnet(assignment.subnet_id)
            ip = assignment.ip
            dhcp = assignment.dhcp
            if dhcp and allocation is not None:
                # A deterministic simulated lease: distinct per client and
                # never an address a static assignment already holds. Giving
                # every client the same address made two hosts
                # indistinguishable and hid path faults in the double.
                offset = dhcp_next.get(assignment.subnet_id, 2)
                candidate = str(ipaddress.IPv4Address(allocation.gateway) + offset)
                while candidate in used_ips:
                    offset += 1
                    candidate = str(ipaddress.IPv4Address(allocation.gateway) + offset)
                dhcp_next[assignment.subnet_id] = offset + 1
                used_ips.add(candidate)
                ip = candidate
            dns = next(
                (d.dns_server for d in self.spec.host_dns if d.device == device.name),
                None,
            )
            device.host = {
                "interface": assignment.interface_hint or "FastEthernet0",
                "ip": ip,
                "mask": allocation.netmask if allocation else None,
                "gateway": assignment.gateway or (allocation.gateway if allocation else None),
                "dns": [dns] if dns else [],
                "dhcp": bool(dhcp),
                "dhcp_server": (
                    allocation.gateway if dhcp and allocation else None
                ),
            }
            port = device.interfaces.setdefault(
                assignment.interface_hint or "FastEthernet0",
                SimInterface(assignment.interface_hint or "FastEthernet0"),
            )
            if ip:
                port.ip = ip
                port.mask = allocation.netmask if allocation else None
        for vlan in self.spec.vlans:
            for device in self.devices.values():
                if device.category == "switch":
                    device.vlans.setdefault(vlan.vlan_id, vlan.name)
        # The compiler does not emit addresses for every L3-switch routed
        # port (a known pre-Stage-4 gap); the simulated network still models
        # the plan so Stage 4 diagnosis is exercised on the intent, not on a
        # half-addressed double.
        for assignment in self.plan.assignments:
            device = self.devices.get(assignment.device)
            if device is None or device.is_host or not assignment.ip:
                continue
            interface_name = assignment.interface_hint or self.compiled.assignment_ports.get(
                (assignment.device, assignment.subnet_id)
            )
            if not interface_name:
                continue
            allocation = self.plan.allocation_by_subnet(assignment.subnet_id)
            interface = device.interfaces.setdefault(
                interface_name, SimInterface(interface_name)
            )
            if not interface.ip:
                interface.ip = assignment.ip
                interface.mask = allocation.netmask if allocation else None
        for route in self.spec.routing.static_routes:
            device = self.devices.get(route.device)
            if device is None:
                continue
            try:
                network = ipaddress.IPv4Network(route.destination, strict=False)
            except ValueError:
                continue
            addition = {
                "network": str(network.network_address),
                "mask": str(network.netmask),
            }
            if route.next_hop:
                addition["next_hop"] = route.next_hop
            if route.exit_interface:
                addition["interface"] = route.exit_interface
            if not any(_same_route(existing, addition) for existing in device.static_routes):
                device.static_routes.append(addition)
        self._rebuild_routes()

    def _commands_for(self, device: str) -> list[str]:
        for node in self.compiled.dag.nodes.values():
            op = node.operation
            if (
                op.operation_type == "configure_ios"
                and str(op.inputs.get("device")) == device
            ):
                return [str(c) for c in op.inputs.get("commands") or []]
        return []

    # -- command interpreter -----------------------------------------------

    def _platform(self, device_name: str) -> str:
        device = self.devices.get(device_name)
        return "asa" if device is not None and device.category == "firewall" else "ios"

    def apply_ios(self, device_name: str, commands: Iterable[str]) -> list[str]:
        device = self.devices[device_name]
        block_kind: str | None = None
        block_name: str | None = None
        unapplied: list[str] = []
        pool_name: str | None = None
        for raw in commands:
            command = str(raw).strip()
            if not command:
                continue
            lowered = command.lower()
            if lowered == "exit":
                block_kind = block_name = pool_name = None
                continue
            if lowered.startswith("hostname "):
                continue
            if lowered.startswith("interface "):
                block_kind, block_name = "interface", command.split(None, 1)[1]
                device.interfaces.setdefault(block_name, SimInterface(block_name))
                continue
            if re.match(r"^vlan\s+\d+$", lowered):
                block_kind, block_name = "vlan", lowered.split()[-1]
                device.vlans.setdefault(int(block_name), f"VLAN{int(block_name):04d}")
                continue
            if lowered.startswith("no vlan "):
                try:
                    device.vlans.pop(int(lowered.split()[-1]), None)
                except ValueError:
                    pass
                continue
            if lowered.startswith("ip dhcp pool "):
                pool_name = command.split(None, 3)[3]
                device.dhcp_pools.setdefault(
                    pool_name, {"network": None, "mask": None, "default_router": []}
                )
                block_kind, block_name = "pool", pool_name
                continue
            if lowered.startswith("no ip dhcp pool "):
                device.dhcp_pools.pop(command.split(None, 4)[4], None)
                continue
            if lowered == "ip routing":
                device.ip_routing = True
                continue
            if lowered == "no ip routing":
                device.ip_routing = False
                continue
            if lowered.startswith("ip route "):
                route = _parse_route(command)
                if route is not None and route not in device.static_routes:
                    device.static_routes.append(route)
                continue
            if lowered.startswith("no ip route "):
                route = _parse_route(command[3:])
                if route is not None:
                    device.static_routes = [
                        r for r in device.static_routes
                        if not _same_route(r, route)
                    ]
                continue
            if lowered.startswith("route ") and self._platform(device_name) == "asa":
                # route outside 0.0.0.0 0.0.0.0 203.0.113.2 1
                parts = command.split()
                if len(parts) >= 5 and parts[1] in ("inside", "outside", "dmz"):
                    route = _parse_route(" ".join(parts[2:]))
                    if route is not None and route not in device.static_routes:
                        device.static_routes.append(route)
                continue
            if lowered.startswith("no route ") and self._platform(device_name) == "asa":
                parts = command.split()
                if len(parts) >= 5 and parts[1] in ("inside", "outside", "dmz"):
                    route = _parse_route(" ".join(parts[2:]))
                    if route is not None:
                        device.static_routes = [
                            r for r in device.static_routes
                            if not _same_route(r, route)
                        ]
                continue
            if lowered.startswith("access-group "):
                parts = command.split()
                binding = {"name": parts[1]}
                if len(parts) >= 4:
                    binding["direction"] = parts[2]
                if "interface" in [p.lower() for p in parts]:
                    index = [p.lower() for p in parts].index("interface")
                    if index + 1 < len(parts):
                        binding["interface"] = parts[index + 1]
                device.acl_bindings.append(binding)
                continue
            if lowered.startswith("no access-group "):
                name = command.split()[2]
                device.acl_bindings = [
                    b for b in device.acl_bindings if b.get("name") != name
                ]
                for interface in device.interfaces.values():
                    if interface.acl_in == name:
                        interface.acl_in = None
                    if interface.acl_out == name:
                        interface.acl_out = None
                continue
            if lowered.startswith("access-list "):
                parts = command.split(None, 2)
                name = parts[1]
                device.acls.setdefault(name, []).append(command.split(None, 2)[2])
                continue
            if lowered.startswith("no access-list "):
                tokens = command.split(None, 3)
                name = tokens[2]
                if len(tokens) > 3:
                    body = tokens[3]
                    device.acls[name] = [
                        rule for rule in device.acls.get(name, [])
                        if rule.strip() != body.strip()
                    ]
                else:
                    device.acls.pop(name, None)
                continue
            if lowered.startswith("ip default-gateway "):
                device.default_gateway = command.split()[-1]
                continue
            interface = device.interfaces.get(block_name) if block_kind == "interface" and block_name else None
            if block_kind == "vlan" and block_name and lowered.startswith("name "):
                device.vlans[int(block_name)] = command.split(None, 1)[1]
                continue
            if block_kind == "pool" and pool_name:
                pool = device.dhcp_pools.setdefault(
                    pool_name, {"network": None, "mask": None, "default_router": []}
                )
                if lowered.startswith("network "):
                    parts = command.split()
                    if len(parts) >= 3:
                        pool["network"], pool["mask"] = parts[1], parts[2]
                elif lowered.startswith("default-router "):
                    pool["default_router"] = command.split()[1:]
                elif lowered.startswith("dns-server "):
                    pool["dns"] = command.split()[1:]
                continue
            if interface is None:
                unapplied.append(command)
                continue
            if lowered == "shutdown":
                interface.shutdown = True
                continue
            if lowered == "no shutdown":
                interface.shutdown = False
                continue
            if lowered.startswith("ip address "):
                parts = command.split()
                if len(parts) >= 4:
                    interface.ip, interface.mask = parts[2], parts[3]
                continue
            if lowered == "no ip address":
                interface.ip = interface.mask = None
                continue
            if lowered.startswith("description "):
                interface.description = command.split(None, 1)[1]
                continue
            if lowered.startswith("switchport mode "):
                interface.switchport_mode = lowered.split()[-1]
                continue
            if lowered.startswith("switchport access vlan "):
                try:
                    interface.access_vlan = int(lowered.split()[-1])
                except ValueError:
                    pass
                continue
            if lowered.startswith("switchport voice vlan "):
                interface.voice_vlan = int(lowered.split()[-1])
                continue
            if lowered.startswith("switchport trunk native vlan "):
                interface.trunk_native = int(lowered.split()[-1])
                continue
            if lowered.startswith("no switchport trunk native vlan"):
                interface.trunk_native = None
                continue
            if lowered.startswith("switchport trunk allowed vlan "):
                rest = lowered.split("vlan ", 1)[1]
                action = "set"
                if rest.startswith("add "):
                    action, rest = "add", rest[4:]
                elif rest.startswith("remove "):
                    action, rest = "remove", rest[7:]
                values = _vlan_list(rest)
                if action == "set":
                    interface.trunk_allowed = values
                elif action == "add":
                    interface.trunk_allowed = sorted(
                        set(interface.trunk_allowed or []) | set(values)
                    )
                elif action == "remove":
                    interface.trunk_allowed = sorted(
                        set(interface.trunk_allowed or []) - set(values)
                    )
                continue
            if lowered.startswith("encapsulation dot1q "):
                interface.encapsulation_vlan = int(lowered.split()[-1])
                continue
            if lowered.startswith("ip helper-address "):
                interface.helpers.append(command.split()[-1])
                continue
            if lowered.startswith("ip access-group "):
                parts = command.split()
                if len(parts) >= 4:
                    if parts[3].lower() == "in":
                        interface.acl_in = parts[2]
                    else:
                        interface.acl_out = parts[2]
                continue
            if lowered.startswith("no ip access-group "):
                interface.acl_in = interface.acl_out = None
                continue
            if lowered.startswith("nameif "):
                interface.nameif = command.split(None, 1)[1]
                continue
            if lowered.startswith("security-level "):
                interface.security_level = int(command.split()[-1])
                continue
            if lowered.startswith("ip nat inside"):
                continue
            if lowered.startswith("ip nat outside"):
                continue
            if lowered.startswith("standby "):
                parts = command.split()
                try:
                    group = int(parts[1])
                except (IndexError, ValueError):
                    continue
                record = next(
                    (r for r in interface.standby if r.get("group") == group), None
                )
                if record is None:
                    record = {"group": group}
                    interface.standby.append(record)
                if len(parts) >= 4 and parts[2].lower() == "ip":
                    record["ip"] = parts[3]
                elif lowered.startswith("standby ") and "priority " in lowered:
                    record["priority"] = int(parts[-1])
                elif lowered.endswith(" preempt"):
                    record["preempt"] = True
                continue
            if lowered.startswith("spanning-tree portfast"):
                continue
            if lowered.startswith("spanning-tree bpduguard"):
                continue
            unapplied.append(command)
        self._rebuild_routes()
        return unapplied

    def apply_host(self, device_name: str, config: dict[str, Any]) -> None:
        device = self.devices[device_name]
        host = device.host or {
            "interface": "FastEthernet0",
            "ip": None,
            "mask": None,
            "gateway": None,
            "dns": [],
            "dhcp": False,
            "dhcp_server": None,
        }
        for key in ("ip", "mask", "gateway", "dhcp"):
            if key in config and config[key] is not None:
                host[key] = config[key]
        if config.get("dns") is not None:
            dns = config["dns"]
            host["dns"] = [dns] if isinstance(dns, str) else list(dns)
        device.host = host
        port = device.interfaces.setdefault(
            host.get("interface") or "FastEthernet0",
            SimInterface(host.get("interface") or "FastEthernet0"),
        )
        if config.get("ip") is not None:
            port.ip = config["ip"]
        if config.get("mask") is not None:
            port.mask = config["mask"]

    def _rebuild_routes(self) -> None:
        for device in self.devices.values():
            routes: list[dict[str, Any]] = []
            for interface in device.interfaces.values():
                if interface.ip and interface.mask:
                    try:
                        network = ipaddress.IPv4Network(
                            f"{interface.ip}/{interface.mask}", strict=False
                        )
                    except ValueError:
                        continue
                    routes.append(
                        {
                            "kind": "connected",
                            "network": str(network.network_address),
                            "prefix": network.prefixlen,
                            "next_hop": None,
                            "interface": interface.name,
                        }
                    )
            for route in device.static_routes:
                try:
                    network = ipaddress.IPv4Network(
                        f"{route['network']}/{route['mask']}", strict=False
                    )
                except ValueError:
                    continue
                routes.append(
                    {
                        "kind": "default" if network.prefixlen == 0 else "static",
                        "network": str(network.network_address),
                        "prefix": network.prefixlen,
                        "next_hop": route.get("next_hop"),
                        "interface": route.get("interface"),
                    }
                )
            device.routes = routes
        # Dynamic protocols: a converged simulated network knows every subnet.
        if self.spec.routing.mode in ("ospf", "eigrp", "rip", "bgp"):
            subnets = []
            for subnet in self.spec.subnets:
                if subnet.cidr:
                    try:
                        subnets.append(ipaddress.IPv4Network(subnet.cidr, strict=False))
                    except ValueError:
                        continue
            for device in self.devices.values():
                if device.category != "router" and not (
                    device.role == "l3_switch" or device.ip_routing
                ):
                    continue
                for network in subnets:
                    already = any(
                        row["network"] == str(network.network_address)
                        and int(row["prefix"]) == network.prefixlen
                        for row in device.routes
                    )
                    if already:
                        continue
                    if not any(
                        int(row["prefix"]) >= network.prefixlen
                        and ipaddress.IPv4Network(
                            f"{row['network']}/{row['prefix']}", strict=False
                        ).overlaps(network)
                        for row in device.routes
                        if row["kind"] in ("connected", "local")
                    ):
                        device.routes.append(
                            {
                                "kind": self.spec.routing.mode,
                                "network": str(network.network_address),
                                "prefix": network.prefixlen,
                                "next_hop": None,
                                "interface": None,
                            }
                        )

    # -- configuration rendering -------------------------------------------

    def render_config(self, device_name: str) -> str:
        device = self.devices[device_name]
        lines = [f"hostname {device.name}", "!"]
        if device.ip_routing:
            lines.append("ip routing")
        for vlan_id, name in sorted(device.vlans.items()):
            lines.extend([f"vlan {vlan_id}", f" name {name}", "exit"])
        for interface in device.interfaces.values():
            if not _interface_has_config(interface):
                continue
            lines.append(f"interface {interface.name}")
            if interface.description:
                lines.append(f" description {interface.description}")
            if interface.ip and interface.mask:
                lines.append(f" ip address {interface.ip} {interface.mask}")
            if interface.encapsulation_vlan is not None:
                lines.append(f" encapsulation dot1Q {interface.encapsulation_vlan}")
            if interface.switchport_mode:
                lines.append(f" switchport mode {interface.switchport_mode}")
            if interface.access_vlan is not None:
                lines.append(f" switchport access vlan {interface.access_vlan}")
            if interface.voice_vlan is not None:
                lines.append(f" switchport voice vlan {interface.voice_vlan}")
            if interface.trunk_native is not None:
                lines.append(f" switchport trunk native vlan {interface.trunk_native}")
            if interface.trunk_allowed is not None:
                lines.append(
                    " switchport trunk allowed vlan "
                    + ",".join(str(v) for v in interface.trunk_allowed)
                )
            if interface.helpers:
                for helper in interface.helpers:
                    lines.append(f" ip helper-address {helper}")
            if interface.acl_in:
                lines.append(f" ip access-group {interface.acl_in} in")
            if interface.acl_out:
                lines.append(f" ip access-group {interface.acl_out} out")
            if interface.nameif:
                lines.append(f" nameif {interface.nameif}")
            if interface.security_level is not None:
                lines.append(f" security-level {interface.security_level}")
            for record in interface.standby:
                if record.get("ip"):
                    lines.append(f" standby {record['group']} ip {record['ip']}")
                if record.get("priority"):
                    lines.append(f" standby {record['group']} priority {record['priority']}")
                if record.get("preempt"):
                    lines.append(f" standby {record['group']} preempt")
            lines.append(" shutdown" if interface.shutdown else " no shutdown")
            lines.append("exit")
        for route in device.static_routes:
            lines.append(
                f"ip route {route['network']} {route['mask']} "
                f"{route.get('next_hop') or route.get('interface') or ''}".strip()
            )
        for name, rules in device.acls.items():
            for rule in rules:
                lines.append(f"access-list {name} {rule}")
        for binding in device.acl_bindings:
            prefix = f"access-group {binding.get('name')}"
            if binding.get("direction"):
                prefix += f" {binding['direction']}"
            if binding.get("interface"):
                prefix += f" interface {binding['interface']}"
            lines.append(prefix)
        for name, pool in device.dhcp_pools.items():
            lines.extend([f"ip dhcp pool {name}"])
            if pool.get("network"):
                lines.append(f" network {pool['network']} {pool['mask']}")
            if pool.get("default_router"):
                lines.append(f" default-router {' '.join(pool['default_router'])}")
            if pool.get("dns"):
                lines.append(f" dns-server {' '.join(pool['dns'])}")
            lines.append("exit")
        if device.default_gateway:
            lines.append(f"ip default-gateway {device.default_gateway}")
        lines.extend(["!", "end"])
        return "\n".join(lines)

    # -- presentation -------------------------------------------------------

    def inventory(self) -> BrownfieldInventory:
        inventory = BrownfieldInventory(
            name=self.spec.name,
            source="simulated",
            observed_at=utc_now_iso(),
        )
        for device_name, device in sorted(self.devices.items()):
            observation = DeviceInventory(
                name=device_name,
                category=device.category,
                model=Observable.known(device.model, source="simulated"),
                role=Observable.known(device.role, source="simulated"),
                position=Observable.known(device.position, source="simulated"),
                platform=platform_for_category(device.category),
                source="simulated",
            )
            for name, interface in device.interfaces.items():
                media = media_for_interface(name, interface.link_class)
                observation.interfaces[name] = InterfaceInventory(
                    name=name,
                    media=(
                        Observable.known(media, source="simulated")
                        if media else Observable[str].unknown("unknown medium")
                    ),
                    ip=Observable.from_raw(interface.ip or None, source="simulated"),
                    mask=Observable.from_raw(interface.mask or None, source="simulated"),
                    state=Observable.known(
                        "up"
                        if interface.linked and not interface.shutdown and interface.oper_up
                        else "down",
                        source="simulated",
                    ),
                    linked=Observable.known(interface.linked, source="simulated"),
                    admin_state=Observable.known(
                        "shutdown" if interface.shutdown else "up", source="simulated"
                    ),
                    description=Observable.from_raw(
                        interface.description, source="simulated"
                    ),
                    vlan=(
                        Observable.known(interface.access_vlan, source="simulated")
                        if interface.access_vlan is not None
                        else Observable.not_observed()
                    ),
                    trunk=(
                        Observable.known(
                            interface.switchport_mode == "trunk", source="simulated"
                        )
                        if interface.switchport_mode is not None
                        else Observable.not_observed()
                    ),
                    allowed_vlans=(
                        Observable.known(
                            list(interface.trunk_allowed), source="simulated"
                        )
                        if interface.trunk_allowed is not None
                        else Observable.not_observed()
                    ),
                    native_vlan=(
                        Observable.known(interface.trunk_native, source="simulated")
                        if interface.trunk_native is not None
                        else Observable.not_observed()
                    ),
                    voice_vlan=(
                        Observable.known(interface.voice_vlan, source="simulated")
                        if interface.voice_vlan is not None
                        else Observable.not_observed()
                    ),
                    switchport_mode=Observable.from_raw(
                        interface.switchport_mode, source="simulated"
                    ),
                    peers=(
                        Observable.known(list(interface.peers), source="simulated")
                        if interface.peers
                        else Observable.not_observed()
                    ),
                    peer_ports=(
                        Observable.known(list(interface.peer_ports), source="simulated")
                        if interface.peer_ports
                        else Observable.not_observed()
                    ),
                    link_class=Observable.from_raw(
                        interface.link_class, source="simulated"
                    ),
                    subinterface_of=(
                        interface.name.split(".", 1)[0]
                        if "." in interface.name else None
                    ),
                    encapsulation_vlan=(
                        Observable.known(interface.encapsulation_vlan, source="simulated")
                        if interface.encapsulation_vlan is not None
                        else Observable.not_observed()
                    ),
                    acl_in=Observable.from_raw(interface.acl_in, source="simulated"),
                    acl_out=Observable.from_raw(interface.acl_out, source="simulated"),
                    nameif=Observable.from_raw(interface.nameif, source="simulated"),
                    security_level=(
                        Observable.known(interface.security_level, source="simulated")
                        if interface.security_level is not None
                        else Observable.not_observed()
                    ),
                    helpers=(
                        Observable.known(list(interface.helpers), source="simulated")
                        if interface.helpers else Observable.not_observed()
                    ),
                )
            if device.host is not None:
                host = device.host
                observation.host = HostInventory(
                    ip=Observable.from_raw(host.get("ip"), source="simulated"),
                    mask=Observable.from_raw(host.get("mask"), source="simulated"),
                    gateway=Observable.from_raw(host.get("gateway"), source="simulated"),
                    dns=(
                        Observable.known(list(host.get("dns") or []), source="simulated")
                        if host.get("dns") else Observable.not_observed()
                    ),
                    dhcp=(
                        Observable.known(bool(host.get("dhcp")), source="simulated")
                    ),
                    dhcp_server=Observable.from_raw(
                        host.get("dhcp_server"), source="simulated"
                    ),
                    interface=Observable.from_raw(
                        host.get("interface"), source="simulated"
                    ),
                )
                observation.default_gateway = observation.host.gateway
                observation.dns = observation.host.dns
            if device.routes:
                observation.routes = Observable.known(
                    list(device.routes), source="simulated"
                )
            if device.vlans:
                observation.vlans_table = Observable.known(
                    [
                        {
                            "id": vlan_id,
                            "name": name,
                            "status": "active",
                            "ports": sorted(
                                interface.name
                                for interface in device.interfaces.values()
                                if interface.access_vlan == vlan_id
                            ),
                        }
                        for vlan_id, name in sorted(device.vlans.items())
                    ],
                    source="simulated",
                )
            trunk_map = {
                interface.name: list(interface.trunk_allowed)
                for interface in device.interfaces.values()
                if interface.switchport_mode == "trunk"
                and interface.trunk_allowed is not None
            }
            if trunk_map:
                observation.trunk_allowed = Observable.known(
                    trunk_map, source="simulated"
                )
                for name, allowed in trunk_map.items():
                    interface = observation.interfaces.get(name)
                    if interface is not None:
                        interface.allowed_vlans = Observable.known(
                            list(allowed), source="simulated"
                        )
                        interface.trunk = Observable.known(True, source="simulated")
            observation.stp = Observable.known(list(device.stp), source="simulated")
            if not device.is_host:
                raw = self.render_config(device_name)
                observation.config = ConfigExtract(
                    device=device_name,
                    platform=observation.platform,
                    status=ConfigStatus.KNOWN,
                    raw=raw,
                    attempts=1,
                    detail=None,
                    sha256=__import__("hashlib").sha256(
                        raw.encode("utf-8")
                    ).hexdigest(),
                    read_at=utc_now_iso(),
                    source="simulated",
                )
            inventory.devices[device_name] = observation
        for link in self.links:
            inventory.links.append(
                LinkInventory(
                    id=_link_id(link),
                    a_device=link["a_device"], a_port=link["a_port"],
                    b_device=link["b_device"], b_port=link["b_port"],
                    media=Observable.known("ethernet", source="simulated"),
                    link_class=Observable.known("Cable", source="simulated"),
                )
            )
        return inventory

    def corpus(self, inventory: BrownfieldInventory | None = None) -> ConfigCorpus:
        inventory = inventory or self.inventory()
        corpus = ConfigCorpus()
        parser = IosConfigParser()
        for name, device in inventory.devices.items():
            if device.config is None or not device.config.raw:
                continue
            corpus.extracts[name] = device.config
            corpus.parsed[name] = parser.parse(
                device.config.raw, device=name, platform=device.platform
            )
        return corpus


class SimulatedAdapter:
    """The adapter surface over a `SimulatedNetwork`."""

    name = "simulated"
    version = "1.0"

    def __init__(self, spec: NetworkSpec):
        self.network = SimulatedNetwork(spec)
        self._evidence: list[dict[str, Any]] = []

    # -- inspection ---------------------------------------------------------

    def ping(self, timeout: float = 5.0) -> AdapterInfo:
        return AdapterInfo(
            adapter_name=self.name, adapter_version=self.version,
            reachable=True,
            detail=Observable.known("deterministic simulated network", source="simulated"),
        )

    def device_exists(self, name: str) -> Observable[bool]:
        return Observable.known(name in self.network.devices, source="simulated")

    def list_device_names(self) -> Observable[list[str]]:
        return Observable.known(sorted(self.network.devices), source="simulated")

    def link_exists(
        self, a_device: str, a_port: str, b_device: str, b_port: str
    ) -> Observable[bool]:
        for link in self.network.links:
            if (
                link["a_device"] == a_device and link["a_port"] == a_port
                and link["b_device"] == b_device and link["b_port"] == b_port
            ) or (
                link["b_device"] == a_device and link["b_port"] == a_port
                and link["a_device"] == b_device and link["a_port"] == b_port
            ):
                return Observable.known(True, source="simulated")
        return Observable.known(False, source="simulated")

    def inspect_device(self, name: str) -> DeviceObservation:
        inventory = self.network.inventory()
        device = inventory.devices.get(name)
        if device is None:
            return DeviceObservation(name=name, notes=["not present"])
        observation = DeviceObservation(
            name=name, model=device.model, role=device.role
        )
        for interface_name, interface in device.interfaces.items():
            observation.ports[interface_name] = PortObservation(
                name=interface_name,
                state=interface.state,
                linked=interface.linked,
                ip=interface.ip,
                mask=interface.mask,
                gateway=Observable.not_observed(),
                vlan=interface.vlan,
                trunk=interface.trunk,
                description=interface.description,
                peers=interface.peers,
                peer_ports=interface.peer_ports,
                link_class=interface.link_class,
            )
        return observation

    def observe_state(self, names=None, *, observed_at=None) -> ActualNetworkState:
        from ..core.ids import utc_now_iso as _now

        state = ActualNetworkState(
            source="adapter:simulated", observed_at=observed_at or _now()
        )
        inventory = self.network.inventory()
        for name in (names if names is not None else inventory.device_names()):
            device = inventory.devices.get(name)
            if device is None:
                state.metadata[f"absent:{name}"] = Observable.known(
                    "absent", source="simulated"
                )
                continue
            state.observe_device(self.inspect_device(name))
        return state

    def read_running_config(self, device: str) -> Observable[str]:
        item = self.network.devices.get(device)
        if item is None:
            return Observable[str].unknown("device not in simulated network")
        if item.is_host:
            return Observable[str].unsupported("host has no IOS configuration")
        return Observable.known(self.network.render_config(device), source="simulated")

    def read_host_ip_config(self, device: str) -> Observable[dict]:
        item = self.network.devices.get(device)
        if item is None or item.host is None:
            return Observable[dict].unknown("no host configuration")
        return Observable.known(dict(item.host), source="simulated")

    def read_device_positions(self) -> Observable[dict[str, tuple[int, int]]]:
        return Observable.known(
            {name: device.position for name, device in self.network.devices.items()},
            source="simulated",
        )

    def read_routing_table(self, device: str) -> Observable[list]:
        item = self.network.devices.get(device)
        if item is None:
            return Observable[list].unknown("device not in simulated network")
        if item.is_host:
            return Observable[list].unsupported("host has no routing table")
        return Observable.known(list(item.routes), source="simulated")

    def read_vlans(self, device: str) -> Observable[list]:
        item = self.network.devices.get(device)
        if item is None or item.category != "switch":
            return Observable[list].unsupported("not a switch")
        return Observable.known(
            [
                {
                    "id": vlan_id,
                    "name": name,
                    "ports": sorted(
                        i.name for i in item.interfaces.values()
                        if i.access_vlan == vlan_id
                    ),
                }
                for vlan_id, name in sorted(item.vlans.items())
            ],
            source="simulated",
        )

    def read_trunk_allowed(self, device: str) -> Observable[dict]:
        item = self.network.devices.get(device)
        if item is None or item.category != "switch":
            return Observable[dict].unsupported("not a switch")
        return Observable.known(
            {
                i.name: list(i.trunk_allowed)
                for i in item.interfaces.values()
                if i.switchport_mode == "trunk" and i.trunk_allowed is not None
            },
            source="simulated",
        )

    def read_spanning_tree(self, device: str) -> Observable[list]:
        item = self.network.devices.get(device)
        if item is None or item.category != "switch":
            return Observable[list].unsupported("not a switch")
        return Observable.known(list(item.stp), source="simulated")

    def read_arp_table(self, device: str) -> Observable[list]:
        item = self.network.devices.get(device)
        if item is None:
            return Observable[list].unknown("device not in simulated network")
        rows = []
        for interface in item.interfaces.values():
            for peer in interface.peers:
                peer_device = self.network.devices.get(peer)
                if peer_device is None:
                    continue
                for peer_iface in peer_device.interfaces.values():
                    if peer_iface.ip:
                        rows.append(
                            {
                                "ip": peer_iface.ip,
                                "mac": "0000.0000.0000",
                                "type": "ARPA",
                                "interface": interface.name,
                            }
                        )
        if not rows:
            return Observable[list].unknown("no neighbours in ARP cache")
        return Observable.known(rows, source="simulated")

    # -- mutations ----------------------------------------------------------

    def _receipt(self, operation: str, requested: dict, result=OperationResult.SUCCESS, error=None):
        return OperationReceipt(
            operation_id=new_id("sim"),
            operation=operation,
            requested=requested,
            executed={"simulated": True},
            result=result,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def configure_ios(self, device: str, commands: list[str], operation_id=None):
        if device not in self.network.devices:
            return self._receipt("configure_ios", {"device": device}, OperationResult.REJECTED, "device missing")
        self.network.apply_ios(device, commands)
        return self._receipt("configure_ios", {"device": device, "commands": commands})

    def configure_host(self, device: str, *, dhcp: bool, ip=None, mask=None, gateway=None, dns=None, operation_id=None):
        if device not in self.network.devices:
            return self._receipt("configure_host", {"device": device}, OperationResult.REJECTED, "device missing")
        self.network.apply_host(
            device,
            {"dhcp": dhcp, "ip": ip, "mask": mask, "gateway": gateway, "dns": dns},
        )
        return self._receipt("configure_host", {"device": device})

    def configure_gateway_lan(self, device: str, ip: str, mask: str, operation_id=None):
        self.network.apply_host(device, {"ip": ip, "mask": mask, "dhcp": False})
        return self._receipt("configure_gateway_lan", {"device": device})

    def create_device(self, name, role, model_hint=None, position=None, operation_id=None):
        if name in self.network.devices:
            return self._receipt("create_device", {"name": name}, OperationResult.REJECTED, "exists")
        category = _ROLE_TO_CATEGORY.get(str(role), "end_device")
        model = model_hint or default_model_for_role(role)
        interfaces = {}
        resolved = resolve_model(model)
        if resolved is not None:
            for port in resolved.ports:
                interfaces[port.full_name] = SimInterface(port.full_name)
        if not interfaces:
            interfaces["FastEthernet0"] = SimInterface("FastEthernet0")
        self.network.devices[name] = SimDevice(
            name=name, category=category, model=model, role=str(role),
            interfaces=interfaces, position=position or (0, 0),
        )
        return self._receipt("create_device", {"name": name})

    def create_link(self, a_device, a_port, b_device, b_port, media="auto", operation_id=None):
        if a_device not in self.network.devices or b_device not in self.network.devices:
            return self._receipt("create_link", {}, OperationResult.REJECTED, "device missing")
        a = self.network.devices[a_device].interfaces.setdefault(a_port, SimInterface(a_port))
        b = self.network.devices[b_device].interfaces.setdefault(b_port, SimInterface(b_port))
        a.linked = b.linked = True
        a.peers, a.peer_ports = [b_device], [b_port]
        b.peers, b.peer_ports = [a_device], [a_port]
        a.link_class = b.link_class = "Cable"
        self.network.links.append(
            {"a_device": a_device, "a_port": a_port, "b_device": b_device, "b_port": b_port}
        )
        return self._receipt("create_link", {"a_device": a_device, "b_device": b_device})

    def delete_link(self, a_device, a_port, b_device, b_port, operation_id=None):
        self.network.links = [
            link for link in self.network.links
            if not (
                {link["a_device"], link["b_device"]} == {a_device, b_device}
                and {link["a_port"], link["b_port"]} == {a_port, b_port}
            )
        ]
        for device_name, port_name in ((a_device, a_port), (b_device, b_port)):
            device = self.network.devices.get(device_name)
            if device is None:
                continue
            interface = device.interfaces.get(port_name)
            if interface is not None:
                interface.linked = False
                interface.peers = []
                interface.peer_ports = []
        return self._receipt("delete_link", {})

    def delete_device(self, name, operation_id=None):
        self.network.devices.pop(name, None)
        self.network.links = [
            link for link in self.network.links
            if name not in (link["a_device"], link["b_device"])
        ]
        return self._receipt("delete_device", {"name": name})

    def rename_device(self, old_name, new_name, operation_id=None):
        device = self.network.devices.pop(old_name, None)
        if device is None:
            return self._receipt("rename_device", {}, OperationResult.REJECTED, "missing")
        device.name = new_name
        self.network.devices[new_name] = device
        return self._receipt("rename_device", {})

    def position_device(self, name, x, y, operation_id=None):
        device = self.network.devices.get(name)
        if device is not None:
            device.position = (int(x), int(y))
        return self._receipt("position_device", {"name": name})

    def install_module(self, device, slot, module, expected_port, operation_id=None):
        item = self.network.devices.get(device)
        if item is not None:
            item.interfaces.setdefault(expected_port, SimInterface(expected_port))
        return self._receipt("install_module", {"device": device})

    def configure_server_services(self, device, services, operation_id=None):
        return self._receipt("configure_server_services", {"device": device})

    def configure_cloud_mapping(self, device, mappings, operation_id=None):
        return self._receipt("configure_cloud_mapping", {"device": device})

    def configure_wireless_ssid(self, device, spec, operation_id=None):
        return self._receipt("configure_wireless_ssid", {"device": device})

    def generate_rsa_key(self, device, bits=1024, operation_id=None):
        return self._receipt("generate_rsa_key", {"device": device})

    def power_cycle(self, device: str):
        return {"status": "simulated"}

    def save_project(self, filename: str, directory: str = "", operation_id=None):
        target = Path(directory or ".") / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"simulated": True, "spec": self.network.spec.name}),
            encoding="utf-8",
        )
        return self._receipt(
            "save_project", {"filename": filename, "directory": directory}
        )

    def open_project(self, path: str, operation_id=None):
        return self._receipt("open_project", {"path": path})

    # -- verification -------------------------------------------------------

    def run_flow_test(
        self,
        source: str,
        destination: str,
        *,
        transport: str = "icmp",
        port: int | None = None,
        destination_ip: str | None = None,
    ) -> FlowResult:
        from .tracer import PathTracer

        inventory = self.network.inventory()
        corpus = self.network.corpus(inventory)
        tracer = PathTracer(inventory, corpus)
        trace = tracer.trace(source, destination, destination_ip=destination_ip)
        if trace.status == "config_consistent":
            outcome = FlowOutcome.REACHABLE
        elif trace.status == "failing":
            outcome = FlowOutcome.BLOCKED
        else:
            outcome = FlowOutcome.UNKNOWN
        return FlowResult(
            source=source,
            destination=destination,
            transport=transport,
            port=port,
            outcome=outcome,
            detail=(
                trace.first_failing_check.detail
                if trace.first_failing_check is not None
                else trace.status
            ),
            evidence={"simulated": True, "trace": trace.to_dict()},
        )


# -- helpers ------------------------------------------------------------------


def _interface_has_config(interface: SimInterface) -> bool:
    return any(
        (
            interface.ip, interface.description, interface.switchport_mode,
            interface.access_vlan is not None, interface.voice_vlan is not None,
            interface.trunk_native is not None, interface.trunk_allowed is not None,
            interface.encapsulation_vlan is not None, interface.helpers,
            interface.acl_in, interface.acl_out, interface.nameif,
            interface.security_level is not None, interface.standby,
            interface.shutdown,
        )
    )


def _link_id(link: dict[str, Any]) -> str:
    raw = (
        f"l-{link['a_device']}-{link['a_port']}-"
        f"{link['b_device']}-{link['b_port']}"
    ).lower()
    safe = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-")
    return safe[:64]


def _parse_route(command: str) -> dict[str, Any] | None:
    parts = command.split()
    if len(parts) >= 2 and parts[0].lower() == "ip" and parts[1].lower() == "route":
        parts = parts[2:]
    elif parts and parts[0].lower() == "route":
        parts = parts[1:]
    if len(parts) < 3:
        return None
    try:
        network = ipaddress.IPv4Network(f"{parts[0]}/{parts[1]}", strict=False)
    except ValueError:
        return None
    route: dict[str, Any] = {
        "network": str(network.network_address),
        "mask": str(network.netmask),
    }
    rest = parts[2:]
    for token in rest:
        try:
            ipaddress.IPv4Address(token)
            route["next_hop"] = token
        except ValueError:
            route["interface"] = token
    return route


def _same_route(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("network") == right.get("network")
        and left.get("mask") == right.get("mask")
        and left.get("next_hop") == right.get("next_hop")
    )


def _vlan_list(text: str) -> list[int]:
    values: set[int] = set()
    for token in str(text).replace(",", " ").split():
        if "-" in token:
            try:
                start, end = token.split("-", 1)
                values.update(range(int(start), int(end) + 1))
            except ValueError:
                continue
        else:
            try:
                values.add(int(token))
            except ValueError:
                continue
    return sorted(values)
