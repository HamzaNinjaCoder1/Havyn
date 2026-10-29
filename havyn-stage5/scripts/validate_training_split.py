"""Validate the assembled training split without weakening any gate.

Checks per row:
  - intent-like payloads: schema -> intent lint -> expander -> spec lint -> IPAM
    -> compile;
  - diagnosis specs: NetworkSpec schema;
  - family disjointness per task;
  - duplicate/leak checks across splits;
  - Gold prompt hashes must not appear in any split.

Usage:
    python validate_training_split.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO),):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.network_autopilot.ai.expander import IntentExpander  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402
from src.network_autopilot.compiler.compiler import compile_spec  # noqa: E402
from src.network_autopilot.compiler.spec_lint import lint_spec  # noqa: E402
from src.network_autopilot.core.network_spec import NetworkSpec  # noqa: E402
from src.network_autopilot.ipam import plan_addressing  # noqa: E402

SPLITS = REPO / "havyn-stage5" / "datasets" / "havyn-train-v1"
GOLD = REPO / "havyn-stage5" / "datasets" / "gold-v1"
OUT = SPLITS / "validation-report.json"


def _intent_chain(data: dict) -> str | None:
    try:
        intent = HavynIntent.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        return f"schema: {type(exc).__name__}: {str(exc)[:120]}"
    lint = lint_intent(intent)
    if lint.blocking:
        return f"intent-lint: {lint.blocking[0].code}"
    try:
        expansion = IntentExpander(intent).expand()
        problems = lint_spec(expansion.spec)
        if problems:
            return f"spec-lint: {problems[0]}"
        compile_spec(expansion.spec, plan_addressing(expansion.spec))
    except Exception as exc:  # noqa: BLE001
        return f"engine: {type(exc).__name__}: {str(exc)[:120]}"
    return None


def _prompt_text(payload: dict) -> str | None:
    for key in ("prompt", "request", "source"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def validation_targets(task: str, payload: dict) -> list[str]:
    """Payload keys that must build for this row, in order.

    An 'impossible' mutation has no valid after-state (its target is the
    named refusal). A clarification's ``prompt_intent`` is deliberately
    incomplete; only a 'complete' record promises that parse is valid too.
    """
    if task in ("prompt", "intent"):
        return ["intent"]
    if task == "enhancement":
        if payload.get("kind") == "impossible" or not payload.get("after_intent"):
            return ["before_intent"]
        return ["before_intent", "after_intent"]
    if task == "clarification":
        if payload.get("kind") == "complete":
            return ["prompt_intent", "complete_intent"]
        return ["complete_intent"]
    if task == "diagnosis":
        return []
    return ["<unknown>"]


def _leak_key(task: str, payload: dict) -> str | None:
    """What must not appear in two splits.

    For prompt-style tasks the prompt text is the shared surface, so a repeat
    is leakage. For intent/enhancement/clarification two rows may legitimately
    share a prompt while carrying different targets (a mutation reuses its
    base request), so the whole example is the key.
    """
    if task in ("prompt", "diagnosis"):
        return _prompt_text(payload)
    return json.dumps(payload, sort_keys=True, default=str)


def main() -> None:
    splits = {}
    for name in ("train", "validation", "heldout"):
        path = SPLITS / f"{name}.jsonl"
        splits[name] = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    errors: list[dict] = []
    counts: dict[str, Counter] = {}
    family_sets: dict[str, dict[str, set[str]]] = {}
    prompt_hashes: dict[str, list[str]] = {}
    gold_hashes = {
        hashlib.sha256(json.loads(p.read_text(encoding="utf-8"))["prompt"].encode()).hexdigest()
        for p in sorted(GOLD.glob("gold-*.json"))
    }

    for split, rows in splits.items():
        counts[split] = Counter()
        for row in rows:
            task = row["task"]
            payload = row["payload"]
            counts[split][task] += 1
            family_sets.setdefault(task, {}).setdefault(split, set()).add(row["family"])
            text = _prompt_text(payload)
            leak_key = _leak_key(task, payload)
            if leak_key:
                digest = hashlib.sha256(leak_key.encode()).hexdigest()
                prompt_hashes.setdefault(digest, []).append(f"{split}:{task}:{row['family']}")
                if text and hashlib.sha256(text.encode()).hexdigest() in gold_hashes:
                    errors.append({"split": split, "task": task, "error": "gold prompt leak"})
            if task == "diagnosis":
                try:
                    NetworkSpec.model_validate(payload["spec"])
                    stage = None
                except Exception as exc:  # noqa: BLE001
                    stage = f"spec-schema: {type(exc).__name__}: {str(exc)[:120]}"
            else:
                stage = None
                for key in validation_targets(task, payload):
                    if key == "<unknown>":
                        stage = f"unknown task {task!r}"
                        break
                    stage = _intent_chain(payload.get(key) or {})
                    if stage:
                        break
            if stage:
                errors.append({"split": split, "task": task, "error": stage})

    family_overlaps = []
    for task, per_split in family_sets.items():
        names = list(per_split)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                overlap = per_split[a] & per_split[b]
                if overlap:
                    family_overlaps.append({"task": task, "a": a, "b": b,
                                            "families": sorted(overlap)})

    cross_split_duplicates = [
        {"hash": digest[:12], "locations": locs}
        for digest, locs in prompt_hashes.items()
        if len({loc.split(":")[0] for loc in locs}) > 1
    ]

    report = {
        "rows": {split: sum(c.values()) for split, c in counts.items()},
        "per_task": {split: dict(c) for split, c in counts.items()},
        "errors": errors,
        "family_overlaps": family_overlaps,
        "cross_split_prompt_duplicates": cross_split_duplicates,
        "gold_prompt_hashes": len(gold_hashes),
        "passed": not errors and not family_overlaps and not cross_split_duplicates,
    }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "rows": report["rows"],
        "errors": len(errors),
        "family_overlaps": len(family_overlaps),
        "cross_split_prompt_duplicates": len(cross_split_duplicates),
        "passed": report["passed"],
    }, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
