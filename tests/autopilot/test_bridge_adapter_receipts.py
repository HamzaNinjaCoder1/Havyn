"""BridgeAdapter receipt semantics, offline: the transport is faked, the
receipt logic is real. A timeout stays UNKNOWN, PT_ERROR is FAILED, and only
a passing read-back postcondition confirms."""

from __future__ import annotations

from src.network_autopilot.adapter.upstream.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.receipts import OperationResult

TEST_TOKEN = "test-token-long-enough-0123456789abcdef"


def _adapter_with_answers(*answers: str) -> BridgeAdapter:
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    queue = list(answers)

    def fake_send(expression, *, statements=None, wait=None):
        if queue:
            return Observable.known(queue.pop(0))
        return Observable.unknown("no answer arrived")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter


def test_create_device_confirmed_when_readback_passes():
    # answers: precondition 'no' -> add 'OK' -> read-back 'yes'
    receipt = _adapter_with_answers("no", "OK", "yes").create_device("R1", "router")
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed
    assert receipt.postconditions[0].passed is True


def test_timeout_is_unknown_never_success():
    # precondition answers, then the mutation itself gets no answer
    receipt = _adapter_with_answers("no").create_device("R1", "router")
    assert receipt.result is OperationResult.UNKNOWN
    assert not receipt.confirmed


def test_pt_error_is_failed_with_evidence():
    receipt = _adapter_with_answers("no", "PT_ERROR: bad model").create_device("R1", "router")
    assert receipt.result is OperationResult.FAILED
    assert "bad model" in (receipt.error or "")
    assert receipt.executed and "PT_ERROR" in receipt.executed["pt_result"]


def test_precondition_unknown_fails_closed():
    # nothing answers: existence cannot be verified, so no mutation happens
    receipt = _adapter_with_answers().create_device("R1", "router")
    assert receipt.result is OperationResult.REJECTED
    assert receipt.error and "fail-closed" in receipt.error


def test_duplicate_precondition_blocks():
    receipt = _adapter_with_answers("yes").create_device("R1", "router")
    assert receipt.result is OperationResult.REJECTED


def test_save_project_confirms_from_inband_readback():
    receipt = _adapter_with_answers("OK:C:/temp/net.pkt|1234").save_project("net.pkt")
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed
    assert receipt.executed and receipt.executed["size"] == "1234"


def test_save_project_failure_reports_err():
    receipt = _adapter_with_answers("ERR:not created C:/x").save_project("net.pkt")
    assert receipt.result is OperationResult.FAILED


def test_configure_ios_is_never_confirmed():
    # answers: precondition 'yes' -> configureIosDevice 'OK'
    receipt = _adapter_with_answers("yes", "OK").configure_ios("R1", ["hostname R1"])
    assert receipt.executed_ok
    assert not receipt.confirmed  # IOS acceptance is not observable over the bridge
    assert receipt.postconditions[0].passed is None


def test_device_exists_maps_answers_honestly():
    adapter = _adapter_with_answers()
    adapter.send_expression = lambda expression, *, statements=None, wait=None: Observable.known("yes")  # type: ignore[method-assign]
    assert adapter.device_exists("R1").value is True
    adapter.send_expression = lambda expression, *, statements=None, wait=None: Observable.known("PT_ERROR: x")  # type: ignore[method-assign]
    assert not adapter.device_exists("R1").is_known


def test_foreign_bridge_is_reported_by_ping_identity():
    # ping() with a fake _http that returns a foreign fingerprint
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    import json as _json

    def fake_http(method, path_query, body=None, timeout=10.0):
        if path_query == "/ping":
            return 200, _json.dumps({"service": "pt-mcp-bridge", "proto": 1, "id": "deadbeef"})
        raise AssertionError("unexpected call")

    adapter._http = fake_http  # type: ignore[method-assign]
    info = adapter.ping()
    assert not info.reachable
    assert "foreign bridge" in (info.detail.value or "")
