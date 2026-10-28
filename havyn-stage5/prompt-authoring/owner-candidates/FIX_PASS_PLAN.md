# Fix-pass plan — post-Model-B review (oc-001..oc-056)

Execution working paper. This plan turns the Model B verdicts (ACCEPT 24 /
REVIEW 27 / REJECT 5 in `review/model-b-reviews/batch-1..4.json`) into concrete
edits on ONE file, then re-proves the pool with the existing harnesses.

## Rules of engagement (do not bend)

1. Edit **only** `candidate_data.py` in this directory. Do NOT edit
   `verify_candidates.py`, `quality_checks.py`, anything under
   `review/model-b-reviews/`, `datasets/gold-v1/`, or anything under `src/`.
2. Preserve the provenance docstring at the top of `candidate_data.py`
   verbatim. Every record stays an AI-assisted draft (`oc-*` id, never `gold-*`).
3. **Fidelity invariant for every prompt edit**: no network fact in a prompt may
   be absent from the intent it claims to represent, and no requirement in the
   intent may vanish from the prompt. Fixes remove Model A's inventions or
   complete the intent where the prompt legitimately implies something the
   intent forgot (e.g. VLAN placement) — never the reverse.
4. Humanized tone: prompts read like a real engineer/manager/beginner wrote
   them. No signposting, no meta-text ("Note:", "Reviewer:", "Both are firm
   requirements", "here's the key thing", "Two hard requirements:"), no
   leftover reviewer notes, no machine enumeration scaffolds unless the
   persona genuinely writes that way.
5. For each REJECT/REVIEW candidate: **read its entry in the batch JSONs first**
   (`review/model-b-reviews/batch-*.json`) — the reviewer's exact catch is
   authoritative over this plan's summary of it. This plan tells you the agreed
   *resolution*; the batch tells you the exact *defect*.
6. When a fix and the batch text seem to conflict, or an engine fact blocks the
   planned fix: do NOT improvise a new scenario — record it under
   "Deviations/escalations" in your report and apply the safest faithful
   alternative (keep facts, keep expectation honest).
7. Do not "fix" anything by weakening verification: never relabel a refusal as
   buildable or vice versa except where this plan says so explicitly, and never
   touch the scripts to make numbers pass.

## Engine facts these fixes rely on (already probe-verified)

- **First-VLAN landing**: `_instance_for(None, site_id)` in
  `src/network_autopilot/ai/expander.py` puts every host group with no `vlan`
  pin into the site's FIRST VLAN. A prompt describing "sales and finance in
  separate networks" whose groups are unpinned is therefore not realized —
  cross-VLAN reachability criteria only generate when groups pin *different*
  VLANs. Probes: `C:\Users\Hamza\AppData\Local\Temp\opencode\probe_placement.py`
  and `probe_unpinned.py`.
- The compiler derives `switchport access vlan` from the endpoint's IP subnet
  (`compiler/compiler.py` `_build_vlan_switch_commands`), so pins are the only
  missing link — no compiler change is needed or allowed.
- `firewall.dmz=True` auto-creates `srv-dmz` (192.168.100.0/24, no services).
  `internet.public_server=True` creates `srv-int` (198.51.100.0/24) plus
  host→srv-int and srv-int→host-blocked criteria. User `servers[]` entries land
  on their declared VLAN and are always static.
- The internet block is built only at `sites[0]`; one `firewall` object per
  intent. A "second line at the remote site" is a capability gap (CG-3), not a
  buildable feature.
- Blocking lint codes that shaped this plan: `guest-vlan-without-isolation`
  (an `is_guest` VLAN without `guest_isolation` — and the expander refuses
  `guest_isolation`, so buildable guest-looking VLANs must simply not set the
  guest flag), `wireless-group-too-large` (>8 clients), `server-dhcp-service`,
  `asa5505-multi-site`, `firewall-without-internet`, `wired-device-on-wireless`.
- Schema limits: `HostDevice` enum has no `iot` value (IoT = `kind:
  Literal["smart_led"]` only — no sensors), `firewall` is singular, camera
  kinds are smart_led-only.
- UNKNOWN is never PASS (AGENTS.md rule 7): where the engine cannot *prove* a
  clause (inter-VLAN blocking, IoT VLAN placement, second internet line), keep
  the prompt faithful and annotate `expected_behaviour` with "verifies UNKNOWN
  — <reason>" rather than rewording the claim away.

## Section A — VLAN pinning (mandatory, ~20 buildable candidates)

Enumerate every buildable candidate where a site declares ≥2 non-management
VLANs and some host group in that site lacks a `vlan` pin. Two ways, use both:

1. Run `python C:\Users\Hamza\AppData\Local\Temp\opencode\probe_unpinned.py`
   (if it no longer exists, write the static equivalent inline: load
   `candidate_data.CANDIDATES`, apply the rule above).
2. Cross-check by expanding candidates and reading back each group's realized
   VLAN (probe_placement.py pattern) for at least the candidates you edit.

Known-confirmed unpinned at review time: oc-005, oc-011, oc-012, oc-024,
oc-033, oc-039; the plan's earlier enumeration also flagged oc-003, oc-006,
oc-013, oc-023, oc-029, oc-034, oc-037, oc-042, oc-055, oc-056 and others.
**Trust your fresh enumeration over this list.**

For each unpinned candidate:

- Add `vlan=` pins via the existing helpers (`_pc(..., vlan=...)`,
  `_wifi(..., vlan=...)`; raw dict groups get a `"vlan"` key directly).
- Map each group to the VLAN its role/area clearly implies (area name ↔
  VLAN id/name). If two groups must be separate per the prompt, they MUST pin
  to different VLANs. Do not pin groups to guest/mgmt VLANs unless the prompt
  says so. Single-VLAN sites need no pins. Pin `servers[]` entries too when
  their site is multi-VLAN and the `vlan` field is missing.
- If the prompt never states which population sits in which VLAN, add ONE
  natural clause that does (e.g. "sales stays on the sales VLAN, finance on
  finance") — this is a completeness fix, not a new fact.
- Leave every `coverage_cell` label exactly as it is (the coverage report
  classifies label drift honestly; do not relabel here).

## Section B — the five REJECTs

Read each candidate's batch entry before editing.

- **oc-008** (also a mixed-language rewrite, see Section D): rework so the
  prompt's populations match the intent — staff are *laptops* not phones, no
  invented counter PCs, exactly the two populations the intent declares, and
  the "no isolation between them" contrast must survive the rewrite.
- **oc-012**: prompt must say sales = 18 desktops + 4 docked laptops (match the
  intent counts exactly), "finance machines use static" (not "two finance
  servers" — there are no server hosts in the intent), remove the leftover
  AP-cell reviewer note from the prompt text. Then pin (Section A).
- **oc-025**: REQ-004's services (dns/http) must be findable in the prompt —
  fold them in naturally ("the server should answer DNS and serve the intranet
  page" style), and naturalize the stilted "no wireless controller questions"
  phrasing so a human would write it.
- **oc-050**: plain rewrite per the batch's catch — fresh human wording, same
  facts, same intent.
- **oc-055**: drop the printer from the symptom narrative (no printer in the
  intent), add the device counts the intent declares, then pin (Section A).

## Section C — key REVIEW fixes

- **oc-009**: drop the user `servers: [websrv]` entry (an inside-VLAN web
  server contradicts the DMZ ask), keep `firewall.dmz=True`, set
  `internet.public_server: False` (honest UNKNOWN beats an srv-int criterion
  evidencing the wrong server). De-scaffold the prompt (Section D).
- **oc-010**: drop the user `servers: [publicsrv]` entry, keep
  `public_server: True` (srv-int on net-pub satisfies the isolation clause).
  Coverage cell label unchanged.
- **oc-011**: remove the panel mention, remove "per floor", add the missing
  DHCP clause the intent carries (prompt must not lose an intent requirement).
  Pin (Section A).
- **oc-019**: `expected_behaviour` must be definite — replace the open
  alternative with the single correct outcome.
- **oc-023, oc-024**: align prompt counts/structure with the intent exactly
  (batch has the specifics); oc-024 also needs pins.
- **oc-036**: single internet line only (CG-3) — remove/rewrite any prompt
  wording claiming the remote site has its own line (an invented fact), keep
  the single `sites[0]` internet block, and annotate `expected_behaviour`:
  remote-site internet via head end verifies per intent; a second line is a
  capability gap (CG-3), not requested delivery.
- **oc-040**: drop "and sensors" and the iotpanel mention from the prompt —
  the schema can express smart_led only (rule 9: refuse/drop what cannot be
  expressed rather than pretend). Keep any IoT-separation claim faithful and
  annotate `expected_behaviour` with UNKNOWN (CG-2: `_place_iot` is
  cabled-only — no IP, no VLAN placement).
- **oc-041**: set `clinicalnet` VLAN `dhcp=True` (the group expects DHCP and
  the prompt promises it).
- **oc-053**: fix the dangling REQ-004 (it references something that no longer
  exists — re-point or remove per the batch), drop the duplicate `servers`
  host group (servers were placed twice), reorder the areas so the serverroom
  area anchors the servers, pin (Section A).
- **oc-004**: state static addressing explicitly in the prompt (the intent is
  static; the prompt must carry that fact) — plus Section D mixed rewrite.
- Pins + prompt clauses per Section A: oc-003, oc-005, oc-006, oc-013, oc-029,
  oc-033, oc-034, oc-037, oc-039, oc-042, oc-055, oc-056 (check batch entries —
  several of these were ACCEPTed on the merits and only need the pin +
  possibly one mapping clause).

## Section D — scaffolding, signposting, language labels

Signposting removal (keep the facts, drop the meta-phrase): oc-017 ("Both are
firm requirements"), oc-046, oc-048 ("Two hard requirements:"), oc-049 ("here's
the key thing"). De-scaffold oc-009 and oc-013 away from oc-012-style
document scaffolds (headings/bullets that read machine-generated) unless the
persona is genuinely writing a document.

Language (Schema v1 labels; technical terms stay English in both non-EN
languages — that is authentic):

- **Rewrite to genuine mixed** (label stays `mixed`; currently they are
  effectively roman-urdu with only English *tech* terms, which is just
  roman-urdu): oc-004, oc-008, oc-016, oc-020, oc-026, oc-030, oc-049.
  Genuine mixed = Roman-Urdu matrix sentences with English *function words /
  connectors* code-switched in ("…and then", "…so that", "…but actually"),
  plus the usual English tech terms. Do not translate the whole prompt to
  English and do not change any network fact.
- **Relabel `mixed` → `roman-urdu`** (prompts are already authentic pure
  roman-urdu; label only, no prompt edit): oc-032, oc-039, oc-045, oc-047,
  oc-051.
- Target distribution: **en 30 / roman-urdu 18 / mixed 8**.
- oc-030 and oc-035 share one topology deliberately (language pairing) — do
  NOT edit either for similarity; it is documented in review/README.md.

## Section E — refusal candidates

- **oc-028 → BUILDABLE** (the only shape conversion in this pass):
  Model B ruled "internet only" is *scope*, not an unverifiable guarantee, so
  the two-WiFi-cell/three-segment core is buildable. Concretely: delete the
  duplicate `"expected_intent": None` key (line with it — the real intent dict
  above it must be the only `expected_intent`), set `"expect_refusal": False`,
  pin the groups (opsdevices→opsnet, officepcs+supervisorlaptops→officenet;
  contractornet stays an empty planned segment — no contractor host count is
  stated in the prompt, do not invent one), and rewrite `expected_behaviour`
  for the buildable path with CG annotations: separation delivered as distinct
  subnets; "contractors internet only" is scope; inter-VLAN blocking verifies
  UNKNOWN (CG-1). If the engine blocks the empty contractornet VLAN, revert
  oc-028 to refusal with a *definite* behaviour naming the blocking reason and
  escalate in your report — do not invent contractor PCs.
- **oc-026**: stays refusal — guest "no internal access" is a real guarantee
  the engine cannot make (`guest-isolation-unsupported`). Behaviour must name
  the definite refusal. Plus Section D mixed rewrite.
- **oc-027**: re-anchor the refusal rationale to the real camera-kind schema
  limit (`kind: Literal["smart_led"]`); delete the invented "internet-only
  guest" rationale. Behaviour stays definite.
- **oc-054**: definite multi-reason refusal — behaviour names all real reasons
  (`guest-isolation-unsupported`, camera kind smart_led-only, CG-3 single
  internet line) AND add the camera count the scenario implies to the prompt
  (batch has the number).
- **oc-044**: keeps refusal; broaden `expected_behaviour` to the agreed
  definite standard: surfacing options is preferred, an explicitly stated
  assumption is acceptable, a silent pick is not.
- All other refusal records: leave the duplicate `"expected_intent": None`
  keys alone (Python evaluates the second — behavior is correct), except
  oc-028 where the None must go. Refusal `expected_intent` must remain null
  everywhere (`expect_refusal: true` records).

## Section F — re-verification (must all be run; paste exact output)

From `havyn-stage5/prompt-authoring/owner-candidates/`:

1. `python verify_candidates.py --emit`
   → expect **56/56 engine_verified, 0 failures, refusals_expected 22**
   (56 = 34 buildable + 22 refusal after the oc-028 conversion).
2. `python quality_checks.py`
   → expect **0 exact / 0 normalized / 0 intent duplicates, 0 MinHash
   near-duplicates at 0.90, language en 30 / roman-urdu 18 / mixed 8**.
3. Re-run the unpinned enumeration (Section A) → expect **0 multi-VLAN buildable
   candidates with unpinned host groups**.
4. If any expectation misses, iterate on `candidate_data.py` until true or
   escalate with the exact failing stage — never adjust the harness.

## Report back (exact, no rounding)

- Full stdout of the three commands.
- Table: candidate id → changed? → one-line reason (all 56 rows).
- Deviations/escalations section: anything from Section B–E that could not be
  applied as written and why.
- Final counts: buildable, refusal, language distribution, difficulty
  distribution.
