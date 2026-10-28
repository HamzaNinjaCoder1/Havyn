"""S5.10 round-trip verification.

For every prompt: an INDEPENDENT model family (different from the teacher
and from the fine-tune base) reconstructs the intent from the prompt; the
reconstruction is compared field-by-field with the source intent.

Queues (nothing is auto-deleted):
  ACCEPT  - reconstruction agrees on all requirement-bearing fields
  REVIEW  - difficult but plausibly valid: minor field drift, ambiguous
            phrasing - a human looks at these rather than the model deciding

Near-duplicate prompts are flagged with MinHash over shingles.

Offline mode (`--dry-run`) exercises the comparison and queueing logic over
template prompts whose reconstruction is the source intent itself - useful
for tests; real verification needs the GPU runner.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from benchmark_harness import compare_intents, extract_json  # noqa: E402
from generate_intents import GENERATOR_VERSION  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402

# Requirement-bearing field paths that MUST round-trip exactly.
CRITICAL_PATHS = (
    "vlans", "sites", "requirements", "routing", "firewall", "internet",
)


def _minhash_signature(text: str, permutations: int = 64, width: int = 4,
                       seed: int = 1) -> list[int]:
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    shingles = {" ".join(words[i:i + width])
                for i in range(max(1, len(words) - width + 1))}
    if not shingles:
        return [0] * permutations
    rng = random.Random(seed)
    salts = [rng.getrandbits(32) for _ in range(permutations)]
    signature = []
    for salt in salts:
        signature.append(min(
            (hash((salt, shingle)) & 0xFFFFFFFF) for shingle in shingles))
    return signature


def _jaccard(a: list[int], b: list[int]) -> float:
    matches = sum(1 for x, y in zip(a, b) if x == y)
    return matches / len(a) if a else 0.0


def find_near_duplicates(records: list[dict], threshold: float = 0.9) -> set[str]:
    """Ids of prompts nearly identical to an earlier prompt."""
    flagged: set[str] = set()
    seen: list[tuple[str, list[int]]] = []
    for record in records:
        sig = _minhash_signature(record["prompt"])
        for prior_id, prior_sig in seen:
            if _jaccard(sig, prior_sig) >= threshold:
                flagged.add(record["id"])
                break
        seen.append((record["id"], sig))
    return flagged


def classify(example: dict, reconstructed: HavynIntent | None) -> tuple[str, str]:
    """ACCEPT / REVIEW / REJECT with a reason."""
    if reconstructed is None:
        return "REJECT", "reconstruction was not a valid intent"
    source = HavynIntent.model_validate(example["intent"])
    result = compare_intents(source, reconstructed)
    if result["fields_total"] == 0:
        return "REVIEW", "no comparable fields"
    accuracy = result["field_accuracy"]
    if accuracy == 1.0:
        return "ACCEPT", "full field agreement"
    critical_drift = False
    source_dump = source.model_dump(exclude_none=True)
    recon_dump = reconstructed.model_dump(exclude_none=True)
    for path in CRITICAL_PATHS:
        if path in source_dump and source_dump[path] != recon_dump.get(path):
            critical_drift = True
            break
    if critical_drift:
        return "REJECT", "drift in a requirement-bearing field"
    return "REVIEW", f"field accuracy {accuracy}"


def reconstruct(prompt: str, model, tokenizer, schema_text: str) -> HavynIntent | None:
    """GPU path: ask the independent model to reconstruct the intent."""
    import torch

    message = [
        {"role": "system", "content": (
            "Convert the networking request into one JSON object following "
            "this schema. Output ONLY the JSON object.\n\n" + schema_text)},
        {"role": "user", "content": prompt},
    ]
    text = tokenizer.apply_chat_template(
        message, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer([text], return_tensors="pt").to(model.device)
    with torch.no_grad():
        output = model.generate(**inputs, max_new_tokens=2048, temperature=0.0)
    response = tokenizer.decode(
        output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    raw = extract_json(response)
    if raw is None:
        return None
    try:
        return HavynIntent.model_validate(json.loads(raw))
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", default="havyn-stage5/datasets/havyn-prompts-v1/prompts.jsonl")
    parser.add_argument("--intents", default="havyn-stage5/datasets/havyn-intents-v1/valid.jsonl")
    parser.add_argument("--out", default="havyn-stage5/datasets/havyn-roundtrip-v1")
    parser.add_argument("--model", default=None,
                        help="independent-family model id (GPU required)")
    parser.add_argument("--dry-run", action="store_true",
                        help="reconstruction := source intent; queue logic only")
    args = parser.parse_args()

    intents = [
        json.loads(line)
        for line in Path(args.intents).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_hash = {r.get("hash"): r for r in intents}
    prompts = [
        json.loads(line)
        for line in Path(args.prompts).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    model = tokenizer = schema_text = None
    if args.model:
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from src.network_autopilot.ai.intent import export_json_schema
        schema_text = json.dumps(export_json_schema())
        tokenizer = AutoTokenizer.from_pretrained(args.model)
        model = AutoModelForCausalLM.from_pretrained(
            args.model, torch_dtype="auto", device_map="auto")

    results = []
    queues = {"ACCEPT": 0, "REVIEW": 0, "REJECT": 0}
    for item in prompts:
        source_record = by_hash.get(item.get("intent_hash"))
        if source_record is None:
            continue
        example = {"prompt": item["prompt"], "intent": source_record["intent"]}
        if model is not None:
            reconstruction = reconstruct(item["prompt"], model, tokenizer, schema_text)
        else:
            reconstruction = HavynIntent.model_validate(source_record["intent"])
        queue, reason = classify(example, reconstruction)
        queues[queue] += 1
        results.append({
            "id": item["id"],
            "prompt_id": item["id"],
            "queue": queue,
            "reason": reason,
            "teacher_generated": item.get("teacher_generated", False),
            "language": item.get("language"),
            "coverage_cell": item.get("coverage_cell"),
            "intent_hash": item.get("intent_hash"),
        })

    prompt_text_by_id = {item["id"]: item["prompt"] for item in prompts}
    duplicates = find_near_duplicates(
        [{"id": r["id"], "prompt": prompt_text_by_id[r["id"]]} for r in results])
    for record in results:
        if record["id"] in duplicates and record["queue"] == "ACCEPT":
            record["queue"] = "REVIEW"
            record["reason"] += "; near-duplicate prompt"
    queues = {"ACCEPT": 0, "REVIEW": 0, "REJECT": 0}
    for record in results:
        queues[record["queue"]] += 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "roundtrip.jsonl").open("w", encoding="utf-8") as f:
        for record in results:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    (out / "manifest.json").write_text(json.dumps({
        "dataset": "havyn-roundtrip-v1",
        "generator_version": GENERATOR_VERSION,
        "model": args.model or "(dry-run: reconstruction == source)",
        "examples": len(results),
        "queues": queues,
        "near_duplicates_flagged": len(duplicates),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    print(f"round-trip: {queues} (near-duplicates flagged: {len(duplicates)})")


if __name__ == "__main__":
    main()
