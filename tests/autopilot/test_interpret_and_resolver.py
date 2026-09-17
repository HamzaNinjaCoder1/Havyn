"""Live-found regressions: _interpret honesty + destination IP resolution."""

from __future__ import annotations

import json

from src.network_autopilot.adapter.upstream.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.receipts import OperationResult
from src.network_autopilot.core.requirements import FlowOutcome

TEST_TOKEN = "test-token-long-enough-0123456789abcdef"


def test_interpret_maps_js_false_to_failed():
    result, executed, error = BridgeAdapter._interpret(Observable.known("false"))
    assert result is OperationResult.FAILED
    assert "false" in (error or "")


def test_interpret_maps_empty_to_failed():
    result, _executed, error = BridgeAdapter._interpret(Observable.known(""))
    assert result is OperationResult.FAILED


def test_interpret_keeps_success_and_pt_error_semantics():
    ok = BridgeAdapter._interpret(Observable.known("ap-dev1"))
    assert ok[0] is OperationResult.SUCCESS
    err = BridgeAdapter._interpret(Observable.known("PT_ERROR: boom"))
    assert err[0] is OperationResult.FAILED
    unk = BridgeAdapter._interpret(Observable.unknown("timeout"))
    assert unk[0] is OperationResult.UNKNOWN


_TOPOLOGY = json.dumps(
    {
        "devices": [
            {
                "name": "pc2",
                "model": "PC-PT",
                "ports": [
                    {"name": "FastEthernet0", "ip": "10.0.0.2", "mask": "255.255.255.0", "up": True, "linked": True},
                    {"name": "Bluetooth", "ip": "0.0.0.0", "mask": "0.0.0.0", "up": False, "linked": False},
                ],
            }
        ]
    }
)


def test_destination_resolver_ignores_0_0_0_0_placeholders():
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    adapter.send_expression = (  # type: ignore[method-assign]
        lambda expression, *, statements=None, wait=None: Observable.known(_TOPOLOGY)
    )
    resolved = adapter._resolve_destination_ip("pc2")
    assert resolved.is_known and resolved.value == "10.0.0.2"


def test_destination_resolver_still_refuses_genuine_ambiguity():
    topo = json.dumps(
        {
            "devices": [
                {
                    "name": "r1",
                    "model": "Router-PT",
                    "ports": [
                        {"name": "Gi0/0", "ip": "10.0.0.1", "mask": "255.255.255.252", "up": True, "linked": True},
                        {"name": "Gi0/1", "ip": "10.0.1.1", "mask": "255.255.255.0", "up": True, "linked": True},
                    ],
                }
            ]
        }
    )
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    adapter.send_expression = (  # type: ignore[method-assign]
        lambda expression, *, statements=None, wait=None: Observable.known(topo)
    )
    resolved = adapter._resolve_destination_ip("r1")
    assert not resolved.is_known
    assert "several IPs" in (resolved.detail or "")


def test_flow_test_on_unreachable_destination_is_blocked_not_pass():
    """With no route/paths, a REAL ping reports 100% loss -> BLOCKED."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    answers = iter(
        [
            _TOPOLOGY,  # resolve destination ip
            "BASE:0",  # arm
            "DONE:Packets: Sent = 4, Received = 0, Lost = 4 (100% loss),",
        ]
    )
    adapter.send_expression = (  # type: ignore[method-assign]
        lambda expression, *, statements=None, wait=None: Observable.known(next(answers))
    )
    flow = adapter.run_flow_test("pc1", "pc2", timeout=5.0)
    assert flow.outcome is FlowOutcome.BLOCKED
    assert "100% loss" in flow.evidence.get("ping_stat", "")
