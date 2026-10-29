"""S5.4 base-model bake-off harness.

Runs the same benchmark prompts through 3-5 open-weight base models
(no fine-tuning) and scores them:

    valid JSON % / schema validity % / lint pass % / field accuracy /
    requirement recall+precision / clarification quality / latency / VRAM.

The scoring half (`compare_intents`, `score_generation`, `summarise`) is pure
Python and offline-testable. The generation half needs a GPU runtime; it is
imported from the Colab notebooks only. Results land in
havyn-stage5/artifacts/bakeoff/<run-id>/ as JSONL plus a markdown table.

Usage on Colab:

    from benchmark_harness import run_bakeoff
    run_bakeoff(config_path="configs/bakeoff.yaml", prompts="bench-prompts.jsonl")
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.network_autopilot.ai.expander import IntentExpander  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402
from src.network_autopilot.compiler.compiler import compile_spec  # noqa: E402
from src.network_autopilot.compiler.spec_lint import lint_spec  # noqa: E402
from src.network_autopilot.ipam import plan_addressing  # noqa: E402


# --- prompt rendering (the deterministic 30% of S5.9) ------------------------

_INTERNET_EN = {
    "wan": "Internet access over a WAN uplink.",
    "dsl": "Internet access over DSL.",
    "cable": "Internet access over cable.",
}
_REQ_EN = "Requirements: {list}."


def render_prompt(intent: HavynIntent, language: str = "en") -> str:
    """A deterministic, intent-faithful natural-language prompt.

    Only information present in the intent appears in the prompt - the same
    S5.9 strictness applied to the template share of the dataset.
    """
    site_list = ", ".join(
        f"{s.id} ({len(s.areas)} area(s))"
        for s in intent.sites
    )
    vlan_list = ", ".join(
        f"{v.id} in {v.site or 'any site'}"
        + (f" for {v.required_hosts} hosts" if v.required_hosts else
           (f" on {v.cidr}" if v.cidr else ""))
        for v in intent.vlans
    )
    server_list = ", ".join(
        f"{s.id} running {'+'.join(s.services) or 'no services'}"
        for s in intent.servers
    ) or "none"
    internet_text = (
        _INTERNET_EN.get(intent.internet.access, "Internet access.")
        if intent.internet else "No internet access."
    )
    req_list = ", ".join(
        f"{r.id}: {r.statement}" for r in intent.requirements
    ) or "none stated"

    if language == "roman-urdu":
        return (
            f"Aik network banao jiska naam {intent.name} ho. "
            f"Sites: {site_list}. VLANs: {vlan_list}. "
            f"Servers: {server_list}. {internet_text} "
            f"Requirements: {req_list}."
        )
    return (
        f"Design a network called {intent.name}. "
        f"Sites: {site_list}. VLANs: {vlan_list}. "
        f"Servers: {server_list}. {internet_text} "
        f"Requirements: {req_list}."
    )


# --- scoring ------------------------------------------------------------------

_LEADING_JSON = re.compile(r"```(?:json)?\s*|\s*```")
_HEX_HASH = re.compile(r"^[0-9a-f]{64}$")


def extract_json(text: str) -> str | None:
    """The first balanced JSON object in a model response, else None."""
    text = _LEADING_JSON.sub("", text.strip())
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


@dataclass
class GenerationScore:
    prompt_id: str
    category: str
    is_valid_json: bool
    is_schema_valid: bool
    lint_pass: bool
    engine_verified: bool = False
    language: str | None = None
    field_accuracy: float | None = None
    requirement_recall: float | None = None
    requirement_precision: float | None = None
    correct_refusal: bool | None = None
    latency_s: float | None = None
    vram_mb: float | None = None
    notes: str | None = None


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def compare_intents(expected: HavynIntent, actual: HavynIntent) -> dict[str, object]:
    """Field-by-field comparison of a generated intent against the reference.

    Returns matched/total counts plus requirement recall and precision over
    the requirement ledger (matched on normalised statement text).
    """
    expected_fields = expected.model_dump(exclude_none=True, exclude={"provenance"})
    actual_fields = actual.model_dump(exclude_none=True, exclude={"provenance"})

    matched = 0
    total = 0
    _compare(expected_fields, actual_fields, matched_counter := [0, 0])
    matched, total = matched_counter

    exp_reqs = {_normalise(r.statement) for r in expected.requirements}
    act_reqs = {_normalise(r.statement) for r in actual.requirements}
    recall = len(exp_reqs & act_reqs) / len(exp_reqs) if exp_reqs else None
    precision = len(exp_reqs & act_reqs) / len(act_reqs) if act_reqs else None
    return {
        "fields_matched": matched,
        "fields_total": total,
        "field_accuracy": matched / total if total else None,
        "requirement_recall": recall,
        "requirement_precision": precision,
    }


def _compare(exp: object, act: object, counter: list[int]) -> None:
    """Count leaf-field agreement between two parsed intent trees.

    Units of comparison are leaves (scalars) and whole lists; intermediate
    dict keys only steer the walk, otherwise they would inflate the total
    without ever earning a match.
    """
    if isinstance(exp, dict) and isinstance(act, dict):
        for key, value in exp.items():
            if key not in act:
                counter[1] += 1
            else:
                _compare(value, act[key], counter)
    elif isinstance(exp, (list, dict)):
        counter[1] += 1
        if exp == act:
            counter[0] += 1
    else:
        counter[1] += 1
        if exp == act:
            counter[0] += 1


def score_generation(
    prompt_id: str,
    category: str,
    response: str,
    expected_intent: dict | None = None,
    expect_refusal: bool = False,
    latency_s: float | None = None,
    vram_mb: float | None = None,
    language: str | None = None,
) -> GenerationScore:
    """Score one raw model response.

    ``expect_refusal`` marks prompts whose correct answer is a refusal or a
    clarification request, not an intent (invalid/ambiguous benchmarks).
    """
    raw = extract_json(response)
    is_valid_json = raw is not None
    schema_valid = False
    lint_pass = False
    engine_verified = False
    fields: dict[str, object] = {}

    if is_valid_json:
        try:
            intent = HavynIntent.model_validate(json.loads(raw))
            schema_valid = True
        except Exception:  # noqa: BLE001 - any validation failure is a miss
            intent = None
        if intent is not None:
            result = lint_intent(intent)
            lint_pass = not result.blocking
            if lint_pass:
                # Full engine acceptance: expand -> spec lint -> IPAM -> compile.
                try:
                    expansion = IntentExpander(intent).expand()
                    if not lint_spec(expansion.spec):
                        compile_spec(expansion.spec, plan_addressing(expansion.spec))
                        engine_verified = True
                except Exception:  # noqa: BLE001
                    engine_verified = False
            if expected_intent is not None:
                try:
                    expected = HavynIntent.model_validate(expected_intent)
                    fields = compare_intents(expected, intent)
                except Exception:  # noqa: BLE001
                    fields = {}

    correct_refusal: bool | None = None
    if expect_refusal:
        # Correct behaviour: no plausible intent, or an explicit refusal.
        text_lower = response.lower()
        refused = (
            not is_valid_json
            or not schema_valid
            or any(
                marker in text_lower
                for marker in ("cannot", "not possible", "unsupported",
                               "refuse", "missing", "clarify", "need more")
            )
        )
        correct_refusal = refused

    return GenerationScore(
        prompt_id=prompt_id,
        category=category,
        is_valid_json=is_valid_json,
        is_schema_valid=schema_valid,
        lint_pass=lint_pass,
        engine_verified=engine_verified,
        language=language,
        field_accuracy=fields.get("field_accuracy"),
        requirement_recall=fields.get("requirement_recall"),
        requirement_precision=fields.get("requirement_precision"),
        correct_refusal=correct_refusal,
        latency_s=latency_s,
        vram_mb=vram_mb,
        notes=None if not fields else json.dumps(fields),
    )


def summarise(scores: list[GenerationScore]) -> dict[str, object]:
    n = len(scores)
    if n == 0:
        return {"examples": 0}

    def pct(values: list[bool]) -> float:
        return round(100.0 * sum(values) / len(values), 2) if values else 0.0

    field_accs = [
        s.field_accuracy for s in scores if s.field_accuracy is not None
    ]
    recalls = [
        s.requirement_recall for s in scores if s.requirement_recall is not None
    ]
    latencies = [s.latency_s for s in scores if s.latency_s is not None]
    refusals = [s.correct_refusal for s in scores if s.correct_refusal is not None]
    precisions = [
        s.requirement_precision for s in scores if s.requirement_precision is not None
    ]
    mean_precision = (
        round(sum(precisions) / len(precisions), 3) if precisions else None
    )

    by_language: dict[str, dict[str, object]] = {}
    for language in sorted({s.language for s in scores if s.language}):
        subset = [s for s in scores if s.language == language]
        subset_fields = [
            s.field_accuracy for s in subset if s.field_accuracy is not None
        ]
        by_language[language] = {
            "examples": len(subset),
            "valid_json_pct": pct([s.is_valid_json for s in subset]),
            "schema_valid_pct": pct([s.is_schema_valid for s in subset]),
            "engine_verified_pct": pct([s.engine_verified for s in subset]),
            "mean_field_accuracy": (
                round(sum(subset_fields) / len(subset_fields), 3)
                if subset_fields else None
            ),
        }

    return {
        "examples": n,
        "valid_json_pct": pct([s.is_valid_json for s in scores]),
        "schema_valid_pct": pct([s.is_schema_valid for s in scores]),
        "lint_pass_pct": pct([s.lint_pass for s in scores]),
        "engine_verified_pct": pct([s.engine_verified for s in scores]),
        "mean_field_accuracy": round(sum(field_accs) / len(field_accs), 3) if field_accs else None,
        "mean_requirement_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "mean_requirement_precision": mean_precision,
        # Extra requirements the model invented; lower precision => more invention.
        "hallucinated_requirement_rate": (
            round(1.0 - mean_precision, 3) if mean_precision is not None else None
        ),
        "refusal_correct_pct": pct(refusals) if refusals else None,
        "mean_latency_s": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "by_language": by_language,
    }


# --- GPU runner (Colab only) ----------------------------------------------------

def run_bakeoff(
    config_path: str | Path,
    prompts: str | Path,
    out_dir: str | Path,
    max_prompts: int | None = None,
    repeat_prompts: int = 0,
) -> Path:
    """Generate + score for every candidate model in the bake-off config.

    ``repeat_prompts`` re-generates the first N prompts and records the exact
    match rate, so determinism is measured instead of asserted.
    Requires torch/transformers and a GPU; raise, never degrade silently.
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    config = json.loads(Path(config_path).read_text(encoding="utf-8")) \
        if str(config_path).endswith(".json") else _load_yaml(config_path)
    records = [json.loads(line) for line in
               Path(prompts).read_text(encoding="utf-8").splitlines() if line.strip()]
    if max_prompts:
        records = records[:max_prompts]
    if len(records) < 100:
        raise ValueError(
            f"the bake-off requires at least 100 identical prompts, got {len(records)}"
        )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    for candidate in config["candidates"]:
        name = candidate["name"]
        print(f"[bakeoff] loading {name}")
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        tokenizer = AutoTokenizer.from_pretrained(candidate["model_id"])
        quant_config = None
        if candidate.get("load_in_4bit") and torch.cuda.is_available():
            from transformers import BitsAndBytesConfig

            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
        model = AutoModelForCausalLM.from_pretrained(
            candidate["model_id"],
            torch_dtype="auto",
            device_map="auto",
            quantization_config=quant_config,
        )
        model.eval()

        def generate_once(record: dict) -> tuple[str, float]:
            messages = [
                {"role": "system", "content": config["system_prompt"]},
                {"role": "user", "content": record["prompt"]},
            ]
            text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True,
            )
            inputs = tokenizer([text], return_tensors="pt").to(model.device)
            start = time.time()
            with torch.no_grad():
                output = model.generate(
                    **inputs,
                    max_new_tokens=candidate.get("max_new_tokens", 2048),
                    temperature=candidate.get("temperature", 0.0),
                    do_sample=candidate.get("temperature", 0.0) > 0,
                )
            latency = time.time() - start
            response = tokenizer.decode(
                output[0][inputs["input_ids"].shape[1]:],
                skip_special_tokens=True,
            )
            return response, latency

        scores: list[GenerationScore] = []
        raw_responses: list[str] = []
        results_path = out / f"{name.replace('/', '_')}-generations.jsonl"

        with results_path.open("w", encoding="utf-8") as results:
            for record in records:
                response, latency = generate_once(record)
                raw_responses.append(response)
                vram = torch.cuda.max_memory_allocated() / 1e6 \
                    if torch.cuda.is_available() else None
                score = score_generation(
                    prompt_id=record["id"],
                    category=record.get("category", "unknown"),
                    response=response,
                    expected_intent=record.get("expected_intent"),
                    expect_refusal=record.get("expect_refusal", False),
                    latency_s=latency,
                    vram_mb=vram,
                    language=record.get("language"),
                )
                scores.append(score)
                results.write(json.dumps({
                    "id": record["id"],
                    "prompt": record["prompt"],
                    "response": response,
                    "score": score.__dict__,
                }) + "\n")

        repeat_pct = None
        if repeat_prompts:
            checked = min(repeat_prompts, len(records))
            same = 0
            for index in range(checked):
                again, _ = generate_once(records[index])
                if again == raw_responses[index]:
                    same += 1
            repeat_pct = round(100.0 * same / checked, 2)

        summary = summarise(scores)
        summary["model"] = name
        summary["model_id"] = candidate["model_id"]
        summary["temperature"] = candidate.get("temperature", 0.0)
        summary["deterministic_repeat_pct"] = repeat_pct
        summary["repeat_prompts_checked"] = (
            min(repeat_prompts, len(records)) if repeat_prompts else 0
        )
        summary["peak_vram_mb"] = (
            round(torch.cuda.max_memory_allocated() / 1e6, 1)
            if torch.cuda.is_available() else None
        )
        summary["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (out / f"{name.replace('/', '_')}-summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8",
        )
        print(f"[bakeoff] {name}: {json.dumps(summary)}")
        del model
        torch.cuda.empty_cache()

    table_path = out / "comparison.md"
    _write_comparison_table(out, table_path)
    return table_path


def _load_yaml(path: str | Path) -> dict:
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _write_comparison_table(out: Path, path: Path) -> None:
    rows = []
    for summary_path in sorted(out.glob("*-summary.json")):
        rows.append(json.loads(summary_path.read_text(encoding="utf-8")))
    if not rows:
        return
    columns = [
        "model", "valid_json_pct", "schema_valid_pct", "lint_pass_pct",
        "engine_verified_pct", "mean_field_accuracy", "mean_requirement_recall",
        "mean_requirement_precision", "hallucinated_requirement_rate",
        "refusal_correct_pct", "deterministic_repeat_pct", "mean_latency_s",
        "peak_vram_mb",
    ]
    lines = ["| " + " | ".join(columns) + " |",
             "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[bakeoff] comparison table at {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="havyn-stage5/configs/bakeoff.yaml")
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--out", default="havyn-stage5/artifacts/bakeoff")
    parser.add_argument("--max-prompts", type=int, default=None)
    parser.add_argument("--repeat-prompts", type=int, default=0,
                        help="re-generate the first N prompts to measure determinism")
    args = parser.parse_args()
    run_bakeoff(args.config, args.prompts, args.out, args.max_prompts, args.repeat_prompts)


if __name__ == "__main__":
    main()
