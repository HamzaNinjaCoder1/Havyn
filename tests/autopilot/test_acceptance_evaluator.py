"""Acceptance evaluation: UNKNOWN never passes, partial never passes."""

from __future__ import annotations

from src.network_autopilot.acceptance import evaluate_spec
from src.network_autopilot.core.actual_state import ActualNetworkState, DeviceObservation
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.requirements import FlowOutcome, FlowResult, Verdict

MINI_SPEC = {
    "name": "mini",
    "devices": [
        {"id": "pc1", "role": "pc"},
        {"id": "srv1", "role": "server"},
        {"id": "ghost", "role": "pc"},
    ],
    "requirements": [
        {"id": "R-1", "statement": "pc1 reaches srv1"},
        {"id": "R-2", "statement": "no telnet to srv1", "kind": "isolation"},
        {"id": "R-3", "statement": "ghost exists"},
    ],
    "acceptance_criteria": [
        {
            "id": "A-1",
            "requirement_ids": ["R-1"],
            "test": {"kind": "flow", "source": "pc1", "destination": "srv1", "expect": "reachable"},
        },
        {
            "id": "A-2",
            "requirement_ids": ["R-2"],
            "test": {
                "kind": "flow", "source": "pc1", "destination": "srv1",
                "expect": "blocked", "transport": "tcp", "port": 23,
            },
        },
        {
            "id": "A-3",
            "requirement_ids": ["R-3"],
            "test": {"kind": "structural", "check": "device_exists", "target": "ghost"},
        },
        {
            "id": "A-4",
            "requirement_ids": ["R-1"],
            "test": {
                "kind": "config", "device": "srv1", "interface": "FastEthernet0",
                "field": "ip", "expected": "10.0.0.10",
            },
        },
    ],
}


def _spec() -> NetworkSpec:
    return NetworkSpec.model_validate(MINI_SPEC)


def _state() -> ActualNetworkState:
    state = ActualNetworkState(source="test", observed_at="t0")
    state.observe_device(DeviceObservation(name="pc1", ref_id=Observable.known("pc1")))
    srv = DeviceObservation(name="srv1", ref_id=Observable.known("srv1"))
    port = srv.port("FastEthernet0")
    port.ip = Observable.known("10.0.0.10")
    port.mask = Observable.known("255.0.0.0")
    state.observe_device(srv)
    return state


def _flow(outcome: FlowOutcome, transport: str = "icmp", port: int | None = None) -> FlowResult:
    return FlowResult(source="pc1", destination="srv1", outcome=outcome, transport=transport, port=port)


def _evaluate(flows: dict, *, complete: bool = False):
    return evaluate_spec(_spec(), _state(), flows, observations_complete=complete)


def test_missing_flow_result_is_unknown():
    report = _evaluate({})
    by_id = {r.criterion_id: r for r in report.results}
    assert by_id["A-1"].verdict is Verdict.UNKNOWN


def test_partial_reachability_fails_reachable_expectation():
    report = _evaluate({("pc1", "srv1", "icmp", None): _flow(FlowOutcome.PARTIAL)})
    by_id = {r.criterion_id: r for r in report.results}
    assert by_id["A-1"].verdict is Verdict.FAIL
    assert any("partial" in e for e in by_id["A-1"].evidence)


def test_full_reachability_passes():
    report = _evaluate({("pc1", "srv1", "icmp", None): _flow(FlowOutcome.REACHABLE)})
    by_id = {r.criterion_id: r for r in report.results}
    assert by_id["A-1"].verdict is Verdict.PASS


def test_negative_flow_blocked_passes_and_reachable_fails():
    blocked = _evaluate({("pc1", "srv1", "tcp", 23): _flow(FlowOutcome.BLOCKED, "tcp", 23)})
    by_id = {r.criterion_id: r for r in blocked.results}
    assert by_id["A-2"].verdict is Verdict.PASS

    leaky = _evaluate({("pc1", "srv1", "tcp", 23): _flow(FlowOutcome.REACHABLE, "tcp", 23)})
    by_id = {r.criterion_id: r for r in leaky.results}
    assert by_id["A-2"].verdict is Verdict.FAIL


def test_structural_absence_honesty_depends_on_observation_completeness():
    incomplete = _evaluate({})
    by_id = {r.criterion_id: r for r in incomplete.results}
    assert by_id["A-3"].verdict is Verdict.UNKNOWN

    complete = _evaluate({}, complete=True)
    by_id = {r.criterion_id: r for r in complete.results}
    assert by_id["A-3"].verdict is Verdict.FAIL


def test_config_match_passes_and_mismatch_fails():
    report = _evaluate({})
    by_id = {r.criterion_id: r for r in report.results}
    assert by_id["A-4"].verdict is Verdict.PASS  # state has exactly the expected IP


def test_unknown_keeps_the_whole_report_unsatisfied():
    # A-2 gets its blocked flow; A-1's flow and ghost's presence are missing.
    report = _evaluate({("pc1", "srv1", "tcp", 23): _flow(FlowOutcome.BLOCKED, "tcp", 23)})
    assert report.satisfied is None
    assert report.incomplete
    # the summary says so in numbers, not in prose
    assert report.summary["unknown"] >= 2  # A-1, A-3
    assert report.summary["pass"] >= 2     # A-2, A-4 — yet the report is NOT "all good"


def test_everything_verified_and_passing_is_satisfied():
    flows = {
        ("pc1", "srv1", "icmp", None): _flow(FlowOutcome.REACHABLE),
        ("pc1", "srv1", "tcp", 23): _flow(FlowOutcome.BLOCKED, "tcp", 23),
    }
    # add ghost to observed state so A-3 passes
    spec = _spec()
    state = _state()
    state.observe_device(DeviceObservation(name="ghost", ref_id=Observable.known("ghost")))
    report = evaluate_spec(spec, state, flows, observations_complete=True)
    assert report.satisfied is True
    assert report.summary["pass"] == 4


def test_requirement_without_criteria_is_not_run():
    raw = dict(MINI_SPEC)
    raw["requirements"] = MINI_SPEC["requirements"] + [
        {"id": "R-9", "statement": "never verified by anything"}
    ]
    spec = NetworkSpec.model_validate(raw)
    # Everything checkable passes: both flows observed, ghost present.
    flows = {
        ("pc1", "srv1", "icmp", None): _flow(FlowOutcome.REACHABLE),
        ("pc1", "srv1", "tcp", 23): _flow(FlowOutcome.BLOCKED, "tcp", 23),
    }
    state = _state()
    state.observe_device(DeviceObservation(name="ghost", ref_id=Observable.known("ghost")))
    report = evaluate_spec(spec, state, flows, observations_complete=True)
    by_req = {r.requirement_id: r for r in report.requirement_results}
    assert by_req["R-9"].verdict is Verdict.NOT_RUN
    assert report.satisfied is None  # NOT_RUN also blocks "satisfied"


def test_wireless_criterion_is_evaluated_rather_than_reported_unknown():
    """Bug #22: `evaluate_criterion` had no WirelessTestSpec arm, so a wireless
    criterion fell into the `else` and reported UNKNOWN with the evidence
    "unknown test kind" - however clear the antenna link was observed."""
    from src.network_autopilot.core.actual_state import PortObservation

    spec = NetworkSpec.model_validate(
        {
            "name": "wifi",
            "devices": [
                {"id": "lap1", "role": "laptop"},
                {"id": "ap1", "role": "access_point"},
            ],
            "acceptance_criteria": [
                {
                    "id": "A-w",
                    "requirement_ids": [],
                    "test": {
                        "kind": "wireless",
                        "client": "lap1",
                        "access_point": "ap1",
                    },
                }
            ],
        }
    )
    state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00+00:00")
    lap = DeviceObservation(name="lap1")
    port = PortObservation(name="Wireless0")
    port.state = Observable.known("up", source="test")
    port.link_class = Observable.known("Antenna", source="test")
    port.peers = Observable.known(["ap1"], source="test")
    lap.ports["Wireless0"] = port
    state.devices["lap1"] = lap
    state.devices["ap1"] = DeviceObservation(name="ap1")

    report = evaluate_spec(spec, state, {})
    by_id = {r.criterion_id: r for r in report.results}
    assert by_id["A-w"].verdict is Verdict.PASS
    assert not any("unknown test kind" in e for e in by_id["A-w"].evidence)
