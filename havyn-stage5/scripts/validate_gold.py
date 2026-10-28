"""Validate the S5.5 gold set: engine-verify every example, prove no leakage.

For each gold-*.json example:
  - schema-validate `expected_intent` (or require expect_refusal=true with a
    null intent);
  - chain it through intent lint -> expansion -> spec lint -> IPAM ->
    compile, and record the verdict in the example itself
    (`engine_verified`, `verified_at`, `failure_stage`);
  - with --leak-check <train-dir>, prove no gold prompt hash appears in the
    training data.

Exit code 1 on any unverified or leaked example - a gold set that has not
passed the engine is not a gold set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.network_autopilot.ai.expander import IntentExpander, ExpansionRefusal  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402
from src.network_autopilot.compiler.compiler import compile_spec  # noqa: E402
from src.network_autopilot.compiler.spec_lint import lint_spec  # noqa: E402
from src.network_autopilot.ipam import plan_addressing  # noqa: E402


def verify_example(path: Path) -> tuple[dict, dict]:
    example = json.loads(path.read_text(encoding="utf-8"))
    result = {
        "id": example.get("id", path.stem),
        "prompt_hash": hashlib.sha256(
            example.get("prompt", "").encode("utf-8")
        ).hexdigest(),
        "schema_valid": False,
        "lint_pass": False,
        "engine_verified": False,
        "failure_stage": None,
    }

    expected = example.get("expected_intent")
    if example.get("expect_refusal"):
        if expected is not None:
            result["failure_stage"] = (
                "expect_refusal=true but expected_intent is not null"
            )
        else:
            result["engine_verified"] = True
        return result, example  # type: ignore[return-value]

    if expected is None:
        result["failure_stage"] = "no expected_intent and expect_refusal is false"
        return result, example  # type: ignore[return-value]

    try:
        intent = HavynIntent.model_validate(expected)
    except Exception as exc:  # noqa: BLE001
        result["failure_stage"] = f"schema: {str(exc)[:200]}"
        return result, example  # type: ignore[return-value]
    result["schema_valid"] = True

    lint = lint_intent(intent)
    if lint.blocking:
        result["failure_stage"] = f"lint: {lint.to_dicts()[:1]}"
        return result, example  # type: ignore[return-value]
    result["lint_pass"] = True

    try:
        expansion = IntentExpander(intent).expand()
        problems = lint_spec(expansion.spec)
        if problems:
            result["failure_stage"] = f"spec-lint: {problems[:1]}"
            return result, example  # type: ignore[return-value]
        plan = plan_addressing(expansion.spec)
        compile_spec(expansion.spec, plan)
    except ExpansionRefusal as exc:
        result["failure_stage"] = f"refusal: {exc}"
        return result, example  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        result["failure_stage"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return result, example  # type: ignore[return-value]

    result["engine_verified"] = True
    return result, example  # type: ignore[return-value]


def leak_check(gold_hashes: set[str], train_dir: Path) -> list[str]:
    leaked: list[str] = []
    for path in sorted(train_dir.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            prompt = record.get("prompt")
            if not prompt:
                continue
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if digest in gold_hashes:
                leaked.append(f"{path.name}: prompt hash {digest[:12]} in gold set")
    return leaked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-dir", default="havyn-stage5/datasets/gold-v1")
    parser.add_argument("--leak-check", default=None,
                        help="training-data directory to prove exclusion against")
    parser.add_argument("--write-verification", action="store_true",
                        help="write engine_verified/verified_at back into examples")
    args = parser.parse_args()

    gold_dir = Path(args.gold_dir)
    examples = sorted(gold_dir.glob("gold-*.json"))
    if not examples:
        raise SystemExit(f"no gold-*.json examples in {gold_dir}")

    hashes: set[str] = set()
    failures = 0
    for path in examples:
        result, example = verify_example(path)
        hashes.add(result["prompt_hash"])
        status = "OK " if result["engine_verified"] else "FAIL"
        if not result["engine_verified"]:
            failures += 1
            print(f"{status} {result['id']}: {result['failure_stage']}")
        if args.write_verification and result["engine_verified"]:
            example["engine_verified"] = True
            example["verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            path.write_text(
                json.dumps(example, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    print(f"{len(examples) - failures}/{len(examples)} examples engine-verified")
    if failures:
        raise SystemExit(1)

    if args.leak_check:
        leaked = leak_check(hashes, Path(args.leak_check))
        if leaked:
            for item in leaked:
                print(f"LEAK {item}")
            raise SystemExit(1)
        print("leak check: no gold prompt appears in the training data")


if __name__ == "__main__":
    main()
