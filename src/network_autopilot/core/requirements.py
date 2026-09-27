"""Verdict semantics for requirements and acceptance criteria.

The contract this module enforces: the final report may say PASS, FAIL or
UNKNOWN — and UNKNOWN is never silently promoted to PASS. A criterion that
could not be checked (no observation, no flow result, unenforceable policy)
stays UNKNOWN, and any requirement with an UNKNOWN criterion reports UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """Verdict of an acceptance criterion or an aggregated requirement."""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"      # could not be verified — never counts as PASS
    NOT_RUN = "not_run"      # verification not attempted (e.g. dry-run)


class FlowOutcome(str, Enum):
    """What was actually observed for one traffic flow."""

    REACHABLE = "reachable"
    BLOCKED = "blocked"
    PARTIAL = "partial"      # some replies, some loss — never reported as healthy
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FlowResult:
    """The observed outcome of one flow test, with its evidence."""

    source: str
    destination: str
    outcome: FlowOutcome
    transport: str = "icmp"
    port: int | None = None
    detail: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


# Ranking used to pick the honest verdict out of repeated flow attempts. PT
# (like real IOS) drops the first pings across a multi-hop path while ARP
# resolves hop by hop, so a healthy transit network answers blocked -> partial
# -> reachable on successive attempts; the converged attempt is the truthful
# one. UNKNOWN/ERROR rank below everything: a test that could not run is never
# promoted over a real observation.
_FLOW_RANK: dict[FlowOutcome, int] = {
    FlowOutcome.REACHABLE: 3,
    FlowOutcome.PARTIAL: 2,
    FlowOutcome.BLOCKED: 1,
    FlowOutcome.ERROR: 0,
    FlowOutcome.UNKNOWN: 0,
}


def best_flow_of(attempts: list[FlowResult]) -> FlowResult:
    """The most favourable honest outcome across repeated flow attempts.

    Returns the best-ranked attempt, preferring the LAST one among equal ranks
    (the most recent observation wins a tie, which is what matters when the
    network is still converging). An empty list is a programming error.
    """
    if not attempts:
        raise ValueError("best_flow_of needs at least one attempt")
    best = attempts[0]
    for attempt in attempts[1:]:
        if _FLOW_RANK[attempt.outcome] >= _FLOW_RANK[best.outcome]:
            best = attempt
    return best


@dataclass(frozen=True)
class AcceptanceResult:
    """Verdict of one acceptance criterion."""

    criterion_id: str
    requirement_ids: tuple[str, ...]
    verdict: Verdict
    evidence: tuple[str, ...] = ()
    checked_at: str | None = None
    checked_by: str | None = None


@dataclass(frozen=True)
class RequirementResult:
    """Aggregated verdict of one requirement over its criteria."""

    requirement_id: str
    verdict: Verdict
    criterion_ids: tuple[str, ...]


@dataclass(frozen=True)
class AcceptanceReport:
    """The requirement→verdict report for a whole spec evaluation."""

    results: tuple[AcceptanceResult, ...]
    requirement_results: tuple[RequirementResult, ...]
    summary: dict[str, int]
    satisfied: bool | None  # None when any criterion is UNKNOWN: unknown ≠ satisfied

    @property
    def incomplete(self) -> bool:
        return self.satisfied is None


def aggregate_verdicts(verdicts: list[Verdict]) -> Verdict:
    """Worst-case aggregation with UNKNOWN preserved.

    FAIL dominates; otherwise any UNKNOWN dominates; otherwise any NOT_RUN
    dominates; PASS only when everything actually passed.
    """
    if not verdicts:
        return Verdict.NOT_RUN
    if any(v is Verdict.FAIL for v in verdicts):
        return Verdict.FAIL
    if any(v is Verdict.UNKNOWN for v in verdicts):
        return Verdict.UNKNOWN
    if any(v is Verdict.NOT_RUN for v in verdicts):
        return Verdict.NOT_RUN
    return Verdict.PASS


def summarize(results: list[AcceptanceResult]) -> dict[str, int]:
    summary = {v.value: 0 for v in Verdict}
    for result in results:
        summary[result.verdict.value] += 1
    return summary
