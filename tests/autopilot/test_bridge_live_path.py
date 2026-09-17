"""Live-path logic, offline: the transport is faked, the adapter logic is real.

Covers deep inspection read-back, device enumeration, honest channel
degradation, and the real console-ping flow test. Nothing here talks to Packet
Tracer — the LIVE contract lives in test_live_contract.py.
"""

from __future__ import annotations

import json

from src.network_autopilot.adapter.upstream import js_commands as js
from src.network_autopilot.adapter.upstream.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.requirements import FlowOutcome

TEST_TOKEN = "test-token-long-enough-0123456789abcdef"

_TOPOLOGY = json.dumps(
    {
        "devices": [
            {
                "name": "r1",
                "model": "Router-PT",
                "ports": [
                    {"name": "GigabitEthernet0/0", "ip": "10.0.0.1", "mask": "255.255.255.252", "up": True, "linked": True},
                    {"name": "GigabitEthernet0/1", "ip": "10.0.1.1", "mask": "255.255.255.0", "up": True, "linked": True},
                ],
            },
            {
                "name": "pc1",
                "model": "PC-PT",
                "ports": [
                    {"name": "FastEthernet0", "ip": "10.0.1.10", "mask": "255.255.255.0", "up": True, "linked": True}
                ],
            },
            {
                "name": "pc9",
                "model": "PC-PT",
                "ports": [{"name": "FastEthernet0", "ip": "", "mask": "", "up": False, "linked": False}],
            },
        ]
    }
)


def _adapter_with_answers(*answers: str, repeat_last: bool = False) -> BridgeAdapter:
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    queue = list(answers)

    def fake_send(expression, *, statements=None, wait=None):
        if queue:
            answer = queue.pop(0)
            if repeat_last and queue == []:
                queue.append(answer)
            return Observable.known(answer)
        return Observable.unknown("no answer arrived")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter


# -- JS builders: injection regressions ---------------------------------------


def test_new_js_builders_escape_hostile_fields():
    hostile = 'x");throw 1;("'
    for built in (
        js.js_console_ping_arm_expr(hostile, "10.0.0.1"),
        js.js_console_poll_expr(hostile, 0),
    ):
        assert '")' + 'x' not in built.replace("\\\"", "")  # sanity: no raw break-out
        assert json.dumps(hostile)[1:-1] in built  # the escaped form is embedded
    statements, final = js.js_live_devices_statements()
    assert statements and final == "JSON.stringify({devices:arr})"
    assert not any(";" + hostile in s for s in statements)


def test_live_devices_js_up_is_three_valued():
    statements, _ = js.js_live_devices_statements()
    joined = "".join(statements)
    # a port whose up-state cannot be read reports null, not false
    assert "up=(typeof p.isPortUp==='function')?p.isPortUp():null;" in joined


# -- deep inspection -----------------------------------------------------------


def test_live_devices_parses_topology_answer():
    adapter = _adapter_with_answers(_TOPOLOGY)
    devices = adapter._live_devices()
    assert devices.is_known and [d["name"] for d in devices.value] == ["r1", "pc1", "pc9"]


def test_list_device_names_enumerates():
    adapter = _adapter_with_answers(_TOPOLOGY)
    names = adapter.list_device_names()
    assert names.is_known and names.value == ["r1", "pc1", "pc9"]


def test_inspect_device_reads_model_and_ports():
    adapter = _adapter_with_answers(_TOPOLOGY)
    obs = adapter.inspect_device("r1")
    assert obs.model.value == "Router-PT"
    port = obs.ports["GigabitEthernet0/0"]
    assert port.ip.value == "10.0.0.1"
    assert port.mask.value == "255.255.255.252"
    assert port.state.value == "up"


def test_inspect_device_unknown_fields_stay_unknown():
    adapter = _adapter_with_answers(_TOPOLOGY)
    obs = adapter.inspect_device("pc9")
    port = obs.ports["FastEthernet0"]
    assert not port.ip.is_known  # empty IP is NOT a known value
    assert port.state.value == "down"  # but a real false up-state is known


def test_inspect_device_absent_is_a_note_not_a_fact_about_fields():
    adapter = _adapter_with_answers(_TOPOLOGY)
    obs = adapter.inspect_device("ghost")
    assert not obs.model.is_known
    assert "not present" in obs.notes[0]


def test_inspect_device_transport_failure_is_unknown():
    adapter = _adapter_with_answers()  # nothing answers
    obs = adapter.inspect_device("r1")
    assert not obs.model.is_known
    assert any("unavailable" in n for n in obs.notes)


def test_observe_state_records_known_absent_devices():
    adapter = _adapter_with_answers(_TOPOLOGY)
    state = adapter.observe_state(["r1", "ghost"])
    assert "r1" in state.devices
    assert "ghost" not in state.devices
    assert state.metadata["absent:ghost"].is_known


def test_observe_state_single_fetch_feeds_all_devices():
    adapter = _adapter_with_answers(_TOPOLOGY)
    state = adapter.observe_state(["r1", "pc1"])
    assert state.devices["pc1"].ports["FastEthernet0"].ip.value == "10.0.1.10"


# -- real console-ping flow test ------------------------------------------------


def test_flow_test_reachable_from_real_ping_stats():
    # answers: topology (resolve pc1 ip) -> arm BASE:2 -> poll DONE (ok)
    adapter = _adapter_with_answers(
        _TOPOLOGY, "BASE:2", "DONE:Success rate is 100 percent (4/4)"
    )
    result = adapter.run_flow_test("r1", "pc1", timeout=3.0)
    assert result.outcome is FlowOutcome.REACHABLE
    assert "10.0.1.10" in (result.detail or "")
    assert "100 percent" in result.evidence["ping_stat"]


def test_flow_test_partial_loss_is_never_healthy():
    adapter = _adapter_with_answers(
        _TOPOLOGY, "BASE:0", "DONE:Success rate is 25 percent (1/4)"
    )
    result = adapter.run_flow_test("r1", "pc1", timeout=3.0)
    assert result.outcome is FlowOutcome.PARTIAL


def test_flow_test_no_replies_is_blocked():
    adapter = _adapter_with_answers(
        _TOPOLOGY, "BASE:0", "DONE:Success rate is 0 percent (0/4)"
    )
    result = adapter.run_flow_test("r1", "pc1", timeout=3.0)
    assert result.outcome is FlowOutcome.BLOCKED


def test_flow_test_unresolvable_destination_is_unknown_not_pass():
    adapter = _adapter_with_answers(_TOPOLOGY)  # topology only: pc4 is not in it
    result = adapter.run_flow_test("r1", "pc4", timeout=3.0)
    assert result.outcome is FlowOutcome.UNKNOWN
    assert "not present" in (result.detail or "")


def test_flow_test_no_console_is_unknown():
    adapter = _adapter_with_answers(_TOPOLOGY, "ERR:device without console")
    result = adapter.run_flow_test("r1", "pc1", timeout=3.0)
    assert result.outcome is FlowOutcome.UNKNOWN
    assert "console" in (result.detail or "")


def test_flow_test_quiet_console_times_out_as_unknown(monkeypatch):
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    answers = iter([_TOPOLOGY])

    def sequenced(expression, *, statements=None, wait=None):
        if "JSON.stringify" in expression or any(
            "getDeviceAt" in s for s in (statements or [])
        ):
            try:
                return Observable.known(next(answers))
            except StopIteration:
                pass
        if "BASE" in expression:
            return Observable.known("BASE:0")
        return Observable.known("WAIT")

    monkeypatch.setattr(adapter, "send_expression", sequenced, raising=False)
    import src.network_autopilot.adapter.upstream.bridge_adapter as ba

    monkeypatch.setattr(ba.time, "sleep", lambda *_: None)
    result = adapter.run_flow_test("r1", "pc1", timeout=1.5)
    assert result.outcome is FlowOutcome.UNKNOWN
    assert "no new ping statistics" in (result.detail or "")


# -- channel routing ------------------------------------------------------------


def test_send_expression_without_any_channel_is_unknown(tmp_path):
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    # hermetic: a temp mailbox with no heartbeat, so the ambient machine's
    # live extension cannot turn this into a file-channel success
    from src.network_autopilot.adapter.upstream.bridge_adapter import FileBridge

    adapter._file_bridge = FileBridge(directory=tmp_path / "mailbox")
    answer = adapter.send_expression("'x'")
    assert not answer.is_known
    assert "no live channel" in (answer.detail or "")


def test_channel_routing_prefers_http_and_falls_back_to_file(tmp_path, monkeypatch):
    import src.network_autopilot.adapter.upstream.bridge_adapter as ba
    from src.network_autopilot.adapter.base import AdapterInfo
    from src.network_autopilot.core.observability import Observable as Obs

    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)

    # 1. HTTP alive -> http
    monkeypatch.setattr(
        adapter, "ping",
        lambda timeout=5.0: AdapterInfo("x", "0", True, Obs.known("ok")),
        raising=False,
    )
    assert adapter._pick_channel() == "http"

    # 2. HTTP dead, file heartbeat fresh -> file
    monkeypatch.setattr(
        adapter, "ping",
        lambda timeout=5.0: AdapterInfo("x", "0", False, Obs.known("no")),
        raising=False,
    )
    from src.network_autopilot.adapter.upstream.bridge_adapter import FileBridge

    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    (mailbox / "alive.txt").write_text("fresh", encoding="utf-8")
    fb = FileBridge(directory=mailbox)
    adapter._file_bridge = fb
    assert adapter._pick_channel() == "file"

    # 3. both dead -> ''
    stale = mailbox / "alive.txt"
    stale.write_text("", encoding="utf-8")
    import os

    old = stale.stat().st_mtime - 60
    os.utime(stale, (old, old))
    assert adapter._pick_channel() == ""


def test_file_channel_sends_wrapper_without_http_report_definition(tmp_path, monkeypatch):
    from src.network_autopilot.adapter.base import AdapterInfo
    from src.network_autopilot.core.observability import Observable as Obs

    captured: dict = {}

    class FakeFileBridge:
        def pt_alive(self):
            return True

        def send_and_wait(self, body, timeout=12.0):
            captured["body"] = body
            return "'answer'"

    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    adapter._file_bridge = FakeFileBridge()
    monkeypatch.setattr(
        adapter, "ping",
        lambda timeout=5.0: AdapterInfo("x", "0", False, Obs.known("no")),
        raising=False,
    )
    answer = adapter.send_expression("1+1", statements=["var q=2;"])
    assert answer.is_known and answer.value == "'answer'"
    body = captured["body"]
    # file channel: reportResult must be CALLED, never DEFINED via XHR
    assert "reportResult(" in body
    assert "XMLHttpRequest" not in body
