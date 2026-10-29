"""S5.14 dataset assembly, family-aware splitting, leakage detection, audit.

Inputs (all versioned, all engine-verified):
  havyn-intents-v1/valid.jsonl
  havyn-roundtrip-v1/roundtrip.jsonl   (ACCEPT queue only)
  havyn-diagnosis-v1/diagnosis.jsonl
  havyn-enhancement-v1/enhancement.jsonl
  havyn-clarification-v1/clarification.jsonl

Split rules:
  - Split by FAMILY (coverage cell for intent/prompt examples, fault class
    for diagnosis, mutation kind for enhancement, removal rule for
    clarification), never by row - near-identical generated siblings must
    not straddle splits.
  - Held-out keeps at least one complete family per task so whole archetypes
    stay unseen during training.
  - Ratios target ~85/10/5 but family granularity wins over exact ratios;
    the audit reports the realised ratios.

Outputs under havyn-train-v1/:
  train.jsonl / validation.jsonl / heldout.jsonl
  audit-report.json (coverage, ratios, duplicates, leakage check)
  dataset-card.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from roundtrip_verify import find_near_duplicates  # noqa: E402
from generate_intents import GENERATOR_VERSION  # noqa: E402
from generate_open_slots import _engine_pin  # noqa: E402

ENGINE_COMMIT_PLACEHOLDER = "engine-commit-from-manifests"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def family_of(task: str, record: dict) -> str:
    if task in ("intent", "prompt"):
        return record.get("coverage_cell") or "unknown"
    if task == "diagnosis":
        causes = record.get("expected_output", {})
        if causes.get("confirmed_cause"):
            return f"fault:{causes['confirmed_cause']}"
        if causes.get("confirmed_causes"):
            return "fault:multi"
        return f"kind:{record.get('kind', 'unknown')}"
    if task == "enhancement":
        kind = record.get("kind")
        if kind == "impossible":
            return "impossible"
        patch = record.get("expected_output", {}).get("patch") or []
        ops = {op.get("path", "").rsplit("/", 2)[0] if op.get("path") else ""
               for op in patch}
        return "mutation:" + "+".join(sorted(ops)[:2])
    if task == "clarification":
        return f"clarification:{record.get('kind', 'unknown')}"
    return "unknown"


def prompt_gate(prompt: dict, roundtrip: dict, model_verified: bool) -> str | None:
    """Which evidence lets this prompt into the prompt->intent task.

    Template prompts are rendered from the intent, so fidelity is
    constructive. Teacher prompts are model-written and need a real ACCEPT
    from an independent-model round-trip; a dry-run ACCEPT is not evidence.
    """
    if prompt.get("teacher_generated"):
        if not (model_verified and roundtrip.get("queue") == "ACCEPT"):
            return None
        return "independent_model_roundtrip_accept"
    return "template_construction"


def split_families(families: list[str], rng: random.Random,
                   heldout_share: float = 0.05, val_share: float = 0.10):
    """Shuffle families; reserve whole families for held-out and validation.

    Family granularity wins over exact ratios, but a task with at least two
    families must keep one family in train. The old arithmetic gave a
    two-family task zero train rows (one held-out + one validation), which is
    a silent training hole, not a valid split.
    """
    shuffled = sorted(families)
    rng.shuffle(shuffled)
    n = len(shuffled)
    if n == 0:
        return set(), set(), set()
    if n == 1:
        return set(shuffled), set(), set()

    n_heldout = max(1, round(n * heldout_share)) if n >= 3 else 0
    n_val = max(1, round(n * val_share))
    while n - n_heldout - n_val < 1:
        if n_heldout > 0:
            n_heldout -= 1
        elif n_val > 1:
            n_val -= 1
        else:
            break
    heldout = set(shuffled[:n_heldout])
    val = set(shuffled[n_heldout:n_heldout + n_val])
    train = set(shuffled[n_heldout + n_val:])
    return train, val, heldout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets-root", default="havyn-stage5/datasets")
    parser.add_argument("--out", default="havyn-stage5/datasets/havyn-train-v1")
    parser.add_argument("--seed", type=int, default=20261028)
    args = parser.parse_args()

    root = Path(args.datasets_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    intents = read_jsonl(root / "havyn-intents-v1" / "valid.jsonl")
    roundtrip = read_jsonl(root / "havyn-roundtrip-v1" / "roundtrip.jsonl")
    prompts = read_jsonl(root / "havyn-prompts-v1" / "prompts.jsonl")
    diagnosis = read_jsonl(root / "havyn-diagnosis-v1" / "diagnosis.jsonl")
    enhancement = read_jsonl(root / "havyn-enhancement-v1" / "enhancement.jsonl")
    clarification = read_jsonl(root / "havyn-clarification-v1" / "clarification.jsonl")

    # valid.jsonl rows are keyed by their stable `id`; `hash` does not exist.
    intents_by_hash = {r.get("hash") or r.get("id"): r for r in intents}
    roundtrip_by_prompt = {r.get("prompt_id"): r for r in roundtrip}
    roundtrip_method = {
        r.get("verification_method") for r in roundtrip
    }
    model_verified = "independent_model_reconstruction" in roundtrip_method

    # Prompt gating:
    #   - a teacher prompt needs a real (independent-model) ACCEPT: it is
    #     model-written and only the round-trip proves it carries the intent;
    #   - a template prompt is rendered from the intent itself, so fidelity is
    #     constructive. A dry-run ACCEPT is not evidence of anything and is
    #     never used to gate; the record says which gate applied.
    prompt_pairs = []
    for p in prompts:
        source = intents_by_hash.get(p.get("intent_hash"))
        if source is None:
            continue
        trip = roundtrip_by_prompt.get(p.get("id"), {})
        gate = prompt_gate(p, trip, model_verified)
        if gate is None:
            continue
        prompt_pairs.append({
            "prompt": p["prompt"],
            "language": p.get("language"),
            "style": p.get("style"),
            "intent": source["intent"],
            "coverage_cell": p.get("coverage_cell"),
            "prompt_gate": gate,
            "roundtrip_queue": trip.get("queue"),
            "roundtrip_method": trip.get("verification_method"),
        })

    commits = set()
    manifests = {
        "intent": root / "havyn-intents-v1" / "manifest.json",
        "diagnosis": root / "havyn-diagnosis-v1" / "manifest.json",
        "enhancement": root / "havyn-enhancement-v1" / "manifest.json",
        "clarification": root / "havyn-clarification-v1" / "manifest.json",
    }
    for path in manifests.values():
        if path.exists():
            commits.add(json.loads(path.read_text(encoding="utf-8")).get("engine_commit"))

    tasks = {
        "prompt": prompt_pairs,
        "intent": intents,
        "diagnosis": diagnosis,
        "enhancement": enhancement,
        "clarification": clarification,
    }

    splits = {"train": [], "validation": [], "heldout": []}
    audit = {"per_task": {}, "engine_commits": sorted(c for c in commits if c)}

    for task, records in tasks.items():
        families = sorted({family_of(task, r) for r in records})
        train_f, val_f, held_f = split_families(families, rng)
        counts = {"train": 0, "validation": 0, "heldout": 0}
        seen_hashes = set()
        duplicates = 0
        for record in records:
            digest = hashlib.sha256(json.dumps(
                record, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            if digest in seen_hashes:
                duplicates += 1
                continue
            seen_hashes.add(digest)
            family = family_of(task, record)
            bucket = ("heldout" if family in held_f
                      else "validation" if family in val_f else "train")
            splits[bucket].append({
                "task": task,
                "family": family,
                "payload": record,
            })
            counts[bucket] += 1
        total = sum(counts.values()) or 1
        audit["per_task"][task] = {
            "examples": sum(counts.values()),
            "duplicates_skipped": duplicates,
            "families": len(families),
            "counts": counts,
            "ratios": {k: round(v / total, 3) for k, v in counts.items()},
        }
        if task == "prompt":
            gates: dict[str, int] = {}
            for record in records:
                gate = record.get("prompt_gate", "unknown")
                gates[gate] = gates.get(gate, 0) + 1
            audit["per_task"][task]["prompt_gates"] = gates

    # Leakage check: within one task, an identical payload must never appear
    # in two buckets. (Across tasks an intent and its prompt pair share
    # content by design; the guarantee is per task.)
    seen: dict[tuple[str, str], str] = {}
    leaks = []
    for bucket, rows in splits.items():
        for row in rows:
            digest = hashlib.sha256(json.dumps(
                row["payload"], sort_keys=True, default=str)
                .encode("utf-8")).hexdigest()
            key = (row["task"], digest)
            if key in seen and seen[key] != bucket:
                leaks.append(
                    f"{row['task']} {digest[:12]} in {seen[key]} and {bucket}")
            seen[key] = bucket
    audit["leaks"] = leaks

    # Near-duplicate prompts across buckets (text level).
    for bucket, rows in splits.items():
        prompt_rows = [
            {"id": f"{bucket}-{i}", "prompt": json.dumps(
                r["payload"].get("intent") or r["payload"], default=str)}
            for i, r in enumerate(rows) if r["task"] in ("prompt", "intent")
        ]
        flagged = find_near_duplicates(prompt_rows, threshold=0.95)
        audit.setdefault("near_duplicate_prompts", {})[bucket] = len(flagged)

    for bucket, rows in splits.items():
        rng.shuffle(rows)
        with (out / f"{bucket}.jsonl").open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    total_rows = sum(len(rows) for rows in splits.values())
    audit["total"] = total_rows
    audit["ratios"] = {
        bucket: round(len(rows) / total_rows, 3) if total_rows else 0
        for bucket, rows in splits.items()
    }
    audit["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    audit["verification_commit"] = _engine_pin()
    audit["provenance"] = {
        "origin": "synthetic_ai",
        "human_contributor": False,
        "owner_authored": False,
        "gold_records_included": 0,
    }
    audit["generator_version"] = GENERATOR_VERSION
    audit["seed"] = args.seed

    card = {
        "name": "havyn-train-v1",
        "created": audit["generated"],
        "engine_commits": audit["engine_commits"],
        "generator_version": GENERATOR_VERSION,
        "seed": args.seed,
        "tasks": {t: a["examples"] for t, a in audit["per_task"].items()},
        "splits": audit["ratios"],
        "leaks": len(leaks),
        "verification_commit": audit["verification_commit"],
        "provenance": audit["provenance"],
        "roundtrip_verification": {
            "methods": sorted(m for m in roundtrip_method if m),
            "independent_model_verified": model_verified,
        },
        "notes": [
            "Split by family, never by row: siblings of one archetype never "
            "straddle splits.",
            "Held-out contains complete families - whole archetypes the "
            "model never saw in training. A task with fewer than three "
            "families cannot hold one out and keep one in train; the audit "
            "reports 0 heldout for it rather than hiding it.",
            "Prompt gating: template prompts are rendered from the intent "
            "(fidelity by construction, prompt_gate=template_construction). "
            "Teacher prompts require an ACCEPT from an independent-model "
            "round-trip; the offline dry-run (reconstruction == source) is "
            "never accepted as that evidence.",
        ],
    }
    (out / "audit-report.json").write_text(
        json.dumps(audit, indent=2), encoding="utf-8")
    (out / "dataset-card.json").write_text(
        json.dumps(card, indent=2), encoding="utf-8")
    print(f"assembled {total_rows} examples -> {out}")
    print(json.dumps(audit["ratios"], indent=1))
    if leaks:
        print(f"LEAKS: {len(leaks)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
