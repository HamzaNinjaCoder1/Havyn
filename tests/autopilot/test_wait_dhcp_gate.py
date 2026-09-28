"""S1.7 — the DHCP convergence gate must not be fooled by PT's 0.0.0.0.

Measured live (build-ee447a04376b): every PC exposes a Bluetooth port reading
`0.0.0.0`, and `_client_has_address` counted `bool("0.0.0.0")` as an answer —
so `op-wait-dhcp` reported `pending: [], polls: 1` while pc6/pc7 were still
leaseless, and A-001/A-002 probed three seconds later and came back UNKNOWN
("destination has no observable IP"). The gate has to mean a real address.
"""

from __future__ import annotations

from network_autopilot.compiler import executor as _ex
from network_autopilot.compiler.executor import DagExecutor, _client_has_address
from network_autopilot.core.actual_state import ActualNetworkState, DeviceObservation
from network_autopilot.core.observability import Observable
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode


def _state_with(device: str, *, ip: str) -> ActualNetworkState:
    state = ActualNetworkState(source="test", observed_at=None)
    obs = DeviceObservation(name=device)
    port = obs.port("FastEthernet0")
    port.ip = Observable.known(ip, source="test")
    state.observe_device(obs)
    return state


class _StubAdapter:
    name = "packet-tracer-bridge"

    def __init__(self, answers: list[ActualNetworkState]) -> None:
        self._answers = list(answers)
        self.observe_calls = 0

    def observe_state(self, names, **kwargs):
        self.observe_calls += 1
        snapshot = self._answers[min(self.observe_calls - 1, len(self._answers) - 1)]
        return snapshot


def test_zero_placeholder_is_not_an_address():
    """Failing-first: Bluetooth's 0.0.0.0 is a placeholder, not a lease."""
    leaseless = _state_with("pc7", ip="0.0.0.0")
    assert _client_has_address(leaseless, "pc7") is False


def test_wait_dhcp_keeps_polling_until_a_real_lease_lands(monkeypatch):
    """The gate loop itself: poll 1 shows only the placeholder, poll 2 shows
    the lease — the op must consume both polls before declaring convergence."""
    monkeypatch.setattr(_ex, "_DHCP_POLL_INTERVAL_S", 0.0)
    before = ActualNetworkState(source="test", observed_at=None)
    pc7 = DeviceObservation(name="pc7")
    pc7.port("Bluetooth").ip = Observable.known("0.0.0.0", source="test")
    before.observe_device(pc7)
    adapter = _StubAdapter([before, _state_with("pc7", ip="10.10.20.3")])
    executor = DagExecutor(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
    )
    op = _make_wait_op()
    node = _make_node(op)
    executor._op_wait_dhcp(op, node, result=None)
    assert adapter.observe_calls == 2, adapter.observe_calls
    assert "all DHCP clients have an address" in node.note


def test_a_client_with_a_real_address_still_converges_on_poll_one(monkeypatch):
    """No regression: a real address on the first read keeps the fast path."""
    monkeypatch.setattr(_ex, "_DHCP_POLL_INTERVAL_S", 0.0)
    adapter = _StubAdapter([_state_with("pc7", ip="10.10.20.3")])
    executor = DagExecutor(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
    )
    op = _make_wait_op()
    node = _make_node(op)
    executor._op_wait_dhcp(op, node, result=None)
    assert adapter.observe_calls == 1, adapter.observe_calls
    assert "all DHCP clients have an address" in node.note


def _make_wait_op():
    from network_autopilot.compiler.operations import (
        MutationClassification,
        NetworkOperation,
        Risk,
    )

    return NetworkOperation(
        operation_id="op-wait-dhcp",
        operation_type="wait_dhcp",
        inputs={"devices": ["pc7"], "timeout_s": 20.0},
        dependencies=(),
        classification=MutationClassification.OBSERVE,
        risk=Risk.LOW,
        blast_radius=(),
        idempotency_key="wait-dhcp-test",
    )


def _make_node(op):
    from network_autopilot.compiler.dag import DagNode

    return DagNode(op)
