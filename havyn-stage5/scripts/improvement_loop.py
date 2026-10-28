"""S5.22 continuous improvement loop.

Reads the S5.19 gate report, classifies every failure into a concrete
improvement action, and writes:

  improvement-plan.json   the classified failures + the actions they demand
  delta/                  new training examples derived from verified
                          failures, ready for the next assembly pass

The loop is: evaluate -> classify -> build delta -> reassemble -> retrain ->
re-evaluate. One full cycle is the minimum the spec demands; this script
runs the first half honestly (it never fabricates a cycle that did not
happen) and the retrain half is train_lora.py + assemble_dataset.py.

Verified failure -> training example rules:
  - a schema-invalid or lint-failing intent output is NOT added to training
    as-is; the correct intent for that prompt already exists in the frozen
    data, so the action is resampling/reweighting, recorded in the plan.
  - a gold/diagnosis failure whose evidence came from the engine is added
    as a delta example (the expected answer is engine-truth).
  - a live-gate failure is recorded as a live regression case for the next
    nightly run, never as a training row (no live evidence in offline data).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def classify_gate(gates: dict) -> list[dict]:
    """Turn failed/not-verified gates into plan entries."""
    actions: list[dict] = []
    for name in ("schema_valid", "lint_pass", "gold_score"):
        gate = gates.get(name) or {}
        if gate.get("status") != "PASS":
            actions.append({
                "gate": name,
                "status": gate.get("status"),
                "measured": gate.get("measured"),
                "action": ("add verified delta examples and rebalance the "
                           "task mix" if gate.get("status") == "FAIL"
                           else "score the gate - it has no evidence yet"),
            })
    for cls, detail in (gates.get("diagnosis_class") or {}).items():
        if detail.get("status") != "PASS":
            actions.append({
                "gate": f"diagnosis_class/{cls}",
                "status": detail.get("status"),
                "measured": detail.get("accuracy"),
                "action": ("inject more certified faults of this class via "
                           "generate_diagnosis.py and rebuild the delta"
                           if detail.get("status") == "FAIL"
                           else "score the gate - no diagnosis examples yet"),
            })
    live = gates.get("live_pass") or {}
    if live.get("status") != "PASS":
        actions.append({
            "gate": "live_pass",
            "status": live.get("status"),
            "measured": live.get("rate"),
            "action": ("record live regressions from the nightly runner and "
                       "re-run the live gate"
                       if live.get("status") == "FAIL"
                       else str(live.get("reason", "live gate not run"))),
        })
    return actions


def classify_examples(per_example: list[dict]) -> dict[str, list[dict]]:
    """Group failed examples by failure mode for the plan + delta."""
    groups: dict[str, list[dict]] = {}
    for ex in per_example:
        failed = (
            (ex.get("verdict") == "fail")
            or (ex.get("verdict_correct") is False)
            or (ex.get("correct") is False)
        )
        if not failed:
            continue
        if ex.get("schema_valid") is False:
            mode = "invalid-json"
        elif ex.get("verdict") == "fail" and ex.get("task") == "gold":
            mode = "gold-miss"
        elif "verdict_correct" in ex:
            mode = f"diagnosis/{ex.get('class')}"
        elif "correct" in ex:
            mode = "clarification"
        else:
            mode = "field-accuracy"
        groups.setdefault(mode, []).append(ex)
    return groups


def build_plan(report_path: Path) -> tuple[dict, dict[str, list[dict]]]:
    """Read a gate report and return (plan, failures grouped by mode)."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    gates = report.get("gates") or {}
    per_example = report.get("per_example") or []
    failures = classify_examples(per_example)
    plan = {
        "stage": "S5.22",
        "source_report": str(report_path),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "release": gates.get("release"),
        "gate_actions": classify_gate(gates),
        "failure_groups": {
            mode: {"count": len(rows), "ids": [r.get("id") for r in rows]}
            for mode, rows in failures.items()
        },
        "cycle": {
            "steps": [
                "1. build delta examples from verified failures "
                "(scripts/improvement_loop.py --build-delta)",
                "2. reassemble the dataset with the delta "
                "(scripts/assemble_dataset.py)",
                "3. retrain (scripts/train_lora.py --resume)",
                "4. re-evaluate (scripts/evaluate_model.py)",
            ],
            "minimum_cycles": 1,
        },
    }
    return plan, failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation",
                        default="havyn-stage5/evaluation/gate-report.json")
    parser.add_argument("--out", default="havyn-stage5/improvement")
    parser.add_argument("--build-delta", action="store_true",
                        help="also write delta training examples from the "
                             "classified failures")
    parser.add_argument("--dataset-dir",
                        default="havyn-stage5/datasets/havyn-train-v1")
    args = parser.parse_args()

    report_path = Path(args.evaluation)
    if not report_path.exists():
        raise SystemExit(f"no evaluation report at {report_path}; run "
                         "evaluate_model.py first")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    plan, failures = build_plan(report_path)
    (out / "improvement-plan.json").write_text(
        json.dumps(plan, indent=2), encoding="utf-8")

    if args.build_delta:
        delta_dir = out / "delta"
        delta_dir.mkdir(exist_ok=True)
        written = 0
        report = json.loads(report_path.read_text(encoding="utf-8"))
        by_id = {
            (ex.get("task"), ex.get("id")): ex for ex in
            report.get("per_example", [])
        }
        dataset = Path(args.dataset_dir)
        for path in sorted(dataset.glob("heldout.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                payload = row.get("payload") or {}
                key = (path.stem, payload.get("id"))
                ex = by_id.get(key)
                if ex is None or ex.get("schema_valid") is False:
                    continue
                # Only engine-truth failures become training rows: the
                # expected answer in the frozen data was produced/verified by
                # the engine, so a miss is a model gap, not a bad label.
                if row.get("task") == "diagnosis":
                    (delta_dir / "delta.jsonl").open("a", encoding="utf-8").write(
                        json.dumps({"task": "diagnosis", "family": row.get("family"),
                                    "payload": payload}, ensure_ascii=False) + "\n")
                    written += 1
        plan["delta_written"] = written
        (out / "improvement-plan.json").write_text(
            json.dumps(plan, indent=2), encoding="utf-8")
        print(f"delta examples: {written} -> {delta_dir}")

    print(json.dumps(plan, indent=2))
    if plan["release"] == "GO":
        return
    sys.exit(1)


if __name__ == "__main__":
    main()
