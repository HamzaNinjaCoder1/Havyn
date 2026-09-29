"""S1.10 / F7: the "route missing" diagnosis rule.

A flow that fails while the destination subnet is absent from the observed
route table has a specific, evidence-backed cause: the router is missing the
route the spec asked for. The repair must re-push that spec route, not guess.
"""

from __future__ import annotations

import copy

from src.network_autopilot.adapter import NullAdapter
from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.core.actual_state import (
    ActualNetworkState,
    DeviceObservation,
    PortObservation,
)
from src.network_autopilot.core.ids import utc_now_iso
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.requirements import (
    AcceptanceReport,
    Verdict,
)
from src.network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from src.network_autopilot.diagnosis import diagnose, fill_missing_routes
from src.network_autopilot.ipam import plan_addressing
from src.network_autopilot.repair import RepairEngine
from src.network_autopilot.verification import VerificationEngine
from src.network_autopilot.verification.engine import (
    VerificationReport,
    VerificationTest,
)
from tests.autopilot.test_compiler import GOLDEN_SPEC
from tests.autopilot.test_diagnosis_repair import _endpoints, _observe_full


def _flow_fail_report(source: str, target: str, observed: str) -> VerificationReport:
    test = VerificationTest(
        test_id="VT-flow-A-1",
        level="connectivity",
        source_requirement=None,
        source_acceptance_criterion=None,
        source=source,
        target=target,
        expected_result="reachable",
        observed_result=observed,
        evidence=(f"flow {source}->{target}: {observed}",),
        timestamp=utc_now_iso(),
        verdict=Verdict.FAIL,
    )
    return VerificationReport(
        tests=(test,),
        summary={Verdict.FAIL.value: 1},
        acceptance=AcceptanceReport((), (), {}, None),
        observations_complete=True,
    )


def _state_with_routes(state, routes_by_device: dict[str, list[dict]]):
    for name, routes in routes_by_device.items():
        device = state.devices.get(name)
        if device is None:
            device = DeviceObservation(name=name)
            state.observe_device(device)
        device.routes = Observable.known(routes, source="test")
    return state


def test_missing_route_is_diagnosed_and_repaired():
    """RED before the fix: a flow fails because the router's route table lacks
    the destination subnet, and diagnosis has no rule for it."""
    spec = NetworkSpec.model_validate(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    dag = compile_spec(spec, plan).dag
    adapter = NullAdapter()
    _execution, state = _observe_full(adapter, spec, dag)

    # r1's route table is observed and does NOT carry net-lan2 (10.0.2.0/24),
    # which the spec's static route asks for.
    state = _state_with_routes(state, {
        "r1": [
            {"kind": "connected", "network": "10.0.0.0", "prefix": 30,
             "next_hop": None, "interface": "GigabitEthernet0/0"},
            {"kind": "connected", "network": "10.0.1.0", "prefix": 24,
             "next_hop": None, "interface": "GigabitEthernet0/1"},
        ],
    })

    report = _flow_fail_report("pc1", "pc4", "blocked")
    diagnoses = diagnose(spec, report, state)

    route_diags = [
        d for d in diagnoses
        if "route" in d.root_cause.lower() and "r1" in d.affected_resources
    ]
    assert route_diags, [d.to_dict() for d in diagnoses]
    d = route_diags[0]
    assert d.repairable
    proposal = d.proposed_repair[0]
    assert proposal["operation_type"] == "ios_command"
    assert proposal["inputs"]["device"] == "r1"
    assert any(
        "ip route 10.0.2.0 255.255.255.0 10.0.0.2" in str(c)
        for c in proposal["inputs"]["commands"]
    ), proposal["inputs"]


def test_present_route_is_not_diagnosed_as_missing():
    """Pin: when the observed route table DOES carry the destination subnet,
    the route-missing rule must stay silent."""
    spec = NetworkSpec.model_validate(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    dag = compile_spec(spec, plan).dag
    adapter = NullAdapter()
    _execution, state = _observe_full(adapter, spec, dag)

    state = _state_with_routes(state, {
        "r1": [
            {"kind": "connected", "network": "10.0.0.0", "prefix": 30,
             "next_hop": None, "interface": "GigabitEthernet0/0"},
            {"kind": "connected", "network": "10.0.1.0", "prefix": 24,
             "next_hop": None, "interface": "GigabitEthernet0/1"},
            {"kind": "static", "network": "10.0.2.0", "prefix": 24,
             "next_hop": "10.0.0.2", "interface": "GigabitEthernet0/0"},
        ],
    })

    report = _flow_fail_report("pc1", "pc4", "blocked")
    diagnoses = diagnose(spec, report, state)

    route_diags = [
        d for d in diagnoses
        if "route" in d.root_cause.lower() and "r1" in d.affected_resources
    ]
    assert route_diags == [], [d.to_dict() for d in route_diags]


def test_unreadable_route_table_is_not_diagnosed():
    """Pin: an UNKNOWN route table is not evidence of a missing route - the
    rule must stay silent rather than guess."""
    spec = NetworkSpec.model_validate(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    dag = compile_spec(spec, plan).dag
    adapter = NullAdapter()
    _execution, state = _observe_full(adapter, spec, dag)

    device = DeviceObservation(name="r1")
    device.routes = Observable[list].unknown("console unreadable")
    state.observe_device(device)

    report = _flow_fail_report("pc1", "pc4", "blocked")
    diagnoses = diagnose(spec, report, state)

    route_diags = [
        d for d in diagnoses
        if "route" in d.root_cause.lower() and "r1" in d.affected_resources
    ]
    assert route_diags == [], [d.to_dict() for d in route_diags]


# -- S1.11 / F7 wiring: observed tables reach the rule ----------------------


def _golden_state_and_spec():
    spec = NetworkSpec.model_validate(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    dag = compile_spec(spec, plan).dag
    adapter = NullAdapter()
    _execution, state = _observe_full(adapter, spec, dag)
    return spec, adapter, state


def _table_without_lan2():
    return [
        {"kind": "connected", "network": "10.0.0.0", "prefix": 30,
         "next_hop": None, "interface": "GigabitEthernet0/0"},
        {"kind": "connected", "network": "10.0.1.0", "prefix": 24,
         "next_hop": None, "interface": "GigabitEthernet0/1"},
    ]


def test_fill_missing_routes_reads_only_route_bearing_devices():
    """The fill touches exactly the devices the spec gives static routes to,
    and marks their tables KNOWN so the rule has evidence to weigh."""
    spec, _adapter, state = _golden_state_and_spec()
    calls: list[str] = []

    def reader(device: str):
        calls.append(device)
        return Observable.known(_table_without_lan2(), source="test")

    filled = fill_missing_routes(spec, state, reader)

    assert calls == ["r1", "r2"], calls
    assert filled == 2, filled
    assert state.devices["r1"].routes.is_known
    assert state.devices["r2"].routes.is_known


def test_route_missing_rule_fires_after_the_fill():
    """Chain: a fresh state (routes never observed) plus a filled table that
    LACKS the destination makes the rule reachable end to end."""
    spec, _adapter, state = _golden_state_and_spec()
    filled = fill_missing_routes(
        spec, state, lambda device: Observable.known(_table_without_lan2(), source="test")
    )
    assert filled == 2

    report = _flow_fail_report("pc1", "pc4", "blocked")
    diagnoses = diagnose(spec, report, state)

    route_diags = [
        d for d in diagnoses
        if "route" in d.root_cause.lower() and "r1" in d.affected_resources
    ]
    assert route_diags, [d.to_dict() for d in diagnoses]


def test_fill_with_a_complete_table_keeps_the_rule_silent():
    """Chain pin: when the filled table DOES carry the destination, the rule
    must not fire — the fill must not manufacture a diagnosis."""
    spec, _adapter, state = _golden_state_and_spec()

    def complete(device: str):
        routes = _table_without_lan2()
        if device == "r1":
            routes.append(
                {"kind": "static", "network": "10.0.2.0", "prefix": 24,
                 "next_hop": "10.0.0.2", "interface": "GigabitEthernet0/0"}
            )
        return Observable.known(routes, source="test")

    assert fill_missing_routes(spec, state, complete) == 2

    report = _flow_fail_report("pc1", "pc4", "blocked")
    diagnoses = diagnose(spec, report, state)

    route_diags = [
        d for d in diagnoses
        if "route" in d.root_cause.lower() and "r1" in d.affected_resources
    ]
    assert route_diags == [], [d.to_dict() for d in route_diags]


def test_fill_without_a_usable_reader_leaves_the_rule_silent():
    """No reader, a reader that raises (dead channel), or an adapter that
    cannot read routes at all (NullAdapter's contract default): the field
    stays NOT_OBSERVED and diagnosis never guesses (rule 7)."""
    spec, adapter, state = _golden_state_and_spec()

    def boom(device: str):
        raise RuntimeError("channel dead")

    assert fill_missing_routes(spec, state, None) == 0
    assert fill_missing_routes(spec, state, boom) == 0
    assert fill_missing_routes(spec, state, adapter.read_routing_table) == 0
    assert not state.devices["r1"].routes.is_known
    assert not state.devices["r2"].routes.is_known

    report = _flow_fail_report("pc1", "pc4", "blocked")
    diagnoses = diagnose(spec, report, state)

    route_diags = [
        d for d in diagnoses
        if "route" in d.root_cause.lower() and "r1" in d.affected_resources
    ]
    assert route_diags == [], [d.to_dict() for d in route_diags]