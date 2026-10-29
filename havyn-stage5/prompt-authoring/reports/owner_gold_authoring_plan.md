# Owner-gold authoring plan (Stage 5)

> **AI candidates are NOT Gold.** The 56 `oc-NNN` records are AI-assisted
> drafts. Nothing becomes `gold-NNN` until **Hamza personally rewrites the
> prompt in his own words, confirms the expected intent/refusal, and adopts the
> record** with the provenance block below. An `oc-NNN` id is never a
> `gold-NNN` id. Do not copy an AI prompt into `datasets/gold-v1/` unchanged.

> **2026-10-29:** the 8 former REVIEW candidates now have a technical
> resolution applied (`owner_decisions_resolution.md`): 6 BUILD (one
> after rewrite), 2 REFUSAL, 0 owner approvals. Owner confirmation is
> still required before any adoption.

## The 100 slots

| Block | Slots | Basis | Status today |
| --- | --- | --- | --- |
| Candidate-backed | `gold-001..056` | 56 engine-verified AI drafts awaiting owner rewrite/adoption | none adopted |
| Coverage-driven | `gold-057..100` | 35 certified cells with zero exact candidate + 9 language-completeness variants | 44 AI candidates verified; owner wording pending |

All 44 coverage-driven slots now have a verified AI candidate in
`../coverage-candidates/candidates/cc-NNN.json` (41 buildable + 3
capability-gap refusals, 44/44 engine-checked). **None is owner-authored
Gold.** They are ready for rapid owner adoption: rewrite or confirm the
prompt in your own words, confirm the expected intent/refusal, and adopt
with `origin: owner`, `ai_assisted: true`. The three refusals (`cc-071`,
`cc-086`, `cc-090`) record a real engine gap (multi-site WAN with an ASA
at site 1 -> `wan-device-missing`) and need the refusal wording confirmed
by the owner.

Total: **100**. Candidate-backed and coverage-driven are not the same thing as
coverage-exact: only **1** of the 56 candidates is an exact cell match (see the
coverage column). The rest are label-only, feature-match, extension-cell or
refusal. Do not relabel a candidate to pretend a cell is covered.

## Status of the candidate pool (shipped text)

- Engine-verified: **56/56** in the current working tree (34 buildable + 22
  refusal, 0 failures). See the reproducibility caveat in
  `reproducibility_dependency.md`: a clean `e8503cd` checkout verifies 48/56
  until the Stage 3/4 server-service support is committed.
- Post-fix semantic re-check: **ACCEPT 48 / REVIEW 8 / REJECT 0**.
- Technical decision pass: **ACCEPT 56 / REVIEW 0 / REJECT 0**. The 8 former
  REVIEWs now have recorded technical recommendations
  (`owner_decisions.json` / `owner_decisions.md`): 6 BUILD (oc-028 after a
  rewrite) and 2 REFUSAL (oc-007, oc-054). These are **not owner approvals**.
  Every candidate still has `owner_confirmation_required: true` and remains
  `ai_assisted_candidate`; none is owner-authored Gold.
- The re-check was performed by the **same base model** as the drafter and as
  Model B. It is **not** independent and **not** cross-family validation.
  Historical Model-B verdicts are preserved in `review/model-b-reviews/`;
  the shipped-text statuses are in `review/post-fix-recheck.json`.
- Language: **en 30 / roman-urdu 18 / mixed 8**.
- Difficulty: **L1 4 / L2 8 / L3 19 / L4 16 / L5 9**.
- Candidate-vs-certified coverage: exact 1 / label-only 14 / feature-match 4 /
  extension-cell 15 / refusal 22.

### Language and difficulty targets for all 100

| Dimension | Candidate-backed | Coverage-driven | Total target |
| --- | --- | --- | --- |
| en | 30 | 24 | **54** |
| roman-urdu | 18 | 11 | **29** |
| mixed | 8 | 9 | **17** |
| L1 | 4 | 6 | **10** |
| L2 | 8 | 0 | **8** |
| L3 | 19 | 4 | **23** |
| L4 | 16 | 29 | **45** |
| L5 | 9 | 5 | **14** |

The open-slot allocation is the plan's current target, not a frozen quota. If
you change a slot's language, keep the total mix roughly intact and record the
change in the slot row.

## How to use this plan

Three passes, in order:

1. **Confirm the 8 technical decisions** (table below). They are recommendations,
   not approvals; accept or override each, then rewrite/adopt or drop.
2. **Rewrite and adopt the 48 ACCEPT candidates.** Do not skip the rewrite: the
   value of the owner half is that the words are yours, not the model's.
3. **Author the 44 open slots** (`gold-057..100`) from scratch, targeting the
   listed certified cell.

Every adopted record must pass `havyn-stage5/scripts/validate_gold.py` before it
enters the frozen set.

Action codes used below:

- **A** — rewrite in your own words, confirm the intent, adopt as `gold-NNN`.
- **R** — resolve the residual question first; then rewrite + adopt or drop.
- **O** — author a new prompt from scratch for the target cell; validate + adopt.

## Table 1 — candidate-backed slots (`gold-001..056`)

`rewrite` is **full** for every row: AI text must not be adopted verbatim.
`coverage` is the objective classification from `coverage-report.json`.

| slot | candidate | lang | diff | coverage cell | coverage | rewrite | status | action |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gold-001 | oc-001 | en | L1 | `small-s1-a1-single-static-none-none` | label-only | full | ACCEPT | A |
| gold-002 | oc-002 | roman-urdu | L1 | `small-s1-a1-single-static-none-none` | label-only | full | ACCEPT | A |
| gold-003 | oc-003 | en | L2 | `small-s1-a2-multi-dhcp-dsl-none` | label-only | full | ACCEPT | A |
| gold-004 | oc-004 | mixed | L2 | `small-s2-a1-multi-dhcp-wan-none` | exact | full | ACCEPT | A |
| gold-005 | oc-005 | en | L3 | `medium-s1-a3-multi-static-ospf-wan-none` | label-only | full | ACCEPT | A |
| gold-006 | oc-006 | roman-urdu | L3 | `small-s2-a1-multi-static-wan-none` | feature-match | full | ACCEPT | A |
| gold-007 | oc-007 | en | L3 | `small-s1-a2-multi-dhcp-cable-none` | refusal | full | **ACCEPT** | A |
| gold-008 | oc-008 | mixed | L2 | `small-s1-a1-multi-dhcp-cable-none` | extension-cell | full | ACCEPT | A |
| gold-009 | oc-009 | en | L4 | `small-s1-a1-single-dhcp-wan-asa-dmz` | label-only | full | ACCEPT | A |
| gold-010 | oc-010 | roman-urdu | L4 | `small-s1-a1-single-dhcp-wan-asa` | label-only | full | ACCEPT | A |
| gold-011 | oc-011 | en | L3 | `small-s1-a2-multi-dhcp-cable-none-iot` | label-only | full | ACCEPT | A |
| gold-012 | oc-012 | en | L5 | `medium-s1-a3-multi-static-wan-none` | extension-cell | full | **ACCEPT** | A |
| gold-013 | oc-013 | mixed | L5 | `small-s2-a2-multi-static-ospf-wan-none` | extension-cell | full | **ACCEPT** | A |
| gold-014 | oc-014 | en | L4 | `ambiguous-missing-sites` | refusal | full | ACCEPT | A |
| gold-015 | oc-015 | roman-urdu | L3 | `ambiguous-addressing` | refusal | full | ACCEPT | A |
| gold-016 | oc-016 | mixed | L4 | `ambiguous-internet-type` | refusal | full | ACCEPT | A |
| gold-017 | oc-017 | en | L5 | `contradiction-guest-isolation-vs-reach` | refusal | full | ACCEPT | A |
| gold-018 | oc-018 | roman-urdu | L5 | `contradiction-isolation-vs-service` | refusal | full | ACCEPT | A |
| gold-019 | oc-019 | en | L4 | `refusal-asa5505-multisite-dmz` | refusal | full | ACCEPT | A |
| gold-020 | oc-020 | mixed | L3 | `refusal-server-dhcp` | refusal | full | ACCEPT | A |
| gold-021 | oc-021 | en | L4 | `refusal-firewall-multivlan` | refusal | full | ACCEPT | A |
| gold-022 | oc-022 | en | L2 | `small-s1-a1-single-static-none-none` | label-only | full | ACCEPT | A |
| gold-023 | oc-023 | roman-urdu | L3 | `small-s1-a2-multi-dhcp-none-none` | extension-cell | full | ACCEPT | A |
| gold-024 | oc-024 | en | L4 | `small-s1-a2-multi-dhcp-dsl-none` | label-only | full | ACCEPT | A |
| gold-025 | oc-025 | en | L4 | `small-s1-a3-multi-dhcp-wan-none` | extension-cell | full | ACCEPT | A |
| gold-026 | oc-026 | mixed | L4 | `medium-s1-a3-multi-dhcp-cable-none` | refusal | full | ACCEPT | A |
| gold-027 | oc-027 | en | L3 | `small-s1-a3-multi-dhcp-cable-none-iot` | refusal | full | ACCEPT | A |
| gold-028 | oc-028 | en | L4 | `medium-s1-a2-multi-dhcp-cable-none` | extension-cell | full | **ACCEPT** | A |
| gold-029 | oc-029 | roman-urdu | L2 | `small-s1-a2-multi-dhcp-dsl-none` | label-only | full | ACCEPT | A |
| gold-030 | oc-030 | mixed | L3 | `small-s3-a1-multi-static-wan-none` | feature-match | full | ACCEPT | A |
| gold-031 | oc-031 | en | L1 | `small-s1-a1-single-static-none-none` | label-only | full | ACCEPT | A |
| gold-032 | oc-032 | roman-urdu | L1 | `small-s1-a1-single-static-none-none` | label-only | full | ACCEPT | A |
| gold-033 | oc-033 | en | L2 | `small-s1-a3-multi-dhcp-dsl-none` | extension-cell | full | ACCEPT | A |
| gold-034 | oc-034 | roman-urdu | L2 | `small-s1-a2-multi-dhcp-none-none` | extension-cell | full | ACCEPT | A |
| gold-035 | oc-035 | en | L3 | `small-s3-a1-multi-static-wan-none` | feature-match | full | **ACCEPT** | A |
| gold-036 | oc-036 | en | L3 | `small-s2-a1-multi-static-ospf-wan-none` | extension-cell | full | ACCEPT | A |
| gold-037 | oc-037 | roman-urdu | L4 | `small-s2-a2-multi-static-wan-none` | feature-match | full | ACCEPT | A |
| gold-038 | oc-038 | en | L2 | `small-s1-a1-multi-dhcp-cable-none` | extension-cell | full | ACCEPT | A |
| gold-039 | oc-039 | roman-urdu | L3 | `small-s1-a2-multi-dhcp-cable-none` | extension-cell | full | ACCEPT | A |
| gold-040 | oc-040 | en | L3 | `small-s1-a2-multi-dhcp-dsl-none` | label-only | full | **ACCEPT** | A |
| gold-041 | oc-041 | roman-urdu | L4 | `small-s1-a3-multi-dhcp-wan-none` | extension-cell | full | ACCEPT | A |
| gold-042 | oc-042 | en | L3 | `medium-s1-a2-multi-dhcp-cable-none` | extension-cell | full | ACCEPT | A |
| gold-043 | oc-043 | en | L3 | `ambiguous-host-count` | refusal | full | ACCEPT | A |
| gold-044 | oc-044 | roman-urdu | L4 | `ambiguous-routing` | refusal | full | ACCEPT | A |
| gold-045 | oc-045 | roman-urdu | L5 | `ambiguous-server-internet` | refusal | full | ACCEPT | A |
| gold-046 | oc-046 | en | L4 | `contradiction-total-isolation-vs-full-mesh` | refusal | full | ACCEPT | A |
| gold-047 | oc-047 | roman-urdu | L4 | `contradiction-static-vs-dhcp` | refusal | full | ACCEPT | A |
| gold-048 | oc-048 | en | L5 | `contradiction-internet-shape` | refusal | full | ACCEPT | A |
| gold-049 | oc-049 | roman-urdu | L5 | `contradiction-guest-triple` | refusal | full | ACCEPT | A |
| gold-050 | oc-050 | en | L3 | `refusal-wireless-too-large` | refusal | full | ACCEPT | A |
| gold-051 | oc-051 | roman-urdu | L3 | `refusal-asa5505-dmz` | refusal | full | ACCEPT | A |
| gold-052 | oc-052 | en | L4 | `refusal-dhcp-and-cidr` | refusal | full | ACCEPT | A |
| gold-053 | oc-053 | en | L5 | `medium-s1-a2-multi-static-wan-none` | extension-cell | full | **ACCEPT** | A |
| gold-054 | oc-054 | mixed | L5 | `small-s2-a2-multi-static-wan-none-iot` | refusal | full | **ACCEPT** | A |
| gold-055 | oc-055 | en | L3 | `small-s1-a2-multi-dhcp-dsl-none` | label-only | full | ACCEPT | A |
| gold-056 | oc-056 | roman-urdu | L3 | `small-s1-a2-multi-dhcp-none-none` | extension-cell | full | ACCEPT | A |

## The 8 technical decisions — owner confirmation still required

These are technical recommendations, not owner approvals. Full basis and
engine-can/cannot text is in `owner_decisions.md` and `owner_decisions.json`.

| slot | candidate | recommendation | owner confirmation still needed |
| --- | --- | --- | --- |
| gold-007 | oc-007 | **REFUSAL** — guest-isolation is refused by name; no buildable form of the guarantee | Confirm you want a mainstream guest-WiFi request represented as a refusal |
| gold-012 | oc-012 | **BUILD** — four VLANs compile; finance isolation stays UNKNOWN | Confirm a critical isolation clause may be carried as UNKNOWN |
| gold-013 | oc-013 | **BUILD** — two sites/OSPF/DNS/WAN compile; accounts isolation stays UNKNOWN | Confirm UNKNOWN accounts isolation is acceptable |
| gold-028 | oc-028 | **BUILD_AFTER_REWRITE** — WMS now modelled; handhelds map to tablets; contractors are scope | Confirm the device mapping and contractor-scope reading |
| gold-035 | oc-035 | **BUILD** — no fidelity defect; duplicate topology with oc-030 is a dataset note | Confirm the duplicate topology earns a slot |
| gold-040 | oc-040 | **BUILD** — till/IoT/DSL compile; IoT separation stays UNKNOWN; similar to oc-011 | Confirm the second smart-LED scenario earns a slot |
| gold-053 | oc-053 | **BUILD** — three VLANs/servers/WAN compile; auditor isolation and WAN scope stay UNKNOWN | Confirm both UNKNOWN clauses are acceptable |
| gold-054 | oc-054 | **REFUSAL** — guest isolation, camera kind and one internet block are three real gaps | Confirm a multi-gap refusal is the single correct answer |

## Table 2 — coverage-driven open slots (`gold-057..100`)

These 44 slots have **no candidate**. Each row names the certified cell the
owner should hit. Author a prompt from scratch, then run it through
`validate_gold.py`. If the cell cannot be built with a faithful prompt, record
the refusal honestly or escalate — do not bend the prompt to fit the engine.

| slot | target cell | lang | diff | status | action |
| --- | --- | --- | --- | --- | --- |
| gold-057 | `large-s1-a4-multi-dhcp-cable-none-iot` | mixed | L4 | CANDIDATE | O |
| gold-058 | `large-s1-a4-multi-dhcp-dsl-none` | roman-urdu | L4 | CANDIDATE | O |
| gold-059 | `large-s1-a4-multi-static-ospf-wan-none` | en | L4 | CANDIDATE | O |
| gold-060 | `large-s1-a4-single-dhcp-wan-asa` | en | L4 | CANDIDATE | O |
| gold-061 | `large-s1-a4-single-dhcp-wan-asa-dmz` | en | L5 | CANDIDATE | O |
| gold-062 | `large-s1-a4-single-static-none-none` | en | L4 | CANDIDATE | O |
| gold-063 | `medium-s1-a3-multi-dhcp-cable-none-iot` | mixed | L4 | CANDIDATE | O |
| gold-064 | `medium-s1-a3-multi-dhcp-dsl-none` | roman-urdu | L3 | CANDIDATE | O |
| gold-065 | `medium-s1-a3-multi-static-ospf-wan-none` | en | L4 | CANDIDATE | O |
| gold-066 | `medium-s1-a3-single-dhcp-wan-asa` | en | L4 | CANDIDATE | O |
| gold-067 | `medium-s1-a3-single-dhcp-wan-asa-dmz` | en | L5 | CANDIDATE | O |
| gold-068 | `medium-s1-a3-single-static-none-none` | en | L1 | CANDIDATE | O |
| gold-069 | `medium-s2-a2-multi-dhcp-cable-none-iot` | mixed | L4 | CANDIDATE | O |
| gold-070 | `medium-s2-a2-multi-dhcp-wan-none` | roman-urdu | L4 | CANDIDATE | O |
| gold-071 | `medium-s2-a2-single-dhcp-wan-asa` | en | L4 | CANDIDATE | O |
| gold-072 | `medium-s2-a2-single-static-none-none` | en | L1 | CANDIDATE | O |
| gold-073 | `small-s1-a1-multi-dhcp-cable-none-iot` | mixed | L4 | CANDIDATE | O |
| gold-074 | `small-s1-a1-multi-dhcp-dsl-none` | roman-urdu | L3 | CANDIDATE | O |
| gold-075 | `small-s1-a1-multi-static-ospf-wan-none` | en | L4 | CANDIDATE | O |
| gold-076 | `small-s1-a1-single-dhcp-wan-asa` | en | L4 | CANDIDATE | O |
| gold-077 | `small-s1-a1-single-dhcp-wan-asa-dmz` | en | L5 | CANDIDATE | O |
| gold-078 | `small-s1-a1-single-static-none-none` | en | L1 | CANDIDATE | O |
| gold-079 | `small-s1-a2-multi-dhcp-cable-none-iot` | mixed | L4 | CANDIDATE | O |
| gold-080 | `small-s1-a2-multi-dhcp-dsl-none` | roman-urdu | L3 | CANDIDATE | O |
| gold-081 | `small-s1-a2-multi-static-ospf-wan-none` | en | L4 | CANDIDATE | O |
| gold-082 | `small-s1-a2-single-dhcp-wan-asa` | en | L4 | CANDIDATE | O |
| gold-083 | `small-s1-a2-single-dhcp-wan-asa-dmz` | en | L5 | CANDIDATE | O |
| gold-084 | `small-s1-a2-single-static-none-none` | en | L1 | CANDIDATE | O |
| gold-085 | `small-s2-a1-multi-dhcp-cable-none-iot` | mixed | L4 | CANDIDATE | O |
| gold-086 | `small-s2-a1-single-dhcp-wan-asa` | en | L4 | CANDIDATE | O |
| gold-087 | `small-s2-a1-single-static-none-none` | en | L1 | CANDIDATE | O |
| gold-088 | `small-s3-a1-multi-dhcp-cable-none-iot` | mixed | L4 | CANDIDATE | O |
| gold-089 | `small-s3-a1-multi-dhcp-wan-none` | roman-urdu | L4 | CANDIDATE | O |
| gold-090 | `small-s3-a1-single-dhcp-wan-asa` | en | L4 | CANDIDATE | O |
| gold-091 | `small-s3-a1-single-static-none-none` | en | L1 | CANDIDATE | O |
| gold-092 | `large-s1-a4-multi-dhcp-cable-none-iot` | en | L4 | CANDIDATE | O |
| gold-093 | `large-s1-a4-multi-dhcp-dsl-none` | mixed | L4 | CANDIDATE | O |
| gold-094 | `large-s1-a4-multi-static-ospf-wan-none` | roman-urdu | L4 | CANDIDATE | O |
| gold-095 | `large-s1-a4-single-dhcp-wan-asa` | roman-urdu | L4 | CANDIDATE | O |
| gold-096 | `large-s1-a4-single-dhcp-wan-asa-dmz` | roman-urdu | L5 | CANDIDATE | O |
| gold-097 | `large-s1-a4-single-static-none-none` | roman-urdu | L4 | CANDIDATE | O |
| gold-098 | `medium-s1-a3-multi-dhcp-cable-none-iot` | en | L4 | CANDIDATE | O |
| gold-099 | `medium-s1-a3-multi-dhcp-dsl-none` | mixed | L3 | CANDIDATE | O |
| gold-100 | `medium-s1-a3-multi-static-ospf-wan-none` | roman-urdu | L4 | CANDIDATE | O |

## Provenance rule (do not bend)

Every adopted `gold-NNN.json` carries:

```json
{
  "id": "gold-004",
  "author": "owner",
  "language": "mixed",
  "category": "wan-multi-site",
  "prompt": "<Hamza's own words, not the oc-NNN prompt>",
  "expect_refusal": false,
  "expected_intent": { "...": "..." },
  "difficulty": "L2",
  "notes": "owner-authored rewrite; inspired by oc-004",
  "provenance": {
    "origin": "owner",
    "author": "Hamza",
    "ai_assisted": true,
    "adopted_from": "oc-004",
    "rewritten": true,
    "adopted_at": "YYYY-MM-DD",
    "engine_commit": "<commit that verified the record>"
  }
}
```

Rules:

1. `prompt` must be rewritten by Hamza. If it is the AI draft verbatim,
   `rewritten` is false and the record is **not** owner-authored Gold.
2. `ai_assisted: true` is mandatory for candidate-backed slots. Hiding the AI
   assistance would make the provenance false; the dataset's value depends on
   this being honest.
3. `adopted_from` records the candidate id; it does not make the candidate Gold.
4. `expected_intent` must be confirmed against the rewritten prompt. If the
   rewrite changes a network fact, update the intent and re-run
   `validate_gold.py`; if the fact was load-bearing, do not change it.
5. A refusal must stay a refusal. Do not "fix" a refusal into a build, or a
   build into a refusal, to make a slot easier.
6. Run `python havyn-stage5/scripts/validate_gold.py`; the record is not Gold
   until it passes. A refusal record passes only with `expect_refusal: true`
   and `expected_intent: null`.
7. Never train on `datasets/gold-v1/`; the freeze/leak check is separate.

## Blockers / what is still open

- **0 of 100 Gold slots exist.** Candidate-backed slots are drafts; open slots
  have no prompt.
- **56 candidates are engine-verified in the working tree**, but a clean
  `e8503cd` checkout verifies 48/56 until the Stage 3/4 server-service support
  is committed. See `reproducibility_dependency.md`.
- **The 8 technical decisions still need your confirmation** before adoption;
  they are not owner approvals. See `owner_decisions.md`.
- **Truly independent review is not done.** The previous Model-B rounds used
  the same base model with context isolation; the post-fix re-check is also the
  same base model. Cross-family review is still required before freeze; the
  package is in `cross-family-review-protocol.md`.
- **200 external examples (gold-101..300) do not exist.** Only the workflow
  exists; see `external_gold_collection_plan.md`.
- Live Packet Tracer, GPU training, evaluation, and GGUF export are outside this
  document and remain unverified.
