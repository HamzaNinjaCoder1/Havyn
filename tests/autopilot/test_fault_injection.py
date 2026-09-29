"""Fault-injection harness (S2.5): five labelled faults, offline.

For every fault: the injected state reads as a broken flow, the diagnosis
engine names the expected cause family (and only proposes a repair when the
evidence supports one), and revert restores the exact prior observation.
The no-anomaly control proves the causes come from the injected evidence:
without the fault, the same failing flow honestly reports unknown instead
of naming a cause out of thin air.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.core.actual_state import (
    ActualNetworkState,
    DeviceObservation,
    PortObservation,
)
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.requirements import AcceptanceReport, Verdict
from src.network_autopilot.diagnosis import diagnose
from src.network_autopilot.diagnosis.fault_injection import (
    FIRST_FIVE,
    port_shutdown_fault,
    route_missing_fault,
    trunk_vlan_not_allowed_fault,
    wrong_access_vlan_fault,
    wrong_gateway_fault,
)
from src.network_autopilot.verification.engine import (
    VerificationReport,
    VerificationTest,
)

SPEC = {
    "name": "fault-lab",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "sw1", "role": "switch"},
        {"id": "pc1", "role": "pc"},
        {"id": "srv", "role": "server"},
    ],
    "links": [
        {"id": "l1", "a": {"device": "sw1"}, "b": {"device": "pc1"}},
        {"id": "l2", "a": {"device": "r1"}, "b": {"device": "sw1"}},
    ],
    "subnets": [{"id": "net1", "cidr": "192.168.1.0/24"}],
    "ip_assignments": [
        {"device": "r1", "subnet": "net1", "ip": "192.168.1.1"},
        {"device": "pc1", "subnet": "net1", "ip": "192.168.1.10"},
        {"device": "srv", "subnet": "net1", "ip": "192.168.1.20"},
    ],
    "routing": {
        "mode": "static",
        "static_routes": [
            {"device": "r1", "destination": "10.9.9.0/24",
             "next_hop": "192.168.1.254"},
        ],
    },
}


def _known(value):
    return Observable.known(value, source="test")


def _state() -> ActualNetworkState:
    """Healthy observed state: everything up, addressed, routed."""
    state = ActualNetworkState(source="test", observed_at="then")
    r1 = DeviceObservation(name="r1")
    r1.port("GigabitEthernet0/0").state = _known("up")
    r1.port("GigabitEthernet0/0").ip = _known("192.168.1.1")
    r1.routes = _known([{"network": "10.9.9.0", "prefix": 24}])
    sw1 = DeviceObservation(name="sw1")
    sw1.port("FastEthernet0/1").state = _known("up")
    sw1.port("FastEthernet0/1").vlan = _known("1")
    sw1.vlans = _known([{"id": 1}, {"id": 20}])
    pc1 = DeviceObservation(name="pc1")
    pc1.port("FastEthernet0").state = _known("up")
    pc1.port("FastEthernet0").ip = _known("192.168.1.10")
    pc1.port("FastEthernet0").gateway = _known("192.168.1.1")
    srv = DeviceObservation(name="srv")
    srv.port("FastEthernet0").state = _known("up")
    srv.port("FastEthernet0").ip = _known("10.9.9.10")
    for dev in (r1, sw1, pc1, srv):
        state.observe_device(dev)
    return state


def _report(*tests: VerificationTest) -> VerificationReport:
    return VerificationReport(
        tests=tuple(tests),
        summary={},
        acceptance=AcceptanceReport(
            results=(), requirement_results=(), summary={}, satisfied=False,
        ),
        observations_complete=True,
    )


def _flow_test(test_id="VT-flow-A-1", source="pc1",
               target="srv") -> VerificationTest:
    return VerificationTest(
        test_id=test_id, level="connectivity", source_requirement="R-1",
        source_acceptance_criterion="A-1", source=source, target=target,
        expected_result="reachable", observed_result="blocked",
        evidence=("flow observed: blocked",), timestamp="then",
        verdict=Verdict.FAIL,
    )


def _struct_test(test_id: str, target: str) -> VerificationTest:
    return VerificationTest(
        test_id=test_id, level="structural", source_requirement="R-1",
        source_acceptance_criterion="A-2", source=None, target=target,
        expected_result="present", observed_result="mismatch",
        evidence=("read-back differs",), timestamp="then",
        verdict=Verdict.FAIL,
    )


def test_first_five_labels_are_stable():
    assert set(FIRST_FIVE) == {
        "wrong-access-vlan", "trunk-vlan-not-allowed", "wrong-gateway",
        "route-missing", "port-shutdown",
    }


def test_port_shutdown_is_detected_and_reverted():
    # the engine correlates down ports on the flow's endpoints, so the
    # fault shuts the source's own interface (the honest observable).
    spec = NetworkSpec.model_validate(SPEC)
    state, fault = _state(), port_shutdown_fault("pc1", "FastEthernet0")
    assert not fault.flow_broken(state)
    token = fault.inject(state)
    try:
        assert fault.flow_broken(state)
        diags = diagnose(spec, _report(_flow_test()), state)
        match = [d for d in diags if fault.expected_cause in d.root_cause]
        assert match, [d.root_cause for d in diags]
        assert match[0].repairable
        assert "no shutdown" in " ".join(
            c for op in match[0].proposed_repair
            for c in op["inputs"].get("commands", [])
        )
    finally:
        fault.revert(state, token)
    assert not fault.flow_broken(state)
    assert state.device("pc1").port("FastEthernet0").state.value == "up"


def test_route_missing_is_detected_and_reverted():
    spec = NetworkSpec.model_validate(SPEC)
    state, fault = _state(), route_missing_fault("r1")
    token = fault.inject(state)
    try:
        assert fault.flow_broken(state)
        diags = diagnose(spec, _report(_flow_test()), state)
        match = [d for d in diags if fault.expected_cause in d.root_cause]
        assert match, [d.root_cause for d in diags]
        assert match[0].repairable
        assert match[0].proposed_repair[0]["inputs"]["device"] == "r1"
    finally:
        fault.revert(state, token)
    assert not fault.flow_broken(state)
    assert state.device("r1").routes.value == [
        {"network": "10.9.9.0", "prefix": 24}]


def test_wrong_access_vlan_is_detected_and_reverted():
    spec = NetworkSpec.model_validate(SPEC)
    state, fault = _state(), wrong_access_vlan_fault("sw1", "FastEthernet0/1")
    token = fault.inject(state)
    try:
        assert fault.flow_broken(state)
        diags = diagnose(
            spec, _report(_struct_test("VT-struct-vlan-l1", "l1")), state)
        match = [d for d in diags if fault.expected_cause in d.root_cause]
        assert match, [d.root_cause for d in diags]
        assert match[0].repairable
    finally:
        fault.revert(state, token)
    assert not fault.flow_broken(state)
    assert state.device("sw1").port("FastEthernet0/1").vlan.value == "1"


def test_trunk_vlan_not_allowed_is_detected_and_reverted():
    spec = NetworkSpec.model_validate(SPEC)
    state, fault = _state(), trunk_vlan_not_allowed_fault("sw1", 20)
    token = fault.inject(state)
    try:
        assert fault.flow_broken(state)
        diags = diagnose(
            spec, _report(_struct_test("VT-struct-trunk-l1", "l1")), state)
        match = [d for d in diags if fault.expected_cause in d.root_cause]
        assert match, [d.root_cause for d in diags]
    finally:
        fault.revert(state, token)
    assert not fault.flow_broken(state)
    assert {"id": 20} in state.device("sw1").vlans.value


def test_wrong_gateway_stays_honestly_unknown_with_repair_blocked():
    """No gateway rule exists on purpose: the correct answer is unknown with
    no repair, never a confident wrong fix."""
    spec = NetworkSpec.model_validate(SPEC)
    state, fault = _state(), wrong_gateway_fault("pc1", "FastEthernet0")
    token = fault.inject(state)
    try:
        assert fault.flow_broken(state)
        diags = diagnose(spec, _report(_flow_test()), state)
        match = [d for d in diags if fault.expected_cause in d.root_cause]
        assert match, [d.root_cause for d in diags]
        assert not match[0].repairable
        assert match[0].proposed_repair == ()
    finally:
        fault.revert(state, token)
    assert state.device("pc1").port("FastEthernet0").gateway.value == "192.168.1.1"


def test_same_failing_flow_without_any_fault_names_no_cause():
    """Control: the causes above come from injected evidence, not thin air."""
    spec = NetworkSpec.model_validate(SPEC)
    diags = diagnose(spec, _report(_flow_test()), _state())
    assert all("interface(s) down" not in d.root_cause for d in diags)
    assert all("is missing on" not in d.root_cause for d in diags)


def test_every_fault_carries_live_commands():
    faults = [
        port_shutdown_fault("r1", "GigabitEthernet0/0"),
        route_missing_fault("r1"),
        wrong_access_vlan_fault("sw1", "FastEthernet0/1"),
        trunk_vlan_not_allowed_fault("sw1"),
        wrong_gateway_fault("pc1", "FastEthernet0"),
    ]
    assert sorted(f.label for f in faults) == sorted(FIRST_FIVE)
    for fault in faults:
        assert fault.live_commands, fault.label
        assert fault.expected_cause, fault.label


def test_inject_on_unknown_device_fails_loudly():
    state = ActualNetworkState(source="test", observed_at="then")
    with pytest.raises(KeyError):
        port_shutdown_fault("ghost", "GigabitEthernet0/0").inject(state)
