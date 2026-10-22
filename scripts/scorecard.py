"""Morning scorecard (S2.4): one page of numbers, each from named evidence.

    python scripts/scorecard.py runner_results [--queue nightly-queue.json]
        [--fault-lab fault_lab_results/fault_lab.json]
        [--history runner_results/history.jsonl]
        [--out scorecard.json] [--md scorecard.md]

Reads the per-case `<id>.json` files the runner (S2.2) writes, its
`summary.json`, and — when present — the append-only `history.jsonl` and the
fault lab's `fault_lab.json`. Emits `scorecard.json` + `scorecard.md`.

The six blueprint metrics and where each one comes from:

* **false PASS rate** — faults the lab proved broken on which Havyn's own
  read-only verdict still said PASS (`false_pass` in the fault-lab payload).
* **criteria PASS %** — summed per-criterion counts in the case results.
* **same-network consistency** — repeated executions of the same topology in
  `history.jsonl` that reached the same terminal status.
* **build time** — `elapsed_s` recorded per case.
* **diagnosis accuracy** — injected faults whose expected cause family the
  diagnosis engine named, over the faults that require a named cause.
* **refusal rate** — cases the engine refused by name (rule 9).

A metric with no evidence behind it is listed as *unmeasured with the reason*,
never rendered as a zero. Nothing here invents a value.

Exit codes: 0 scorecard written · 1 bad/empty results dir or bad evidence file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1

HISTORY_FILE = "history.jsonl"

TERMINAL_STATUSES = ("pass", "fail", "error", "blocked")

#: Metrics the blueprint asks for that the available evidence may not support.
UNMEASURED = {
    "false_pass_rate": "needs a live fault-lab run with a recorded verdict "
    "after injection",
    "diagnosis_accuracy": "needs fault-injection labels (S2.5), not just outcomes",
    "same_network_consistency": "needs at least two executions of the same "
    "topology in history.jsonl",
}


def load_results(results_dir: Path) -> tuple[list[dict], dict]:
    """Per-case results + the runner summary. Raises ValueError when empty."""
    cases = []
    for path in sorted(results_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue  # a torn write is not a result
        if data.get("status"):
            cases.append(data)
    if not cases:
        raise ValueError(f"no case results in {results_dir}")
    summary_path = results_dir / "summary.json"
    summary = (
        json.loads(summary_path.read_text(encoding="utf-8"))
        if summary_path.is_file()
        else {}
    )
    return cases, summary


def load_history(results_dir: Path, path: Path | None = None) -> list[dict]:
    """Completed executions, oldest first. Missing history is empty, not an error."""
    history_path = path or (results_dir / HISTORY_FILE)
    if not history_path.is_file():
        return []
    entries: list[dict] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a torn line is not an execution
        if entry.get("status"):
            entries.append(entry)
    return entries


def load_fault_lab(path: Path) -> dict:
    """The fault lab's payload. Raises ValueError/OSError when unreadable."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"fault lab {path} is not an object")
    if not isinstance(data.get("faults"), list):
        raise ValueError(f"fault lab {path} has no faults list")
    return data


def _false_pass_metric(fault_lab: dict | None) -> dict[str, Any] | None:
    """A fault the lab proved broken, still read as PASS by Havyn."""
    faults = (fault_lab or {}).get("faults", [])
    # Only a real verdict counts: "unknown" means the read-only pass could not
    # answer, so including it in the denominator would dilute the rate with
    # attempts that made no claim either way.
    measured = [
        f for f in faults
        if f.get("verdict_after_injection") in ("pass", "fail")
    ]
    if not measured:
        return None
    false_passes = [f for f in measured if f.get("false_pass")]
    return {
        "rate": round(len(false_passes) / len(measured), 3),
        "false_passes": len(false_passes),
        "labels": [f.get("label") for f in false_passes],
        "measured": len(measured),
        "measured_labels": [f.get("label") for f in measured],
    }


def _diagnosis_metric(fault_lab: dict | None) -> dict[str, Any] | None:
    """Named expected causes over the faults that require a named cause.

    ``expect_named_cause=False`` faults (the wrong gateway) are deliberately
    excluded from the denominator: the honest answer there is UNKNOWN, so
    scoring it as a miss would reward guessing.
    """
    faults = (fault_lab or {}).get("faults", [])
    expected = [f for f in faults if f.get("expect_named_cause", True)]
    measured = [f for f in expected if f.get("diagnosis_ran")]
    if not measured:
        return None
    matched = [f for f in measured if f.get("diagnosis_matched")]
    missed = [f.get("label") for f in measured if not f.get("diagnosis_matched")]
    return {
        "accuracy": round(len(matched) / len(measured), 3),
        "matched": len(matched),
        "measured": len(measured),
        "missed": missed,
        "unknown_by_design": [
            f.get("label") for f in faults if not f.get("expect_named_cause", True)
        ],
    }


def _consistency_metric(history: list[dict]) -> dict[str, Any] | None:
    """Repeated executions of one topology: did they reach the same verdict?

    Grouped by ``topology_id`` (falling back to the spec path, then case id),
    and only groups with two or more terminal executions count. One run per
    topology cannot demonstrate consistency; it stays unmeasured.
    """
    groups: dict[str, list[str]] = {}
    for entry in history:
        status = entry.get("status")
        if status not in TERMINAL_STATUSES:
            continue
        key = (
            entry.get("topology_id")
            or entry.get("spec")
            or entry.get("case_id")
            or "<unknown>"
        )
        groups.setdefault(str(key), []).append(str(status))
    repeated = {key: statuses for key, statuses in groups.items()
                if len(statuses) >= 2}
    if not repeated:
        return None
    consistent = sum(1 for statuses in repeated.values()
                     if len(set(statuses)) == 1)
    return {
        "rate": round(consistent / len(repeated), 3),
        "consistent": consistent,
        "repeated_networks": len(repeated),
        "groups": {
            key: {"runs": len(statuses),
                  "statuses": sorted(set(statuses)),
                  "consistent": len(set(statuses)) == 1}
            for key, statuses in sorted(repeated.items())
        },
    }


def _outcome_class(case: dict) -> str:
    """The one word a reader needs, without collapsing unlike states."""
    status = case.get("status", "error")
    if status == "pass":
        return "PASS"
    if case.get("refused"):
        return "REFUSED"
    if case.get("timed_out"):
        return "INCOMPLETE (timeout)"
    if status == "blocked":
        return "BLOCKED (environment)"
    if status == "error":
        return "ERROR"
    return "FAIL"


def _case_row(case: dict) -> dict[str, Any]:
    spec = str(case.get("spec") or "")
    return {
        "topology_id": case.get("topology_id")
        or (Path(spec).stem if spec else case.get("case_id")),
        "case_id": case.get("case_id"),
        "run_id": case.get("run_id"),
        "mode": case.get("mode"),
        "status": case.get("status"),
        "outcome": _outcome_class(case),
        "criteria": dict(case.get("criteria") or {}),
        "elapsed_s": float(case.get("elapsed_s", 0.0) or 0.0),
        "evidence_dir": case.get("evidence_dir"),
        "refused": bool(case.get("refused")),
        "refusal_reason": case.get("refusal_reason"),
        "timed_out": bool(case.get("timed_out")),
        "reason": case.get("detail") or case.get("refusal_reason"),
        "started_at": case.get("started_at"),
        "finished_at": case.get("finished_at"),
    }


def build_scorecard(cases: list[dict], summary: dict,
                    skipped: list[dict], *,
                    fault_lab: dict | None = None,
                    history: list[dict] | None = None) -> dict:
    """Aggregate measured metrics. No fabrication: unmeasured stays named."""
    from network_autopilot.core.ids import utc_now_iso

    crit = {"pass": 0, "fail": 0, "unknown": 0, "total": 0}
    elapsed: list[float] = []
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.get("status", "error")] = (
            counts.get(case.get("status", "error"), 0) + 1
        )
        for key in crit:
            crit[key] += int((case.get("criteria") or {}).get(key, 0))
        elapsed.append(float(case.get("elapsed_s", 0.0) or 0.0))
    total = len(cases)
    measured_pct = (
        round(100.0 * crit["pass"] / crit["total"], 1) if crit["total"] else 0.0
    )

    measured = {
        "false_pass": _false_pass_metric(fault_lab),
        "diagnosis": _diagnosis_metric(fault_lab),
        "consistency": _consistency_metric(history or []),
    }
    unmeasured = {}
    for key, value in measured.items():
        metric_key = {
            "false_pass": "false_pass_rate",
            "diagnosis": "diagnosis_accuracy",
            "consistency": "same_network_consistency",
        }[key]
        if value is None:
            unmeasured[metric_key] = UNMEASURED[metric_key]

    return {
        "generated_at": utc_now_iso(),
        "cases": total,
        "status_counts": counts,
        "criteria": {**crit, "pass_pct": measured_pct},
        "timing_s": {
            "total": round(sum(elapsed), 1),
            "mean": round(sum(elapsed) / total, 1) if total else 0.0,
            "max": round(max(elapsed), 1) if elapsed else 0.0,
        },
        "refusal_rate": (
            round(counts.get("blocked", 0) / total, 3) if total else 0.0
        ),
        "false_pass": measured["false_pass"],
        "diagnosis": measured["diagnosis"],
        "consistency": measured["consistency"],
        "runner_summary": summary,
        "skipped": list(skipped),
        "per_case": sorted(
            (_case_row(case) for case in cases),
            key=lambda row: (str(row["topology_id"]), str(row["case_id"])),
        ),
        "unmeasured": unmeasured,
    }


def _rate_cell(value: dict | None, numerator: str,
               denominator: str, rate_key: str = "rate") -> str:
    if value is None:
        return "not measured"
    return f"{value[rate_key]} ({value[numerator]}/{value[denominator]})"


def render_markdown(card: dict) -> str:
    """The morning page: table first, gaps stated, not hidden."""
    lines = [
        "# Havyn nightly scorecard",
        "",
        f"_generated {card['generated_at']}_",
        "",
        f"Cases: **{card['cases']}** · "
        + ", ".join(f"{k} {v}" for k, v in sorted(card["status_counts"].items())),
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| false PASS rate | "
        f"{_rate_cell(card['false_pass'], 'false_passes', 'measured')} "
        f"(PASS on a proven-broken lab) |",
        f"| criteria pass % | {card['criteria']['pass_pct']} "
        f"({card['criteria']['pass']}/{card['criteria']['total']}) |",
        f"| same-network consistency | "
        f"{_rate_cell(card['consistency'], 'consistent', 'repeated_networks')} |",
        f"| build time total / mean / max (s) | "
        f"{card['timing_s']['total']} / {card['timing_s']['mean']} / "
        f"{card['timing_s']['max']} |",
        f"| diagnosis accuracy | "
        f"{_rate_cell(card['diagnosis'], 'matched', 'measured', 'accuracy')} |",
        f"| refusal (blocked) rate | {card['refusal_rate']} |",
        "",
        "## Cases",
        "",
        "| topology | case | run | mode | outcome | criteria | time (s) | evidence | reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in card.get("per_case", []):
        criteria = row.get("criteria") or {}
        crit_text = (
            f"{criteria.get('pass', 0)}/{criteria.get('total', 0)} pass"
            + (f", {criteria['fail']} fail" if criteria.get("fail") else "")
            + (f", {criteria['unknown']} unknown" if criteria.get("unknown") else "")
        )
        lines.append(
            f"| {row.get('topology_id') or '?'} "
            f"| {row.get('case_id') or '?'} "
            f"| {row.get('run_id') or '?'} "
            f"| {row.get('mode') or '?'} "
            f"| {row.get('outcome') or row.get('status') or '?'} "
            f"| {crit_text} "
            f"| {row.get('elapsed_s', 0.0)} "
            f"| {row.get('evidence_dir') or '—'} "
            f"| {row.get('reason') or '—'} |"
        )
    lines.append("")

    if card.get("false_pass"):
        fp = card["false_pass"]
        lines.append("## False PASS detail")
        lines.append("")
        lines.append(
            f"{fp['false_passes']} of {fp['measured']} proven-broken labs "
            f"still read as PASS"
            + (f": {', '.join(fp['labels'])}" if fp["labels"] else ".")
        )
        lines.append("")

    if card.get("diagnosis"):
        diag = card["diagnosis"]
        lines.append("## Diagnosis detail")
        lines.append("")
        lines.append(
            f"{diag['matched']} of {diag['measured']} required causes named"
            + (f"; missed: {', '.join(diag['missed'])}" if diag["missed"] else ".")
        )
        if diag["unknown_by_design"]:
            lines.append(
                "Unknown by design (not scored): "
                + ", ".join(diag["unknown_by_design"]) + "."
            )
        lines.append("")

    if card.get("consistency"):
        consistency = card["consistency"]
        lines.append("## Consistency detail")
        lines.append("")
        for key, group in consistency["groups"].items():
            lines.append(
                f"- `{key}`: {group['runs']} run(s), "
                f"statuses {', '.join(group['statuses'])} — "
                + ("consistent" if group["consistent"] else "INCONSISTENT")
            )
        lines.append("")

    if card["skipped"]:
        lines.append("## Skipped (pending specs, not silently dropped)")
        lines.append("")
        for skip in card["skipped"]:
            lines.append(f"- **{skip['id']}**: {skip.get('reason', 'pending')}")
        lines.append("")

    if card["unmeasured"]:
        lines.append("## Not measured by this run")
        lines.append("")
        for metric, reason in card["unmeasured"].items():
            lines.append(f"- `{metric}`: {reason}.")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scorecard")
    parser.add_argument("results_dir")
    parser.add_argument("--queue", default=None)
    parser.add_argument(
        "--fault-lab", default=None,
        help="fault_lab.json (S2.5); enables false-PASS and diagnosis accuracy",
    )
    parser.add_argument(
        "--history", default=None,
        help="execution history JSONL (default: <results-dir>/history.jsonl)",
    )
    parser.add_argument("--out", default="scorecard.json")
    parser.add_argument("--md", default="scorecard.md")
    args = parser.parse_args(argv)

    results_dir = Path(args.results_dir)
    try:
        cases, summary = load_results(results_dir)
    except (ValueError, OSError) as exc:
        print(f"scorecard: {exc}", file=sys.stderr)
        return EXIT_ERROR

    skipped: list[dict] = []
    if args.queue:
        try:
            skipped = json.loads(Path(args.queue).read_text(
                encoding="utf-8")).get("skipped", [])
        except (ValueError, OSError) as exc:
            print(f"scorecard: bad queue file: {exc}", file=sys.stderr)
            return EXIT_ERROR

    fault_lab = None
    fault_lab_path = args.fault_lab
    if fault_lab_path is None:
        candidate = Path("fault_lab_results") / "fault_lab.json"
        if candidate.is_file():
            fault_lab_path = str(candidate)
    if fault_lab_path:
        try:
            fault_lab = load_fault_lab(Path(fault_lab_path))
        except (ValueError, OSError) as exc:
            print(f"scorecard: bad fault lab: {exc}", file=sys.stderr)
            return EXIT_ERROR

    try:
        history = load_history(
            results_dir, Path(args.history) if args.history else None
        )
    except OSError as exc:
        print(f"scorecard: bad history: {exc}", file=sys.stderr)
        return EXIT_ERROR

    card = build_scorecard(
        cases, summary, skipped, fault_lab=fault_lab, history=history
    )
    Path(args.out).write_text(json.dumps(card, indent=1), encoding="utf-8")
    Path(args.md).write_text(render_markdown(card), encoding="utf-8")
    print(f"{card['cases']} cases, criteria pass {card['criteria']['pass_pct']}% "
          f"-> {args.out}, {args.md}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
