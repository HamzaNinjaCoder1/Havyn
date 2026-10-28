"""Stage 5 prompt-authoring: duplicate control + humanized quality scoring.

Two jobs, both offline and deterministic:

1. DUPLICATE / NEAR-DUPLICATE control over the candidate pool:
   - exact prompt-hash duplicates,
   - normalized-text duplicates (case/punctuation/whitespace folded),
   - MinHash near-duplicates (same shingle-Jaccard scheme as
     ``roundtrip_verify.py``, so the two stages agree),
   - INTENT-level duplicates (two different prompts that encode the same
     requirement-bearing shape — caught by hashing the canonical intent).

2. HUMANIZED QUALITY scoring. Dimensions are reported SEPARATELY (never
   collapsed into one number, per the brief). Several dimensions are
   objective and computed here; the subjective ones (realism, naturalness,
   language authenticity) are filled by the Model B reviewer and merged in
   ``build_reports.py``.

Usage:
    python quality_checks.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAND_DIR = HERE / "candidates"

# Same shingle params as roundtrip_verify.py so both stages agree.
_WIDTH = 4
_PERMS = 64


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _shingles(text: str) -> set[str]:
    words = _normalize(text).split()
    if not words:
        return set()
    return {" ".join(words[i:i + _WIDTH])
            for i in range(max(1, len(words) - _WIDTH + 1))}


def _minhash(text: str) -> list[int]:
    sh = _shingles(text)
    if not sh:
        return [0] * _PERMS
    import random
    rng = random.Random(1)
    salts = [rng.getrandbits(32) for _ in range(_PERMS)]
    return [min((hash((s, shingle)) & 0xFFFFFFFF) for shingle in sh) for s in salts]


def _jaccard(a: list[int], b: list[int]) -> float:
    if not a:
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def _intent_hash(record: dict) -> str | None:
    intent = record.get("expected_intent")
    if not intent:
        return None
    canonical = json.dumps(intent, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_candidates() -> list[dict]:
    records = []
    for path in sorted(CAND_DIR.glob("oc-*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def duplicate_report(records: list[dict]) -> dict:
    exact: dict[str, list[str]] = defaultdict(list)
    normalized: dict[str, list[str]] = defaultdict(list)
    intent_map: dict[str, list[str]] = defaultdict(list)
    minhash_sigs: list[tuple[str, list[int]]] = []
    near_dup: list[dict] = []

    for r in records:
        cid = r["id"]
        exact[hashlib.sha256(r["prompt"].encode()).hexdigest()].append(cid)
        normalized[_normalize(r["prompt"])].append(cid)
        ih = _intent_hash(r)
        if ih:
            intent_map[ih].append(cid)
        sig = _minhash(r["prompt"])
        for prior_id, prior_sig in minhash_sigs:
            j = _jaccard(sig, prior_sig)
            if j >= 0.90:
                near_dup.append({"a": prior_id, "b": cid, "jaccard": round(j, 3)})
        minhash_sigs.append((cid, sig))

    exact_dups = {k: v for k, v in exact.items() if len(v) > 1}
    norm_dups = {k: v for k, v in normalized.items() if len(v) > 1}
    intent_dups = {k[:12]: v for k, v in intent_map.items() if len(v) > 1}

    return {
        "total_candidates": len(records),
        "exact_prompt_duplicates": list(exact_dups.values()),
        "normalized_prompt_duplicates": list(norm_dups.values()),
        "intent_level_duplicates": list(intent_dups.values()),
        "near_duplicates_minhash": near_dup,
        "minhash_threshold": 0.90,
        "distinct_prompts": len({hashlib.sha256(r["prompt"].encode()).hexdigest()
                                 for r in records}),
        "distinct_intents": len(intent_map),
    }


def objective_quality(records: list[dict]) -> dict:
    """Objective, computed dimensions. Subjective ones come from Model B."""
    lang_count: dict[str, int] = defaultdict(int)
    diff_count: dict[str, int] = defaultdict(int)
    cat_count: dict[str, int] = defaultdict(int)
    length_count: dict[str, int] = defaultdict(int)
    refusal_count = 0
    with_intent = 0
    prompt_words = []

    for r in records:
        lang_count[r.get("language", "?")] += 1
        diff_count[r.get("difficulty", "?")] += 1
        cat_count[r.get("category", "?")] += 1
        length_count[r.get("length_category", "?")] += 1
        if r.get("expect_refusal"):
            refusal_count += 1
        if r.get("expected_intent"):
            with_intent += 1
        prompt_words.append(len(r["prompt"].split()))

    return {
        "language_distribution": dict(lang_count),
        "difficulty_distribution": dict(diff_count),
        "category_distribution": dict(cat_count),
        "length_distribution": dict(length_count),
        "refusal_candidates": refusal_count,
        "buildable_candidates": with_intent,
        "prompt_words_min": min(prompt_words) if prompt_words else 0,
        "prompt_words_max": max(prompt_words) if prompt_words else 0,
        "prompt_words_mean": round(sum(prompt_words) / len(prompt_words), 1)
        if prompt_words else 0,
    }


def main() -> None:
    records = load_candidates()
    if not records:
        raise SystemExit(f"no candidate JSON in {CAND_DIR}")

    dup = duplicate_report(records)
    qual = objective_quality(records)
    out = {"duplicates": dup, "objective_quality": qual}
    (HERE / "quality-report.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps({
        "candidates": dup["total_candidates"],
        "exact_prompt_duplicates": len(dup["exact_prompt_duplicates"]),
        "normalized_prompt_duplicates": len(dup["normalized_prompt_duplicates"]),
        "intent_level_duplicates": len(dup["intent_level_duplicates"]),
        "near_duplicates_minhash": len(dup["near_duplicates_minhash"]),
        "language_distribution": qual["language_distribution"],
        "difficulty_distribution": qual["difficulty_distribution"],
        "refusal_candidates": qual["refusal_candidates"],
        "buildable_candidates": qual["buildable_candidates"],
    }, indent=2))


if __name__ == "__main__":
    main()
