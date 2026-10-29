"""Export adoption-pending Gold drafts from the accepted candidate pool.

Each draft is an AI-assisted candidate, not owner-authored Gold:
    origin: ai_assisted_candidate
    author_status: ai_generated_or_ai_assisted
    owner_adoption: pending
    owner_confirmation_required: true

The draft keeps the original AI prompt and a whitespace-cleaned version; it does
not pretend Hamza wrote either one. Run validate_gold.py only after an owner
rewrite has been adopted into datasets/gold-v1/.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAND_DIR = HERE / "candidates"
OUT_DIR = HERE.parent / "accepted-candidates"
MANIFEST = OUT_DIR / "MANIFEST.json"
DECISIONS_FILE = HERE.parent / "reports" / "owner_decisions.json"


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def _decision_records() -> dict[str, dict]:
    if not DECISIONS_FILE.exists():
        return {}
    payload = json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))
    return {d["id"]: d for d in payload.get("decisions", [])}


def _merge_decision(record: dict, records: dict[str, dict]) -> dict | None:
    decision = record.get("decision")
    if not decision:
        return None
    source = records.get(record["id"], {})
    extra = {
        key: source[key]
        for key in ("resolution_status", "resolved_at", "owner_approval",
                    "owner_action_required")
        if key in source
    }
    return {**decision, **extra}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    decision_records = _decision_records()
    decision_limits = {
        key: value["engine_cannot_verify"] for key, value in decision_records.items()
    }
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CAND_DIR.glob("oc-*.json"))
    ]
    manifest = {
        "status": "AI-ASSISTED DRAFTS — NOT OWNER-AUTHORED GOLD",
        "provenance_rule": "Every draft is ai_assisted_candidate with owner_adoption pending. Hamza must rewrite and adopt before any record enters datasets/gold-v1/.",
        "drafts": 0,
        "buildable": 0,
        "refusal": 0,
        "owner_approved": 0,
        "files": [],
    }
    for record in records:
        slot = f"gold-{record['id'].split('-')[1]}"
        draft = {
            "id": slot,
            "candidate_id": record["id"],
            "language": record["language"],
            "difficulty": record["difficulty"],
            "category": record["category"],
            "coverage_cell": record["coverage_cell"],
            "length_category": record["length_category"],
            "original_ai_prompt": record["prompt"],
            "cleaned_prompt": _clean(record["prompt"]),
            "expect_refusal": record["expect_refusal"],
            "expected_intent": record["expected_intent"],
            "expected_behaviour": record["expected_behaviour"],
            "capability_limitations": (
                record["expected_behaviour"]
                + (
                    " Engine cannot verify: "
                    + decision_limits[record["id"]]
                    if record["id"] in decision_limits
                    else ""
                )
            ),
            "why_suitable": record["why_valuable"],
            "decision": _merge_decision(record, decision_records),
            "provenance": {
                "origin": "ai_assisted_candidate",
                "author_status": "ai_generated_or_ai_assisted",
                "owner_adoption": "pending",
                "adopted_from": record["id"],
                "rewritten": False,
                "owner_confirmation_required": True,
            },
        }
        path = OUT_DIR / f"{slot}.json"
        path.write_text(
            json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        manifest["drafts"] += 1
        manifest["buildable"] += 0 if record["expect_refusal"] else 1
        manifest["refusal"] += 1 if record["expect_refusal"] else 0
        manifest["files"].append(path.name)

    MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: manifest[k] for k in
                      ("drafts", "buildable", "refusal", "owner_approved")},
                     indent=2))


if __name__ == "__main__":
    main()
