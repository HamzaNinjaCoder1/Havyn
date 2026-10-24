"""Hop-by-hop path tracer (S4.6).

The tracer walks the physical path between two endpoints and judges every hop
with the evidence the import actually collected: link state, administrative
state, VLAN membership, trunk allowance, interface addressing, routing table,
next-hop choice, ACLs and the destination's return path.

It reports the FIRST observable failing hop and labels each check:

* `pass` / `fail` — decided by observed evidence;
* `unknown` — the evidence needed to decide was not observable;
* `info` — context that does not by itself decide connectivity.

A trace with no `fail` is `config_consistent`, not `reachable`: config
consistency is not traffic. A trace with no `fail` but a critical `unknown`
stays `unknown`. That vocabulary is what keeps the tracer from turning "I saw
no problem" into "the path works".
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..core.network_spec import LinkSpec
from .config_reader import ConfigCorpus
from .models import (
    BrownfieldInventory,
    DeviceInventory,
    InterfaceInventory,
    Objective,
)
from .objectives import ObjectiveRegistry
from .parser import ParsedAclRule, ParsedConfig, acl_rule_matches

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
INFO = "info"


@dataclass(frozen=True)
class HopCheck:
    layer: str          # L1 | L2 | L3 | security | services
    name: str
    status: str
    detail: str
    evidence: tuple[str, ...] = ()
    suspected: bool = False

    @property
    def critical(self) -> bool:
        return self.status in (FAIL, UNKNOWN) or (
            self.status == PASS and self.name in _CRITICAL_NAMES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": list(self.evidence),
            "suspected": self.suspected,
        }


_CRITICAL_NAMES = {
    "link", "port-state", "access-vlan", "trunk-allowance", "vlan-defined",
    "addressing", "gateway", "route", "next-hop", "acl", "return-route",
    "dhcp-lease",
}


@dataclass
class Hop:
    index: int
    device: str
    ingress: str | None
    egress: str | None
    vlan: int | None
    checks: list[HopCheck] = field(default_factory=list)

    @property
    def status(self) -> str:
        for status in (FAIL, UNKNOWN):
            if any(check.status == status for check in self.checks):
                return status
        return PASS

    @property
    def failing(self) -> list[HopCheck]:
        return [check for check in self.checks if check.status == FAIL]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "device": self.device,
            "ingress": self.ingress,
            "egress": self.egress,
            "vlan": self.vlan,
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass
class TraceResult:
    source: str
    destination: str
    source_ip: str | None
    destination_ip: str | None
    objective_id: str | None
    hops: list[Hop] = field(default_factory=list)
    path: list[str] = field(default_factory=list)
    status: str = UNKNOWN      # failing | unknown | config_consistent
    first_failing_hop: int | None = None
    first_failing_check: HopCheck | None = None
    confirmed_fault: bool = False
    evidence: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    vlan: int | None = None
    transport: str = "icmp"

    @property
    def failing(self) -> bool:
        return self.status == FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "source_ip": self.source_ip,
            "destination_ip": self.destination_ip,
            "objective_id": self.objective_id,
            "path": list(self.path),
            "vlan": self.vlan,
            "status": self.status,
            "first_failing_hop": self.first_failing_hop,
            "first_failing_check": (
                self.first_failing_check.to_dict()
                if self.first_failing_check is not None
                else None
            ),
            "confirmed_fault": self.confirmed_fault,
            "evidence": list(self.evidence),
            "unknowns": list(self.unknowns),
            "transport": self.transport,
            "hops": [hop.to_dict() for hop in self.hops],
        }


class PathTracer:
    """Trace one objective across the imported network."""

    def __init__(
        self,
        inventory: BrownfieldInventory,
        corpus: ConfigCorpus,
        *,
        objectives: ObjectiveRegistry | None = None,
        expected_links: Iterable[LinkSpec] | None = None,
        expected_access_vlans: dict[tuple[str, str], int] | None = None,
    ):
        self.inventory = inventory
        self.corpus = corpus
        self.objectives = objectives or ObjectiveRegistry()
        self.expected_links = list(expected_links or [])
        # Expected access VLANs let a trace judge a port against the design
        # instead of against its own (possibly wrong) configuration.
        self.expected_access_vlans = dict(expected_access_vlans or {})

    # -- public -------------------------------------------------------------

    def trace_objective(self, objective: Objective) -> TraceResult:
        params = objective.params
        if objective.kind in ("reachability", "isolation"):
            source = str(params.get("source") or "")
            destination = str(params.get("destination") or "")
            if not source and params.get("vlan") is not None:
                # A VLAN-gateway objective has no source device: use the
                # switch that owns the VLAN as the origin.
                source = str(params.get("switch") or "")
            if not destination and params.get("destination_ip"):
                destination = source
            return self.trace(
                source,
                destination,
                destination_ip=params.get("destination_ip"),
                vlan=params.get("vlan"),
                objective_id=objective.id,
                transport=str(params.get("transport") or "icmp"),
            )
        if objective.kind == "addressing" and params.get("dhcp"):
            device = str(params.get("device") or "")
            return self.trace(
                device, device, objective_id=objective.id, dhcp_check=True
            )
        if objective.kind == "dns":
            device = str(params.get("device") or "")
            return self.trace(
                device, device, objective_id=objective.id, dns_check=True
            )
        return TraceResult(
            source=str(params.get("source") or ""),
            destination=str(params.get("destination") or ""),
            source_ip=None,
            destination_ip=None,
            objective_id=objective.id,
            status=UNKNOWN,
            unknowns=[
                f"objective kind {objective.kind!r} has no tracer rule"
            ],
        )

    def trace(
        self,
        source: str,
        destination: str,
        *,
        destination_ip: str | None = None,
        vlan: int | None = None,
        objective_id: str | None = None,
        dhcp_check: bool = False,
        dns_check: bool = False,
        transport: str = "icmp",
    ) -> TraceResult:
        result = TraceResult(
            source=source,
            destination=destination,
            source_ip=None,
            destination_ip=destination_ip,
            objective_id=objective_id,
            vlan=vlan,
            transport=transport,
        )
        source_device = self.inventory.device(source)
        destination_device = self.inventory.device(destination)
        if source_device is None:
            result.status = FAIL
            result.first_failing_hop = 0
            result.first_failing_check = HopCheck(
                "L1", "device", FAIL,
                f"source device {source!r} is not present in the imported network",
                (f"imported devices: {', '.join(self.inventory.device_names())}",),
                suspected=False,
            )
            result.confirmed_fault = True
            return result
        if destination_device is None:
            result.status = FAIL
            result.first_failing_hop = 0
            result.first_failing_check = HopCheck(
                "L1", "device", FAIL,
                f"destination device {destination!r} is not present",
                suspected=False,
            )
            result.confirmed_fault = True
            return result

        source_ip, source_ip_evidence = self._host_ip(source_device)
        result.source_ip = source_ip
        if result.destination_ip is None:
            result.destination_ip = self._destination_ip(
                destination_device, destination_ip, result
            )

        if dhcp_check:
            self._dhcp_check(source_device, result)
            return result
        if dns_check:
            self._dns_check(source_device, result)
            return result

        path = self._shortest_path(
            source,
            destination,
            require_router=self._requires_routing(
                source_device, destination_device, result
            ),
        )
        result.path = path
        if not path:
            result.status = FAIL
            result.first_failing_hop = 0
            result.first_failing_check = HopCheck(
                "L1", "link", FAIL,
                f"no cabled path exists between {source!r} and {destination!r}",
                tuple(
                    f"{link.a_device}:{link.a_port} <-> "
                    f"{link.b_device}:{link.b_port}"
                    for link in self.inventory.links
                ),
                suspected=False,
            )
            result.confirmed_fault = True
            return result

        flow_vlan = vlan
        self._trace_path(path, result, flow_vlan, source_ip)

        failures = [
            (hop.index, check)
            for hop in result.hops
            for check in hop.checks
            if check.status == FAIL
        ]
        unknowns = [
            f"hop {hop.index} {hop.device}: {check.name}: {check.detail}"
            for hop in result.hops
            for check in hop.checks
            if check.status == UNKNOWN
        ]
        result.unknowns = unknowns
        if failures:
            index, check = failures[0]
            result.status = FAIL
            result.first_failing_hop = index
            result.first_failing_check = check
            result.confirmed_fault = not check.suspected
        elif unknowns:
            result.status = UNKNOWN
        else:
            result.status = "config_consistent"
        result.evidence = [
            f"path: {' -> '.join(path)}",
            *(
                [f"source {source_ip}"] if source_ip else
                ["source address was not observed"]
            ),
            *(
                [f"destination {result.destination_ip}"]
                if result.destination_ip
                else ["destination address was not resolved"]
            ),
        ]
        return result

    # -- path discovery -----------------------------------------------------

    def _shortest_path(
        self, source: str, destination: str, *, require_router: bool = False
    ) -> list[str]:
        if source == destination:
            return [source]
        adjacency: dict[str, list[str]] = {}
        for link in self.inventory.links:
            adjacency.setdefault(link.a_device, []).append(link.b_device)
            adjacency.setdefault(link.b_device, []).append(link.a_device)

        def passes_through_router(path: list[str]) -> bool:
            return any(
                self._is_routing_device(self.inventory.devices.get(name))
                for name in path
            )

        queue: list[list[str]] = [[source]]
        seen = {(source, not require_router)}
        while queue:
            path = queue.pop(0)
            node = path[-1]
            if node == destination and (
                not require_router or passes_through_router(path)
            ):
                return path
            for neighbor in sorted(adjacency.get(node, [])):
                state = (neighbor, passes_through_router([*path, neighbor]))
                if state in seen:
                    continue
                seen.add(state)
                queue.append([*path, neighbor])
        return []

    def _has_alternate_path(
        self, source: str, destination: str, excluded_port: str
    ) -> bool:
        adjacency: dict[str, list[str]] = {}
        for link in self.inventory.links:
            if link.a_device == source and link.a_port == excluded_port:
                continue
            if link.b_device == source and link.b_port == excluded_port:
                continue
            adjacency.setdefault(link.a_device, []).append(link.b_device)
            adjacency.setdefault(link.b_device, []).append(link.a_device)
        queue = [source]
        seen = {source}
        while queue:
            node = queue.pop(0)
            if node == destination:
                return True
            for neighbor in adjacency.get(node, []):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        return False

    @staticmethod
    def _is_routing_device(device: DeviceInventory | None) -> bool:
        if device is None:
            return False
        role = str(device.role.value) if device.role.is_known else ""
        return device.category in (
            "router", "firewall", "wireless_router"
        ) or role == "l3_switch"

    def _requires_routing(
        self,
        source: DeviceInventory,
        destination: DeviceInventory,
        result: TraceResult,
    ) -> bool:
        source_subnet = self._host_subnet(source)
        destination_subnet = self._host_subnet(destination)
        if source_subnet is None or destination_subnet is None:
            return False
        return source_subnet != destination_subnet

    @staticmethod
    def _host_subnet(device: DeviceInventory) -> str | None:
        if device.host is not None and device.host.ip.is_known and device.host.mask.is_known:
            try:
                return str(
                    ipaddress.IPv4Network(
                        f"{device.host.ip.value}/{device.host.mask.value}",
                        strict=False,
                    )
                )
            except ValueError:
                return None
        for interface in device.interfaces.values():
            if interface.ip.is_known and interface.mask.is_known:
                try:
                    return str(
                        ipaddress.IPv4Network(
                            f"{interface.ip.value}/{interface.mask.value}",
                            strict=False,
                        )
                    )
                except ValueError:
                    continue
        return None

    def _segment_vlans(self, path: list[str]) -> tuple[list[int | None], int | None]:
        """The VLAN carried by each path segment, and the source VLAN."""
        segments: list[int | None] = []
        for index in range(len(path) - 1):
            link = self._link_for(self.inventory, path[index], path[index + 1])
            segments.append(self._link_vlan(link))
        source_vlan = next((v for v in segments if v is not None), None)
        return segments, source_vlan

    def _link_vlan(self, link) -> int | None:
        if link is None:
            return None
        for device_name, port_name in (
            (link.a_device, link.a_port), (link.b_device, link.b_port)
        ):
            expected = self.expected_access_vlans.get((device_name, port_name))
            if expected is not None:
                return expected
            device = self.inventory.device(device_name)
            if device is None:
                continue
            interface = device.interface(port_name)
            if interface is None:
                continue
            if interface.vlan.is_known:
                return int(interface.vlan.value)
            if interface.encapsulation_vlan.is_known:
                return int(interface.encapsulation_vlan.value)
        return None

    @staticmethod
    def _link_for(inventory: BrownfieldInventory, a: str, b: str):
        return inventory.link_between(a, b)

    # -- address resolution -------------------------------------------------

    @staticmethod
    def _host_ip(device: DeviceInventory) -> tuple[str | None, str]:
        if device.host is not None and device.host.ip.is_known:
            return str(device.host.ip.value), "host ipconfig /all"
        for interface in device.interfaces.values():
            if interface.ip.is_known:
                value = str(interface.ip.value)
                if value and value != "0.0.0.0":
                    return value, f"topology read-back:{interface.name}"
        return None, "no address was observed"

    def _destination_ip(
        self, device: DeviceInventory, explicit: str | None, result: TraceResult
    ) -> str | None:
        if explicit:
            return explicit
        if device.host is not None and device.host.ip.is_known:
            return str(device.host.ip.value)
        addresses = {
            str(interface.ip.value)
            for interface in device.interfaces.values()
            if interface.ip.is_known
            and str(interface.ip.value) not in ("", "0.0.0.0")
        }
        if len(addresses) == 1:
            return addresses.pop()
        if len(addresses) > 1:
            result.unknowns.append(
                f"{device.name} has several addresses {sorted(addresses)}; the "
                "objective did not name one"
            )
        return None

    def _flow_vlan(
        self,
        source: DeviceInventory,
        destination: DeviceInventory,
        destination_ip: str | None,
    ) -> int | None:
        # A router subinterface serving the destination subnet is the
        # strongest VLAN signal for an inter-VLAN flow.
        if destination_ip:
            try:
                dest = ipaddress.IPv4Address(destination_ip)
            except ValueError:
                dest = None
            if dest is not None:
                for device in self.inventory.devices.values():
                    for interface in device.interfaces.values():
                        if not (
                            interface.encapsulation_vlan.is_known
                            and interface.ip.is_known
                            and interface.mask.is_known
                        ):
                            continue
                        try:
                            net = ipaddress.IPv4Network(
                                f"{interface.ip.value}/{interface.mask.value}",
                                strict=False,
                            )
                        except ValueError:
                            continue
                        if dest in net:
                            return int(interface.encapsulation_vlan.value)
        # Otherwise the source's access port owns the VLAN.
        for peer, local_port, peer_port in self.inventory.neighbors(source.name):
            peer_device = self.inventory.device(peer)
            if peer_device is None or not peer_device.is_switch:
                continue
            interface = peer_device.interface(peer_port)
            if interface is not None and interface.vlan.is_known:
                return int(interface.vlan.value)
        return None

    # -- the walk -----------------------------------------------------------

    def _trace_path(
        self,
        path: list[str],
        result: TraceResult,
        vlan: int | None,
        source_ip: str | None,
    ) -> None:
        segment_vlans, source_vlan = self._segment_vlans(path)
        if result.vlan is None:
            result.vlan = vlan if vlan is not None else source_vlan
        for index, device_name in enumerate(path):
            device = self.inventory.devices[device_name]
            previous = path[index - 1] if index > 0 else None
            following = path[index + 1] if index + 1 < len(path) else None
            ingress = self._port_towards(device_name, previous) if previous else None
            egress = self._port_towards(device_name, following) if following else None
            ingress_vlan = segment_vlans[index - 1] if index > 0 else None
            egress_vlan = segment_vlans[index] if index < len(segment_vlans) else None
            hop = Hop(
                index=index,
                device=device_name,
                ingress=ingress,
                egress=egress,
                vlan=ingress_vlan if ingress_vlan is not None else egress_vlan,
            )
            if index == 0:
                self._source_checks(device, hop, result, result.vlan, source_ip)
            else:
                self._l1_checks(device, hop, ingress)
                if egress and egress != ingress:
                    # The outgoing port is as load-bearing as the incoming one;
                    # a shutdown on the far side of a switch was invisible when
                    # only the ingress port was checked.
                    self._l1_checks(device, hop, egress)
                self._l2_checks(
                    device, hop, ingress, egress, ingress_vlan, egress_vlan
                )
                role = str(device.role.value) if device.role.is_known else ""
                routes = device.category in (
                    "router", "firewall", "wireless_router"
                ) or role == "l3_switch"
                if not device.is_host and routes:
                    # A plain L2 switch has no routing table; asking it for a
                    # route would turn every bridged hop into a false UNKNOWN.
                    self._l3_checks(
                        device, hop, path, index, result, result.vlan, source_ip
                    )
                if not device.is_host:
                    self._security_checks(
                        device, hop, ingress, egress, source_ip,
                        result.destination_ip, result.transport,
                    )
            if index == len(path) - 1:
                if index > 0:
                    self._l1_checks(device, hop, ingress)
                self._destination_checks(device, hop, result, source_ip)
            result.hops.append(hop)
            if any(check.status == FAIL for check in hop.checks):
                # Later hops cannot be reached; record them as unexamined
                # rather than pretending their state is healthy.
                for remainder in path[index + 1:]:
                    result.hops.append(
                        Hop(
                            index=len(result.hops),
                            device=remainder,
                            ingress=None,
                            egress=None,
                            vlan=result.vlan,
                            checks=[
                                HopCheck(
                                    "L1", "reachability", UNKNOWN,
                                    f"not examined: hop {index} failed first",
                                )
                            ],
                        )
                    )
                break

    def _port_towards(self, device: str, peer: str | None) -> str | None:
        if peer is None:
            return None
        link = self._link_for(self.inventory, device, peer)
        if link is None:
            return None
        # The port on THIS device, not the peer's port.
        if link.a_device == device and link.b_device == peer:
            return link.a_port
        if link.b_device == device and link.a_device == peer:
            return link.b_port
        return None

    # -- checks -------------------------------------------------------------

    def _source_checks(
        self,
        device: DeviceInventory,
        hop: Hop,
        result: TraceResult,
        vlan: int | None,
        source_ip: str | None,
    ) -> None:
        interface = device.interface(hop.ingress) if hop.ingress else None
        if source_ip is None:
            hop.checks.append(
                HopCheck(
                    "L3", "addressing", FAIL,
                    f"{device.name} has no observable IPv4 address",
                    ("no IP on any observed interface",),
                )
            )
            return
        hop.checks.append(
            HopCheck(
                "L3", "addressing", PASS,
                f"source address {source_ip}",
                ("topology read-back" if device.host is None else "ipconfig /all",),
            )
        )
        if device.is_host and device.host is not None:
            if device.arp.is_known and result.destination_ip:
                entries = {
                    str(row.get("ip")) for row in device.arp.value or []
                    if isinstance(row, dict)
                }
                if result.destination_ip in entries:
                    hop.checks.append(
                        HopCheck(
                            "L3", "arp", PASS,
                            f"{device.name} has an ARP entry for "
                            f"{result.destination_ip}",
                            ("ARP table read-back",),
                        )
                    )
                else:
                    hop.checks.append(
                        HopCheck(
                            "L3", "arp", INFO,
                            f"{device.name} has no cached ARP entry for "
                            f"{result.destination_ip} yet",
                            ("ARP cache is populated only after traffic",),
                        )
                    )
            else:
                hop.checks.append(
                    HopCheck(
                        "L3", "arp", INFO,
                        "ARP cache was not readable; adjacency was judged from "
                        "the link and subnet instead",
                    )
                )
            gateway = (
                str(device.host.gateway.value)
                if device.host.gateway.is_known
                else None
            )
            mask = (
                str(device.host.mask.value)
                if device.host.mask.is_known
                else None
            )
            if not gateway:
                if self._on_link(source_ip, mask, result.destination_ip):
                    hop.checks.append(
                        HopCheck(
                            "L3", "gateway", INFO,
                            f"{device.name} has no default gateway; the "
                            "destination is on-link, so none is needed",
                        )
                    )
                else:
                    hop.checks.append(
                        HopCheck(
                            "L3", "gateway", FAIL,
                            f"{device.name} has no default gateway",
                            ("ipconfig /all reported no default gateway",),
                        )
                    )
            elif self._on_link(source_ip, mask, result.destination_ip):
                hop.checks.append(
                    HopCheck(
                        "L3", "gateway", INFO,
                        f"destination is on-link; gateway {gateway} is not used",
                        ("ipconfig /all",),
                    )
                )
            else:
                expected = self._gateway_for(source_ip, mask, device)
                if expected is not None and expected != gateway:
                    hop.checks.append(
                        HopCheck(
                            "L3", "gateway", FAIL,
                            f"{device.name} uses gateway {gateway}, but the "
                            f"router interface on its subnet is {expected}",
                            (
                                f"host gateway {gateway}",
                                f"subnet router address {expected}",
                            ),
                            suspected=False,
                        )
                    )
                else:
                    hop.checks.append(
                        HopCheck(
                            "L3", "gateway", PASS,
                            f"default gateway {gateway}",
                            ("ipconfig /all",),
                        )
                    )
        if interface is not None:
            self._l1_checks(device, hop, hop.ingress, include_link=True)
        elif device.is_host and hop.ingress is None:
            hop.checks.append(
                HopCheck(
                    "L1", "link", INFO,
                    "source has no physical path recorded", ()
                )
            )
        elif hop.egress is not None:
            # A router/switch source has no ingress port; its outgoing port
            # is the one whose cable and state decide whether the flow starts.
            self._l1_checks(device, hop, hop.egress, include_link=True)

    def _l1_checks(
        self,
        device: DeviceInventory,
        hop: Hop,
        port_name: str | None,
        *,
        include_link: bool = True,
    ) -> None:
        if not port_name:
            return
        interface = device.interface(port_name)
        if interface is None:
            hop.checks.append(
                HopCheck(
                    "L1", "link", FAIL,
                    f"{device.name} has no observed interface {port_name}",
                )
            )
            return
        if include_link:
            peers = interface.peers.value if interface.peers.is_known else None
            linked = interface.linked.value if interface.linked.is_known else None
            if linked is False:
                hop.checks.append(
                    HopCheck(
                        "L1", "link", FAIL,
                        f"{device.name}:{port_name} has no cable",
                        (f"read-back says {port_name} has no link",),
                        suspected=False,
                    )
                )
            elif peers is None and linked is None:
                hop.checks.append(
                    HopCheck(
                        "L1", "link", UNKNOWN,
                        f"{device.name}:{port_name} link state was not observed",
                    )
                )
            elif peers is not None and not peers:
                hop.checks.append(
                    HopCheck(
                        "L1", "link", FAIL,
                        f"{device.name}:{port_name} has no cable",
                        (f"peer list is empty on {port_name}",),
                        suspected=False,
                    )
                )
            elif peers:
                hop.checks.append(
                    HopCheck(
                        "L1", "link", PASS,
                        f"{device.name}:{port_name} is linked to {peers[0]}",
                        (f"peer {peers[0]}",),
                    )
                )
            else:
                hop.checks.append(
                    HopCheck(
                        "L1", "link", PASS,
                        f"{device.name}:{port_name} has a link (peer not reported)",
                        ("read-back: linked=true",),
                    )
                )
        state = interface.state
        if not state.is_known:
            hop.checks.append(
                HopCheck(
                    "L1", "port-state", UNKNOWN,
                    f"{device.name}:{port_name} operational state was not observed",
                )
            )
            return
        if str(state.value) == "up":
            hop.checks.append(
                HopCheck(
                    "L1", "port-state", PASS,
                    f"{device.name}:{port_name} is up", (state.source or "read-back",)
                )
            )
            return
        admin = (
            str(interface.admin_state.value)
            if interface.admin_state.is_known
            else None
        )
        if admin == "shutdown":
            hop.checks.append(
                HopCheck(
                    "L1", "port-state", FAIL,
                    f"{device.name}:{port_name} is administratively down",
                    (
                        "observed port state: down",
                        "running-config: shutdown",
                    ),
                    suspected=False,
                )
            )
        else:
            hop.checks.append(
                HopCheck(
                    "L1", "port-state", FAIL,
                    f"{device.name}:{port_name} is down",
                    (
                        "observed port state: down",
                        f"running-config admin state: {admin or 'not shut down'}",
                    ),
                    suspected=False,
                )
            )

    @staticmethod
    def _on_link(source_ip: str | None, mask: str | None, destination_ip: str | None) -> bool:
        if not source_ip or not mask or not destination_ip:
            return False
        try:
            return ipaddress.IPv4Address(destination_ip) in ipaddress.IPv4Network(
                f"{source_ip}/{mask}", strict=False
            )
        except ValueError:
            return False

    def _gateway_for(
        self, source_ip: str | None, mask: str | None, source: DeviceInventory
    ) -> str | None:
        """The router address on the source's subnet, when exactly one exists.

        Two candidates is ambiguity, not a vote: returning None keeps the
        gateway rule from flagging a host against a guess.
        """
        if not source_ip or not mask:
            return None
        try:
            network = ipaddress.IPv4Network(f"{source_ip}/{mask}", strict=False)
        except ValueError:
            return None
        candidates: set[str] = set()
        for device in self.inventory.devices.values():
            if device.name == source.name or device.category not in (
                "router", "firewall", "wireless_router"
            ):
                continue
            for interface in device.interfaces.values():
                if not (interface.ip.is_known and interface.mask.is_known):
                    continue
                try:
                    if ipaddress.IPv4Address(
                        str(interface.ip.value)
                    ) in network:
                        candidates.add(str(interface.ip.value))
                except ValueError:
                    continue
        return candidates.pop() if len(candidates) == 1 else None

    def _l2_checks(
        self,
        device: DeviceInventory,
        hop: Hop,
        ingress: str | None,
        egress: str | None,
        ingress_vlan: int | None,
        egress_vlan: int | None,
    ) -> None:
        if not device.is_switch:
            return
        parsed = self.corpus.parsed_for(device.name)
        # A frame cannot change VLAN inside an L2 switch: if the two access
        # segments carry different VLANs and neither side is a trunk, the
        # bridge cannot deliver it (this is the wrong-access-VLAN fault).
        if (
            ingress_vlan is not None
            and egress_vlan is not None
            and ingress_vlan != egress_vlan
            and ingress
            and egress
        ):
            ingress_iface = device.interface(ingress)
            egress_iface = device.interface(egress)
            if not any(
                i is not None and i.trunk.is_known and i.trunk.value
                for i in (ingress_iface, egress_iface)
            ):
                hop.checks.append(
                    HopCheck(
                        "L2", "access-vlan", FAIL,
                        f"{device.name} bridges VLAN {ingress_vlan} "
                        f"({ingress}) to VLAN {egress_vlan} ({egress}) with no "
                        "trunk or router between them",
                        (
                            f"ingress VLAN {ingress_vlan}",
                            f"egress VLAN {egress_vlan}",
                        ),
                        suspected=False,
                    )
                )
        for role, port_name, side_vlan, other_vlan in (
            ("ingress", ingress, ingress_vlan, egress_vlan),
            ("egress", egress, egress_vlan, ingress_vlan),
        ):
            if not port_name:
                continue
            interface = device.interface(port_name)
            if interface is None:
                continue
            is_trunk = bool(interface.trunk.is_known and interface.trunk.value)
            # A trunk segment has no single access VLAN: the VLAN it must
            # allow for THIS flow is the one on the other side of the hop.
            effective_vlan = side_vlan if side_vlan is not None else (
                other_vlan if is_trunk else None
            )
            if side_vlan is not None and not is_trunk:
                if interface.vlan.is_known:
                    observed = int(interface.vlan.value)
                    if observed != side_vlan:
                        hop.checks.append(
                            HopCheck(
                                "L2", "access-vlan", FAIL,
                                f"{device.name}:{port_name} ({role}) is in VLAN "
                                f"{observed}, but the flow needs VLAN {side_vlan}",
                                (
                                    f"observed access VLAN {observed}",
                                    f"flow VLAN {side_vlan}",
                                ),
                                suspected=False,
                            )
                        )
                    else:
                        hop.checks.append(
                            HopCheck(
                                "L2", "access-vlan", PASS,
                                f"{device.name}:{port_name} ({role}) is in VLAN {side_vlan}",
                                (interface.vlan.source or "read-back",),
                            )
                        )
                elif not interface.trunk.is_known:
                    hop.checks.append(
                        HopCheck(
                            "L2", "access-vlan", UNKNOWN,
                            f"{device.name}:{port_name} VLAN membership was not observed",
                        )
                    )
            if is_trunk and effective_vlan is not None:
                if interface.allowed_vlans.is_known:
                    allowed = {int(v) for v in interface.allowed_vlans.value or []}
                    if effective_vlan not in allowed:
                        hop.checks.append(
                            HopCheck(
                                "L2", "trunk-allowance", FAIL,
                                f"{device.name}:{port_name} trunk does not allow "
                                f"VLAN {effective_vlan}",
                                (
                                    f"allowed VLANs: {sorted(allowed) or 'none'}",
                                ),
                                suspected=False,
                            )
                        )
                    else:
                        hop.checks.append(
                            HopCheck(
                                "L2", "trunk-allowance", PASS,
                                f"{device.name}:{port_name} trunk allows VLAN {effective_vlan}",
                                (interface.allowed_vlans.source or "read-back",),
                            )
                        )
                else:
                    hop.checks.append(
                        HopCheck(
                            "L2", "trunk-allowance", UNKNOWN,
                            f"{device.name}:{port_name} trunk allowed list was "
                            "not observed",
                        )
                    )
            # VLAN existence, from the observed table when available.
            if effective_vlan is not None:
                defined, detail = self._vlan_defined(device, parsed, effective_vlan)
                if defined is True:
                    hop.checks.append(
                        HopCheck(
                            "L2", "vlan-defined", PASS,
                            f"VLAN {effective_vlan} is defined on {device.name}",
                            (detail,),
                        )
                    )
                elif defined is False:
                    hop.checks.append(
                        HopCheck(
                            "L2", "vlan-defined", FAIL,
                            f"VLAN {effective_vlan} is referenced on {device.name} but not defined",
                            (detail,),
                            suspected=False,
                        )
                    )
                else:
                    hop.checks.append(
                        HopCheck(
                            "L2", "vlan-defined", UNKNOWN,
                            f"VLAN {effective_vlan} definition on {device.name} was not observed",
                            (detail,),
                        )
                    )
            # Native VLAN mismatch across a trunk.
            if is_trunk:
                peer = self.inventory.neighbors(device.name)
                for peer_name, local_port, peer_port in peer:
                    if local_port != port_name:
                        continue
                    peer_device = self.inventory.device(peer_name)
                    if peer_device is None:
                        continue
                    peer_iface = peer_device.interface(peer_port)
                    if (
                        peer_iface is not None
                        and interface.native_vlan.is_known
                        and peer_iface.native_vlan.is_known
                        and int(interface.native_vlan.value)
                        != int(peer_iface.native_vlan.value)
                    ):
                        hop.checks.append(
                            HopCheck(
                                "L2", "native-vlan", FAIL,
                                f"native VLAN mismatch on {device.name}:{port_name} "
                                f"({interface.native_vlan.value}) vs {peer_name}:{peer_port} "
                                f"({peer_iface.native_vlan.value})",
                                ("trunk native VLAN is direction-dependent",),
                                suspected=False,
                            )
                        )
            # STP blocking, only when the device's table was observed.
            if device.stp.is_known:
                blocked = [
                    row for row in device.stp.value or []
                    if str(row.get("interface")) == port_name
                    and str(row.get("state")).upper() in ("BLK", "ALN")
                ]
                if blocked:
                    # STP blocking is only a fault when it removes the last
                    # path. In a loop, the blocked port is correct design and
                    # must not be reported as a connectivity failure.
                    peer_name = next(
                        (
                            name for name, local, _peer in self.inventory.neighbors(
                                device.name
                            )
                            if local == port_name
                        ),
                        None,
                    )
                    alternate = (
                        peer_name is not None
                        and self._has_alternate_path(
                            device.name, peer_name, port_name
                        )
                    )
                    hop.checks.append(
                        HopCheck(
                            "L2", "stp",
                            INFO if alternate else FAIL,
                            (
                                f"{device.name}:{port_name} is "
                                f"{blocked[0].get('state')} in spanning-tree"
                                + (
                                    "; an alternate forwarding path exists"
                                    if alternate else
                                    "; no alternate path was found"
                                )
                            ),
                            (f"show spanning-tree: {blocked[0]}",),
                            suspected=False,
                        )
                    )

    @staticmethod
    def _vlan_defined(
        device: DeviceInventory, parsed: ParsedConfig | None, vlan: int
    ) -> tuple[bool | None, str]:
        if device.vlans_table.is_known:
            ids = {
                int(row.get("id"))
                for row in device.vlans_table.value or []
                if isinstance(row, dict) and str(row.get("id", "")).isdigit()
            }
            return (vlan in ids), "show vlan brief"
        if parsed is not None and parsed.vlans:
            return (vlan in parsed.vlans), "running-config vlan block"
        return None, "no VLAN table or vlan block was observed"

    def _l3_checks(
        self,
        device: DeviceInventory,
        hop: Hop,
        path: list[str],
        index: int,
        result: TraceResult,
        vlan: int | None,
        source_ip: str | None,
    ) -> None:
        interface = device.interface(hop.ingress) if hop.ingress else None
        if interface is not None and device.category in (
            "router", "firewall", "wireless_router"
        ):
            addressed = interface
            if not (interface.ip.is_known and interface.mask.is_known):
                # Router-on-a-stick: the physical port is L2 and the address
                # lives on the dot1Q subinterface serving the destination.
                parsed_ifaces = self.corpus.parsed_for(device.name)
                if parsed_ifaces is not None and result.destination_ip:
                    try:
                        dest = ipaddress.IPv4Address(result.destination_ip)
                    except ValueError:
                        dest = None
                    if dest is not None:
                        for candidate in parsed_ifaces.interfaces.values():
                            if not (candidate.ip and candidate.mask):
                                continue
                            try:
                                if dest in ipaddress.IPv4Network(
                                    f"{candidate.ip}/{candidate.mask}",
                                    strict=False,
                                ):
                                    addressed = device.interface(candidate.name) or interface
                                    break
                            except ValueError:
                                continue
            if addressed.ip.is_known and addressed.mask.is_known:
                hop.checks.append(
                    HopCheck(
                        "L3", "addressing", PASS,
                        f"{device.name}:{addressed.name} {addressed.ip.value}/"
                        f"{addressed.mask.value}",
                        (addressed.ip.source or "read-back",),
                    )
                )
            else:
                hop.checks.append(
                    HopCheck(
                        "L3", "addressing", UNKNOWN,
                        f"{device.name}:{interface.name} address was not observed",
                    )
                )
        destination_ip = result.destination_ip
        if destination_ip is None:
            hop.checks.append(
                HopCheck(
                    "L3", "route", UNKNOWN,
                    "destination address is unknown, so no route can be looked up",
                )
            )
            return
        route, route_evidence, route_status = self._lookup_route(device, destination_ip)
        if route_status == FAIL:
            hop.checks.append(
                HopCheck(
                    "L3", "route", FAIL,
                    f"no route to {destination_ip} on {device.name}",
                    (route_evidence,),
                    suspected=not device.routes.is_known,
                )
            )
            return
        if route_status == UNKNOWN:
            hop.checks.append(
                HopCheck(
                    "L3", "route", UNKNOWN,
                    f"route lookup on {device.name} could not be decided",
                    (route_evidence,),
                )
            )
            return
        hop.checks.append(
            HopCheck(
                "L3", "route", PASS,
                f"{device.name} routes {destination_ip} via {route.get('kind')} "
                f"{route.get('network')}/{route.get('prefix')}",
                (route_evidence,),
            )
        )
        # Next hop must be the device we actually walk to next.
        next_device = path[index + 1] if index + 1 < len(path) else None
        if next_device is None:
            return
        next_hop = route.get("next_hop")
        next_iface = route.get("interface")
        if next_hop:
            # The next hop may be several devices away on the physical path:
            # a modem and a cloud are transparent L2 hops, and the routed
            # next hop legitimately sits behind them. What must be true is
            # that the path actually reaches a device that owns that address.
            owner = next(
                (
                    name for name in path[index + 1:]
                    if self._device_owns_ip(name, next_hop)
                ),
                None,
            )
            if owner is not None:
                hop.checks.append(
                    HopCheck(
                        "L3", "next-hop", PASS,
                        f"next hop {next_hop} is {owner}",
                        (f"observed interface address {next_hop}",),
                    )
                )
            else:
                hop.checks.append(
                    HopCheck(
                        "L3", "next-hop", FAIL,
                        f"route to {destination_ip} points at {next_hop}, which "
                        f"is not an address on the rest of the path "
                        f"({' -> '.join(path[index + 1:])})",
                        (
                            f"configured next hop {next_hop}",
                            f"path tail {path[index + 1:]}",
                        ),
                        suspected=True,
                    )
                )
        elif next_iface:
            expected_port = hop.egress
            # A dot1Q subinterface leaves through its parent port.
            exit_parent = (
                next_iface.split(".", 1)[0] if "." in next_iface else next_iface
            )
            if expected_port and _normalise_interface(exit_parent) != _normalise_interface(expected_port):
                hop.checks.append(
                    HopCheck(
                        "L3", "next-hop", FAIL,
                        f"route to {destination_ip} exits {next_iface}, but the "
                        f"path leaves {device.name} through {expected_port}",
                        (
                            f"configured exit interface {next_iface}",
                            f"path egress {expected_port}",
                        ),
                        suspected=True,
                    )
                )
            else:
                hop.checks.append(
                    HopCheck(
                        "L3", "next-hop", PASS,
                        f"route exits {next_iface}",
                        ("configured exit interface",),
                    )
                )

    def _device_owns_ip(self, device_name: str, address: str) -> bool:
        device = self.inventory.device(device_name)
        if device is None:
            return False
        try:
            wanted = ipaddress.IPv4Address(address)
        except ValueError:
            return False
        for interface in device.interfaces.values():
            if not interface.ip.is_known:
                continue
            try:
                if ipaddress.IPv4Address(str(interface.ip.value)) == wanted:
                    return True
            except ValueError:
                continue
        return False

    def _lookup_route(        self, device: DeviceInventory, destination_ip: str
    ) -> tuple[dict[str, Any], str, str]:
        try:
            dest = ipaddress.IPv4Address(destination_ip)
        except ValueError:
            return {}, f"invalid destination {destination_ip!r}", UNKNOWN
        if device.routes.is_known:
            best: dict[str, Any] | None = None
            best_prefix = -1
            for row in device.routes.value or []:
                if not isinstance(row, dict):
                    continue
                try:
                    network = ipaddress.IPv4Network(
                        f"{row.get('network')}/{row.get('prefix')}", strict=False
                    )
                except (TypeError, ValueError):
                    continue
                if dest in network and network.prefixlen >= best_prefix:
                    best = row
                    best_prefix = network.prefixlen
            if best is None:
                return {}, "observed route table has no matching entry", FAIL
            return best, "show ip route", PASS
        # No observed table: configured routes are evidence of intent, not a
        # forwarding fact. A decision from them is marked suspected by callers.
        parsed = self.corpus.parsed_for(device.name)
        if parsed is None:
            return {}, "no route table and no configuration were observed", UNKNOWN
        candidates: list[tuple[int, dict[str, Any]]] = []
        for interface in parsed.interfaces.values():
            if not interface.ip or not interface.mask:
                continue
            try:
                network = ipaddress.IPv4Network(
                    f"{interface.ip}/{interface.mask}", strict=False
                )
            except ValueError:
                continue
            if dest in network:
                candidates.append(
                    (
                        network.prefixlen,
                        {
                            "kind": "connected",
                            "network": str(network.network_address),
                            "prefix": network.prefixlen,
                            "next_hop": None,
                            "interface": interface.name,
                        },
                    )
                )
        for route in parsed.static_routes:
            cidr = route.cidr
            if not cidr:
                continue
            network = ipaddress.IPv4Network(cidr)
            if dest in network:
                candidates.append(
                    (
                        network.prefixlen,
                        {
                            "kind": "static",
                            "network": str(network.network_address),
                            "prefix": network.prefixlen,
                            "next_hop": route.next_hop,
                            "interface": route.interface,
                        },
                    )
                )
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1], "running-config (not observed)", PASS
        # A dynamic protocol may carry the route, but presence of a protocol
        # is not presence of a prefix: report that honestly.
        protocol = next(
            (
                p.protocol
                for p in parsed.routing_protocols
                if any(
                    _network_covers(n.network, n.wildcard, dest)
                    for n in p.networks
                )
            ),
            None,
        )
        if protocol:
            return (
                {},
                f"{protocol} is configured but its neighbour/route table was "
                "not observed",
                UNKNOWN,
            )
        return {}, "no connected, static or dynamic route covers the destination", FAIL

    def _security_checks(
        self,
        device: DeviceInventory,
        hop: Hop,
        ingress: str | None,
        egress: str | None,
        source_ip: str | None,
        destination_ip: str | None,
        transport: str = "icmp",
    ) -> None:
        parsed = self.corpus.parsed_for(device.name)
        if parsed is None:
            return
        if not source_ip or not destination_ip:
            return
        for port_name, direction in ((ingress, "in"), (egress, "out")):
            if not port_name:
                continue
            interface = parsed.interface(port_name)
            if interface is None:
                continue
            acl_name = interface.acl_in if direction == "in" else interface.acl_out
            if not acl_name:
                continue
            verdict, detail = self._evaluate_acl(
                parsed, acl_name, source_ip, destination_ip, transport
            )
            if verdict is None:
                hop.checks.append(
                    HopCheck(
                        "security", "acl", UNKNOWN,
                        f"{device.name}:{port_name} inbound ACL {acl_name} could "
                        f"not be evaluated",
                        (detail,),
                    )
                )
            elif verdict == "deny":
                hop.checks.append(
                    HopCheck(
                        "security", "acl", FAIL,
                        f"ACL {acl_name} denies {source_ip} -> {destination_ip} "
                        f"on {device.name}:{port_name} {direction}",
                        (detail,),
                        suspected=False,
                    )
                )
            else:
                hop.checks.append(
                    HopCheck(
                        "security", "acl", PASS,
                        f"ACL {acl_name} permits {source_ip} -> {destination_ip}",
                        (detail,),
                    )
                )
        # ASA interface policy: an addressed interface with no nameif cannot
        # participate in policy, and is a real configuration defect.
        if device.category == "firewall":
            interface = parsed.interface(ingress or "")
            if interface is not None and interface.ip and interface.ip != "dhcp":
                if not interface.nameif:
                    hop.checks.append(
                        HopCheck(
                            "security", "nameif", FAIL,
                            f"ASA interface {interface.name} has an address but no nameif",
                            ("running-config has no nameif on the interface",),
                            suspected=False,
                        )
                    )
                if interface.security_level is None:
                    hop.checks.append(
                        HopCheck(
                            "security", "security-level", FAIL,
                            f"ASA interface {interface.name} has no security-level",
                            ("running-config has no security-level",),
                            suspected=False,
                        )
                    )
            # Global access-group bindings.
            for binding in parsed.globals.acl_bindings:
                if str(binding.get("direction") or "") != "in":
                    continue
                if interface is None:
                    continue
                bound_iface = parsed.interface(str(binding.get("interface") or ""))
                if bound_iface is None or bound_iface.name != interface.name:
                    continue
                name = str(binding.get("name") or "")
                verdict, detail = self._evaluate_acl(
                    parsed, name, source_ip, destination_ip, transport
                )
                if verdict == "deny":
                    hop.checks.append(
                        HopCheck(
                            "security", "acl", FAIL,
                            f"ASA access-group {name} denies {source_ip} -> "
                            f"{destination_ip} on {interface.name}",
                            (detail,),
                            suspected=False,
                        )
                    )
                elif verdict == "permit":
                    hop.checks.append(
                        HopCheck(
                            "security", "acl", PASS,
                            f"ASA access-group {name} permits the flow",
                            (detail,),
                        )
                    )

    @staticmethod
    def _evaluate_acl(
        parsed: ParsedConfig,
        acl_name: str,
        source_ip: str,
        destination_ip: str,
        protocol: str,
    ) -> tuple[str | None, str]:
        acl = parsed.acl(acl_name)
        if acl is None:
            return None, f"ACL {acl_name} is bound but not defined in this config"
        for rule in acl.rules:
            matched = acl_rule_matches(rule, source_ip, destination_ip, protocol)
            if not matched:
                if rule.source_kind in ("object", "object-group") or rule.destination_kind in ("object", "object-group"):
                    return None, (
                        f"rule {rule.raw!r} uses an ASA object that was not resolved"
                    )
                continue
            if rule.action == "deny":
                return "deny", f"first matching rule: {rule.raw!r} (line {rule.line_no})"
            return "permit", f"first matching rule: {rule.raw!r} (line {rule.line_no})"
        return "deny", "implicit deny: no rule matched"

    def _destination_checks(
        self,
        device: DeviceInventory,
        hop: Hop,
        result: TraceResult,
        source_ip: str | None,
    ) -> None:
        if device.host is not None:
            if device.host.ip.is_known:
                hop.checks.append(
                    HopCheck(
                        "L3", "addressing", PASS,
                        f"destination {device.name} has {device.host.ip.value}",
                        ("ipconfig /all",),
                    )
                )
            else:
                hop.checks.append(
                    HopCheck(
                        "L3", "addressing", FAIL,
                        f"destination {device.name} has no observed address",
                    )
                )
            if result.destination_ip and result.source_ip and result.destination_ip != result.source_ip:
                same_subnet = self._on_link(
                    str(device.host.ip.value) if device.host.ip.is_known else None,
                    str(device.host.mask.value) if device.host.mask.is_known else None,
                    result.source_ip,
                )
                gateway = (
                    device.host.gateway.value
                    if device.host.gateway.is_known
                    else None
                )
                if same_subnet:
                    hop.checks.append(
                        HopCheck(
                            "L3", "return-route", INFO,
                            f"{device.name} replies on-link to {result.source_ip}",
                        )
                    )
                elif not gateway:
                    hop.checks.append(
                        HopCheck(
                            "L3", "return-route", UNKNOWN,
                            f"{device.name} has no default gateway; its return "
                            "path is not configured",
                        )
                    )
                else:
                    hop.checks.append(
                        HopCheck(
                            "L3", "return-route", PASS,
                            f"{device.name} returns through {gateway}",
                            ("ipconfig /all default gateway",),
                        )
                    )
            return
        # A router destination: it must know how to answer the source.
        if source_ip:
            route, evidence, status = self._lookup_route(device, source_ip)
            if status == FAIL:
                hop.checks.append(
                    HopCheck(
                        "L3", "return-route", FAIL,
                        f"{device.name} has no route back to {source_ip}",
                        (evidence,),
                        suspected=not device.routes.is_known,
                    )
                )
            elif status == UNKNOWN:
                hop.checks.append(
                    HopCheck(
                        "L3", "return-route", UNKNOWN,
                        f"return route to {source_ip} on {device.name} was not observed",
                        (evidence,),
                    )
                )
            else:
                hop.checks.append(
                    HopCheck(
                        "L3", "return-route", PASS,
                        f"{device.name} returns to {source_ip} via {route.get('kind')}",
                        (evidence,),
                    )
                )

    # -- service objectives -------------------------------------------------

    def _dhcp_check(self, device: DeviceInventory, result: TraceResult) -> None:
        hop = Hop(0, device.name, None, None, None)
        if device.host is None:
            hop.checks.append(
                HopCheck("services", "dhcp-lease", UNKNOWN,
                         f"{device.name} is not an imported host")
            )
        elif device.host.ip.is_known and str(device.host.ip.value) != "0.0.0.0":
            if device.host.dhcp.is_known and not device.host.dhcp.value:
                hop.checks.append(
                    HopCheck("services", "dhcp-lease", INFO,
                             f"{device.name} is statically addressed; no DHCP lease expected")
                )
            else:
                hop.checks.append(
                    HopCheck("services", "dhcp-lease", PASS,
                             f"{device.name} holds {device.host.ip.value}",
                             ("ipconfig /all",))
                )
        else:
            hop.checks.append(
                HopCheck("services", "dhcp-lease", FAIL,
                         f"{device.name} has no lease",
                         ("ipconfig /all reported no IPv4 address",))
            )
        result.hops.append(hop)
        result.status = hop.status if hop.status != PASS else "config_consistent"

    def _dns_check(self, device: DeviceInventory, result: TraceResult) -> None:
        hop = Hop(0, device.name, None, None, None)
        if device.host is None:
            hop.checks.append(
                HopCheck("services", "dns-config", UNKNOWN,
                         f"{device.name} is not an imported host")
            )
        elif not device.host.dns.is_known or not device.host.dns.value:
            hop.checks.append(
                HopCheck("services", "dns-config", FAIL,
                         f"{device.name} has no DNS server configured",
                         ("ipconfig /all",))
            )
        else:
            hop.checks.append(
                HopCheck("services", "dns-config", PASS,
                         f"{device.name} uses DNS {device.host.dns.value}; "
                         "name resolution itself was not probed",
                         ("ipconfig /all",))
            )
        result.hops.append(hop)
        result.status = hop.status if hop.status != PASS else "config_consistent"


# -- helpers ------------------------------------------------------------------


def _normalise_interface(name: str | None) -> str:
    text = str(name or "").strip()
    replacements = {
        "Gi": "GigabitEthernet", "Fa": "FastEthernet", "Et": "Ethernet",
        "Te": "TenGigabitEthernet", "Po": "Port-channel",
    }
    for short, full in replacements.items():
        if text.startswith(short) and not text.startswith(full):
            return full + text[len(short):]
    return text


def _network_covers(
    network: str | None, wildcard: str | None, address: ipaddress.IPv4Address
) -> bool:
    if not network:
        return False
    try:
        if wildcard:
            mask_int = int(ipaddress.IPv4Address(wildcard))
            prefix = ipaddress.IPv4Address(0xFFFFFFFF ^ mask_int)
            return address in ipaddress.IPv4Network(f"{network}/{prefix}", strict=False)
        return address in ipaddress.IPv4Network(network, strict=False)
    except ValueError:
        return False
