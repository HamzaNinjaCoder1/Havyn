# Cross-family review protocol (Stage 5 candidates)

> **STATUS: NOT DONE.** No model from a different family has reviewed these
> candidates. The previous "Model B" review used the **same base model** as the
> drafter (MiMo-V2.6) with **context isolation**, and the post-fix re-check used
> the same base model again. Neither counts as cross-family validation. Do not
> mark the gold set as cross-family-reviewed until this protocol has actually
> been run and its output committed.

## Why same-base context isolation is not enough

Context isolation hides Model A's reasoning from Model B, which removes the
most obvious sycophancy path. It does **not** remove shared pretraining,
shared tokenizer, shared language habits or shared blind spots. A prompt that
both models find natural, or a fact both fill in the same way, can still be
wrong. Cross-family review is the control for that shared failure mode.

A reviewer counts as cross-family only if it has different weights from the
drafter (different model family or a provider whose base model is not
MiMo-V2.6). A different prompt, temperature, or context on the same base model
does **not** qualify.

## Configured families

| Role | Model | Family | Env var |
| --- | --- | --- | --- |
| Teacher / drafter | `Qwen/Qwen3-32B` | Qwen3 | `TEACHER_MODEL_ID` |
| Independent reviewer | `mistralai/Mistral-Small-24B-Instruct-2501` | Mistral | `REVIEW_MODEL_ID` |

The reviewer must differ from the teacher **and** from the fine-tune base.
Changing either id requires recording it with the review output.

## Input package (exact)

| Item | Value |
| --- | --- |
| File | `../review/cross-family-input.jsonl` |
| Manifest | `../review/cross-family-input.manifest.json` |
| Records | 56 |
| SHA-256 | `f7a0a1f4edb1a3a16b990878e2333cc00a5b6a0aa141a6891d8ab418c02bc492` |
| Fields per record | `id`, `prompt_hash`, `prompt`, `expect_refusal`, `expected_intent`, `expected_behaviour` |
| Deliberately excluded | `why_valuable`, `research_inspiration`, `scenario`, `category`, `difficulty`, `language`, `coverage_cell`, `length_category` |

Regenerate with `python havyn-stage5/prompt-authoring/review/export_review_package.py`.
If the SHA changes, the candidate set changed: re-run the review; do not mix
results from two package hashes.

The package is input only. It contains no review verdicts and no Model A
justification. Give the reviewer the file and the task prompt below, nothing
else.

## Reviewer task prompt (copy-paste)

```text
You are an independent adversarial reviewer for a networking-intent dataset.
You will receive JSONL records. Each record has:
  id, prompt, expect_refusal, expected_intent, expected_behaviour.

Your job is to decide whether the prompt and the source intent describe the
same network request, and whether the expected behaviour is the single correct
response. You must NOT trust the intent as ground truth merely because it is
present. Re-derive the requirements from the prompt yourself, then compare.

For each record:
1. Re-derive the network requirements from the prompt alone:
   sites, areas, host groups and counts, VLANs/segments, addressing
   (DHCP/static), routing mode, internet access and where it enters, servers
   and services, wireless, IoT, firewall/DMZ, exclusions, and any stated
   priority or constraint.
2. Compare your derivation with expected_intent field by field.
   - missing_facts: facts in the intent that the prompt never states
     (invented by the author).
   - lost_requirements: facts in the prompt that the intent does not carry.
   - contradictions: two prompt facts, or a prompt fact and the intent, that
     cannot both hold.
3. Check expected_behaviour:
   - For a buildable record: does it state the single correct build outcome,
     without claiming verification the engine cannot perform?
   - For expect_refusal=true: is the named refusal / clarification / surfaced
     conflict the single correct response? Is the capability claim real, or
     only asserted? Flag unverifiable capability claims; do not invent them.
   - Flag any open menu ("either X or Y is acceptable") where one definite
     response is required.
4. Judge language authenticity for the record's language (do not translate;
   technical terms staying English is authentic). Flag meta-text, reviewer
   leftovers, form scaffolds and machine enumeration.
5. Verdict: ACCEPT / REVIEW / REJECT.
   ACCEPT = faithful, complete, natural, one definite expected behaviour.
   REVIEW = a human must confirm a deliberate design/capability choice.
   REJECT = invented facts, lost requirements, unresolved contradiction, or
            unnatural/template language that a rewrite cannot fix cheaply.
6. Output one JSON object per record, in the input order, with exactly:
   id, recovered_intent_summary, missing_facts, lost_requirements,
   invented_facts, contradictions, ambiguity_notes, human_likeness (0..1),
   fidelity (0..1), verdict, reason.
Do not edit the input. Do not use the author's rationale; it is not provided.
```

## Output location and format

Save the reviewer's raw output, unedited, as:

```text
havyn-stage5/prompt-authoring/review/cross-family-reviews/<provider>-<model>-<YYYY-MM-DD>.json
```

with a wrapper:

```json
{
  "provider": "...",
  "model": "...",
  "model_version_or_snapshot": "...",
  "reviewed_at": "...",
  "input_sha256": "c0baaa...",
  "temperature": 0,
  "records": [ ...one object per candidate... ]
}
```

Rules:

- Never edit a review file after it is written. A later review goes in a new
  file with its own date/model.
- Record the exact model id/version. "GPT-class" is not a model id.
- If the provider cannot expose temperature, record `null`; do not guess.
- Keep the input package hash in the review file. A review without the hash is
  not tied to the candidate set.

## Adjudication

1. Run the package on at least **two different families** if possible. One is
   the minimum; two lets you see family-specific disagreement.
2. Compare against the same-base batch-1..5 verdicts and the post-fix re-check.
   Report **agreement rate** and list every disagreement by id. Do not average
   the verdicts and do not silently overrule the independent reviewer.
3. Any `REJECT` from an independent family blocks freeze until the candidate is
   fixed or dropped. A `REVIEW` is resolved by the owner with a recorded
   decision, as in `POST_FIX_RECHECK.md`.
4. Update the status table in `owner_gold_authoring_plan.md` only
   after the independent file is committed. Keep the historical Model-B files
   unchanged.
5. Freeze gate: 56/56 candidates have an independent-family verdict, no
   unresolved REJECT, and every REVIEW has an owner decision.

## What this protocol does not verify

- It does **not** verify live Packet Tracer behaviour. The reviewer sees text,
  not the engine.
- It does **not** verify that a candidate's exact wire-input triggers the named
  refusal. The capability probes are separate
  (`capability-gap-probes.json`).
- It does **not** make the candidate owner-authored Gold. Only Hamza's rewrite
  and adoption do that.

## Current honest status

| Check | Status |
| --- | --- |
| Input package generated | yes (56 records, SHA above) |
| Same-base Model B (batch-1..4) | done; **not cross-family** |
| Same-base batch-5 re-read | done; **not cross-family** |
| Same-base post-fix re-check | done; **not cross-family** |
| Different-family review | **not done** |
| Cross-family freeze gate | **not met** |
