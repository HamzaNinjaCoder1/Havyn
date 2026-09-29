"""Overnight runner (S2.2 / havyn-runner): a queue of specs, built unattended.

    python scripts/havyn_runner.py queue.json --results-dir runner_results
        [--adapter null|bridge] [--evidence-dir autopilot_evidence]
        [--prune-every 10] [--keep-evidence 20]

Queue file: {"cases": [{"id": "T01", "spec": "library/T01-....json",
"safety_mode": "autonomous"}]}. Every case owns its result JSON
(`results/<id>.json`), so a crash or restart resumes where it stopped —
finished cases are never rebuilt.

Per case, in order: clean the workspace (protected devices survive; if the
workspace cannot be cleaned the run STOPS, it never builds on a dirty
canvas) → build + verify → small result JSON. Every `prune_every` cases the
local evidence is pruned and, when an uploader is configured, results are
uploaded; the upload outcome is recorded honestly (configured or not).

Exit codes: 0 every case passed · 1 engine error · 2 one or more cases
failed/blocked · 4 Packet Tracer unreachable (bridge mode).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CASES_FAILED = 2
EXIT_UNREACHABLE = 4

TERMINAL_STATUSES = ("pass", "fail", "error", "blocked")


@dataclass
class CaseResult:
    """One case's outcome, JSON-serialisable."""

    case_id: str
    spec: str
    status: str  # pass | fail | error | blocked | skipped-clean
    criteria: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    build_id: str | None = None
    detail: str | None = None
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "spec": self.spec,
            "status": self.status,
            "criteria": dict(self.criteria),
            "elapsed_s": round(self.elapsed_s, 1),
            "build_id": self.build_id,
            "detail": self.detail,
            "finished_at": self.finished_at,
        }


@dataclass
class RunnerSummary:
    """The whole queue run."""

    total: int = 0
    ran: int = 0
    resumed: int = 0
    passed: int = 0
    failed: int = 0
    stopped: str | None = None
    uploads: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "ran": self.ran,
            "resumed": self.resumed,
            "passed": self.passed,
            "failed": self.failed,
            "stopped": self.stopped,
            "uploads": list(self.uploads),
        }


def _utc_now() -> str:
    from network_autopilot.core.ids import utc_now_iso

    return utc_now_iso()


def _result_path(results_dir: Path, case_id: str) -> Path:
    return results_dir / f"{case_id}.json"


def load_queue(queue_path: Path) -> list[dict[str, Any]]:
    data = json.loads(Path(queue_path).read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"queue {queue_path} has no cases")
    for case in cases:
        if not case.get("id") or not case.get("spec"):
            raise ValueError(f"queue case needs id + spec: {case!r}")
    return cases


def _criteria_summary(report) -> dict[str, int]:
    summary = report.verification.summary if report else {}
    total = len(report.verification.tests) if report else 0
    return {
        "pass": int(summary.get("pass", 0)),
        "fail": int(summary.get("fail", 0)),
        "unknown": int(summary.get("unknown", 0)),
        "total": total,
    }


def run_case(case: dict[str, Any], adapter, results_dir: Path,
             evidence_root: Path, keep_evidence: int) -> CaseResult:
    """Clean → build → verify one case. Never raises for case-level faults."""
    from network_autopilot.core.build_errors import PreflightBlocked
    from network_autopilot.core.network_spec import NetworkSpec
    from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
    from network_autopilot.evidence.store import EvidenceStore
    from network_autopilot.orchestrator import Autopilot
    from scripts.pt_clean_workspace import clean

    case_id = str(case["id"])
    t0 = time.monotonic()
    try:
        spec = NetworkSpec.from_json(
            Path(case["spec"]).read_text(encoding="utf-8")
        )
    except Exception as exc:
        return CaseResult(case_id, str(case["spec"]), "error",
                          finished_at=_utc_now(),
                          detail=f"spec unreadable: {exc}",
                          elapsed_s=time.monotonic() - t0)

    cleaning = clean(adapter)
    if cleaning.read_failed or cleaning.failed or cleaning.remaining:
        return CaseResult(
            case_id, str(case["spec"]), "blocked",
            finished_at=_utc_now(),
            detail=(
                "workspace could not be cleaned: "
                f"read_failed={cleaning.read_failed} "
                f"failed={sorted(cleaning.failed)} "
                f"remaining={sorted(cleaning.remaining)}"
            ),
            elapsed_s=time.monotonic() - t0,
        )

    gate = SafetyGate(
        SafetyMode(case.get("safety_mode", "autonomous")), MutationBudget()
    )
    evidence = EvidenceStore(root=evidence_root)
    try:
        report, error = Autopilot(
            adapter, gate, evidence, keep_evidence=keep_evidence
        ).build(spec)
    except Exception as exc:  # an engine crash is a case error, not a runner crash
        return CaseResult(case_id, str(case["spec"]), "error",
                          finished_at=_utc_now(),
                          detail=f"engine crash: {type(exc).__name__}: {exc}",
                          elapsed_s=time.monotonic() - t0)
    elapsed = time.monotonic() - t0
    if error is not None:
        status = "blocked" if isinstance(error, PreflightBlocked) else "error"
        return CaseResult(case_id, str(case["spec"]), status,
                          finished_at=_utc_now(), detail=str(error)[:500],
                          elapsed_s=elapsed)
    assert report is not None
    return CaseResult(
        case_id, str(case["spec"]),
        "pass" if report.success else "fail",
        criteria=_criteria_summary(report),
        elapsed_s=elapsed,
        build_id=report.build_id,
        finished_at=_utc_now(),
    )


def run_queue(
    cases: list[dict[str, Any]],
    adapter_factory: Callable[[], Any],
    results_dir: Path,
    evidence_root: Path,
    *,
    keep_evidence: int = 20,
    prune_every: int = 10,
    uploader: Callable[[Path], dict[str, Any]] | None = None,
) -> RunnerSummary:
    """Run the queue with crash-resume. Finished cases are skipped by id."""
    from network_autopilot.evidence.store import prune_evidence

    summary = RunnerSummary(total=len(cases))
    results_dir.mkdir(parents=True, exist_ok=True)
    since_upload = 0
    for case in cases:
        path = _result_path(results_dir, str(case["id"]))
        if path.exists():
            try:
                prior = json.loads(path.read_text(encoding="utf-8"))
                if prior.get("status") in TERMINAL_STATUSES:
                    summary.resumed += 1
                    if prior.get("status") == "pass":
                        summary.passed += 1
                    else:
                        summary.failed += 1
                    continue
            except (ValueError, OSError):
                pass  # a torn write is not a result; re-run the case
        adapter = adapter_factory()
        result = run_case(case, adapter, results_dir, evidence_root,
                          keep_evidence)
        summary.ran += 1
        if result.status == "pass":
            summary.passed += 1
        else:
            summary.failed += 1
        path.write_text(json.dumps(result.to_dict(), indent=1), encoding="utf-8")
        if result.status == "blocked" and (result.detail or "").startswith(
            "workspace"
        ):
            summary.stopped = result.detail
            break
        since_upload += 1
        if since_upload >= prune_every:
            prune_evidence(evidence_root, keep=keep_evidence)
            summary.uploads.append(_maybe_upload(uploader, results_dir))
            since_upload = 0
    if since_upload and (summary.ran or summary.resumed):
        summary.uploads.append(_maybe_upload(uploader, results_dir))
    (results_dir / "summary.json").write_text(
        json.dumps(summary.to_dict(), indent=1), encoding="utf-8"
    )
    return summary


def _maybe_upload(uploader, results_dir: Path) -> dict[str, Any]:
    if uploader is None:
        return {"uploaded": False, "reason": "no uploader configured"}
    try:
        outcome = uploader(results_dir)
        return {"uploaded": bool(outcome.get("uploaded", True)),
                "detail": str(outcome.get("detail", ""))[:300]}
    except Exception as exc:
        return {"uploaded": False,
                "reason": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="havyn-runner", description="Build a queue of specs unattended.")
    parser.add_argument("queue", help="queue JSON file")
    parser.add_argument("--results-dir", default="runner_results")
    parser.add_argument("--evidence-dir", default="autopilot_evidence")
    parser.add_argument("--adapter", default="null", choices=("null", "bridge"))
    parser.add_argument("--prune-every", type=int, default=10)
    parser.add_argument("--keep-evidence", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        cases = load_queue(Path(args.queue))
    except (OSError, ValueError) as exc:
        print(f"bad queue: {exc}", file=sys.stderr)
        return EXIT_ERROR

    def _factory():
        if args.adapter == "null":
            from network_autopilot.adapter import NullAdapter

            return NullAdapter()
        from network_autopilot.adapter.packet_tracer.bridge_adapter import (
            BridgeAdapter,
        )
        from network_autopilot.pt.mailbox import FileMailbox

        return BridgeAdapter(host_bridge=False, file_bridge=FileMailbox())

    if args.adapter == "bridge":
        probe_adapter = _factory()
        if not probe_adapter.ping(timeout=5.0).reachable:
            print("Packet Tracer is not reachable.", file=sys.stderr)
            return EXIT_UNREACHABLE

    summary = run_queue(
        cases, _factory, Path(args.results_dir), Path(args.evidence_dir),
        keep_evidence=args.keep_evidence, prune_every=args.prune_every,
    )
    print(json.dumps(summary.to_dict(), indent=1))
    if summary.stopped:
        print(f"STOPPED: {summary.stopped}", file=sys.stderr)
        return EXIT_CASES_FAILED
    if summary.failed:
        return EXIT_CASES_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
