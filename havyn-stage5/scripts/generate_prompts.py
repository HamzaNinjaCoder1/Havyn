"""S5.9 prompt-writer.

Produces ~2 prompts per valid intent:

  - the deterministic template share (30%): render_prompt variations with
    persona and style wrappers, both languages. These need no external
    model and are reproducible bit-for-bit.
  - the teacher-model share (70%): on a GPU runtime, an open-weight teacher
    writes a natural prompt from the intent under a STRICT rule: the prompt
    may only contain information present in the intent. Teacher outputs are
    tagged teacher_generated=true and land in the REVIEW queue until
    round-trip verification (S5.10) accepts them.

Usage (offline, template share only):
    python generate_prompts.py --valid <valid.jsonl> --out <dir> --share 0.3

Usage (Colab, full):
    python generate_prompts.py --valid <valid.jsonl> --out <dir> \
        --teacher Qwen/Qwen3-32B --share 0.3
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

from benchmark_harness import render_prompt  # noqa: E402
from generate_intents import GENERATOR_VERSION  # noqa: E402
from generate_open_slots import _engine_pin  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402

PERSONAS = [
    ("student", "I'm a networking student practising, "),
    ("it-admin", "I manage IT for a small company, "),
    ("beginner", "I don't know much about networking, sorry, "),
    ("rushed", "Quick one, need this today: "),
    ("architect", "For a client design document, "),
    ("stakeholder", "From the business side: "),
]

TEACHER_SYSTEM = """\
You write natural networking requests. You are given a structured intent
(JSON). Write a prompt a real user could have sent that expresses EXACTLY
this intent - no more, no less.

STRICT RULES:
1. Every requirement, device, VLAN, server, routing choice and constraint in
   the intent MUST be expressible from your prompt alone.
2. Invent NOTHING: no extra devices, no extra hosts, no security policies
   the intent does not contain, no brand names, no deadlines.
3. You may choose tone, word order and level of detail, and you may leave
   out details a deterministic planner can safely assume (the engine will).
4. Reply with ONLY the prompt text - no quotes, no commentary.
"""


def intent_key(record: dict) -> str | None:
    """Stable key for an intent dataset row.

    ``valid.jsonl`` rows carry ``id``; ``hash`` was never a field. Reading
    only ``hash`` silently returned None for every row and collapsed every
    prompt onto one intent downstream.
    """
    return record.get("hash") or record.get("id")


def template_prompts(intent: HavynIntent, rng: random.Random) -> list[dict]:
    out = []
    persona, prefix = PERSONAS[rng.randrange(len(PERSONAS))]
    language = "roman-urdu" if rng.random() < 0.35 else "en"
    text = render_prompt(intent, language=language)
    if language == "en":
        text = prefix + text[0].lower() + text[1:]
    out.append({
        "prompt": text,
        "language": language,
        "style": f"template/{persona}",
        "teacher_generated": False,
    })
    # A second, more structured phrasing (document-like) for variety.
    if rng.random() < 0.5:
        doc = render_prompt(intent, language="en")
        out.append({
            "prompt": f"Network requirement document.\n\n{doc}",
            "language": "en",
            "style": "template/document",
            "teacher_generated": False,
        })
    return out


def teacher_prompts(intents: list[HavynIntent], model_id: str, out_dir: Path,
                    rng: random.Random) -> list[dict]:
    """GPU path: batch the teacher over intents, write raw outputs for
    review. Import torch only here so the offline share never needs it."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id, torch_dtype="auto", device_map="auto")
    model.eval()
    results = []
    for index, intent in enumerate(intents):
        message = [
            {"role": "system", "content": TEACHER_SYSTEM},
            {"role": "user", "content": json.dumps(intent.model_dump())},
        ]
        text = tokenizer.apply_chat_template(
            message, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer([text], return_tensors="pt").to(model.device)
        with torch.no_grad():
            output = model.generate(
                **inputs, max_new_tokens=512, do_sample=True, temperature=0.8,
                top_p=0.95)
        prompt_text = tokenizer.decode(
            output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        results.append({
            "prompt": prompt_text.strip(),
            "language": "en",
            "style": "teacher",
            "teacher_generated": True,
            "teacher_model": model_id,
            "intent_index": index,
        })
        if index % 100 == 0:
            print(f"[teacher] {index}/{len(intents)}")
    del model
    torch.cuda.empty_cache()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--valid", default="havyn-stage5/datasets/havyn-intents-v1/valid.jsonl")
    parser.add_argument("--out", default="havyn-stage5/datasets/havyn-prompts-v1")
    parser.add_argument("--share", type=float, default=0.3,
                        help="template share; the rest goes to the teacher")
    parser.add_argument("--teacher", default=None,
                        help="teacher model id (GPU required)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--per-cell", type=int, default=None,
                        help="cap source intents per coverage cell (spreads families)")
    parser.add_argument("--seed", type=int, default=20261028)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    records = [
        json.loads(line)
        for line in Path(args.valid).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.per_cell:
        per_cell: dict[str, int] = {}
        capped = []
        for record in records:
            cell = record.get("coverage_cell") or "unknown"
            if per_cell.get(cell, 0) < args.per_cell:
                capped.append(record)
                per_cell[cell] = per_cell.get(cell, 0) + 1
        records = capped
    if args.limit:
        records = records[:args.limit]

    prompts = []
    hashes = set()
    teacher_records = []
    for record in records:
        intent = HavynIntent.model_validate(record["intent"])
        for item in template_prompts(intent, rng):
            digest = hashlib.sha256(item["prompt"].encode("utf-8")).hexdigest()
            if digest in hashes:
                continue
            hashes.add(digest)
            prompts.append({
                "id": f"pr-{len(prompts):06d}",
                "intent_hash": intent_key(record),
                "coverage_cell": record.get("coverage_cell"),
                "verification_method": (
                    "teacher_roundtrip" if item.get("teacher_generated")
                    else "template_construction"
                ),
                **item,
            })
        if args.teacher and rng.random() >= args.share:
            teacher_records.append(intent)

    if args.teacher:
        prompts.extend(teacher_prompts(teacher_records, args.teacher, out, rng))

    with (out / "prompts.jsonl").open("w", encoding="utf-8") as f:
        for item in prompts:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    (out / "manifest.json").write_text(json.dumps({
        "dataset": "havyn-prompts-v1",
        "generator_version": GENERATOR_VERSION,
        "seed": args.seed,
        "prompts": len(prompts),
        "template": sum(1 for p in prompts if not p["teacher_generated"]),
        "teacher": sum(1 for p in prompts if p["teacher_generated"]),
        "teacher_model": args.teacher,
        "engine_commit": _engine_pin(),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    print(f"wrote {len(prompts)} prompts to {out}")


if __name__ == "__main__":
    main()
