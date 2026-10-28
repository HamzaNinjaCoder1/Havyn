# Owner-gold authoring plan (Stage 5)

PROVENANCE — read this before using anything here.

The 56 records in `../owner-candidates/` are **AI-assisted drafts** (`oc-NNN`),
not owner-authored gold. Nothing in this plan is labelled lived experience, and
no record may enter `datasets/gold-v1/` until Hamza **reviews it, rewrites it in
his own words, confirms the expected intent/refusal, and adopts it** with
`provenance.origin: "owner"` + `ai_assisted: true`. An `oc-NNN` id is never a
`gold-NNN` id.

This file lays out **100 gold slots**:

- **gold-001..056** — backed by an existing candidate (engine-verified; may still
  carry a Model B `REVIEW` question for Hamza).
- **gold-057..100** — **coverage-driven OPEN slots**: no candidate exists yet, so
  they need an owner-authored prompt targeting that certified cell.

## Status rule

| final status | meaning |
| --- | --- |
| **ACCEPT** | engine-verified AND (post-fix Model B ACCEPT, or pre-fix ACCEPT with only mechanical pins/label changes) |
| **REVIEW** | engine-verified, but a residual fidelity/naturalness/grading question remains — Hamza must confirm before adoption |
| **REJECT** | engine-verified but the reviewer's catch stands — must not be adopted as-is (none remain) |
| **OPEN** | no candidate; needs owner authoring |

A candidate's final status is the Model B verdict for the revision that shipped.
25 candidates were re-reviewed after the fix pass (`review/model-b-reviews/batch-5.json`);
the rest kept a pre-fix verdict only where nothing but VLAN pins / language
labels changed.

## Counts as shipped

- engine-verified: **56 / 56** (0 failures; 34 buildable + 22 refusal) at commit `06c57ced050cda532915c15b1ce772b0a0eaf607`
- Model B, post-fix re-review (25 candidates): **ACCEPT 16 / REVIEW 9 / REJECT 0**
- final status across all 56: **ACCEPT 40 / REVIEW 16 / REJECT 0**
- language labels: **en 30 / roman-urdu 18 / mixed 8**
- candidate-vs-certified coverage: exact 1 / label-only 14 / feature-match 4 / extension-cell 15 / refusal 22
- open-slot target languages: en 24 / roman-urdu 11 / mixed 9

The coverage classification is descriptive, not a relabel. `label-only` means the
declared cell id exists but the intent's features differ from it; `extension-cell`
means the certified matrix has no cell for that shape. Those are honest gaps, and
several are deliberate capability-boundary/campus shapes.

## Table 1 — candidate-backed slots (gold-001..056)

| slot | candidate | lang | diff | category | target cell | shape | coverage | final |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gold-001 | oc-001 | en | L1 | basic | `small-s1-a1-single-static-none-none` | buildable | label-only | **ACCEPT** |
| gold-002 | oc-002 | roman-urdu | L1 | basic | `small-s1-a1-single-static-none-none` | buildable | label-only | **ACCEPT** |
| gold-003 | oc-003 | en | L2 | vlan-dhcp | `small-s1-a2-multi-dhcp-dsl-none` | buildable | label-only | **ACCEPT** |
| gold-004 | oc-004 | mixed | L2 | wan-multi-site | `small-s2-a1-multi-dhcp-wan-none` | buildable | exact | **REVIEW** |
| gold-005 | oc-005 | en | L3 | routing | `medium-s1-a3-multi-static-ospf-wan-none` | buildable | label-only | **REVIEW** |
| gold-006 | oc-006 | roman-urdu | L3 | routing | `small-s2-a1-multi-static-wan-none` | buildable | feature-match | **REVIEW** |
| gold-007 | oc-007 | en | L3 | wireless | `small-s1-a2-multi-dhcp-cable-none` | refusal | refusal | **REVIEW** |
| gold-008 | oc-008 | mixed | L2 | wireless | `small-s1-a1-multi-dhcp-cable-none` | buildable | extension-cell | **ACCEPT** |
| gold-009 | oc-009 | en | L4 | firewall-security | `small-s1-a1-single-dhcp-wan-asa-dmz` | buildable | label-only | **ACCEPT** |
| gold-010 | oc-010 | roman-urdu | L4 | firewall-security | `small-s1-a1-single-dhcp-wan-asa` | buildable | label-only | **ACCEPT** |
| gold-011 | oc-011 | en | L3 | iot | `small-s1-a2-multi-dhcp-cable-none-iot` | buildable | label-only | **ACCEPT** |
| gold-012 | oc-012 | en | L5 | long-document | `medium-s1-a3-multi-static-wan-none` | buildable | extension-cell | **REVIEW** |
| gold-013 | oc-013 | mixed | L5 | enterprise | `small-s2-a2-multi-static-ospf-wan-none` | buildable | extension-cell | **REVIEW** |
| gold-014 | oc-014 | en | L4 | ambiguous | `ambiguous-missing-sites` | refusal | refusal | **ACCEPT** |
| gold-015 | oc-015 | roman-urdu | L3 | ambiguous | `ambiguous-addressing` | refusal | refusal | **ACCEPT** |
| gold-016 | oc-016 | mixed | L4 | ambiguous | `ambiguous-internet-type` | refusal | refusal | **ACCEPT** |
| gold-017 | oc-017 | en | L5 | contradictory | `contradiction-guest-isolation-vs-reach` | refusal | refusal | **ACCEPT** |
| gold-018 | oc-018 | roman-urdu | L5 | contradictory | `contradiction-isolation-vs-service` | refusal | refusal | **ACCEPT** |
| gold-019 | oc-019 | en | L4 | impossible-refusal | `refusal-asa5505-multisite-dmz` | refusal | refusal | **ACCEPT** |
| gold-020 | oc-020 | mixed | L3 | impossible-refusal | `refusal-server-dhcp` | refusal | refusal | **ACCEPT** |
| gold-021 | oc-021 | en | L4 | impossible-refusal | `refusal-firewall-multivlan` | refusal | refusal | **ACCEPT** |
| gold-022 | oc-022 | en | L2 | dhcp-static | `small-s1-a1-single-static-none-none` | buildable | label-only | **ACCEPT** |
| gold-023 | oc-023 | roman-urdu | L3 | servers | `small-s1-a2-multi-dhcp-none-none` | buildable | extension-cell | **ACCEPT** |
| gold-024 | oc-024 | en | L4 | diagnosis | `small-s1-a2-multi-dhcp-dsl-none` | buildable | label-only | **ACCEPT** |
| gold-025 | oc-025 | en | L4 | enterprise | `small-s1-a3-multi-dhcp-wan-none` | buildable | extension-cell | **ACCEPT** |
| gold-026 | oc-026 | mixed | L4 | enterprise | `medium-s1-a3-multi-dhcp-cable-none` | refusal | refusal | **ACCEPT** |
| gold-027 | oc-027 | en | L3 | enterprise | `small-s1-a3-multi-dhcp-cable-none-iot` | refusal | refusal | **ACCEPT** |
| gold-028 | oc-028 | en | L4 | enterprise | `medium-s1-a2-multi-dhcp-cable-none` | buildable | extension-cell | **REVIEW** |
| gold-029 | oc-029 | roman-urdu | L2 | vlan-dhcp | `small-s1-a2-multi-dhcp-dsl-none` | buildable | label-only | **ACCEPT** |
| gold-030 | oc-030 | mixed | L3 | routing | `small-s3-a1-multi-static-wan-none` | buildable | feature-match | **REVIEW** |
| gold-031 | oc-031 | en | L1 | basic | `small-s1-a1-single-static-none-none` | buildable | label-only | **ACCEPT** |
| gold-032 | oc-032 | roman-urdu | L1 | basic | `small-s1-a1-single-static-none-none` | buildable | label-only | **ACCEPT** |
| gold-033 | oc-033 | en | L2 | vlan-dhcp | `small-s1-a3-multi-dhcp-dsl-none` | buildable | extension-cell | **ACCEPT** |
| gold-034 | oc-034 | roman-urdu | L2 | dhcp-static | `small-s1-a2-multi-dhcp-none-none` | buildable | extension-cell | **ACCEPT** |
| gold-035 | oc-035 | en | L3 | wan-multi-site | `small-s3-a1-multi-static-wan-none` | buildable | feature-match | **REVIEW** |
| gold-036 | oc-036 | en | L3 | routing | `small-s2-a1-multi-static-ospf-wan-none` | buildable | extension-cell | **ACCEPT** |
| gold-037 | oc-037 | roman-urdu | L4 | wan-multi-site | `small-s2-a2-multi-static-wan-none` | buildable | feature-match | **ACCEPT** |
| gold-038 | oc-038 | en | L2 | wireless | `small-s1-a1-multi-dhcp-cable-none` | buildable | extension-cell | **ACCEPT** |
| gold-039 | oc-039 | roman-urdu | L3 | wireless | `small-s1-a2-multi-dhcp-cable-none` | buildable | extension-cell | **ACCEPT** |
| gold-040 | oc-040 | en | L3 | iot | `small-s1-a2-multi-dhcp-cable-none-iot` | buildable | label-only | **REVIEW** |
| gold-041 | oc-041 | roman-urdu | L4 | enterprise | `small-s1-a3-multi-dhcp-wan-none` | buildable | extension-cell | **ACCEPT** |
| gold-042 | oc-042 | en | L3 | campus | `medium-s1-a2-multi-dhcp-cable-none` | buildable | extension-cell | **ACCEPT** |
| gold-043 | oc-043 | en | L3 | ambiguous | `ambiguous-host-count` | refusal | refusal | **ACCEPT** |
| gold-044 | oc-044 | roman-urdu | L4 | ambiguous | `ambiguous-routing` | refusal | refusal | **REVIEW** |
| gold-045 | oc-045 | roman-urdu | L5 | ambiguous | `ambiguous-server-internet` | refusal | refusal | **ACCEPT** |
| gold-046 | oc-046 | en | L4 | contradictory | `contradiction-total-isolation-vs-full-mesh` | refusal | refusal | **ACCEPT** |
| gold-047 | oc-047 | roman-urdu | L4 | contradictory | `contradiction-static-vs-dhcp` | refusal | refusal | **ACCEPT** |
| gold-048 | oc-048 | en | L5 | contradictory | `contradiction-internet-shape` | refusal | refusal | **REVIEW** |
| gold-049 | oc-049 | roman-urdu | L5 | contradictory | `contradiction-guest-triple` | refusal | refusal | **REVIEW** |
| gold-050 | oc-050 | en | L3 | impossible-refusal | `refusal-wireless-too-large` | refusal | refusal | **ACCEPT** |
| gold-051 | oc-051 | roman-urdu | L3 | impossible-refusal | `refusal-asa5505-dmz` | refusal | refusal | **ACCEPT** |
| gold-052 | oc-052 | en | L4 | impossible-refusal | `refusal-dhcp-and-cidr` | refusal | refusal | **ACCEPT** |
| gold-053 | oc-053 | en | L5 | long-document | `medium-s1-a2-multi-static-wan-none` | buildable | extension-cell | **REVIEW** |
| gold-054 | oc-054 | mixed | L5 | long-document | `small-s2-a2-multi-static-wan-none-iot` | refusal | refusal | **REVIEW** |
| gold-055 | oc-055 | en | L3 | diagnosis | `small-s1-a2-multi-dhcp-dsl-none` | buildable | label-only | **ACCEPT** |
| gold-056 | oc-056 | roman-urdu | L3 | enhancement | `small-s1-a2-multi-dhcp-none-none` | buildable | extension-cell | **REVIEW** |

## Review residuals (16 REVIEW candidates)

Each is engine-verified; the note is what Hamza must confirm (or fix) before
adoption. Sourced from `review/model-b-reviews/batch-5.json` or the pre-fix batch
for mechanical-only candidates.

- **oc-004** (pre-fix REVIEW + mechanical-only): Mostly faithful and natural, but the static-routing requirement rests on a vague phrase. A model that chose OSPF or left routing to defaults would be penalized by the intent despite the prompt not clearly forbidding it. A human should confirm the phrasing or the grading.
- **oc-005** (pre-fix REVIEW + mechanical-only): Strong natural phrasing (the OSPF ask is embedded in a growth rationale, not a keyword), but two intent requirements — DHCP everywhere and OSPF specifically — are not stated or only weakly implied by the prompt. A human should decide whether those are graded or defaulted.
- **oc-006** (pre-fix REVIEW + mechanical-only): All the stated requirements (two offices, static-only, internet through HO) are faithfully represented in natural Roman-Urdu. The one drift is DHCP: the intent requires it at both sites but the prompt never mentions addressing, which is a substantive build decision (DHCP server device), not just capacity slack.
- **oc-007** (pre-fix REVIEW + mechanical-only): As a refusal-by-name probe the trigger phrase ('must not be able to reach any of our internal machines') is clearly present and the named refusal plus offer-to-build-on-separate-subnet is consistent with the project's refuse-by-name rule. But this is a deliberate capability-boundary item with a null intent; a human should confirm the capability gap is still real and that a clear, reasonable user request being refused is the intended eval signal.
- **oc-012** (batch-5 re-review): Strong long-form brief and the four-VLAN/DHCP-vs-static shape is faithful, but two real gaps remain: the guest-facing kiosks named in the prompt are silently dropped, and the stated 'sales and warehouse can open the intranet' audience is not expressed. Modelling the 4 docked laptops as PCs is a substitution a human should confirm. The finance-isolation requirement is carried as a critical REQ while the declared connected routing leaves it unenforceable - the candidate does not claim otherwise, but the contradiction is unresolved. Structure is section-labelled and slightly form-like ('Background:', 'Constraints / exclusions:'), which hurts naturalness.
- **oc-013** (pre-fix REVIEW + mechanical-only): Faithful and internally consistent — no lost or invented network facts of substance. Held back only because its document skeleton and exclusion clauses duplicate oc-012 almost clause-for-clause, which a human should diversify before this looks like real user traffic.
- **oc-028** (batch-5 re-review): The three-role split, two SSIDs and DHCP shape are faithful, but two defects need a human: (1) the wireless handhelds/forklift terminals are modelled as six tablets, a substitution the prompt never asks for, and (2) the 'warehouse system' the ops devices exist to talk to is dropped entirely, so a load-bearing part of the operations requirement is unrepresented. The contractor segment is declared with no hosts and the 'internet only' clause is resolved by interpretation - defensible, but worth confirming given the same phrasing is treated as a refusal in oc-054.
- **oc-030** (batch-5 re-review): Routing requirements are faithful, but the intent invents host groups (8/4/4 PCs) plus DHCP/NAT facts from a prompt that states no endpoints at all — a human should confirm or strip them.
- **oc-035** (pre-fix REVIEW + mechanical-only): Faithful and natural on its own ('we don't run a routing protocol on a network this size' is good human texture), but it duplicates oc-030's intent exactly. Flagging the pair for human confirmation.
- **oc-040** (batch-5 re-review): Fidelity to its own prompt is good - staff VLAN, IoT VLAN, cabled smart LEDs, cable internet, and the honest UNKNOWN note on the separation clause. The defect is dataset-level rather than factual: oc-040 restates oc-011 (same scenario, same infrastructure, same expected_behaviour) and adds no new discriminating power, so a human should decide whether to keep both. No invented network facts and the English is natural.
- **oc-044** (batch-5 re-review): The scenario is genuinely under-determined and the prompt reads like a real user, so the candidate is not nonsense; the problem is grading determinism. expected_behaviour offers two acceptable behaviours instead of one definite ASK, so a correct clarification ('which routing mode, and how many hosts?') and a correct stated assumption both count - that is an open menu. The clarification also stops at routing mode while the prompt is missing load-bearing build data (host counts, addressing), which the rule says an ASK candidate should request rather than assume.
- **oc-048** (batch-5 re-review): The bonding refusal is solid and definite, but the second required issue is shaky — the prompt already states a single office, so 'must confirm the topology' is an open ask rather than a clearly correct response.
- **oc-049** (batch-5 re-review): Language and the internet-only-vs-internal-server contradiction are clean, but limb (c) leans on an unqualified generalisation about enforcement points and the expected output names no definite fallback, leaving the target response looser than oc-026's.
- **oc-053** (batch-5 re-review): The four-way structure (accounting, reception folded in, auditors, server room) and the DHCP/static split match the prompt well, and the two-server HTTP+DNS shape is right. Two defects keep it out of ACCEPT: auditor addressing is invented as static when the prompt is silent (unlike the explicitly automatic accountant/reception PCs), and the auditors-cannot-reach-servers requirement coexists with connected routing and no ACL, so the headline isolation ask is not actually enforced by the declared build. The 'accounting side only' internet scope is also unexpressed. Section-labelled phrasing reads form-like.
- **oc-054** (batch-5 re-review): Prompt is faithful, language-authentic and human-sounding, but the refusal expectation is internally inconsistent: it refuses the guest 'no other device can see it' guarantee while declaring the identically-shaped POS isolation guarantee buildable, and reason (c) reads as a planner default rather than a hard refusal — a human should confirm whether refusal (or build-plus-UNKNOWN) is the single correct answer.
- **oc-056** (pre-fix REVIEW + mechanical-only): Natural and a valuable request type, but the intent's host counts and server details are ungrounded in the prompt. Either state rough sizes in the prompt or grade only on structural properties (separate accounts VLAN, file server exists, rest DHCP).

## Table 2 — coverage-driven open slots (gold-057..100)

Derived deterministically: 35 certified cells currently have **zero exact-match
candidate** (one slot each), plus 9 second-language variants of the largest
cells (to spread `roman-urdu` / `mixed`). Each needs an owner-authored prompt.

| slot | target cell | lang | diff | status | basis |
| --- | --- | --- | --- | --- | --- |
| gold-057 | `large-s1-a4-multi-dhcp-cable-none-iot` | mixed | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-058 | `large-s1-a4-multi-dhcp-dsl-none` | roman-urdu | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-059 | `large-s1-a4-multi-static-ospf-wan-none` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-060 | `large-s1-a4-single-dhcp-wan-asa` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-061 | `large-s1-a4-single-dhcp-wan-asa-dmz` | en | L5 | OPEN | uncovered certified cell (0 exact fill) |
| gold-062 | `large-s1-a4-single-static-none-none` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-063 | `medium-s1-a3-multi-dhcp-cable-none-iot` | mixed | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-064 | `medium-s1-a3-multi-dhcp-dsl-none` | roman-urdu | L3 | OPEN | uncovered certified cell (0 exact fill) |
| gold-065 | `medium-s1-a3-multi-static-ospf-wan-none` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-066 | `medium-s1-a3-single-dhcp-wan-asa` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-067 | `medium-s1-a3-single-dhcp-wan-asa-dmz` | en | L5 | OPEN | uncovered certified cell (0 exact fill) |
| gold-068 | `medium-s1-a3-single-static-none-none` | en | L1 | OPEN | uncovered certified cell (0 exact fill) |
| gold-069 | `medium-s2-a2-multi-dhcp-cable-none-iot` | mixed | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-070 | `medium-s2-a2-multi-dhcp-wan-none` | roman-urdu | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-071 | `medium-s2-a2-single-dhcp-wan-asa` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-072 | `medium-s2-a2-single-static-none-none` | en | L1 | OPEN | uncovered certified cell (0 exact fill) |
| gold-073 | `small-s1-a1-multi-dhcp-cable-none-iot` | mixed | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-074 | `small-s1-a1-multi-dhcp-dsl-none` | roman-urdu | L3 | OPEN | uncovered certified cell (0 exact fill) |
| gold-075 | `small-s1-a1-multi-static-ospf-wan-none` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-076 | `small-s1-a1-single-dhcp-wan-asa` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-077 | `small-s1-a1-single-dhcp-wan-asa-dmz` | en | L5 | OPEN | uncovered certified cell (0 exact fill) |
| gold-078 | `small-s1-a1-single-static-none-none` | en | L1 | OPEN | uncovered certified cell (0 exact fill) |
| gold-079 | `small-s1-a2-multi-dhcp-cable-none-iot` | mixed | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-080 | `small-s1-a2-multi-dhcp-dsl-none` | roman-urdu | L3 | OPEN | uncovered certified cell (0 exact fill) |
| gold-081 | `small-s1-a2-multi-static-ospf-wan-none` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-082 | `small-s1-a2-single-dhcp-wan-asa` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-083 | `small-s1-a2-single-dhcp-wan-asa-dmz` | en | L5 | OPEN | uncovered certified cell (0 exact fill) |
| gold-084 | `small-s1-a2-single-static-none-none` | en | L1 | OPEN | uncovered certified cell (0 exact fill) |
| gold-085 | `small-s2-a1-multi-dhcp-cable-none-iot` | mixed | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-086 | `small-s2-a1-single-dhcp-wan-asa` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-087 | `small-s2-a1-single-static-none-none` | en | L1 | OPEN | uncovered certified cell (0 exact fill) |
| gold-088 | `small-s3-a1-multi-dhcp-cable-none-iot` | mixed | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-089 | `small-s3-a1-multi-dhcp-wan-none` | roman-urdu | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-090 | `small-s3-a1-single-dhcp-wan-asa` | en | L4 | OPEN | uncovered certified cell (0 exact fill) |
| gold-091 | `small-s3-a1-single-static-none-none` | en | L1 | OPEN | uncovered certified cell (0 exact fill) |
| gold-092 | `large-s1-a4-multi-dhcp-cable-none-iot` | en | L4 | OPEN | language-completeness variant of a large/high-value cell |
| gold-093 | `large-s1-a4-multi-dhcp-dsl-none` | mixed | L4 | OPEN | language-completeness variant of a large/high-value cell |
| gold-094 | `large-s1-a4-multi-static-ospf-wan-none` | roman-urdu | L4 | OPEN | language-completeness variant of a large/high-value cell |
| gold-095 | `large-s1-a4-single-dhcp-wan-asa` | roman-urdu | L4 | OPEN | language-completeness variant of a large/high-value cell |
| gold-096 | `large-s1-a4-single-dhcp-wan-asa-dmz` | roman-urdu | L5 | OPEN | language-completeness variant of a large/high-value cell |
| gold-097 | `large-s1-a4-single-static-none-none` | roman-urdu | L4 | OPEN | language-completeness variant of a large/high-value cell |
| gold-098 | `medium-s1-a3-multi-dhcp-cable-none-iot` | en | L4 | OPEN | language-completeness variant of a large/high-value cell |
| gold-099 | `medium-s1-a3-multi-dhcp-dsl-none` | mixed | L3 | OPEN | language-completeness variant of a large/high-value cell |
| gold-100 | `medium-s1-a3-multi-static-ospf-wan-none` | roman-urdu | L4 | OPEN | language-completeness variant of a large/high-value cell |

## How to adopt a candidate into gold-v1

1. Read the candidate prompt + `expected_behaviour` + its REVIEW residual if any.
2. Rewrite the prompt in your own words (keep the network facts; do not "fix" a
   refusal into a build or vice versa).
3. Confirm the `expected_intent` still describes what you actually asked for. If
   the engine refuses, confirm the named refusal is the correct answer.
4. Write a `gold-NNN.json` per `datasets/gold-v1/README.md`: `provenance.origin =
   "owner"`, `ai_assisted = true`, `adopted_from = "oc-NNN"`, your `author` id.
5. Run `python scripts/validate_gold.py` (schema → lint → expand → spec-lint →
   IPAM → compile) and only then add it to the frozen set.

## Blockers / what is still unverified

- **gold-001..100 do not exist yet.** gold-001..056 are candidates awaiting
  adoption; gold-057..100 have no prompt at all.
- **gold-101..300** need real human contributors; only the workflow/templates in
  `../external-collection/` exist.
- **Truly independent Model B**: both rounds ran on the same base model with
  context isolation (`../review/README.md`), not a different model family. A
  cross-family review is still required before freeze.
- **Refusal candidates are source-verified, not executed end-to-end** — see
  `refusal_source_verification.md`.
- Live Packet Tracer, GPU training and GGUF export are out of scope here and
  remain unverified.
