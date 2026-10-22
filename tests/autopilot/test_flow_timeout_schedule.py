"""S1.6a — adaptive per-attempt ping timeouts (F-7), re-measured 2026-10-16.

A quiet attempt costs its full deadline (measured live: 25 s x 3 attempts on
dead paths — 31 such windows in one reference run). PT answers a WARM path
(ARP already resolved) in well under a second and prints a 0%-loss stat block
within ~4-8 s on dead ones. BUT the first ping on a freshly built cross-fabric
path is COLD: measured single-ping latencies on build-ef996a15797f's canvas
were 14.3 s (pc16->srv1), 16.1 s (pc9->srvisp) and 24.3 s (pc1->pc7) — the
first echo pays ARP resolution and, on a lossy path, per-echo timeouts. The
original schedule's 8 s first window never caught a cold ping, so every flow
in build-ef996a15797f paid a doomed first attempt, was re-armed (discarding the
ARP convergence), and five flows printed no stat block in 8+15+25 s at all.

The first window is therefore sized to cover the measured worst cold ping
(24.3 s + margin); after it the second ping is ARP-warm and cheap, and the
last keeps 25 s patience for a slow console. Verdict stability is still the
contract: the schedule changes the waits, never the outcome — the matrix test
pins every scripted attempt pattern's final verdict across the change.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.adapter.packet_tracer import bridge_adapter as _ba
from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.requirements import FlowOutcome, FlowResult

TEST_TOKEN = "test-token-long-enough-0123456789abcdef"

QUIET = "no new ping statistics appeared"


def _attempt(outcome: FlowOutcome, detail: str = "") -> FlowResult:
    return FlowResult(
        source="pc1", destination="r1", outcome=outcome, detail=detail
    )


def _adapter_with_script(monkeypatch, answers: list[FlowResult]) -> list[float]:
    """Adapter whose _single_ping_flow pops scripted attempts; returns the
    list every call appends its deadline to."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    monkeypatch.setattr(_ba, "_FLOW_PRE_SETTLE_S", 0.0)
    monkeypatch.setattr(_ba, "_FLOW_CONVERGE_SETTLE_S", 0.0)
    calls: list[float] = []

    def fake(source, destination, target, timeout):
        calls.append(timeout)
        return answers.pop(0)

    monkeypatch.setattr(adapter, "_single_ping_flow", fake)
    return adapter, calls  # type: ignore[return-value]


def test_the_first_window_covers_a_cold_ping_and_the_middle_one_shrinks(monkeypatch):
    """Failing-first for the 2026-10-16 re-measurement: the first attempt must
    reserve enough room for a COLD cross-fabric ping (14-24 s measured), not the
    old 8 s that timed out on every freshly built path; the middle attempt is
    ARP-warm and may be short. All three attempts are retryable so the whole
    schedule is observable."""
    adapter, calls = _adapter_with_script(
        monkeypatch,
        [_attempt(FlowOutcome.PARTIAL, "Success rate is 60 percent")] * 4,
    )
    adapter.run_flow_test("pc1", "r1", destination_ip="10.0.0.1")
    # The fourth is the partial-confirmation attempt (same 25 s patience).
    assert calls == [30.0, 15.0, 25.0, 25.0], calls


def test_an_explicit_timeout_still_applies_to_every_attempt(monkeypatch):
    """Callers that pass timeout= (bridge_live_path, scale, contract tests)
    keep the old contract: their value is every attempt's deadline."""
    adapter, calls = _adapter_with_script(
        monkeypatch,
        [_attempt(FlowOutcome.PARTIAL, "Success rate is 60 percent")] * 4,
    )
    adapter.run_flow_test("pc1", "r1", destination_ip="10.0.0.1", timeout=3.0)
    assert calls == [3.0, 3.0, 3.0, 3.0], calls


@pytest.mark.parametrize(
    "script, expected_outcome, expected_attempts",
    [
        (["REACHABLE"], FlowOutcome.REACHABLE, 1),
        (["PARTIAL", "REACHABLE"], FlowOutcome.REACHABLE, 2),
        (["QUIET", "BLOCKED", "QUIET"], FlowOutcome.BLOCKED, 3),
        (["QUIET", "QUIET", "QUIET"], FlowOutcome.UNKNOWN, 3),
        (["BLOCKED", "BLOCKED", "BLOCKED"], FlowOutcome.BLOCKED, 3),
        (["PARTIAL", "PARTIAL", "REACHABLE"], FlowOutcome.REACHABLE, 3),
        # A PARTIAL on the last budgeted attempt earns one confirmation try.
        (["PARTIAL", "PARTIAL", "PARTIAL", "REACHABLE"],
         FlowOutcome.REACHABLE, 4),
    ],
)
def test_verdicts_are_stable_across_attempt_patterns(
    monkeypatch, script, expected_outcome, expected_attempts
):
    """Golden master: whatever the schedule does to the waits, these attempt
    patterns must keep producing these verdicts (blueprint S1.6 step 5 — speed
    may not buy itself with a changed answer)."""
    scripted = []
    for kind in script:
        if kind == "REACHABLE":
            scripted.append(_attempt(FlowOutcome.REACHABLE, "Success rate is 100 percent"))
        elif kind == "PARTIAL":
            scripted.append(_attempt(FlowOutcome.PARTIAL, "Success rate is 60 percent"))
        elif kind == "BLOCKED":
            scripted.append(_attempt(FlowOutcome.BLOCKED, "ping r1 -> 10.0.0.1: none"))
        else:
            scripted.append(_attempt(FlowOutcome.UNKNOWN, f"{QUIET} within 25s"))
    adapter, _ = _adapter_with_script(monkeypatch, scripted)
    result = adapter.run_flow_test("pc1", "r1", destination_ip="10.0.0.1")
    assert result.outcome is expected_outcome, (script, result.outcome, result.detail)
    if expected_attempts > 1:
        assert len(result.evidence["attempts"]) == expected_attempts
    else:
        assert "attempts" not in result.evidence


def test_the_converged_marker_survives_the_schedule_change(monkeypatch):
    """[quiet, BLOCKED, quiet] is the measured dead-path pattern: the best
    attempt is the middle one, so the report keeps its convergence marker and
    the 'none' detail — exactly what the archived reference run printed."""
    adapter, _ = _adapter_with_script(
        monkeypatch,
        [
            _attempt(FlowOutcome.UNKNOWN, f"{QUIET} within 25s"),
            _attempt(FlowOutcome.BLOCKED, "ping r1 -> 10.0.0.1: none"),
            _attempt(FlowOutcome.UNKNOWN, f"{QUIET} within 25s"),
        ],
    )
    result = adapter.run_flow_test("pc1", "r1", destination_ip="10.0.0.1")
    assert result.outcome is FlowOutcome.BLOCKED
    assert result.detail.endswith("(after ARP convergence)"), result.detail
    assert result.detail.startswith("ping r1 -> 10.0.0.1: none"), result.detail
