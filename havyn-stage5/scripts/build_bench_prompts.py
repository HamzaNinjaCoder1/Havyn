"""Build the S5.4 benchmark prompt set (>=100 identical prompts).

Sources:
  - valid intents from the generated dataset (datasets/havyn-intents-v1/valid.jsonl)
    rendered through benchmark_harness.render_prompt (deterministic templates,
    both languages);
  - the manual invalid examples (data/intents/invalid/*.json) as
    expect_refusal=true prompts.

Every candidate model receives byte-identical prompts in the same order.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from benchmark_harness import render_prompt  # noqa: E402


def build(valid_path: Path, invalid_dir: Path, target: int) -> list[dict]:
    records: list[dict] = []
    valid_lines = [
        json.loads(line)
        for line in valid_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not valid_lines:
        raise SystemExit(f"no valid intents in {valid_path}; run generate_intents.py first")

    rng = random.Random(20261028)
    # Cycle through the valid set with varying language until the target.
    index = 0
    while len(records) < target:
        rec = valid_lines[index % len(valid_lines)]
        intent = HavynIntent.model_validate(rec["intent"])
        language = "roman-urdu" if (len(records) % 4 == 3) else "en"
        records.append({
            "id": f"bench-{len(records):04d}",
            "prompt": render_prompt(intent, language=language),
            "category": f"{rec.get('coverage_cell', 'unknown')}/{language}",
            "expected_intent": rec["intent"],
            "expect_refusal": False,
            "language": language,
        })
        index += 1

    for invalid_path in sorted(invalid_dir.glob("*.json")):
        try:
            HavynIntent.model_validate(json.loads(invalid_path.read_text(encoding="utf-8")))
            continue  # schema-valid examples are not refusal prompts
        except Exception:  # noqa: BLE001
            pass
        body = json.loads(invalid_path.read_text(encoding="utf-8"))
        name = body.get("name", invalid_path.stem)
        records.append({
            "id": f"bench-refusal-{invalid_path.stem}",
            "prompt": (
                f"Design a network called {name}. Sections are given as JSON "
                "hints below; convert them into the Havyn intent JSON.\n"
                + json.dumps(body, indent=1)
            ),
            "category": f"refusal/{invalid_path.stem}",
            "expected_intent": None,
            "expect_refusal": True,
            "language": "en",
        })
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--valid", default="dataset/intents/valid.jsonl")
    parser.add_argument("--invalid-dir", default="data/intents/invalid")
    parser.add_argument("--target", type=int, default=120)
    parser.add_argument("--out", default="havyn-stage5/datasets/bench-prompts-v1.jsonl")
    args = parser.parse_args()

    records = build(Path(args.valid), Path(args.invalid_dir), args.target)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    refusals = sum(1 for r in records if r["expect_refusal"])
    print(f"wrote {len(records)} prompts ({len(records) - refusals} intent, "
          f"{refusals} refusal) to {out}")


if __name__ == "__main__":
    main()
