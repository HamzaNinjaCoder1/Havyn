"""Verification + diff: evidence contracts, honest unknowns, drift detection."""

from __future__ import annotations

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.core.actual_state import ActualNetworkState, DeviceObservation
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.requirements import FlowOutcome, FlowResult, Verdict
from src.network_autopilot.diff import diff_expected_vs_actual
from src.network_autopilot.ipam import plan_addressing
from src.network_autopilot.verification import VerificationEngine
from tests.autopilot.test_compiler import GOLDEN_SPEC


def _golden():
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)
    plan = plan_addressing(spec)
    return spec, plan, compile_spec(spec, plan).dag


def _link_endpoints(dag) -> dict:
    out = {}
    for node in dag.nodes.values():
        if node.operation.operation_type == "create_link":
            i = node.operation.inputs
            out[i["link_id"]] = (i["a_device"], i["a_port"], i["b_device"], i["b_port"])
    return out


def _observe_full(adapter, spec, dag):
    from src.network_autopilot.compiler.executor import DagExecutor
    from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
    from src.network_autopilot.orchestrator import _populate_link_observations

    executor = DagExecutor(adapter, SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)))
    execution = executor.execute(dag)
    state = executor.state
    state.merge(adapter.observe_state([d.id for d in spec.devices]))
    _populate_link_observations(adapter, _link_endpoints(dag), state)
    return execution, state


def test_full_verify_on_clean_null_build():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    execution, state = _observe_full(adapter, spec, dag)

    engine = VerificationEngine(adapter)
    report = engine.verify(
        spec, plan, state, execution.flow_results,
        link_endpoints=_link_endpoints(dag), observations_complete=True,
    )
    # structural + addressing pass; connectivity is UNKNOWN (no data plane)
    structural = report.by_level("structural")
    assert all(t.verdict is Verdict.PASS for t in structural)
    addressing = report.by_level("addressing")
    assert all(t.verdict is Verdict.PASS for t in addressing)
    connectivity = report.by_level("connectivity")
    assert connectivity and all(t.verdict is Verdict.UNKNOWN for t in connectivity)
    assert not report.passed  # UNKNOWN blocks overall pass — by design
    assert report.summary["unknown"] >= 1


def test_addressing_failures_detected_from_wrong_state():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    adapter.create_device("pc1", "pc")
    # wrong IP configured (mask included so the read-back is fully observable)
    adapter.configure_host("pc1", dhcp=False, ip="10.0.1.99", mask="255.255.255.0", gateway="10.0.1.1")
    state = adapter.observe_state(["pc1"])

    engine = VerificationEngine(adapter)
    report = engine.verify(spec, plan, state, {}, observations_complete=True)
    addr_pc1 = [t for t in report.by_level("addressing") if t.target.startswith("pc1")]
    assert addr_pc1 and all(t.verdict is Verdict.FAIL for t in addr_pc1)
    assert "10.0.1.99" in addr_pc1[0].observed_result


def test_missing_device_is_unknown_on_partial_observation():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    state = ActualNetworkState(source="t", observed_at="now")  # observed nothing
    engine = VerificationEngine(adapter)
    report = engine.verify(spec, plan, state, {}, observations_complete=False)
    device_tests = [
        t for t in report.by_level("structural") if t.test_id.startswith("VT-struct-device-")
    ]
    assert device_tests and all(t.verdict is Verdict.UNKNOWN for t in device_tests)
    assert not report.passed


def test_negative_flow_blocked_passes_when_actually_blocked():
    raw = dict(GOLDEN_SPEC)
    raw["requirements"] = [
        {"id": "R-1", "statement": "pc1 reaches pc4"},
        {"id": "R-2", "statement": "telnet blocked", "kind": "isolation"},
    ]
    raw["acceptance_criteria"] = [
        {
            "id": "A-1",
            "requirement_ids": ["R-1"],
            "test": {"kind": "flow", "source": "pc1", "destination": "pc4", "expect": "reachable"},
        },
        {
            "id": "A-2",
            "requirement_ids": ["R-2"],
            "test": {
                "kind": "flow", "source": "pc1", "destination": "pc4",
                "expect": "blocked", "transport": "tcp", "port": 23,
            },
        },
    ]
    spec = NetworkSpec.model_validate(raw)
    plan = plan_addressing(spec)
    engine = VerificationEngine(NullAdapter())
    blocked = FlowResult(source="pc1", destination="pc4", outcome=FlowOutcome.BLOCKED, transport="tcp", port=23)
    report = engine.verify(
        spec, plan, ActualNetworkState("t", "now"),
        {("pc1", "pc4"): blocked}, observations_complete=True,
    )
    flow_tests = {t.test_id: t for t in report.by_level("connectivity")}
    assert flow_tests["VT-flow-A-2"].verdict is Verdict.PASS
    # an untested negative flow is UNKNOWN, never PASS
    report2 = engine.verify(
        spec, plan, ActualNetworkState("t", "now"), {}, observations_complete=True,
    )
    flow_tests2 = {t.test_id: t for t in report2.by_level("connectivity")}
    assert flow_tests2["VT-flow-A-2"].verdict is Verdict.UNKNOWN


def test_diff_clean_when_state_matches_expectation():
    spec, plan, dag = _golden()
    adapter = NullAdapter()
    _execution, state = _observe_full(adapter, spec, dag)
    diff = diff_expected_vs_actual(spec, plan, state, observations_complete=True)
    drift = [e for e in diff.entries if e.kind.value in ("change", "remove", "add")]
    assert diff.drift_count == 0, [e.to_dict() for e in drift]


def test_diff_change_on_wrong_ip():
    spec, plan, _ = _golden()
    adapter = NullAdapter()
    adapter.create_device("pc1", "pc")
    adapter.configure_host("pc1", dhcp=False, ip="10.0.1.99", mask="255.255.255.0", gateway="10.0.1.1")
    state = adapter.observe_state(["pc1"])
    diff = diff_expected_vs_actual(spec, plan, state, observations_complete=True)
    ip_entries = [e for e in diff.entries if ".ip" in e.path and "pc1" in e.path]
    assert ip_entries and ip_entries[0].kind.value == "change"
    assert ip_entries[0].actual == "10.0.1.99/255.255.255.0"
    assert diff.drift_count >= 1


def test_diff_missing_device_remove_only_on_complete_observation():
    spec, plan, _ = _golden()
    state = ActualNetworkState(source="t", observed_at="now")
    incomplete = diff_expected_vs_actual(spec, plan, state, observations_complete=False)
    device_entries = [e for e in incomplete.entries if e.path.startswith("device(")]
    assert all(e.kind.value == "unknown" for e in device_entries)

    complete = diff_expected_vs_actual(spec, plan, state, observations_complete=True)
    device_entries = [e for e in complete.entries if e.path.startswith("device(")]
    assert all(e.kind.value == "remove" for e in device_entries)


def test_diff_reports_clean_drift_counts():
    spec, plan, _ = _golden()
    diff = diff_expected_vs_actual(
        spec, plan, ActualNetworkState("t", "now"), observations_complete=True
    )
    # 8 devices missing -> 8 removes (+ links unknown/removed)
    assert diff.drift_count >= 8


# -- L2 evidence: access VLAN + trunk membership --------------------------


def _vlan_spec() -> NetworkSpec:
    return NetworkSpec.model_validate({
        "name": "vlan-evidence",
        "devices": [
            {"id": "pc1", "role": "pc"},
            {"id": "s1", "role": "switch"},
            {"id": "r1", "role": "router"},
        ],
        "links": [
            {"id": "l-s1-pc1",
             "a": {"device": "s1", "interface_hint": "FastEthernet0/1"},
             "b": {"device": "pc1"}},
            {"id": "l-s1-r1", "trunk": True,
             "a": {"device": "s1", "interface_hint": "FastEthernet0/24"},
             "b": {"device": "r1", "interface_hint": "GigabitEthernet0/0"}},
        ],
        "subnets": [{"id": "users", "cidr": "10.0.10.0/24", "vlan_id": 10}],
        "ip_assignments": [
            {"device": "r1", "subnet": "users", "ip": "10.0.10.1",
             "interface_hint": "GigabitEthernet0/0.10"},
            {"device": "pc1", "subnet": "users", "ip": "10.0.10.10"},
        ],
        "vlans": [{"vlan_id": 10, "name": "USERS"}],
    })


_VLAN_ENDPOINTS = {
    "l-s1-pc1": ("s1", "FastEthernet0/1", "pc1", "FastEthernet0"),
    "l-s1-r1": ("s1", "FastEthernet0/24", "r1", "GigabitEthernet0/0"),
}


def _switch_state(*, access_vlan: str, trunk: bool, trunk_vlans: str):
    state = ActualNetworkState(source="test", observed_at="now")
    s1 = DeviceObservation(name="s1")
    s1.port("FastEthernet0/1").state = Observable.known("up")
    s1.port("FastEthernet0/1").vlan = Observable.known(access_vlan)
    s1.port("FastEthernet0/1").trunk = Observable.known(False)
    s1.port("FastEthernet0/24").state = Observable.known("up")
    s1.port("FastEthernet0/24").vlan = Observable.known(trunk_vlans)
    s1.port("FastEthernet0/24").trunk = Observable.known(trunk)
    state.observe_device(s1)
    return state


def _verify_l2(state):
    spec = _vlan_spec()
    report = VerificationEngine(NullAdapter()).verify(
        spec, plan_addressing(spec), state, {},
        link_endpoints=_VLAN_ENDPOINTS, observations_complete=True,
    )
    return {t.test_id: t for t in report.by_level("structural")}


def test_an_access_port_in_the_wrong_vlan_fails_its_evidence_test():
    tests = _verify_l2(_switch_state(
        access_vlan="999", trunk=True, trunk_vlans="1,10"))
    vlan = tests["VT-struct-vlan-l-s1-pc1"]
    assert vlan.verdict is Verdict.FAIL
    assert "999" in vlan.observed_result and "VLAN 10" in vlan.expected_result


def test_an_access_port_in_the_right_vlan_passes():
    tests = _verify_l2(_switch_state(
        access_vlan="10", trunk=True, trunk_vlans="1,10"))
    assert tests["VT-struct-vlan-l-s1-pc1"].verdict is Verdict.PASS


def test_a_trunk_missing_a_required_vlan_fails_its_evidence_test():
    tests = _verify_l2(_switch_state(
        access_vlan="10", trunk=True, trunk_vlans="1"))
    trunk = tests["VT-struct-trunk-l-s1-r1"]
    assert trunk.verdict is Verdict.FAIL
    assert "10" in trunk.evidence[1]


def test_a_trunk_carrying_every_vlan_passes():
    tests = _verify_l2(_switch_state(
        access_vlan="10", trunk=True, trunk_vlans="1,10"))
    assert tests["VT-struct-trunk-l-s1-r1"].verdict is Verdict.PASS


def test_unobserved_vlan_state_emits_no_l2_test_rather_than_a_guess():
    spec = _vlan_spec()
    state = ActualNetworkState(source="test", observed_at="now")
    s1 = DeviceObservation(name="s1")
    s1.port("FastEthernet0/1").state = Observable.known("up")
    state.observe_device(s1)
    report = VerificationEngine(NullAdapter()).verify(
        spec, plan_addressing(spec), state, {},
        link_endpoints=_VLAN_ENDPOINTS, observations_complete=True,
    )
    assert not [t for t in report.tests if t.test_id.startswith(
        ("VT-struct-vlan", "VT-struct-trunk")
    )]


def test_the_vlan_test_drives_the_l2_diagnosis_rule():
    from src.network_autopilot.diagnosis import diagnose

    spec = _vlan_spec()
    state = _switch_state(access_vlan="999", trunk=True, trunk_vlans="1,10")
    report = VerificationEngine(NullAdapter()).verify(
        spec, plan_addressing(spec), state, {},
        link_endpoints=_VLAN_ENDPOINTS, observations_complete=True,
    )
    diags = diagnose(spec, report, state)
    assert any(
        "does not report the expected VLAN or trunk state" in d.root_cause
        for d in diags
    ), [d.root_cause for d in diags]
