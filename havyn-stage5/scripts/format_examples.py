"""Shared training/evaluation record formatting (S5.17-S5.19).

Pure, offline-testable core used by ``train_lora.py`` and
``evaluate_model.py``: turns the frozen ``havyn-train-v1`` split rows into
chat-format training examples, and balances per-task sampling. Nothing here
touches a GPU or a model - the GPU half lives in the callers.

Task -> example mapping (record = one row of train/validation.jsonl):

  prompt         user = payload.prompt (template/teacher, round-trip ACCEPT)
                 assistant = payload.intent JSON
  intent         user = a deterministic request rendered from the intent
                 assistant = payload.intent JSON
  enhancement    user = current intent JSON + mutation request
                 assistant = payload.expected_output (patch or named refusal)
  clarification  user = the draft intent JSON + "what is missing?"
                 assistant = payload.expected_output (questions or none)
  diagnosis      user = spec + evidence, no labels
                 assistant = payload.expected_output (verdict/causes/repair)

The intent request is rendered from the intent itself, never from the saved
`source_excerpt` field: those excerpts were synthetic placeholders that did
not describe the full intent, so training on them would teach the model to
output facts the request never contained. The rendered request is
deterministic (same intent + renderer => same text) and is labelled
`request_source: rendered_intent`.

Clarification always shows the draft JSON. Showing an invented excerpt
instead would hide the draft the expected questions were computed from.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import yaml

SYSTEM_PROMPT = (
    "You convert natural-language networking requests into a single JSON "
    "object matching the Havyn Intent Schema v1. Output ONLY the JSON object "
    "- no markdown, no explanation. If the request is missing information or "
    "asks for something impossible, do not invent values; reply with a short "
    "refusal or clarification request instead."
)

DIAGNOSIS_SYSTEM_PROMPT = (
    "You diagnose failed network verification. You are given the network "
    "specification and the observed evidence. Output ONLY a JSON object with "
    "verdict, confirmed_cause/confirmed_causes where applicable, evidence "
    "citations and whether a repair is recommended. Never invent a cause the "
    "evidence does not name; when nothing in the observed state explains the "
    "failure, say so and do not propose a repair."
)

TASK_SYSTEM_PROMPTS = {
    "prompt": SYSTEM_PROMPT,
    "intent": SYSTEM_PROMPT,
    "enhancement": SYSTEM_PROMPT,
    "clarification": SYSTEM_PROMPT,
    "diagnosis": DIAGNOSIS_SYSTEM_PROMPT,
}

TASKS = ("intent", "prompt", "diagnosis", "enhancement", "clarification")


def load_config(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _render_intent_request(intent: dict[str, Any]) -> str:
    """Deterministic request rendered from the intent (fidelity by design)."""
    from benchmark_harness import render_prompt
    from src.network_autopilot.ai.intent import HavynIntent

    model = HavynIntent.model_validate(intent)
    language = "roman-urdu" if intent.get("language") == "roman-urdu" else "en"
    return render_prompt(model, language=language)


def format_record(row: dict[str, Any]) -> dict[str, Any] | None:
    """One frozen-split row -> chat messages, or None when unformattable."""
    task = row.get("task")
    payload = row.get("payload") or {}
    system = TASK_SYSTEM_PROMPTS.get(task)
    if system is None:
        return None

    request_source = "payload"
    if task == "prompt":
        request = payload.get("prompt")
        intent = payload.get("intent")
    elif task == "intent":
        intent = payload.get("intent") or {}
        try:
            request = _render_intent_request(intent)
            request_source = "rendered_intent"
        except Exception:
            request = None
    elif task == "enhancement":
        before = payload.get("before_intent")
        request = None
        if before is not None:
            request = (
                "Here is the current network intent as JSON:\n" + _dump(before)
                + "\n\nApply this change: " + str(payload.get("request", ""))
            )
        intent = payload.get("expected_output")
    elif task == "clarification":
        draft = payload.get("prompt_intent") or {}
        request = (
            "Here is a draft network intent as JSON:\n" + _dump(draft)
            + "\n\nWhat information is missing before this can be built?"
        )
        request_source = "draft_json"
        intent = payload.get("expected_output")
    elif task == "diagnosis":
        request = (
            "Network specification:\n" + _dump(payload.get("spec"))
            + "\n\nObserved evidence:\n" + _dump(payload.get("evidence"))
            + "\n\nDiagnose the failed verification from this evidence only."
        )
        intent = payload.get("expected_output")
    else:
        return None

    if not request or intent is None:
        return None
    # Prompt-pair payloads carry no id; the request text hashes to a stable
    # join key so evaluation generations can be matched back to records.
    record_id = payload.get("id") or hashlib.sha256(
        request.encode("utf-8")).hexdigest()[:16]
    return {
        "task": task,
        "family": row.get("family"),
        "id": record_id,
        "request_source": request_source,
        "payload": payload,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": request},
            {"role": "assistant", "content": _dump(intent)},
        ],
    }


def format_split(path: str | Path) -> tuple[list[dict], dict[str, int]]:
    """Format a jsonl split; returns (examples, per-task skip counts)."""
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    examples: list[dict] = []
    skipped: dict[str, int] = {}
    for row in rows:
        formatted = format_record(row)
        if formatted is None:
            task = str(row.get("task", "unknown"))
            skipped[task] = skipped.get(task, 0) + 1
        else:
            examples.append(formatted)
    return examples, skipped


def balance_tasks(examples: list[dict], rng: random.Random,
                  mode: str = "oversample", max_repeat: int = 10) -> list[dict]:
    """Deterministic per-task balancing for the mixed-task SFT mix.

    ``oversample`` repeats minority-task examples (whole examples, never
    fragments) toward an equal task share, capped at ``max_repeat`` plays per
    example so tiny tasks cannot be repeated into memorisation; majority
    tasks keep every example. The realised counts are reported in the
    training stats either way. ``none`` returns the examples unchanged.
    """
    if mode == "none":
        return list(examples)
    by_task: dict[str, list[dict]] = {}
    for ex in examples:
        by_task.setdefault(ex["task"], []).append(ex)
    target = max(len(v) for v in by_task.values())
    balanced: list[dict] = []
    for task in sorted(by_task):
        rows = list(by_task[task])
        rng.shuffle(rows)
        full, remainder = divmod(target, len(rows))
        full = min(full, max_repeat)
        repeated = rows * full + rows[:remainder if full < max_repeat else 0]
        balanced.extend(repeated)
    rng.shuffle(balanced)
    return balanced


def _dedupe(examples: list[dict]) -> tuple[list[dict], int]:
    """Drop exact chat duplicates across tasks (rendered intent vs template)."""
    seen: set[str] = set()
    kept: list[dict] = []
    duplicates = 0
    for example in examples:
        key = hashlib.sha256(
            json.dumps(example["messages"], sort_keys=True).encode("utf-8")
        ).hexdigest()
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        kept.append(example)
    return kept, duplicates


def load_training_data(dataset_dir: str | Path, seed: int,
                       balance: str = "oversample") -> tuple[list[dict], dict]:
    """Load + format train/validation splits from the frozen dataset."""
    root = Path(dataset_dir)
    train, train_skipped = format_split(root / "train.jsonl")
    val, val_skipped = format_split(root / "validation.jsonl")
    train, train_dupes = _dedupe(train)
    val, val_dupes = _dedupe(val)
    rng = random.Random(seed)
    train_balanced = balance_tasks(train, rng, balance)
    counts: dict[str, int] = {}
    for ex in train_balanced:
        counts[ex["task"]] = counts.get(ex["task"], 0) + 1
    stats = {
        "train_raw": len(train),
        "train_formatted": len(train_balanced),
        "validation": len(val),
        "train_skipped": train_skipped,
        "validation_skipped": val_skipped,
        "train_duplicates_removed": train_dupes,
        "validation_duplicates_removed": val_dupes,
        "train_task_counts": dict(sorted(counts.items())),
    }
    return train_balanced, val, stats
