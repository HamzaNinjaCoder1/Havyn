# External gold collection — workspace

This directory holds the workflow for `gold-101..300`: gold examples authored by
**real people other than Hamza**. Nothing in here is gold yet.

## Provenance rule (the whole point)

- `gold-001..100` = owner-authored (Hamza personally). Only Hamza can create
  them; AI drafts live in `../owner-candidates/` and are **not** gold.
- `gold-101..300` = external-authored. A record may only enter this pipeline
  with a real person behind it (contributor id + how the prompt was captured).
- **AI may never fill a contributor slot.** AI can transcribe, sanitise, format,
  validate against the engine, and translate for review — it must not invent or
  rewrite the human's wording into a "better" prompt and keep `author` pointing
  at that person.
- If a contributor's prompt cannot pass the engine chain (`validate_gold.py`),
  it is either corrected *with the contributor* (their words, their fix) or
  recorded as `expect_refusal: true` with null intent — never quietly repaired
  by the collector.

## Files

| File | Purpose |
| --- | --- |
| `contributor-guide.md` | What we ask a contributor to do (10–15 minutes, no networking degree required to try). |
| `submission-template.json` | The only shape a submission may take. One file per prompt, id `sub-<contributor-id>-<n>`. |
| `provenance-ledger.md` | Ledger of contributors and captures (pseudonymous ids). |
| `incoming/` | Raw submissions, exactly as captured. Never edited in place. |

## Pipeline

1. **Capture** — contributor writes a real request in their own words (typed in
   the template, or dictated/transcribed verbatim with their approval). Language
   stays as they speak it (English / Roman-Urdu / mixed; other languages go to
   the exploratory pool, never silently into Schema v1).
2. **Intake** — collector files the raw submission under `incoming/`, records
   the contributor in `provenance-ledger.md` (id, date, method, consent).
3. **Engine check** — collector runs the submission through the same chain as
   `validate_gold.py` (schema → lint → expand → spec-lint → IPAM → compile).
   Results attached to the submission; failures are returned to the contributor
   with the engine's exact reason (this is how "her words, her fix" stays true).
4. **Review** — a second person (not the collector) reads prompt ↔ intent and
   flags any fact the prompt states that the intent lacks or vice versa.
5. **Adoption** — only after 3–4: the record is written to
   `datasets/gold-v1/gold-<101..300>.json` with `author` = contributor
   pseudonym, plus the proposed `provenance` block (see
   `../reports/external_gold_collection_plan.md`). Hamza approves the drop.

## Consent, privacy, licensing

- Written consent per contributor: the prompt may be used for engine evaluation
  and stored in the private gold set; the contributor may withdraw before
  adoption.
- No employer-confidential detail: contributors strip real hostnames, public IPs,
  customer names, and credentials before submission; the collector double-checks.
- Pseudonymous ids only (`ext-001`…). Contact details live in the ledger, not in
  gold records.
- Nothing here is published: the gold set stays private (see
  `datasets/gold-v1/README.md` rule 4).
