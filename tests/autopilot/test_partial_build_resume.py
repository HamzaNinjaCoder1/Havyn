"""S1.14 / F11: partial build and resume.

One bad link must not zero the whole build: preflight failures scope to the
implicated ops (plus their dependents), everything else still builds, and the
report names the blocked part. `--resume <build-id>` re-runs only what the
prior build did not confirm.
"""

from __future__ import annotations

import copy

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.compiler import compile_spec, net_build
from src.network_autopilot.compiler.executor import DagExecutor
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.evidence.store import (
    EvidenceStore,
    completed_ops_from_evidence,
)
from src.network_autopilot.ipam import plan_addressing
from tests.autopilot.test_compiler import GOLDEN_SPEC


def _bad_link_spec() -> NetworkSpec:
    raw = copy.deepcopy(GOLDEN_SPEC)
    for link in raw["links"]:
        if link["id"] == "l-s2-pc4":
            link["b"] = {"device": "pc4", "interface_hint": "GigabitEthernet9/99"}
    return NetworkSpec.model_validate(raw)


def _executor(
    evidence: EvidenceStore | None = None, adapter: NullAdapter | None = None
) -> DagExecutor:
    return DagExecutor(
        adapter or NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=500)),
        evidence,
    )


def test_one_bad_link_blocks_only_its_branch():
    """RED before the fix: any preflight failure returns (None, error) and
    134/134 ops die. After: the bad link op is named, the rest still builds."""
    spec = _bad_link_spec()
    plan = plan_addressing(spec)
    result, error = net_build(spec, plan)

    assert error is None, error
    assert result is not None
    assert result.preflight is not None
    assert not result.preflight.can_execute
    assert "op-link-l-s2-pc4" in result.preflight_blocked_ops

    execution = _executor().execute(
        result.dag, pre_blocked=set(result.preflight_blocked_ops)
    )
    nodes = result.dag.nodes
    from src.network_autopilot.compiler.executor import OpStatus

    assert nodes["op-link-l-s2-pc4"].status is OpStatus.BLOCKED
    assert "op-link-l-s2-pc4" in execution.preflight_blocked
    # precisely the implicated op is blocked — nothing else. The golden DAG
    # declares no dependents on a link op (configs depend on the device op;
    # flows are probes that report the half-built truth), so the rest of the
    # network still builds: creates, links, addressing, VLANs, flows.
    assert execution.blocked == ["op-link-l-s2-pc4"], execution.blocked
    assert not execution.failed, execution.failed
    rest = [
        op_id for op_id in result.dag.nodes
        if op_id != "op-link-l-s2-pc4"
    ]
    assert rest, "a one-link failure must leave a remainder to build"
    assert all(
        nodes[op_id].status is OpStatus.SUCCEEDED for op_id in rest
    ), [
        (op_id, nodes[op_id].status.value)
        for op_id in rest
        if nodes[op_id].status is not OpStatus.SUCCEEDED
    ]
    # pc4's own address push is NOT a dependent of the cable: setting an IP
    # on an uncabled PC is valid work, and the flow verdict — not a blocked
    # config — is what reports the missing cable.
    assert nodes["op-host-pc4"].status is OpStatus.SUCCEEDED


class _BlindAdapter(NullAdapter):
    def list_device_names(self) -> Observable[list[str]]:
        return Observable.unknown("test: workspace unreadable")


def test_unscoped_preflight_failure_still_blocks_everything():
    """Pin: a failure that cannot be scoped to ops (unreadable workspace)
    keeps the old fail-closed behaviour — (None, error), nothing executes."""
    spec = NetworkSpec.model_validate(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    result, error = net_build(spec, plan, adapter=_BlindAdapter())

    assert result is None
    assert error and "workspace unreadable" in error


def test_resume_skips_confirmed_ops(tmp_path):
    """A second run over the same DAG reuses the first run's confirmed
    receipts instead of re-executing them."""
    spec = NetworkSpec.model_validate(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    evidence = EvidenceStore(root=tmp_path / "ev")
    # One shared adapter: resume reuses the prior run's workspace, exactly
    # like a live PT canvas that keeps what the first build created. A fresh
    # adapter would be an empty workspace where skipped creates leave links
    # with nothing to cable to.
    adapter = NullAdapter()

    first = _executor(evidence, adapter).execute(compile_spec(spec, plan).dag)
    assert not first.failed, first.failed

    completed = completed_ops_from_evidence(evidence.root, evidence.build_id)
    assert completed, "the first run must leave confirmed receipts to resume from"

    from src.network_autopilot.compiler.executor import OpStatus

    second = _executor(None, adapter).execute(
        compile_spec(spec, plan).dag, pre_completed=completed
    )
    assert not second.failed and not second.blocked
    assert sorted(second.resumed) == sorted(completed)
    dag2_nodes = second.dag.nodes
    assert all(
        dag2_nodes[op_id].status is OpStatus.SKIPPED for op_id in completed
    )


def test_resume_runs_only_the_remainder():
    """Partial prior work: listed ops are skipped, everything else executes."""
    spec = NetworkSpec.model_validate(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    dag = compile_spec(spec, plan).dag

    from src.network_autopilot.compiler.executor import OpStatus

    topo = list(dag.topo_order())
    done = {op_id: "build-prior" for op_id in topo if "create" in op_id}
    assert done, "expected create ops in the golden DAG"

    execution = _executor().execute(dag, pre_completed=done)
    assert not execution.failed
    assert sorted(execution.resumed) == sorted(done)
    ran = [
        op_id
        for op_id in topo
        if dag.nodes[op_id].status is OpStatus.SUCCEEDED
    ]
    assert ran, "the remainder must actually execute"
    assert not (set(ran) & set(done)), "resumed ops must not re-execute"


def _populated_adapter():
    """A NullAdapter whose workspace already holds a golden build."""
    spec = NetworkSpec.model_validate(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    adapter = NullAdapter()
    _executor(None, adapter).execute(compile_spec(spec, plan).dag)
    return spec, plan, adapter


def test_workspace_collision_stays_global():
    """Pin (S1.4): a dirty workspace refuses the whole build. Scoping must
    never bless building on top of possibly-foreign devices."""
    spec, plan, adapter = _populated_adapter()
    result, error = net_build(spec, plan, adapter=adapter)

    assert result is None
    assert error and "collision" in error


def test_resume_bypasses_the_collision_guard():
    """--resume is an explicit ownership claim: the prior build's devices
    are expected in the workspace, so the collision guard steps aside."""
    spec, plan, adapter = _populated_adapter()
    result, error = net_build(
        spec, plan, adapter=adapter, resume_from="build-prior"
    )

    assert error is None, error
    assert result is not None
