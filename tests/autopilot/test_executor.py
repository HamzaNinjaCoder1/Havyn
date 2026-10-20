"""DAG executor: golden offline pipeline, idempotency, safety, propagation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler import executor as executor_mod
from src.network_autopilot.compiler import checks as checks_mod
from src.network_autopilot.compiler.dag import OpStatus
from src.network_autopilot.compiler.executor import DagExecutor
from src.network_autopilot.compiler.operations import Check
from src.network_autopilot.core.receipts import CheckResult, OperationReceipt, OperationResult
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


# ---------------------------------------------------------------------------
# configure_ios batching (S1.19 runtime): the bulk TRANSPORT must not cost
# any serial SEMANTICS - the precondition gate, the serial-clock proof and
# the postcondition evaluation all still run for batched ops.
# ---------------------------------------------------------------------------


class _BulkConfigureAdapter(NullAdapter):
    """A bulk configure path, and a serial one that REFUSES to run.

    The refusal is the test's red: if the executor does not route
    configure_ios through configure_ios_bulk, the serial call raises, the
    op fails with "executor exception", and every assertion below goes red
    naming exactly what is missing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.bulk_batches: list[list[dict]] = []

    def configure_ios_bulk(self, specs, operation_id_prefix="batch-config"):
        self.bulk_batches.append([dict(s) for s in specs])
        return [
            OperationReceipt(
                operation_id=f"{operation_id_prefix}-{s['device']}",
                operation="configure_ios",
                requested={
                    "device": s["device"],
                    "command_count": len(s["commands"]),
                },
                executed={"batched": True},
                result=OperationResult.SUCCESS,
                postconditions=[
                    CheckResult.of(
                        "configured_addresses_present", "True",
                        Observable.known(True),
                    )
                ],
            )
            for s in specs
        ]

    def configure_ios(self, device, commands, operation_id=None):
        raise AssertionError(
            "configure_ios ran serially - the bulk path (configure_ios_bulk) "
            "was not used"
        )


def test_configure_ios_ops_batch_through_the_bulk_transport(tmp_path):
    """Wiring red: without `configure_ios` in the executor's bulk handlers the
    four consecutive configure ops fall back to serial — the serial path must
    refuse, so this cannot pass on the unfixed code."""
    spec, _plan, dag = _golden_dag()
    adapter = _BulkConfigureAdapter()
    executor = DagExecutor(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(tmp_path),
    )
    result = executor.execute(dag)

    assert not result.failed, result.failed
    assert len(adapter.bulk_batches) == 1, (
        "the four consecutive configure ops must travel as ONE batch, got "
        f"{len(adapter.bulk_batches)}"
    )
    pushed = {s["device"] for s in adapter.bulk_batches[0]}
    assert pushed == {"r1", "r2", "s1", "s2"}, pushed
    for op_id in ("op-config-r1", "op-config-r2",
                  "op-vlan-switch-s1", "op-vlan-switch-s2"):
        assert dag.nodes[op_id].status is OpStatus.SUCCEEDED, (
            op_id, dag.nodes[op_id].status, dag.nodes[op_id].note
        )


def test_batched_configure_keeps_the_serial_clock_proof(tmp_path, monkeypatch):
    """Batching merges transport, not verification: a configure op carrying
    `serial_clocks` still gets its DCE/DTE read-back through the batch path,
    and a PROVEN mismatch still fails the op. Red on unfixed code (serial
    path refuses) and red if the bulk path skips the proof (mutation: drop
    the serial-clock hook and op-config-r1 ends SUCCEEDED)."""
    spec, _plan, dag = _golden_dag()
    dag.nodes["op-config-r1"].operation.inputs["serial_clocks"] = [
        {"device": "r1", "interface": "Serial2/0"},
    ]

    def fake_clock_evidence(adapter, entries):
        return [{
            "device": "r1", "interface": "Serial2/0", "status": "fail",
            "detail": "clock roles mismatch on r1:Serial2/0",
        }]

    monkeypatch.setattr(
        executor_mod, "read_serial_clock_evidence", fake_clock_evidence
    )
    adapter = _BulkConfigureAdapter()
    executor = DagExecutor(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(tmp_path),
    )
    executor.execute(dag)

    node = dag.nodes["op-config-r1"]
    assert node.status is OpStatus.FAILED, (
        "a proven serial-clock mismatch must fail the batched op, got "
        f"{node.status} ({node.note})"
    )
    assert "clock roles mismatch" in (node.note or ""), node.note


def test_batched_op_with_unsatisfiable_precondition_is_blocked_not_pushed(
    tmp_path, monkeypatch
):
    """The precondition gate is not optional because the op is batched: a
    configure op whose `device_present` cannot be satisfied is BLOCKED before
    any push, exactly like the serial path (rule 9: refuse, never
    half-configure).

    Red on unfixed code (the bulk path is never reached, so `bulk_batches` is
    empty) and red if the bulk path pushes the op anyway (mutation: drop the
    dry precondition gate and r1 sails into the batch).
    """
    # Excluding r1 leaves three configure ops: keep them batchable so the
    # batch path under test is the one that makes the exclusion.
    monkeypatch.setattr(DagExecutor, "_BULK_MIN_RUN", 2)
    monkeypatch.setattr(checks_mod, "_PRECONDITION_SETTLE_S", 0.0, raising=False)

    spec, _plan, dag = _golden_dag()
    # Fail r1's presence check surgically: a device that is never in the
    # workspace, so r1's own creates and links (which legitimately need r1
    # present) are untouched and the configure op is the only one blocked.
    # NetworkOperation is frozen, so the op is replaced on the node.
    node_r1 = dag.nodes["op-config-r1"]
    node_r1.operation = replace(
        node_r1.operation,
        preconditions=(*node_r1.operation.preconditions,
                       Check(kind="device_present",
                             target="ghost-that-never-existed",
                             expected="True")),
    )
    adapter = _BulkConfigureAdapter()
    executor = DagExecutor(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        EvidenceStore(tmp_path),
    )
    result = executor.execute(dag)

    assert adapter.bulk_batches, (
        "the other configure ops must still batch (the exclusion happens "
        "inside the batch decision)"
    )
    pushed = {s["device"] for batch in adapter.bulk_batches for s in batch}
    assert "r1" not in pushed, (
        f"a device whose precondition fails was pushed anyway: {pushed}"
    )
    node = dag.nodes["op-config-r1"]
    assert node.status is OpStatus.BLOCKED, (node.status, node.note)
    assert (node.note or "").startswith("preconditions not satisfied"), node.note
    assert "op-config-r1" in result.blocked, result.blocked


def test_batched_ops_record_postcondition_evidence(tmp_path):
    """Evidence parity: a batched configure op gets the same read-back
    evaluation the serial path records (`postcondition_evaluation`), so the
    evidence chain never has a hole where the batch was. Red on unfixed code
    (serial path refuses → no receipt → no evaluation) and red if the bulk
    path skips the hook (mutation: drop `_evaluate_postconditions` from the
    batch finisher)."""
    spec, _plan, dag = _golden_dag()
    adapter = _BulkConfigureAdapter()
    evidence = EvidenceStore(tmp_path)
    executor = DagExecutor(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        evidence,
    )
    result = executor.execute(dag)

    assert not result.failed, result.failed
    batched = {"op-config-r1", "op-config-r2",
               "op-vlan-switch-s1", "op-vlan-switch-s2"}
    evaluated = {
        r.payload["operation_id"]
        for r in evidence.read_all()
        if r.kind == "postcondition_evaluation"
        and r.payload.get("operation_id") in batched
    }
    assert evaluated == batched, (
        f"every batched configure op needs its postconditions evaluated, "
        f"missing: {batched - evaluated}"
    )
