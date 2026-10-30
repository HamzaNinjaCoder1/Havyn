"""S5.19/S5.20 evaluation gates and live gate.

Gate thresholds (spec S5.19) - a failed gate BLOCKS the release, it is never
converted to a pass:

  schema_valid      = 100%  (every output parses and validates as HavynIntent)
  lint_pass         >= 95%
  gold_score        >= 90%  (private gold set; NEVER part of training data)
  diagnosis_class   >= 85%  per class (fault / healthy / unknown / multi-fault)
  live_pass         >= 90%  (S5.20: 50 gold prompts through live Packet
                             Tracer; exact X/Y reported)

The scoring half is pure and offline-testable. The generation half needs a
GPU; the live half needs Packet Tracer. When either is unavailable the gate
is reported NOT VERIFIED with the reason - never skipped silently, and a
NOT VERIFIED release gate blocks the release just like a FAIL.

Usage (see notebooks/10-evaluation-export.ipynb):
  python evaluate_model.py --model checkpoints/standard \
      --gold havyn-stage5/datasets/gold-v1 --out havyn-stage5/evaluation

Offline scoring of a pre-generated file (also how tests drive this):
  python evaluate_model.py --generations-file gens.jsonl \
      --gold havyn-stage5/datasets/gold-v1 --out ...
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from benchmark_harness import _compare, extract_json  # noqa: E402
from format_examples import format_split, load_config  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402

GATES = {
    "schema_valid": {"threshold": 1.00},
    "lint_pass": {"threshold": 0.95},
    "gold_score": {"threshold": 0.90},
    "diagnosis_class": {"threshold": 0.85},
    "live_pass": {"threshold": 0.90},
}

REFUSAL_MARKERS = (
    "cannot", "not possible", "not supported", "unsupported",
    "missing information", "need", "clarif", "specify",
)
DIAGNOSIS_CLASSES = ("fault", "healthy", "unknown", "multi-fault")


def _parse_body(text: str) -> Any:
    raw = extract_json(text)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def diagnosis_class_of(family: str) -> str:
    """family is 'kind:<x>' or 'fault:<cause>' / 'fault:multi'."""
    if family.startswith("kind:"):
        cls = family.split(":", 1)[1]
        return cls if cls in DIAGNOSIS_CLASSES else "fault"
    if family.startswith("fault:multi"):
        return "multi-fault"
    return "fault"


# --- intent scoring ----------------------------------------------------------

def score_intent_output(text: str, expected_intent: dict | None,
                        expect_refusal: bool) -> dict[str, Any]:
    """Score one raw model output against the expected intent (or refusal)."""
    result: dict[str, Any] = {
        "is_valid_json": False, "schema_valid": False, "lint_pass": False,
        "field_accuracy": 0.0, "refusal_correct": False, "verdict": "fail",
    }
    body = _parse_body(text)
    if body is None:
        if expect_refusal:
            lowered = text.lower()
            result["refusal_correct"] = any(m in lowered for m in REFUSAL_MARKERS)
            result["verdict"] = "pass" if result["refusal_correct"] else "fail"
        return result
    result["is_valid_json"] = True
    if expect_refusal:
        # A schema-valid intent where a refusal was expected is a wrong answer.
        return result
    try:
        intent = HavynIntent.model_validate(body)
    except Exception:
        return result
    result["schema_valid"] = True
    findings = lint_intent(intent)
    result["lint_pass"] = not findings.blocking
    if expected_intent is None:
        result["verdict"] = "pass" if result["lint_pass"] else "fail"
        return result
    counter: list[int] = [0, 0]
    _compare(expected_intent, body, counter)
    accuracy = counter[0] / counter[1] if counter[1] else 0.0
    result["field_accuracy"] = accuracy
    result["verdict"] = (
        "pass" if accuracy == 1.0 and result["lint_pass"] else "fail")
    return result


# --- task scoring ------------------------------------------------------------

def score_diagnosis_output(text: str, expected: dict,
                           cls: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "class": cls, "verdict_correct": False, "cause_correct": False,
        "repair_correct": False, "is_valid_json": False,
    }
    body = _parse_body(text)
    if not isinstance(body, dict):
        return result
    result["is_valid_json"] = True
    result["verdict_correct"] = body.get("verdict") == expected.get("verdict")
    expected_causes = sorted(filter(None, [
        expected.get("confirmed_cause"),
        *(expected.get("confirmed_causes") or []),
    ]))
    got_causes = sorted(filter(None, [
        body.get("confirmed_cause"),
        *(body.get("confirmed_causes") or []),
    ]))
    result["cause_correct"] = expected_causes == got_causes
    result["repair_correct"] = bool(
        body.get("repair_recommended")) == bool(expected.get("repair_recommended"))
    return result


def score_clarification_output(text: str, expected: dict) -> dict[str, Any]:
    result: dict[str, Any] = {"is_valid_json": False, "correct": False}
    body = _parse_body(text)
    if not isinstance(body, dict):
        return result
    result["is_valid_json"] = True
    needs = bool(expected.get("needs_clarification"))
    got_needs = bool(body.get("needs_clarification"))
    questions = body.get("questions") or []
    result["correct"] = got_needs == needs and (
        (needs and bool(questions)) or not needs)
    return result


def score_enhancement_output(text: str, payload: dict) -> dict[str, Any]:
    """The strong check: an expected patch must apply to before_intent,
    validate as HavynIntent, and equal after_intent. An expected refusal must
    name the unsupported feature instead of producing an intent."""
    result: dict[str, Any] = {
        "is_valid_json": False, "patch_correct": False, "refusal_correct": False,
    }
    expected = payload.get("expected_output") or {}
    body = _parse_body(text)
    if expected.get("refusal"):
        # A refusal is text naming the unsupported feature; an intent object
        # where a refusal was owed is a wrong answer.
        if isinstance(body, dict) and any(
                k in body for k in ("sites", "vlans")):
            return result
        marker = expected["refusal"].split("-")[0]
        result["refusal_correct"] = marker in text.lower()
        return result
    if not isinstance(body, dict):
        return result
    result["is_valid_json"] = True
    before = payload.get("before_intent")
    after = payload.get("after_intent")
    try:
        patched = apply_json_patch(before, body.get("patch") or [])
        HavynIntent.model_validate(patched)
    except Exception:
        return result
    result["patch_correct"] = (
        json.dumps(patched, sort_keys=True)
        == json.dumps(after, sort_keys=True))
    return result


def apply_json_patch(doc: dict, ops: list[dict]) -> dict:
    """Minimal JSON-Patch subset (add/remove/replace) used by the
    enhancement scorer."""
    current = json.loads(json.dumps(doc))
    for op in ops:
        path = str(op.get("path", "")).strip("/")
        parts = ([p.replace("~1", "/").replace("~0", "~")
                  for p in path.split("/")] if path else [])
        if op.get("op") == "add" and not parts:
            current = op.get("value")
            continue
        parent = current
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        last = parts[-1] if parts else None
        if op.get("op") == "remove":
            if isinstance(parent, list):
                parent.pop(int(last))
            else:
                parent.pop(last, None)
        elif isinstance(parent, list):
            idx = int(last) if last != "-" else len(parent)
            parent.insert(idx, op.get("value"))
        else:
            parent[last] = op.get("value")
    return current


# --- gate aggregation --------------------------------------------------------

def compute_gates(results: dict[str, Any]) -> dict[str, Any]:
    """results keys: intent_outputs, gold_scores, diagnosis_scores,
    live (dict or None). Returns the machine-readable gate report."""
    intent = results.get("intent_outputs") or []
    n_intent = len(intent) or 1
    schema = sum(1 for r in intent if r.get("schema_valid")) / n_intent
    lint = sum(1 for r in intent if r.get("lint_pass")) / n_intent

    def _rate(rows: list, key: str = "verdict", good: str = "pass") -> float:
        rows = [r for r in rows if r.get(key) in (good, "fail")]
        return (sum(1 for r in rows if r.get(key) == good) / len(rows)) if rows else -1.0

    gold = results.get("gold_scores") or []
    gold_rate = _rate(gold)

    per_class: dict[str, dict] = {}
    diagnosis = results.get("diagnosis_scores") or []
    for cls in DIAGNOSIS_CLASSES:
        rows = [r for r in diagnosis if r.get("class") == cls]
        if not rows:
            per_class[cls] = {"examples": 0, "accuracy": None,
                              "status": "NOT VERIFIED"}
            continue
        correct = sum(
            1 for r in rows
            if r.get("verdict_correct") and r.get("cause_correct")
            and r.get("repair_correct"))
        acc = correct / len(rows)
        per_class[cls] = {
            "examples": len(rows), "accuracy": round(acc, 4),
            "status": "PASS" if acc >= GATES["diagnosis_class"]["threshold"]
            else "FAIL",
        }

    live = results.get("live")
    if live is None or not live.get("ran"):
        live_gate: dict[str, Any] = {
            "ran": False, "status": "NOT VERIFIED",
            "reason": (live.get("reason") if live else
                       "live gate not run (needs Packet Tracer reachable)")}
    else:
        passed, total = int(live["passed"]), int(live["total"])
        live_gate = {
            "ran": True, "passed": passed, "total": total,
            "rate": round(passed / total, 4) if total else 0.0,
            "status": "PASS" if total and passed / total >=
            GATES["live_pass"]["threshold"] else "FAIL",
        }

    def _gate(value: float, name: str) -> dict:
        if value < 0:
            return {"measured": None, "threshold": GATES[name]["threshold"],
                    "status": "NOT VERIFIED",
                    "reason": "no scored examples were available"}
        return {"measured": round(value, 4),
                "threshold": GATES[name]["threshold"],
                "status": "PASS" if value >= GATES[name]["threshold"]
                else "FAIL"}

    gates = {
        "schema_valid": _gate(schema if intent else -1, "schema_valid"),
        "lint_pass": _gate(lint if intent else -1, "lint_pass"),
        "gold_score": _gate(gold_rate, "gold_score"),
        "diagnosis_class": per_class,
        "live_pass": live_gate,
    }
    blocking = [
        g["status"] in ("FAIL", "NOT VERIFIED")
        for g in (gates["schema_valid"], gates["lint_pass"], gates["gold_score"])
    ] + [c["status"] in ("FAIL", "NOT VERIFIED") for c in per_class.values()]
    gates["release"] = "BLOCKED" if any(blocking) or live_gate["status"] != "PASS" else "GO"
    return gates


# --- record-level evaluation -------------------------------------------------

def score_record(rec: dict, generation: str) -> dict[str, Any]:
    """Score one formatted record's model output against its expected
    answer (the record's own assistant message)."""
    expected = json.loads(rec["messages"][-1]["content"])
    task = rec["task"]
    if task in ("intent", "prompt"):
        return score_intent_output(generation, expected, expect_refusal=False)
    if task == "diagnosis":
        return score_diagnosis_output(
            generation, expected, diagnosis_class_of(rec["family"]))
    if task == "clarification":
        return score_clarification_output(generation, expected)
    return {"is_valid_json": False}


def evaluate(records: list[dict], generate: Callable[[dict], str]) -> dict[str, Any]:
    """Generate + score every held-out record, aggregate the gates.
    ``generate`` is the model half (GPU notebook) or a test double."""
    results: dict[str, Any] = {
        "intent_outputs": [], "diagnosis_scores": [],
        "clarification_scores": [], "enhancement_scores": [],
    }
    for rec in records:
        generation = generate(rec)
        score = score_record(rec, generation)
        score["id"] = rec.get("id")
        score["source_task"] = rec["task"]
        if rec["task"] in ("intent", "prompt"):
            results["intent_outputs"].append(score)
        elif rec["task"] == "diagnosis":
            results["diagnosis_scores"].append(score)
        elif rec["task"] == "clarification":
            results["clarification_scores"].append(score)
        elif rec["task"] == "enhancement":
            enhancement = score_enhancement_output(
                generation, rec.get("payload") or {})
            enhancement["id"] = rec.get("id")
            enhancement["source_task"] = rec["task"]
            results["enhancement_scores"].append(enhancement)
    return results


def load_generations(path: Path) -> dict[str, str]:
    return {
        r["prompt_id"]: r["generation"]
        for r in (json.loads(line) for line in
                  path.read_text(encoding="utf-8").splitlines() if line.strip())
        if r.get("prompt_id") and "generation" in r
    }


# --- GPU + live halves (NOT verifiable offline - rule 10) --------------------

def _render_generation_prompt(tokenizer: Any, messages: list[dict],
                              disable_thinking: bool) -> str:
    kwargs: dict[str, Any] = {
        "tokenize": False,
        "add_generation_prompt": True,
    }
    if disable_thinking:
        kwargs["enable_thinking"] = False
    try:
        return tokenizer.apply_chat_template(messages, **kwargs)
    except TypeError:
        kwargs.pop("enable_thinking", None)
        return tokenizer.apply_chat_template(messages, **kwargs)


def make_gpu_generator(model_dir: str, config_path: str,
                       adapter_dir: str | None = None,
                       backend: str = "auto",
                       disable_thinking: bool = False,
                       max_new_tokens: int = 2048) -> Callable[[dict], str]:
    cfg = load_config(config_path)
    selected = backend
    if selected == "auto":
        selected = "unsloth" if importlib.util.find_spec("unsloth") else "transformers"
    if selected == "unsloth":
        try:
            import torch
            from unsloth import FastLanguageModel
        except ImportError as exc:
            raise SystemExit(
                f"GPU generation unavailable ({exc}); run on Colab or pass "
                "--generations-file")
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=adapter_dir or model_dir,
            max_seq_length=int(cfg.get("max_seq_length", 4096)),
            load_in_4bit=True)
        FastLanguageModel.for_inference(model)
    else:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise SystemExit(
                f"GPU generation unavailable ({exc}); install the Modal "
                "requirements or pass --generations-file")
        from peft import PeftModel

        base_model = cfg.get("base_model") or model_dir
        tokenizer = AutoTokenizer.from_pretrained(
            model_dir, trust_remote_code=True)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        quant_config = None
        if cfg.get("load_in_4bit", True) and torch.cuda.is_available():
            from transformers import BitsAndBytesConfig

            compute_dtype = (torch.bfloat16
                             if torch.cuda.is_bf16_supported()
                             else torch.float16)
            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=compute_dtype,
                bnb_4bit_use_double_quant=True,
            )
        model = AutoModelForCausalLM.from_pretrained(
            base_model,
            torch_dtype="auto",
            device_map={"": 0},
            quantization_config=quant_config,
            trust_remote_code=True,
        )
        if adapter_dir:
            model = PeftModel.from_pretrained(model, adapter_dir)
        model.eval()
        model.config.use_cache = True

    device = next(model.parameters()).device

    def _generate(rec: dict) -> str:
        text = _render_generation_prompt(
            tokenizer, rec["messages"][:-1], disable_thinking)
        inputs = tokenizer(
            text, return_tensors="pt", truncation=True,
            max_length=int(cfg.get("max_seq_length", 2048))).to(device)
        with torch.no_grad():
            out = model.generate(
                **inputs, max_new_tokens=int(max_new_tokens), do_sample=False,
                pad_token_id=tokenizer.eos_token_id)
        return tokenizer.decode(out[0][inputs["input_ids"].shape[1]:],
                                skip_special_tokens=True)

    return _generate


def run_live_gate(model_dir: str, generate: Callable[[dict], str],
                  gold_dir: Path, limit: int = 50) -> dict[str, Any]:
    """S5.20: gold prompts -> intent -> engine chain -> live Packet Tracer
    build + traffic verification. Reports the exact X/Y. No PT, no claim."""
    try:
        from src.network_autopilot.adapter.packet_tracer.bridge import BridgeClient
    except Exception as exc:
        return {"ran": False, "reason": f"bridge import failed: {exc}"}
    try:
        client = BridgeClient()
        client.ping()
    except Exception as exc:
        return {"ran": False, "reason": f"Packet Tracer not reachable: {exc}"}
    # The live driver itself is wired in the notebook session where PT is
    # open (it needs the full engine build path); this guard keeps the gate
    # from ever reporting a pass without the real thing.
    raise NotImplementedError(
        "live driver runs in the notebook session: expand -> compile -> "
        "net_build -> flow verification per gold prompt, X/Y counted there")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None,
                        help="adapter or merged model dir (GPU path)")
    parser.add_argument("--adapter", default=None,
                        help="PEFT adapter dir applied to --model")
    parser.add_argument("--backend", default="auto",
                        choices=("auto", "unsloth", "transformers"))
    parser.add_argument("--disable-thinking", action="store_true",
                        help="pass enable_thinking=False to the chat template")
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--config", default="havyn-stage5/configs/bakeoff.yaml")
    parser.add_argument("--generations-file", default=None,
                        help="score pre-generated outputs ({prompt_id, "
                             "generation}) instead of loading a model")
    parser.add_argument("--dataset-dir",
                        default="havyn-stage5/datasets/havyn-train-v1")
    parser.add_argument("--gold", default="havyn-stage5/datasets/gold-v1")
    parser.add_argument("--gold-limit", type=int, default=50)
    parser.add_argument("--out", default="havyn-stage5/evaluation")
    parser.add_argument("--live", action="store_true",
                        help="also run the S5.20 live gate (needs Packet "
                             "Tracer reachable)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    per_example: list[dict] = []

    if args.model:
        generate = make_gpu_generator(
            args.model, args.config, adapter_dir=args.adapter,
            backend=args.backend, disable_thinking=args.disable_thinking,
            max_new_tokens=args.max_new_tokens)
    elif args.generations_file:
        gens = load_generations(Path(args.generations_file))
        generate = lambda rec: gens.get(rec.get("id") or "", "")
    else:
        parser.error("pass --model or --generations-file")

    records, _skipped = format_split(Path(args.dataset_dir) / "heldout.jsonl")
    results = evaluate(records, generate)
    for key in ("intent_outputs", "diagnosis_scores", "clarification_scores",
                "enhancement_scores"):
        per_example.extend({"task": "heldout", **s}
                           for s in results[key])

    # Gold gate: score gold prompts with the same generator. Gold is never
    # part of training; its expected intents live in gold-v1.
    gold_scores: list[dict] = []
    for gold in load_gold_examples(Path(args.gold), args.gold_limit):
        generation = generate({"messages": [
            {"role": "system", "content": ""},
            {"role": "user", "content": gold["prompt"]},
            {"role": "assistant", "content": ""},
        ], "id": gold["id"]})
        score = score_intent_output(
            generation, gold.get("expected_intent"),
            expect_refusal=bool(gold.get("expect_refusal")))
        gold_scores.append(score)
        per_example.append({"task": "gold", **score})
    results["gold_scores"] = gold_scores

    if args.live:
        results["live"] = run_live_gate(args.model or "", generate,
                                        Path(args.gold), args.gold_limit)

    gates = compute_gates(results)
    report = {
        "stage": "S5.19/S5.20",
        "model": args.model,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gates": gates,
        "per_example": per_example,
    }
    (out_dir / "gate-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(gates, indent=2))
    if gates["release"] == "BLOCKED":
        sys.exit(1)


def load_gold_examples(gold_dir: Path, limit: int) -> list[dict]:
    """Load real gold examples (skip the template example file)."""
    examples = []
    for path in sorted(gold_dir.glob("gold-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("notes", "").startswith("TEMPLATE EXAMPLE"):
            continue
        if data.get("prompt") and (
                data.get("expected_intent") or data.get("expect_refusal")):
            examples.append(data)
    return examples[:limit]


if __name__ == "__main__":
    main()
