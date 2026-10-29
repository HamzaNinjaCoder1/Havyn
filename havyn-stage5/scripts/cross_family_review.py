"""Run the prepared 56-record cross-family review on a different model family.

This script is the executor for `reports/cross-family-review-protocol.md`. It
does NOT fabricate verdicts: if the model fails to produce a valid verdict for
a record, that record is recorded as UNPARSED with the raw output.

It also has an offline half (`--precheck-only`) that checks everything that can
be checked without a model: package SHA, engine verification, language labels,
refusal shape, duplicates, secrets and baseline decisions.

Usage (offline):
    python cross_family_review.py --precheck-only
    python cross_family_review.py --dry-run

Usage (GPU / different family):
    REVIEW_MODEL_ID=mistralai/Mistral-Small-24B-Instruct-2501 \
    python cross_family_review.py --model "$REVIEW_MODEL_ID" --load-in-4bit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "havyn-stage5" / "scripts"
REVIEW_DIR = REPO / "havyn-stage5" / "prompt-authoring" / "review"
OWNER_DIR = REPO / "havyn-stage5" / "prompt-authoring" / "owner-candidates"
for path in (str(REPO), str(SCRIPTS), str(OWNER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

PACKAGE = REVIEW_DIR / "cross-family-input.jsonl"
MANIFEST = REVIEW_DIR / "cross-family-input.manifest.json"
CANDIDATES = OWNER_DIR / "candidates"
POSTFIX = REVIEW_DIR / "post-fix-recheck.json"
BATCHES = REVIEW_DIR / "model-b-reviews"
DECISIONS = REPO / "havyn-stage5" / "prompt-authoring" / "reports" / "owner_decisions.json"
OUT_ROOT = REVIEW_DIR / "cross-family-reviews"
PRECHECK = REVIEW_DIR / "cross-family-precheck.json"
REGISTRY = REPO / "havyn-stage5" / "configs" / "models.yaml"

VERDICTS = {"ACCEPT", "REVIEW", "REJECT"}
REQUIRED_KEYS = {
    "id", "recovered_intent_summary", "missing_facts", "lost_requirements",
    "invented_facts", "contradictions", "ambiguity_notes", "human_likeness",
    "fidelity", "verdict", "reason",
}

REVIEWER_SYSTEM = """\
You are an independent adversarial reviewer for a networking-intent dataset.
You will receive ONE JSON record with: id, prompt, expect_refusal,
expected_intent, expected_behaviour.

Decide whether the prompt and the source intent describe the same network
request, and whether the expected behaviour is the single correct response.
Do NOT trust the intent as ground truth merely because it is present. Re-derive
the requirements from the prompt yourself, then compare.

Check:
1. Re-derive sites, areas, host groups and counts, VLANs/segments, addressing
   (DHCP/static), routing mode, internet access and where it enters, servers
   and services, wireless, IoT, firewall/DMZ, exclusions, priorities.
2. Compare with expected_intent field by field:
   - missing_facts: facts in the intent that the prompt never states.
   - lost_requirements: facts in the prompt that the intent does not carry.
   - invented_facts: requirements or devices not in the prompt.
   - contradictions: facts that cannot both hold.
3. Check expected_behaviour: for a buildable record, does it state the single
   correct build outcome without claiming verification the engine cannot do?
   For expect_refusal=true, is the named refusal/clarification the single
   correct response, and is the capability claim real?
4. Judge language authenticity (do not translate; English tech terms are fine).
   Flag meta-text, reviewer leftovers, scaffolds and machine enumeration.
5. Verdict: ACCEPT / REVIEW / REJECT.
   ACCEPT = faithful, complete, natural, one definite expected behaviour.
   REVIEW = a human must confirm a deliberate design/capability choice.
   REJECT = invented facts, lost requirements, unresolved contradiction, or
            unnatural/template language a rewrite cannot fix cheaply.

Reply with ONLY one JSON object with exactly these keys:
id, recovered_intent_summary, missing_facts, lost_requirements, invented_facts,
contradictions, ambiguity_notes, human_likeness (0..1), fidelity (0..1),
verdict, reason.
No markdown, no commentary.
"""

ROMAN = re.compile(
    r"\b(hai|chahiye|karna|karo|aur|nahi|bhi|ke|ka|ko|mein|sab|wala|kya|hoga|"
    r"dena|rakhna|rakhein|zaroorat|sahi|theek|rahe|jayenge|kar)\b", re.I)
ENGLISH = re.compile(
    r"\b(the|and|with|for|so|that|but|keep|provide|need|make|use|from)\b", re.I)
SECRETS = re.compile(
    r"(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|"
    r"BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]\s*\S+|secret\s+\S+)", re.I)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def load_package() -> tuple[list[dict], dict]:
    if not PACKAGE.exists() or not MANIFEST.exists():
        raise SystemExit(f"missing package or manifest in {REVIEW_DIR}")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    digest = sha256_file(PACKAGE)
    if digest != manifest["sha256"]:
        raise SystemExit(
            f"package SHA mismatch: file {digest} != manifest {manifest['sha256']}")
    rows = [json.loads(line) for line in
            PACKAGE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != manifest["records"]:
        raise SystemExit(
            f"package has {len(rows)} records, manifest says {manifest['records']}")
    return rows, manifest


def language_guess(prompt: str) -> str:
    roman = bool(ROMAN.search(prompt))
    english = bool(ENGLISH.search(prompt))
    if roman and english:
        return "mixed"
    if roman:
        return "roman-urdu"
    return "en"


def baseline_map() -> dict[str, dict]:
    """Previous verdict per candidate and where it came from."""
    base: dict[str, dict] = {}
    for path in sorted(BATCHES.glob("batch-*.json")):
        for entry in json.loads(path.read_text(encoding="utf-8")):
            if entry.get("verdict"):
                base[entry["id"]] = {
                    "verdict": entry["verdict"].upper(),
                    "source": path.stem,
                }
    if POSTFIX.exists():
        payload = json.loads(POSTFIX.read_text(encoding="utf-8"))
        for entry in payload.get("entries", []):
            if entry.get("post_fix_verdict"):
                base[entry["id"]] = {
                    "verdict": entry["post_fix_verdict"].upper(),
                    "source": "post-fix-recheck",
                }
    if DECISIONS.exists():
        payload = json.loads(DECISIONS.read_text(encoding="utf-8"))
        mapping = {"BUILD": "ACCEPT", "BUILD_AFTER_REWRITE": "REVIEW",
                   "REFUSAL": "ACCEPT"}
        for decision in payload.get("decisions", []):
            verdict = mapping.get(decision.get("technical_recommendation"))
            if verdict:
                base[decision["id"]] = {
                    "verdict": verdict,
                    "source": "owner-decision (technical, not approval)",
                }
    return base


def precheck(run_engine: bool = True, write: bool = True) -> dict:
    rows, manifest = load_package()
    candidates = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CANDIDATES.glob("oc-*.json"))
    }
    base = baseline_map()
    issues: list[dict] = []
    seen_prompts: dict[str, str] = {}

    def issue(record_id: str, kind: str, detail: str) -> None:
        issues.append({"id": record_id, "kind": kind, "detail": detail})

    engine_ok = 0
    engine_fail = []
    verify_record = None
    if run_engine:
        from verify_candidates import verify_record  # noqa: F401

    for row in rows:
        rid = row["id"]
        candidate = candidates.get(rid)
        if candidate is None:
            issue(rid, "candidate_missing", "no owner candidate for this id")
            continue
        if row.get("prompt_hash") != hashlib.sha256(
                row["prompt"].encode("utf-8")).hexdigest():
            issue(rid, "prompt_hash", "package prompt_hash does not match prompt")
        if row["prompt"] in seen_prompts:
            issue(rid, "duplicate_prompt", f"same prompt as {seen_prompts[row['prompt']]}")
        seen_prompts[row["prompt"]] = rid

        labelled = candidate.get("language")
        guessed = language_guess(row["prompt"])
        if labelled == "mixed" and guessed != "mixed":
            issue(rid, "language_label", f"labelled mixed, prompt looks {guessed}")
        elif labelled == "roman-urdu" and guessed == "en":
            issue(rid, "language_label", "labelled roman-urdu, prompt has no Roman-Urdu markers")
        elif labelled == "en" and guessed != "en":
            issue(rid, "language_label", f"labelled en, prompt looks {guessed}")

        refusal = bool(row.get("expect_refusal"))
        if refusal and row.get("expected_intent") is not None:
            issue(rid, "refusal_shape", "expect_refusal=true but expected_intent is not null")
        if not refusal and row.get("expected_intent") is None:
            issue(rid, "build_shape", "expect_refusal=false but expected_intent is null")
        if refusal and not row.get("expected_behaviour"):
            issue(rid, "refusal_shape", "refusal has no expected_behaviour")

        blob = json.dumps(row, ensure_ascii=False)
        if SECRETS.search(blob):
            issue(rid, "secret_like", "credential-like text in the package record")

        if run_engine:
            check = verify_record({
                "id": rid,
                "prompt": row["prompt"],
                "expected_intent": row.get("expected_intent"),
                "expect_refusal": refusal,
            })
            if check["engine_verified"]:
                engine_ok += 1
            else:
                engine_fail.append({"id": rid, "stage": check["failure_stage"]})

    # Near-duplicate prompts at 0.90.
    try:
        from roundtrip_verify import find_near_duplicates

        flagged = find_near_duplicates(
            [{"id": row["id"], "prompt": row["prompt"]} for row in rows],
            threshold=0.90)
        for rid in sorted(flagged):
            issue(rid, "near_duplicate", "MinHash >= 0.90 against an earlier prompt")
    except Exception as exc:  # noqa: BLE001
        issue("package", "near_duplicate_check", f"not run: {exc}")

    report = {
        "input_package": {
            "file": str(PACKAGE.relative_to(REPO)),
            "sha256": manifest["sha256"],
            "records": len(rows),
        },
        "candidate_languages": {
            lang: sum(1 for c in candidates.values() if c.get("language") == lang)
            for lang in ("en", "roman-urdu", "mixed")
        },
        "baseline_verdicts": {
            "entries": len(base),
            "by_verdict": {
                verdict: sum(1 for v in base.values() if v["verdict"] == verdict)
                for verdict in sorted(VERDICTS)
            },
        },
        "engine": {
            "checked": len(rows) if run_engine else 0,
            "verified": engine_ok,
            "failures": engine_fail,
        },
        "issues": issues,
        "passed": not engine_fail and not [
            i for i in issues
            if i["kind"] in {"candidate_missing", "prompt_hash", "refusal_shape",
                             "build_shape", "duplicate_prompt", "near_duplicate",
                             "secret_like"}
        ],
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    PRECHECK.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return report


def normalize_review(obj: object, expected_id: str) -> tuple[dict | None, str | None]:
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    missing = REQUIRED_KEYS - set(obj)
    if missing:
        return None, f"missing keys: {sorted(missing)}"
    if obj.get("id") != expected_id:
        return None, f"id mismatch: {obj.get('id')} != {expected_id}"
    verdict = str(obj.get("verdict", "")).upper().strip()
    if verdict not in VERDICTS:
        return None, f"invalid verdict: {obj.get('verdict')!r}"
    obj["verdict"] = verdict
    for key in ("missing_facts", "lost_requirements", "invented_facts",
                "contradictions", "ambiguity_notes"):
        if not isinstance(obj.get(key), list):
            return None, f"{key} is not a list"
    for key in ("human_likeness", "fidelity"):
        value = obj.get(key)
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            return None, f"{key} is not a number in [0,1]"
    if not isinstance(obj.get("reason"), str) or not obj["reason"].strip():
        return None, "reason is empty"
    return obj, None


def model_family(model_id: str) -> str:
    try:
        import yaml
        registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
        for group in ("standard_candidates", "lite_candidates"):
            for candidate in registry.get(group, []):
                if candidate["model_id"] == model_id:
                    return candidate["family"]
        for role in ("teacher", "review"):
            if registry.get(role, {}).get("model_id") == model_id:
                return registry[role]["family"]
    except Exception:
        pass
    return model_id.split("/", 1)[0] if "/" in model_id else "unknown"


def run_review(model_id: str, out_dir: Path, limit: int | None,
               max_new_tokens: int, load_in_4bit: bool, temperature: float,
               retries: int, trust_remote_code: bool) -> dict:
    rows, manifest = load_package()
    if limit:
        rows = rows[:limit]

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from benchmark_harness import extract_json

    tokenizer = AutoTokenizer.from_pretrained(
        model_id, trust_remote_code=trust_remote_code)
    quant_config = None
    if load_in_4bit and torch.cuda.is_available():
        from transformers import BitsAndBytesConfig

        quant_config = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_id, torch_dtype="auto", device_map="auto",
        quantization_config=quant_config,
        trust_remote_code=trust_remote_code)
    model.eval()

    revision = getattr(model.config, "_commit_hash", None)
    try:
        from huggingface_hub import HfApi
        revision = HfApi().model_info(model_id).sha or revision
    except Exception:
        pass

    base = baseline_map()
    results = []
    for index, row in enumerate(rows, 1):
        user = json.dumps(row, ensure_ascii=False)
        messages = [{"role": "system", "content": REVIEWER_SYSTEM},
                    {"role": "user", "content": user}]
        raw = ""
        verdict_obj = None
        error = None
        for attempt in range(retries + 1):
            text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)
            inputs = tokenizer([text], return_tensors="pt").to(model.device)
            gen_kwargs = {
                "max_new_tokens": max_new_tokens,
                "do_sample": temperature > 0,
                "pad_token_id": tokenizer.eos_token_id,
            }
            if temperature > 0:
                gen_kwargs["temperature"] = temperature
            with torch.no_grad():
                output = model.generate(**inputs, **gen_kwargs)
            raw = tokenizer.decode(
                output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
            parsed = extract_json(raw)
            if parsed is not None:
                verdict_obj, error = normalize_review(parsed, row["id"])
            else:
                error = "no JSON object in output"
            if verdict_obj is not None:
                break
            if attempt < retries:
                messages = [
                    {"role": "system", "content": REVIEWER_SYSTEM},
                    {"role": "user", "content":
                     user + "\n\nYour previous reply was not valid: " + str(error)
                     + "\nReturn ONLY the required JSON object for this record."},
                ]
        baseline = base.get(row["id"])
        results.append({
            "id": row["id"],
            "verdict": verdict_obj["verdict"] if verdict_obj else None,
            "review": verdict_obj,
            "baseline_verdict": baseline["verdict"] if baseline else None,
            "baseline_source": baseline["source"] if baseline else None,
            "agrees_with_baseline": (
                verdict_obj is not None and baseline is not None
                and verdict_obj["verdict"] == baseline["verdict"]
            ),
            "error": error,
            "raw_output": raw,
        })
        print(f"[review] {index}/{len(rows)} {row['id']} -> "
              f"{results[-1]['verdict'] or 'UNPARSED'}", flush=True)

    counts = {verdict: 0 for verdict in sorted(VERDICTS)}
    for result in results:
        if result["verdict"]:
            counts[result["verdict"]] += 1
    disagreements = [
        result["id"] for result in results
        if result["verdict"] and result["baseline_verdict"]
        and not result["agrees_with_baseline"]
    ]
    unparsed = [result["id"] for result in results if not result["verdict"]]
    payload = {
        "provider": "local-transformers",
        "model": model_id,
        "family": model_family(model_id),
        "model_revision": revision,
        "inference_environment": {
            "device": "cuda" if torch.cuda.is_available() else "cpu",
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "load_in_4bit": bool(load_in_4bit and torch.cuda.is_available()),
        },
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_sha256": manifest["sha256"],
        "temperature": temperature,
        "records_requested": len(rows),
        "records": results,
        "summary": {
            "reviewed": len(results),
            "ACCEPT": counts["ACCEPT"],
            "REVIEW": counts["REVIEW"],
            "REJECT": counts["REJECT"],
            "unparsed": unparsed,
            "agreement": sum(1 for r in results if r["agrees_with_baseline"]),
            "disagreements": disagreements,
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", model_id)
    path = out_dir / f"local-transformers-{slug}-{datetime.now(timezone.utc):%Y-%m-%d}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))
    print(f"review written to {path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.environ.get("REVIEW_MODEL_ID"))
    parser.add_argument("--out-dir", default=str(OUT_ROOT))
    parser.add_argument("--precheck-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-new-tokens", type=int, default=1500)
    parser.add_argument("--load-in-4bit", action="store_true", default=True)
    parser.add_argument("--no-4bit", action="store_false", dest="load_in_4bit")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--trust-remote-code", action="store_true")
    args = parser.parse_args()

    if args.precheck_only or args.dry_run:
        report = precheck(run_engine=not args.dry_run)
        print(json.dumps({
            "passed": report["passed"],
            "records": report["input_package"]["records"],
            "engine_verified": report["engine"]["verified"],
            "issues": len(report["issues"]),
            "report": str(PRECHECK),
        }, indent=2))
        if args.dry_run:
            print(json.dumps({
                "dry_run": True,
                "would_run": args.model or "<REVIEW_MODEL_ID not set>",
                "records": report["input_package"]["records"],
            }, indent=2))
        return

    if not args.model:
        raise SystemExit(
            "REVIEW_MODEL_ID is not set and --model was not passed. Use a "
            "different family from the teacher/fine-tune base, e.g. "
            "mistralai/Mistral-Small-24B-Instruct-2501.")
    if model_family(args.model).lower().startswith("qwen"):
        raise SystemExit("refusing a Qwen-family reviewer: it is not cross-family")
    run_review(args.model, Path(args.out_dir), args.limit, args.max_new_tokens,
               args.load_in_4bit, args.temperature, args.retries,
               args.trust_remote_code)


if __name__ == "__main__":
    main()
