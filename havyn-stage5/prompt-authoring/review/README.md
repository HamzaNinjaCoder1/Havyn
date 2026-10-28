# Two-model review (Stage 5 prompt authoring)

Model B is the **independent adversarial reviewer**. It is deliberately kept
separate from Model A (the drafter) so the review is genuine, not a rubber
stamp.

## Independence rules

- Model B sees ONLY the candidate `prompt` and the source `expected_intent` /
  `expect_refusal` / `expected_behaviour`. It does **not** see Model A's
  `why_valuable`, `research_inspiration`, or any self-justification field —
  those would bias it toward agreeing.
- Model B re-derives the network requirements from the prompt on its own, then
  compares them against the source intent field-by-field.
- Verdict is one of `ACCEPT` / `REVIEW` / `REJECT`.

## Honesty note (rule 10)

In this environment Model A and Model B are both executed by the same base
model (MiMo-V2.6). Independence is approximated by **context isolation**: each
Model B run is a fresh subagent that never sees Model A's reasoning, only the
prompt + source intent. This is a weaker guarantee than a truly different model
family. Before the official Gold Set is frozen, Model B review should be
re-run on a genuinely different model family (e.g. via the Colab fine-tune
eval or a second provider). This limitation is recorded, not hidden.

## Verdict meanings

- `ACCEPT` — faithful to intent, no invented facts, no lost requirements,
  natural language, recoverable intent.
- `REVIEW` — minor drift or a deliberate design choice a human should confirm
  (e.g. a candidate held at a capability boundary).
- `REJECT` — invented facts, lost requirements, contradiction the prompt
  didn't intend, or unnatural/template-sounding language.

## Output

Each Model B run writes `review/model-b-reviews/<batch>.json` containing one
record per candidate:

```json
{
  "id": "oc-001",
  "recovered_intent_summary": "...what Model B thinks the prompt asks for...",
  "missing_facts": ["..."],
  "invented_facts": ["..."],
  "contradictions": ["..."],
  "ambiguity_notes": ["..."],
  "human_likeness": 0.0,
  "fidelity": 0.0,
  "verdict": "ACCEPT | REVIEW | REJECT",
  "reason": "..."
}
```

Batch totals from the first review run (batch-1..4, one record per candidate):
**ACCEPT 24 / REVIEW 27 / REJECT 5** (REJECTs: oc-008, oc-012, oc-025, oc-050,
oc-055). batch-5 is a separate re-read of 25 candidates. The batch JSONs are
the authoritative record; nothing in them is edited after the fact.

## Decisions taken on specific REVIEW verdicts (recorded, not hidden)

| Candidate | Reviewer point | Decision |
| --- | --- | --- |
| oc-007 | held at a behaviour boundary (which clarification to ask) | kept as `expect_refusal`; `expected_behaviour` must be definite (one correct response, not a menu) — checked in the fix pass |
| oc-026 | guest "no internal access" reads like a guarantee | kept as `expect_refusal`: the engine genuinely cannot deliver it (`guest-isolation-unsupported`); prompt left faithful, refusal names the real code |
| oc-044 | reviewer preferred a clarification over a silent assumption | kept as `expect_refusal`; `expected_behaviour` broadened to the agreed standard: surfacing options preferred, an explicitly stated assumption acceptable, a silent pick not acceptable |
| oc-030 / oc-035 | two candidates share one topology (same network, two languages) | deliberate language-pairing, not a duplicate: prompts and language differ, the shape is intentionally identical; documented here rather than edited apart |

## Fix pass and re-review — corrected record

The fix pass (`../owner-candidates/FIX_PASS_PLAN.md`) was applied **after**
the checkpoint. batch-5 is therefore not a blanket post-fix review: several of
its REVIEW reasons describe text the fix pass replaced. For example, batch-5
says oc-030's prompt "states no endpoints at all" and that oc-012's kiosks are
"silently dropped"; the shipped prompts contain the counts and the kiosks.
Those batch-5 texts are kept as-is because the batch record is immutable. They
are valid for the revision they saw, not for the shipped text.

Statuses for the shipped text are recorded in
[`POST_FIX_RECHECK.md`](POST_FIX_RECHECK.md) and
[`post-fix-recheck.json`](post-fix-recheck.json). That re-check was performed by
the **same base model** as the drafter and as Model B, so it is **not**
independent and **not** cross-family validation. It supersedes the *status*
only; the historical verdicts above remain the record of what was reviewed.

Engine truth for the shipped revision is
`../owner-candidates/verification-report.json`. A genuine cross-family review
is still required before the gold set is frozen; the input package and protocol
are in `../reports/cross-family-review-protocol.md`.
