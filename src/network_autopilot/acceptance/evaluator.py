"""Acceptance-criterion evaluation: intended spec vs. observed state.

Rules this module enforces (the product's spine):

- A flow criterion PASSES only on an observed outcome; a missing flow result
  is UNKNOWN, never PASS.
- "Reachable" fails on PARTIAL: partial connectivity is not connectivity
  (the "CONECTIVIDAD OK with 75% loss" class of lie).
- Structural absence only becomes FAIL when the observation itself was
  complete; a partial observation honestly reports UNKNOWN.
- A policy the engine cannot yet enforce stays UNKNOWN — planned capability
  is not satisfied capability.
"""

from __future__ import annotations

from ..core.actual_state import ActualNetworkState
from ..core.ids import utc_now_iso
from ..core.network_spec import (
    AcceptanceCriterionSpec,
    ConfigTestSpec,
    FlowTestSpec,
    NetworkSpec,
    StructuralTestSpec,
    WirelessTestSpec,
)
from ..core.requirements import (
    AcceptanceReport,
    AcceptanceResult,
    FlowOutcome,
    FlowResult,
    RequirementResult,
    Verdict,
    aggregate_verdicts,
    summarize,
)

# detail marker used by Observable.from_raw for null/empty runtime reads
_EMPTY_READ = "runtime returned an empty value"

FlowKey = tuple[str, str, str, "int | None"]


def _flow_key(test: FlowTestSpec) -> FlowKey:
    return (test.source, test.destination, test.transport, test.port)


def resolve_device(state: ActualNetworkState, spec_id: str) -> str | None:
    """Map a spec device id to an observed device name.

    A direct name match counts only when it does not contradict a KNOWN
    ref_id; an ambiguous ref_id match (two devices claiming it) resolves to
    None — ambiguity is unknown, not a guess.
    """
    direct = state.devices.get(spec_id)
    if direct is not None and (not direct.ref_id.is_known or direct.ref_id.value == spec_id):
        return spec_id
    matches = [
        name
        for name, dev in state.devices.items()
        if dev.ref_id.is_known and dev.ref_id.value == spec_id
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def evaluate_criterion(
    criterion: AcceptanceCriterionSpec,
    state: ActualNetworkState,
    flow_results: dict[FlowKey, FlowResult],
    *,
    observations_complete: bool = False,
    checked_by: str = "acceptance-evaluator",
) -> AcceptanceResult:
    """Evaluate one criterion; never raises on missing evidence — reports UNKNOWN."""
    evidence: list[str] = []
    test = criterion.test

    if isinstance(test, FlowTestSpec):
        result = flow_results.get(_flow_key(test))
        if result is None:
            verdict = Verdict.UNKNOWN
            evidence.append(
                f"no flow result recorded for {test.source} -> {test.destination} "
                f"({test.transport}" + (f"/{test.port}" if test.port else "") + ")"
            )
        else:
            evidence.append(
                f"observed outcome: {result.outcome.value}"
                + (f" ({result.detail})" if result.detail else "")
            )
            verdict = _verdict_for_flow(test.expect, result.outcome, evidence)
    elif isinstance(test, StructuralTestSpec):
        verdict = _evaluate_structural(test, state, evidence, observations_complete)
    elif isinstance(test, ConfigTestSpec):
        verdict = _evaluate_config(test, state, evidence, observations_complete)
    elif isinstance(test, WirelessTestSpec):
        # #22: this dispatcher used to have no wireless arm, so a wireless
        # criterion fell into `else` and reported UNKNOWN however clear the
        # antenna link was. `evaluate_wireless` is the one implementation of
        # the association verdict, shared with the per-criterion verifier.
        verdict, _observed, wireless_evidence = evaluate_wireless(test, state)
        evidence.extend(wireless_evidence)
    else:  # pragma: no cover - discriminated union prevents this
        verdict = Verdict.UNKNOWN
        evidence.append("unknown test kind")

    return AcceptanceResult(
        criterion_id=criterion.id,
        requirement_ids=tuple(criterion.requirement_ids),
        verdict=verdict,
        evidence=tuple(evidence),
        checked_at=utc_now_iso(),
        checked_by=checked_by,
    )


def _verdict_for_flow(expect: str, outcome: FlowOutcome, evidence: list[str]) -> Verdict:
    if expect == "reachable":
        table = {
            FlowOutcome.REACHABLE: Verdict.PASS,
            FlowOutcome.PARTIAL: Verdict.FAIL,
            FlowOutcome.BLOCKED: Verdict.FAIL,
            FlowOutcome.ERROR: Verdict.FAIL,
            FlowOutcome.UNKNOWN: Verdict.UNKNOWN,
        }
        if outcome is FlowOutcome.PARTIAL:
            evidence.append("partial connectivity fails a 'reachable' expectation")
        return table[outcome]
    # expect == "blocked"
    table = {
        FlowOutcome.BLOCKED: Verdict.PASS,
        FlowOutcome.REACHABLE: Verdict.FAIL,
        FlowOutcome.PARTIAL: Verdict.FAIL,
        FlowOutcome.ERROR: Verdict.UNKNOWN,
        FlowOutcome.UNKNOWN: Verdict.UNKNOWN,
    }
    return table[outcome]


def _evaluate_structural(
    test: StructuralTestSpec,
    state: ActualNetworkState,
    evidence: list[str],
    observations_complete: bool,
) -> Verdict:
    if test.check == "device_exists":
        name = resolve_device(state, test.target)
        if name is None:
            evidence.append(
                "device not present in observed state"
                if observations_complete
                else "device not found and observation completeness unknown"
            )
            return Verdict.FAIL if observations_complete else Verdict.UNKNOWN
        evidence.append(f"device observed as {name!r}")
        return Verdict.PASS

    if test.check == "link_exists":
        for link in state.links:
            if link.ref_id.is_known and link.ref_id.value == test.target:
                evidence.append("link present in observed state")
                return Verdict.PASS
        evidence.append(
            "link not present in observed state"
            if observations_complete
            else "link not found and observation completeness unknown"
        )
        return Verdict.FAIL if observations_complete else Verdict.UNKNOWN

    if test.check in ("ip_assigned", "gateway_assigned"):
        field_name = "ip" if test.check == "ip_assigned" else "gateway"
        port, dev_name = _find_port(state, test.target, test.detail)
        if port is None or dev_name is None:
            evidence.append("target device/port not observed")
            return Verdict.UNKNOWN
        observed = getattr(port, field_name)
        if observed.is_known:
            evidence.append(f"{dev_name} {port.name} {field_name} = {observed.value}")
            return Verdict.PASS
        if observed.status.value == "unknown" and observed.detail == _EMPTY_READ:
            evidence.append(f"{dev_name} {port.name} has no {field_name} configured")
            return Verdict.FAIL
        evidence.append(f"{field_name} not determinable: {observed.detail or observed.status.value}")
        return Verdict.UNKNOWN

    if test.check == "vlan_defined":
        for dev in state.devices.values():
            for port in dev.ports.values():
                if port.vlan.is_known and str(port.vlan.value) == str(test.target):
                    evidence.append(f"vlan {test.target} seen on {dev.name} {port.name}")
                    return Verdict.PASS
        evidence.append(
            "vlan not present in observed state"
            if observations_complete
            else "vlan not found and observation completeness unknown"
        )
        return Verdict.FAIL if observations_complete else Verdict.UNKNOWN

    if test.check == "trunk_configured":
        port, dev_name = _find_port(state, test.target, test.detail)
        if port is None or dev_name is None:
            evidence.append("target device/port not observed")
            return Verdict.UNKNOWN
        if port.trunk.is_known:
            evidence.append(f"{dev_name} {port.name} trunk={port.trunk.value}")
            return Verdict.PASS if port.trunk.value else Verdict.FAIL
        evidence.append("trunk state not determinable")
        return Verdict.UNKNOWN

    return Verdict.UNKNOWN  # pragma: no cover - Literal guard


def _evaluate_config(
    test: ConfigTestSpec,
    state: ActualNetworkState,
    evidence: list[str],
    observations_complete: bool,
) -> Verdict:
    del observations_complete  # config presence itself decides the verdict
    port, dev_name = _find_port(state, test.device, test.interface)
    if port is None or dev_name is None:
        evidence.append("target device/port not observed")
        return Verdict.UNKNOWN
    observed = getattr(port, test.field)
    if observed.is_known:
        matches = str(observed.value) == test.expected
        evidence.append(
            f"{dev_name} {port.name} {test.field} = {observed.value!r} "
            f"(expected {test.expected!r})"
        )
        return Verdict.PASS if matches else Verdict.FAIL
    if observed.status.value == "unknown" and observed.detail == _EMPTY_READ:
        evidence.append(f"{dev_name} {port.name} {test.field} is not configured")
        return Verdict.FAIL
    evidence.append(f"{test.field} not determinable: {observed.detail or observed.status.value}")
    return Verdict.UNKNOWN


def evaluate_wireless(
    test: WirelessTestSpec, state: ActualNetworkState
) -> tuple[Verdict, str | None, tuple[str, ...]]:
    """Verdict, observed text and evidence for a wireless association criterion.

    Association is real only when PT shows an `Antenna` link on the client's
    radio to the named access point: a Wireless0 that merely exists, or an
    access point that merely exists, is not an association. One function, so
    `evaluate_spec` and `PerCriterionVerifier.verify` cannot disagree about
    the same observation (#22).
    """
    evidence: list[str] = []
    client = state.devices.get(test.client)
    if client is None:
        evidence.append("client absent from the observed workspace")
        return Verdict.FAIL, "client not observed", tuple(evidence)
    port = client.ports.get(test.interface)
    if port is None:
        evidence.append(
            f"{test.client} has no {test.interface} interface; a host "
            "without a wireless NIC cannot associate"
        )
        return Verdict.FAIL, f"{test.interface} not present", tuple(evidence)
    link_class = port.link_class
    peers = port.peers.value if port.peers.is_known else None
    if not link_class.is_known:
        evidence.append("PT did not report the link type on the radio")
        return Verdict.UNKNOWN, None, tuple(evidence)
    associated = link_class.value == "Antenna" and bool(peers)
    peer_names = [str(p) for p in (peers or [])]
    matches = associated and test.access_point in peer_names
    observed = (
        f"{test.interface} linked to {peer_names[0]} via {link_class.value}"
        if associated
        else f"{test.interface} has no antenna link"
    )
    evidence.extend(
        [
            f"antenna link class={link_class.value}",
            f"peers={peer_names}",
            f"port state={port.state.value if port.state.is_known else 'unknown'}",
        ]
    )
    passed = matches if test.expect == "associated" else not matches
    return Verdict.PASS if passed else Verdict.FAIL, observed, tuple(evidence)


def _find_port(
    state: ActualNetworkState, device_id: str, interface: str | None
) -> tuple[object | None, str | None]:
    name = resolve_device(state, device_id)
    if name is None:
        return None, None
    dev = state.devices.get(name)
    if dev is None:
        return None, None
    if interface:
        return dev.ports.get(interface), name
    # No interface specified: only unambiguous when the device has exactly one
    # observed port.
    if len(dev.ports) == 1:
        return next(iter(dev.ports.values())), name
    return None, name


def evaluate_spec(
    spec: NetworkSpec,
    state: ActualNetworkState,
    flow_results: dict[FlowKey, FlowResult],
    *,
    observations_complete: bool = False,
    checked_by: str = "acceptance-evaluator",
) -> AcceptanceReport:
    """Evaluate every criterion and aggregate per requirement."""
    results = [
        evaluate_criterion(
            criterion, state, flow_results,
            observations_complete=observations_complete, checked_by=checked_by,
        )
        for criterion in spec.acceptance_criteria
    ]

    requirement_results: list[RequirementResult] = []
    for requirement in spec.requirements:
        verdicts = [
            r.verdict for r in results if requirement.id in r.requirement_ids
        ]
        # A requirement with no criteria is a traceability gap: NOT_RUN, never
        # silently satisfied.
        verdict = aggregate_verdicts(verdicts) if verdicts else Verdict.NOT_RUN
        requirement_results.append(
            RequirementResult(
                requirement_id=requirement.id,
                verdict=verdict,
                criterion_ids=tuple(
                    r.criterion_id for r in results if requirement.id in r.requirement_ids
                ),
            )
        )

    satisfied: bool | None
    req_verdicts = [r.verdict for r in requirement_results]
    if not results or Verdict.FAIL in req_verdicts:
        satisfied = False if Verdict.FAIL in req_verdicts else None
    elif any(v is not Verdict.PASS for v in req_verdicts):
        satisfied = None  # UNKNOWN / NOT_RUN present: not provably satisfied
    else:
        satisfied = True

    return AcceptanceReport(
        results=tuple(results),
        requirement_results=tuple(requirement_results),
        summary=summarize(results),
        satisfied=satisfied,
    )
