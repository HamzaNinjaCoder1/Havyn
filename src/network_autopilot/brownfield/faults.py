"""Layer-ordered fault detection and the standardized diagnosis (S4.7/S4.8).

Every rule here answers the same question: *what evidence shows that this
piece of the network does not do what the confirmed objective needs?* A rule
that cannot answer from observed or configured evidence emits an UNKNOWN
diagnosis with the reason; it never guesses, and it never proposes a mutation
it cannot justify.

Rules are organized by the layer they decide:

* L1 — missing/wrong cables, administrative shutdown, physical down
* L2 — access VLAN, trunk allowance, native VLAN, undefined VLAN, STP blocking
* L3 — address, mask, gateway, route, next hop
* security — ACL block, ASA nameif/level/access-group
* services — DHCP subnet/pool, DNS

A topology can fail for several independent reasons at once; `detect_all`
returns every one it can prove, and each carries its own evidence and fix.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..core.network_spec import (
    AsaInterfaceSpec,
    DeviceSpec,
    DhcpPoolSpec,
    LinkSpec,
    NetworkSpec,
    StaticRouteSpec,
)
from ..core.ids import new_id
from ..ipam.planner import AddressPlan, plan_addressing
from .config_reader import ConfigCorpus
from .models import (
    CAUSE_CLASSES,
    BrownfieldInventory,
    Diagnostic,
    DeviceInventory,
    FixAction,
    InterfaceInventory,
    Objective,
)
from .objectives import ObjectiveRegistry
from .parser import ParsedConfig
from .tracer import FAIL, UNKNOWN, Hop, HopCheck, PathTracer, TraceResult

# -- expected network ---------------------------------------------------------


@dataclass
class ExpectedNetwork:
    """The target the network is judged against (as-is or desired spec).

    A fault is a difference from an expectation; without one, the only
    defensible detections are self-contradictions (an address used twice, an
    ACL bound but undefined) and trace failures against a confirmed objective.
    """

    spec: NetworkSpec | None = None
    plan: AddressPlan | None = None
    addresses: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    device_addresses: dict[str, str] = field(default_factory=dict)
    gateways: dict[str, str] = field(default_factory=dict)          # subnet id -> ip
    access_vlans: dict[tuple[str, str], int] = field(default_factory=dict)
    trunk_allowed: dict[tuple[str, str], list[int] | None] = field(default_factory=dict)
    native_vlans: dict[tuple[str, str], int] = field(default_factory=dict)
    links: list[LinkSpec] = field(default_factory=list)
    routes: dict[str, list[StaticRouteSpec]] = field(default_factory=dict)
    asa_zones: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)
    dhcp_pools: list[DhcpPoolSpec] = field(default_factory=list)
    dns: dict[str, str] = field(default_factory=dict)
    vlan_names: dict[int, str] = field(default_factory=dict)
    host_assignments: dict[str, tuple[str | None, str | None, str | None]] = field(
        default_factory=dict
    )  # device -> (ip, mask, gateway)

    @classmethod
    def from_spec(
        cls, spec: NetworkSpec, plan: AddressPlan | None = None
    ) -> "ExpectedNetwork":
        expected = cls(spec=spec, plan=plan or plan_addressing(spec))
        assert expected.plan is not None
        for allocation in expected.plan.allocations:
            expected.gateways[allocation.subnet_id] = allocation.gateway
        for assignment in expected.plan.assignments:
            allocation = expected.plan.allocation_by_subnet(assignment.subnet_id)
            if assignment.ip:
                expected.addresses[(assignment.device, assignment.interface_hint or "")] = (
                    assignment.ip,
                    allocation.netmask if allocation else "",
                )
                expected.device_addresses.setdefault(assignment.device, assignment.ip)
            subnet = spec.subnet_by_id(assignment.subnet_id)
            if subnet is not None and subnet.vlan_id is not None:
                if assignment.device and assignment.interface_hint:
                    expected.access_vlans.setdefault(
                        (assignment.device, assignment.interface_hint), subnet.vlan_id
                    )
            if spec.device_by_id(assignment.device) is not None:
                device = spec.device_by_id(assignment.device)
                if device is not None and str(device.role.value) in (
                    "pc", "laptop", "server", "printer", "tablet", "smartphone",
                    "ip_phone", "other",
                ):
                    expected.host_assignments[assignment.device] = (
                        assignment.ip,
                        allocation.netmask if allocation else None,
                        assignment.gateway,
                    )
        for link in spec.links:
            expected.links.append(link)
            if not link.trunk:
                vlan_a = link.a.vlan or expected.access_vlans.get(
                    (link.a.device, link.a.interface_hint or "")
                )
                vlan_b = link.b.vlan or expected.access_vlans.get(
                    (link.b.device, link.b.interface_hint or "")
                )
                # An access link has one VLAN: a host's assignment defines it,
                # and the switch port at the other end inherits that fact.
                vlan_a = vlan_a or vlan_b
                vlan_b = vlan_b or vlan_a
                if vlan_a is not None and link.a.interface_hint:
                    expected.access_vlans[(link.a.device, link.a.interface_hint)] = vlan_a
                if vlan_b is not None and link.b.interface_hint:
                    expected.access_vlans[(link.b.device, link.b.interface_hint)] = vlan_b
            for endpoint in (link.a, link.b):
                if endpoint.interface_hint:
                    expected.trunk_allowed[(endpoint.device, endpoint.interface_hint)] = (
                        sorted(link.allowed_vlans) if link.allowed_vlans is not None else None
                    )
                    if link.native_vlan is not None:
                        expected.native_vlans[
                            (endpoint.device, endpoint.interface_hint)
                        ] = link.native_vlan
        for route in spec.routing.static_routes:
            expected.routes.setdefault(route.device, []).append(route)
        for device in spec.devices:
            for zone in device.asa_interfaces:
                expected.asa_zones[(device.id, zone.interface)] = (
                    zone.nameif,
                    zone.security_level,
                )
        expected.dhcp_pools = list(spec.routing.dhcp_pools)
        for subnet in spec.subnets:
            if subnet.dhcp and subnet.id in expected.gateways:
                expected.dhcp_pools.append(
                    DhcpPoolSpec(
                        device=self._gateway_device(spec, expected, subnet.id) or "",
                        subnet=subnet.id,
                        default_router=expected.gateways[subnet.id],
                    )
                )
        for dns in spec.host_dns:
            expected.dns[dns.device] = dns.dns_server
        for vlan in spec.vlans:
            expected.vlan_names[vlan.vlan_id] = vlan.name
        return expected

    @staticmethod
    def _gateway_device(
        spec: NetworkSpec, expected: "ExpectedNetwork", subnet_id: str
    ) -> str | None:
        if expected.plan is None:
            return None
        gateway = expected.gateways.get(subnet_id)
        for assignment in expected.plan.assignments:
            if assignment.ip == gateway:
                return assignment.device
        return None


# -- detector -----------------------------------------------------------------


class FaultDetector:
    """Prove the faults that the evidence supports, one diagnosis at a time."""

    def __init__(
        self,
        inventory: BrownfieldInventory,
        corpus: ConfigCorpus,
        objectives: ObjectiveRegistry | None = None,
        *,
        expected: ExpectedNetwork | None = None,
    ):
        self.inventory = inventory
        self.corpus = corpus
        self.objectives = objectives or ObjectiveRegistry()
        self.expected = expected
        self.tracer = PathTracer(
            inventory,
            corpus,
            objectives=self.objectives,
            expected_links=expected.links if expected is not None else None,
            expected_access_vlans=(
                expected.access_vlans if expected is not None else None
            ),
        )

    # -- public -------------------------------------------------------------

    def diagnose(self, objective: Objective) -> list[Diagnostic]:
        """Diagnose one confirmed objective, plus related config faults."""
        if not objective.actionable:
            return [
                self._unknown(
                    root_cause=(
                        f"objective {objective.id} is inferred and has not been "
                        "confirmed; no repair may be based on it"
                    ),
                    layer="L3",
                    evidence=("objective source: inferred",),
                    objective=objective,
                    uncertainty=(
                        "ask the user to confirm this objective before acting"
                    ),
                )
            ]
        if objective.kind not in ("reachability", "isolation", "addressing", "dns"):
            return self.diagnose_structural(objective)
        if (
            objective.kind in ("reachability", "isolation")
            and not objective.params.get("source")
            and objective.params.get("vlan") is None
        ):
            return self.diagnose_structural(objective)
        if objective.kind == "addressing" and not objective.params.get("dhcp"):
            return self.diagnose_structural(objective)
        trace = self.tracer.trace_objective(objective)
        diagnostics = self._from_trace(trace, objective)
        diagnostics.extend(
            self._config_faults(
                devices=set(trace.path) | {trace.source, trace.destination},
                objective=objective,
            )
        )
        return self._dedupe(diagnostics)

    def diagnose_structural(self, objective: Objective) -> list[Diagnostic]:
        """A structural/ service objective is judged by config rules, not a flow."""
        return self._dedupe(
            self._config_faults(devices=None, objective=objective)
        )

    def detect_all(self) -> list[Diagnostic]:
        """Every config-level fault that can be proven without a flow."""
        return self._dedupe(self._config_faults(devices=None, objective=None))

    def diagnose_all_confirmed(self) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        for objective in self.objectives.confirmed():
            out.extend(self.diagnose(objective))
        return self._dedupe(out)

    # -- trace -> diagnosis -------------------------------------------------

    def _from_trace(
        self, trace: TraceResult, objective: Objective
    ) -> list[Diagnostic]:
        if trace.status == "config_consistent":
            return []
        if trace.status == UNKNOWN and not trace.first_failing_check:
            return [
                self._unknown(
                    root_cause=(
                        f"connectivity for {trace.source} -> {trace.destination} "
                        "could not be decided from the available evidence"
                    ),
                    layer="L3",
                    evidence=(*trace.evidence, *trace.unknowns),
                    objective=objective,
                    uncertainty=(
                        "UNKNOWN - ADDITIONAL VERIFICATION REQUIRED: "
                        + "; ".join(trace.unknowns[:3])
                    ),
                )
            ]
        check = trace.first_failing_check
        if check is None:
            return [
                self._unknown(
                    root_cause=(
                        f"{trace.source} -> {trace.destination} is not working, "
                        "but no failing hop was observable"
                    ),
                    layer="L3",
                    evidence=tuple(trace.evidence),
                    objective=objective,
                    uncertainty="UNKNOWN - ADDITIONAL VERIFICATION REQUIRED",
                )
            ]
        hop = (
            trace.hops[trace.first_failing_hop]
            if trace.first_failing_hop is not None
            and trace.first_failing_hop < len(trace.hops)
            else None
        )
        return [self._diagnose_check(trace, hop, check, objective)]

    def _diagnose_check(
        self,
        trace: TraceResult,
        hop: Hop | None,
        check: HopCheck,
        objective: Objective,
    ) -> Diagnostic:
        device = hop.device if hop is not None else trace.source
        port = None
        if hop is not None:
            port = hop.ingress if check.name in (
                "port-state", "link", "access-vlan", "trunk-allowance", "stp"
            ) else (hop.ingress or hop.egress)
        expected_vlan = trace.vlan
        base_evidence = (
            f"objective {objective.id}: {objective.statement}",
            f"first failing hop {trace.first_failing_hop}: {device}"
            + (f" port {port}" if port else ""),
            check.detail,
            *check.evidence,
        )

        if check.name == "link":
            return self._missing_cable(device, port, base_evidence, objective, trace)
        if check.name == "port-state":
            inventory = self.inventory.device(device)
            interface = inventory.interface(port) if inventory and port else None
            admin = (
                str(interface.admin_state.value)
                if interface is not None and interface.admin_state.is_known
                else None
            )
            if admin == "shutdown":
                return self._diagnostic(
                    cause_class="port-shutdown",
                    layer="L1",
                    root_cause=f"{device}:{port} is administratively shut down",
                    device=device, port=port, vlan=expected_vlan,
                    evidence=base_evidence,
                    confidence="high",
                    fix=FixAction(
                        device=device,
                        commands=(f"interface {port}", "no shutdown", "exit"),
                        reason="interface is administratively down",
                        rollback_commands=(f"interface {port}", "shutdown", "exit"),
                        expected_state={"state": "up"},
                    ),
                    impact=f"no traffic can cross {device}:{port}",
                    objective=objective,
                    trace=trace,
                    suspected=check.suspected,
                )
            return self._diagnostic(
                cause_class="port-down-physical",
                layer="L1",
                root_cause=(
                    f"{device}:{port} is down although it is not administratively "
                    "shut down"
                ),
                device=device, port=port, vlan=expected_vlan,
                evidence=base_evidence,
                confidence="high",
                fix=None,
                impact=f"the physical link on {device}:{port} carries no traffic",
                objective=objective,
                trace=trace,
                uncertainty=(
                    "physical faults have no remote repair; reseat/replace the "
                    "cable or the port"
                ),
                suspected=False,
            )
        if check.name == "access-vlan":
            expected = self._expected_access_vlan(device, port, objective)
            current = self._observed_access_vlan(device, port)
            fix = None
            if expected is not None and port is not None:
                commands = [f"interface {port}", f"switchport access vlan {expected}", "exit"]
                rollback = (
                    [f"interface {port}", f"switchport access vlan {current}", "exit"]
                    if current is not None else [f"interface {port}", "no switchport access vlan", "exit"]
                )
                fix = FixAction(
                    device=device, commands=tuple(commands),
                    reason=f"port is in VLAN {current}, objective needs VLAN {expected}",
                    rollback_commands=tuple(rollback),
                    expected_state={"access_vlan": expected},
                )
            return self._diagnostic(
                cause_class="wrong-access-vlan",
                layer="L2",
                root_cause=(
                    f"{device}:{port} is in VLAN {current if current is not None else 'unknown'}; "
                    f"the objective needs VLAN {expected if expected is not None else expected_vlan}"
                ),
                device=device, port=port, vlan=expected or expected_vlan,
                evidence=base_evidence,
                confidence="high" if expected is not None else "medium",
                fix=fix,
                impact="the port forwards the flow into the wrong broadcast domain",
                objective=objective,
                trace=trace,
                uncertainty=None if expected is not None else (
                    "the expected VLAN is not stated by a confirmed objective or spec"
                ),
                suspected=False,
            )
        if check.name == "trunk-allowance":
            expected = self._expected_trunk_vlan(device, port, objective, trace)
            vlan = expected if expected is not None else expected_vlan
            current = self._observed_allowed(device, port)
            fix = None
            if vlan is not None:
                fix = FixAction(
                    device=device,
                    commands=(
                        f"interface {port}",
                        f"switchport trunk allowed vlan add {vlan}",
                        "exit",
                    ),
                    reason=f"VLAN {vlan} is not allowed on the trunk",
                    rollback_commands=(
                        f"interface {port}",
                        f"switchport trunk allowed vlan remove {vlan}",
                        "exit",
                    ),
                    expected_state={"allowed_vlans_add": vlan},
                )
            return self._diagnostic(
                cause_class="trunk-vlan-not-allowed",
                layer="L2",
                root_cause=(
                    f"{device}:{port} trunk does not allow VLAN {vlan}"
                ),
                device=device, port=port, vlan=vlan,
                evidence=base_evidence,
                confidence="high" if current is not None else "medium",
                fix=fix,
                impact="the VLAN cannot cross the trunk, so the two sides are isolated",
                objective=objective,
                trace=trace,
                suspected=check.suspected,
            )
        if check.name == "native-vlan":
            expected = self._expected_native_vlan(device, port)
            fix = None
            if expected is not None:
                fix = FixAction(
                    device=device,
                    commands=(
                        f"interface {port}",
                        f"switchport trunk native vlan {expected}",
                        "exit",
                    ),
                    reason="native VLANs disagree across the trunk",
                    rollback_commands=(
                        f"interface {port}",
                        "no switchport trunk native vlan",
                        "exit",
                    ),
                    expected_state={"native_vlan": expected},
                )
            return self._diagnostic(
                cause_class="native-vlan-mismatch",
                layer="L2",
                root_cause=(
                    f"native VLAN mismatch on {device}:{port}"
                ),
                device=device, port=port, vlan=expected,
                evidence=base_evidence,
                confidence="high",
                fix=fix,
                impact="untagged frames are classified into different VLANs at each end",
                objective=objective,
                trace=trace,
                suspected=False,
            )
        if check.name == "vlan-defined":
            vlan = expected_vlan
            name = (
                self.expected.vlan_names.get(vlan)
                if self.expected is not None and vlan is not None
                else None
            )
            commands: list[str] = []
            rollback: list[str] = []
            if vlan is not None:
                commands = [f"vlan {vlan}"]
                if name:
                    commands.append(f"name {name}")
                commands.append("exit")
                rollback = [f"no vlan {vlan}"]
            return self._diagnostic(
                cause_class="undefined-vlan",
                layer="L2",
                root_cause=f"VLAN {vlan} is referenced on {device} but not defined",
                device=device, port=port, vlan=vlan,
                evidence=base_evidence,
                confidence="high",
                fix=(
                    FixAction(
                        device=device, commands=tuple(commands),
                        reason="create the referenced VLAN",
                        rollback_commands=tuple(rollback),
                        expected_state={"vlan_defined": vlan},
                    )
                    if commands else None
                ),
                impact="the switch cannot forward a VLAN it does not know",
                objective=objective,
                trace=trace,
                suspected=False,
            )
        if check.name == "stp":
            return self._diagnostic(
                cause_class="stp-blocking",
                layer="L2",
                root_cause=(
                    f"{device}:{port} is blocked by spanning tree on the path "
                    "the objective needs"
                ),
                device=device, port=port, vlan=expected_vlan,
                evidence=base_evidence,
                confidence="high",
                fix=None,
                impact="the port does not forward while STP blocks it",
                objective=objective,
                trace=trace,
                uncertainty=(
                    "blocking is often correct loop prevention; changing the STP "
                    "topology needs a stated design, so no automatic fix is made"
                ),
                suspected=False,
            )
        if check.name == "gateway":
            return self._wrong_gateway(device, port, base_evidence, objective, trace)
        if check.name == "addressing":
            return self._wrong_address(device, port, base_evidence, objective, trace)
        if check.name == "route":
            return self._missing_route(device, base_evidence, objective, trace)
        if check.name == "next-hop":
            return self._wrong_next_hop(device, base_evidence, objective, trace)
        if check.name == "acl":
            return self._acl_block(device, port, base_evidence, objective, trace)
        if check.name == "nameif":
            return self._asa_nameif(device, port, base_evidence, objective, trace)
        if check.name == "security-level":
            return self._asa_level(device, port, base_evidence, objective, trace)
        if check.name == "dhcp-lease":
            return self._dhcp_lease(device, base_evidence, objective, trace)
        if check.name == "dns-config":
            return self._dns_wrong(device, base_evidence, objective, trace)
        return self._unknown(
            root_cause=check.detail,
            layer=check.layer,
            evidence=base_evidence,
            objective=objective,
            uncertainty="UNKNOWN - ADDITIONAL VERIFICATION REQUIRED",
            device=device,
            port=port,
        )

    def _route_command(
        self, device: str, network: ipaddress.IPv4Network, next_hop: str
    ) -> str:
        """Render a static route in the platform's own syntax."""
        parsed = self.corpus.parsed_for(device)
        if parsed is not None and parsed.platform.value == "asa":
            nameif = next(
                (
                    i.nameif for i in parsed.interfaces.values()
                    if i.nameif == "outside"
                ),
                "outside",
            )
            return (
                f"route {nameif} {network.network_address} "
                f"{network.netmask} {next_hop}"
            )
        return (
            f"ip route {network.network_address} {network.netmask} {next_hop}"
        )

    # -- L3 -----------------------------------------------------------------

    def _wrong_gateway(
        self, device: str, port: str | None, evidence: tuple[str, ...],
        objective: Objective, trace: TraceResult,
    ) -> Diagnostic:
        expected = None
        observed = None
        inventory = self.inventory.device(device)
        if inventory is not None and inventory.host is not None:
            observed = (
                str(inventory.host.gateway.value)
                if inventory.host.gateway.is_known else None
            )
        if self.expected is not None:
            assignment = self.expected.host_assignments.get(device)
            if assignment:
                expected = assignment[2]
        if expected is None and inventory is not None:
            ip = (
                str(inventory.host.ip.value)
                if inventory.host is not None and inventory.host.ip.is_known
                else None
            )
            mask = (
                str(inventory.host.mask.value)
                if inventory.host is not None and inventory.host.mask.is_known
                else None
            )
            if ip and mask:
                expected = self._gateway_on_subnet(ip, mask, exclude=device)
        fix = None
        if expected is not None:
            fix = FixAction(
                device=device, operation="configure_host",
                host_config={
                    "dhcp": False,
                    "ip": (
                        str(inventory.host.ip.value)
                        if inventory is not None and inventory.host is not None
                        and inventory.host.ip.is_known else None
                    ),
                    "mask": (
                        str(inventory.host.mask.value)
                        if inventory is not None and inventory.host is not None
                        and inventory.host.mask.is_known else None
                    ),
                    "gateway": expected,
                },
                reason=f"gateway is {observed}, the subnet router is {expected}",
                expected_state={"gateway": expected},
            )
        return self._diagnostic(
            cause_class="wrong-gateway",
            layer="L3",
            root_cause=(
                f"{device} uses default gateway {observed}, but the router "
                f"address on its subnet is {expected}"
            ),
            device=device, port=port,
            subnet=self._subnet_for_device(device),
            evidence=evidence,
            confidence="high" if expected is not None else "medium",
            fix=fix,
            impact="the host sends off-subnet traffic to an address that cannot route it",
            objective=objective,
            trace=trace,
            uncertainty=None if expected is not None else (
                "no router address was found on the host's subnet"
            ),
            suspected=False,
        )

    def _wrong_address(
        self, device: str, port: str | None, evidence: tuple[str, ...],
        objective: Objective, trace: TraceResult,
    ) -> Diagnostic:
        expected_ip = expected_mask = None
        observed_ip = observed_mask = None
        inventory = self.inventory.device(device)
        interface = inventory.interface(port) if inventory and port else None
        if interface is not None:
            observed_ip = str(interface.ip.value) if interface.ip.is_known else None
            observed_mask = str(interface.mask.value) if interface.mask.is_known else None
        if self.expected is not None and port:
            expected_ip, expected_mask = self.expected.addresses.get(
                (device, port), (None, None)
            )
        if expected_ip is None and inventory is not None and inventory.host is not None:
            expected_assignment = (
                self.expected.host_assignments.get(device)
                if self.expected is not None else None
            )
            if expected_assignment:
                expected_ip, expected_mask = (
                    expected_assignment[0], expected_assignment[1]
                )
        fix = None
        if expected_ip:
            if inventory is not None and inventory.is_host:
                fix = FixAction(
                    device=device, operation="configure_host",
                    host_config={
                        "dhcp": False,
                        "ip": expected_ip,
                        "mask": expected_mask,
                        "gateway": (
                            str(inventory.host.gateway.value)
                            if inventory.host is not None and inventory.host.gateway.is_known
                            else None
                        ),
                        "dns": (
                            list(inventory.host.dns.value)
                            if inventory.host is not None and inventory.host.dns.is_known
                            else None
                        ),
                    },
                    reason="restore the expected address",
                    expected_state={"ip": expected_ip, "mask": expected_mask},
                )
            elif port:
                fix = FixAction(
                    device=device,
                    commands=(
                        f"interface {port}",
                        f"ip address {expected_ip} {expected_mask or ''}".strip(),
                        "no shutdown",
                        "exit",
                    ),
                    reason="restore the expected interface address",
                    rollback_commands=(
                        f"interface {port}",
                        f"ip address {observed_ip or ''} {observed_mask or ''}".strip(),
                        "exit",
                    ),
                    expected_state={"ip": expected_ip, "mask": expected_mask},
                )
        return self._diagnostic(
            cause_class="wrong-ip",
            layer="L3",
            root_cause=(
                f"{device}{':' + port if port else ''} has "
                f"{observed_ip or 'no address'}/{observed_mask or ''}; expected "
                f"{expected_ip or 'unknown'}/{expected_mask or ''}"
            ),
            device=device, port=port,
            subnet=self._subnet_for_device(device),
            evidence=evidence,
            confidence="high" if expected_ip else "medium",
            fix=fix,
            impact="the endpoint is addressed outside its intended subnet",
            objective=objective,
            trace=trace,
            uncertainty=None if expected_ip else (
                "the expected address is not stated by a confirmed spec"
            ),
            suspected=False,
        )

    def _missing_route(
        self, device: str, evidence: tuple[str, ...],
        objective: Objective, trace: TraceResult,
    ) -> Diagnostic:
        destination = trace.destination_ip
        expected_routes = (
            self.expected.routes.get(device, [])
            if self.expected is not None else []
        )
        route = None
        if destination:
            try:
                dest = ipaddress.IPv4Address(destination)
            except ValueError:
                dest = None
            if dest is not None:
                for candidate in expected_routes:
                    try:
                        if dest in ipaddress.ip_network(candidate.destination, strict=False):
                            route = candidate
                            break
                    except ValueError:
                        continue
        fix = None
        if route is not None and (route.next_hop or route.exit_interface):
            try:
                net = ipaddress.ip_network(route.destination, strict=False)
            except ValueError:
                net = None
            if net is not None:
                command = self._route_command(
                    device, net, route.next_hop or route.exit_interface or ""
                )
                fix = FixAction(
                    device=device,
                    commands=(command,),
                    reason=f"the route to {route.destination} is missing",
                    rollback_commands=(f"no {command}",),
                    expected_state={"route": route.destination},
                )
        return self._diagnostic(
            cause_class="missing-route",
            layer="L3",
            root_cause=(
                f"{device} has no route to {destination}"
                + (
                    f"; the confirmed design requires {route.destination} -> "
                    f"{route.next_hop or route.exit_interface}"
                    if route is not None else ""
                )
            ),
            device=device,
            subnet=self._subnet_for_device(device),
            evidence=evidence,
            confidence="high" if route is not None else "medium",
            fix=fix,
            impact="traffic to the destination is dropped at this router",
            objective=objective,
            trace=trace,
            uncertainty=None if route is not None else (
                "the required next hop is not stated by the confirmed spec"
            ),
            suspected=False,
        )

    def _wrong_next_hop(
        self, device: str, evidence: tuple[str, ...],
        objective: Objective, trace: TraceResult,
    ) -> Diagnostic:
        expected_routes = (
            self.expected.routes.get(device, [])
            if self.expected is not None else []
        )
        destination = trace.destination_ip
        expected = None
        observed_route = None
        if destination:
            for route in expected_routes:
                try:
                    if ipaddress.IPv4Address(destination) in ipaddress.ip_network(
                        route.destination, strict=False
                    ):
                        expected = route
                        break
                except ValueError:
                    continue
        parsed = self.corpus.parsed_for(device)
        if parsed is not None and destination:
            for route in parsed.static_routes:
                if route.cidr and ipaddress.IPv4Address(destination) in ipaddress.ip_network(
                    route.cidr, strict=False
                ):
                    observed_route = route
                    break
        fix = None
        if expected is not None and observed_route is not None:
            try:
                net = ipaddress.ip_network(expected.destination, strict=False)
                old = self._route_command(
                    device,
                    ipaddress.ip_network(observed_route.cidr or "", strict=False),
                    observed_route.next_hop or observed_route.interface or "",
                )
                new = self._route_command(
                    device, net, expected.next_hop or expected.exit_interface or ""
                )
                fix = FixAction(
                    device=device,
                    commands=(f"no {old}", new),
                    reason="the route points at the wrong next hop",
                    rollback_commands=(f"no {new}", old),
                    expected_state={"next_hop": expected.next_hop},
                )
            except ValueError:
                fix = None
        return self._diagnostic(
            cause_class="wrong-next-hop",
            layer="L3",
            root_cause=(
                f"{device} forwards {destination} to the wrong next hop"
            ),
            device=device,
            subnet=self._subnet_for_device(device),
            evidence=evidence,
            confidence="medium",
            fix=fix,
            impact="the packet is handed to a device that cannot deliver it",
            objective=objective,
            trace=trace,
            uncertainty=None if fix is not None else (
                "the correct next hop is not stated by the confirmed spec"
            ),
            suspected=True,
        )

    # -- security -----------------------------------------------------------

    def _acl_block(
        self, device: str, port: str | None, evidence: tuple[str, ...],
        objective: Objective, trace: TraceResult,
    ) -> Diagnostic:
        parsed = self.corpus.parsed_for(device)
        acl_name = None
        rule = None
        if parsed is not None and port:
            interface = parsed.interface(port)
            if interface is not None:
                acl_name = interface.acl_in or interface.acl_out
        if parsed is not None and acl_name:
            acl = parsed.acl(acl_name)
            if acl is not None:
                rule = next(
                    (r for r in acl.rules if r.action == "deny"),
                    None,
                )
        fix = None
        if parsed is not None and acl_name and rule is not None:
            # Minimal change: remove the specific deny, then re-add it on
            # rollback. An ACL left with no permit and still bound would
            # still hit the implicit deny, so in that case the binding is
            # removed with it.
            raw = rule.raw.strip()
            acl = parsed.acl(acl_name)
            has_permit = bool(
                acl and any(r.action == "permit" for r in acl.rules)
            )
            if parsed.platform.value == "asa":
                commands = [f"no access-list {acl_name} extended {raw}"]
                rollback = [f"access-list {acl_name} extended {raw}"]
                if not has_permit and port:
                    commands.extend(
                        [
                            f"no access-group {acl_name} in interface {port}",
                        ]
                    )
                    rollback.extend(
                        [f"access-group {acl_name} in interface {port}"]
                    )
            else:
                commands = [f"no access-list {acl_name} {raw}"]
                rollback = [f"access-list {acl_name} {raw}"]
                if not has_permit and port:
                    commands.extend(
                        [
                            f"interface {port}",
                            f"no ip access-group {acl_name} in",
                            "exit",
                        ]
                    )
                    rollback.extend(
                        [
                            f"interface {port}",
                            f"ip access-group {acl_name} in",
                            "exit",
                        ]
                    )
            fix = FixAction(
                device=device,
                commands=tuple(commands),
                reason="the ACL's denying rule blocks the confirmed objective",
                rollback_commands=tuple(rollback),
                expected_state=(
                    {"acl_rule_removed": raw}
                    if has_permit else {"acl_binding_removed": acl_name}
                ),
            )
        return self._diagnostic(
            cause_class="acl-block",
            layer="security",
            root_cause=(
                f"ACL {acl_name or '?'} on {device}"
                f"{':' + port if port else ''} blocks the objective"
            ),
            device=device, port=port,
            evidence=evidence,
            confidence="high" if rule is not None else "medium",
            fix=fix,
            impact="the flow is discarded by policy before it reaches the destination",
            objective=objective,
            trace=trace,
            uncertainty=None if fix is not None else (
                "the specific rule could not be isolated; no automatic edit"
            ),
            suspected=False,
        )

    def _asa_nameif(
        self, device: str, port: str | None, evidence: tuple[str, ...],
        objective: Objective, trace: TraceResult,
    ) -> Diagnostic:
        expected = (
            self.expected.asa_zones.get((device, port or ""))
            if self.expected is not None else None
        )
        fix = None
        if expected is not None and port:
            fix = FixAction(
                device=device,
                commands=(f"interface {port}", f"nameif {expected[0]}", "exit"),
                reason="ASA interface has no nameif",
                rollback_commands=(f"interface {port}", "no nameif", "exit"),
                expected_state={"nameif": expected[0]},
            )
        return self._diagnostic(
            cause_class="wrong-asa-nameif",
            layer="security",
            root_cause=f"ASA interface {device}:{port} has no nameif",
            device=device, port=port,
            evidence=evidence,
            confidence="high" if expected is not None else "medium",
            fix=fix,
            impact="the interface cannot be named in an ASA policy",
            objective=objective,
            trace=trace,
            uncertainty=None if fix is not None else (
                "the intended zone name is not stated"
            ),
            suspected=False,
        )

    def _asa_level(
        self, device: str, port: str | None, evidence: tuple[str, ...],
        objective: Objective, trace: TraceResult,
    ) -> Diagnostic:
        expected = (
            self.expected.asa_zones.get((device, port or ""))
            if self.expected is not None else None
        )
        fix = None
        if expected is not None and port:
            fix = FixAction(
                device=device,
                commands=(
                    f"interface {port}", f"security-level {expected[1]}", "exit"
                ),
                reason="ASA interface has no security-level",
                rollback_commands=(f"interface {port}", "no security-level", "exit"),
                expected_state={"security_level": expected[1]},
            )
        return self._diagnostic(
            cause_class="wrong-security-level",
            layer="security",
            root_cause=f"ASA interface {device}:{port} has no security-level",
            device=device, port=port,
            evidence=evidence,
            confidence="high" if expected is not None else "medium",
            fix=fix,
            impact="the ASA cannot order traffic between zones",
            objective=objective,
            trace=trace,
            uncertainty=None if fix is not None else (
                "the intended level is not stated"
            ),
            suspected=False,
        )

    # -- services -----------------------------------------------------------

    def _dhcp_lease(
        self, device: str, evidence: tuple[str, ...],
        objective: Objective, trace: TraceResult,
    ) -> Diagnostic:
        pools = [
            (router, pool)
            for router, config in self.corpus.parsed.items()
            for pool in config.dhcp_pools.values()
        ]
        bad: tuple[str, Any] | None = None
        for router, pool in pools:
            if not pool.network or not pool.mask:
                continue
            try:
                network = ipaddress.IPv4Network(
                    f"{pool.network}/{pool.mask}", strict=False
                )
            except ValueError:
                bad = (router, pool)
                break
            if pool.default_router:
                try:
                    if ipaddress.IPv4Address(pool.default_router[0]) not in network:
                        bad = (router, pool)
                        break
                except ValueError:
                    bad = (router, pool)
                    break
        pool_evidence = tuple(
            f"DHCP pool {pool.name!r} on {router}: network {pool.network} "
            f"{pool.mask}, default-router "
            f"{', '.join(pool.default_router) or 'none'}"
            for router, pool in pools
        )
        fix = None
        router_name = bad[0] if bad is not None else device
        if bad is not None:
            router_name, pool = bad
            expected_pool = self._expected_pool_for_device(router_name)
            correct_router = (
                expected_pool.default_router
                if expected_pool is not None
                else self._gateway_on_subnet(
                    pool.network or "", pool.mask or "", exclude=router_name
                )
            )
            if correct_router:
                previous = pool.default_router[0] if pool.default_router else None
                commands = [
                    f"ip dhcp pool {pool.name}",
                    f"network {pool.network} {pool.mask}",
                    f"default-router {correct_router}",
                    "exit",
                ]
                rollback = [
                    f"ip dhcp pool {pool.name}",
                    f"network {pool.network} {pool.mask}",
                    *(
                        [f"default-router {previous}"] if previous else []
                    ),
                    "exit",
                ]
                fix = FixAction(
                    device=router_name,
                    commands=tuple(commands),
                    reason="DHCP pool's default router is outside its subnet",
                    rollback_commands=tuple(rollback),
                    expected_state={"dhcp_default_router": correct_router},
                )
        if bad is None and not pools:
            return self._unknown(
                root_cause=(
                    f"{device} has no DHCP lease and no pool was observed on the "
                    "network"
                ),
                layer="services",
                evidence=(*evidence, *pool_evidence),
                objective=objective,
                uncertainty=(
                    "UNKNOWN - ADDITIONAL VERIFICATION REQUIRED: the server "
                    "configuration is not visible"
                ),
                device=device,
            )
        return self._diagnostic(
            cause_class="dhcp-subnet-mismatch",
            layer="services",
            root_cause=(
                f"DHCP pool {bad[1].name!r} on {router_name} does not serve its "
                "own default router"
                if bad is not None else
                f"{device} has no lease although pools exist"
            ),
            device=router_name,
            evidence=(*evidence, *pool_evidence),
            confidence="high" if bad is not None else "medium",
            fix=fix,
            impact="clients on the subnet cannot obtain a usable address",
            objective=objective,
            trace=trace,
            uncertainty=None if bad is not None else (
                "no pool-level contradiction was observed; the lease failure has "
                "no configuration evidence"
            ),
            suspected=False,
        )

    def _dns_wrong(
        self, device: str, evidence: tuple[str, ...],
        objective: Objective, trace: TraceResult,
    ) -> Diagnostic:
        inventory = self.inventory.device(device)
        observed = None
        if inventory is not None and inventory.host is not None and inventory.host.dns.is_known:
            servers = list(inventory.host.dns.value or [])
            observed = servers[0] if servers else None
        expected = self.expected.dns.get(device) if self.expected is not None else None
        fix = None
        if expected and inventory is not None and inventory.host is not None:
            fix = FixAction(
                device=device, operation="configure_host",
                host_config={
                    "dhcp": False,
                    "ip": (
                        str(inventory.host.ip.value)
                        if inventory.host.ip.is_known else None
                    ),
                    "mask": (
                        str(inventory.host.mask.value)
                        if inventory.host.mask.is_known else None
                    ),
                    "gateway": (
                        str(inventory.host.gateway.value)
                        if inventory.host.gateway.is_known else None
                    ),
                    "dns": expected,
                },
                reason="restore the expected DNS server",
                expected_state={"dns": expected},
            )
        return self._diagnostic(
            cause_class="dns-misconfiguration",
            layer="services",
            root_cause=(
                f"{device} uses DNS {observed or 'none'}; expected {expected or 'unknown'}"
            ),
            device=device,
            evidence=evidence,
            confidence="high" if expected else "medium",
            fix=fix,
            impact="name resolution fails even when IP connectivity works",
            objective=objective,
            trace=trace,
            uncertainty=None if expected else (
                "no confirmed DNS server is stated"
            ),
            suspected=False,
        )

    # -- config-wide rules --------------------------------------------------

    def _config_faults(
        self,
        devices: set[str] | None,
        objective: Objective | None,
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        out.extend(self._l1_config_faults(devices, objective))
        out.extend(self._l2_config_faults(devices, objective))
        out.extend(self._l3_config_faults(devices, objective))
        out.extend(self._security_config_faults(devices, objective))
        out.extend(self._service_config_faults(devices, objective))
        return out

    # -- L1 -----------------------------------------------------------------

    def _missing_cable(
        self,
        device: str,
        port: str | None,
        evidence: tuple[str, ...],
        objective: Objective,
        trace: TraceResult,
    ) -> Diagnostic:
        expected_link: LinkSpec | None = None
        expected_peer: str | None = None
        if self.expected is not None and port is not None:
            for link in self.expected.links:
                if link.a.device == device and link.a.interface_hint == port:
                    expected_link = link
                    expected_peer = link.b.device
                    break
                if link.b.device == device and link.b.interface_hint == port:
                    expected_link = link
                    expected_peer = link.a.device
                    break
        inventory = self.inventory.device(device)
        interface = inventory.interface(port) if inventory and port else None
        observed_peer = (
            str(interface.peers.value[0])
            if interface is not None and interface.peers.is_known
            and interface.peers.value else None
        )
        if expected_peer and observed_peer and observed_peer != expected_peer:
            return self._diagnostic(
                cause_class="wrong-cable-endpoint",
                layer="L1",
                root_cause=(
                    f"{device}:{port} is cabled to {observed_peer}, not the "
                    f"designed {expected_peer}"
                ),
                device=device, port=port,
                evidence=(*evidence, f"observed peer {observed_peer}",
                          f"expected peer {expected_peer}"),
                confidence="high",
                fix=None,
                impact="traffic follows the wrong physical path",
                objective=objective,
                trace=trace,
                uncertainty=(
                    "recabling needs the two correct ports; no cable is "
                    "deleted or created automatically"
                ),
                suspected=False,
            )
        fix = None
        if expected_link is not None and port is not None:
            fix = FixAction(
                device=device,
                operation="create_link",
                commands=(),
                reason="the designed cable is missing",
                expected_state={
                    "link_id": expected_link.id,
                    "a_device": expected_link.a.device,
                    "a_port": expected_link.a.interface_hint,
                    "b_device": expected_link.b.device,
                    "b_port": expected_link.b.interface_hint,
                    "media": expected_link.media,
                },
            )
        return self._diagnostic(
            cause_class="missing-cable",
            layer="L1",
            root_cause=(
                f"{device}:{port or '?'} has no cable"
                + (f"; the design connects it to {expected_peer}" if expected_peer else "")
            ),
            device=device, port=port,
            evidence=evidence,
            confidence="high" if expected_link is not None else "medium",
            fix=fix,
            impact="the two devices have no physical path",
            objective=objective,
            trace=trace,
            uncertainty=None if fix is not None else (
                "no designed peer for this port is known; a cable cannot be "
                "placed without guessing"
            ),
            suspected=False,
        )

    def _l1_config_faults(
        self, devices: set[str] | None, objective: Objective | None
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        for device in self.inventory.devices.values():
            if devices is not None and device.name not in devices:
                continue
            for interface in device.interfaces.values():
                if interface.peers.is_known and interface.peers.value:
                    if interface.state.is_known and str(interface.state.value) == "down":
                        admin = (
                            str(interface.admin_state.value)
                            if interface.admin_state.is_known else None
                        )
                        cause = "port-shutdown" if admin == "shutdown" else "port-down-physical"
                        fix = (
                            FixAction(
                                device=device.name,
                                commands=(
                                    f"interface {interface.name}",
                                    "no shutdown",
                                    "exit",
                                ),
                                reason="cabled interface is administratively down",
                                rollback_commands=(
                                    f"interface {interface.name}",
                                    "shutdown",
                                    "exit",
                                ),
                            )
                            if admin == "shutdown" else None
                        )
                        out.append(
                            self._diagnostic(
                                cause_class=cause,
                                layer="L1",
                                root_cause=(
                                    f"{device.name}:{interface.name} is "
                                    + ("administratively down" if admin == "shutdown" else "down")
                                ),
                                device=device.name,
                                port=interface.name,
                                evidence=(
                                    "topology read-back: cabled port "
                                    f"{interface.name} state down",
                                    f"running-config admin state: {admin or 'not shut down'}",
                                ),
                                confidence="high",
                                fix=fix,
                                impact="this cable carries no traffic",
                                objective=objective,
                                suspected=False,
                            )
                        )
        # Expected cables that are not present, or land on another port.
        if self.expected is not None:
            for link in self.expected.links:
                for endpoint in (link.a, link.b):
                    if not endpoint.interface_hint:
                        continue
                    device = self.inventory.device(endpoint.device)
                    if device is None:
                        continue
                    interface = device.interface(endpoint.interface_hint)
                    if interface is None:
                        continue
                    no_cable = (
                        interface.linked.is_known and not interface.linked.value
                    ) or (
                        interface.peers.is_known and not interface.peers.value
                    )
                    if no_cable:
                        expected_peer = (
                            link.b.device if endpoint is link.a else link.a.device
                        )
                        out.append(
                            self._diagnostic(
                                cause_class="missing-cable",
                                layer="L1",
                                root_cause=(
                                    f"{endpoint.device}:{endpoint.interface_hint} has "
                                    f"no cable; the design connects it to {expected_peer}"
                                ),
                                device=endpoint.device,
                                port=endpoint.interface_hint,
                                evidence=(
                                    f"expected link {link.id}",
                                    "observed peer list is empty",
                                ),
                                confidence="high",
                                fix=FixAction(
                                    device=endpoint.device,
                                    operation="create_link",
                                    commands=(),
                                    reason="the designed cable is missing",
                                    expected_state={
                                        "link_id": link.id,
                                        "a_device": link.a.device,
                                        "a_port": link.a.interface_hint,
                                        "b_device": link.b.device,
                                        "b_port": link.b.interface_hint,
                                        "media": link.media,
                                    },
                                ),
                                impact="the two devices have no physical path",
                                objective=objective,
                                suspected=False,
                            )
                        )
                    elif interface.peers.value:
                        observed_peer = str(interface.peers.value[0])
                        expected_peer = (
                            link.b.device if endpoint is link.a else link.a.device
                        )
                        if observed_peer != expected_peer:
                            out.append(
                                self._diagnostic(
                                    cause_class="wrong-cable-endpoint",
                                    layer="L1",
                                    root_cause=(
                                        f"{endpoint.device}:{endpoint.interface_hint} "
                                        f"is cabled to {observed_peer}, not the "
                                        f"designed {expected_peer}"
                                    ),
                                    device=endpoint.device,
                                    port=endpoint.interface_hint,
                                    evidence=(
                                        f"expected peer {expected_peer}",
                                        f"observed peer {observed_peer}",
                                    ),
                                    confidence="high",
                                    fix=None,
                                    impact="traffic follows the wrong physical path",
                                    objective=objective,
                                    uncertainty=(
                                        "recabling needs the two correct ports; "
                                        "the engine will not delete a cable it "
                                        "cannot reconnect safely"
                                    ),
                                    suspected=False,
                                )
                            )
        return out

    def _l2_config_faults(
        self, devices: set[str] | None, objective: Objective | None
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        for device in self.inventory.devices.values():
            if devices is not None and device.name not in devices:
                continue
            if not device.is_switch:
                continue
            parsed = self.corpus.parsed_for(device.name)
            for interface in device.interfaces.values():
                if not (interface.peers.is_known and interface.peers.value):
                    continue
                expected_vlan = (
                    self.expected.access_vlans.get((device.name, interface.name))
                    if self.expected is not None else None
                )
                if (
                    expected_vlan is not None
                    and interface.vlan.is_known
                    and int(interface.vlan.value) != expected_vlan
                ):
                    current = int(interface.vlan.value)
                    out.append(
                        self._diagnostic(
                            cause_class="wrong-access-vlan",
                            layer="L2",
                            root_cause=(
                                f"{device.name}:{interface.name} is in VLAN "
                                f"{current}; the design puts it in VLAN {expected_vlan}"
                            ),
                            device=device.name, port=interface.name,
                            vlan=expected_vlan,
                            evidence=(
                                f"observed access VLAN {current}",
                                f"expected access VLAN {expected_vlan}",
                            ),
                            confidence="high",
                            fix=FixAction(
                                device=device.name,
                                commands=(
                                    f"interface {interface.name}",
                                    f"switchport access vlan {expected_vlan}",
                                    "exit",
                                ),
                                reason="wrong access VLAN",
                                rollback_commands=(
                                    f"interface {interface.name}",
                                    f"switchport access vlan {current}",
                                    "exit",
                                ),
                                expected_state={"access_vlan": expected_vlan},
                            ),
                            impact="the port bridges the endpoint into the wrong VLAN",
                            objective=objective,
                            suspected=False,
                        )
                    )
                if self.expected is not None and interface.trunk.is_known and interface.trunk.value:
                    allowed_expected = self.expected.trunk_allowed.get(
                        (device.name, interface.name)
                    )
                    if allowed_expected is not None and interface.allowed_vlans.is_known:
                        allowed = {int(v) for v in interface.allowed_vlans.value or []}
                        missing = sorted(set(allowed_expected) - allowed)
                        if missing:
                            out.append(
                                self._diagnostic(
                                    cause_class="trunk-vlan-not-allowed",
                                    layer="L2",
                                    root_cause=(
                                        f"{device.name}:{interface.name} trunk does "
                                        f"not allow VLAN(s) {missing}"
                                    ),
                                    device=device.name, port=interface.name,
                                    vlan=missing[0],
                                    evidence=(
                                        f"expected allowed {sorted(set(allowed_expected))}",
                                        f"observed allowed {sorted(allowed)}",
                                    ),
                                    confidence="high",
                                    fix=FixAction(
                                        device=device.name,
                                        commands=(
                                            f"interface {interface.name}",
                                            "switchport trunk allowed vlan add "
                                            + ",".join(str(v) for v in missing),
                                            "exit",
                                        ),
                                        reason="trunk is missing designed VLANs",
                                        rollback_commands=(
                                            f"interface {interface.name}",
                                            "switchport trunk allowed vlan remove "
                                            + ",".join(str(v) for v in missing),
                                            "exit",
                                        ),
                                        expected_state={"add_vlans": missing},
                                    ),
                                    impact="those VLANs cannot cross this trunk",
                                    objective=objective,
                                    suspected=False,
                                )
                            )
                # VLAN referenced by an access port but absent from the table.
                if interface.vlan.is_known and device.vlans_table.is_known:
                    vlan = int(interface.vlan.value)
                    defined = {
                        int(row.get("id"))
                        for row in device.vlans_table.value or []
                        if isinstance(row, dict) and str(row.get("id", "")).isdigit()
                    }
                    if vlan not in defined:
                        name = (
                            self.expected.vlan_names.get(vlan)
                            if self.expected is not None else None
                        )
                        commands = [f"vlan {vlan}"]
                        if name:
                            commands.append(f"name {name}")
                        commands.append("exit")
                        out.append(
                            self._diagnostic(
                                cause_class="undefined-vlan",
                                layer="L2",
                                root_cause=(
                                    f"{device.name}:{interface.name} is assigned to "
                                    f"VLAN {vlan}, which is not defined on the switch"
                                ),
                                device=device.name, port=interface.name,
                                vlan=vlan,
                                evidence=(
                                    f"show vlan brief: {sorted(defined)}",
                                    f"access port VLAN {vlan}",
                                ),
                                confidence="high",
                                fix=FixAction(
                                    device=device.name,
                                    commands=tuple(commands),
                                    reason="referenced VLAN does not exist",
                                    rollback_commands=(f"no vlan {vlan}",),
                                    expected_state={"vlan_defined": vlan},
                                ),
                                impact="the switch drops frames for an unknown VLAN",
                                objective=objective,
                                suspected=False,
                            )
                        )
                # Native VLAN mismatch, reported once from the lower port.
                if interface.trunk.is_known and interface.trunk.value and interface.native_vlan.is_known:
                    for peer_name, local_port, peer_port in self.inventory.neighbors(device.name):
                        if local_port != interface.name:
                            continue
                        peer = self.inventory.device(peer_name)
                        peer_iface = peer.interface(peer_port) if peer else None
                        if (
                            peer_iface is not None
                            and peer_iface.native_vlan.is_known
                            and int(peer_iface.native_vlan.value)
                            != int(interface.native_vlan.value)
                            and (device.name, interface.name)
                            < (peer_name, peer_port)
                        ):
                            out.append(
                                self._diagnostic(
                                    cause_class="native-vlan-mismatch",
                                    layer="L2",
                                    root_cause=(
                                        f"native VLAN mismatch on "
                                        f"{device.name}:{interface.name} vs "
                                        f"{peer_name}:{peer_port}"
                                    ),
                                    device=device.name, port=interface.name,
                                    evidence=(
                                        f"native VLAN {interface.native_vlan.value}",
                                        f"peer native VLAN {peer_iface.native_vlan.value}",
                                    ),
                                    confidence="high",
                                    fix=None,
                                    impact="untagged frames are classified inconsistently",
                                    objective=objective,
                                    uncertainty=(
                                        "the correct native VLAN is a design choice"
                                    ),
                                    suspected=False,
                                )
                            )
                # STP blocking on a cabled port.
                if device.stp.is_known:
                    blocked = [
                        row for row in device.stp.value or []
                        if str(row.get("interface")) == interface.name
                        and str(row.get("state")).upper() in ("BLK", "ALN")
                    ]
                    if blocked:
                        out.append(
                            self._diagnostic(
                                cause_class="stp-blocking",
                                layer="L2",
                                root_cause=(
                                    f"{device.name}:{interface.name} is blocked by "
                                    "spanning tree"
                                ),
                                device=device.name, port=interface.name,
                                evidence=(f"show spanning-tree: {blocked[0]}",),
                                confidence="high",
                                fix=None,
                                impact="the port does not forward",
                                objective=objective,
                                uncertainty=(
                                    "blocking may be correct loop prevention; "
                                    "changing it requires a stated STP design"
                                ),
                                suspected=False,
                            )
                        )
        return out

    def _l3_config_faults(
        self, devices: set[str] | None, objective: Objective | None
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        if self.expected is None:
            # Without an expected address there is no evidence a wrong address
            # is wrong; only self-contradictions are detectable.
            return out
        for (device_name, interface_hint), (ip, mask) in self.expected.addresses.items():
            if devices is not None and device_name not in devices:
                continue
            device = self.inventory.device(device_name)
            if device is None:
                continue
            interface = device.interface(interface_hint) if interface_hint else None
            if interface is None:
                continue
            if not (interface.ip.is_known and interface.mask.is_known):
                continue
            observed_ip = str(interface.ip.value)
            observed_mask = str(interface.mask.value)
            if observed_ip == ip and observed_mask == mask:
                continue
            cause = "wrong-ip" if observed_ip != ip else "wrong-mask"
            if device.is_host:
                host = device.host
                fix = FixAction(
                    device=device_name, operation="configure_host",
                    host_config={
                        "dhcp": False,
                        "ip": ip,
                        "mask": mask,
                        "gateway": (
                            str(host.gateway.value)
                            if host is not None and host.gateway.is_known else None
                        ),
                        "dns": (
                            (list(host.dns.value) or [None])[0]
                            if host is not None and host.dns.is_known else None
                        ),
                    },
                    reason="host address differs from the confirmed design",
                    expected_state={"ip": ip, "mask": mask},
                )
            else:
                commands = (
                    f"interface {interface_hint}",
                    f"ip address {ip} {mask}",
                    "no shutdown",
                    "exit",
                )
                fix = FixAction(
                    device=device_name,
                    commands=tuple(commands),
                    reason="address differs from the confirmed design",
                    rollback_commands=(
                        f"interface {interface_hint}",
                        f"ip address {observed_ip} {observed_mask}",
                        "exit",
                    ),
                    expected_state={"ip": ip, "mask": mask},
                )
            out.append(
                self._diagnostic(
                    cause_class=cause,
                    layer="L3",
                    root_cause=(
                        f"{device_name}:{interface_hint} has {observed_ip}/{observed_mask}; "
                        f"expected {ip}/{mask}"
                    ),
                    device=device_name, port=interface_hint,
                    subnet=self._subnet_for_device(device_name),
                    evidence=(
                        f"observed {observed_ip}/{observed_mask}",
                        f"expected {ip}/{mask}",
                    ),
                    confidence="high",
                    fix=fix,
                    impact="the interface is addressed outside the design",
                    objective=objective,
                    suspected=False,
                )
            )
        # Expected host gateways.
        for device_name, assignment in self.expected.host_assignments.items():
            if devices is not None and device_name not in devices:
                continue
            device = self.inventory.device(device_name)
            if device is None or device.host is None or not device.host.gateway.is_known:
                continue
            expected_gateway = assignment[2]
            observed_gateway = str(device.host.gateway.value)
            if expected_gateway and observed_gateway != expected_gateway:
                out.append(
                    self._diagnostic(
                        cause_class="wrong-gateway",
                        layer="L3",
                        root_cause=(
                            f"{device_name} uses gateway {observed_gateway}; "
                            f"expected {expected_gateway}"
                        ),
                        device=device_name,
                        subnet=self._subnet_for_device(device_name),
                        evidence=(
                            f"ipconfig /all gateway {observed_gateway}",
                            f"expected gateway {expected_gateway}",
                        ),
                        confidence="high",
                        fix=FixAction(
                            device=device_name, operation="configure_host",
                            host_config={
                                "dhcp": False,
                                "ip": (
                                    str(device.host.ip.value)
                                    if device.host.ip.is_known else None
                                ),
                                "mask": (
                                    str(device.host.mask.value)
                                    if device.host.mask.is_known else None
                                ),
                                "gateway": expected_gateway,
                            },
                            reason="host default gateway differs from the design",
                            expected_state={"gateway": expected_gateway},
                        ),
                        impact="off-subnet traffic is sent to a non-routing address",
                        objective=objective,
                        suspected=False,
                    )
                )
        # Expected routes missing from the observed table.
        for device_name, routes in self.expected.routes.items():
            if devices is not None and device_name not in devices:
                continue
            device = self.inventory.device(device_name)
            if device is None or not device.routes.is_known:
                continue
            table = device.routes.value or []
            for route in routes:
                try:
                    network = ipaddress.ip_network(route.destination, strict=False)
                except ValueError:
                    continue
                present = any(
                    isinstance(row, dict)
                    and row.get("network") == str(network.network_address)
                    and (
                        int(row["prefix"])
                        if row.get("prefix") is not None else -1
                    ) == network.prefixlen
                    for row in table
                )
                if present:
                    # Present is not correct: a route can point at the wrong
                    # next hop and still be in the table (and on a connected
                    # destination the flow may not even notice).
                    parsed = self.corpus.parsed_for(device_name)
                    observed_route = None
                    if parsed is not None:
                        observed_route = next(
                            (
                                r for r in parsed.static_routes
                                if r.cidr == str(network)
                            ),
                            None,
                        )
                    expected_next = route.next_hop or route.exit_interface
                    observed_next = (
                        (observed_route.next_hop or observed_route.interface)
                        if observed_route is not None else None
                    )
                    if (
                        expected_next and observed_next
                        and expected_next != observed_next
                    ):
                        old = self._route_command(
                            device_name, network, observed_next
                        )
                        new = self._route_command(
                            device_name, network, expected_next
                        )
                        out.append(
                            self._diagnostic(
                                cause_class="wrong-next-hop",
                                layer="L3",
                                root_cause=(
                                    f"{device_name} routes {route.destination} "
                                    f"via {observed_next}; expected {expected_next}"
                                ),
                                device=device_name,
                                subnet=str(network),
                                evidence=(
                                    f"observed route {old}",
                                    f"expected route {new}",
                                ),
                                confidence="high",
                                fix=FixAction(
                                    device=device_name,
                                    commands=(f"no {old}", new),
                                    reason="route points at the wrong next hop",
                                    rollback_commands=(f"no {new}", old),
                                    expected_state={"route": route.destination},
                                ),
                                impact="packets are handed to the wrong next hop",
                                objective=objective,
                                suspected=False,
                            )
                        )
                    continue
                if not (route.next_hop or route.exit_interface):
                    continue
                command = self._route_command(
                    device_name, network, route.next_hop or route.exit_interface or ""
                )
                out.append(
                    self._diagnostic(
                        cause_class="missing-route",
                        layer="L3",
                        root_cause=(
                            f"{device_name} has no observed route to "
                            f"{route.destination}"
                        ),
                        device=device_name,
                        subnet=str(network),
                        evidence=(
                            f"observed route table: {len(table)} entries",
                            f"expected {route.destination} -> "
                            f"{route.next_hop or route.exit_interface}",
                        ),
                        confidence="high",
                        fix=FixAction(
                            device=device_name,
                            commands=(command,),
                            reason="a designed static route is absent",
                            rollback_commands=(f"no {command}",),
                            expected_state={"route": route.destination},
                        ),
                        impact="traffic for that network is dropped or default-routed",
                        objective=objective,
                        suspected=False,
                    )
                )
        return out

    def _security_config_faults(
        self, devices: set[str] | None, objective: Objective | None
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        for device_name, parsed in self.corpus.parsed.items():
            if devices is not None and device_name not in devices:
                continue
            if parsed.platform.value != "asa":
                continue
            for interface in parsed.interfaces.values():
                if not interface.ip or interface.ip == "dhcp":
                    continue
                if not interface.nameif:
                    out.append(
                        self._diagnostic(
                            cause_class="wrong-asa-nameif",
                            layer="security",
                            root_cause=(
                                f"ASA interface {device_name}:{interface.name} has "
                                "an address but no nameif"
                            ),
                            device=device_name, port=interface.name,
                            evidence=("running-config has no nameif",),
                            confidence="high",
                            fix=None,
                            impact="the interface cannot be referenced by ASA policy",
                            objective=objective,
                            uncertainty="the intended zone name is a design choice",
                            suspected=False,
                        )
                    )
                if interface.security_level is None:
                    out.append(
                        self._diagnostic(
                            cause_class="wrong-security-level",
                            layer="security",
                            root_cause=(
                                f"ASA interface {device_name}:{interface.name} has "
                                "an address but no security-level"
                            ),
                            device=device_name, port=interface.name,
                            evidence=("running-config has no security-level",),
                            confidence="high",
                            fix=None,
                            impact="the ASA cannot order traffic between this zone and others",
                            objective=objective,
                            uncertainty="the intended level is a design choice",
                            suspected=False,
                        )
                    )
            # An ACL defined but never bound is a policy that is not enforced.
            bound = {
                str(binding.get("name"))
                for binding in parsed.globals.acl_bindings
                if binding.get("name")
            }
            defined = set(parsed.acls)
            unbound = sorted(defined - bound)
            if unbound:
                out.append(
                    self._diagnostic(
                        cause_class="missing-asa-access-group",
                        layer="security",
                        root_cause=(
                            f"ASA ACL(s) {unbound} are defined on {device_name} but "
                            "not bound to any interface"
                        ),
                        device=device_name,
                        evidence=(
                            f"defined ACLs: {sorted(defined)}",
                            f"bound ACLs: {sorted(bound)}",
                        ),
                        confidence="medium",
                        fix=None,
                        impact="the policy exists on the box but is not enforced",
                        objective=objective,
                        uncertainty=(
                            "binding an ACL is only safe when the intended "
                            "interface and direction are stated; no automatic bind"
                        ),
                        suspected=False,
                    )
                )
        return out

    def _service_config_faults(
        self, devices: set[str] | None, objective: Objective | None
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        for device_name, parsed in self.corpus.parsed.items():
            if devices is not None and device_name not in devices:
                continue
            for pool in parsed.dhcp_pools.values():
                if not pool.network or not pool.mask:
                    continue
                try:
                    network = ipaddress.IPv4Network(
                        f"{pool.network}/{pool.mask}", strict=False
                    )
                except ValueError:
                    continue
                for router in pool.default_router:
                    try:
                        inside = ipaddress.IPv4Address(router) in network
                    except ValueError:
                        inside = False
                    if not inside:
                        out.append(
                            self._diagnostic(
                                cause_class="dhcp-subnet-mismatch",
                                layer="services",
                                root_cause=(
                                    f"DHCP pool {pool.name!r} on {device_name} has "
                                    f"default-router {router} outside "
                                    f"{pool.network}/{pool.mask}"
                                ),
                                device=device_name,
                                subnet=str(network),
                                evidence=(
                                    f"pool network {pool.network} {pool.mask}",
                                    f"default-router {router}",
                                ),
                                confidence="high",
                                fix=None,
                                impact="clients receive a gateway that does not exist on their subnet",
                                objective=objective,
                                uncertainty=(
                                    "the correct router address is not stated"
                                ),
                                suspected=False,
                            )
                        )
        if self.expected is not None:
            for device_name, expected_dns in self.expected.dns.items():
                if devices is not None and device_name not in devices:
                    continue
                device = self.inventory.device(device_name)
                if device is None or device.host is None or not device.host.dns.is_known:
                    continue
                observed = list(device.host.dns.value or [])
                if observed and expected_dns not in observed:
                    out.append(
                        self._diagnostic(
                            cause_class="dns-misconfiguration",
                            layer="services",
                            root_cause=(
                                f"{device_name} uses DNS {observed}; expected "
                                f"{expected_dns}"
                            ),
                            device=device_name,
                            evidence=(
                                f"ipconfig /all DNS {observed}",
                                f"expected DNS {expected_dns}",
                            ),
                            confidence="high",
                            fix=FixAction(
                                device=device_name, operation="configure_host",
                                host_config={
                                    "dhcp": False,
                                    "ip": (
                                        str(device.host.ip.value)
                                        if device.host.ip.is_known else None
                                    ),
                                    "mask": (
                                        str(device.host.mask.value)
                                        if device.host.mask.is_known else None
                                    ),
                                    "gateway": (
                                        str(device.host.gateway.value)
                                        if device.host.gateway.is_known else None
                                    ),
                                    "dns": expected_dns,
                                },
                                reason="host points at the wrong DNS server",
                                expected_state={"dns": expected_dns},
                            ),
                            impact="name resolution fails",
                            objective=objective,
                            suspected=False,
                        )
                    )
        return out

    # -- helpers ------------------------------------------------------------

    def _diagnostic(
        self,
        *,
        cause_class: str,
        layer: str,
        root_cause: str,
        evidence: Iterable[str],
        confidence: str,
        impact: str,
        device: str | None = None,
        port: str | None = None,
        vlan: int | None = None,
        subnet: str | None = None,
        fix: FixAction | None = None,
        objective: Objective | None = None,
        trace: TraceResult | None = None,
        uncertainty: str | None = None,
        suspected: bool = False,
    ) -> Diagnostic:
        if cause_class not in CAUSE_CLASSES:
            raise ValueError(f"unknown cause class {cause_class!r}")
        evidence_tuple = tuple(str(item) for item in evidence if str(item).strip())
        return Diagnostic(
            diagnosis_id=new_id("s4diag"),
            cause_class=cause_class,
            layer=layer,
            root_cause=root_cause,
            affected_device=device,
            affected_interface=port,
            vlan=vlan,
            subnet=subnet,
            evidence=evidence_tuple,
            confidence=confidence,
            recommended_fix=(fix,) if fix is not None else (),
            impact=impact,
            verification_requirements=(
                (
                    f"re-check objective {objective.id}: {objective.statement}"
                    if objective is not None else
                    "re-run the failing objective"
                ),
                "re-run every criterion that passed before the change",
            ),
            objective_id=objective.id if objective is not None else None,
            objective_confirmed=bool(objective is not None and objective.actionable),
            suspected=suspected,
            uncertainty=uncertainty,
            first_failing_hop=(
                trace.first_failing_hop if trace is not None else None
            ),
            details={
                "trace_status": trace.status if trace is not None else None,
                "first_failing_check": (
                    trace.first_failing_check.name
                    if trace is not None and trace.first_failing_check is not None
                    else None
                ),
            },
        )

    def _unknown(
        self,
        *,
        root_cause: str,
        layer: str,
        evidence: Iterable[str],
        objective: Objective | None,
        uncertainty: str,
        device: str | None = None,
        port: str | None = None,
    ) -> Diagnostic:
        return self._diagnostic(
            cause_class="unknown",
            layer=layer,
            root_cause=root_cause,
            evidence=evidence,
            confidence="unknown",
            impact="cannot be stated without more evidence",
            device=device,
            port=port,
            fix=None,
            objective=objective,
            uncertainty=uncertainty,
            suspected=True,
        )

    def _expected_access_vlan(
        self, device: str, port: str | None, objective: Objective
    ) -> int | None:
        if objective.params.get("vlan") is not None:
            return int(objective.params["vlan"])
        if self.expected is not None and port is not None:
            return self.expected.access_vlans.get((device, port))
        return None

    def _expected_trunk_vlan(
        self,
        device: str,
        port: str | None,
        objective: Objective,
        trace: TraceResult | None = None,
    ) -> int | None:
        if objective.params.get("vlan") is not None:
            return int(objective.params["vlan"])
        if trace is not None and trace.vlan is not None:
            return trace.vlan
        if self.expected is not None and port is not None:
            allowed = self.expected.trunk_allowed.get((device, port))
            if allowed:
                return allowed[0]
        return None

    def _expected_native_vlan(self, device: str, port: str | None) -> int | None:
        if self.expected is not None and port is not None:
            return self.expected.native_vlans.get((device, port))
        return None

    def _observed_access_vlan(self, device: str, port: str | None) -> int | None:
        inventory = self.inventory.device(device)
        interface = inventory.interface(port) if inventory and port else None
        if interface is not None and interface.vlan.is_known:
            return int(interface.vlan.value)
        return None

    def _observed_allowed(self, device: str, port: str | None) -> list[int] | None:
        inventory = self.inventory.device(device)
        interface = inventory.interface(port) if inventory and port else None
        if interface is not None and interface.allowed_vlans.is_known:
            return [int(v) for v in interface.allowed_vlans.value or []]
        return None

    def _gateway_on_subnet(
        self, ip: str, mask: str, *, exclude: str | None = None
    ) -> str | None:
        try:
            network = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        except ValueError:
            return None
        candidates: set[str] = set()
        for device in self.inventory.devices.values():
            if device.name == exclude or device.category not in (
                "router", "firewall", "wireless_router"
            ):
                continue
            for interface in device.interfaces.values():
                if not (interface.ip.is_known and interface.mask.is_known):
                    continue
                try:
                    if ipaddress.IPv4Address(str(interface.ip.value)) in network:
                        candidates.add(str(interface.ip.value))
                except ValueError:
                    continue
        return candidates.pop() if len(candidates) == 1 else None

    def _subnet_for_device(self, device: str) -> str | None:
        inventory = self.inventory.device(device)
        if inventory is None:
            return None
        if inventory.host is not None and inventory.host.ip.is_known and inventory.host.mask.is_known:
            try:
                return str(
                    ipaddress.IPv4Network(
                        f"{inventory.host.ip.value}/{inventory.host.mask.value}",
                        strict=False,
                    )
                )
            except ValueError:
                return None
        for interface in inventory.interfaces.values():
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

    def _pools_for_device(self, device: str) -> list:
        pools: list = []
        for config in self.corpus.parsed.values():
            pools.extend(config.dhcp_pools.values())
        return pools

    def _expected_pool_for_device(self, device: str) -> DhcpPoolSpec | None:
        if self.expected is None:
            return None
        subnet = self._subnet_for_device(device)
        for pool in self.expected.dhcp_pools:
            if pool.device == device:
                return pool
            if subnet:
                expected_subnet = self.expected.spec.subnet_by_id(pool.subnet) if self.expected.spec else None
                if expected_subnet is not None and expected_subnet.cidr == subnet:
                    return pool
        return None

    @staticmethod
    def _dedupe(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
        seen: set[tuple[Any, ...]] = set()
        out: list[Diagnostic] = []
        for diagnostic in diagnostics:
            key = (
                diagnostic.cause_class,
                diagnostic.affected_device,
                diagnostic.affected_interface,
                diagnostic.vlan,
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(diagnostic)
        return out
