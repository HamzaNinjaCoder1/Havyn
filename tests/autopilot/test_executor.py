"""DAG executor: golden offline pipeline, idempotency, safety, propagation."""

from __future__ import annotations

import pytest

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler.dag import OpStatus
from src.network_autopilot.compiler.executor import DagExecutor
from src.network_autopilot.core.receipts import CheckResult, OperationResult
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.evidence import EvidenceStore
from src.network_autopilot.ipam import plan_addressing
from src.network_autopilot.core.observability import Observable
from tests.autopilot.test_compiler import GOLDEN_SPEC, _DHCP_GATE_RAW

from src.network_autopilot.core.network_spec import NetworkSpec


def _golden_dag():
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)
    plan = plan_addressing(spec)
    return spec, plan, compile_spec(spec, plan).dag


# ---------------------------------------------------------------------------
# Bug #26 - the convergence gate is a real operation, not a comment
# ---------------------------------------------------------------------------

class _LeaseWithheldAdapter(NullAdapter):
    """A host whose DHCP answer has deliberately not arrived yet.

    The name is not "null": the executor short-circuits the wait for the
    structural double, and this stub must go the long way round.
    """

    def __init__(self, leases_after: int) -> None:
        super().__init__()
        self.name = "lease-stub"
        self._leases_after = leases_after
        self.gate_polls = 0

    def observe_state(self, names=None, *, observed_at=None):
        if names == ["pc1"]:
            self.gate_polls += 1
        state = super().observe_state(names, observed_at=observed_at)
        target = state.devices.get("pc1")
        if self.gate_polls < self._leases_after:
            for observation in state.devices.values():
                for port in observation.ports.values():
                    port.ip = Observable.not_observed()
        elif target is not None:
            for port in target.ports.values():
                port.ip = Observable.known("10.0.0.50", source="stub-lease")
        return state


def test_dhcp_convergence_gate_executes_as_a_known_operation(tmp_path):
    """Without a handler the executor rejects the whole DAG with "unknown
    operation type", so this fails before the gate exists."""
    spec = NetworkSpec.model_validate(_DHCP_GATE_RAW)
    plan = plan_addressing(spec)
    dag = compile_spec(spec, plan).dag
    assert "op-wait-dhcp" in dag.nodes
    executor = DagExecutor(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(tmp_path),
    )
    result = executor.execute(dag)
    assert not result.failed, result.failed
    assert dag.nodes["op-wait-dhcp"].status is OpStatus.SUCCEEDED


def test_dhcp_gate_polls_until_the_lease_lands(tmp_path):
    """The gate must keep asking until the address shows up - one read that
    came back empty is not convergence."""
    spec = NetworkSpec.model_validate(_DHCP_GATE_RAW)
    plan = plan_addressing(spec)
    dag = compile_spec(spec, plan).dag
    dag.nodes["op-wait-dhcp"].operation.inputs["timeout_s"] = 5
    adapter = _LeaseWithheldAdapter(leases_after=3)
    executor = DagExecutor(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(tmp_path),
    )
    result = executor.execute(dag)
    assert not result.failed, result.failed
    assert adapter.gate_polls == 3, adapter.gate_polls
    assert dag.nodes["op-wait-dhcp"].status is OpStatus.SUCCEEDED
    assert "have an address" in dag.nodes["op-wait-dhcp"].note


def test_golden_pipeline_executes_end_to_end(tmp_path):
    spec, _plan, dag = _golden_dag()
    evidence = EvidenceStore(tmp_path)
    executor = DagExecutor(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        evidence,
    )
    result = executor.execute(dag)
    summary = dag.summary()
    assert summary["succeeded"] == len(dag.nodes), dag.to_dict()
    assert not result.failed and not result.blocked
    # devices actually exist in the (structural) workspace
    for device_id in ("r1", "r2", "s1", "s2", "pc1", "pc2", "pc3", "pc4"):
        assert executor.adapter.device_exists(device_id).value is True
    # observation happened and models are known
    assert executor.state.devices["r1"].model.value == "2911"
    # flow verification ran (NullAdapter has no data plane -> UNKNOWN)
    assert ("pc1", "pc4") in result.flow_results
    assert result.flow_results[("pc1", "pc4")].outcome.value == "unknown"
    # evidence chain recorded
    kinds = {r.kind for r in evidence.read_all()}
    assert "operation_receipt" in kinds
    assert "observation" in kinds
    assert "flow_result" in kinds


def test_second_run_reuses_instead_of_recreating(tmp_path):
    spec, plan, _dag = _golden_dag()
    adapter = NullAdapter()
    gate = SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=500))
    first = DagExecutor(adapter, gate).execute(compile_spec(spec, plan).dag)
    assert not first.failed
    # a re-run recompiles the same spec (deterministic -> same op ids) and
    # must reuse existing resources instead of recreating anything
    second = DagExecutor(adapter, gate).execute(compile_spec(spec, plan).dag)
    assert not second.failed and not second.blocked
    # 8 devices + 7 links are reusable existence-checks...
    assert len(second.reused) == 15
    # ...while configs/save re-apply (idempotent re-configuration) and the
    # two observe ops re-run. Nothing is destroyed or recreated.
    for op_id, node in second.dag.nodes.items():
        kind = node.operation.operation_type
        if kind in ("create_device", "create_link"):
            assert node.status is OpStatus.SKIPPED, op_id
        else:
            assert node.status is OpStatus.SUCCEEDED, op_id


def test_read_only_gate_blocks_everything_mutating(tmp_path):
    spec, _plan, dag = _golden_dag()
    adapter = NullAdapter()
    executor = DagExecutor(adapter, SafetyGate(SafetyMode.READ_ONLY))
    result = executor.execute(dag)
    # A READ_ONLY denial is a clean BLOCK, not a crash: the gate answers
    # execute=False (P1-10) so the op is recorded as blocked, never FAILED,
    # and no budget is consumed for a mutation that never happened.
    assert not result.failed
    assert result.blocked  # every mutating op was refused
    assert adapter.device_exists("r1").value is False  # nothing was created
    summary = dag.summary()
    assert summary["failed"] == 0    # refused ≠ failed
    assert summary["blocked"] == 26  # the 8 create ops + their 18 dependents
    assert summary["succeeded"] == 0


def test_dry_run_executes_nothing(tmp_path):
    spec, _plan, dag = _golden_dag()
    adapter = NullAdapter()
    executor = DagExecutor(adapter, SafetyGate(SafetyMode.DRY_RUN))
    result = executor.execute(dag)
    assert adapter.device_exists("r1").value is False  # nothing was created
    summary = dag.summary()
    assert summary["succeeded"] == 0
    assert summary["failed"] == 0
    assert summary["blocked"] == len(dag.nodes)  # planned, never executed


def test_dependency_failure_blocks_dependents(tmp_path):
    spec, _plan, dag = _golden_dag()

    class ExplodingAdapter(NullAdapter):
        def create_link(self, a_device, a_port, b_device, b_port, media="auto", operation_id=None):
            raise RuntimeError("simulated link failure")

    executor = DagExecutor(
        ExplodingAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
    )
    result = executor.execute(dag)
    assert "op-link-l-r1-r2" in result.failed
    # configuration depends (transitively) on links -> blocked
    assert "op-config-r1" in result.blocked
    assert "op-flow-A-1" in result.blocked
    assert dag.nodes["op-flow-A-1"].note.startswith("blocked")


def test_budget_exhaustion_stops_mutations(tmp_path):
    spec, _plan, dag = _golden_dag()
    executor = DagExecutor(
        NullAdapter(), SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=3))
    )
    result = executor.execute(dag)
    # 3 mutations succeeded, the rest could not be authorized
    summary = dag.summary()
    assert summary["succeeded"] <= 5  # 3 mutations + 2 observe ops at most
    assert summary["blocked"] + summary["failed"] >= 1


def test_receipts_carry_operation_ids(tmp_path):
    spec, _plan, dag = _golden_dag()
    evidence = EvidenceStore(tmp_path)
    executor = DagExecutor(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        evidence,
    )
    executor.execute(dag)
    receipts = [r for r in evidence.read_all() if r.kind == "operation_receipt"]
    ids = {r.payload["operation_id"] for r in receipts}
    assert "op-create-r1" in ids
    assert "op-save" in ids


# ---------------------------------------------------------------------------
# Deferred route confirmation - a route held because its next hop's peer is
# configured by a LATER operation is re-read once every interface is up, not
# frozen as a failure. (root cause of "NOT SUCCESS / Blocked: 2" on the
# reference build: redge's routes pointed at rhq/rbr, which were configured
# after it, so the in-op read-back ran before the far end of the link existed.)
# ---------------------------------------------------------------------------

#: The GOLDEN_SPEC devices that carry static routes (switches do not).
_ROUTE_DEVICES = frozenset({"r1", "r2"})


class _RoutesHeldAdapter(NullAdapter):
    """A configure whose routes are held on the first attempt, then present.

    Models PT's behaviour exactly: the first ``configure_ios`` for a routing
    device reports FAILED *only* because the ``ip route`` lines were held as an
    unresolvable next hop (the far end of the link is configured by a later op)
    while its addresses landed. Every later call succeeds, standing in for the
    peer having come up by the time the deferred reconciliation re-reads.
    """

    def __init__(self) -> None:
        super().__init__()
        self.name = "route-hold-stub"
        self._held: set[str] = set()
        self.configure_calls: dict[str, int] = {}

    def configure_ios(self, device, commands, operation_id=None):
        self.configure_calls[device] = self.configure_calls.get(device, 0) + 1
        if device in _ROUTE_DEVICES and device not in self._held:
            self._held.add(device)
            return self._route_held_receipt(device, commands, operation_id)
        return super().configure_ios(device, commands, operation_id)

    def _route_held_receipt(self, device, commands, operation_id):
        requested = {"device": device, "command_count": len(commands)}
        pre = [CheckResult.of("device_present", "True", self.device_exists(device))]
        missing = [{"network": "10.0.2.0", "next_hop": "10.0.0.2"}]
        post = [
            # addresses landed ...
            CheckResult(
                name="configured_addresses_present",
                expected="{}",
                actual=Observable.known({}, source="stub"),
                passed=True,
            ),
            # ... only the routes were held as an unresolvable next hop
            CheckResult(
                name="configured_routes_present",
                expected="[]",
                actual=Observable.known(missing, source="stub"),
                passed=False,
            ),
        ]
        return self._receipt(
            "configure_ios", requested, operation_id, OperationResult.FAILED,
            preconditions=pre, postconditions=post,
            error=(
                "configuration did not take effect - routes still missing after "
                "retry: 10.0.2.0/24 via 10.0.0.2"
            ),
        )


def test_route_only_failure_is_confirmed_once_peers_are_up(tmp_path):
    """A held route is re-read (not frozen) once every interface is configured.

    Without the deferred reconciliation this fails: r1/r2 stay FAILED, so
    op-verify-state and op-save remain blocked and the whole build reports
    NOT SUCCESS even though the routes are genuinely installed by then.
    """
    spec, _plan, dag = _golden_dag()
    adapter = _RoutesHeldAdapter()
    executor = DagExecutor(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(tmp_path),
    )
    result = executor.execute(dag)
    # the held routes reconcile once the peers are up -> nothing failed/blocked
    assert not result.failed, result.failed
    assert not result.blocked, result.blocked
    for op_id in ("op-config-r1", "op-config-r2", "op-verify-state", "op-save"):
        assert dag.nodes[op_id].status is OpStatus.SUCCEEDED, op_id
    # each route-failed configure was re-run exactly once to confirm it, and
    # the switch configs (which carry no routes) were never re-run
    assert adapter.configure_calls["r1"] == 2
    assert adapter.configure_calls["r2"] == 2
    assert adapter.configure_calls["s1"] == 1
    assert adapter.configure_calls["s2"] == 1


def test_real_address_failure_is_not_deferred_or_masked(tmp_path):
    """An address that never landed is a real failure; reconcile must not retry it.

    Only a route-only failure is deferrable. When BOTH the address and the route
    postconditions fail, the deferred path must leave the op FAILED - re-running
    it and calling the result success would mask a genuine misconfiguration.
    """
    spec, _plan, dag = _golden_dag()

    class _AddressHeldAdapter(NullAdapter):
        """r1's addresses AND routes both never landed (a real failure)."""

        def __init__(self) -> None:
            super().__init__()
            self.name = "address-hold-stub"
            self.configure_calls: dict[str, int] = {}

        def configure_ios(self, device, commands, operation_id=None):
            self.configure_calls[device] = self.configure_calls.get(device, 0) + 1
            if device != "r1":
                return super().configure_ios(device, commands, operation_id)
            requested = {"device": device, "command_count": len(commands)}
            pre = [CheckResult.of("device_present", "True", self.device_exists(device))]
            post = [
                CheckResult(
                    name="configured_addresses_present",
                    expected="{}",
                    actual=Observable.known(
                        [{"interface": "GigabitEthernet0/0"}], source="stub"
                    ),
                    passed=False,
                ),
                CheckResult(
                    name="configured_routes_present",
                    expected="[]",
                    actual=Observable.known(
                        [{"network": "10.0.2.0", "next_hop": "10.0.0.2"}], source="stub"
                    ),
                    passed=False,
                ),
            ]
            return self._receipt(
                "configure_ios", requested, operation_id, OperationResult.FAILED,
                preconditions=pre, postconditions=post,
                error="not observed after 1 attempt(s): GigabitEthernet0/0",
            )

    adapter = _AddressHeldAdapter()
    executor = DagExecutor(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(tmp_path),
    )
    result = executor.execute(dag)
    # r1's address failure is real: it stays FAILED and is NOT re-run by the
    # deferred path (one call only) - the misconfiguration is not masked.
    assert "op-config-r1" in result.failed, result.failed
    assert dag.nodes["op-config-r1"].status is OpStatus.FAILED
    assert adapter.configure_calls["r1"] == 1, adapter.configure_calls
