"""Morning scorecard (S2.4): one page of numbers from a runner results dir.

    python scripts/scorecard.py runner_results [--queue nightly-queue.json]
        [--out scorecard.json] [--md scorecard.md]

Reads the per-case `<id>.json` files the runner (S2.2) writes plus its
`summary.json`, and emits `scorecard.json` + `scorecard.md`. Metrics are
only what the files support: criteria pass %, timing, refusal (blocked)
rate, and the queue's skip record. Anything that needs a second source of
truth — false-PASS rate (needs live cross-check), diagnosis accuracy
(needs fault-injection labels) — is listed as *unmeasured with the reason*,
never as zero.

Exit codes: 0 scorecard written · 1 bad/empty results dir.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1

#: Metrics the blueprint asks for that a results dir alone cannot prove.
UNMEASURED = {
    "false_pass_rate": "needs a live cross-check beyond the runner's own verdicts",
    "diagnosis_accuracy": "needs fault-injection labels (S2.5), not just outcomes",
    "same_network_consistency": "needs at least two runs over the same queue",
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


def build_scorecard(cases: list[dict], summary: dict,
                    skipped: list[dict]) -> dict:
    """Aggregate the measured metrics. No fabrication: unmeasured stays named."""
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
        elapsed.append(float(case.get("elapsed_s", 0.0)))
    total = len(cases)
    measured_pct = round(100.0 * crit["pass"] / crit["total"], 1) if crit["total"] else 0.0
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
        "refusal_rate": round(counts.get("blocked", 0) / total, 3) if total else 0.0,
        "runner_summary": summary,
        "skipped": list(skipped),
        "unmeasured": dict(UNMEASURED),
    }


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
        f"| criteria pass % | {card['criteria']['pass_pct']} "
        f"({card['criteria']['pass']}/{card['criteria']['total']}) |",
        f"| build time total / mean / max (s) | "
        f"{card['timing_s']['total']} / {card['timing_s']['mean']} / "
        f"{card['timing_s']['max']} |",
        f"| refusal (blocked) rate | {card['refusal_rate']} |",
        "",
    ]
    if card["skipped"]:
        lines.append("## Skipped (pending specs, not silently dropped)")
        lines.append("")
        for skip in card["skipped"]:
            lines.append(f"- **{skip['id']}**: {skip.get('reason', 'pending')}")
        lines.append("")
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
    parser.add_argument("--out", default="scorecard.json")
    parser.add_argument("--md", default="scorecard.md")
    args = parser.parse_args(argv)

    try:
        cases, summary = load_results(Path(args.results_dir))
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
    card = build_scorecard(cases, summary, skipped)
    Path(args.out).write_text(json.dumps(card, indent=1), encoding="utf-8")
    Path(args.md).write_text(render_markdown(card), encoding="utf-8")
    print(f"{card['cases']} cases, criteria pass {card['criteria']['pass_pct']}% "
          f"-> {args.out}, {args.md}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
