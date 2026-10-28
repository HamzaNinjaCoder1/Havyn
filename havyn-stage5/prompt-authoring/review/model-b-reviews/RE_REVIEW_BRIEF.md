# Model B re-review brief (post-fix pass)

The first review round (batch-1..4) judged the **pre-fix** revision: 24 ACCEPT /
27 REVIEW / 5 REJECT. The fix pass (`owner-candidates/FIX_PASS_PLAN.md`) then
changed intents and prompts, so those verdicts no longer describe what shipped.
This brief defines the second round.

## What is re-reviewed

1. **All five REJECTs** — were the reviewer's catches actually resolved?
2. **Every candidate with a semantic fix** (prompt wording that carries network
   facts, intent shape changes, refusal-behaviour rewrites): oc-009, oc-010,
   oc-011, oc-012, oc-019, oc-023, oc-024, oc-025, oc-028, oc-036, oc-040,
   oc-041, oc-044, oc-053, oc-054.
3. **Mechanically changed candidates** (VLAN pins and language labels only) are
   re-verified by the engine, not re-reviewed by Model B; this is stated in the
   status table rather than implied to be a fresh review.

## Isolation (honest limits)

The reviewer reads only these fields of each candidate:
`id`, `language`, `difficulty`, `category`, `prompt`, `expected_intent`,
`expect_refusal`, `expected_behaviour`.
It does **not** use `why_valuable`, `research_inspiration`, `scenario`, or any
fix-pass note, and it does not read `FIX_PASS_PLAN.md` or the first round's
batch files before forming its own view (the original catches are re-stated for
it by the operator only *after* it has recorded its own recovered intent).

Both rounds run on the same base model with context isolation, not a different
model family — the limitation recorded in `../README.md` still applies.

## Output (per candidate)

```json
{
  "id": "oc-028",
  "recovered_intent_summary": "...what the prompt asks for, re-derived...",
  "missing_facts": [],
  "invented_facts": [],
  "contradictions": [],
  "ambiguity_notes": [],
  "human_likeness": 0.0,
  "fidelity": 0.0,
  "fix_resolved": "yes | partly | no | n/a",
  "verdict": "ACCEPT | REVIEW | REJECT",
  "reason": "..."
}
```

`fix_resolved` is `n/a` for candidates that the fix pass did not touch.

## Status rule for the final table

| final status | condition |
| --- | --- |
| ACCEPT | engine-verified AND (re-review ACCEPT, or pre-fix ACCEPT with only mechanical pins/label changes) |
| REVIEW | engine-verified but a residual fidelity/naturalness question remains for Hamza |
| REJECT | engine-verified but the reviewer's catch stands (must not be adopted) |
| NOT VERIFIED | engine check did not run for it — never promoted silently |
