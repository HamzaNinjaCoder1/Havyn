"""Per-criterion verification (§H).

Every acceptance criterion generates its own verification operation/
evidence chain. Instead of one broad final verification, each
criterion has its own verify operation, observation, PASS/FAIL/UNKNOWN,
and evidence — independently traceable.

UNKNOWN is NEVER converted to PASS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.ids import utc_now_iso, new_id
from ..core.network_spec import (
    AcceptanceCriterionSpec,
    ConfigTestSpec,
    FlowTestSpec,
    StructuralTestSpec,
    WirelessTestSpec,
)
from ..core.requirements import (
    AcceptanceResult,
    Verdict,
    FlowOutcome,
    FlowResult,
)
from ..core.actual_state import ActualNetworkState
# One verdict source (#21): structural/config/wireless verdicts are decided by
# the acceptance evaluator, and this module only wraps them in the per-criterion
# evidence chain. Two independent implementations of the same decision is how
# one spec ended up reporting PASS here and UNKNOWN there.
from ..acceptance.evaluator import evaluate_criterion, evaluate_wireless


class VerificationKind(str, Enum):
    STRUCTURAL = "structural"
    ADDRESSING = "addressing"
    CONNECTIVITY = "connectivity"
    CONFIG = "config"
    ROUTING = "routing"
    VLAN = "vlan"


@dataclass(frozen=True)
class CriterionVerification:
    """One per-criterion verification operation/evidence chain (§H)."""
    criterion_id: str
    requirement_ids: tuple[str, ...]
    test_kind: str
    verification_id: str
    kind: VerificationKind
    target: str
    expected: str
    observed: str | None
    verdict: Verdict
    evidence: tuple[str, ...]
    timestamp: str
    checked_by: str = "per-criterion-verifier"
    raw_output: str | None = None

    @property
    def is_traceable(self) -> bool:
        """Every criterion must be independently traceable."""
        return self.verification_id is not None and len(self.evidence) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "requirement_ids": list(self.requirement_ids),
            "test_kind": self.test_kind,
            "verification_id": self.verification_id,
            "kind": self.kind.value,
            "target": self.target,
            "expected": self.expected,
            "observed": self.observed,
            "verdict": self.verdict.value,
            "evidence": list(self.evidence),
            "timestamp": self.timestamp,
            "checked_by": self.checked_by,
            "raw_output": self.raw_output,
        }


@dataclass
class PerCriterionVerificationReport:
    """Complete per-criterion verification report."""
    verifications: tuple[CriterionVerification, ...]
    summary: dict[str, int]
    all_traceable: bool
    unknown_count: int
    fail_count: int
    pass_count: int

    @property
    def all_pass(self) -> bool:
        return self.pass_count > 0 and self.fail_count == 0 and self.unknown_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verifications": [v.to_dict() for v in self.verifications],
            "summary": self.summary,
            "all_traceable": self.all_traceable,
            "unknown_count": self.unknown_count,
            "fail_count": self.fail_count,
            "pass_count": self.pass_count,
        }


class PerCriterionVerifier:
    """Generate per-criterion verification operations."""

    def __init__(self, adapter=None):
        self.adapter = adapter

    def verify(
        self,
        spec,
        state: ActualNetworkState,
        flow_results: dict,
    ) -> PerCriterionVerificationReport:
        """Verify every acceptance criterion independently."""
        verifications: list[CriterionVerification] = []

        for criterion in spec.acceptance_criteria:
            v = self._verify_one(criterion, state, flow_results)
            verifications.append(v)

        summary: dict[str, int] = {v.value: 0 for v in Verdict}
        for v in verifications:
            summary[v.verdict.value] += 1

        all_traceable = all(v.is_traceable for v in verifications)
        unknown_count = summary.get("unknown", 0)
        fail_count = summary.get("fail", 0)
        pass_count = summary.get("pass", 0)

        return PerCriterionVerificationReport(
            verifications=tuple(verifications),
            summary=summary,
            all_traceable=all_traceable,
            unknown_count=unknown_count,
            fail_count=fail_count,
            pass_count=pass_count,
        )

    def _verify_one(
        self,
        criterion: AcceptanceCriterionSpec,
        state: ActualNetworkState,
        flow_results: dict,
    ) -> CriterionVerification:
        test = criterion.test
        now = utc_now_iso()
        vid = f"CV-{criterion.id}"

        if isinstance(test, FlowTestSpec):
            return self._verify_flow(criterion, test, flow_results, now, vid)
        if isinstance(test, StructuralTestSpec):
            return self._verify_structural(criterion, test, state, now, vid)
        if isinstance(test, ConfigTestSpec):
            return self._verify_config(criterion, test, state, now, vid)
        if isinstance(test, WirelessTestSpec):
            return self._verify_wireless(criterion, test, state, now, vid)

        return CriterionVerification(
            criterion_id=criterion.id,
            requirement_ids=tuple(criterion.requirement_ids),
            test_kind="unknown",
            verification_id=vid,
            kind=VerificationKind.STRUCTURAL,
            target=str(test),
            expected="verifiable",
            observed=None,
            verdict=Verdict.UNKNOWN,
            evidence=("unknown test kind",),
            timestamp=now,
        )

    def _verify_config(
        self,
        criterion: AcceptanceCriterionSpec,
        test: ConfigTestSpec,
        state: ActualNetworkState,
        now: str,
        vid: str,
    ) -> CriterionVerification:
        """Verify a `config` criterion (#18).

        `_verify_one` dispatched ConfigTestSpec here, but this method never
        existed - so any spec carrying a config criterion raised AttributeError
        out of `net_verify`, `net_diff` and `net_build`. `config` is a
        first-class kind of the published schema; it is evaluated now, by the
        same code the acceptance report uses.
        """
        acc = evaluate_criterion(
            criterion, state, {}, observations_complete=False,
            checked_by="per-criterion-verifier",
        )
        target = test.device
        if test.interface:
            target = f"{test.device}:{test.interface}"
        return CriterionVerification(
            criterion_id=criterion.id,
            requirement_ids=tuple(criterion.requirement_ids),
            test_kind="config",
            verification_id=vid,
            kind=VerificationKind.CONFIG,
            target=target,
            expected=f"{test.field} == {test.expected}",
            observed=acc.evidence[-1] if acc.evidence else None,
            verdict=acc.verdict,
            evidence=acc.evidence,
            timestamp=now,
        )

    def _verify_wireless(
        self,
        criterion: AcceptanceCriterionSpec,
        test,
        state: ActualNetworkState,
        now: str,
        vid: str,
    ) -> CriterionVerification:
        """Association is real when PT shows the antenna link, nothing else.

        The observation the state carries is the `Antenna` link on the
        client's radio: its presence and its peer device. A Wireless0 that
        merely exists, or an AP that merely exists, is not an association.
        The verdict itself comes from `evaluate_wireless` - the function the
        acceptance report also uses - so one spec cannot PASS here and read
        UNKNOWN there (#22).
        """
        verdict, observed, evidence = evaluate_wireless(test, state)
        return CriterionVerification(
            criterion_id=criterion.id,
            requirement_ids=tuple(criterion.requirement_ids),
            test_kind="wireless",
            verification_id=vid,
            kind=VerificationKind.CONNECTIVITY,
            target=f"{test.client} ))) {test.access_point}",
            expected=test.expect,
            observed=observed,
            verdict=verdict,
            evidence=evidence,
            timestamp=now,
        )

    def _verify_flow(
        self,
        criterion: AcceptanceCriterionSpec,
        test: FlowTestSpec,
        flow_results: dict,
        now: str,
        vid: str,
    ) -> CriterionVerification:
        key = (test.source, test.destination, test.transport, test.port)
        result = flow_results.get(key)
        if result is None:
            return CriterionVerification(
                criterion_id=criterion.id,
                requirement_ids=tuple(criterion.requirement_ids),
                test_kind="flow",
                verification_id=vid,
                kind=VerificationKind.CONNECTIVITY,
                target=f"{test.source}->{test.destination}",
                expected=test.expect,
                observed=None,
                verdict=Verdict.UNKNOWN,
                evidence=("no flow result recorded",),
                timestamp=now,
            )
        # NEVER convert UNKNOWN to PASS; partial is always FAIL for reachable
        verdict = _flow_verdict(test.expect, result.outcome)
        return CriterionVerification(
            criterion_id=criterion.id,
            requirement_ids=tuple(criterion.requirement_ids),
            test_kind="flow",
            verification_id=vid,
            kind=VerificationKind.CONNECTIVITY,
            target=f"{test.source}->{test.destination}",
            expected=test.expect,
            observed=result.outcome.value,
            verdict=verdict,
            evidence=tuple([result.detail or ""] + [f"{k}={v}" for k, v in result.evidence.items()]),
            timestamp=now,
            raw_output=str(result.evidence),
        )

    def _verify_structural(
        self,
        criterion: AcceptanceCriterionSpec,
        test: StructuralTestSpec,
        state: ActualNetworkState,
        now: str,
        vid: str,
    ) -> CriterionVerification:
        if test.check == "device_exists":
            dev = state.devices.get(test.target)
            if dev is not None:
                return CriterionVerification(
                    criterion_id=criterion.id,
                    requirement_ids=tuple(criterion.requirement_ids),
                    test_kind="structural",
                    verification_id=vid,
                    kind=VerificationKind.STRUCTURAL,
                    target=test.target,
                    expected="device present",
                    observed="present",
                    verdict=Verdict.PASS,
                    evidence=(f"device {test.target} observed",),
                    timestamp=now,
                )
            return CriterionVerification(
                criterion_id=criterion.id,
                requirement_ids=tuple(criterion.requirement_ids),
                test_kind="structural",
                verification_id=vid,
                kind=VerificationKind.STRUCTURAL,
                target=test.target,
                expected="device present",
                observed=None,
                verdict=Verdict.UNKNOWN if not state.devices else Verdict.FAIL,
                evidence=("device not observed",),
                timestamp=now,
            )

        if test.check == "link_exists":
            for link in state.links:
                if link.ref_id.is_known and link.ref_id.value == test.target:
                    return CriterionVerification(
                        criterion_id=criterion.id,
                        requirement_ids=tuple(criterion.requirement_ids),
                        test_kind="structural",
                        verification_id=vid,
                        kind=VerificationKind.STRUCTURAL,
                        target=test.target,
                        expected="link present",
                        observed="present",
                        verdict=Verdict.PASS,
                        evidence=("link observed",),
                        timestamp=now,
                    )
            return CriterionVerification(
                criterion_id=criterion.id,
                requirement_ids=tuple(criterion.requirement_ids),
                test_kind="structural",
                verification_id=vid,
                kind=VerificationKind.STRUCTURAL,
                target=test.target,
                expected="link present",
                observed=None,
                verdict=Verdict.UNKNOWN,
                evidence=("link not observed",),
                timestamp=now,
            )

        if test.check in ("ip_assigned", "gateway_assigned"):
            # #21: `detail` names the interface to inspect - the acceptance
            # path's reading of the field - and the verdict is the acceptance
            # verdict, not a second opinion that could contradict it.
            acc = evaluate_criterion(
                criterion, state, {}, observations_complete=False,
                checked_by="per-criterion-verifier",
            )
            return CriterionVerification(
                criterion_id=criterion.id,
                requirement_ids=tuple(criterion.requirement_ids),
                test_kind="config",
                verification_id=vid,
                kind=VerificationKind.ADDRESSING,
                target=test.target,
                expected=test.detail or "configured",
                observed=acc.evidence[-1] if acc.evidence else None,
                verdict=acc.verdict,
                evidence=acc.evidence,
                timestamp=now,
            )

        if test.check == "vlan_defined":
            for dev in state.devices.values():
                for port in dev.ports.values():
                    if port.vlan.is_known and str(port.vlan.value) == str(test.target):
                        return CriterionVerification(
                            criterion_id=criterion.id,
                            requirement_ids=tuple(criterion.requirement_ids),
                            test_kind="config",
                            verification_id=vid,
                            kind=VerificationKind.VLAN,
                            target=test.target,
                            expected="vlan present",
                            observed=f"vlan {test.target}",
                            verdict=Verdict.PASS,
                            evidence=(f"vlan {test.target} on {dev.name}:{port.name}",),
                            timestamp=now,
                        )
            return CriterionVerification(
                criterion_id=criterion.id,
                requirement_ids=tuple(criterion.requirement_ids),
                test_kind="config",
                verification_id=vid,
                kind=VerificationKind.VLAN,
                target=test.target,
                expected="vlan present",
                observed=None,
                verdict=Verdict.UNKNOWN,
                evidence=("vlan not observed",),
                timestamp=now,
            )

        if test.check == "trunk_configured":
            for dev in state.devices.values():
                for port in dev.ports.values():
                    if port.trunk.is_known and port.trunk.value:
                        return CriterionVerification(
                            criterion_id=criterion.id,
                            requirement_ids=tuple(criterion.requirement_ids),
                            test_kind="config",
                            verification_id=vid,
                            kind=VerificationKind.VLAN,
                            target=f"{dev.name}:{port.name}",
                            expected="trunk enabled",
                            observed="trunk enabled",
                            verdict=Verdict.PASS,
                            evidence=(f"trunk on {dev.name}:{port.name}",),
                            timestamp=now,
                        )
            return CriterionVerification(
                criterion_id=criterion.id,
                requirement_ids=tuple(criterion.requirement_ids),
                test_kind="config",
                verification_id=vid,
                kind=VerificationKind.VLAN,
                target=test.target,
                expected="trunk enabled",
                observed=None,
                verdict=Verdict.UNKNOWN,
                evidence=("trunk not observed",),
                timestamp=now,
            )

        return CriterionVerification(
            criterion_id=criterion.id,
            requirement_ids=tuple(criterion.requirement_ids),
            test_kind="unknown",
            verification_id=vid,
            kind=VerificationKind.STRUCTURAL,
            target=str(test),
            expected="verifiable",
            observed=None,
            verdict=Verdict.UNKNOWN,
            evidence=("unhandled test type",),
            timestamp=now,
        )


def _flow_verdict(expect: str, outcome: FlowOutcome) -> Verdict:
    """Verdict for a flow test (never converts UNKNOWN to PASS)."""
    if expect == "reachable":
        table = {
            FlowOutcome.REACHABLE: Verdict.PASS,
            FlowOutcome.PARTIAL: Verdict.FAIL,
            FlowOutcome.BLOCKED: Verdict.FAIL,
            FlowOutcome.ERROR: Verdict.FAIL,
            FlowOutcome.UNKNOWN: Verdict.UNKNOWN,
        }
        return table[outcome]
    table = {
        FlowOutcome.BLOCKED: Verdict.PASS,
        FlowOutcome.REACHABLE: Verdict.FAIL,
        FlowOutcome.PARTIAL: Verdict.FAIL,
        FlowOutcome.ERROR: Verdict.UNKNOWN,
        FlowOutcome.UNKNOWN: Verdict.UNKNOWN,
    }
    return table[outcome]