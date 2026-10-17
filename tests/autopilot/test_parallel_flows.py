"""Parallel flow batching: the wiring, pinned — including on the failure path.

S1.19 added ``DagExecutor._try_parallel_flows`` and
``BridgeAdapter.run_flow_tests_bulk`` with no test at all, which is how it
shipped as dead code. The executor's probe waiver (S1.2/F-2) deliberately lets
a ``verify_flow`` run past an unsatisfied dependency so a partially-built
network still reports what it can — but ``_try_parallel_flows`` applied a
stricter rule and refused to batch the very same flow, so every build in which
a configure op failed silently fell back to twelve serial ping windows
(measured: build-859ca11f4988, 12 flows / 483 s, ``parallel`` absent from all
twelve evidence records, ``probe_waived_dependencies`` present 13 times).

These tests pin the wiring rather than the adapter method in isolation.
"""

from __future__ import annotations

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.compiler.dag import OpStatus, OperationDag
from src.network_autopilot.compiler.executor import DagExecutor
from src.network_autopilot.compiler.operations import (
    MutationClassification,
    NetworkOperation,
    Risk,
)
from src.network_autopilot.core.receipts import OperationResult
from src.network_autopilot.core.requirements import FlowOutcome, FlowResult
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.evidence import EvidenceStore


def _flow_op(op_id: str, criterion: str, source: str, dest: str, ip: str) -> NetworkOperation:
    return NetworkOperation(
        operation_id=op_id,
        operation_type="verify_flow",
        inputs={
            "criterion_id": criterion,
            "requirement_ids": ["R-001"],
            "source": source,
            "destination": dest,
            "expect": "reachable",
            "transport": "icmp",
            "port": None,
            "destination_ip": ip,
        },
        dependencies=("op-config-r1",),
        classification=MutationClassification.OBSERVE,
        risk=Risk.LOW,
        blast_radius=(),
        idempotency_key=f"k-{op_id}",
    )


def _dag() -> OperationDag:
    """One configure op that two flow probes depend on.

    That shape is what the reference build produces: every ``verify_flow``
    depends on the whole config run, so a single failed configure leaves every
    flow with an unmet dependency.
    """
    dag = OperationDag()
    dag.add(
        NetworkOperation(
            operation_id="op-config-r1",
            operation_type="configure_ios",
            inputs={"device": "r1", "commands": ["hostname r1"]},
            classification=MutationClassification.CONFIGURE,
            risk=Risk.LOW,
            blast_radius=("r1",),
            idempotency_key="k-config-r1",
        )
    )
    dag.add(_flow_op("op-flow-A-005", "A-005", "pc1", "srv1", "10.0.0.9"))
    dag.add(_flow_op("op-flow-A-006", "A-006", "pc2", "srv2", "10.0.0.10"))
    return dag


class _BulkFlowAdapter(NullAdapter):
    """A data plane that can batch; optionally refuses one configure push.

    The refusal is modelled as a REJECTED receipt rather than an exception
    because that is the path the reference build actually took: ``_fail``
    marks the op FAILED without blocking its dependents, which is what leaves
    the probes pending for the waiver. The executor's *exception* path calls
    ``dag.mark_failed`` and blocks them instead — a different scenario, and
    not the one measured in build-859ca11f4988.
    """

    def __init__(self, *, can_batch: bool = True, refuse_push: bool = False) -> None:
        super().__init__()
        self.name = "bulk-flow-stub"
        self.can_batch = can_batch
        self.refuse_push = refuse_push
        self.bulk_calls: list[list[dict]] = []
        self.serial_calls: list[tuple[str, str]] = []
        # The configure op targets this device, so it has to exist for the
        # happy path; the refused path is a receipt decision, not a missing one.
        self._devices["r1"] = {
            "role": "router", "model": "Router-PT", "position": (0, 0), "ports": {},
        }

    def configure_ios(self, device, commands, operation_id=None):
        if self.refuse_push:
            return self._receipt(
                "configure_ios",
                {"device": device, "command_count": len(commands)},
                operation_id,
                OperationResult.REJECTED,
                error="stub: PT refused the push",
            )
        return super().configure_ios(device, commands, operation_id=operation_id)

    @staticmethod
    def _verdict(spec) -> FlowResult:
        return FlowResult(
            source=str(spec["source"]),
            destination=str(spec["destination"]),
            transport=str(spec.get("transport") or "icmp"),
            port=spec.get("port"),
            outcome=FlowOutcome.REACHABLE,
            detail=f"observed {spec['source']}->{spec['destination']}",
        )

    def run_flow_tests_bulk(self, specs):
        if not self.can_batch:
            return None  # the base-class contract: "I cannot batch"
        self.bulk_calls.append([dict(s) for s in specs])
        return [self._verdict(s) for s in specs]

    def run_flow_test(
        self,
        source,
        destination,
        *,
        transport="icmp",
        port=None,
        destination_ip=None,
    ):
        self.serial_calls.append((source, destination))
        return self._verdict(
            {
                "source": source,
                "destination": destination,
                "transport": transport,
                "port": port,
            }
        )


def _gate() -> SafetyGate:
    return SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100))


def _configured_adapter() -> _BulkFlowAdapter:
    """A stub whose configure push is accepted, so the happy path is covered."""
    return _BulkFlowAdapter()


def test_independent_flows_are_batched_into_one_burst(tmp_path):
    """The happy path: nothing failed, so the whole flow run goes at once."""
    adapter = _configured_adapter()
    dag = _dag()
    DagExecutor(adapter, _gate(), EvidenceStore(tmp_path)).execute(dag)

    assert len(adapter.bulk_calls) == 1, adapter.bulk_calls
    assert len(adapter.bulk_calls[0]) == 2, adapter.bulk_calls
    assert adapter.serial_calls == [], adapter.serial_calls
    assert dag.summary()["failed"] == 0, dag.summary()


def test_a_failed_configure_still_batches_its_waived_probes(tmp_path):
    """Regression: the failure path must batch exactly like the happy path.

    A configure that is refused leaves every flow with an unmet dependency.
    The executor waives that dependency for probe types so the half-built
    network still reports what it can (S1.2/F-2), so the parallel path must
    apply the same waiver. Without it the probes run one at a time and a
    twelve-flow build pays twelve ping windows — which is what happened in
    build-859ca11f4988.

    The configure op is expected to be FAILED; the point is that the probes
    still run, and run together.
    """
    adapter = _BulkFlowAdapter(refuse_push=True)
    dag = _dag()
    DagExecutor(adapter, _gate(), EvidenceStore(tmp_path)).execute(dag)

    # The configure really did fail, so this test is exercising the real shape.
    assert dag.nodes["op-config-r1"].status is OpStatus.FAILED, (
        dag.nodes["op-config-r1"].note
    )
    assert dag.summary()["failed"] == 1, dag.summary()

    # ...and the probes are neither blocked nor run one by one.
    for op_id in ("op-flow-A-005", "op-flow-A-006"):
        assert dag.nodes[op_id].status is OpStatus.SUCCEEDED, dag.nodes[op_id].note

    assert adapter.serial_calls == [], (
        "probes fell back to the serial path despite the waiver: "
        f"{adapter.serial_calls}"
    )
    assert len(adapter.bulk_calls) == 1, (
        f"expected one batched burst, got {adapter.bulk_calls!r}"
    )
    assert [s["criterion_id"] for s in adapter.bulk_calls[0]] == ["A-005", "A-006"]


def test_an_adapter_that_cannot_batch_keeps_serial_verdicts(tmp_path):
    """``None`` from the bulk method is a contract, not an error to hide."""
    adapter = _BulkFlowAdapter(can_batch=False)
    dag = _dag()
    DagExecutor(adapter, _gate(), EvidenceStore(tmp_path)).execute(dag)

    assert adapter.bulk_calls == [], adapter.bulk_calls
    assert sorted(adapter.serial_calls) == [("pc1", "srv1"), ("pc2", "srv2")]
    assert dag.summary()["failed"] == 0, dag.summary()


def test_batched_flows_are_recorded_as_parallel_evidence(tmp_path):
    """Evidence must say the burst was used, or the runtime claim is unauditable."""
    adapter = _configured_adapter()
    dag = _dag()
    store = EvidenceStore(tmp_path)
    DagExecutor(adapter, _gate(), store).execute(dag)

    # The store mints a build id per instance, so the assertion has to read
    # the very store the run wrote to.
    parallel = [r for r in store.read_all() if r.kind == "flow_result"]
    assert len(parallel) == 2, parallel
    assert all(r.payload.get("parallel") is True for r in parallel), parallel


# ==========================================================================
# The first ping window must cover a COLD cross-fabric ping (build-ef996a15797f)
# ==========================================================================
#
# Measured live on build-ef996a15797f's canvas (2026-10-16, PT 9.0.1): a single
# clean `ping` typed on a freshly built path printed its `Packets: Sent` stat
# block 14.3 s (pc16->srv1), 24.3 s (pc1->pc7) and 16.1 s (pc9->srvisp) later —
# the first echo pays ARP resolution and, on a lossy cold path, per-echo
# timeouts. The old first window was 9 s (bulk) / 8 s (serial), sized only to
# out-last a DEAD path's 0%-loss block (~8 s) and never a live cold one. So
# every flow's first attempt timed out, was re-armed (a fresh `ping` that threw
# away the ARP convergence the first had begun), and in build-ef996a15797f five
# flows never printed a stat block in 9+16+25 s at all. The window has to cover
# the measured worst case (24.3 s) plus margin so a cold ping finishes inside
# ONE attempt and does not depend on a re-arm.


def test_first_ping_window_covers_the_measured_cold_path_latency():
    """The first bulk/serial ping window must out-last a measured cold ping.

    The values are the live single-ping latencies measured on the reference
    canvas (pc16->srv1 14.3 s, pc9->srvisp 16.1 s, pc1->pc7 24.3 s); the window
    must cover the worst of them plus margin, or every flow pays a doomed first
    attempt and a convergence-discarding re-arm.
    """
    worst_measured_cold_ping_s = 24.3
    margin_s = 5.0
    import src.network_autopilot.adapter.packet_tracer.bridge_adapter as bridge_mod
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import (
        BridgeAdapter,
    )

    assert BridgeAdapter._BULK_ATTEMPT_TIMEOUTS[0] >= worst_measured_cold_ping_s + margin_s, (
        "the bulk first ping window is shorter than a measured cold ping "
        f"({worst_measured_cold_ping_s}s): "
        f"{BridgeAdapter._BULK_ATTEMPT_TIMEOUTS}"
    )
    assert bridge_mod._FLOW_ATTEMPT_TIMEOUTS[0] >= worst_measured_cold_ping_s + margin_s, (
        "the serial first ping window is shorter than a measured cold ping "
        f"({worst_measured_cold_ping_s}s): "
        f"{bridge_mod._FLOW_ATTEMPT_TIMEOUTS}"
    )


class _FakeClock:
    """A stand-in for the time module: sleeps advance an in-test clock so a
    12-second ping completes instantly in wall-clock terms."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(float(seconds), 0.0)


def test_a_cold_ping_is_caught_by_the_first_window_not_a_rearm(monkeypatch):
    """RED pre-fix: a ping that finishes inside the first window was abandoned.

    The fake console prints the stat block `COMPLETION_DELAY_S` after the ping is
    armed (12 s — a scaled stand-in for the measured 14-24 s cold latency, chosen
    to sit INSIDE the fixed first window and OUTSIDE the old 9 s one). With the
    first window too short the flow timed out, was re-armed (a fresh ping that
    discards the first's ARP convergence), and only the second attempt caught
    it; the fix must give the cold ping ONE window so it resolves on the first
    arm.
    """
    import json as _json

    from src.network_autopilot.adapter.packet_tracer import bridge_adapter as bridge_mod
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
    from src.network_autopilot.core.observability import Observable

    clock = _FakeClock()
    monkeypatch.setattr(bridge_mod.time, "time", clock.time)
    monkeypatch.setattr(bridge_mod.time, "sleep", clock.sleep)

    completion_delay_s = 12.0
    arm_times: list[float] = []

    def fake_send(expression, **_kw):
        # The arm expression is the only one that types `ping ` (the poll JS
        # matches on `Packets: Sent`, never the bare command).
        if "ping " in expression:
            arm_times.append(clock.now)
            return Observable[str].known(_json.dumps([{"i": 0, "base": 0}]))
        if arm_times and (clock.now - arm_times[-1]) >= completion_delay_s:
            return Observable[str].known(_json.dumps([{
                "i": 0,
                "r": "DONE:Packets: Sent = 4, Received = 4, Lost = 0 (0% loss),",
            }]))
        return Observable[str].known(_json.dumps([{"i": 0, "r": "WAIT"}]))

    adapter = BridgeAdapter(port=59999, token="t" * 40, host_bridge=False)
    monkeypatch.setattr(adapter, "send_expression", fake_send)

    results = adapter.run_flow_tests_bulk([
        {"source": "pc1", "destination": "srv1", "transport": "icmp",
         "port": None, "destination_ip": "10.20.40.10"},
    ])

    assert results[0].outcome is FlowOutcome.REACHABLE, results[0].detail
    assert len(arm_times) == 1, (
        "the cold ping was abandoned and re-armed instead of being given one "
        f"window long enough to finish: {len(arm_times)} arms"
    )
