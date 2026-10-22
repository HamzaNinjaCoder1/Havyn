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

#: ``python scripts/havyn_runner.py`` (the nightly .ps1 does exactly this) puts
#: ``scripts/`` -- not the repo root -- on ``sys.path``, so the sibling import
#: ``scripts.pt_clean_workspace`` raises ModuleNotFoundError and the run dies
#: before the first build. Put the repo root back on the path. Under pytest the
#: root is already present (pyproject ``pythonpath = [".", "src"]``), so this is
#: a no-op there.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CASES_FAILED = 2
EXIT_UNREACHABLE = 4

#: Stamped into every result so a stored verdict can be attributed to the
#: runner generation that produced it (the result schema is a contract).
RUNNER_VERSION = "s2.2"

TERMINAL_STATUSES = ("pass", "fail", "error", "blocked")

#: Every execution is also appended here, so "same-network consistency" (S2.4)
#: has repeated observations to compare. The per-case JSON is the current
#: verdict and is replaced on a re-run; this file is the history that a
#: re-run must not erase.
HISTORY_FILE = "history.jsonl"


@dataclass
class CaseResult:
    """One case's outcome, JSON-serialisable.

    The field set is the runner's contract with everything downstream (the
    scorecard S2.4, the capability map S2.6, a human at 8am). A result that
    cannot say WHICH topology it was, WHICH run produced it, whether it was
    live or offline, and where its evidence lives is a result nobody can act
    on — so those are first-class fields, not something a reader has to
    reconstruct from the file path.

    ``status`` is the verdict and is never upgraded: ``refused`` and
    ``pending`` are separate booleans beside it rather than extra statuses,
    because "blocked" already means different things (preflight refusal vs a
    dirty workspace) and collapsing those into one word is how a refusal
    starts reading as a failure of the network.
    """

    case_id: str
    spec: str
    status: str  # pass | fail | error | blocked | skipped-clean
    topology_id: str | None = None
    run_id: str = ""
    mode: str = "offline"  # live | offline
    started_at: str = ""
    criteria: dict[str, int] = field(default_factory=dict)
    #: Per-criterion verdicts (`VT-...`/`A-...` -> pass|fail|unknown), so a
    #: failure can be read without opening the evidence store.
    criteria_detail: dict[str, str] = field(default_factory=dict)
    elapsed_s: float = 0.0
    build_id: str | None = None
    evidence_dir: str | None = None
    detail: str | None = None
    #: True when the engine refused to act (rule 9) — a decision, not a fault.
    refused: bool = False
    refusal_reason: str | None = None
    #: True when the case was cut short by a deadline rather than answered.
    timed_out: bool = False
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        failed = sorted(
            key for key, verdict in self.criteria_detail.items()
            if verdict in ("fail", "not_run")
        )
        return {
            "case_id": self.case_id,
            "topology_id": self.topology_id,
            "spec": self.spec,
            "run_id": self.run_id,
            "runner_version": RUNNER_VERSION,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": round(self.elapsed_s, 1),
            "criteria": dict(self.criteria),
            "criteria_detail": dict(self.criteria_detail),
            "failures": failed,
            "build_id": self.build_id,
            "evidence_dir": self.evidence_dir,
            "detail": self.detail,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "timed_out": self.timed_out,
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


def new_id(prefix: str) -> str:
    from network_autopilot.core.ids import new_id as _new_id

    return _new_id(prefix)


def _result_path(results_dir: Path, case_id: str) -> Path:
    return results_dir / f"{case_id}.json"


def append_history(results_dir: Path, result: CaseResult) -> None:
    """Append one completed execution to the append-only run history.

    Written after the per-case result so a torn history line can never be
    the only record of a case: the result JSON first, then history. A
    crash between the two loses one history entry (consistency then has one
    fewer sample), never a verdict.
    """
    with (results_dir / HISTORY_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")


def load_queue(queue_path: Path) -> list[dict[str, Any]]:
    # utf-8-sig: PowerShell's Set-Content -Encoding UTF8 writes a BOM on
    # Windows PowerShell 5.1, and a BOM-prefixed JSON file is still a valid
    # queue to every human who opens it. Rejecting it was a launch-time failure
    # with no case result to explain it.
    data = json.loads(Path(queue_path).read_text(encoding="utf-8-sig"))
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


def _criteria_detail(report) -> dict[str, str]:
    """Per-criterion verdicts, keyed by the acceptance criterion where known.

    The report's tests are what a reader greps for in the evidence store, but
    the criterion is what the spec promised, so a test that belongs to one is
    filed under it. A criterion backed by several tests takes the worst of
    them (FAIL over UNKNOWN over PASS) — the honest summary, and one that
    never upgrades a verdict this runner did not observe.
    """
    if report is None:
        return {}
    detail: dict[str, str] = {}
    for test in report.verification.tests:
        verdict = getattr(test.verdict, "value", str(test.verdict))
        key = test.source_acceptance_criterion or test.test_id
        previous = detail.get(key)
        if previous is None or _verdict_rank(verdict) > _verdict_rank(previous):
            detail[key] = verdict
    return detail


#: Ordering for collapsing several tests onto one criterion. Higher wins.
_VERDICT_RANK = {"pass": 0, "not_run": 1, "unknown": 2, "fail": 3}


def _verdict_rank(verdict: str) -> int:
    return _VERDICT_RANK.get(verdict.lower(), 2)


def _identity(case: dict[str, Any], *, mode: str, run_id: str,
              started_at: str) -> dict[str, Any]:
    """The fields every result must carry, whatever happened to the case.

    ``topology_id`` falls back to the spec's file stem, which is what the
    library manifest names its specs by: a result from a queue that forgot to
    label its case is still attributable to a topology.
    """
    spec = str(case["spec"])
    return {
        "topology_id": case.get("topology") or Path(spec).stem,
        "run_id": run_id,
        "mode": mode,
        "started_at": started_at,
    }


def run_case(case: dict[str, Any], adapter, results_dir: Path,
             evidence_root: Path, keep_evidence: int, *,
             mode: str = "offline", run_id: str = "") -> CaseResult:
    """Clean → build → verify one case. Never raises for case-level faults.

    "Never raises" is load-bearing, not a nicety. A case that dies has to
    leave a result behind, or it takes the queue with it and a multi-case
    gate can never finish: the S1.19 exit run stopped inside run06 with
    results written for run01-run05 and nothing at all for run06. Anything
    that escapes here escapes ``run_queue`` too, so the wrapper below is the
    last line of defence — it converts an unexpected fault into a case-level
    ``error`` result that a restart then resumes past.
    """
    case_id = str(case["id"])
    started_at = _utc_now()
    t0 = time.monotonic()
    try:
        return _clean_build_verify(
            case, adapter, results_dir, evidence_root, keep_evidence,
            mode=mode, run_id=run_id, started_at=started_at,
        )
    except Exception as exc:
        return CaseResult(
            case_id, str(case["spec"]), "error",
            **_identity(case, mode=mode, run_id=run_id, started_at=started_at),
            finished_at=_utc_now(),
            detail=f"case crashed: {type(exc).__name__}: {exc}",
            elapsed_s=time.monotonic() - t0,
        )


def _clean_build_verify(case: dict[str, Any], adapter, results_dir: Path,
                        evidence_root: Path, keep_evidence: int, *,
                        mode: str = "offline", run_id: str = "",
                        started_at: str = "") -> CaseResult:
    """The per-case work; ``run_case`` wraps it so nothing escapes the queue."""
    from network_autopilot.core.build_errors import PreflightBlocked
    from network_autopilot.core.network_spec import NetworkSpec
    from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
    from network_autopilot.evidence.store import EvidenceStore
    from network_autopilot.orchestrator import Autopilot
    from scripts.pt_clean_workspace import clean

    case_id = str(case["id"])
    started_at = started_at or _utc_now()
    ident = _identity(case, mode=mode, run_id=run_id, started_at=started_at)
    t0 = time.monotonic()
    try:
        spec = NetworkSpec.from_json(
            Path(case["spec"]).read_text(encoding="utf-8")
        )
    except Exception as exc:
        return CaseResult(case_id, str(case["spec"]), "error", **ident,
                          finished_at=_utc_now(),
                          detail=f"spec unreadable: {exc}",
                          elapsed_s=time.monotonic() - t0)

    cleaning = clean(adapter)
    # `clean()`'s ``remaining`` is the full post-run read-back, so it always
    # contains the *protected* devices that were never meant to go. Only a
    # leftover that should have been deleted means the canvas is still dirty.
    leftovers = [n for n in cleaning.remaining if n not in set(cleaning.protected)]
    if cleaning.read_failed or cleaning.failed or leftovers:
        # Not a refusal by the engine: the canvas could not be made safe, so
        # no case ran. `refused` stays False and the reason says which of the
        # two it was, because a blocked workspace is an environment fault and
        # a preflight refusal is a decision about the spec.
        return CaseResult(
            case_id, str(case["spec"]), "blocked", **ident,
            finished_at=_utc_now(),
            detail=(
                "workspace could not be cleaned: "
                f"read_failed={cleaning.read_failed} "
                f"failed={sorted(cleaning.failed)} "
                f"remaining={sorted(leftovers)}"
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
        return CaseResult(case_id, str(case["spec"]), "error", **ident,
                          finished_at=_utc_now(),
                          detail=f"engine crash: {type(exc).__name__}: {exc}",
                          elapsed_s=time.monotonic() - t0)
    elapsed = time.monotonic() - t0
    if error is not None:
        # A preflight refusal is the engine refusing by name (rule 9): the
        # spec asked for something PT cannot do, and NOTHING was mutated.
        # Recording it as `refused` next to the status keeps the scorecard
        # able to separate "the network is wrong" from "we declined to try".
        refused = isinstance(error, PreflightBlocked)
        return CaseResult(case_id, str(case["spec"]),
                          "blocked" if refused else "error", **ident,
                          finished_at=_utc_now(), detail=str(error)[:500],
                          elapsed_s=elapsed,
                          refused=refused,
                          refusal_reason=str(error)[:500] if refused else None)
    assert report is not None
    return CaseResult(
        case_id, str(case["spec"]),
        "pass" if report.success else "fail", **ident,
        criteria=_criteria_summary(report),
        criteria_detail=_criteria_detail(report),
        elapsed_s=elapsed,
        build_id=report.build_id,
        evidence_dir=str(Path(evidence_root) / report.build_id),
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
    mode: str = "offline",
) -> RunnerSummary:
    """Run the queue with crash-resume. Finished cases are skipped by id.

    ``mode`` is stamped onto every result. It is a caller fact, not something
    the runner can infer: the same queue over the NullAdapter and over the
    bridge produce identical-looking verdicts, and a scorecard that cannot
    tell them apart would let an offline run stand in for a live one.
    """
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
        # Every attempt gets its own run id: two results for the same case id
        # are two executions (the first was replaced, or a restart re-ran it),
        # and the evidence store keeps both under their own build ids.
        run_id = new_id("run")
        started_at = _utc_now()
        # The factory runs per case — a fresh adapter each time, so a channel
        # fault in one case cannot leak into the next. Its failure is that
        # case's failure, never the queue's.
        try:
            adapter = adapter_factory()
        except Exception as exc:
            result = CaseResult(
                str(case["id"]), str(case["spec"]), "error",
                **_identity(case, mode=mode, run_id=run_id,
                            started_at=started_at),
                finished_at=_utc_now(),
                detail=f"adapter unavailable: {type(exc).__name__}: {exc}",
            )
        else:
            result = run_case(case, adapter, results_dir, evidence_root,
                              keep_evidence, mode=mode, run_id=run_id)
        summary.ran += 1
        if result.status == "pass":
            summary.passed += 1
        else:
            summary.failed += 1
        path.write_text(json.dumps(result.to_dict(), indent=1), encoding="utf-8")
        append_history(results_dir, result)
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


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_dir_uploader(destination: Path) -> Callable[[Path], dict[str, Any]]:
    """A real, verifiable upload: copy the results, then read them back.

    The destination is a directory the operator controls (a network share, a
    synced folder, a mounted bucket). "Uploaded" has to mean the bytes
    ARRIVED, so every copied file is hashed on both sides afterwards; a
    short or corrupt copy downgrades the whole upload to ``uploaded: False``
    with the file named. Nothing here can turn a failed upload into a
    successful one, and a missing destination is reported as such.
    """

    def _upload(results_dir: Path) -> dict[str, Any]:
        import shutil

        if not destination.is_dir():
            return {"uploaded": False,
                    "detail": f"destination {destination} is not a directory"}
        sources = sorted(results_dir.glob("*.json"))
        # History is part of the result set: the published scorecard derives
        # same-network consistency from it, so an upload without it would
        # leave the destination unable to reproduce the table.
        history = results_dir / HISTORY_FILE
        if history.is_file():
            sources.append(history)
        copied: list[str] = []
        for source in sources:
            target = destination / f"{results_dir.name}-{source.name}"
            shutil.copy2(source, target)
            if _sha256(source) != _sha256(target):
                return {"uploaded": False,
                        "detail": f"read-back mismatch on {target.name}"}
            copied.append(target.name)
        if not copied:
            return {"uploaded": False, "detail": "no results to upload"}
        return {"uploaded": True,
                "detail": f"{len(copied)} file(s) verified in {destination}"}

    return _upload


def _uploader_from_args(args) -> Callable[[Path], dict[str, Any]] | None:
    """The uploader this run is configured with, or None — never a pretence.

    ``--upload-dir`` wins over ``HAVYN_UPLOAD_DIR``; with neither set there is
    no uploader and every cadence point records "no uploader configured"
    instead of a success nobody performed.
    """
    import os

    destination = args.upload_dir or os.environ.get("HAVYN_UPLOAD_DIR")
    if not destination:
        return None
    return make_dir_uploader(Path(destination))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="havyn-runner", description="Build a queue of specs unattended.")
    parser.add_argument("queue", help="queue JSON file")
    parser.add_argument("--results-dir", default="runner_results")
    parser.add_argument("--evidence-dir", default="autopilot_evidence")
    parser.add_argument("--adapter", default="null", choices=("null", "bridge"))
    parser.add_argument("--prune-every", type=int, default=10)
    parser.add_argument("--keep-evidence", type=int, default=20)
    parser.add_argument(
        "--upload-dir", default=None,
        help=("directory to upload each batch of results to (verified by "
              "read-back); defaults to $HAVYN_UPLOAD_DIR, and with neither "
              "set the run records that no uploader is configured"),
    )
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
        uploader=_uploader_from_args(args),
        mode="live" if args.adapter == "bridge" else "offline",
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
