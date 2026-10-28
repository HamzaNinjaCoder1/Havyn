# Gold Set v1 (S5.5) — PRIVATE, NEVER USED FOR TRAINING

This directory holds the 300-example gold evaluation set:

- `gold-001..100.json` — authored by the project owner (Hamza) personally.
- `gold-101..300.json` — authored by other people.

## Rules (do not bend)

1. **Never train on any file here.** The training-data assembly (S5.14) must
   exclude these ids by hash; `validate_gold.py --leak-check <train-dir>`
   proves the exclusion.
2. Every example must carry `expected_intent`, and that intent must pass the
   deterministic engine: `validate_gold.py` chains schema validation →
   intent lint → expansion → spec lint → IPAM → compile. An example that
   fails the chain is wrong, not the engine.
3. Invalid/ambiguous prompts are allowed as gold examples, but then
   `expect_refusal` must be true and `expected_intent` must be null.
4. Store this directory outside the training pipeline and outside any
   public artifact. If using Hugging Face for backup, use a **private**
   repository with a scoped token read from Colab secrets.

## Example format

```json
{
  "id": "gold-001",
  "author": "owner",
  "language": "en | roman-urdu | mixed",
  "category": "enterprise | security | routing | wireless | iot | dhcp | multi-site | ambiguous | contradictory | long-document | ...",
  "prompt": "the natural-language request, verbatim",
  "expect_refusal": false,
  "expected_intent": { "... Havyn Intent v1 ..." },
  "difficulty": "L1..L5",
  "notes": "optional context from the author"
}
```

Authors write `prompt` (+ `expected_intent` or `expect_refusal`). Everything
engine-related (`engine_verified`, `verified_at`) is filled by
`validate_gold.py`, never by hand.

## Provenance for owner slots (gold-001..100)

AI-assisted drafts live in `../../prompt-authoring/owner-candidates/` as
`oc-NNN.json`. **An `oc-NNN` draft is not gold and must never be copied here
unchanged.** Owner adoption requires:

1. Hamza rewrites the prompt in his own words (the draft text is not adopted
   verbatim).
2. He confirms the `expected_intent` / refusal against the rewritten prompt.
3. The record carries this block:

```json
"provenance": {
  "origin": "owner",
  "author": "Hamza",
  "ai_assisted": true,
  "adopted_from": "oc-004",
  "rewritten": true,
  "adopted_at": "YYYY-MM-DD",
  "engine_commit": "<commit that verified the record>"
}
```

`ai_assisted: true` is mandatory for candidate-backed slots even after a full
rewrite: the scenario and expected intent were AI-drafted. Hiding that would
make the provenance false. `rewritten: false` is not owner-authored gold.

The 100-slot plan (candidate-backed + coverage-driven, with per-slot actions and
the 8 open owner questions) is
`../../prompt-authoring/reports/owner_gold_authoring_plan.md`.
