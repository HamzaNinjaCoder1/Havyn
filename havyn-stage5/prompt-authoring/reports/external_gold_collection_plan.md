# External gold collection plan — gold-101..300

Goal: 200 gold examples authored by **real people other than Hamza**, each
engine-validated and provenance-recorded. Status today: workflow, templates and
ledger exist (`../external-collection/`); **zero external examples collected** —
this cannot be advanced by AI, only by people.

## Why this half cannot be AI-generated

`datasets/gold-v1/README.md` splits the set by *author*, not by content:
`gold-001..100` are owner-authored, `gold-101..300` are authored by others. If
AI writes them, the split becomes a lie and the gold set loses its whole
function (measuring how a model handles prompts written by humans who are not
the person who built the engine). So:

- AI may draft, transcribe, sanitise, translate for review, and run the engine
  validator.
- AI must **not** author or reword the prompt and keep the contributor as
  `author`. If wording is AI-touched, the record is marked
  `ai_assisted_in_authoring: yes` and is not admissible as external gold.
- The honest failure mode is "collected fewer than 200", never "filled the
  quota with generated text".

## Target mix (mirrors gold-001..100 so both halves are comparable)

| Dimension | Target for gold-101..300 |
| --- | --- |
| Language | en ~90, roman-urdu ~65, mixed ~30, exploratory-other ~15 (kept separate; see exploratory pool) |
| Difficulty | L1 15, L2 40, L3 60, L4 55, L5 30 |
| Site count | 1 site ~110, 2 sites ~60, 3 sites ~30 |
| Categories | vlan-dhcp, routing, multi-site, wireless, security, iot, enterprise, troubleshooting/diagnosis, long-document, ambiguity, contradiction, refusal |
| Special | ≥25 long/document prompts, ≥20 ambiguity, ≥15 contradiction, ≥15 impossible/refusal, ≥15 diagnosis-style |
| Authors | ≥8 distinct contributors, no contributor >35 examples, ≤60% from any single organisation |

## Collection protocol (per contributor)

1. **Recruit real people**: colleagues/peers from networking or IT operations,
   CCNA/CCNP students who have done real lab work, small-business IT admins,
   friends working in ISP/bank/hospital/university IT. Diversity of voice is the
   point — not seniority.
2. **Brief (5 min)**: `external-collection/contributor-guide.md`. Emphasise: own
   words, own language, plain language preferred, no employer secrets, one
   request per file, variants welcome, ambiguity welcome.
3. **Capture (10–15 min)**: they write into `submission-template.json`; the
   collector may transcribe a dictated request (`method` records it).
4. **Intake**: file under `../external-collection/incoming/`; create/extend the
   ledger row (consent before id creation).
5. **Engine check**: collector runs the record through the same chain as
   `validate_gold.py` (schema → intent lint → expansion → spec lint → IPAM →
   compile) and writes the exact result into `collector_engine_check`.
6. **Independent read**: a second person verifies prompt ↔ intent fidelity (no
   added/lost facts). Failures are returned to the contributor to fix **in their
   own words**.
7. **Adoption** (Hamza approves): `datasets/gold-v1/gold-<nnn>.json`, `author` =
   contributor pseudonym, intent exactly as validated, plus the provenance block
   below.

## Proposed gold-record extension (needs Hamza's approval)

The current gold format (`datasets/gold-v1/README.md`) has `author` + `notes`.
For external records we propose two extra fields — a *record* extension, not a
change to Intent Schema v1:

```json
"author": "ext-007",
"provenance": {
  "contributor_id": "ext-007",
  "method": "typed",
  "captured_by": "collector-1",
  "captured_at": "2026-11-04T11:20:00+05:00",
  "consent_ref": "ledger#ext-007",
  "ai_assisted_in_authoring": false,
  "verbatim": true
}
```

If this is declined, the same facts go into `notes` — but then they are
free-text, which is why the structured form is preferred.

## Quality gates (each external record must pass all)

1. Real-person provenance + consent recorded; `ai_assisted_in_authoring: false`.
2. `validate_gold.py` chain passes **or** `expect_refusal: true` with null
   intent and a definite expected behaviour naming a real engine refusal code.
3. Prompt ↔ intent fidelity: every prompt fact is in the intent, every intent
   requirement is in the prompt.
4. No secrets/personal data (collector double-check).
5. Duplicate control: not an exact/near duplicate of any existing gold or
   candidate (`quality_checks.py` scheme, MinHash ≥0.90).
6. Language label honest: `mixed` only when English *function words* are really
   code-switched in; tech terms alone do not make it mixed.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Contributors produce polished "exam answers" | Brief for plain speech; ask for a rushed/confused variant of the same request; prefer their first draft |
| Contributors send unreconstructable prompts | Engine check + independent read; refusal path when genuinely ambiguous |
| Quota pressure leads to AI-written "external" text | Hard rule stated above; ledger records method and AI involvement |
| Concentration in one voice/org | ≥8 contributors, ≤35 per contributor, ≤60% per org |
| Whisper/typing changes their wording | `verbatim: true` + captured_by recorded; contributor confirms the final text |

## Dependencies / blocked until

- Real contributors available (recruitment is human work).
- Hamza's approval of the `provenance` extension and of each adoption.
- Freeze (`freeze_dataset.py`) runs only after a set is complete and reviewed.
