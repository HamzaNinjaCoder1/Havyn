"""Diagnosis + bounded repair: evidence-backed causes, budgets, no faking."""

from __future__ import annotations

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.diagnosis import diagnose
from src.network_autopilot.ipam import plan_addressing
from src.network_autopilot.repair import RepairEngine
from src.network_autopilot.verification import VerificationEngine
from tests.autopilot.test_verification_diff import _observe_full, _golden
from src.network_autopilot.core.network_spec import NetworkSpec


def _diagnose_clean():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    _execution, state = _observe_full(adapter, spec, dag)
    report = VerificationEngine(adapter).verify(
        spec, plan, state, _execution.flow_results,
        link_endpoints=_endpoints(dag), observations_complete=True,
    )
    return spec, plan, adapter, report, state


def _endpoints(dag):
    out = {}
    for node in dag.nodes.values():
        if node.operation.operation_type == "create_link":
            i = node.operation.inputs
            out[i["link_id"]] = (i["a_device"], i["a_port"], i["b_device"], i["b_port"])
    return out


def test_clean_build_has_no_repairable_diagnoses():
    spec, plan, adapter, report, state = _diagnose_clean()
    diagnoses = diagnose(spec, report, state)
    # only connectivity UNKNOWNs (no data plane) -> no repairable causes
    assert all(not d.repairable for d in diagnoses)


def test_missing_device_diagnosed_with_create_repair():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    # build everything EXCEPT pc4
    from src.network_autopilot.compiler.executor import DagExecutor
    from src.network_autopilot.compiler.dag import OpStatus

    executor = DagExecutor(adapter, SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)))
    for op_id in list(dag.topo_order()):
        node = dag.nodes[op_id]
        if node.operation.operation_id == "op-create-pc4":
            node.status = OpStatus.SKIPPED  # pretend it was never created
            continue
    execution = executor.execute(dag)
    # pc4 creation was skipped -> dependents blocked; force-fake not needed.
    # Now diagnose on the observed state.
    state = executor.state
    state.merge(adapter.observe_state([d.id for d in spec.devices]))
    report = VerificationEngine(adapter).verify(
        spec, plan, state, {},
        link_endpoints=_endpoints(dag), observations_complete=True,
    )
    diagnoses = diagnose(spec, report, state)
    pc4_diags = [d for d in diagnoses if "pc4" in d.affected_resources and d.repairable]
    assert pc4_diags, [d.to_dict() for d in diagnoses]
    d = pc4_diags[0]
    assert d.confidence == "high"
    assert d.proposed_repair[0]["operation_type"] == "create_device"


def test_repair_executes_and_budget_blocks_further_attempts():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    _execution, state = _observe_full(adapter, spec, dag)
    report = VerificationEngine(adapter).verify(
        spec, plan, state, {}, link_endpoints=_endpoints(dag), observations_complete=True,
    )
    # a clean build yields no repairable diagnoses (see
    # test_clean_build_has_no_repairable_diagnoses), so this test drives the
    # RepairEngine with a synthetic evidence-backed diagnosis instead.
    engine = RepairEngine(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        max_attempts=1,
    )
    # Simulate a repairable diagnosis: device absent
    from src.network_autopilot.diagnosis.engine import Diagnosis

    fake = Diagnosis(
        diagnosis_id="diag-test",
        failed_tests=("VT-struct-device-pc9",),
        root_cause="device 'pc9' is absent from the workspace",
        evidence=("observation complete; not seen",),
        confidence="high",
        affected_resources=("pc9",),
        proposed_repair=(
            {"operation_type": "create_device", "inputs": {"name": "pc9", "role": "pc"}, "reason": "missing"},
        ),
        uncertainty=None,
    )
    repair_plan = engine.plan_repair(fake, plan)
    assert not repair_plan.blocked
    outcome = engine.execute(repair_plan)
    assert outcome.executed
    assert adapter.device_exists("pc9").value is True  # really created
    # budget: only 1 attempt allowed
    outcome2 = engine.execute(repair_plan)
    assert not outcome2.executed
    assert "budget" in (outcome2.blocked_reason or "")


def test_repair_blocked_without_evidence():
    from src.network_autopilot.diagnosis.engine import Diagnosis

    adapter = NullAdapter()
    engine = RepairEngine(adapter, SafetyGate(SafetyMode.AUTONOMOUS))
    no_evidence = Diagnosis(
        diagnosis_id="diag-weak",
        failed_tests=("VT-flow-A-1",),
        root_cause="connectivity failed but no state anomaly was observed",
        evidence=("flow observed: blocked",),
        confidence="unknown",
        affected_resources=("pc1", "pc4"),
        proposed_repair=(),
        uncertainty="no evidence-backed root cause",
    )
    repair_plan = engine.plan_repair(no_evidence)
    assert repair_plan.blocked
    assert "REPAIR BLOCKED" in repair_plan.blocked_reason
    outcome = engine.execute(repair_plan)
    assert not outcome.executed  # nothing was mutated


def test_repair_via_safety_gate_read_only_is_blocked():
    from src.network_autopilot.diagnosis.engine import Diagnosis

    adapter = NullAdapter()
    engine = RepairEngine(adapter, SafetyGate(SafetyMode.READ_ONLY))
    diag = Diagnosis(
        diagnosis_id="diag-x",
        failed_tests=("VT-struct-device-pc9",),
        root_cause="device 'pc9' absent",
        evidence=(),
        confidence="high",
        affected_resources=("pc9",),
        proposed_repair=(
            {"operation_type": "create_device", "inputs": {"name": "pc9", "role": "pc"}, "reason": "missing"},
        ),
        uncertainty=None,
    )
    repair_plan = engine.plan_repair(diag)
    outcome = engine.execute(repair_plan)
    # the gate refused every mutation -> the repair did NOT execute
    assert not outcome.executed
    assert adapter.device_exists("pc9").value is False
