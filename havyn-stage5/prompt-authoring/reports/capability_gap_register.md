# Capability-gap register (Stage 5)

Scope: gaps between what a real-world prompt asks for and what the Havyn engine
can *build* or *verify* today. Research and public sources are advisory; **the
engine is the source of truth** (AGENTS.md rules 7/9/10).

## Evidence classes (read before quoting anything here)

| Class | Meaning in this register |
| --- | --- |
| **Engine-executed (offline)** | A minimal triggering intent was run through schema → intent lint → expander (and, for buildable candidates, through spec lint → IPAM → compile). No Packet Tracer. Produced by `havyn-stage5/scripts/probe_capability_gaps.py`. |
| **Source-traced** | The code string/condition exists at the cited line and is raised on the described condition. This proves the capability claim, not that a specific candidate's wire-input reaches it. |
| **Expected refusal** | A candidate marked `expect_refusal: true`. `verify_candidates.py` only asserts `expected_intent is null`; it does not execute a refusal trigger for that candidate. |
| **Unsupported capability** | No expressible shape exists (schema `Literal`, singular field, or expander shape limit). |
| **Unknown / not verified** | The engine cannot prove the clause: no acceptance criterion is emitted, so acceptance reports `NOT_RUN` / `satisfied=None`, never PASS. |
| **Live Packet Tracer** | **Not used for any claim in this register.** No live PT run backs this document. |

### Source state and reproducibility (honest limitation)

- Source lines were traced in the **working tree** at engine commit
  `e8503cdfb32a83817a2fbc84ad4245057acf75a2` (HEAD) plus the pre-existing,
  uncommitted Stage 3/4 engine changes.
- `src/network_autopilot/ai/` (the intent schema, lint and expander) was
  **untracked at `e8503cd`**. It is Stage-5-only code (docstrings `S5.1`/`S5.2`;
  only Stage-5 consumers import it) and is committed in the follow-up Stage-5
  commit. It is **not** part of the Stage 3/4 work.
- A clean `e8503cd` tree with only `ai/` restored verifies **48/56**
  candidates. The 8 failures — `oc-012, oc-013, oc-023, oc-025, oc-041,
  oc-053, oc-055, oc-056` — fail at compile because Server-PT services are not
  in the Stage 3 compiler at `e8503cd`. The **56/56** result is only reproducible
  with the uncommitted Stage 3/4 compiler changes, which this Stage-5 work is
  forbidden to commit. See `stage5_handoff.md` for the blocker.

### Engine-executed probe results

`havyn-stage5/scripts/probe_capability_gaps.py` ran 14 probes; **14/14 matched**
(`capability-gap-probes.json`). Those probes are offline engine runs.
They do not replace live Packet Tracer verification, and they do not execute a
candidate's exact wire-input.

---

## CG-1 — No inter-VLAN blocking capability

- **Source**: the only ACL the expander emits is `acl-perimeter` on the firewall
  (`src/network_autopilot/ai/expander.py:765-786`, inside-initiated ICMP plus
  echo-reply on PT's stateless ASA). Multi-VLAN sites are router-on-a-stick with
  **no inter-VLAN ACL**. Isolation requirements map to `RequirementSpec(kind=
  "isolation")` (`expander.py:940-961`), but a blocking criterion is emitted
  only for the firewall + public-server edge (`expander.py:1055-1067`). A
  requirement with no criterion is `NOT_RUN`, never satisfied
  (`acceptance/evaluator.py:328-330`).
- **Reachability is not a substitute**: generic flow/link/association checks
  are attached to the engine's own `R-ENG-001` requirement
  (`expander.py:938, 940-981`), not to a user isolation or internet requirement.
  A reachable flow can therefore never be reported as proof of isolation.
- **Engine-executed probe**: `cg1-no-inter-vlan-acl` on a two-VLAN site →
  `acl_policy_count = 0`, `blocked_criteria = 0`; expanding the isolation
  fixtures shows zero criteria attached to the isolation requirement.
- **Effect**: "X must not reach Y" in a multi-VLAN prompt is *delivered* only as
  separate subnets; active blocking is neither configured nor verified.
- **Disposition**: prompt and requirement stay faithful; `expected_behaviour`
  states the separation clause verifies UNKNOWN.
- **Closed by**: an inter-VLAN ACL emission + verification path.

## CG-2 — IoT devices are cabled-only (no IP, no VLAN placement)

- **Source**: `expander.py:605-619` (`_place_iot`) creates and links devices to
  an anchor; it emits no IP assignment and no VLAN pin. `IotIntent.kind` is
  `Literal["smart_led"]` (`ai/intent.py:299`).
- **Engine-executed probe**: `cg2-iot-no-ip-placement` → 2 IoT devices created,
  `iot_ip_assignments = []`.
- **Effect**: smart lights are not placed on a separately verified segment;
  "lights are on their own network" verifies UNKNOWN. Cameras/sensors cannot be
  expressed at all.
- **Disposition**: IoT separation claims stay faithful + UNKNOWN-annotated;
  inexpressible fragments are dropped from prompts (rule 9).
- **Closed by**: an IoT placement path (IP/VLAN) and a wider device-kind enum.

## CG-3 — One internet line, built only at `sites[0]`

- **Source**: `expander.py:622-631` (`_build_internet` takes
  `first_site = self.intent.sites[0]`); multi-site is a WAN star from
  `sites[0]` (`expander.py:787`).
- **Engine-executed probe**: `cg3-one-internet-block` on a two-site intent →
  exactly one `net-wan` subnet, assignments `s1-r1 = 203.0.113.1` and
  `isp = 203.0.113.2`, one `isp` device.
- **Effect**: a prompt claiming the remote site has its own line over-claims;
  remote sites reach the internet through site 1 only. One `firewall` object per
  intent, so per-site firewalls/lines are not representable.
- **Disposition**: remove invented second-line claims from prompts; requests
  that genuinely need per-site lines are refusals or carry a CG-3 note.
- **Closed by**: per-site internet blocks (or a multi-line model).

## CG-4 — Guest isolation cannot be delivered

- **Source**: `expander.py:297-303` refuses `guest-isolation-unsupported` when
  `firewall.enabled` and `firewall.guest_isolation`; `ai/lint.py:236-247`
  blocks `guest-vlan-without-isolation` when an `is_guest` VLAN lacks the flag.
- **Engine-executed probes**: `guest-isolation` → expander refusal
  `guest-isolation-unsupported`; `guest-vlan-without-isolation` → intent-lint
  finding with the same code family.
- **Effect**: no buildable intent can honestly carry a guest flag plus a
  "guests cannot reach internal X" guarantee. It is a named refusal or a
  segment/scope statement, not an enforced block.
- **Disposition**: refusal candidates name `guest-isolation-unsupported`;
  buildable guest-style VLANs omit the guest flag and make no internal-block
  guarantee.
- **Closed by**: a verified guest-isolation emission path.

## CG-5 — Server-side DHCP service unsupported

- **Source**: `ai/lint.py:262-269` blocks `server-dhcp-service`; the verified
  path is per-VLAN pools on the gateway.
- **Engine-executed probe**: `server-dhcp-service` → intent-lint finding.
- **Effect**: prompts asking a server to *be* the DHCP server refuse by name.
- **Disposition**: `expect_refusal` with `server-dhcp-service`; LAN DHCP prompts
  use VLAN `dhcp: true` pools.
- **Closed by**: an intent + config path for Server-PT DHCP pools.

## CG-6 — ASA 5505 zone/multi-site limits

- **Source**:
  - `expander.py:306-312` — `dmz-multi-site-unsupported`.
  - `expander.py:668-672` — `asa5505-no-dmz` (no third zone).
  - `expander.py:673-679` — `firewall-needs-single-vlan` (ASA is the inside
    gateway).
  - `ai/lint.py:225-233` — `asa5505-multi-site`.
- **Engine-executed probes**: `dmz-multi-site`, `asa5505-no-dmz`,
  `firewall-needs-single-vlan`, `asa5505-multi-site` → all 4 matched with the
  named code at the expected stage.
- **Effect**: "5505 with DMZ", "5505 across two sites", "DMZ + multi-site",
  "firewall on a multi-VLAN site" are unrepresentable.
- **Disposition**: named-refusal candidates citing the exact code; prompts are
  not weakened to dodge the refusal.
- **Closed by**: zone/multi-site modelling, multi-firewall intents, and a
  multi-VLAN firewall shape.

## CG-7 — Camera/IoT kind and internet access shape are closed enums

- **Source**: `ai/intent.py:299` — `kind: Literal["smart_led"]`;
  `ai/intent.py:237` — `access: Literal["wan", "dsl", "cable"]`.
- **Engine-executed probes**: `iot-camera-kind` and `bonded-access` →
  `ValidationError` at schema stage, with the enum error naming the rejected
  value.
- **Effect**: IP cameras/NVR/sensors and bonded DSL+cable links cannot be
  expressed.
- **Disposition**: prompts keep only expressible kinds; camera and bonding asks
  become named refusals anchored to the schema limit.
- **Closed by**: a wider IoT kind enum and a multi-link access model.

## CG-9 — Wireless cells verified up to 8 clients

- **Source**: `ai/lint.py:141-151` — `wireless-group-too-large` for a wireless
  host group with `count > 8`.
- **Engine-executed probe**: `wireless-group-too-large` → intent-lint finding
  for 9 laptops.
- **Effect**: prompts with a 9+ client AP cell refuse by name; large offices use
  several cells/areas.
- **Disposition**: refusal candidates cite the code; buildable prompts keep ≤8
  clients per cell.
- **Closed by**: multi-cell wireless modelling with per-cell verification.

## CG-10 — Fixed VLAN CIDRs must sit inside the site block

- **Source**: `expander.py:367-374` — `vlan-outside-site-block`; expander v1
  allocates site N from `10.N.0.0/16`.
- **Engine-executed probe**: `vlan-outside-site-block` → expander refusal for
  `192.168.50.0/24` at site 1 (`10.1.0.0/16`).
- **Effect**: prompts that dictate arbitrary addressing cannot be honoured as
  stated.
- **Disposition**: named refusal or express the constraint via
  `required_hosts` inside the block.
- **Closed by**: user-fixed subnets per site in the address planner.

*(CG-8 is not assigned in the planning register; numbering is preserved.)*

---

## Affected-candidate index

Computed from the emitted `candidates/*.json` and the expansion check. This is
the shipped state, not the plan.

### A. Explicit CG / UNKNOWN annotations in `expected_behaviour`

| Candidate | Shape | CG cited | Clause marked UNKNOWN |
| --- | --- | --- | --- |
| oc-004 | buildable | CG-3 | (factual note: one WAN block at the head office; no UNKNOWN clause) |
| oc-009 | buildable | — | "outside visitors reach the website" (auto-created `srv-dmz`, no per-server service evidence) |
| oc-011 | buildable | CG-2 | IoT "not mixed with staff PCs" (no IP/VLAN placement) |
| oc-028 | buildable | CG-1, CG-3 | inter-VLAN blocking; "contractors internet only"; one internet line |
| oc-036 | buildable | CG-3 | remote-site second line (one line built at `sites[0]`) |
| oc-040 | buildable | CG-2 | IoT "on its own network, separate from tills" |
| oc-054 | refusal | CG-1, CG-3 | (in the refusal) POS/guest separation and the store's second line |

### B. CG-1 applies: isolation/security requirement has no blocked criterion

Computed by expanding each buildable candidate and checking whether every
`isolation`/`security` requirement received a criterion with
`test.expect == "blocked"`. These 15 have at least one such requirement with no
blocked criterion and no explicit Table-A note:

`oc-003, oc-005, oc-010, oc-012, oc-013, oc-024, oc-025, oc-033, oc-034,
oc-037, oc-039, oc-041, oc-042, oc-053, oc-056`

`oc-009, oc-011, oc-028, oc-040` have the same limitation plus an explicit
UNKNOWN note in Table A. `oc-010`'s perimeter shape does attach the one
`srv-int → host` blocked criterion the engine verifies, but its VLAN-separation
requirement still has none.

### C. Refusal / limitation candidates by evidence basis

Distinct refusal candidates: **22**. Rows overlap because one candidate can cite
more than one gap; the union is 22.

| Basis | Code / limit | Candidates |
| --- | --- | --- |
| CG-4 (engine) | `guest-isolation-unsupported` | oc-007, oc-026, oc-049, oc-054 |
| CG-5 (engine) | `server-dhcp-service` | oc-020, oc-052 |
| CG-6 (engine) | `dmz-multi-site-unsupported`, `asa5505-no-dmz`, `firewall-needs-single-vlan`, `asa5505-multi-site` | oc-019, oc-021, oc-051 |
| CG-7 (schema) | `kind: Literal["smart_led"]` only | oc-027, oc-054 |
| CG-7 (schema) | `access: Literal["wan","dsl","cable"]` only (no bonding) | oc-048 |
| CG-9 (engine) | `wireless-group-too-large` | oc-050 |
| CG-10 (engine) | `vlan-outside-site-block` | oc-052 |
| CG-3 (shape) | one internet block at `sites[0]` | oc-054 |
| Ambiguous (clarify) | no refusal code; oc-016 cites the access enum as why it must ask | oc-014, oc-015, oc-016, oc-043, oc-044, oc-045 |
| Contradictory (surface) | no refusal code; oc-017 overlaps CG-4, oc-048 overlaps CG-7, oc-049 overlaps CG-4 | oc-017, oc-018, oc-046, oc-047, oc-048, oc-049 |

### D. Count reconciliation

- Buildable candidates: **34** (all pass the full chain in the working tree).
- Refusal candidates: **22** (all have `expected_intent: null`; their named
  codes are engine-executed at the capability level or schema-rejected, but the
  candidate's own wire-input is not executed).
- 34 + 22 = **56**.
- `verify_candidates.py` reports **56/56 engine_verified, 0 failures,
  22 refusals_expected** in the working tree. The clean-checkout limitation is
  in the source-state section above.

## Reproduce

```bash
python havyn-stage5/scripts/probe_capability_gaps.py
python havyn-stage5/prompt-authoring/owner-candidates/verify_candidates.py --quiet
python havyn-stage5/prompt-authoring/owner-candidates/audit_unpinned.py
```

The first command writes `capability-gap-probes.json`; the second is the
candidate-level engine check; the third is the structural VLAN-pin audit.
