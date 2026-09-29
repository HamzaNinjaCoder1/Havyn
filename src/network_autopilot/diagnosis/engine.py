"""Evidence-based diagnosis: failed verification -> candidate root causes.

Rules:
- every candidate root cause is backed by named evidence;
- confidence reflects how much evidence supports the cause — a single weak
  signal is LOW, corroborated signals are HIGH;
- when evidence is insufficient, the diagnosis says so and proposes NO repair
  (speculative mutation is how labs get destroyed);
- the engine never mutates anything: it only produces diagnoses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.actual_state import ActualNetworkState
from ..core.ids import new_id
from ..core.network_spec import NetworkSpec
from ..core.observability import Observable
from ..core.requirements import Verdict
from ..verification.engine import VerificationReport

_EMPTY_READ = "runtime returned an empty value"


def fill_missing_routes(
    spec: NetworkSpec,
    state: ActualNetworkState,
    reader: Callable[[str], Observable] | None,
) -> int:
    """Observe route tables for the devices the spec gives static routes to.

    S1.11/F7 wiring: the route-missing rule weighs observed tables, but
    nothing used to fill `DeviceObservation.routes` on the live path. This
    reads only what the rule can use, only for route-bearing devices, and
    only when the field is not already KNOWN. A missing or failing reader
    leaves the field untouched — NOT_OBSERVED is evidence of nothing, so the
    rule stays silent rather than guessing (rule 7). Returns tables filled.
    """
    if reader is None:
        return 0
    filled = 0
    seen: set[str] = set()
    for route in spec.routing.static_routes:
        device_id = route.device
        if device_id in seen:
            continue
        seen.add(device_id)
        observed = state.devices.get(device_id)
        if observed is None or observed.routes.is_known:
            continue
        try:
            table = reader(device_id)
        except Exception:
            continue
        if table is not None and getattr(table, "is_known", False):
            observed.routes = table
            filled += 1
    return filled


@dataclass(frozen=True)
class Diagnosis:
    diagnosis_id: str
    failed_tests: tuple[str, ...]
    root_cause: str
    evidence: tuple[str, ...]
    confidence: str                  # high | medium | low | unknown
    affected_resources: tuple[str, ...]
    proposed_repair: tuple[dict[str, Any], ...]  # operation dicts; empty = no repair
    uncertainty: str | None

    @property
    def repairable(self) -> bool:
        return bool(self.proposed_repair) and self.confidence in ("high", "medium")

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnosis_id": self.diagnosis_id,
            "failed_tests": list(self.failed_tests),
            "root_cause": self.root_cause,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "affected_resources": list(self.affected_resources),
            "proposed_repair": list(self.proposed_repair),
            "uncertainty": self.uncertainty,
            "repairable": self.repairable,
        }


def diagnose(
    spec: NetworkSpec,
    report: VerificationReport,
    state: ActualNetworkState,
) -> tuple[Diagnosis, ...]:
    """Diagnose every failing/unknown area the evidence can explain."""
    diagnoses: list[Diagnosis] = []

    # 1. missing devices (structural failures with complete observation)
    for test in report.tests:
        if test.level == "structural" and test.target.startswith(("device:",)) is False and test.verdict is Verdict.FAIL:
            if test.test_id.startswith("VT-struct-device-"):
                device_id = test.target
                diagnoses.append(
                    Diagnosis(
                        diagnosis_id=new_id("diag"),
                        failed_tests=(test.test_id,),
                        root_cause=f"device {device_id!r} is absent from the workspace",
                        evidence=(str(test.observed_result), *test.evidence),
                        confidence="high",
                        affected_resources=(device_id,),
                        proposed_repair=(
                            {
                                "operation_type": "create_device",
                                "inputs": _device_inputs(spec, device_id),
                                "reason": "expected device missing",
                            },
                        ),
                        uncertainty=None,
                    )
                )
            elif test.test_id.startswith("VT-struct-link-"):
                diagnoses.append(
                    Diagnosis(
                        diagnosis_id=new_id("diag"),
                        failed_tests=(test.test_id,),
                        root_cause=f"link {test.target!r} is not present on its ports",
                        evidence=(str(test.observed_result), *test.evidence),
                        confidence="high",
                        affected_resources=(test.target,),
                        proposed_repair=(
                            {
                                "operation_type": "create_link",
                                "inputs": _link_inputs(spec, test.target),
                                "reason": "expected link missing",
                            },
                        ),
                        uncertainty=None,
                    )
                )

    # 2. addressing failures with observed evidence
    for test in report.tests:
        if test.level != "addressing" or test.verdict is not Verdict.FAIL:
            continue
        device_id, _, iface = test.target.partition(":")
        evidence = [str(test.observed_result), *test.evidence]
        if _is_dhcp_client(spec, device_id):
            # Rule 5 owns DHCP clients: the fix there is a fresh lease request,
            # gated on the pool being observed. Proposing a static address here
            # would contradict the spec and quietly replace DHCP with a hard
            # address the user never asked for.
            continue
        if test.observed_result == "no ip configured":
            root_cause = f"{device_id} {iface} has no IP configured"
            confidence = "high"
            uncertainty = None
        elif test.observed_result and "/" in str(test.observed_result):
            root_cause = f"{device_id} {iface} has wrong addressing"
            confidence = "high"
            uncertainty = None
        else:
            root_cause = f"{device_id} {iface} addressing is wrong"
            confidence = "medium"
            uncertainty = "observed value could not be fully read back"
        diagnoses.append(
            Diagnosis(
                diagnosis_id=new_id("diag"),
                failed_tests=(test.test_id,),
                root_cause=root_cause,
                evidence=tuple(evidence),
                confidence=confidence,
                affected_resources=(device_id,),
                proposed_repair=(
                    {
                        "operation_type": "reconfigure_address",
                        "inputs": _address_inputs(spec, device_id, iface),
                        "reason": "correct the observed addressing",
                    },
                ),
                uncertainty=uncertainty,
            )
        )

    # 3. connectivity failures: correlate flow evidence with state
    for test in report.tests:
        if test.level != "connectivity" or test.verdict is Verdict.PASS:
            continue
        candidates: list[Diagnosis] = []
        source, target = test.source or "?", test.target
        evidence: list[str] = [f"flow {source}->{target} observed: {test.observed_result}"]
        # endpoint absence is a direct, evidence-backed cause
        for endpoint in (source, target):
            device = state.devices.get(endpoint)
            if device is None:
                candidates.append(
                    Diagnosis(
                        diagnosis_id=new_id("diag"),
                        failed_tests=(test.test_id,),
                        root_cause=f"endpoint {endpoint!r} is absent from the observed workspace",
                        evidence=(*evidence, f"{endpoint} not in observed state"),
                        confidence="medium" if not report.observations_complete else "high",
                        affected_resources=(endpoint,),
                        proposed_repair=(),
                        uncertainty=(
                            "endpoint absence inferred from a possibly-partial observation"
                            if not report.observations_complete
                            else None
                        ),
                    )
                )
            else:
                down = [
                    f"{endpoint}:{p.name}"
                    for p in device.ports.values()
                    if p.state.is_known and p.state.value == "down"
                ]
                if down:
                    # One "no shutdown" pair per down port. `down` entries are
                    # already "device:port"; the port is the part after the
                    # first colon (port names contain no colon).
                    commands: list[str] = []
                    for entry in down:
                        commands.append(f"interface {entry.split(':', 1)[1]}")
                        commands.append("no shutdown")
                    candidates.append(
                        Diagnosis(
                            diagnosis_id=new_id("diag"),
                            failed_tests=(test.test_id,),
                            root_cause=f"interface(s) down on the path: {', '.join(down)}",
                            evidence=(*evidence, f"observed state=down on {', '.join(down)}"),
                            confidence="high",
                            affected_resources=(endpoint, *down),
                            proposed_repair=(
                                {
                                    "operation_type": "ios_command",
                                    "inputs": {
                                        "device": endpoint,
                                        "commands": commands,
                                    },
                                    "reason": "interface administratively/physically down",
                                },
                            ),
                            uncertainty=None,
                        )
                    )
        if candidates:
            diagnoses.extend(candidates)
        else:
            # no state signal correlates with the failure: say so, propose nothing
            diagnoses.append(
                Diagnosis(
                    diagnosis_id=new_id("diag"),
                    failed_tests=(test.test_id,),
                    root_cause="connectivity failed but no state anomaly was observed",
                    evidence=(*evidence, *test.evidence),
                    confidence="unknown",
                    affected_resources=(source, target),
                    proposed_repair=(),
                    uncertainty=(
                        "No correlating evidence (devices present, interfaces up, "
                        "addressing as expected). Possible causes outside observed "
                        "state: routing, cable media, service config. REPAIR BLOCKED: "
                        "no evidence-backed root cause; do not mutate speculatively."
                    ),
                )
            )

    # 3b. route-missing rule (S1.10/F7): a flow that fails while the destination
    # subnet is absent from the observed route table has a specific cause - the
    # router is missing the route the spec asked for. The repair re-pushes that
    # spec route. An UNKNOWN route table is not evidence, so it stays silent.
    for test in report.tests:
        if test.level != "connectivity" or test.verdict is Verdict.PASS:
            continue
        destination = state.devices.get(test.target or "")
        if destination is None:
            continue
        dest_ip = next(
            (
                str(p.ip.value)
                for p in destination.ports.values()
                if p.ip.is_known and p.ip.value and str(p.ip.value) != "0.0.0.0"
            ),
            None,
        )
        if not dest_ip:
            continue
        for route in spec.routing.static_routes:
            device_id = route.device
            if not _ip_in_network(dest_ip, route.destination):
                continue
            observed = state.devices.get(device_id)
            if observed is None or not observed.routes.is_known:
                continue
            table = observed.routes.value or []
            if any(
                _ip_in_network(dest_ip, f"{r.get('network')}/{r.get('prefix')}")
                for r in table
            ):
                continue
            commands = _route_commands(route)
            if not commands:
                continue
            diagnoses.append(
                Diagnosis(
                    diagnosis_id=new_id("diag"),
                    failed_tests=(test.test_id,),
                    root_cause=(
                        f"route to {route.destination} is missing on {device_id}; "
                        f"the flow to {dest_ip} cannot leave its subnet"
                    ),
                    evidence=(
                        f"flow {test.source}->{test.target}: {test.observed_result}",
                        f"observed route table on {device_id} has no entry for "
                        f"{route.destination}",
                    ),
                    confidence="high",
                    affected_resources=(device_id,),
                    proposed_repair=(
                        {
                            "operation_type": "ios_command",
                            "inputs": {
                                "device": device_id,
                                "commands": commands,
                            },
                            "reason": "re-push the spec's static route",
                        },
                    ),
                    uncertainty=None,
                )
            )

    # 4. L2 failures: a switch whose VLAN table or trunk state does not match
    # (#29). The repair re-pushes the switch's own compiled block rather than
    # guessing individual lines, so it cannot drift from a build.
    for test in report.tests:
        if test.level != "structural" or test.verdict is not Verdict.FAIL:
            continue
        if not test.test_id.startswith(("VT-struct-vlan", "VT-struct-trunk")):
            continue
        switch = _switch_for_l2_failure(spec, test.target)
        if switch is None:
            continue
        diagnoses.append(
            Diagnosis(
                diagnosis_id=new_id("diag"),
                failed_tests=(test.test_id,),
                root_cause=(
                    f"switch {switch!r} does not report the expected VLAN or "
                    f"trunk state for {test.target!r}"
                ),
                evidence=(str(test.observed_result), *test.evidence),
                confidence="medium" if report.observations_complete else "low",
                affected_resources=(switch,),
                proposed_repair=(
                    {
                        "operation_type": "ios_command",
                        "inputs": {
                            "device": switch,
                            "source": "switch_vlan_config",
                        },
                        "reason": "re-push the switch's compiled VLAN/trunk block",
                    },
                ),
                uncertainty=(
                    "the VLAN table read back empty or unparsed, so the missing "
                    "line is not known; the whole block is re-pushed instead"
                    if not report.observations_complete
                    else None
                ),
            )
        )

    # 5. DHCP clients that never took a lease (#29). Only proposed when the
    # server end is actually observed to be addressed: with no evidence about
    # the pool, re-pushing a client config is a guess and stays blocked.
    for test in report.tests:
        if test.level != "addressing" or test.verdict is not Verdict.FAIL:
            continue
        device_id, _, iface = test.target.partition(":")
        if not _is_dhcp_client(spec, device_id):
            continue
        if not _dhcp_server_observed(spec, state):
            diagnoses.append(
                Diagnosis(
                    diagnosis_id=new_id("diag"),
                    failed_tests=(test.test_id,),
                    root_cause=(
                        f"{device_id} has no address and no observed DHCP server "
                        "on its subnet, so the lease failure cannot be repaired "
                        "without evidence"
                    ),
                    evidence=(str(test.observed_result), *test.evidence),
                    confidence="unknown",
                    affected_resources=(device_id,),
                    proposed_repair=(),
                    uncertainty=(
                        "REPAIR BLOCKED: the DHCP pool's state was not observed. "
                        "Mutating a client config would not produce a lease."
                    ),
                )
            )
            continue
        diagnoses.append(
            Diagnosis(
                diagnosis_id=new_id("diag"),
                failed_tests=(test.test_id,),
                root_cause=f"{device_id} did not obtain a DHCP lease",
                evidence=(
                    str(test.observed_result),
                    *test.evidence,
                    "a DHCP server on the subnet is observed with an address",
                ),
                confidence="medium",
                affected_resources=(device_id,),
                proposed_repair=(
                    {
                        "operation_type": "reconfigure_address",
                        "inputs": _address_inputs(spec, device_id, iface),
                        "reason": "re-issue the client's DHCP request",
                    },
                ),
                uncertainty=(
                    None
                    if report.observations_complete
                    else "observation incomplete: the server's address may be missing"
                ),
            )
        )

    # 6. security failures on a firewall (#29). An ASA that has no nameif or
    # no interface address forwards nothing, and the repair is its own compiled
    # block - the same one the build pushes.
    for test in report.tests:
        if test.level not in ("structural", "addressing"):
            continue
        if test.verdict is not Verdict.FAIL:
            continue
        device_id = test.target.partition(":")[0]
        if not _is_firewall(spec, device_id):
            continue
        diagnoses.append(
            Diagnosis(
                diagnosis_id=new_id("diag"),
                failed_tests=(test.test_id,),
                root_cause=(
                    f"firewall {device_id!r} does not match its expected config "
                    f"({test.target!r}), so it is not enforcing its policy"
                ),
                evidence=(str(test.observed_result), *test.evidence),
                confidence="medium",
                affected_resources=(device_id,),
                proposed_repair=(
                    {
                        "operation_type": "ios_command",
                        "inputs": {
                            "device": device_id,
                            "source": "firewall_config",
                        },
                        "reason": "re-push the firewall's compiled ASA block",
                    },
                ),
                uncertainty=None,
            )
        )

    return diagnoses


def _ip_in_network(ip: str, network: str) -> bool:
    """Is `ip` inside `network` (CIDR)? Pure stdlib, no guessing."""
    import ipaddress

    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(network, strict=False)
    except ValueError:
        return False


def _route_commands(route) -> list[str]:
    """Render the spec's static route as IOS CLI, from the spec itself."""
    import ipaddress

    try:
        net = ipaddress.ip_network(route.destination, strict=False)
    except ValueError:
        return []
    commands = [f"ip route {net.network_address} {net.netmask}"]
    if route.next_hop:
        commands[-1] += f" {route.next_hop}"
    elif route.exit_interface:
        commands[-1] += f" {route.exit_interface}"
    else:
        return []
    return commands


def _role_of(spec: NetworkSpec, device_id: str) -> str:
    device = spec.device_by_id(device_id)
    return str(getattr(getattr(device, "role", None), "value", ""))


def _switch_for_l2_failure(spec: NetworkSpec, target: str) -> str | None:
    """Which switch a VLAN/trunk criterion is about.

    A `vlan_defined` target is a VLAN id, not a device, so the switch is the
    first one the spec makes a member of that VLAN; a `trunk_configured` target
    is a link id, whose endpoints are both switches. None means the criterion
    names something this spec does not describe, and no repair is proposed.
    """
    roles = {d.id: str(getattr(d.role, "value", d.role)) for d in spec.devices}
    link = next((l for l in spec.links if l.id == target), None)
    if link is not None:
        for endpoint in (link.a, link.b):
            if roles.get(endpoint.device) in ("switch", "l3_switch"):
                return endpoint.device
        return None
    try:
        vlan_id = int(str(target).strip())
    except ValueError:
        return None
    members = [
        s.id for s in spec.subnets if s.vlan_id == vlan_id
    ]
    for device in spec.devices:
        if roles.get(device.id) not in ("switch", "l3_switch"):
            continue
        if any(
            a.device == device.id and a.subnet in members
            for a in spec.ip_assignments
        ):
            return device.id
        if any(
            e.device == device.id and e.vlan == vlan_id
            for l in spec.links for e in (l.a, l.b)
        ):
            return device.id
    return None


def _is_dhcp_client(spec: NetworkSpec, device_id: str) -> bool:
    return any(
        a.device == device_id and a.uses_dhcp
        for a in spec.ip_assignments
    )


def _is_firewall(spec: NetworkSpec, device_id: str) -> bool:
    device = spec.device_by_id(device_id)
    role = str(getattr(getattr(device, "role", None), "value", ""))
    return role == "firewall"


def _dhcp_server_observed(spec: NetworkSpec, state: ActualNetworkState) -> bool:
    """Is any DHCP-serving interface actually observed with an address?

    Without this the client-side repair is a guess: the usual cause is a pool
    that was never configured, and re-pushing the client changes nothing.
    """
    servers = {
        a.device
        for a in spec.ip_assignments
        if a.uses_dhcp is False
        and spec.device_by_id(a.device) is not None
        and str(getattr(spec.device_by_id(a.device).role, "value", "")) in
        ("router", "firewall")
    }
    for name in servers:
        device = state.devices.get(name)
        if device is None:
            continue
        if any(p.ip.is_known and p.ip.value for p in device.ports.values()):
            return True
    return False

def _device_inputs(spec: NetworkSpec, device_id: str) -> dict[str, Any]:
    device = spec.device_by_id(device_id)
    role = device.role.value if device else "pc"
    model = device.model_hint if device else None
    return {"name": device_id, "role": role, "model": model or None}


def _link_inputs(spec: NetworkSpec, link_id: str) -> dict[str, Any]:
    link = next((l for l in spec.links if l.id == link_id), None)
    if link is None:
        return {"link_id": link_id}
    return {
        "link_id": link_id,
        "a_device": link.a.device,
        "a_port": link.a.interface_hint,
        "b_device": link.b.device,
        "b_port": link.b.interface_hint,
        "media": link.media,
    }


def _address_inputs(spec: NetworkSpec, device_id: str, iface: str) -> dict[str, Any]:
    # Inputs are completed by the repair engine, which has the AddressPlan.
    return {"device": device_id, "interface": iface, "needs_plan": True}
