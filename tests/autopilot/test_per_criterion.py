"""Per-criterion verification tests (§H).

Tests that every acceptance criterion generates its own
verification operation/evidence chain.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.verification.per_criterion import (
    CriterionVerification,
    PerCriterionVerifier,
    PerCriterionVerificationReport,
    VerificationKind,
)
from src.network_autopilot.core.network_spec import (
    NetworkSpec,
    DeviceSpec,
    DeviceRole,
    AcceptanceCriterionSpec,
    FlowTestSpec,
    StructuralTestSpec,
    ConfigTestSpec,
    RequirementSpec,
)
from src.network_autopilot.core.actual_state import ActualNetworkState, DeviceObservation, PortObservation
from src.network_autopilot.core.observability import Observable


def _make_spec() -> NetworkSpec:
    spec = NetworkSpec(
        name="per-criterion-test",
        requirements=[
            RequirementSpec(id="R-1", statement="PCs can ping each other")
        ],
        devices=[
            DeviceSpec(id="pc1", role=DeviceRole.PC),
            DeviceSpec(id="pc2", role=DeviceRole.PC),
        ],
        acceptance_criteria=[
            AcceptanceCriterionSpec(
                id="A-1",
                requirement_ids=["R-1"],
                test=FlowTestSpec(
                    kind="flow",
                    source="pc1",
                    destination="pc2",
                    expect="reachable",
                ),
            ),
            AcceptanceCriterionSpec(
                id="A-2",
                requirement_ids=["R-1"],
                test=StructuralTestSpec(
                    kind="structural",
                    check="device_exists",
                    target="pc1",
                ),
            ),
            AcceptanceCriterionSpec(
                id="A-3",
                requirement_ids=[],
                test=StructuralTestSpec(
                    kind="structural",
                    check="link_exists",
                    target="link1",
                ),
            ),
        ],
    )
    return spec


class TestPerCriterionVerifier:
    """Per-criterion verifier tests."""

    def test_verify_generates_criterion_verifications(self):
        spec = _make_spec()
        state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00")
        verifier = PerCriterionVerifier()
        report = verifier.verify(spec, state, {})
        assert isinstance(report, PerCriterionVerificationReport)
        assert len(report.verifications) == 3

    def test_each_verification_is_traceable(self):
        spec = _make_spec()
        state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00")
        verifier = PerCriterionVerifier()
        report = verifier.verify(spec, state, {})
        for v in report.verifications:
            assert v.is_traceable, f"{v.criterion_id} not traceable"

    def test_flow_criterion_unknown_without_result(self):
        spec = _make_spec()
        state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00")
        verifier = PerCriterionVerifier()
        report = verifier.verify(spec, state, {})
        # A-1 (flow) has no flow results -> UNKNOWN
        a1 = next(v for v in report.verifications if v.criterion_id == "A-1")
        assert a1.verdict.value == "unknown"

    def test_structural_criterion_unknown_without_observation(self):
        spec = _make_spec()
        state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00")
        verifier = PerCriterionVerifier()
        report = verifier.verify(spec, state, {})
        # A-2 (device_exists) has no device observed -> UNKNOWN
        a2 = next(v for v in report.verifications if v.criterion_id == "A-2")
        assert a2.verdict.value == "unknown"

    def test_summary_counts(self):
        spec = _make_spec()
        state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00")
        verifier = PerCriterionVerifier()
        report = verifier.verify(spec, state, {})
        assert isinstance(report.pass_count, int)
        assert isinstance(report.fail_count, int)
        assert isinstance(report.unknown_count, int)

    def test_criterion_verification_structure(self):
        spec = _make_spec()
        state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00")
        verifier = PerCriterionVerifier()
        report = verifier.verify(spec, state, {})
        for v in report.verifications:
            d = v.to_dict()
            assert "criterion_id" in d
            assert "verification_id" in d
            assert "verdict" in d
            assert "evidence" in d
            assert d["verification_id"].startswith("CV-")


class TestCriterionVerification:
    """CriterionVerification model tests."""

    def test_never_converts_unknown_to_pass(self):
        """The core invariant: UNKNOWN is never converted to PASS."""
        from src.network_autopilot.core.requirements import Verdict
        v = CriterionVerification(
            criterion_id="A-99",
            requirement_ids=(),
            test_kind="unknown",
            verification_id="CV-A-99",
            kind=VerificationKind.STRUCTURAL,
            target="unknown",
            expected="test",
            observed=None,
            verdict=Verdict.UNKNOWN,
            evidence=("no evidence",),
            timestamp="2026-01-01T00:00:00",
        )
        assert v.verdict.value == "unknown"

    def test_to_dict_roundtrip(self):
        from src.network_autopilot.core.requirements import Verdict
        v = CriterionVerification(
            criterion_id="A-1",
            requirement_ids=("R-1",),
            test_kind="flow",
            verification_id="CV-A-1",
            kind=VerificationKind.CONNECTIVITY,
            target="pc1->pc2",
            expected="reachable",
            observed="reachable",
            verdict=Verdict.PASS,
            evidence=("flow observed",),
            timestamp="2026-01-01T00:00:00",
        )
        d = v.to_dict()
        assert d["criterion_id"] == "A-1"
        assert d["verdict"] == "pass"
        assert len(d["evidence"]) == 1


class TestPerCriterionReport:
    """PerCriterionVerificationReport tests."""

    def test_all_pass_property(self):
        report = PerCriterionVerificationReport(
            verifications=(
                CriterionVerification(
                    criterion_id="A-1",
                    requirement_ids=(),
                    test_kind="flow",
                    verification_id="CV-A-1",
                    kind=VerificationKind.CONNECTIVITY,
                    target="t",
                    expected="reachable",
                    observed="reachable",
                    verdict="pass",
                    evidence=("ok",),
                    timestamp="2026-01-01T00:00:00",
                ),
            ),
            summary={"pass": 1, "fail": 0, "unknown": 0},
            all_traceable=True,
            unknown_count=0,
            fail_count=0,
            pass_count=1,
        )
        assert report.all_pass is True

    def test_all_pass_false_with_unknown(self):
        from src.network_autopilot.core.requirements import Verdict
        report = PerCriterionVerificationReport(
            verifications=(
                CriterionVerification(
                    criterion_id="A-1",
                    requirement_ids=(),
                    test_kind="flow",
                    verification_id="CV-A-1",
                    kind=VerificationKind.CONNECTIVITY,
                    target="t",
                    expected="reachable",
                    observed=None,
                    verdict=Verdict.UNKNOWN,
                    evidence=("no data",),
                    timestamp="2026-01-01T00:00:00",
                ),
            ),
            summary={"pass": 0, "fail": 0, "unknown": 1},
            all_traceable=True,
            unknown_count=1,
            fail_count=0,
            pass_count=0,
        )
        assert report.all_pass is False

# ---------------------------------------------------------------------------
# Bugs #18 (missing `_verify_config`) and #21 (two verdict sources)
# ---------------------------------------------------------------------------


def _state_with(port_name: str, ip_value: str) -> ActualNetworkState:
    state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00+00:00")
    dev = DeviceObservation(name="r1")
    port = PortObservation(name=port_name)
    port.ip = Observable.known(ip_value, source="test")
    port.state = Observable.known("up", source="test")
    dev.ports[port_name] = port
    state.devices["r1"] = dev
    return state


class TestConfigCriterion:
    """`config` is a first-class schema kind, so it must verify (#18)."""

    def test_config_criterion_is_evaluated_and_not_crashing(self):
        spec = NetworkSpec(
            name="cfg-criterion",
            devices=[DeviceSpec(id="r1", role=DeviceRole.ROUTER)],
            acceptance_criteria=[
                AcceptanceCriterionSpec(
                    id="A-cfg",
                    requirement_ids=[],
                    test=ConfigTestSpec(
                        kind="config",
                        device="r1",
                        interface="GigabitEthernet0/0",
                        field="ip",
                        expected="10.0.0.1",
                    ),
                )
            ],
        )
        report = PerCriterionVerifier().verify(
            spec, _state_with("GigabitEthernet0/0", "10.0.0.1"), {}
        )
        assert report.verifications[0].verdict is Verdict.PASS

    def test_config_criterion_fails_when_the_value_differs(self):
        spec = NetworkSpec(
            name="cfg-mismatch",
            devices=[DeviceSpec(id="r1", role=DeviceRole.ROUTER)],
            acceptance_criteria=[
                AcceptanceCriterionSpec(
                    id="A-cfg2",
                    requirement_ids=[],
                    test=ConfigTestSpec(
                        kind="config",
                        device="r1",
                        interface="GigabitEthernet0/0",
                        field="ip",
                        expected="10.0.0.9",
                    ),
                )
            ],
        )
        report = PerCriterionVerifier().verify(
            spec, _state_with("GigabitEthernet0/0", "10.0.0.1"), {}
        )
        assert report.verifications[0].verdict is Verdict.FAIL

    def test_config_criterion_unknown_when_the_interface_is_not_observed(self):
        spec = NetworkSpec(
            name="cfg-unknown",
            devices=[DeviceSpec(id="r1", role=DeviceRole.ROUTER)],
            acceptance_criteria=[
                AcceptanceCriterionSpec(
                    id="A-cfg3",
                    requirement_ids=[],
                    test=ConfigTestSpec(
                        kind="config",
                        device="r1",
                        interface="GigabitEthernet0/0",
                        field="ip",
                        expected="10.0.0.1",
                    ),
                )
            ],
        )
        state = ActualNetworkState(
            source="test", observed_at="2026-01-01T00:00:00+00:00"
        )
        state.devices["r1"] = DeviceObservation(name="r1")
        report = PerCriterionVerifier().verify(spec, state, {})
        assert report.verifications[0].verdict is Verdict.UNKNOWN


class TestSingleVerdictSource:
    """#21: `detail` means "interface" in both paths, and both must agree."""

    @staticmethod
    def _criterion(detail: str) -> AcceptanceCriterionSpec:
        return AcceptanceCriterionSpec(
            id="A-ip",
            requirement_ids=[],
            test=StructuralTestSpec(
                kind="structural",
                check="ip_assigned",
                target="r1",
                detail=detail,
            ),
        )

    def _verdicts(self, detail: str, state: ActualNetworkState):
        crit = self._criterion(detail)
        acceptance = evaluate_criterion(crit, state, {})
        spec = NetworkSpec(
            name="agree",
            devices=[DeviceSpec(id="r1", role=DeviceRole.ROUTER)],
            acceptance_criteria=[crit],
        )
        per_criterion = PerCriterionVerifier().verify(spec, state, {}).verifications[0]
        return acceptance.verdict, per_criterion.verdict

    def test_agreement_when_detail_names_an_interface(self):
        state = _state_with("GigabitEthernet0/0", "10.0.0.1")
        acceptance, per_criterion = self._verdicts("GigabitEthernet0/0", state)
        assert acceptance is Verdict.PASS
        assert per_criterion is acceptance

    def test_agreement_when_detail_names_a_value(self):
        state = _state_with("GigabitEthernet0/0", "10.0.0.1")
        acceptance, per_criterion = self._verdicts("10.0.0.1", state)
        assert per_criterion is acceptance

from src.network_autopilot.acceptance.evaluator import evaluate_criterion  # noqa: E402
from src.network_autopilot.core.requirements import Verdict  # noqa: E402
