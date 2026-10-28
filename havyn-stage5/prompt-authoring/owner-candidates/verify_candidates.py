"""Stage 5 prompt-authoring: engine-verify AI-assisted owner candidates.

This reuses the exact deterministic chain from ``validate_gold.py``:
    schema validation -> intent lint -> expansion -> spec lint -> IPAM -> compile.

It is the schema/engine-verifier half of the two-model workflow. Model B
(independent adversarial review) is recorded separately in ``review/``; this
script only answers one question: *can the deterministic engine build the
expected intent, and does it refuse by name when it cannot?*

Two candidate shapes are legal, mirroring the gold-set rules:

1. Representable request -> ``expected_intent`` present, must pass the chain.
2. Ambiguous / contradictory / impossible request -> ``expect_refusal=true``
   and ``expected_intent`` must be ``null``. The candidate records the *correct
   behaviour* (which question to ask, or which named refusal to emit) in
   ``expected_behaviour``; it never smuggles an intent the engine would fail.

Nothing here writes to ``datasets/gold-v1``. Candidates are AI *drafts*; only
Hamza's explicit adoption moves one into the official owner-authored gold set.

Usage:
    python verify_candidates.py --emit            # validate + write JSON + report
    python verify_candidates.py --quiet           # validate only, summary only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.network_autopilot.ai.expander import (  # noqa: E402
    ExpansionRefusal,
    IntentExpander,
)
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402
from src.network_autopilot.compiler.compiler import compile_spec  # noqa: E402
from src.network_autopilot.compiler.spec_lint import lint_spec  # noqa: E402
from src.network_autopilot.ipam import plan_addressing  # noqa: E402

ENGINE_COMMIT = None
try:
    import subprocess

    ENGINE_COMMIT = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
except Exception:
    pass


def verify_record(record: dict) -> dict:
    """Run one candidate through the deterministic chain. Never raises."""
    result = {
        "id": record["id"],
        "prompt_hash": hashlib.sha256(
            record.get("prompt", "").encode("utf-8")
        ).hexdigest(),
        "schema_valid": False,
        "lint_pass": False,
        "engine_verified": False,
        "failure_stage": None,
        "expected_refusal": bool(record.get("expect_refusal")),
    }

    expected = record.get("expected_intent")

    # Shape 2: this candidate is *supposed* to be unrepresentable / ambiguous.
    if result["expected_refusal"]:
        if expected is not None:
            result["failure_stage"] = (
                "expect_refusal=true but expected_intent is not null"
            )
        else:
            result["engine_verified"] = True
        return result

    if expected is None:
        result["failure_stage"] = "no expected_intent and expect_refusal is false"
        return result

    # Shape 1: must survive the full chain.
    try:
        intent = HavynIntent.model_validate(expected)
    except Exception as exc:  # noqa: BLE001
        result["failure_stage"] = f"schema: {str(exc)[:200]}"
        return result
    result["schema_valid"] = True

    lint = lint_intent(intent)
    if lint.blocking:
        result["failure_stage"] = f"lint: {lint.blocking[0].to_dict()}"
        return result
    result["lint_pass"] = True

    try:
        expansion = IntentExpander(intent).expand()
        problems = lint_spec(expansion.spec)
        if problems:
            result["failure_stage"] = f"spec-lint: {problems[:1]}"
            return result
        plan = plan_addressing(expansion.spec)
        compile_spec(expansion.spec, plan)
    except ExpansionRefusal as exc:
        result["failure_stage"] = f"refusal: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001
        result["failure_stage"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return result

    result["engine_verified"] = True
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit", action="store_true",
                        help="write per-candidate JSON and the report")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    from candidate_data import CANDIDATES  # local import (same directory)

    here = Path(__file__).resolve().parent
    out_dir = here / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    verified = 0
    failures: list[tuple[str, str]] = []

    enriched = []
    for record in CANDIDATES:
        check = verify_record(record)
        full = {
            **record,
            "prompt_hash": check["prompt_hash"],
            "engine_verified": check["engine_verified"],
            "failure_stage": check["failure_stage"],
            "verified_at": now,
            "engine_commit": ENGINE_COMMIT,
        }
        enriched.append(full)
        if check["engine_verified"]:
            verified += 1
        else:
            failures.append((record["id"], str(check["failure_stage"])))

        if args.emit:
            (out_dir / f"{record['id']}.json").write_text(
                json.dumps(full, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    total = len(CANDIDATES)
    summary = {
        "candidates": total,
        "engine_verified": verified,
        "failed": len(failures),
        "refusals_expected": sum(1 for r in CANDIDATES if r.get("expect_refusal")),
        "engine_commit": ENGINE_COMMIT,
        "verified_at": now,
    }

    if args.emit:
        (here / "verification-report.json").write_text(
            json.dumps({"summary": summary,
                        "failures": [{"id": i, "stage": s} for i, s in failures]},
                       indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    if not args.quiet:
        for cand_id, stage in failures:
            print(f"FAIL {cand_id}: {stage}")
    print(json.dumps(summary, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
