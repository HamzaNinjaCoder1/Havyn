"""S5.13 clarification-question data generation.

Take valid intents, remove the fields whose absence genuinely blocks a safe
build, and ask: what must be asked before proceeding? The expected output is
at most 1-2 questions, only about information the deterministic engine
cannot safely decide. About 40% of examples keep everything required and the
correct answer is "no clarification needed".

Field-removal rules (each maps to the question the model should ask):
  vlans without any size source   -> "how many hosts / which subnet?"
  host group without count        -> "how many devices?"
  site without areas              -> "which areas/rooms exist?"
  wireless clients without an AP  -> "is there an access point already?"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from generate_intents import GENERATOR_VERSION, build_intent, coverage_cells  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402

REMOVALS = {
    "vlan-size-source": {
        "strip": lambda d: [v.pop("required_hosts", None) or v.pop("cidr", None)
                            for v in d["vlans"]],
        "expected_question": (
            "How many devices should this network segment support, or do you "
            "already have a subnet in mind?"),
        "field": "vlans[].required_hosts|cidr",
    },
    "host-group-count": {
        "strip": lambda d: [g.pop("count", None)
                            for s in d["sites"] for a in s["areas"]
                            for g in a["host_groups"]][:1],
        "expected_question": (
            "How many devices are in that group?"),
        "field": "sites[].areas[].host_groups[].count",
    },
    "no-areas": {
        "strip": lambda d: d["sites"][0].update(areas=[]),
        "expected_question": (
            "Which areas or floors does this site have?"),
        "field": "sites[].areas",
    },
}


def engine_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()[:12]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--out", default="havyn-stage5/datasets/havyn-clarification-v1")
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--complete-share", type=float, default=0.4,
                        help="share of examples that need no question")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cells = [c for c in coverage_cells() if c.site_count == 1]

    examples = []
    hashes = set()
    attempts = 0
    while len(examples) < args.count and attempts < args.count * 20:
        attempts += 1
        cell = rng.choice(cells)
        data = build_intent(cell, rng.randint(0, 4), random.Random(rng.getrandbits(32)))
        if isinstance(data, HavynIntent):
            data = data.model_dump()
        # Deep-copy twice: once for the prompt side, once for reference.
        prompt_body = json.loads(json.dumps(data))

        if rng.random() < args.complete_share:
            expected = {"needs_clarification": False, "questions": []}
            kind = "complete"
        else:
            key = rng.choice(list(REMOVALS))
            rule = REMOVALS[key]
            # Only strip when the field is actually present.
            stripped = json.dumps(prompt_body)
            rule["strip"](prompt_body)
            if json.dumps(prompt_body) == stripped:
                continue
            # The stripped body must NOT be a complete intent any more, but
            # the original must still validate (sanity on the reference).
            HavynIntent.model_validate(data)
            expected = {
                "needs_clarification": True,
                "questions": [rule["expected_question"]],
            }
            kind = f"missing:{key}"

        lint = lint_intent(HavynIntent.model_validate(data))
        if lint.blocking:
            continue
        digest = hashlib.sha256(json.dumps(
            prompt_body, sort_keys=True).encode("utf-8")).hexdigest()
        if digest in hashes:
            continue
        hashes.add(digest)

        examples.append({
            "id": f"clq-{len(examples):05d}",
            "kind": kind,
            "prompt_intent": prompt_body,
            "complete_intent": data,
            "expected_output": expected,
            "generator_version": GENERATOR_VERSION,
            "engine_commit": engine_commit(),
            "seed": args.seed,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    with (out / "clarification.jsonl").open("w", encoding="utf-8") as f:
        for example in examples:
            f.write(json.dumps(example, ensure_ascii=False) + "\n")
    (out / "manifest.json").write_text(json.dumps({
        "dataset": "havyn-clarification-v1",
        "generator_version": GENERATOR_VERSION,
        "engine_commit": engine_commit(),
        "seed": args.seed,
        "examples": len(examples),
        "complete": sum(1 for e in examples if e["kind"] == "complete"),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    complete = sum(1 for e in examples if e["kind"] == "complete")
    print(f"wrote {len(examples)} clarification examples "
          f"({complete} complete / {len(examples) - complete} missing-field) to {out}")


if __name__ == "__main__":
    main()
