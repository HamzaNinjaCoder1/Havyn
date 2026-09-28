"""Transient reads must not decide (S1.7 follow-up, build-68648f705ccb).

Measured live: a ~2 s mailbox-heartbeat hiccup at config-phase start made
`device_present` UNKNOWN for `op-config-rhq` and `op-config-rbr` — both were
permanently BLOCKED, the router never got its ROAS subinterfaces or relay
config, and 14 DHCP clients fell to APIPA. The same window failed
`op-wifi-radio-lap5` (its visibility read-back lost a settle race), which
blocked the association op and the verify/save chain behind it.

None of these may decide on the FIRST unknown read. A bounded re-read is
not knowledge — it is how the engine waits out a hiccup; if the answers
never become known the verdict stays UNKNOWN/BLOCKED, honestly.
"""

from __future__ import annotations

import json

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.adapter.packet_tracer import bridge_adapter as bridge_mod
from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from src.network_autopilot.compiler import checks as checks_mod
from src.network_autopilot.compiler.checks import CheckEvaluator
from src.network_autopilot.compiler.dag import DagNode, OpStatus, OperationDag
from src.network_autopilot.compiler.executor import DagExecutor
from src.network_autopilot.compiler.operations import Check
from src.network_autopilot.core.actual_state import ActualNetworkState
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.receipts import OperationResult
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.evidence import EvidenceStore
from tests.autopilot.test_compiler import GOLDEN_SPEC

TEST_TOKEN = "test-token-long-enough-0123456789abcdef"


def _state() -> ActualNetworkState:
    return ActualNetworkState(source="stub", observed_at="2026-09-28T00:00:00+00:00")


class _HiccupAdapter(NullAdapter):
    """`device_exists` answers 'no live channel' for the first N calls, then
    the truth (`present_after`) — the device was there all along."""

    def __init__(self, unknown_reads: int, present_after: bool | None = None) -> None:
        super().__init__()
        self.name = "hiccup-stub"
        self._unknown_reads = unknown_reads
        self._present_after = present_after

    def device_exists(self, name: str):
        if self._unknown_reads > 0:
            self._unknown_reads -= 1
            return Observable[bool].unknown(
                "no live channel: HTTP bridge sees no PT poll and the file "
                "mailbox heartbeat is stale (is PT open with the extension installed?)"
            )
        if self._present_after is None:
            return super().device_exists(name)
        return Observable[bool].known(self._present_after, source="hiccup-stub")


def test_device_present_reads_through_a_channel_unknown(monkeypatch):
    """RED pre-fix: one stale heartbeat blocked an op forever (rhq/rbr)."""
    monkeypatch.setattr(checks_mod, "_PRECONDITION_SETTLE_S", 0, raising=False)
    monkeypatch.setattr(checks_mod, "_PRECONDITION_UNKNOWN_RETRIES", 3, raising=False)
    evaluator = CheckEvaluator(_HiccupAdapter(2, present_after=True), _state())
    outcome = evaluator.evaluate(
        Check("device_present", "r1", "present")
    )
    assert outcome.passed is True, outcome.evidence


def test_device_present_stays_unknown_when_the_channel_never_answers(monkeypatch):
    """The counterpart: bounded retries must not invent knowledge."""
    monkeypatch.setattr(checks_mod, "_PRECONDITION_SETTLE_S", 0, raising=False)
    monkeypatch.setattr(checks_mod, "_PRECONDITION_UNKNOWN_RETRIES", 3, raising=False)
    evaluator = CheckEvaluator(_HiccupAdapter(unknown_reads=99), _state())
    outcome = evaluator.evaluate(
        Check("device_present", "r1", "present")
    )
    assert outcome.passed is None, outcome.passed
    assert "no live channel" in (outcome.evidence or "")


def test_device_absent_reads_through_a_channel_unknown(monkeypatch):
    """The create gate fails closed on unknown too — same hiccup, same fix."""
    monkeypatch.setattr(checks_mod, "_PRECONDITION_SETTLE_S", 0, raising=False)
    monkeypatch.setattr(checks_mod, "_PRECONDITION_UNKNOWN_RETRIES", 3, raising=False)
    evaluator = CheckEvaluator(_HiccupAdapter(2, present_after=False), _state())
    outcome = evaluator.evaluate(
        Check("device_absent", "r1", "absent")
    )
    assert outcome.passed is True, outcome.evidence


def _radio_stub(monkeypatch, visible_after_reads: int) -> tuple[BridgeAdapter, dict]:
    """A bridge whose port-visibility read-back stays False until Nth read."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    reads = {"n": 0}

    adapter.device_exists = lambda name: Observable[bool].known(True)  # type: ignore[method-assign]
    adapter.send_expression = lambda expression, **kw: Observable.known(  # type: ignore[method-assign]
        "MCPWIRELESSRADIO:installed"
    )

    def port_visible(device: str, interface: str):
        reads["n"] += 1
        return Observable[bool].known(reads["n"] >= visible_after_reads)

    adapter._port_visible = port_visible  # type: ignore[method-assign]
    monkeypatch.setattr(bridge_mod, "_RADIO_SETTLE_S", 0, raising=False)
    monkeypatch.setattr(bridge_mod, "_RADIO_SETTLE_RETRIES", 4, raising=False)
    return adapter, reads


def test_radio_readback_settles_before_declaring_failure(monkeypatch):
    """RED pre-fix: lap5's visibility read lost the settle race and the op
    FAILED even though PT finished the swap a moment later."""
    adapter, reads = _radio_stub(monkeypatch, visible_after_reads=3)
    answer = adapter.ensure_wireless_radio("lap5")
    assert answer["installed"] is True, answer
    assert reads["n"] >= 3


def test_radio_failure_stays_honest_when_the_port_never_appears(monkeypatch):
    """Bounded settle, honest failure: no port ever means installed=False."""
    adapter, _reads = _radio_stub(monkeypatch, visible_after_reads=10_000)
    answer = adapter.ensure_wireless_radio("lap5")
    assert answer["installed"] is False
    assert answer.get("answer")


def test_destination_resolve_retries_through_a_channel_unknown(monkeypatch):
    """RED pre-fix: a stale heartbeat at resolve time made a whole flow
    UNKNOWN without ever looking at the topology (A-002/A-003)."""
    monkeypatch.setattr(bridge_mod, "_RESOLVE_RETRY_S", 0, raising=False)
    monkeypatch.setattr(bridge_mod, "_RESOLVE_RETRIES", 3, raising=False)
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    reads = {"n": 0}

    def live_devices(*, fresh: bool = True):
        reads["n"] += 1
        if reads["n"] == 1:
            return Observable[list].unknown(
                "no live channel: HTTP bridge sees no PT poll and the file "
                "mailbox heartbeat is stale"
            )
        return Observable.known(
            [{"name": "pc7", "ports": [{"ip": "10.10.20.3"}]}], source="stub"
        )

    adapter._live_devices = live_devices  # type: ignore[method-assign]
    obs = adapter._resolve_destination_ip("pc7")
    assert obs.is_known and obs.value == "10.10.20.3", obs.detail


def test_failed_operation_leaves_a_named_receipt(tmp_path):
    """RED pre-fix: a failed op left its reason only in node.note, which the
    evidence never records — radio-lap5's failure reason was invisible."""
    from src.network_autopilot.compiler import compile_spec
    from src.network_autopilot.core.network_spec import NetworkSpec
    from src.network_autopilot.ipam import plan_addressing

    spec = NetworkSpec.model_validate(GOLDEN_SPEC)
    dag = compile_spec(spec, plan_addressing(spec)).dag

    class ExplodingAdapter(NullAdapter):
        def create_link(self, a_device, a_port, b_device, b_port,
                        media="auto", operation_id=None):
            raise RuntimeError("simulated link failure")

    evidence = EvidenceStore(tmp_path)
    executor = DagExecutor(
        ExplodingAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        evidence,
    )
    result = executor.execute(dag)
    assert "op-link-l-r1-r2" in result.failed
    receipt = next(
        r
        for r in evidence.read_all()
        if r.kind == "operation_receipt"
        and r.payload.get("operation_id") == "op-link-l-r1-r2"
        and r.payload.get("result") == OperationResult.FAILED.value
    )
    assert "simulated link failure" in json.dumps(receipt.payload)
    assert dag.nodes["op-link-l-r1-r2"].status is OpStatus.FAILED


def test_a_refused_mutation_leaves_a_named_receipt(tmp_path):
    """RED pre-fix: `_fail` only set node.note — radio-lap5's failure reason
    ('wireless radio not installed: ...') never reached the evidence file."""
    from src.network_autopilot.compiler.executor import ExecutionResult
    from src.network_autopilot.compiler.operations import (
        MutationClassification,
        NetworkOperation,
        Risk,
    )
    from src.network_autopilot.compiler.dag import DagNode

    evidence = EvidenceStore(tmp_path)
    executor = DagExecutor(
        NullAdapter(),
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
        evidence,
    )
    op = NetworkOperation(
        operation_id="op-wifi-radio-lap5",
        operation_type="install_wireless_radio",
        inputs={"device": "lap5", "interface": "Wireless0"},
        dependencies=(),
        classification=MutationClassification.CONFIGURE,
        risk=Risk.MEDIUM,
        blast_radius=("lap5",),
        idempotency_key="radio-lap5-test",
    )
    node = DagNode(op)
    executor._fail(
        node,
        "op-wifi-radio-lap5",
        ExecutionResult(
            dag=OperationDag(),
            state=ActualNetworkState(source="test", observed_at=None),
        ),
        "wireless radio not installed: MCPWIRELESSRADIO:no-device",
    )
    receipt = next(
        r
        for r in evidence.read_all()
        if r.kind == "operation_receipt"
        and r.payload.get("operation_id") == "op-wifi-radio-lap5"
    )
    assert receipt.payload.get("result") == OperationResult.FAILED.value
    assert "MCPWIRELESSRADIO:no-device" in json.dumps(receipt.payload)


def test_configure_push_retries_through_a_channel_unknown(monkeypatch):
    """RED pre-fix: one channel-stale push answer aborted the whole configure
    loop (scale run: `op-config-*-r1` result=unknown, read-back never ran,
    while the same push a second later succeeded on its own)."""
    monkeypatch.setattr(bridge_mod, "_IOS_SETTLE_SECONDS", 0, raising=False)
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    adapter.device_exists = lambda name: Observable[bool].known(True)  # type: ignore[method-assign]
    pushes = {"n": 0}

    def send(expression, **kw):
        pushes["n"] += 1
        if pushes["n"] == 1:
            return Observable[str].unknown(
                "no live channel: HTTP bridge sees no PT poll and the file "
                "mailbox heartbeat is stale"
            )
        return Observable[str].known("true")

    adapter.send_expression = send  # type: ignore[method-assign]
    adapter._missing_ios_addresses = lambda device, expected: {}  # type: ignore[method-assign]
    receipt = adapter.configure_ios(
        "r1",
        [
            "hostname r1",
            "interface GigabitEthernet0/0",
            "ip address 10.1.1.1 255.255.255.0",
            "no shutdown",
        ],
    )
    assert receipt.result is OperationResult.SUCCESS, receipt.error
    assert pushes["n"] >= 2, pushes


def test_a_refused_push_is_not_retried(monkeypatch):
    """Pin: a KNOWN PT failure keeps its verdict on the first answer — only
    the channel-unknown class gets the bounded re-send."""
    monkeypatch.setattr(bridge_mod, "_IOS_SETTLE_SECONDS", 0, raising=False)
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    adapter.device_exists = lambda name: Observable[bool].known(True)  # type: ignore[method-assign]
    pushes = {"n": 0}

    def send(expression, **kw):
        pushes["n"] += 1
        return Observable[str].known("false")

    adapter.send_expression = send  # type: ignore[method-assign]
    receipt = adapter.configure_ios("r1", ["hostname r1"])
    assert receipt.result is OperationResult.FAILED, receipt.result
    assert pushes["n"] == 1, pushes
