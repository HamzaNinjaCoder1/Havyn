"""Offline end-to-end: Autopilot.build() on the NullAdapter is the same
pipeline the real bridge will drive — only the adapter differs.

On the NullAdapter the data plane cannot be observed, so connectivity verdicts
are UNKNOWN and the report must NOT claim overall success. That is the product
invariant (UNKNOWN is never promoted to PASS), not a test limitation.
"""

from __future__ import annotations

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.core.ids import utc_now_iso
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.receipts import OperationReceipt, OperationResult
from src.network_autopilot.core.requirements import Verdict
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.evidence.store import EvidenceStore
from src.network_autopilot.orchestrator import Autopilot
from tests.autopilot.test_compiler import GOLDEN_SPEC


def _autopilot(tmp_path, adapter=None):
    adapter = adapter or NullAdapter()
    evidence = EvidenceStore(root=tmp_path / "evidence")
    autopilot = Autopilot(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        evidence,
    )
    return autopilot, adapter, evidence


def test_e2e_golden_build_offline_reports_honest_unknown(tmp_path):
    autopilot, _adapter, evidence = _autopilot(tmp_path)
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)

    build, error = autopilot.build(spec)

    assert error is None
    assert build is not None
    assert build.spec_name == "two-routers-lab"
    assert build.project_file == "two-routers-lab.pkt"

    structural = build.verification.by_level("structural")
    assert structural and all(t.verdict is Verdict.PASS for t in structural)
    addressing = build.verification.by_level("addressing")
    assert addressing and all(t.verdict is Verdict.PASS for t in addressing)

    # the null adapter has no data plane: connectivity must stay UNKNOWN
    connectivity = build.verification.by_level("connectivity")
    assert connectivity and all(t.verdict is Verdict.UNKNOWN for t in connectivity)

    assert build.execution["failed"] == []
    assert build.execution["blocked"] == []
    assert build.diff["drift_count"] == 0

    # the invariant at report level: UNKNOWN is never promoted to success
    assert build.success is False
    text = build.human_text()
    assert "UNKNOWN" in text
    assert "NOT SUCCESS" in text

    # the full evidence chain exists on disk
    kinds = {r.kind for r in evidence.read_all()}
    for expected in (
        "build_request", "addressing_planned", "compiled", "execution_summary",
        "operation_receipt", "flow_result", "observation", "verification",
        "diff", "build_result",
    ):
        assert expected in kinds, kinds


def test_e2e_rebuild_reuses_instead_of_recreating(tmp_path):
    autopilot, adapter, _evidence = _autopilot(tmp_path)
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)

    first, err1 = autopilot.build(spec)
    assert err1 is None and first is not None
    assert first.reused == []

    device_count = len(adapter.list_device_names().value)
    second, err2 = autopilot.build(spec)
    assert err2 is None and second is not None
    assert second.execution["failed"] == []
    # 8 devices + 7 links reused; configs legitimately re-applied
    assert len(second.reused) == 15
    assert len(adapter.list_device_names().value) == device_count


class _NoEffectConfigureHost(NullAdapter):
    """Test double for a real failure mode: the command is accepted (receipt
    says success) but has no effect on the device. The receipt hides nothing
    from verification, which reads observed state — that is the point."""

    def configure_host(self, device, *, dhcp=False, ip=None, mask=None,
                       gateway=None, operation_id=None):
        return OperationReceipt(
            operation_id=operation_id or "op-noop",
            operation="configure_host",
            requested={"device": device, "ip": ip, "mask": mask, "gateway": gateway},
            result=OperationResult.SUCCESS,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            evidence={"accepted": True},
        )


def test_e2e_silent_config_failure_is_diagnosed_repaired_and_still_honest(tmp_path):
    adapter = _NoEffectConfigureHost()
    autopilot, _a, evidence = _autopilot(tmp_path, adapter=adapter)
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)

    build, error = autopilot.build(spec)

    assert error is None and build is not None
    # receipts said success, but verification reads observed state: FAIL
    pc_failures = [
        t for t in build.verification.by_level("addressing")
        if t.verdict is Verdict.FAIL
    ]
    assert pc_failures, "silent configure_host failure must be caught by verification"
    assert any("no ip configured" in t.observed_result for t in pc_failures)

    # diagnosis produced a repairable, evidence-backed cause and the repair ran
    attempted = [
        r for r in build.repairs
        if r["plan"]["operations"] and r["outcome"]["executed"]
    ]
    assert attempted, [r["plan"].get("blocked_reason") for r in build.repairs]

    # the repair did NOT magically fix a broken adapter: re-verification ran
    # and the final report stays honest
    assert build.success is False
    assert "NOT SUCCESS" in build.human_text()
    reverified = [r["re_verified"] for r in attempted if r["re_verified"]]
    assert reverified and all(s.get("fail", 0) > 0 for s in reverified)
    # the repair loop left evidence of the attempt
    kinds = {r.kind for r in evidence.read_all()}
    assert "repair_attempt" in kinds
