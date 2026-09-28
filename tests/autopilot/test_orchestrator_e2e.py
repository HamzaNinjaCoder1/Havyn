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
from src.network_autopilot.core.requirements import (
    AcceptanceReport,
    AcceptanceResult,
)
from src.network_autopilot.orchestrator import Autopilot, functional_layers
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


def test_e2e_rebuild_is_blocked_until_the_workspace_is_cleaned(tmp_path):
    """S1.4 changed cross-run rebuild semantics on purpose.

    The blueprint: "Same naam ke purane devices build ko bigaad dete hain" -
    a device that already answers to a spec name may be a leftover from
    another topology, so the build must refuse to start and name the
    colliding devices instead of silently reusing them. The old test here
    pinned the opposite (blind reuse); cleaning unblocks the build, which is
    the blueprint's done criterion. The executor's reuse path itself is
    still pinned by test_executor.py::test_second_run_reuses_instead_of_recreating,
    and repair (which compiles without a workspace read) still reuses live
    devices mid-run.
    """
    from scripts.pt_clean_workspace import clean
    from src.network_autopilot.core.build_errors import PreflightBlocked

    autopilot, adapter, _evidence = _autopilot(tmp_path)
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)

    first, err1 = autopilot.build(spec)
    assert err1 is None and first is not None
    assert first.reused == []

    device_count = len(adapter.list_device_names().value)
    assert device_count > 0

    second, err2 = autopilot.build(spec)
    assert second is None, "the second build must not start"
    assert isinstance(err2, PreflightBlocked), err2
    assert "workspace collision" in str(err2)
    for name in ("r1", "pc1"):
        assert name in str(err2), (name, err2)
    assert len(adapter.list_device_names().value) == device_count, (
        "a refused build must not touch the workspace"
    )

    report = clean(adapter)
    assert report.failed == {} and not report.read_failed

    third, err3 = autopilot.build(spec)
    assert err3 is None and third is not None
    assert third.execution["failed"] == []
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


def test_inspect_actually_runs_the_flow_probes(tmp_path):
    """Bug #19: `inspect()` handed `_verify_and_diff` an empty flow_results, so
    every connectivity criterion came back UNKNOWN with "no flow result
    recorded" no matter how healthy the workspace was - the drift check could
    never say the network forwards."""
    from src.network_autopilot.core.requirements import FlowOutcome, FlowResult

    class ReachableAdapter(NullAdapter):
        def run_flow_test(
            self, source, destination, *, transport="icmp", port=None,
            timeout=25.0, destination_ip=None,
        ):
            return FlowResult(
                source=source,
                destination=destination,
                transport=transport,
                port=port,
                outcome=FlowOutcome.REACHABLE,
                detail="stubbed reachability",
            )

    autopilot, _adapter, _evidence = _autopilot(tmp_path, ReachableAdapter())
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)

    report, _diff, error = autopilot.inspect(spec)

    assert error is None
    assert report is not None
    connectivity = report.by_level("connectivity")
    assert connectivity, "the golden spec has flow criteria"
    assert all(t.verdict is Verdict.PASS for t in connectivity), [
        (t.verdict, t.evidence) for t in connectivity
    ]


NO_FLOW_SPEC = {
    "name": "dry-run-lab",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "p1", "role": "pc"},
    ],
    "links": [
        {
            "id": "l1",
            "a": {"device": "r1", "interface_hint": "GigabitEthernet0/1"},
            "b": {"device": "p1", "interface_hint": "FastEthernet0"},
        }
    ],
    "subnets": [{"id": "net1", "cidr": "192.168.1.0/24"}],
    "ip_assignments": [
        {
            "device": "r1",
            "subnet": "net1",
            "ip": "192.168.1.1",
            "interface_hint": "GigabitEthernet0/1",
        },
        {"device": "p1", "subnet": "net1"},
    ],
}


def test_dry_run_reports_plan_ok_never_success(tmp_path):
    """Bug #28: DRY_RUN executes nothing, but every mutation receipt is merely
    NOT_EXECUTED - `failed` and `blocked` stay empty - so a spec whose reads
    happen to agree with the workspace reported "OVERALL: SUCCESS" for a run
    that changed nothing."""
    autopilot = Autopilot(
        NullAdapter(),
        SafetyGate(SafetyMode.DRY_RUN, MutationBudget(max_mutations=100)),
        EvidenceStore(root=tmp_path / "evidence"),
    )
    spec = NetworkSpec.model_validate(NO_FLOW_SPEC)

    build, error = autopilot.build(spec)

    assert error is None
    assert build is not None
    assert build.success is False
    text = build.human_text()
    assert "PLAN OK" in text
    assert "OVERALL:        SUCCESS" not in text


# ---------------------------------------------------------------------------
# Bug #23 - a structural PASS must not stand in for functional layers
# ---------------------------------------------------------------------------

_LAYER_SPEC_RAW = {
    "name": "layer-report",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "fw", "role": "firewall"},
        {"id": "pc1", "role": "pc"},
    ],
    "links": [
        {"id": "l1",
         "a": {"device": "r1", "interface_hint": "GigabitEthernet0/1"},
         "b": {"device": "pc1"}},
    ],
    "subnets": [{"id": "lan", "cidr": "10.0.0.0/24"}],
    "ip_assignments": [
        {"device": "r1", "subnet": "lan", "ip": "10.0.0.1",
         "interface_hint": "GigabitEthernet0/1"},
        {"device": "pc1", "subnet": "lan"},
    ],
    "acceptance_criteria": [
        {"id": "A-flow", "requirement_ids": [],
         "test": {"kind": "flow", "source": "pc1", "destination": "r1",
                  "expect": "reachable"}},
        {"id": "A-vlan", "requirement_ids": [],
         "test": {"kind": "structural", "check": "vlan_defined",
                  "target": "lan"}},
        {"id": "A-ip", "requirement_ids": [],
         "test": {"kind": "structural", "check": "ip_assigned",
                  "target": "r1:GigabitEthernet0/1"}},
        {"id": "A-fw", "requirement_ids": [],
         "test": {"kind": "config", "device": "fw", "field": "ip",
                  "expected": "10.0.0.1"}},
        {"id": "A-cable", "requirement_ids": [],
         "test": {"kind": "structural", "check": "device_exists",
                  "target": "pc1"}},
    ],
}


def test_functional_layers_group_the_criteria_and_keep_their_verdicts():
    """#23: `structural 87/87 PASS` says devices and cables exist. Each layer
    must be counted on its own so an unexercised layer says so."""
    spec = NetworkSpec.model_validate(_LAYER_SPEC_RAW)
    acceptance = AcceptanceReport(
        results=(
            AcceptanceResult("A-flow", (), Verdict.PASS),
            AcceptanceResult("A-vlan", (), Verdict.UNKNOWN),
            AcceptanceResult("A-ip", (), Verdict.PASS),
            AcceptanceResult("A-fw", (), Verdict.FAIL),
        ),
        requirement_results=(),
        summary={},
        satisfied=None,
    )
    layers = functional_layers(spec, acceptance)
    assert layers["data_plane"] == {
        "total": 1, "pass": 1, "fail": 0, "unknown": 0, "not_run": 0,
    }
    assert layers["l2"]["total"] == 1 and layers["l2"]["unknown"] == 1
    assert layers["l3"]["total"] == 1 and layers["l3"]["pass"] == 1
    assert layers["security"]["total"] == 1 and layers["security"]["fail"] == 1
    # device/link existence belongs to the structural headline, not a layer
    assert sum(c["total"] for c in layers.values()) == 4


def test_the_report_prints_each_layer_and_admits_an_unexercised_one(tmp_path):
    autopilot, _adapter, _evidence = _autopilot(tmp_path)
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)
    build, error = autopilot.build(spec)
    assert error is None and build is not None
    text = build.human_text()
    assert "Functional layers:" in text, text
    for label in ("L2 (VLAN/trunk):", "L3 (routing/DHCP):",
                  "Security (ASA):", "Data plane (flows):"):
        assert label in text, label
    # a layer with no criterion must say it was not exercised, not print 0/0 PASS
    assert "not exercised" in text, text
    assert "0/0 PASS" not in text, text
    assert build.to_dict()["functional_layers"] == build.layers
    assert build.layers["data_plane"]["total"] >= 1, build.layers
