"""The verification engine: multi-level evidence-backed verdicts.

Levels implemented in Phase 2:
- STRUCTURAL: devices exist (observed), links exist (adapter-observed)
- ADDRESSING: planned ip/mask/gateway vs observed port state
- CONNECTIVITY: required flows, actually executed by the adapter
- NEGATIVE flows: handled through the same acceptance path (expect=blocked)

Every verdict carries its evidence and timestamp. UNKNOWN is a first-class
verdict and is never converted to PASS (the Phase-1 acceptance evaluator
enforces the same invariant at requirement level).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..acceptance.evaluator import evaluate_spec
from ..adapter.base import PacketTracerAdapter
from ..core.actual_state import ActualNetworkState
from ..core.ids import utc_now_iso
from ..core.network_spec import NetworkSpec
from ..core.requirements import (
    AcceptanceReport,
    FlowOutcome,
    FlowResult,
    Verdict,
)
from ..ipam.planner import AddressPlan
from .per_criterion import PerCriterionVerifier, PerCriterionVerificationReport

_EMPTY_READ = "runtime returned an empty value"

FlowKey = tuple[str, str, str, "int | None"]


@dataclass(frozen=True)
class VerificationTest:
    """One evidence-backed verification record (the §18 contract)."""

    test_id: str
    level: str                     # structural | addressing | connectivity
    source_requirement: str | None
    source_acceptance_criterion: str | None
    source: str | None
    target: str
    expected_result: str
    observed_result: str | None
    evidence: tuple[str, ...]
    timestamp: str
    verdict: Verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "level": self.level,
            "source_requirement": self.source_requirement,
            "source_acceptance_criterion": self.source_acceptance_criterion,
            "source": self.source,
            "target": self.target,
            "expected_result": self.expected_result,
            "observed_result": self.observed_result,
            "evidence": list(self.evidence),
            "timestamp": self.timestamp,
            "verdict": self.verdict.value,
        }


@dataclass(frozen=True)
class VerificationReport:
    tests: tuple[VerificationTest, ...]
    summary: dict[str, int]
    acceptance: AcceptanceReport
    observations_complete: bool
    per_criterion: PerCriterionVerificationReport | None = None

    def by_level(self, level: str) -> tuple[VerificationTest, ...]:
        return tuple(t for t in self.tests if t.level == level)

    @property
    def passed(self) -> bool:
        """True only when every test passed — UNKNOWN or NOT_RUN blocks it."""
        return bool(self.tests) and all(t.verdict is Verdict.PASS for t in self.tests)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tests": [t.to_dict() for t in self.tests],
            "summary": dict(self.summary),
            "acceptance": {
                "results": [
                    {
                        "criterion_id": r.criterion_id,
                        "requirement_ids": list(r.requirement_ids),
                        "verdict": r.verdict.value,
                        "evidence": list(r.evidence),
                    }
                    for r in self.acceptance.results
                ],
                "requirement_results": [
                    {"requirement_id": r.requirement_id, "verdict": r.verdict.value}
                    for r in self.acceptance.requirement_results
                ],
                "summary": dict(self.acceptance.summary),
                "satisfied": self.acceptance.satisfied,
            },
            "observations_complete": self.observations_complete,
        }


class VerificationEngine:
    def __init__(self, adapter: PacketTracerAdapter):
        self.adapter = adapter

    def verify(
        self,
        spec: NetworkSpec,
        plan: AddressPlan,
        state: ActualNetworkState,
        flow_results: dict[tuple[str, str], FlowResult],
        *,
        link_endpoints: dict[str, tuple[str, str, str, str]] | None = None,
        observations_complete: bool = False,
    ) -> VerificationReport:
        """Run all levels and assemble the report.

        `link_endpoints` maps link_id -> (a_dev, a_port, b_dev, b_port); the
        executor derives it from the compiled DAG.
        """
        now = utc_now_iso()
        tests: list[VerificationTest] = []
        tests.extend(self._structural_tests(spec, state, now, observations_complete))
        tests.extend(
            self._link_tests(spec, link_endpoints or {}, now, observations_complete)
        )
        tests.extend(self._addressing_tests(spec, plan, state, now, observations_complete))
        tests.extend(
            self._connectivity_tests(spec, flow_results, now, observations_complete)
        )

        summary: dict[str, int] = {v.value: 0 for v in Verdict}
        for test in tests:
            summary[test.verdict.value] += 1

        keyed_flows: dict[FlowKey, FlowResult] = {}
        for criterion in spec.acceptance_criteria:
            test = criterion.test
            if getattr(test, "kind", "") != "flow":
                continue
            result = flow_results.get((test.source, test.destination))
            if result is not None:
                keyed_flows[(test.source, test.destination, test.transport, test.port)] = result

        # Per-criterion verification (§H)
        per_criterion = PerCriterionVerifier()
        pc_report = per_criterion.verify(spec, state, keyed_flows)

        acceptance = evaluate_spec(
            spec, state, keyed_flows, observations_complete=observations_complete
        )

        return VerificationReport(
            tests=tuple(tests),
            summary=summary,
            acceptance=acceptance,
            observations_complete=observations_complete,
            per_criterion=pc_report,
        )

    # -- levels ---------------------------------------------------------------

    def _structural_tests(
        self,
        spec: NetworkSpec,
        state: ActualNetworkState,
        now: str,
        complete: bool,
    ) -> list[VerificationTest]:
        tests: list[VerificationTest] = []
        for device in spec.devices:
            observed = state.devices.get(device.id)
            if observed is not None:
                evidence = [f"device observed in workspace state"]
                if observed.model.is_known:
                    evidence.append(f"model={observed.model.value}")
                tests.append(
                    VerificationTest(
                        test_id=f"VT-struct-device-{device.id}",
                        level="structural",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=device.id,
                        target=device.id,
                        expected_result="device present",
                        observed_result="present",
                        evidence=tuple(evidence),
                        timestamp=now,
                        verdict=Verdict.PASS,
                    )
                )
            elif complete:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-struct-device-{device.id}",
                        level="structural",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=device.id,
                        target=device.id,
                        expected_result="device present",
                        observed_result="absent from observed state",
                        evidence=("observation was complete; device not seen",),
                        timestamp=now,
                        verdict=Verdict.FAIL,
                    )
                )
            else:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-struct-device-{device.id}",
                        level="structural",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=device.id,
                        target=device.id,
                        expected_result="device present",
                        observed_result=None,
                        evidence=("device not in observed state; observation completeness unknown",),
                        timestamp=now,
                        verdict=Verdict.UNKNOWN,
                    )
                )
        return tests

    def _link_tests(
        self,
        spec: NetworkSpec,
        link_endpoints: dict[str, tuple[str, str, str, str]],
        now: str,
        complete: bool,
    ) -> list[VerificationTest]:
        tests: list[VerificationTest] = []
        for link in spec.links:
            endpoints = link_endpoints.get(link.id)
            if endpoints is None:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-struct-link-{link.id}",
                        level="structural",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=None,
                        target=link.id,
                        expected_result="link present",
                        observed_result=None,
                        evidence=("no compiled endpoint data for this link",),
                        timestamp=now,
                        verdict=Verdict.UNKNOWN,
                    )
                )
                continue
            a_dev, a_port, b_dev, b_port = endpoints
            exists = self.adapter.link_exists(a_dev, a_port, b_dev, b_port)
            if exists.is_known and exists.value:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-struct-link-{link.id}",
                        level="structural",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=None,
                        target=link.id,
                        expected_result="link present",
                        observed_result=f"{a_dev}:{a_port} <-> {b_dev}:{b_port}",
                        evidence=("both ports report an attached link",),
                        timestamp=now,
                        verdict=Verdict.PASS,
                    )
                )
            elif exists.is_known and not exists.value:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-struct-link-{link.id}",
                        level="structural",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=None,
                        target=link.id,
                        expected_result="link present",
                        observed_result="no link on one or both ports",
                        evidence=(f"adapter checked {a_dev}:{a_port} and {b_dev}:{b_port}",),
                        timestamp=now,
                        verdict=Verdict.FAIL,
                    )
                )
            else:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-struct-link-{link.id}",
                        level="structural",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=None,
                        target=link.id,
                        expected_result="link present",
                        observed_result=None,
                        evidence=(exists.detail or exists.status.value,),
                        timestamp=now,
                        verdict=Verdict.UNKNOWN,
                    )
                )
        return tests

    def _addressing_tests(
        self,
        spec: NetworkSpec,
        plan: AddressPlan,
        state: ActualNetworkState,
        now: str,
        complete: bool,
    ) -> list[VerificationTest]:
        tests: list[VerificationTest] = []
        for assignment in plan.assignments:
            if assignment.ip is None:
                continue  # DHCP clients: addressing verified via flows, not read-back
            allocation = plan.allocation_by_subnet(assignment.subnet_id)
            iface = assignment.interface_hint or "FastEthernet0"
            device = state.devices.get(assignment.device)
            expected = f"{assignment.ip}/{allocation.netmask}" if allocation else assignment.ip
            if device is None:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-addr-{assignment.device}-{iface}",
                        level="addressing",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=assignment.device,
                        target=f"{assignment.device}:{iface}",
                        expected_result=expected,
                        observed_result=None,
                        evidence=("device not observed",),
                        timestamp=now,
                        verdict=Verdict.FAIL if complete else Verdict.UNKNOWN,
                    )
                )
                continue
            port = device.ports.get(iface)
            if port is None:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-addr-{assignment.device}-{iface}",
                        level="addressing",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=assignment.device,
                        target=f"{assignment.device}:{iface}",
                        expected_result=expected,
                        observed_result=None,
                        evidence=(
                            f"interface {iface} not in observed ports "
                            f"({sorted(device.ports) or 'none'})",
                        ),
                        timestamp=now,
                        verdict=Verdict.UNKNOWN,
                    )
                )
                continue
            if port.ip.is_known and port.mask.is_known:
                observed = f"{port.ip.value}/{port.mask.value}"
                matches = (
                    port.ip.value == assignment.ip
                    and (allocation is None or port.mask.value == allocation.netmask)
                )
                tests.append(
                    VerificationTest(
                        test_id=f"VT-addr-{assignment.device}-{iface}",
                        level="addressing",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=assignment.device,
                        target=f"{assignment.device}:{iface}",
                        expected_result=expected,
                        observed_result=observed,
                        evidence=("ip and mask read back from the device",),
                        timestamp=now,
                        verdict=Verdict.PASS if matches else Verdict.FAIL,
                    )
                )
            elif port.ip.status.value == "unknown" and port.ip.detail == _EMPTY_READ:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-addr-{assignment.device}-{iface}",
                        level="addressing",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=assignment.device,
                        target=f"{assignment.device}:{iface}",
                        expected_result=expected,
                        observed_result="no ip configured",
                        evidence=("runtime reported an empty address",),
                        timestamp=now,
                        verdict=Verdict.FAIL,
                    )
                )
            else:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-addr-{assignment.device}-{iface}",
                        level="addressing",
                        source_requirement=None,
                        source_acceptance_criterion=None,
                        source=assignment.device,
                        target=f"{assignment.device}:{iface}",
                        expected_result=expected,
                        observed_result=None,
                        evidence=(port.ip.detail or port.ip.status.value,),
                        timestamp=now,
                        verdict=Verdict.UNKNOWN,
                    )
                )
        return tests

    def _connectivity_tests(
        self,
        spec: NetworkSpec,
        flow_results: dict[tuple[str, str], FlowResult],
        now: str,
        complete: bool,
    ) -> list[VerificationTest]:
        tests: list[VerificationTest] = []
        for criterion in spec.acceptance_criteria:
            test = criterion.test
            if getattr(test, "kind", "") != "flow":
                continue
            requirement = criterion.requirement_ids[0] if criterion.requirement_ids else None
            flow = flow_results.get((test.source, test.destination))
            if flow is None:
                tests.append(
                    VerificationTest(
                        test_id=f"VT-flow-{criterion.id}",
                        level="connectivity",
                        source_requirement=requirement,
                        source_acceptance_criterion=criterion.id,
                        source=test.source,
                        target=test.destination,
                        expected_result=f"{test.expect} ({test.transport})",
                        observed_result=None,
                        evidence=("no flow result recorded",),
                        timestamp=now,
                        verdict=Verdict.UNKNOWN,
                    )
                )
                continue
            verdict = _flow_verdict(test.expect, flow.outcome)
            tests.append(
                VerificationTest(
                    test_id=f"VT-flow-{criterion.id}",
                    level="connectivity",
                    source_requirement=requirement,
                    source_acceptance_criterion=criterion.id,
                    source=test.source,
                    target=test.destination,
                    expected_result=f"{test.expect} ({test.transport})",
                    observed_result=flow.outcome.value,
                    evidence=tuple(
                        [flow.detail]
                        + [f"{k}={v}" for k, v in flow.evidence.items()]
                    ),
                    timestamp=now,
                    verdict=verdict,
                )
            )
        return tests


def _flow_verdict(expect: str, outcome: FlowOutcome) -> Verdict:
    if expect == "reachable":
        return {
            FlowOutcome.REACHABLE: Verdict.PASS,
            FlowOutcome.PARTIAL: Verdict.FAIL,
            FlowOutcome.BLOCKED: Verdict.FAIL,
            FlowOutcome.ERROR: Verdict.FAIL,
            FlowOutcome.UNKNOWN: Verdict.UNKNOWN,
        }[outcome]
    return {
        FlowOutcome.BLOCKED: Verdict.PASS,
        FlowOutcome.REACHABLE: Verdict.FAIL,
        FlowOutcome.PARTIAL: Verdict.FAIL,
        FlowOutcome.ERROR: Verdict.UNKNOWN,
        FlowOutcome.UNKNOWN: Verdict.UNKNOWN,
    }[outcome]
