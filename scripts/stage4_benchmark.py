#!/usr/bin/env python
"""Run the Stage 4 fault-injection and enhancement benchmarks.

    python scripts/stage4_benchmark.py [--include-repair] [--active-only]

Writes ``docs/reports/stage4-benchmark.{json,md}`` and
``docs/reports/stage4-enhancement.{json,md}``. Every number comes from a run
of the real diagnosis/repair/enhancement engines over deterministic simulated
networks built by the real compiler. Live Packet Tracer evidence is produced
separately and is never replaced by this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network_autopilot.brownfield.benchmark import FaultInjectionBenchmark
from network_autopilot.brownfield.enhance_benchmark import run_enhancement_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", default=str(ROOT / "library"))
    parser.add_argument("--reports", default=str(ROOT / "docs" / "reports"))
    parser.add_argument("--include-repair", action="store_true", default=True)
    parser.add_argument("--active-only", action="store_true")
    args = parser.parse_args()

    reports = Path(args.reports)
    reports.mkdir(parents=True, exist_ok=True)

    benchmark = FaultInjectionBenchmark(
        args.library,
        include_repair=args.include_repair,
        include_pending=not args.active_only,
    ).run()
    (reports / "stage4-benchmark.json").write_text(
        benchmark.to_json(), encoding="utf-8", newline="\n"
    )
    (reports / "stage4-benchmark.md").write_text(
        benchmark.to_markdown() + "\n", encoding="utf-8", newline="\n"
    )

    enhancement = run_enhancement_benchmark(args.library, target=5, max_networks=8)
    (reports / "stage4-enhancement.json").write_text(
        enhancement.to_json(), encoding="utf-8", newline="\n"
    )
    enhancement_lines = [
        "# Stage 4 enhancement benchmark",
        "",
        f"_mode: {enhancement.mode}_",
        "",
        f"Networks enhanced **{enhancement.summary()['enhanced']}** · "
        f"previously passing criteria preserved: "
        f"**{enhancement.summary()['all_original_criteria_preserved']}**",
        "",
        "| library | added | executed | preserved | operations | result |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in enhancement.cases:
        enhancement_lines.append(
            f"| {case.library} | {case.added_host} on {case.added_to}:"
            f"{case.added_port} | {case.executed} | {case.preserved} | "
            f"{case.operations} | "
            f"{'enhanced' if case.success else (case.error or 'not enhanced')} |"
        )
    enhancement_lines.extend(
        [
            "",
            "The added host, cable and assignment are real new resources on an "
            "existing subnet; unchanged devices are not re-pushed (the "
            "operation count is the changed DAG subset).",
        ]
    )
    (reports / "stage4-enhancement.md").write_text(
        "\n".join(enhancement_lines) + "\n", encoding="utf-8", newline="\n"
    )

    summary = benchmark.summary()
    print(json.dumps(
        {
            "benchmark": {
                k: summary[k]
                for k in (
                    "scenarios", "correct", "overall_accuracy",
                    "incorrect_fixes_left_in_place", "repairs_attempted",
                    "repairs_succeeded", "rollbacks", "excluded_networks",
                )
            },
            "enhancement": {
                k: enhancement.summary()[k]
                for k in ("cases", "enhanced", "all_original_criteria_preserved")
            },
            "reports": str(reports),
        },
        indent=2,
    ))
    failed = (
        summary["overall_accuracy"] < 0.90
        or summary["incorrect_fixes_left_in_place"] > 0
        or enhancement.summary()["enhanced"] < 5
        or not enhancement.summary()["all_original_criteria_preserved"]
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
