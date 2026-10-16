"""The network compiler: NetworkSpec + AddressPlan -> executable OperationDag.

Deterministic end to end: same spec, same plan, same compiler version -> the
same operation DAG (same ids, same order, same idempotency keys). The compiler
knows the adapter CONTRACT, never the transport: it emits create/link/
configure/verify/persist operations; the adapter decides how each one reaches
Packet Tracer.

Phase-2 scope is explicit. Features outside it are refused by name — never
half-implemented, never silently skipped.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from ..core.ids import build_fingerprint
from ..core.network_spec import DeviceRole, NetworkSpec, default_model_for_role
from ..core.safety import MutationBudget, SafetyGate, SafetyMode
from ..ipam.planner import AddressPlan
from ..serial import serial_clock_expectations, serial_dce_sides
from ..pt.catalog import resolve_model
from . import firewall_policy
from .dag import OperationDag
from .operations import Check, MutationClassification, NetworkOperation, Risk, key_for
from .layout import build_layout
from .port_solver import PortPlan, solve_ports
from .preflight import PreflightLevel, PreflightResult, run_preflight

# Device categories PT gives neither an addressable port nor an IOS CLI
# (measured live in _probe_wan2: their port objects have no
# setIpSubnetMask and configureIosDevice answers failed). They can be linked,
# never configured.
UNADDRESSABLE_CATEGORIES = frozenset({"cloud", "modem"})

COMPILER_VERSION = "2.0.0"

_PHASE2_FEATURES = {
    "devices": "roles router/switch/pc/server/laptop/access_point with fixed models",
    "links": "auto-media ethernet links",
    "addressing": "static IPv4 interface addressing",
    "routing": "static routes, OSPF, DHCP, NAT, IPv6, VLANs",
    "wireless": "wireless interface modules (wireless laptops, access points)",
    "verification": "structural, addressing and ICMP flow verification",
    "persistence": "project save",
}


class CompileError(ValueError):
    """The spec asks for something the compiler does not (yet) implement."""


@dataclass
class CompileResult:
    dag: OperationDag
    unsupported_features: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fingerprint: dict[str, str] = field(default_factory=dict)
    port_plan: PortPlan | None = None
    preflight: PreflightResult | None = None
    # S1.14/F11: when preflight fails but the failure scopes to specific ops,
    # net_build returns the result (not an error) with these op ids named.
    # The executor pre-blocks them; everything else still builds.
    preflight_blocked_ops: list[str] = field(default_factory=list)
    # The interface each address assignment was compiled onto. Verification
    # MUST read this: without it, an assignment with no interface_hint falls
    # back to a hardcoded "FastEthernet0" that does not exist on a 2911 and
    # the addressing test reads UNKNOWN forever (P1-2).
    # Keyed by (device, subnet_id): the interface must be the one the port
    # solver cabled towards that subnet, not merely the i-th interface in
    # sorted order (which silently put a LAN IP on the WAN-facing port and
    # made an entire transit path ping 100% loss while every op was green).
    assignment_ports: dict[tuple[str, str], str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dag": self.dag.to_dict(),
            "unsupported_features": list(self.unsupported_features),
            "notes": list(self.notes),
            "fingerprint": self.fingerprint,
            "assignment_ports": {
                f"{dev}#{subnet}": iface
                for (dev, subnet), iface in self.assignment_ports.items()
            },
            "preflight_blocked_ops": list(self.preflight_blocked_ops),
        }


# How long the flow gate gives a DHCP client to answer (seconds). The gate
# exits the moment every client is addressed, so this is a ceiling, not a
# timer — it only costs time when leases are genuinely slow.
#
# Measured (S1.19 live, PT 9.0.1, 45-device reference): the old 20 s ceiling
# closed with all 14 HQ clients still leaseless while a later observation
# showed every one of them holding a real address (pc1 10.10.10.4, pc7
# 10.10.20.2, lap1 10.10.30.3). PT was still converging under the load, so
# the flows probed leaseless hosts and reported "destination has no
# observable IP" on a network that was in fact healthy.
_DHCP_WAIT_TIMEOUT_S = 120.0


def _model_for(spec: NetworkSpec, device_id: str, role: DeviceRole) -> str:
    """The PT model name for a spec device.

    Must be a name addDevice actually accepts (aliases resolve in the adapter),
    so this stays in sync with the canonical role defaults from DEFAULT_MODEL_FOR_ROLE
    rather than inventing pt_type strings like "PC"/"Generic" that PT rejects.
    """
    device = spec.device_by_id(device_id)
    if device and device.model_hint:
        from ..pt.catalog import resolve_model
        # A model_hint that is a known PT model is used directly;
        # if it looks like a role value, resolve it to the default PT model.
        if resolve_model(device.model_hint):
            return device.model_hint
        # Treat the hint as a role and map to the canonical PT model name.
        return default_model_for_role(role)
    return default_model_for_role(role)


def _check_phase2_support(spec: NetworkSpec) -> None:
    unsupported: list[str] = []
    if spec.routing.mode not in ("connected", "static", "ospf"):
        unsupported.append(f"routing mode {spec.routing.mode!r}")
    if spec.routing.static_routes and spec.routing.mode == "connected":
        unsupported.append("static routes with routing.mode=connected (use 'static')")
    if unsupported:
        raise CompileError(
            "spec requires features outside the Phase-2 compiler scope: "
            + ", ".join(sorted(set(unsupported)))
            + f". Supported: {sorted(_PHASE2_FEATURES.values())}"
        )
    # A policy that cannot be enforced must fail the build, not degrade into a
    # config PT accepts and ignores.
    try:
        firewall_policy.assert_policies_target_firewalls(
            spec,
            lambda device_id: _resolve_policy_model(
                spec, spec.device_by_id(device_id)
            ) if spec.device_by_id(device_id) is not None else None,
        )
    except firewall_policy.PolicyCompileError as exc:
        raise CompileError(str(exc)) from exc
    # #25: Packet Tracer's script engine exposes no verified TCP/UDP probe, so
    # a `transport: tcp` criterion can never be decided - it would sit UNKNOWN
    # forever and a requirement gated on it could never be satisfied. Refusing
    # at compile time is the only honest answer; the port-level intent belongs
    # in a design decision, which the spec carries explicitly. #35 shares this
    # rule with the lint pass so both report it the same way.
    from .spec_lint import transport_issues

    unprobeable = transport_issues(spec)
    if unprobeable:
        raise CompileError(
            "spec asks for a probe Packet Tracer cannot make: "
            + "; ".join(unprobeable)
        )


def _wildcard_mask(netmask: str) -> str:
    """Compute the wildcard (inverse) mask from a dotted-decimal netmask."""
    return ".".join(str(255 - int(o)) for o in netmask.split("."))


def _split_cidr(cidr: str) -> tuple[str, str]:
    """'10.0.2.0/24' -> ('10.0.2.0', '255.255.255.0').

    IOS static routes (and several other IOS commands) need the network and
    the dotted netmask as separate arguments; a CIDR string is silently
    rejected and the route never installs.
    """
    import ipaddress
    net = ipaddress.ip_network(cidr, strict=False)
    return str(net.network_address), str(net.netmask)


def _is_l3_switch(spec: NetworkSpec, device_id: str) -> bool:
    """Does this switch need `switchport trunk encapsulation dot1q` first?

    PT's 2960/2950 are L2-only and REJECT that line (it aborts the whole
    config block), while a 3560/3750 is L3 and REQUIRES it before
    `switchport mode trunk`. The role says it directly; for a plain `switch`
    role the model name is the only signal PT gives us.
    """
    device = spec.device_by_id(device_id)
    if device is None:
        return False
    if device.role is DeviceRole.L3_SWITCH:
        return True
    model = (device.model_hint or "").upper()
    return any(marker in model for marker in ("3560", "3650", "3750"))


def _endpoint_vlan(spec: NetworkSpec, device_id: str, iface_hint: str | None) -> int | None:
    """The VLAN an endpoint sits in.

    Three sources, in priority order: the VLAN stated on the endpoint itself
    (#37 - a trunking host or a port in an address-less VLAN has no subnet to
    infer from), then the subinterface number in the interface hint
    (router-on-a-stick, `GigabitEthernet0/0.10` -> 10), then the VLAN the
    endpoint's own subnet declares. An endpoint with none of them carries no
    VLAN and must NOT be handed somebody else's.
    """
    for link in spec.links:
        for endpoint in (link.a, link.b):
            if endpoint.device != device_id or endpoint.vlan is None:
                continue
            if endpoint.interface_hint is None or endpoint.interface_hint == iface_hint:
                return endpoint.vlan
    if iface_hint and "." in iface_hint:
        tail = iface_hint.rsplit(".", 1)[1]
        if tail.isdigit() and 1 <= int(tail) <= 4094:
            return int(tail)
    subnet = next(
        (a.subnet for a in spec.ip_assignments if a.device == device_id), None
    )
    if subnet is None:
        return None
    declared = next((s.vlan_id for s in spec.subnets if s.id == subnet), None)
    return declared


def _vlans_on(spec: NetworkSpec, device_id: str) -> set[int]:
    """Every VLAN this device is addressed in."""
    return {
        vid
        for vid in (
            _endpoint_vlan(spec, a.device, a.interface_hint)
            for a in spec.ip_assignments
            if a.device == device_id
        )
        if vid is not None
    }


def _switch_endpoints(
    spec: NetworkSpec, port_plan: PortPlan, device_id: str
) -> list[tuple[str, str, str]]:
    """(link_id, this device's own port, peer device id) for every link this
    switch terminates. The port is ALWAYS the switch's own - indexing the peer
    is what made the compiler emit `interface FastEthernet0` (the PC's port)
    and even `interface s1` (the switch's device id) on the switch.
    """
    out: list[tuple[str, str, str]] = []
    for link in spec.links:
        endpoints = port_plan.link_endpoints.get(link.id)
        if endpoints is None:
            continue
        _a_dev, a_port, _b_dev, b_port = endpoints
        if link.a.device == device_id and b_port:
            out.append((link.id, a_port, link.b.device))
        elif link.b.device == device_id and a_port:
            out.append((link.id, b_port, link.a.device))
    return out


def _render_switch_block(
    port: str, mode: str, vid: int | None, *, l3: bool, carried: set[int],
    native: int | None = None, allowed: list[int] | None = None,
) -> list[str]:
    """One `interface ... exit` block for an access or trunk port.

    `native` and `allowed` (#37) let a spec override what was inferred; the
    native VLAN is only written when the trunk actually carries something,
    because a trunk with no allowed VLANs has nothing to carry.
    """
    if mode == "trunk":
        block = [f"interface {port}"]
        if l3:
            block.append(" switchport trunk encapsulation dot1q")
        block.append(" switchport mode trunk")
        effective = set(allowed) if allowed else carried
        if effective:
            ordered = ",".join(str(v) for v in sorted(effective))
            block.append(f" switchport trunk allowed vlan {ordered}")
            block.append(f" switchport trunk native vlan {native or 1}")
        block.append(" exit")
        return block
    block = [f"interface {port}", " switchport mode access"]
    if vid is not None:
        block.append(f" switchport access vlan {vid}")
    block.append(" exit")
    return block


def _build_vlan_switch_commands(
    spec: NetworkSpec, port_plan: PortPlan
) -> dict[str, list[str]]:
    """VLAN / access-port / trunk / hostname CLI for each switch.

    This replaces three measured bugs at once:

    * the access block used the PEER's interface name - or even the switch's
      own device id - instead of the switch-side port, so PT rejected every
      line and the switch stayed unconfigured;
    * the VLAN number came from ``spec.vlans[0]`` regardless of which subnet
      the host is on, so a host whose subnet declares no VLAN was tagged
      anyway;
    * ``"trunk": true`` only SUPPRESSED the access block: no
      ``switchport mode trunk`` was rendered anywhere in the compiler.

    VLAN definitions go only to switches that carry them, and every switch
    gets its hostname (the generic router path already did; switches did not).
    """
    commands_by_switch: dict[str, list[str]] = {}

    # Pass 1 - VLAN membership per link and per switch. Trunk VLANs need every
    # switch's access VLANs first, so access membership is settled before the
    # trunk pass runs.
    access_vid: dict[str, int | None] = {}
    for link in spec.links:
        if link.trunk:
            continue
        a_vid = _endpoint_vlan(spec, link.a.device, link.a.interface_hint)
        b_vid = _endpoint_vlan(spec, link.b.device, link.b.interface_hint)
        access_vid[link.id] = a_vid if a_vid is not None else b_vid

    switches = [
        d for d in spec.devices
        if d.role in (DeviceRole.SWITCH, DeviceRole.L3_SWITCH)
    ]
    trunk_link_ids = {l.id for l in spec.links if l.trunk}
    access_by_switch: dict[str, set[int]] = {}
    for switch in switches:
        vids: set[int] = set()
        for link_id, _port, peer in _switch_endpoints(spec, port_plan, switch.id):
            if link_id in trunk_link_ids:
                continue
            vid = access_vid.get(link_id)
            if vid is not None:
                vids.add(vid)
            else:
                # a link whose endpoints declare no VLAN: inherit the peer's
                # addressed subnet so two switches agree on the number
                vids |= _vlans_on(spec, peer)
        access_by_switch[switch.id] = vids

    # Pass 2 - trunk membership: what each trunk must carry.
    trunk_by_switch: dict[str, set[int]] = {}
    for switch in switches:
        carried: set[int] = set()
        for link in spec.links:
            if not link.trunk:
                continue
            if switch.id not in (link.a.device, link.b.device):
                continue
            peer = link.b.device if link.a.device == switch.id else link.a.device
            carried |= _vlans_on(spec, peer)
            carried |= _vlans_on(spec, switch.id)
            carried |= access_by_switch.get(peer, set())
            carried |= set(access_by_switch.get(switch.id, set()))
        trunk_by_switch[switch.id] = carried

    # Pass 3 - render.
    for switch in switches:
        l3 = _is_l3_switch(spec, switch.id)
        access_vids = access_by_switch.get(switch.id, set())
        trunk_vids = trunk_by_switch.get(switch.id, set())
        blocks: list[list[str]] = []
        for link_id, port, _peer in _switch_endpoints(spec, port_plan, switch.id):
            if link_id in trunk_link_ids:
                trunk_link = next(
                    (l for l in spec.links if l.id == link_id), None
                )
                blocks.append(
                    _render_switch_block(
                        port, "trunk", None, l3=l3,
                        carried=access_vids | trunk_vids,
                        # #37: a spec may state the trunk's own VLAN set
                        # instead of the inferred one.
                        native=getattr(trunk_link, "native_vlan", None),
                        allowed=getattr(trunk_link, "allowed_vlans", None),
                    )
                )
            else:
                blocks.append(
                    _render_switch_block(
                        port, "access", access_vid.get(link_id), l3=l3, carried=set()
                    )
                )
        lines: list[str] = [f"hostname {switch.id}"]
        # VLAN database: only what this switch actually carries (#7).
        for vid in sorted(access_vids | trunk_vids):
            lines.append(f"vlan {vid}")
            declared = next((v for v in spec.vlans if v.vlan_id == vid), None)
            if declared is not None and declared.name:
                lines.append(f" name {declared.name}")
            lines.append(" exit")
        for block in blocks:
            lines.extend(block)
        if len(lines) > 1:
            commands_by_switch[switch.id] = lines
    return commands_by_switch


def _build_vlan_router_commands(
    spec: NetworkSpec, plan: AddressPlan
) -> dict[str, list[str]]:
    """Generate subinterface .1q CLI commands for routers with VLAN assignments."""
    commands_by_router: dict[str, list[str]] = {}
    # No `spec.vlans` requirement here: the VLAN number lives in the interface
    # hint (`GigabitEthernet0/0.10`), and the generic router path deliberately
    # skips dotted subinterfaces - if this bailed out too, the address would
    # never be configured at all.
    dotted = any(
        a.interface_hint and "." in a.interface_hint for a in plan.assignments
    )
    if not dotted:
        return commands_by_router
    for device in spec.devices:
        if device.role not in (DeviceRole.ROUTER, DeviceRole.L3_SWITCH):
            continue
        lines: list[str] = []
        parent_ports: set[str] = set()
        for assignment in plan.assignments:
            if assignment.device != device.id:
                continue
            if assignment.interface_hint and "." in assignment.interface_hint:
                parent = assignment.interface_hint.split(".")[0]
                vlan_id = int(assignment.interface_hint.split(".")[1])
                if parent not in parent_ports:
                    lines.append(f"interface {parent}")
                    lines.append(" no shutdown")
                    lines.append(" exit")
                    parent_ports.add(parent)
                mask = plan.allocation_by_subnet(assignment.subnet_id)
                if mask is None:
                    continue
                lines.append(f"interface {assignment.interface_hint}")
                lines.append(f" encapsulation dot1Q {vlan_id}")
                lines.append(f" ip address {assignment.ip} {mask.netmask}")
                lines.append(" exit")
        if lines:
            commands_by_router[device.id] = lines
    return commands_by_router


def _build_ospf_commands(spec: NetworkSpec) -> dict[str, list[str]]:
    """Generate OSPF CLI commands for each router."""
    commands_by_router: dict[str, list[str]] = {}
    if spec.routing.ospf is None or spec.routing.mode != "ospf":
        return commands_by_router
    ospf = spec.routing.ospf
    for device in spec.devices:
        if device.role not in (DeviceRole.ROUTER, DeviceRole.L3_SWITCH):
            continue
        lines: list[str] = []
        lines.append(f"router ospf {ospf.process_id}")
        lines.append(f" router-id {device.id}")
        for net in ospf.networks:
            network = ipaddress.ip_network(net, strict=False)
            wildcard = _wildcard_mask(str(network.netmask))
            lines.append(
                f" network {network.network_address} {wildcard} area {ospf.area}"
            )
        lines.append(" exit")
        if len(lines) > 2:
            commands_by_router[device.id] = lines
    return commands_by_router


def _build_dhcp_commands(spec: NetworkSpec, plan: AddressPlan) -> dict[str, list[str]]:
    """Generate DHCP pool CLI commands for each router.

    The gateway comes from the AddressPlan (planner.py computes and reserves
    it); the spec's IpAssignmentSpec has no gateway field.
    """
    commands_by_router: dict[str, list[str]] = {}
    for subnet in spec.subnets:
        if not subnet.dhcp:
            continue
        # The gateway is the first router/l3_switch assignment on this subnet.
        gateway = next(
            (
                a.gateway
                for a in plan.assignments
                if a.subnet_id == subnet.id and a.gateway
            ),
            None,
        )
        if gateway is None:
            continue
        router = next(
            (
                a.device
                for a in plan.assignments
                if a.subnet_id == subnet.id and a.gateway == gateway
            ),
            None,
        )
        if router is None or not any(d.id == router for d in spec.devices):
            continue
        lines = commands_by_router.setdefault(router, [])
        # Bug #4: IOS takes `network <net> <dotted mask>`, NOT the CIDR form -
        # `network 10.0.10.0/24` is rejected and the pool never serves an
        # address. The gateway is excluded so the router never leases its own
        # interface address to a client (#4). There is no DNS field in the
        # spec, so no `dns-server` line is invented here either (#9): an
        # unasked-for public resolver in a lab config is worse than none.
        if subnet.cidr:
            network, mask = _split_cidr(subnet.cidr)
            lines.append(f" ip dhcp excluded-address {gateway}")
            lines.append(f" ip dhcp pool {subnet.id}")
            lines.append(f" network {network} {mask}")
        else:
            lines.append(f" ip dhcp pool {subnet.id}")
        lines.append(f" default-router {gateway}")
        lines.append(" exit")
    return commands_by_router


def _build_nat_commands(spec: NetworkSpec) -> dict[str, list[str]]:
    """Generate NAT CLI commands for each router."""
    commands_by_router: dict[str, list[str]] = {}
    if not spec.routing.nat:
        return commands_by_router
    for nat_cfg in spec.routing.nat:
        lines: list[str] = []
        lines.append(f"interface {nat_cfg.inside_interface}")
        lines.append(" ip nat inside")
        lines.append(" exit")
        lines.append(f"interface {nat_cfg.outside_interface}")
        lines.append(" ip nat outside")
        lines.append(" exit")
        if nat_cfg.mode == "static":
            for m in nat_cfg.static_mappings:
                lines.append(
                    f"ip nat inside source static {m['inside_local']} {m['inside_global']}"
                )
        elif nat_cfg.mode == "dynamic":
            if nat_cfg.inside_networks:
                for net in nat_cfg.inside_networks:
                    lines.append(f"access-list {nat_cfg.acl_number} permit {net}")
                lines.append(
                    f"ip nat pool {nat_cfg.pool_name} {nat_cfg.start_ip} {nat_cfg.end_ip} netmask {nat_cfg.netmask}"
                )
                lines.append(
                    f"ip nat inside source list {nat_cfg.acl_number} pool {nat_cfg.pool_name}"
                )
        elif nat_cfg.mode == "pat":
            if nat_cfg.use_interface_overload:
                lines.append(
                    f"ip nat inside source list {nat_cfg.acl_number} interface {nat_cfg.outside_interface} overload"
                )
            else:
                if nat_cfg.inside_networks:
                    for net in nat_cfg.inside_networks:
                        lines.append(f"access-list {nat_cfg.acl_number} permit {net}")
                    lines.append(
                        f"ip nat pool {nat_cfg.pool_name} {nat_cfg.start_ip} {nat_cfg.end_ip} netmask {nat_cfg.netmask}"
                    )
                    lines.append(
                        f"ip nat inside source list {nat_cfg.acl_number} pool {nat_cfg.pool_name} overload"
                    )
        if lines:
            commands_by_router[nat_cfg.router] = lines
    return commands_by_router


def _build_ipv6_commands(spec: NetworkSpec) -> dict[str, list[str]]:
    """Generate IPv6 CLI commands for each device."""
    commands_by_device: dict[str, list[str]] = {}
    if spec.routing.ipv6 is None or not spec.routing.ipv6.enabled:
        return commands_by_device
    ipv6_cfg = spec.routing.ipv6
    for device in spec.devices:
        if device.role not in (DeviceRole.ROUTER, DeviceRole.L3_SWITCH):
            continue
        lines: list[str] = []
        lines.append("ipv6 unicast-routing")
        for iface, addr in ipv6_cfg.addresses.items():
            lines.append(f"interface {iface}")
            lines.append(f" ipv6 address {addr}")
            lines.append(" ipv6 enable")
            lines.append(" exit")
        if len(lines) > 1:
            commands_by_device[device.id] = lines
    return commands_by_device


def _detect_wireless_devices(spec: NetworkSpec) -> list[str]:
    """Return device IDs that require wireless interface modules."""
    wireless: list[str] = []
    for device in spec.devices:
        for req in device.interface_requirements:
            if req.media == "wireless":
                wireless.append(device.id)
                break
        # Also check laptops with wireless flag from link assignments
        # (laptops with wireless laptops in the legacy model)
    return wireless


def _assign_router_ports(
    spec: NetworkSpec, plan: AddressPlan, port_plan: PortPlan
) -> dict[tuple[str, str], str]:
    """Map each router address assignment to the interface carrying its subnet.

    The port solver decides which physical interface a link uses; the router's
    per-subnet IP MUST land on the interface cabled towards that subnet's
    members. This walks the solved link graph from each router interface and
    picks, for every assignment, the interface whose link reaches a device that
    holds an address on the same subnet. Assignments with an explicit
    interface_hint are left alone (the author chose the port on purpose).

    Without this, IPs were placed by assignment-order-against-sorted-ports,
    which swapped LAN and WAN IPs whenever the two orders disagreed and left a
    fully built transit path pinging 100% loss.
    """
    # device -> list of (my_interface, neighbour_device), from the SOLVED links.
    neighbours: dict[str, list[tuple[str, str]]] = {}
    for link in spec.links:
        endpoints = port_plan.link_endpoints.get(link.id)
        if not endpoints:
            continue
        a_dev, a_port, b_dev, b_port = endpoints
        neighbours.setdefault(a_dev, []).append((a_port, b_dev))
        neighbours.setdefault(b_dev, []).append((b_port, a_dev))

    # subnet_id -> the devices holding an address on it.
    subnet_members: dict[str, set[str]] = {}
    for assignment in plan.assignments:
        if assignment.ip:
            subnet_members.setdefault(assignment.subnet_id, set()).add(
                assignment.device
            )

    def hops_to_member(router: str, iface: str, members: set[str]) -> int | None:
        """BFS from the neighbour on `iface` (never back through `router`).

        Returns the hop distance to the nearest subnet member, or None if that
        interface does not lead to the subnet.
        """
        start = next(
            (nb for my_iface, nb in neighbours.get(router, []) if my_iface == iface),
            None,
        )
        if start is None:
            return None
        seen = {router}
        frontier = [start]
        hops = 0
        while frontier:
            nxt: list[str] = []
            for node in frontier:
                if node in seen:
                    continue
                seen.add(node)
                if node in members:
                    return hops
                nxt.extend(nb for _, nb in neighbours.get(node, []))
            frontier = nxt
            hops += 1
        return None

    mapping: dict[tuple[str, str], str] = {}
    wireless_clients = _wireless_client_ids(spec)
    # PT's ASA 5505 carries addresses on the Vlan SVI the switchport joins,
    # not on Ethernet0/x itself — verification read-back must target the SVI.
    # The home gateway is the same story: its LAN address lives on Vlan1
    # whichever "GigabitEthernet N" port a cable actually uses.
    from ..pt.catalog import resolve_model

    asa5505_ids: set[str] = set()
    home_gateway_ids: set[str] = set()
    for d in spec.devices:
        m = resolve_model(_model_for(spec, d.id, d.role))
        if m is not None and m.pt_type == "5505":
            asa5505_ids.add(d.id)
        elif m is not None and m.category == "home_gateway":
            home_gateway_ids.add(d.id)
    for assignment in plan.assignments:
        if assignment.interface_hint or assignment.ip is None:
            continue
        device = next((d for d in spec.devices if d.id == assignment.device), None)
        if device is None:
            continue
        # A wireless client carries its address on the radio, not on the wired
        # NIC that the module install removes.
        if assignment.device in wireless_clients:
            mapping[(assignment.device, assignment.subnet_id)] = "Wireless0"
            continue
        if device.id not in home_gateway_ids and device.role not in (
            DeviceRole.ROUTER, DeviceRole.L3_SWITCH
        ):
            continue
        members = subnet_members.get(assignment.subnet_id, set())
        # The router's own membership does not count: we want the interface
        # that leads AWAY to the other members of the subnet.
        members = members - {assignment.device}
        interfaces = {
            resolution.interface
            for resolution in port_plan.resolutions.values()
            if resolution.device == assignment.device
        }
        best_iface, best_hops = None, None
        for iface in sorted(interfaces):
            hops = hops_to_member(assignment.device, iface, members)
            if hops is not None and (best_hops is None or hops < best_hops):
                best_iface, best_hops = iface, hops
        if best_iface is None and not members:
            # A subnet whose only addressed member is this router is the WAN
            # side: the ISP endpoint (Cloud-PT, a modem, a cell tower) holds
            # no address at all, so nothing above can point at it. The link
            # that reaches an unaddressed device IS the WAN interface.
            best_iface = _interface_towards_unaddressed(
                spec, plan, port_plan, assignment.device
            )
        if best_iface is not None:
            key = (assignment.device, assignment.subnet_id)
            if assignment.device in home_gateway_ids:
                mapping[key] = "Vlan1"
            else:
                mapping[key] = (
                    _asa5505_config_iface(best_iface)
                    if assignment.device in asa5505_ids
                    else best_iface
                )
    return mapping


def _interface_towards_unaddressed(
    spec: NetworkSpec,
    plan: AddressPlan,
    port_plan,
    device_id: str,
) -> str | None:
    """The port on this device that faces a device with no address.

    Cloud-PT, the modems, cell towers and central-office servers cannot hold
    an address in PT, so they never appear in a subnet's addressed members.
    The cable to one of them is what identifies a WAN interface.
    """
    addressed = {
        a.device for a in plan.assignments if a.ip is not None
    }
    for link in spec.links:
        for near, far in ((link.a.device, link.b.device), (link.b.device, link.a.device)):
            if near != device_id or far in addressed:
                continue
            endpoints = port_plan.link_endpoints.get(link.id)
            if not endpoints:
                continue
            a_device, a_port, b_device, b_port = endpoints
            if a_device == device_id:
                return a_port
            if b_device == device_id:
                return b_port
    return None


def _asa5505_config_iface(port: str) -> str:
    """Ethernet0/N (the L2 switchport) -> the Vlan interface that carries IPs.

    The compiler joins each 5505 switchport to vlan N+1 and configures the
    address on that SVI; PT's factory ships every port unjoined with
    192.168.1.1 on Vlan1 (live probes r7-r16). Non-Ethernet0/N names (an
    author hinting the Vlan directly) pass through untouched.
    """
    if port.startswith("Ethernet0/"):
        try:
            return f"Vlan{int(port.split('/', 1)[1]) + 1}"
        except ValueError:
            return port
    return port


def _reject_unaddressable_assignments(spec: NetworkSpec, plan: AddressPlan) -> None:
    """Refuse an address on a device whose ports cannot hold one.

    Measured live (_probe_wan2): a Cloud-PT, DSL-Modem or Cable-Modem port has
    no `setIpSubnetMask` at all, and none of the three accepts IOS CLI. An
    address assigned to one would be emitted as a command PT cannot execute and
    would read back as 0.0.0.0 forever - so the build stops here with the
    model named and the working alternative stated.
    """
    for assignment in plan.assignments:
        if assignment.ip is None:
            continue
        device = _device_of(spec, assignment.device)
        if device is None:
            continue
        model = resolve_model(_model_for(spec, assignment.device, device.role))
        if model is None or model.category not in UNADDRESSABLE_CATEGORIES:
            continue
        raise CompileError(
            f"cannot put {assignment.ip} on {assignment.device} "
            f"({model.pt_type}): Packet Tracer gives this device neither an "
            "addressable port nor an IOS CLI. Give the address to the router "
            "on its WAN side, and keep the "
            f"{model.pt_type} as the link endpoint."
        )


def _reject_unsupported_wireless(spec: NetworkSpec) -> None:
    """Guard the wireless path against clients PT can never associate.

    PT associates hosts that have a wireless NIC module - laptop, tablet,
    phone, wireless end device - on its own, and the association is read back
    as an `Antenna` link. IoE "things" are different: their Wireless0 never
    forms an antenna link in any probe, so a topology that expects one is
    refused here with the model named, instead of building a topology that
    looks wireless and carries nothing.
    """
    from ..wireless import module_for_model

    for link in spec.links:
        if link.media != "wireless":
            continue
        client, ap = _wireless_ends(spec, link)
        if client is not None:
            continue
        # Neither end can hold a radio: name them so the author can see which
        # side is the problem. The spec's own model hint is used first because
        # that is the string the author wrote ("Solar Panel"); the device id is
        # the fallback, then the catalog's canonical model string.
        named = []
        for device_id in (link.a.device, link.b.device):
            device = _device_of(spec, device_id)
            if device is None:
                continue
            hint = (getattr(device, "model_hint", "") or "").strip()
            if hint:
                named.append(hint)
                continue
            model = resolve_model(_model_for(spec, device_id, device.role))
            named.append(model.pt_type if model is not None else device_id)
        raise CompileError(
            f"link {link.id}: {' and '.join(named)} cannot form a wireless "
            "association - Packet Tracer only associates hosts that take a "
            "wireless NIC module (Laptop-PT, TabletPC-PT, SMARTPHONE-PT, "
            "WirelessEndDevice-PT) with an access point. Use one of those as "
            "the client, or cabled the device instead."
        )


def _reject_wireless_criteria_without_links(spec: NetworkSpec) -> None:
    """F-6: a wireless criterion must name a link the spec declares.

    The reference run carried two Wireless0 criteria on a spec with zero
    wireless links; the build happily ran, and the criteria failed late with
    "no Wireless0 interface" - a refusal disguised as a red verdict, payable
    only after mutations. A topology that never declares the association can
    never satisfy it, so this is refused by criterion id before any mutation.
    """
    declared = {
        frozenset({link.a.device, link.b.device})
        for link in spec.links
        if link.media == "wireless"
    }
    for criterion in spec.acceptance_criteria:
        test = criterion.test
        if getattr(test, "kind", None) != "wireless":
            continue
        pair = frozenset({test.client, test.access_point})
        if pair not in declared:
            raise CompileError(
                f"criterion {criterion.id}: wireless association "
                f"{test.client} <-> {test.access_point} has no declared "
                "wireless link - add a link with media \"wireless\" between "
                "the two devices, or drop the criterion."
            )


def _reject_gateway_wan_cabling(
    spec: NetworkSpec,
    plan: AddressPlan,
    port_plan,
) -> None:
    """A host may not be cabled to a home gateway's WAN port by accident.

    HomeRouter-PT-AC has four LAN ports and one "Internet" port. The solver
    takes ports in catalog order (LAN first) and simply runs out of LAN ports,
    so a fifth client would silently land on the WAN - a topology that reports
    every link present and carries no LAN traffic. An EXPLICIT
    interface_hint of "Internet" is a deliberate WAN and stays allowed.
    """
    lan_by_gateway: dict[str, set[str]] = {}
    for assignment in plan.assignments:
        if assignment.ip is None:
            continue
        device = _device_of(spec, assignment.device)
        if device is None:
            continue
        if _resolve_home_gateway_model(spec, device) is None:
            continue
        lan_by_gateway.setdefault(assignment.device, set()).add(assignment.subnet_id)

    if not lan_by_gateway:
        return

    for link in spec.links:
        endpoints = {"a": link.a, "b": link.b}
        for side, endpoint in endpoints.items():
            if endpoint.device not in lan_by_gateway:
                continue
            if endpoint.interface_hint:
                continue  # the author asked for this port on purpose
            solved = port_plan.link_endpoints.get(link.id)
            if solved is None:
                continue  # RF association, not a cable: no WAN-port question
            a_device, a_port, b_device, b_port = solved
            port = a_port if side == "a" else b_port
            if port != "Internet":
                continue
            peer = link.b.device if side == "a" else link.a.device
            raise CompileError(
                f"link {link.id}: {peer} would be cabled to the home gateway "
                f"{endpoint.device}'s WAN port. It has four LAN ports "
                "(GigabitEthernet 1-4) and they are all taken; put the extra "
                "device behind a switch instead."
            )


def _association_op_for_client(dag: OperationDag, client: str) -> str | None:
    """The association operation a wireless client must wait for."""
    for node in dag.nodes.values():
        op = node.operation
        if op.operation_type == "associate_wireless" and op.inputs.get("client") == client:
            return op.operation_id
    return None


def _wireless_channel(spec: NetworkSpec, link) -> int:
    """Channel for this wireless link: the endpoint hint, else the default.

    A non-default channel is what separates the intended access point from
    radio objects PT still remembers from other topologies, so the default is
    deliberately not channel 0.
    """
    from ..wireless import DEFAULT_CHANNEL

    for endpoint in (link.a, link.b):
        hint = endpoint.interface_hint
        if hint and hint.lower().startswith("channel"):
            parts = [p for p in hint.replace("channel", "").replace(":", "").split() if p]
            for part in parts:
                if part.isdigit():
                    return int(part)
    return DEFAULT_CHANNEL


def _wireless_client_ids(spec: NetworkSpec) -> set[str]:
    """Devices that join a wireless link as the CLIENT (they take a radio)."""
    from ..wireless import module_for_model

    clients: set[str] = set()
    for link in spec.links:
        if link.media != "wireless":
            continue
        client, ap = _wireless_ends(spec, link)
        if client and ap:
            clients.add(client)
    return clients


def _device_of(spec: NetworkSpec, device_id: str):
    return next((d for d in spec.devices if d.id == device_id), None)


def _wireless_ends(spec: NetworkSpec, link) -> tuple[str | None, str | None]:
    """(client, access_point) for a wireless link, by which side can associate."""
    from ..wireless import module_for_model

    def can_associate(device_id: str) -> bool:
        device = _device_of(spec, device_id)
        if device is None:
            return False
        model = resolve_model(_model_for(spec, device_id, device.role))
        return model is not None and module_for_model(model.pt_type) is not None

    if can_associate(link.a.device):
        return link.a.device, link.b.device
    if can_associate(link.b.device):
        return link.b.device, link.a.device
    return None, None


def _resolve_home_gateway_model(spec: NetworkSpec, device) -> Any:
    """The catalog DeviceModel when this device is a home gateway.

    Both category spellings belong here: the catalog calls `HomeRouter-PT-AC`
    a `home_gateway` and a `Linksys-WRT300N` a `wireless_router`, and matching
    only the first meant every Linksys fell through to the generic router path
    - which then failed with "no resolved interface" because a Linksys has
    `Internet` / `Ethernet 1..4`, not `GigabitEthernet0/0` (issue #36).
    """
    from ..pt.catalog import resolve_model

    model = resolve_model(_model_for(spec, device.id, device.role))
    if model is not None and model.category in ("home_gateway", "wireless_router"):
        return model
    return None


def _build_home_gateway_commands(
    spec: NetworkSpec,
    plan: AddressPlan,
    assignment_ports: dict[tuple[str, str], str],
    device,
) -> list[str]:
    """LAN address + DHCP service for a home gateway (PT-verified order).

    Probe _probe_wiot10 on a real HomeRouter-PT-AC: `ip address` at global
    level, then the DHCP pool, then the Vlan1 block, and the read-back shows
    the LAN address on Vlan1. The gateway exposes a single LAN SVI, so a
    second served subnet cannot be built and is rejected rather than silently
    overwriting the first.
    """
    assignments = [
        a for a in plan.assignments
        if a.device == device.id and a.ip is not None
    ]
    if len(assignments) > 1:
        raise CompileError(
            f"home gateway {device.id}: PT exposes a single LAN (Vlan1), so at "
            f"most one served subnet fits (got {len(assignments)})"
        )
    if not assignments:
        return []
    assignment = assignments[0]
    allocation = plan.allocation_by_subnet(assignment.subnet_id)
    if allocation is None:  # pragma: no cover - plan/spec consistency
        raise CompileError(f"assignment {device.id} has no allocation")

    from ..pt.constants import DEFAULT_DNS

    ip_line = f"ip address {assignment.ip} {allocation.netmask}"
    return [
        ip_line,
        "ip dhcp pool LANPOOL",
        f"network {allocation.network} {allocation.netmask}",
        f"default-router {assignment.ip}",
        f"dns-server {DEFAULT_DNS}",
        "exit",
        "interface Vlan1",
        ip_line,
        "no shutdown",
        "exit",
    ]


def _add_home_gateway_config_op(
    dag: OperationDag,
    spec: NetworkSpec,
    plan: AddressPlan,
    assignment_ports: dict[tuple[str, str], str],
    device,
    device_ops: dict[str, str],
) -> None:
    """configure_ios op for a home gateway; hostname LAST, after its links.

    Category split (#10): PT's HomeRouter-PT-AC answers `configureIosDevice`
    (probe _probe_wiot10 — the read-back shows the LAN address on Vlan1), but
    the Linksys-WRT300N answers `true` and applies nothing (live probe). The
    Linksys must leave the CLI path for the port-API op.
    """
    model = _resolve_home_gateway_model(spec, device)
    if model is not None and model.category == "wireless_router":
        _add_wireless_router_config_op(dag, spec, plan, device, device_ops)
        return
    commands = _build_home_gateway_commands(spec, plan, assignment_ports, device)
    if not commands:
        return
    commands.append(f"hostname {device.id}")
    if spec.routing.mode == "static":
        from ..pt.catalog import resolve_model

        for route in spec.routing.static_routes:
            if route.device != device.id:
                continue
            net, mask = _split_cidr(route.destination)
            commands.insert(
                len(commands) - 1,
                f"ip route {net} {mask} "
                f"{route.next_hop or route.exit_interface or ''}".strip(),
            )
    deps = [device_ops[device.id], *_link_ops_for_device(dag, device.id)]
    op = NetworkOperation(
        operation_id=f"op-config-{device.id}",
        operation_type="configure_ios",
        inputs={"device": device.id, "commands": commands},
        dependencies=tuple(dict.fromkeys(deps)),
        preconditions=(Check("device_present", device.id, "present"),),
        expected_postconditions=(
            Check("ios_acceptance", device.id, "read back by verification"),
        ),
        classification=MutationClassification.CONFIGURE,
        risk=Risk.MEDIUM,
        blast_radius=(device.id,),
        retry_policy=1,
        idempotency_key=key_for("configure_ios", device.id, tuple(commands)),
    )
    dag.add(op)


def _add_wireless_router_config_op(
    dag: OperationDag,
    spec: NetworkSpec,
    plan: AddressPlan,
    device,
    device_ops: dict[str, str],
) -> None:
    """Port-API LAN op for a PT Linksys (#10); refuses what it cannot push.

    Live probes on a Linksys-WRT300N settled what this path may claim:

    * `port.setIpSubnetMask` on Vlan1 moves the LAN address and the topology
      read-back shows it — that is the one push worth emitting;
    * `configureIosDevice` answers `true` and applies nothing, so a
      `configure_ios` block here (routes, pool, hostname) would be a green
      receipt for a no-op — static routes are refused by name instead;
    * the factory DHCP pool serves only 192.168.0.1/24. A custom LAN IP
      kills the pool entirely (clients fall to APIPA) and no PT API
      repoints it, so DHCP clients behind a custom LAN IP are refused
      rather than left waiting on leases that cannot arrive. The factory
      192.168.0.1/24 case stays legal — measured: an untouched wr1 handed
      out .100+ leases.
    """
    if spec.routing.mode == "static":
        for route in spec.routing.static_routes:
            if route.device == device.id:
                raise CompileError(
                    f"wireless router {device.id}: PT's Linksys has no reachable "
                    f"CLI, so the static route to {route.destination} cannot be "
                    f"pushed (configureIosDevice is a no-op on it - live probe)"
                )
    assignments = [
        a for a in plan.assignments
        if a.device == device.id and a.ip is not None
    ]
    if len(assignments) > 1:
        raise CompileError(
            f"wireless router {device.id}: PT exposes a single LAN (Vlan1), so at "
            f"most one served subnet fits (got {len(assignments)})"
        )
    if not assignments:
        return
    assignment = assignments[0]
    allocation = plan.allocation_by_subnet(assignment.subnet_id)
    if allocation is None:  # pragma: no cover - plan/spec consistency
        raise CompileError(f"assignment {device.id} has no allocation")
    factory_lan = (
        assignment.ip == "192.168.0.1"
        and allocation.network == "192.168.0.0"
        and allocation.netmask == "255.255.255.0"
    )
    if not factory_lan:
        dhcp_clients = sorted(
            a.device for a in plan.assignments
            if a.subnet_id == assignment.subnet_id
            and getattr(a, "dhcp", False)
            and a.device != device.id
        )
        if dhcp_clients:
            raise CompileError(
                f"wireless router {device.id}: cannot serve DHCP to "
                f"{', '.join(dhcp_clients)} behind the custom LAN IP "
                f"{assignment.ip} - PT's Linksys pool is welded to the factory "
                f"192.168.0.1/24 and exposes no API to repoint it "
                f"(measured: clients fall to APIPA)"
            )
    deps = [device_ops[device.id], *_link_ops_for_device(dag, device.id)]
    op = NetworkOperation(
        operation_id=f"op-config-{device.id}",
        operation_type="configure_gateway_lan",
        inputs={
            "device": device.id,
            "ip": assignment.ip,
            "mask": allocation.netmask,
        },
        dependencies=tuple(dict.fromkeys(deps)),
        preconditions=(Check("device_present", device.id, "present"),),
        expected_postconditions=(
            Check(
                "configured_addresses_present",
                device.id,
                "read back by verification",
            ),
        ),
        classification=MutationClassification.CONFIGURE,
        risk=Risk.MEDIUM,
        blast_radius=(device.id,),
        retry_policy=1,
        idempotency_key=key_for(
            "configure_gateway_lan",
            device.id,
            assignment.ip,
            allocation.netmask,
        ),
    )
    dag.add(op)


def _resolve_firewall_model(spec: NetworkSpec, device) -> Any:
    """The catalog DeviceModel when this device compiles as an ASA firewall."""
    from ..pt.catalog import resolve_model

    model = resolve_model(_model_for(spec, device.id, device.role))
    if model is not None and model.category == "firewall":
        return model
    return None


def _resolve_policy_model(spec: NetworkSpec, device) -> Any:
    """The catalog DeviceModel for a policy target, whatever its category.

    Which categories may actually enforce is firewall_policy's decision
    (ENFORCING_CATEGORIES); this resolver only maps the spec id to a model so
    that check sees the real category instead of None for non-ASAs.
    """
    from ..pt.catalog import resolve_model

    return resolve_model(_model_for(spec, device.id, device.role))


#: Roles that count as "the network an ASA protects". A firewall's inside
#: faces the users; routers, switches and other firewalls are transit.
_END_DEVICE_ROLES = frozenset(
    {"pc", "laptop", "server", "iot"}
)


def _asa_outside_subnet(
    spec: NetworkSpec, plan: AddressPlan, device
) -> str | None:
    """Subnet id of the interface an ASA should call ``outside``.

    PT's ASA is directional: traffic from the level-100 interface reaches the
    level-0 one and NOT the other way, even when a matching ``permit`` is
    bound to the level-0 interface. Live A/B (PT 9.0.1, S1.19) proved the
    *name* is what decides it — one router+ASA+switch lab, with only the order
    of the two ``ip_assignments`` swapped, flipped the same flow PASS->FAIL.

    So the name has to come from the topology, not from the order the
    assignments happen to be listed in. The interface the ASA protects — its
    ``inside`` — is the one facing the larger share of the spec's end
    devices; the edge it guards is ``outside``. For the reference campus that
    names fwhq's WAN port outside (HQ behind it) and fwdc's DC port outside
    (the core behind it), which is what each firewall actually protects.

    Returns ``None`` when the question is undecidable — no links, an
    unresolved port, or two sides holding the same number of end devices —
    and the caller keeps its positional fallback.
    """
    links = [
        link
        for link in spec.links
        if device.id in (link.a.device, link.b.device)
    ]
    if not links:
        return None

    adjacency: dict[str, set[str]] = {}
    for link in spec.links:
        adjacency.setdefault(link.a.device, set()).add(link.b.device)
        adjacency.setdefault(link.b.device, set()).add(link.a.device)

    role_by_id = {d.id: str(d.role.value) for d in spec.devices}

    def _end_devices(neighbour: str) -> int:
        """End devices reachable from ``neighbour`` without crossing the ASA."""
        seen = {neighbour}
        stack = [neighbour]
        while stack:
            node = stack.pop()
            for peer in adjacency.get(node, ()):
                if peer == device.id or peer in seen:
                    continue
                seen.add(peer)
                stack.append(peer)
        return sum(1 for node in seen if role_by_id.get(node) in _END_DEVICE_ROLES)

    # interface (as the spec spells it) -> how many end devices are behind it
    behind: dict[str, int] = {}
    for link in links:
        side = link.a if link.a.device == device.id else link.b
        far = link.b.device if side is link.a else link.a.device
        if side.interface_hint:
            behind[side.interface_hint] = _end_devices(far)

    if len(behind) < 2:
        return None
    counts = sorted(behind.values())
    if counts[0] == counts[-1]:
        return None  # a tie names no edge; fall back rather than guess

    guarded = min(behind, key=lambda iface: behind[iface])
    for assignment in plan.assignments:
        if assignment.device == device.id and assignment.interface_hint == guarded:
            return assignment.subnet_id
    return None


def _build_firewall_commands(
    spec: NetworkSpec,
    plan: AddressPlan,
    assignment_ports: dict[tuple[str, str], str],
    device,
    model: Any,
) -> tuple[list[str], dict[str, str]]:
    """Interface + ACL CLI for an ASA, in shapes proven against live PT.

    Returns ``(commands, nameif_by_subnet)``: the second element maps each of
    this ASA's subnet ids to the ``nameif`` its interface got, so the caller
    can render an ASA static route against the interface the next hop is
    reachable through (PT's ASA needs ``route <nameif> ...``; ``ip route`` is
    silently ignored - S1.19 live root cause).

    Interface evidence (PT 5505/5506-X, all re-measured live): configureIosDevice
    ABORTS at the first invalid line, so every block leads with its most
    critical line; the 5505's factory Vlan1 gets a double ip push (a different
    value errors and zeroes the IP, the second push lands it - same-value
    re-push is a no-op) and NEVER a nameif re-push (the factory owns the name
    and a rename error would abort the block); fresh interfaces (5506-X ports,
    Vlan2) take the full ip/nameif/security-level/no-shutdown block. `hostname`
    is appended by the caller LAST: it is the one line never exercised on ASA
    CLI, so an abort there stays cosmetic.

    ACL evidence (see firewall_policy for the full matrix): with no access-group
    PT blocks every flow both ways, enforcement is protocol-selective on the
    outside nameif, and address-scoped rules ARE matched (S1.16 live A/B/C) -
    so a spec policy renders scoped operands in the ASA's netmask syntax. A
    policy replaces the default ICMP permit rather than widening it.
    """
    assignments = [
        a for a in plan.assignments
        if a.device == device.id and a.ip is not None
    ]
    n_links = sum(
        1 for link in spec.links
        if device.id in (link.a.device, link.b.device)
    )
    if model.pt_type == "5505" and (n_links > 2 or len(assignments) > 2):
        raise CompileError(
            f"ASA 5505 {device.id}: PT exposes only Vlan1/Vlan2, so at most "
            f"2 links/assignments fit (got {n_links} links, "
            f"{len(assignments)} assignments)"
        )

    # (physical port for the L2 side, config interface carrying the IP, assignment)
    resolved: list[tuple[str, str, Any]] = []
    for assignment in assignments:
        if assignment.interface_hint:
            hint = assignment.interface_hint
            if model.pt_type != "5505":
                resolved.append((hint, hint, assignment))
                continue
            if hint.startswith("Ethernet0/"):
                physical = hint
                config_iface = _asa5505_config_iface(hint)
            elif hint.startswith("Vlan"):
                config_iface = hint
                try:
                    physical = f"Ethernet0/{int(hint.split('Vlan', 1)[1]) - 1}"
                except ValueError:
                    raise CompileError(
                        f"ASA 5505 {device.id}: bad Vlan hint {hint!r}"
                    ) from None
            else:
                raise CompileError(
                    f"ASA 5505 {device.id}: hint {hint!r} is neither an "
                    "Ethernet0/x switchport nor a Vlan interface"
                )
            resolved.append((physical, config_iface, assignment))
            continue
        config_iface = assignment_ports.get((device.id, assignment.subnet_id))
        if not config_iface:
            raise CompileError(
                f"no resolved interface for address assignment on {device.id}"
            )
        if model.pt_type == "5505":
            try:
                physical = f"Ethernet0/{int(config_iface.split('Vlan', 1)[1]) - 1}"
            except ValueError:
                raise CompileError(
                    f"ASA 5505 {device.id}: assignment port {config_iface!r} "
                    "is not a Vlan interface"
                ) from None
        else:
            physical = config_iface
        resolved.append((physical, config_iface, assignment))

    cmds: list[str] = []
    #: subnet id -> the nameif its interface carries, for ASA route rendering.
    nameif_by_subnet: dict[str, str] = {}
    if model.pt_type == "5505":
        # L2 joins first (r12 emission order): each switchport into the vlan
        # whose SVI will carry its subnet's gateway address.
        joined: set[str] = set()
        for physical, _, _ in resolved:
            if physical in joined:
                continue
            joined.add(physical)
            vlan_id = int(physical.split("/", 1)[1]) + 1
            cmds += [
                f"interface {physical}",
                f"switchport access vlan {vlan_id}",
                # The 5505 physical ports default to shutdown. The switchport
                # line puts them in the right VLAN, but the link stays down
                # until we explicitly bring them up - without this, ICMP from
                # the inside Vlan SVI never crosses to the outside VLAN's
                # neighbour (A-005..A-008 were all blocked because Ethernet0/1
                # stayed ``up=False`` despite being cabled).
                "no shutdown",
                "exit",
            ]

    acl_targets: list[str] = []  # nameif values that need the ICMP ACL
    policy_targets: list[str] = []  # nameif values a spec policy binds to
    # The name of an ASA interface is policy, not decoration: PT's ASA denies
    # traffic from the level-0 interface to the level-100 one, so calling the
    # WAN port "inside" turns the perimeter into a wall against its own LAN.
    # The name therefore comes from the topology (which side the ASA guards),
    # never from the order the assignments happen to be listed in.
    outside_subnet = _asa_outside_subnet(spec, plan, device)
    # #37: a spec may name its own zones. Keyed by the interface it configures,
    # so `nameif dmz` on GigabitEthernet1/2 means exactly that interface.
    declared_asa = {
        entry.interface: (entry.nameif, entry.security_level)
        for entry in device.asa_interfaces
    }
    for index, (physical, config_iface, assignment) in enumerate(resolved):
        mask = plan.allocation_by_subnet(assignment.subnet_id)
        if mask is None:  # pragma: no cover - plan/spec consistency
            raise CompileError(f"assignment {assignment.device} has no allocation")
        ip_line = f"ip address {assignment.ip} {mask.netmask}"
        if config_iface == "Vlan1":
            # Factory-named, factory-IP Vlan1 (see docstring): double ip
            # push, security/shutdown, and no nameif line at all. PT's 5505
            # factory names it `inside`, which is the name a route via this
            # interface must use.
            nameif_by_subnet[assignment.subnet_id] = "inside"
            cmds += [
                f"interface {config_iface}", ip_line, "exit",
                f"interface {config_iface}", ip_line, "exit",
                f"interface {config_iface}",
                "security-level 100", "no shutdown", "exit",
            ]
        elif model.pt_type == "5505":
            # Vlan2 is fresh in PT: the full r10/r12 block applies at once.
            declared = declared_asa.get(config_iface) or declared_asa.get(physical)
            nameif, level = declared if declared else ("outside", 0)
            nameif_by_subnet[assignment.subnet_id] = nameif
            cmds += [
                f"interface {config_iface}", ip_line,
                f"nameif {nameif}", f"security-level {level}",
                "no shutdown", "exit",
            ]
            if nameif == "outside":
                acl_targets.append(nameif)
        else:
            # 5506-X family: clean interfaces. ip FIRST — accepted before
            # nameif (round-4) and survives a later abort (r8).
            declared = declared_asa.get(config_iface) or declared_asa.get(physical)
            if declared is not None:
                nameif, level = declared
            elif outside_subnet is not None:
                # Topology-derived (S1.19): the port the default route exits
                # is the outside; every other addressed port is inside.
                if assignment.subnet_id == outside_subnet:
                    nameif, level = "outside", 0
                else:
                    nameif, level = "inside", 100
            elif index == 0:
                nameif, level = "inside", 100
            else:
                nameif = "outside" if index == 1 else f"outside{index}"
                level = 0
            if level == 0:
                acl_targets.append(nameif)
            nameif_by_subnet[assignment.subnet_id] = nameif
            cmds += [
                f"interface {physical}", ip_line,
                f"nameif {nameif}", f"security-level {level}",
                "no shutdown", "exit",
            ]

    # A spec policy REPLACES the default permit: if the spec states what may
    # cross, the engine must not quietly widen it back to "icmp any any".
    try:
        policy_cmds, policy_acls = firewall_policy.build_policy_commands(
            spec, device.id
        )
    except firewall_policy.PolicyCompileError as exc:
        raise CompileError(str(exc)) from exc
    if policy_acls:
        cmds.extend(policy_cmds)
        return cmds, nameif_by_subnet

    # Otherwise the default: an ICMP permit on each security-level-0 nameif.
    # With no access-group at all PT blocks EVERY flow in both directions -
    # it does not implement the real ASA's implicit inside->outside rule - so
    # without this the firewall would silently black-hole the topology.
    # Names: "out_in" is the exact string proven live.
    for i, nameif_value in enumerate(acl_targets):
        acl_name = "out_in" if i == 0 else f"out_in{i + 1}"
        cmds += [
            f"access-list {acl_name} extended permit icmp any any",
            f"access-group {acl_name} in interface {nameif_value}",
        ]
    return cmds, nameif_by_subnet


def _asa_route_nameif(
    plan: AddressPlan,
    device,
    route,
    nameif_by_subnet: dict[str, str],
) -> str:
    """The nameif an ASA route to ``route.next_hop`` must name.

    PT's ASA installs a static route only in its own form -
    ``route <nameif> <net> <mask> <next_hop> <metric>`` - and only when the
    nameif is the interface the next hop is reachable through. The IOS
    ``ip route`` form is accepted and then silently never installed: live
    probe (PT 9.0.1, S1.19) showed every flow across a routed ASA dropping
    100% with ``ip route`` and passing on the first probe once the same route
    was pushed as ``route inside ... 1``.
    """
    if not route.next_hop:
        raise CompileError(
            f"ASA {device.id}: the static route to {route.destination} has no "
            "next_hop. PT's ASA `route` needs a gateway address, so an "
            "exit-interface-only route cannot be pushed - refused by name "
            "rather than emitted as a line the ASA would silently ignore."
        )
    hop = ipaddress.ip_address(route.next_hop)
    for assignment in plan.assignments:
        if assignment.device != device.id or not assignment.ip:
            continue
        nameif = nameif_by_subnet.get(assignment.subnet_id)
        if nameif is None:
            continue
        allocation = plan.allocation_by_subnet(assignment.subnet_id)
        if allocation is None:  # pragma: no cover - plan/spec consistency
            continue
        network = ipaddress.ip_network(
            f"{allocation.network}/{allocation.prefix_len}", strict=False
        )
        if hop in network:
            return nameif
    raise CompileError(
        f"ASA {device.id}: next hop {route.next_hop} of the route to "
        f"{route.destination} is on no subnet this ASA is addressed on, so "
        "PT's ASA has no interface to reach it through."
    )


def _add_firewall_config_op(
    dag: OperationDag,
    spec: NetworkSpec,
    plan: AddressPlan,
    assignment_ports: dict[tuple[str, str], str],
    device,
    model: Any,
    device_ops: dict[str, str],
) -> None:
    """configure_ios op for an ASA: proven commands + routes, hostname LAST.

    Deps cover every link touching the device: the switchport joins inside
    this op configure the very ports those links terminate on.
    """
    commands, nameif_by_subnet = _build_firewall_commands(
        spec, plan, assignment_ports, device, model
    )
    static_routes: list[dict[str, str]] = []
    if spec.routing.mode == "static":
        for route in spec.routing.static_routes:
            if route.device != device.id:
                continue
            static_routes.append(
                {
                    "destination": route.destination,
                    "next_hop": route.next_hop or "",
                    "exit_interface": route.exit_interface or "",
                }
            )
            # An ASA static route needs a dotted netmask, not CIDR, AND the
            # ASA's own `route <nameif> ... <metric>` form: PT's ASA ignores
            # `ip route` entirely (S1.19 live root cause).
            net, mask = _split_cidr(route.destination)
            nameif = _asa_route_nameif(plan, device, route, nameif_by_subnet)
            commands.append(
                f"route {nameif} {net} {mask} {route.next_hop} 1"
            )
    commands.append(f"hostname {device.id}")
    deps = [device_ops[device.id], *_link_ops_for_device(dag, device.id)]
    op = NetworkOperation(
        operation_id=f"op-config-{device.id}",
        operation_type="configure_ios",
        inputs={
            "device": device.id,
            "commands": commands,
            "static_routes": static_routes,
        },
        dependencies=tuple(dict.fromkeys(deps)),
        preconditions=(Check("device_present", device.id, "present"),),
        expected_postconditions=(
            Check("ios_accepted", device.id, "commands accepted"),
        ),
        classification=MutationClassification.CONFIGURE,
        risk=Risk.MEDIUM,
        blast_radius=(device.id,),
        retry_policy=1,
        idempotency_key=key_for("configure_ios", device.id, commands),
    )
    dag.add(op)


def _flow_destination_ip(spec, plan, test) -> str | None:
    """Which address of the destination a flow probe should hit (#24).

    A spec can say so outright (`destination_ip`, or `destination_interface`
    to have it read off the plan). Otherwise a target with several addresses
    used to make the adapter refuse to guess - "destination 'rbr' has several
    IPs" - and the probe never ran at all. Falling back to the address the
    destination shares with the source's subnet picks the one the source can
    actually reach, and `None` keeps the old honest refusal when no such
    address exists.
    """
    explicit = getattr(test, "destination_ip", None)
    if explicit:
        return str(explicit)
    interface = getattr(test, "destination_interface", None)
    if interface:
        for a in plan.assignments:
            if a.device == test.destination and a.interface_hint == interface:
                return a.ip
        return None
    candidates = [a for a in plan.assignments if a.device == test.destination and a.ip]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].ip
    source_subnets = {
        a.subnet_id for a in plan.assignments if a.device == test.source
    }
    shared = [a for a in candidates if a.subnet_id in source_subnets]
    if len(shared) == 1:
        return shared[0].ip
    return None


def compile_spec(
    spec: NetworkSpec,
    plan: AddressPlan,
    packet_tracer_version: str = "unknown",
    *,
    workspace_names: "Observable[list[str]] | None" = None,
    strict_ports: bool = True,
) -> CompileResult:
    """Compile a validated spec + addressing plan into an OperationDag.

    `workspace_names` (S1.4) is an optional device-name read of the live
    workspace; when given, preflight refuses a spec whose device names
    already exist there. Callers without a workspace (lint, net_preflight,
    repair) pass nothing and keep the old behaviour.

    `strict_ports` (S1.14/F11): with the default True, an unresolvable port
    refuses the whole spec by name. net_build passes False so one bad cable
    becomes a scoped preflight failure (the bad link op pre-blocks, the rest
    still builds) instead of zeroing 134/134 ops.
    """
    _check_phase2_support(spec)
    _reject_unsupported_wireless(spec)
    _reject_wireless_criteria_without_links(spec)

    dag = OperationDag()
    device_ops: dict[str, str] = {}
    port_plan = solve_ports(spec, plan)
    if port_plan.incompatible:
        if strict_ports:
            raise CompileError("port preflight failed: " + "; ".join(port_plan.incompatible))
    _reject_unaddressable_assignments(spec, plan)
    _reject_gateway_wan_cabling(spec, plan, port_plan)
    layout = build_layout(spec)
    if not layout.valid:
        raise CompileError(
            "layout validation failed: "
            + "; ".join(issue.detail for issue in layout.issues)
        )

    # -- devices ------------------------------------------------------------
    # Decide which interface carries each router's per-subnet IP. The interface
    # must be the one the port solver actually cabled TOWARDS that subnet: a
    # router with a LAN link on Gi0/0 and a WAN link on Gi0/1 must get its LAN
    # IP on Gi0/0. Assigning by sorted-interface order instead put IPs on the
    # wrong ports whenever the assignment order diverged from the cable order,
    # and the router then answered nothing while every op still reported green.
    assignment_ports = _assign_router_ports(spec, plan, port_plan)

    for device in spec.devices:
        model = _model_for(spec, device.id, device.role)
        x, y = layout.positions[device.id]
        op = NetworkOperation(
            operation_id=f"op-create-{device.id}",
            operation_type="create_device",
            inputs={
                "name": device.id,
                "role": device.role.value,
                "model": model,
                "position": [x, y],
            },
            preconditions=(Check("device_absent", device.id, "absent"),),
            expected_postconditions=(
                Check("device_present", device.id, "present"),
                Check("device_model", device.id, model),
            ),
            classification=MutationClassification.CREATE,
            risk=Risk.LOW,
            blast_radius=(device.id,),
            retry_policy=1,
            idempotency_key=key_for("create_device", device.id, model),
        )
        dag.add(op)
        device_ops[device.id] = op.operation_id

    # -- modules -------------------------------------------------------------
    # Each module the port solver requires gets an install operation BEFORE the
    # link that consumes its port exists (§9). Without this the DAG references
    # an interface (Serial0/0/0, GigabitEthernet0/2/0) that PT never created.
    module_ops: dict[tuple[str, str], str] = {}
    seen_modules: set[tuple[str, str, str]] = set()
    for requirement in port_plan.module_requirements:
        dedup_key = (requirement.device, requirement.slot, requirement.module_name)
        if dedup_key in seen_modules:
            continue  # one module per slot; two serial links share the HWIC-2T
        seen_modules.add(dedup_key)
        # The port this install must expose: first port of the module in that slot.
        expected_port = (
            requirement.ports_added[0] if requirement.ports_added else ""
        )
        op = NetworkOperation(
            operation_id=f"op-module-{requirement.device}-{requirement.slot}",
            operation_type="install_module",
            inputs={
                "device": requirement.device,
                "slot": requirement.slot,
                "module": requirement.module_name,
                "expected_port": expected_port,
            },
            dependencies=(device_ops[requirement.device],),
            preconditions=(Check("device_present", requirement.device, "present"),),
            expected_postconditions=(
                Check("module_port_visible", requirement.device, expected_port),
            ),
            classification=MutationClassification.CREATE,
            risk=Risk.LOW,
            blast_radius=(requirement.device,),
            retry_policy=1,
            idempotency_key=key_for("install_module", requirement.device, requirement.slot, requirement.module_name),
        )
        dag.add(op)
        module_ops[(requirement.device, requirement.slot)] = op.operation_id

    # -- wireless associations ------------------------------------------------
    # PT associates a wireless client on its own once both devices exist, but
    # the client needs a radio first and the result must be read back. Both
    # are operations so the DAG stays honest: a failed association is a
    # failed build, not a drawn antenna.
    wireless_ops: dict[tuple[str, str], str] = {}
    for link in spec.links:
        if link.media != "wireless":
            continue
        client, ap = _wireless_ends(spec, link)
        if client is None or ap is None:
            continue
        model = resolve_model(_model_for(spec, client, _device_of(spec, client).role))
        from ..wireless import module_for_model

        spec_module = module_for_model(model.pt_type if model is not None else None)
        if spec_module is None:  # pragma: no cover - guarded by _reject_unsupported_wireless
            raise CompileError(
                f"link {link.id}: {client} has no wireless NIC module in the catalog"
            )
        module, slot = spec_module
        radio_id = f"op-wifi-radio-{client}"
        if radio_id not in {n.operation.operation_id for n in dag.nodes.values()}:
            dag.add(NetworkOperation(
                operation_id=radio_id,
                operation_type="install_wireless_radio",
                inputs={
                    "device": client,
                    "module": module,
                    "slot": slot,
                    "interface": "Wireless0",
                },
                dependencies=(device_ops[client],),
                preconditions=(Check("device_present", client, "present"),),
                expected_postconditions=(
                    Check("wireless_interface_visible", client, "Wireless0"),
                ),
                classification=MutationClassification.CONFIGURE,
                risk=Risk.LOW,
                blast_radius=(client,),
                retry_policy=1,
                idempotency_key=key_for("install_wireless_radio", client, module, slot),
            ))
        op = NetworkOperation(
            operation_id=f"op-wifi-assoc-{link.id}",
            operation_type="associate_wireless",
            inputs={
                "link_id": link.id,
                "client": client,
                "access_point": ap,
                "client_interface": "Wireless0",
                "channel": _wireless_channel(spec, link),
            },
            dependencies=(device_ops[client], device_ops[ap], radio_id),
            preconditions=(
                Check("device_present", client, "present"),
                Check("device_present", ap, "present"),
                Check("wireless_interface_visible", client, "Wireless0"),
            ),
            expected_postconditions=(
                Check("wireless_associated", f"{client}->{ap}", "Antenna"),
            ),
            classification=MutationClassification.CONFIGURE,
            risk=Risk.LOW,
            blast_radius=(client, ap),
            retry_policy=2,
            idempotency_key=key_for(
                "associate_wireless", link.id, client, ap, "Wireless0"
            ),
        )
        dag.add(op)
        wireless_ops[(link.a.device, link.b.device)] = op.operation_id

    # -- links --------------------------------------------------------------
    for link in spec.links:
        if link.media == "wireless":
            continue  # handled by the association operation above
        a_device, a_port, b_device, b_port = port_plan.link_endpoints[link.id]
        media = port_plan.resolutions[f"{a_device}:{a_port}"].media
        link_deps = [device_ops[link.a.device], device_ops[link.b.device]]
        # A link that terminates on a module port must wait for the install.
        for dev, port in ((a_device, a_port), (b_device, b_port)):
            resolution = port_plan.resolutions.get(f"{dev}:{port}")
            if resolution is None or not resolution.module_required:
                continue
            model = _model_for(spec, dev, spec.device_by_id(dev).role)
            for module in port_plan.module_requirements:
                if module.device != dev or module.module_name != resolution.module_required:
                    continue
                if module.ports_added and port in module.ports_added:
                    install_op = module_ops.get((dev, module.slot))
                    if install_op and install_op not in link_deps:
                        link_deps.append(install_op)
        op = NetworkOperation(
            operation_id=f"op-link-{link.id}",
            operation_type="create_link",
            inputs={
                "link_id": link.id,
                "a_device": a_device,
                "a_port": a_port,
                "b_device": b_device,
                "b_port": b_port,
                "media": media,
            },
            dependencies=tuple(dict.fromkeys(link_deps)),
            preconditions=(
                Check("device_present", link.a.device, "present"),
                Check("device_present", link.b.device, "present"),
                Check("port_free", f"{link.a.device}:{a_port}", "free"),
                Check("port_free", f"{link.b.device}:{b_port}", "free"),
            ),
            expected_postconditions=(
                Check("link_present", link.id, "present"),
            ),
            classification=MutationClassification.LINK,
            risk=Risk.LOW,
            blast_radius=(link.a.device, link.b.device),
            retry_policy=1,
            idempotency_key=key_for(
                "create_link", tuple(sorted([(link.a.device, a_port), (link.b.device, b_port)]))
            ),
        )
        dag.add(op)

    # -- configuration: routers ----------------------------------------------
    route_deps: list[str] = []
    # A firewall or wireless router is a router to the compiler: the
    # firewall model resolves the ASA path and the home-gateway model
    # resolves the Linksys path. Scoping this to ROUTER alone left
    # every `role: firewall` device without a config op (#14).
    IOS_CONFIG_ROLES = (
        DeviceRole.ROUTER,
        DeviceRole.FIREWALL,
        DeviceRole.WIRELESS_ROUTER,
    )
    for device in spec.devices:
        if device.role not in IOS_CONFIG_ROLES:
            continue
        firewall = _resolve_firewall_model(spec, device)
        if firewall is not None:
            # ASA: its own emission (PT-verified shapes only) and its own
            # op — the generic router path below would push IOS feature
            # commands the ASA CLI never accepted in our probes.
            _add_firewall_config_op(
                dag, spec, plan, assignment_ports, device, firewall, device_ops
            )
            continue
        if _resolve_home_gateway_model(spec, device) is not None:
            # Home gateway: LAN address on the Vlan1 SVI plus a DHCP service.
            # The generic router path would address a cabled LAN port, which
            # is not where PT puts the address.
            _add_home_gateway_config_op(
                dag, spec, plan, assignment_ports, device, device_ops
            )
            continue
        commands: list[str] = [f"hostname {device.id}"]
        interface_commands: list[str] = []
        configured_ifaces: list[str] = []
        dce_sides = serial_dce_sides(spec, port_plan)
        for assignment in plan.assignments:
            if assignment.device != device.id or assignment.ip is None:
                continue
            iface = assignment.interface_hint or assignment_ports.get(
                (device.id, assignment.subnet_id)
            )
            if not iface:
                raise CompileError(
                    f"no resolved interface for address assignment on {device.id}"
                )
            # Bug #6: a dotted subinterface must NOT be addressed here. This
            # path emits `ip address` with no encapsulation yet, PT rejects it,
            # configureIosDevice aborts on the first rejected line - and every
            # route/DHCP/NAT line appended behind it is lost. The VLAN
            # renderer below owns subinterfaces, encapsulation first.
            if "." in iface:
                if iface not in configured_ifaces:
                    configured_ifaces.append(iface)
                link_dep = _link_op_for_port(dag, device.id, iface.split(".", 1)[0])
                if link_dep and link_dep not in route_deps:
                    route_deps.append(link_dep)
                continue
            mask = plan.allocation_by_subnet(assignment.subnet_id)
            if mask is None:  # pragma: no cover - plan/spec consistency
                raise CompileError(f"assignment {assignment.device} has no allocation")
            if iface not in configured_ifaces:
                configured_ifaces.append(iface)
            link_dep = _link_op_for_port(dag, device.id, iface)
            # A back-to-back serial link is a lab wire with no timing of its
            # own: exactly one end generates the clock or the line protocol
            # drops (measured in _probe_serial5.py). `serial_dce_sides` owns
            # which end that is, so this cannot put a clock on both.
            clock_rate = dce_sides.get((device.id, iface))
            # Each interface block ends with `exit` back to global config.
            # Without it PT's console stays in the first interface's context
            # and the next `interface X` + `ip address` lands on the WRONG
            # port (QA §2.3: LAN IP observed on the WAN interface).
            interface_commands.extend(
                [
                    f"interface {iface}",
                    f"ip address {assignment.ip} {mask.netmask}",
                    *([f"clock rate {clock_rate}"] if clock_rate else []),
                    "no shutdown",
                    "exit",
                ]
            )
            if link_dep and link_dep not in route_deps:
                route_deps.append(link_dep)
        if interface_commands:
            commands.extend(interface_commands)
        static_routes: list[dict[str, str]] = []
        if spec.routing.mode == "static":
            for route in spec.routing.static_routes:
                if route.device != device.id:
                    continue
                static_routes.append(
                    {
                        "destination": route.destination,
                        "next_hop": route.next_hop or "",
                        "exit_interface": route.exit_interface or "",
                    }
                )
                # IOS static routes need a dotted netmask, not CIDR: 
                # `ip route 10.0.2.0 255.255.255.0 10.0.0.2`. Emitting
                # `10.0.2.0/24` makes IOS reject the line silently and the
                # ping between subnets stays blocked.
                net, mask = _split_cidr(route.destination)
                commands.append(
                    f"ip route {net} {mask} "
                    f"{route.next_hop or route.exit_interface or ''}".strip()
                )
        # -- feature commands: VLAN subinterfaces, OSPF, DHCP, NAT, IPv6 --
        commands.extend(_build_vlan_router_commands(spec, plan).get(device.id, []))
        commands.extend(_build_ospf_commands(spec).get(device.id, []))
        commands.extend(_build_dhcp_commands(spec, plan).get(device.id, []))
        commands.extend(_build_nat_commands(spec).get(device.id, []))
        commands.extend(_build_ipv6_commands(spec).get(device.id, []))
        # -- spec policy on this router (#16): numbered IOS ACL, bound `in`
        # on every interface this path addressed (live-probed shapes; an
        # empty rule set returns nothing, so routers without policies are
        # untouched). Re-entering `interface X` here is deliberate: the
        # address blocks above already exited back to global config.
        try:
            policy_cmds, _policy_acls = firewall_policy.build_router_policy_commands(
                spec, device.id, configured_ifaces
            )
        except firewall_policy.PolicyCompileError as exc:
            raise CompileError(str(exc)) from exc
        commands.extend(policy_cmds)
        deps: list[str] = [device_ops[device.id]]
        # interfaces must exist before they are configured where a link op
        # targets the configured port
        deps.extend(d for d in route_deps if d.startswith("op-link-"))
        op = NetworkOperation(
            operation_id=f"op-config-{device.id}",
            operation_type="configure_ios",
            inputs={
                "device": device.id,
                "commands": commands,
                "static_routes": static_routes,
                # Non-empty only for a router on a back-to-back serial link:
                # the executor then proves DCE/DTE against PT's own
                # `show controllers` output instead of trusting the emission.
                "serial_clocks": serial_clock_expectations(spec, port_plan).get(
                    device.id, []
                ),
            },
            dependencies=tuple(dict.fromkeys(deps)),
            preconditions=(Check("device_present", device.id, "present"),),
            expected_postconditions=(
                Check("ios_accepted", device.id, "commands accepted"),
            ),
            classification=MutationClassification.CONFIGURE,
            risk=Risk.MEDIUM,
            blast_radius=(device.id,),
            retry_policy=1,
            idempotency_key=key_for("configure_ios", device.id, commands),
        )
        dag.add(op)

    # -- configuration: VLAN switches ----------------------------------------
    switch_vlan_commands = _build_vlan_switch_commands(spec, port_plan)
    for switch_id, vlan_lines in switch_vlan_commands.items():
        switch_deps = [device_ops[switch_id]]
        op = NetworkOperation(
            operation_id=f"op-vlan-switch-{switch_id}",
            operation_type="configure_ios",
            inputs={
                "device": switch_id,
                "commands": vlan_lines,
                "static_routes": [],
            },
            dependencies=tuple(dict.fromkeys(switch_deps)),
            preconditions=(Check("device_present", switch_id, "present"),),
            expected_postconditions=(
                Check("ios_accepted", switch_id, "commands accepted"),
            ),
            classification=MutationClassification.CONFIGURE,
            risk=Risk.MEDIUM,
            blast_radius=(switch_id,),
            retry_policy=1,
            idempotency_key=key_for("configure_vlan_switch", switch_id, vlan_lines),
        )
        dag.add(op)

    # -- wireless module installation ----------------------------------------
    wireless_devices = _detect_wireless_devices(spec)
    for dev_id in wireless_devices:
        if dev_id not in device_ops:
            continue
        op = NetworkOperation(
            operation_id=f"op-wireless-{dev_id}",
            operation_type="install_module",
            inputs={
                "device": dev_id,
                "slot": "0/0",
                "module": "HM-WIFI",
                "expected_port": "Wlan0",
            },
            dependencies=(device_ops[dev_id],),
            preconditions=(Check("device_present", dev_id, "present"),),
            expected_postconditions=(
                Check("module_port_visible", dev_id, "Wlan0"),
            ),
            classification=MutationClassification.CREATE,
            risk=Risk.LOW,
            blast_radius=(dev_id,),
            retry_policy=1,
            idempotency_key=key_for("install_module", dev_id, "0/0", "HM-WIFI"),
        )
        dag.add(op)

    # -- configuration: hosts (PCs, servers, laptops) --------------------------
    for device in spec.devices:
        if device.role not in (DeviceRole.PC, DeviceRole.SERVER, DeviceRole.LAPTOP):
            continue
        assignment = next((a for a in plan.assignments if a.device == device.id), None)
        if assignment is None:
            continue
        # A DHCP client has ip=None by design — it still needs a configure_host
        # op with dhcp=True so the adapter requests DHCP instead of a static IP.
        is_dhcp = bool(assignment.dhcp)
        if not is_dhcp and assignment.ip is None:
            continue
        link_dep = _link_op_for_port(
            dag, device.id, assignment.interface_hint or "FastEthernet0"
        )
        deps = [device_ops[device.id]] + ([link_dep] if link_dep else [])
        # A wireless client is addressed AFTER it associates: the association
        # step power-cycles the client to re-scan, which clears an address
        # configured before it.
        if device.id in _wireless_client_ids(spec):
            assoc_dep = _association_op_for_client(dag, device.id)
            if assoc_dep:
                deps.append(assoc_dep)
        host_inputs: dict[str, Any] = {
            "device": device.id,
            "dhcp": is_dhcp,
        }
        if not is_dhcp:
            host_inputs.update({
                "ip": assignment.ip,
                "mask": plan.allocation_by_subnet(assignment.subnet_id).netmask,
                "gateway": assignment.gateway,
            })
        # For a DHCP client the expected postcondition is that it HAS an IP
        # (any address obtained via DHCP), not a specific one. The check kind
        # stays host_ip with the expected value None meaning "any address".
        expected_ip = assignment.ip if not is_dhcp else None
        op = NetworkOperation(
            operation_id=f"op-host-{device.id}",
            operation_type="configure_host",
            inputs=host_inputs,
            dependencies=tuple(dict.fromkeys(deps)),
            preconditions=(Check("device_present", device.id, "present"),),
            expected_postconditions=(
                Check("host_ip", device.id, expected_ip),
            ) if expected_ip is not None else (),
            classification=MutationClassification.CONFIGURE,
            risk=Risk.MEDIUM,
            blast_radius=(device.id,),
            retry_policy=1,
            idempotency_key=key_for(
                "configure_host", device.id,
                assignment.ip or "dhcp",
                assignment.gateway or "",
            ),
        )
        dag.add(op)

    # -- verification ---------------------------------------------------------
    config_ops = [
        node.operation.operation_id
        for node in dag.nodes.values()
        if node.operation.classification is MutationClassification.CONFIGURE
    ]
    # -- dhcp convergence gate (#26) ----------------------------------------
    # A DHCP client that has not answered yet has no address to probe, so a
    # flow ran the moment its config op finished and failed with "destination
    # 'pc7' has no observable IP" on a build that was perfectly healthy. The
    # leases now have a bounded window to land before anything asks for a
    # verdict. A static build has nothing to wait for and gets no gate.
    _device_ids = {d.id for d in spec.devices}
    _dhcp_clients = sorted({
        a.device for a in plan.assignments if a.device in _device_ids and a.dhcp
    })
    flow_dep: tuple[str, ...] = ()
    if _dhcp_clients:
        dag.add(NetworkOperation(
            operation_id="op-wait-dhcp",
            operation_type="wait_dhcp",
            inputs={"devices": _dhcp_clients, "timeout_s": _DHCP_WAIT_TIMEOUT_S},
            dependencies=tuple(dict.fromkeys(config_ops)) or (),
            classification=MutationClassification.OBSERVE,
            risk=Risk.LOW,
            blast_radius=(),
            idempotency_key=key_for("wait_dhcp", *_dhcp_clients),
        ))
        flow_dep = ("op-wait-dhcp",)

    flow_ops: list[str] = []
    for criterion in spec.acceptance_criteria:
        test = criterion.test
        if getattr(test, "kind", "") == "flow":
            op = NetworkOperation(
                operation_id=f"op-flow-{criterion.id}",
                operation_type="verify_flow",
                inputs={
                    "criterion_id": criterion.id,
                    "requirement_ids": list(criterion.requirement_ids),
                    "source": test.source,
                    "destination": test.destination,
                    "expect": test.expect,
                    "transport": test.transport,
                    "port": test.port,
                    "destination_ip": _flow_destination_ip(spec, plan, test),
                },
                dependencies=tuple(
                    dict.fromkeys([*config_ops, *flow_dep])
                ) or (),
                classification=MutationClassification.OBSERVE,
                risk=Risk.LOW,
                blast_radius=(),
                idempotency_key=key_for(
                    "verify_flow", criterion.id, test.source, test.destination
                ),
            )
            dag.add(op)
            flow_ops.append(op.operation_id)

    verify_state = NetworkOperation(
        operation_id="op-verify-state",
        operation_type="verify_state",
        inputs={"device_names": [d.id for d in spec.devices]},
        dependencies=tuple(dict.fromkeys(config_ops)) or (),
        classification=MutationClassification.OBSERVE,
        risk=Risk.LOW,
        blast_radius=(),
        idempotency_key=key_for("verify_state", spec.spec_hash()),
    )
    dag.add(verify_state)

    # -- persistence ------------------------------------------------------------
    filename = f"{spec.name.replace(' ', '_').lower()}.pkt"
    save_deps = flow_ops + [verify_state.operation_id]
    dag.add(
        NetworkOperation(
            operation_id="op-save",
            operation_type="save_project",
            inputs={"filename": filename},
            dependencies=tuple(dict.fromkeys(save_deps)),
            classification=MutationClassification.PERSIST,
            risk=Risk.MEDIUM,
            blast_radius=(),
            idempotency_key=key_for("save_project", filename, spec.spec_hash()),
        )
    )

    dag.validate()

    # -- port solving + preflight (§9 + §G) --------------------------------
    # solve_ports() already ran once above (its incompatible list is the hard
    # compile gate); re-running it would duplicate the work and could diverge
    # from the port_plan the DAG was built from.
    preflight_result = run_preflight(spec, plan, port_plan, workspace_names=workspace_names)

    return CompileResult(
        dag=dag,
        unsupported_features=[],
        notes=[
            "Phase-2 compiler: devices, links, static IPv4, static routes, "
            "VLANs, OSPF, DHCP, NAT, IPv6, wireless, flow verification, save.",
        ],
        fingerprint=build_fingerprint(
            spec.spec_hash(),
            {
                "compiler": COMPILER_VERSION,
                "packet_tracer": packet_tracer_version,
                "adapter": "adapter-agnostic",
            },
            layout_seed=spec.layout_seed,
        ),
        port_plan=port_plan,
        preflight=preflight_result,
        assignment_ports=assignment_ports,
    )


def net_build(
    spec: NetworkSpec,
    plan: AddressPlan | None = None,
    gate: SafetyGate | None = None,
    adapter: "PacketTracerAdapter | None" = None,
    *,
    lint: bool = True,
    resume_from: str | None = None,
) -> tuple[CompileResult | None, str | None]:
    """Compile with preflight as a HARD execution gate (§G).

    Flow: request → lint → IPAM → compile → port/media/module preflight →
    safety gate → execution. If preflight fails, execution is blocked
    and a structured failure is returned immediately.

    `plan` may be None so the caller can hand over a spec it has not planned
    yet: the lint runs before addressing on purpose, because the planner
    aborts on the first bad address and would hide everything else (#35).

    `adapter` (S1.4) is read once for the workspace device names and handed
    to preflight, which refuses a spec whose names already exist in the
    workspace. Without an adapter there is no workspace to compare, so the
    collision check does not run (lint, net_preflight, repair).

    `resume_from` (S1.14/F11) is an explicit ownership claim over the
    workspace: the run continues a prior build, so its devices are expected
    there and the collision check is skipped. Anything else still gates.
    """
    from .compiler import compile_spec

    # #35: every offline check runs first and every problem comes back in one
    # message. Otherwise a spec with a bad address, a misaimed policy and a
    # missing port costs three separate build attempts to diagnose.
    if lint:
        from .spec_lint import format_issues, lint_spec, split_lint_problems

        problems = lint_spec(spec)
        if problems:
            hard, _deferred = split_lint_problems(problems)
            if hard:
                return None, format_issues(problems)
            # Only preflight-originated problems (S1.14/F11): they resurface
            # below in the compile-time preflight — same deterministic
            # checks, plus workspace context — where they scope to the
            # implicated ops instead of zeroing the build.

    if plan is None:
        from ..ipam.planner import plan_addressing

        plan = plan_addressing(spec)

    workspace_names = None
    if adapter is not None and resume_from is None:
        from ..core.observability import Observable

        try:
            workspace_names = adapter.list_device_names()
        except Exception as exc:
            # A read that raised is still a read we do not have: fail closed
            # in preflight instead of assuming the canvas is empty.
            workspace_names = Observable[list[str]].unknown(
                f"workspace read raised: {exc}"
            )

    result = compile_spec(
        spec, plan, workspace_names=workspace_names, strict_ports=False
    )

    # Size the mutation budget to the actual DAG instead of relying on
    # the default max_mutations=25 which would abort non-trivial builds.
    op_count = len(result.dag.nodes)
    budget = MutationBudget.for_operations(op_count)
    if gate is None:
        gate = SafetyGate(SafetyMode.AUTONOMOUS, budget)
    else:
        gate.budget = budget

    # Preflight is a HARD gate: if it fails, DO NOT execute the implicated
    # ops. A failure that scopes to specific ops (S1.14/F11) blocks only them
    # — plus their dependents, via the executor's dependency gate — while the
    # rest of the build still runs. A failure that cannot be scoped keeps the
    # old fail-closed behaviour: no result, execution blocked entirely.
    if result.preflight is not None and not result.preflight.can_execute:
        error_parts = result.preflight.errors
        if result.port_plan:
            for key, res in result.port_plan.resolutions.items():
                if res.constraint.name != "COMPATIBLE":
                    error_parts.append(f"{key}: {res.constraint.value}")
        scoped = preflight_blocked_ops(
            result.preflight, result.dag, result.port_plan
        )
        if scoped is None:
            return None, (
                "preflight blocked execution: "
                + "; ".join(error_parts)
            )
        result.preflight_blocked_ops = sorted(scoped)
        result.notes.append(
            "partial build: preflight refused "
            + "; ".join(error_parts)
        )
        return result, None

    # If preflight has warnings, still proceed but record them
    if result.preflight is not None and result.preflight.warnings:
        # Warnings don't block, but they're evidence
        pass

    return result, None


def _link_op_for_port(dag: OperationDag, device: str, interface: str) -> str | None:
    """The link op that terminates on (device, interface), if any."""
    for node in dag.nodes.values():
        op = node.operation
        if op.operation_type != "create_link":
            continue
        inputs = op.inputs
        if (inputs.get("a_device"), inputs.get("a_port")) == (device, interface) or (
            inputs.get("b_device"), inputs.get("b_port")
        ) == (device, interface):
            return op.operation_id
    return None


def _link_ops_for_device(dag: OperationDag, device: str) -> list[str]:
    """Every create_link op that touches `device`, in DAG insertion order."""
    out: list[str] = []
    for node in dag.nodes.values():
        op = node.operation
        if op.operation_type != "create_link":
            continue
        if device in (op.inputs.get("a_device"), op.inputs.get("b_device")):
            out.append(op.operation_id)
    return out


def _create_op_for_device(dag: OperationDag, device: str) -> str | None:
    """The create_device op for `device`, if the DAG has one."""
    for node in dag.nodes.values():
        op = node.operation
        if op.operation_type == "create_device" and op.inputs.get("name") == device:
            return op.operation_id
    return None


def _device_config_ops(dag: OperationDag, device: str) -> list[str]:
    """The device's own config ops (addressing, VLAN, wireless, modules) —
    everything that configures it short of creating it or cabling it."""
    out: list[str] = []
    for node in dag.nodes.values():
        op = node.operation
        if op.inputs.get("device") != device:
            continue
        if op.operation_type in ("create_device", "create_link"):
            continue
        if op.operation_id.startswith("op-flow-"):
            continue
        out.append(op.operation_id)
    return out


def preflight_blocked_ops(
    preflight: PreflightResult,
    dag: OperationDag,
    port_plan: PortPlan | None,
) -> set[str] | None:
    """Scope FAILED preflight checks to the ops they implicate (S1.14/F11).

    Returns the op ids to pre-block, or None when a failure cannot be scoped
    — the caller then keeps the old fail-closed behaviour (block everything).
    WARNING checks never block. Dependents of a blocked op block themselves
    through the executor's dependency gate.
    """
    create_names = {
        node.operation.inputs.get("name")
        for node in dag.nodes.values()
        if node.operation.operation_type == "create_device"
    }
    blocked: set[str] = set()
    for check in preflight.checks:
        if check.status is not PreflightLevel.FAIL:
            continue
        name = check.name
        scoped: set[str] = set()
        if name == "workspace_readable":
            return None
        elif name == "workspace_collision":
            # S1.4 guard stays global: a workspace that already holds spec
            # names may belong to another topology, and scoping it away
            # would silently bless building on top of foreign devices. The
            # only way past it is --resume, which is an explicit ownership
            # claim and bypasses the check before it ever runs (net_build).
            return None
        elif name.startswith("port_"):
            port_key = name[len("port_"):]
            device, _, iface = port_key.partition(":")
            link_op = _link_op_for_port(dag, device, iface) if iface else None
            if link_op is not None:
                scoped.add(link_op)
            else:
                scoped.update(_device_config_ops(dag, device))
                create_op = _create_op_for_device(dag, device)
                if create_op is not None:
                    scoped.add(create_op)
        elif name.startswith("module_"):
            rest = name[len("module_"):]
            device = next(
                (n for n in create_names if rest.startswith(n + "_")), None
            )
            module = rest[len(device) + 1:] if device is not None else rest
            if device is not None:
                scoped.update(
                    op_id for op_id in _device_config_ops(dag, device)
                    if dag.nodes[op_id].operation.operation_type == "install_module"
                )
                if port_plan is not None:
                    for resolution in port_plan.resolutions.values():
                        if (
                            resolution.device == device
                            and resolution.module_required == module
                        ):
                            link_op = _link_op_for_port(
                                dag, device, resolution.interface
                            )
                            if link_op is not None:
                                scoped.add(link_op)
        elif name.startswith("interface_hint_"):
            device = name[len("interface_hint_"):].rsplit("_", 1)[0]
            scoped.update(_device_config_ops(dag, device))
        elif name.startswith("reservation_"):
            rid = name[len("reservation_"):]
            device, iface = _reservation_endpoint(rid, create_names)
            if device is None:
                return None
            link_op = _link_op_for_port(dag, device, iface) if iface else None
            if link_op is not None:
                scoped.add(link_op)
            else:
                scoped.update(_device_config_ops(dag, device))
        elif name.startswith("link_"):
            link_id = name[len("link_"):].rsplit("_", 1)[0]
            op_id = f"op-link-{link_id}"
            if op_id in dag.nodes:
                scoped.add(op_id)
            else:
                return None
        elif name.startswith("device_") and name.endswith("_role"):
            device = name[len("device_"):-len("_role")]
            create_op = _create_op_for_device(dag, device)
            if create_op is not None:
                scoped.add(create_op)
            else:
                return None
        else:
            # A failure we do not understand: fail closed, block everything.
            return None
        if not scoped:
            # A scoped category that names nothing in this DAG is a mapping
            # miss, not a pass — fail closed.
            return None
        blocked.update(scoped)
    return blocked


def _reservation_endpoint(
    rid: str, device_names: set[str]
) -> tuple[str | None, str | None]:
    """Split a reservation id into (device, interface).

    reserve_single() keys them `res-{device}-{interface}`; the removed bulk
    path keyed them `res-{device}:{interface}`. Device names win over naive
    splitting so ids containing dashes still parse.
    """
    body = rid[4:] if rid.startswith("res-") else rid
    for device in sorted(device_names, key=len, reverse=True):
        for sep in ("-", ":"):
            if body.startswith(device + sep):
                return device, body[len(device) + 1:]
    return None, None
