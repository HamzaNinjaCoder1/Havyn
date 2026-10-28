"""Export the cross-family review input package.

The package contains only what an independent reviewer is allowed to see:
    id, prompt_hash, prompt, expect_refusal, expected_intent, expected_behaviour

It deliberately excludes Model A's `why_valuable`, `research_inspiration`,
`scenario`, `category` and `difficulty` so the reviewer cannot inherit the
drafter's justification.

It does not run a review. It produces the immutable input + a manifest with its
SHA-256, so a later cross-family run can be tied to exactly this candidate set.

Usage:
    python export_review_package.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAND_DIR = HERE.parent / "owner-candidates" / "candidates"
OUT = HERE / "cross-family-input.jsonl"
MANIFEST = HERE / "cross-family-input.manifest.json"

ALLOWED = (
    "id",
    "prompt_hash",
    "prompt",
    "expect_refusal",
    "expected_intent",
    "expected_behaviour",
)


def main() -> None:
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CAND_DIR.glob("oc-*.json"))
    ]
    if not records:
        raise SystemExit(f"no candidate JSON in {CAND_DIR}")

    lines = []
    for record in records:
        payload = {key: record.get(key) for key in ALLOWED}
        payload["prompt_hash"] = hashlib.sha256(
            record["prompt"].encode("utf-8")
        ).hexdigest()
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    body = "\n".join(lines) + "\n"
    OUT.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "records": len(records),
        "fields": list(ALLOWED),
        "excluded_fields": [
            "why_valuable",
            "research_inspiration",
            "scenario",
            "category",
            "difficulty",
            "language",
            "coverage_cell",
            "length_category",
        ],
        "file": OUT.name,
        "sha256": digest,
        "candidate_revision": "working tree after the Stage-5 fix pass",
        "engine_commit": "e8503cdfb32a83817a2fbc84ad4245057acf75a2",
        "status": "INPUT ONLY — no cross-family review has been run",
    }
    MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
