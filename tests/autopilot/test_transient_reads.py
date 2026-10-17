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


def _bulk_adapter(monkeypatch, snapshots):
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    adapter.device_exists = lambda name: Observable[bool].known(True)  # type: ignore[method-assign]
    adapter.send_expression = (  # type: ignore[method-assign]
        lambda expr, **kw: Observable[str].known(
            json.dumps([{"device": "pc0", "ok": True}])
        )
    )
    calls = {"n": 0}

    def fake_live(*, fresh=True):
        calls["n"] += 1
        return snapshots[min(calls["n"] - 1, len(snapshots) - 1)]

    monkeypatch.setattr(adapter, "_live_devices", fake_live)
    return adapter, calls


_BULK_SPEC = [
    {
        "device": "pc0",
        "dhcp": False,
        "ip": "10.0.0.10",
        "mask": "255.255.255.0",
        "gateway": "10.0.0.1",
    }
]


def test_bulk_readback_settles_before_declaring_failure(monkeypatch):
    """RED pre-fix: one unknown workspace snapshot made every static host
    receipt FAILED with 'address not observed' although PT had reported
    ok:true (scale run: 20/20 uniform miss)."""
    monkeypatch.setattr(bridge_mod, "_BULK_READ_SETTLE_S", 0, raising=False)
    good = Observable[list[dict]].known(
        [{"name": "pc0", "ports": [{"name": "Ethernet0", "ip": "10.0.0.10"}]}]
    )
    adapter, calls = _bulk_adapter(
        monkeypatch, [Observable[list[dict]].unknown("heartbeat stale"), good]
    )
    receipts = adapter.configure_hosts_bulk(_BULK_SPEC)
    assert len(receipts) == 1
    assert receipts[0].result is OperationResult.SUCCESS, receipts[0].error
    assert calls["n"] >= 2, calls


def test_bulk_readback_stays_unknown_when_snapshot_never_returns(monkeypatch):
    """Rule 7: an unobservable read-back is UNKNOWN, never a FAILED verdict
    with a message that blames the device."""
    monkeypatch.setattr(bridge_mod, "_BULK_READ_SETTLE_S", 0, raising=False)
    dead = Observable[list[dict]].unknown("no live channel: heartbeat is stale")
    adapter, _ = _bulk_adapter(monkeypatch, [dead])
    receipts = adapter.configure_hosts_bulk(_BULK_SPEC)
    assert receipts[0].result is OperationResult.UNKNOWN, receipts[0].result
    assert receipts[0].error


def test_bulk_readback_fails_when_the_address_is_truly_missing(monkeypatch):
    """Pin: a KNOWN snapshot without the address is a real failure."""
    adapter, _ = _bulk_adapter(
        monkeypatch,
        [Observable[list[dict]].known([{"name": "pc0", "ports": []}])],
    )
    receipts = adapter.configure_hosts_bulk(_BULK_SPEC)
    assert receipts[0].result is OperationResult.FAILED
    assert "not observed" in (receipts[0].error or "")


def test_delete_settles_existence_before_fail_closed(monkeypatch):
    """RED pre-fix: one unknown existence read made teardown fail-closed the
    delete as 'rejected' and leak the device (scale run: asa1-4 rejected)."""
    monkeypatch.setattr(bridge_mod.time, "sleep", lambda s: None)
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    reads = {"n": 0}
    answers = [
        Observable[bool].unknown("heartbeat stale"),
        Observable[bool].known(True),
        Observable[bool].known(False),
    ]

    def fake_exists(name):
        reads["n"] += 1
        return answers[min(reads["n"] - 1, len(answers) - 1)]

    adapter.device_exists = fake_exists  # type: ignore[method-assign]
    adapter.send_expression = (  # type: ignore[method-assign]
        lambda expr, **kw: Observable[str].known("OK")
    )
    receipt = adapter.delete_device("asa1")
    assert receipt.result is OperationResult.SUCCESS, (
        receipt.result,
        receipt.error,
    )
    assert reads["n"] >= 3, reads


# ==========================================================================
# A channel outage is survived, not concluded from (build-81df879ad555)
# ==========================================================================


def _outage_adapter(monkeypatch, *, recover_after_probes, ever_live=True):
    """A bridge whose channel probes see darkness for N rounds, then `file`.

    Models PT's measured stall pattern: the mailbox heartbeat stops ticking
    (so `_probe_channels` answers "") for a stretch, then comes back — the
    repairs minutes after the stall proved PT had been open the whole time.
    """
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    adapter._ever_live_channel = ever_live
    probes = {"n": 0}

    def probe():
        probes["n"] += 1
        if recover_after_probes is None or probes["n"] < recover_after_probes:
            return ""
        return "file"

    monkeypatch.setattr(adapter, "_probe_channels", probe)
    monkeypatch.setattr(
        adapter, "_send_file",
        lambda expression, statements, wait: Observable[str].known("OK:ride-out"),
    )
    monkeypatch.setattr(bridge_mod.time, "sleep", lambda s: None)
    return adapter, probes


def test_send_rides_out_a_channel_outage_this_adapter_once_survived(monkeypatch):
    """RED pre-fix: one dark probe decided for everyone.

    build-81df879ad555, measured on the evidence timeline: PT's Script
    Engine stopped answering for 215 s (20:39:11 -> 20:42:55). Inside that
    window 8 `device_present` preconditions read "no live channel" and were
    BLOCKED, the 22-host batch never left the machine (no `__mcpHostResults`
    in any of 246 sends), and the build fell to 102/123 — while the repair
    pushes minutes later succeeded, proving PT had been open all along. A
    send on an adapter that HAS seen a live channel waits one bounded
    window for the channel to come back.
    """
    adapter, probes = _outage_adapter(monkeypatch, recover_after_probes=3)

    answer = adapter.send_expression("'x'", wait=0.2)

    assert answer.is_known and answer.value == "OK:ride-out", answer.detail
    assert probes["n"] >= 3, probes


def test_a_channel_this_adapter_never_had_fails_fast_without_waiting(monkeypatch):
    """Boundary control: the window is for an outage of a PT that proved
    itself, never for an adapter that has never seen one. Live-gated tests
    and CLI runs against a closed PT keep their immediate answer."""
    adapter, _ = _outage_adapter(monkeypatch, recover_after_probes=None, ever_live=False)
    slept: list[float] = []
    monkeypatch.setattr(bridge_mod.time, "sleep", slept.append)

    answer = adapter.send_expression("'x'", wait=0.2)

    assert not answer.is_known
    assert "no live channel" in (answer.detail or "")
    assert slept == [], "an adapter that never saw PT must not wait for it"


def test_one_outage_gets_one_window_not_one_per_send(monkeypatch):
    """Bounded: a PT that never comes back costs the window ONCE; every
    later send fails fast with the same honest UNKNOWN instead of
    re-waiting 240 s per operation."""
    adapter, _ = _outage_adapter(monkeypatch, recover_after_probes=None)
    adapter._CHANNEL_OUTAGE_BUDGET_S = 0.05
    slept: list[float] = []
    monkeypatch.setattr(bridge_mod.time, "sleep", slept.append)

    first = adapter.send_expression("'x'", wait=0.2)
    waited = len(slept)
    slept.clear()
    second = adapter.send_expression("'x'", wait=0.2)

    assert not first.is_known and not second.is_known
    assert waited > 0, (
        "the first send must ride the window, not conclude UNKNOWN on the "
        "first dark probe"
    )
    assert len(slept) == 0, f"a spent window must not be re-opened: {slept}"


# ==========================================================================
# An unanswered batch is UNKNOWN — it may not blame the device (rule 7/8)
# ==========================================================================


def test_bulk_batch_that_never_reached_PT_is_unknown_not_blame(monkeypatch):
    """RED pre-fix: build-81df879ad555's host batch never left the machine
    (`__mcpHostResults` absent from all 246 sends), yet all 22 receipts
    claimed `address not observed after configure` — blaming the device for
    a transport failure. An unanswered round trip is UNKNOWN with the
    channel detail; only a KNOWN answer can fail a host."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    adapter.device_exists = lambda name: Observable[bool].known(True)  # type: ignore[method-assign]
    adapter.send_expression = lambda expr, **kw: Observable[str].unknown(  # type: ignore[method-assign]
        "no live channel: HTTP bridge sees no PT poll and the file mailbox "
        "heartbeat is stale (is PT open with the extension installed?)"
    )
    monkeypatch.setattr(
        adapter, "_live_devices",
        lambda *, fresh=True: Observable[list[dict]].known([]),
    )

    receipts = adapter.configure_hosts_bulk(_BULK_SPEC)

    assert receipts[0].result is OperationResult.UNKNOWN, receipts[0].error
    assert "never reached PT" in (receipts[0].error or ""), receipts[0].error
    assert "not observed" not in (receipts[0].error or ""), receipts[0].error


def test_bulk_receipt_names_PT_omission_instead_of_blaming_the_address(monkeypatch):
    """A KNOWN batch answer that omits the device is reported for what it is:
    PT ran the batch and said nothing about this host."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    adapter.device_exists = lambda name: Observable[bool].known(True)  # type: ignore[method-assign]
    adapter.send_expression = lambda expr, **kw: Observable[str].known(  # type: ignore[method-assign]
        json.dumps([{"device": "somebody-else", "ok": True}])
    )
    monkeypatch.setattr(
        adapter, "_live_devices",
        lambda *, fresh=True: Observable[list[dict]].known([]),
    )

    receipts = adapter.configure_hosts_bulk(_BULK_SPEC)

    assert receipts[0].result is OperationResult.FAILED, receipts[0].result
    assert "without a result for this device" in (receipts[0].error or ""), receipts[0].error
    assert "not observed" not in (receipts[0].error or ""), receipts[0].error
