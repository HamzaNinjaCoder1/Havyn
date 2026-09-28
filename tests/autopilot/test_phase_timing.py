"""S1.5: per-phase timing in evidence and in the report.

"Pehle naapo, phir tez karo." Before this, a build produced one wall-clock
number (the CLI's TIME line) and nothing else, so nobody could say where
the 1349 seconds of the reference run went. These tests pin:

* the report carries `phase_timing` with a fixed key set (wall segments
  preflight/execute/verify/repair/total, plus the op families inside
  execute: create/cable/configure/flows/execute_other);
* the evidence stream has a `phase_timing` record;
* `human_text()` prints the table at the end of the report;
* family grouping comes from operation types, not from message sniffing -
  the unit test feeds synthetic receipts straight into the helper.

Every test fails before the feature: `build.phase_timing` does not exist.
"""

from __future__ import annotations

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.evidence.store import EvidenceStore
from src.network_autopilot.orchestrator import (
    Autopilot,
    _families_from_op_seconds,
    _OP_FAMILIES,
)
from tests.autopilot.test_compiler import GOLDEN_SPEC

WALL_AND_TOTAL = {"preflight", "execute", "verify", "repair", "total"}
FAMILIES = {"create", "cable", "configure", "flows", "execute_other"}


def test_families_group_op_seconds_by_operation_type():
    """The mapping is data, not prose: these exact types drive the table.

    (OperationReceipt.started_at/finished_at are stamped identically at
    construction, so receipt durations are always 0 - the executor's own
    clock is the only honest source for the breakdown.)
    """
    assert _OP_FAMILIES["create_device"] == "create"
    assert _OP_FAMILIES["install_module"] == "create"
    assert _OP_FAMILIES["install_wireless_radio"] == "create"
    assert _OP_FAMILIES["create_link"] == "cable"
    assert _OP_FAMILIES["associate_wireless"] == "cable"
    assert _OP_FAMILIES["configure_host"] == "configure"
    assert _OP_FAMILIES["configure_ios"] == "configure"
    assert _OP_FAMILIES["configure_gateway_lan"] == "configure"
    assert _OP_FAMILIES["verify_flow"] == "flows"
    assert _OP_FAMILIES["wait_dhcp"] == "flows"
    # read-backs and save are execution time but not one of the four
    assert _OP_FAMILIES["verify_state"] == "execute_other"
    assert _OP_FAMILIES["save_project"] == "execute_other"

    got = _families_from_op_seconds(
        {
            "create_device": 11.0,
            "create_link": 3.0,
            "configure_ios": 30.0,
            "configure_host": 4.5,
            "verify_flow": 2.0,
            "save_project": 1.0,
            "mystery_type": 7.0,
        }
    )

    assert got["create"] == 11.0
    assert got["cable"] == 3.0
    assert got["configure"] == 34.5
    assert got["flows"] == 2.0
    assert got["execute_other"] == 8.0
    assert FAMILIES <= set(got)


def test_golden_build_reports_every_phase(tmp_path):
    adapter = NullAdapter()
    evidence = EvidenceStore(root=tmp_path / "evidence")
    autopilot = Autopilot(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        evidence,
    )
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)

    build, error = autopilot.build(spec)

    assert error is None and build is not None
    timing = build.phase_timing
    assert WALL_AND_TOTAL <= set(timing), sorted(timing)
    assert FAMILIES <= set(timing), sorted(timing)
    for key, seconds in timing.items():
        assert seconds >= 0, (key, seconds)
    assert timing["total"] >= timing["execute"] >= 0

    # the evidence stream carries it for later comparison (S1.6 before/after)
    kinds = {r.kind for r in evidence.read_all()}
    assert "phase_timing" in kinds, kinds

    # the report prints the table at the end (after the OVERALL line)
    text = build.human_text()
    assert "Phase timing" in text
    assert "OVERALL" in text
    assert text.index("Phase timing") > text.index("OVERALL")
    assert "repair" in text

    # and the machine-readable form survives serialization
    assert build.to_dict()["phase_timing"] == timing


def test_repair_phase_is_measured_when_the_loop_runs(tmp_path):
    from tests.autopilot.test_orchestrator_e2e import _NoEffectConfigureHost

    adapter = _NoEffectConfigureHost()
    evidence = EvidenceStore(root=tmp_path / "evidence")
    autopilot = Autopilot(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        evidence,
    )
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)

    build, error = autopilot.build(spec)

    assert error is None and build is not None
    assert build.repairs, "this adapter is built to force the repair loop"
    assert "repair" in build.phase_timing
    assert build.phase_timing["repair"] >= 0


def test_create_time_comes_from_the_executor_clock_not_the_receipts(tmp_path):
    """The receipts' started_at/finished_at are written at construction and
    always equal (archived reference run: all 123 receipts are 0.000s), so a
    family breakdown read from them would report 0 for a build that really
    spent minutes there. This pins the executor's own clock: a create that
    sleeps must show up in the `create` family."""
    import time as _time

    adapter = NullAdapter()
    real_create = adapter.create_device

    def slow_create(*args, **kwargs):
        _time.sleep(0.35)
        return real_create(*args, **kwargs)

    adapter.create_device = slow_create
    evidence = EvidenceStore(root=tmp_path / "evidence")
    autopilot = Autopilot(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        evidence,
    )
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)

    build, error = autopilot.build(spec)

    assert error is None and build is not None
    assert build.execution.get("op_seconds"), "executor must expose op_seconds"
    assert build.phase_timing["create"] >= 0.3, (
        "a 0.35s create must not measure as 0"
    )
