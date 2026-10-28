# Capability-gap register (Stage 5)

Scope: gaps between what a real-world prompt asks for and what the Havyn
engine can *build* or *verify* today. Research and public sources are
advisory; **the engine is the source of truth** (AGENTS.md rule 7/9/10):

- A clause the engine cannot verify is kept faithful in the prompt and marked
  `verifies UNKNOWN` in `expected_behaviour` — never silently reworded away,
  never counted as PASS.
- A request the engine refuses outright becomes a named-refusal candidate
  (`expect_refusal: true`, null intent) citing the *real* refusal code.
- Where Schema v1 cannot even express the thing ("and sensors"), the
  inexpressible fragment is dropped from the prompt (rule 9) and noted here.

Evidence lines are from engine commit `06c57ced050cda532915c15b1ce772b0a0eaf607`.
IDs follow the planning register (CG-1..CG-7, CG-9, CG-10).

---

## CG-1 — No inter-VLAN blocking capability

- **Evidence**: the only ACL the expander emits is `acl-perimeter` on the
  firewall (`src/network_autopilot/ai/expander.py:764-783`, inside-may-ping-out
  + echo-reply on PT's stateless ASA). Multi-VLAN sites are built as
  router-on-a-stick with **no inter-VLAN ACL anywhere** — separate subnets
  route to each other freely.
- **Effect**: "X must not reach Y" in a multi-VLAN prompt is *delivered* only
  as separate subnets; active blocking between VLANs is not configured or
  verified.
- **Disposition**: prompt and requirement stay faithful (isolation intent is
  the user's real requirement); `expected_behaviour` states the separation
  clause *verifies UNKNOWN — no inter-VLAN blocking capability*.
- **Closed by**: an inter-VLAN ACL emission + verification path in the
  expander/verifier.

## CG-2 — IoT devices are cabled-only (no IP, no VLAN placement)

- **Evidence**: `expander.py:605-618` (`_place_iot`) — devices are created and
  linked to an anchor; the emitted assumption says so verbatim. There is no IP
  assignment and no VLAN pinning for IoT; `HostDevice` has no `iot` value and
  `IotThing.kind` is `Literal["smart_led"]` (`ai/intent.py:299`).
- **Effect**: smart lights share the site's default VLAN (typically with staff
  traffic); "lights are on their own segment" verifies UNKNOWN (CG-1 also
  applies). "Sensors" cannot be expressed at all.
- **Disposition**: IoT separation claims stay faithful + UNKNOWN-annotated;
  the inexpressible "and sensors" fragment is dropped from prompts (rule 9).
- **Closed by**: an IoT placement path (IP/VLAN) and a wider device-kind enum.

## CG-3 — One internet line, built only at `sites[0]`

- **Evidence**: `expander.py:622-631` — `_build_internet` takes
  `first_site = self.intent.sites[0]` and builds exactly one WAN block (direct
  `203.0.113.0/30` or behind-firewall shape). The multi-site WAN is a star from
  `sites[0]` (`expander.py:787-794`).
- **Effect**: a prompt claiming "the remote site has its own line" over-claims:
  remote sites reach the internet *through* site 1 only. One `firewall` object
  per intent, so per-site firewalls/lines are not representable.
- **Disposition**: remove the invented second-line claim from prompts (it was
  never in the intent); requests that genuinely need per-site lines are named
  refusals or carry a CG-3 note in `expected_behaviour`.
- **Closed by**: per-site internet blocks (or a multi-line model) in intent +
  expander.

## CG-4 — Guest isolation cannot be delivered

- **Evidence**: `expander.py:298-300` refuses `guest-isolation-unsupported`
  when `guest_isolation` is requested; `lint.py:242` blocks
  `guest-vlan-without-isolation` when a VLAN sets `is_guest` without it — the
  two close each other, so **no buildable intent can honestly carry a guest
  flag plus a "guests cannot reach internal X" guarantee**.
- **Effect**: "guest Wi-Fi with no access to internal systems" is either a
  named refusal (guest flag + guarantee) or must be phrased as segment/scope,
  not as an enforced block.
- **Disposition**: refusal candidates name `guest-isolation-unsupported`;
  buildable guest-style VLANs omit the guest flag (engine-sanctioned) and make
  no internal-block guarantee.
- **Closed by**: a verified guest-isolation emission path.

## CG-5 — Server-side DHCP service unsupported

- **Evidence**: `lint.py:262-269` blocks `server-dhcp-service`: a requested
  `dhcp` service on a Server-PT; the verified path is per-VLAN pools on the
  gateway.
- **Effect**: prompts asking a server to *be* the DHCP server refuse by name.
- **Disposition**: such candidates are `expect_refusal` citing
  `server-dhcp-service`; LAN DHCP prompts use VLAN `dhcp: true` pools instead.
- **Closed by**: an intent+config path for Server-PT DHCP pools.

## CG-6 — ASA 5505 zone/multi-site limits

- **Evidence**:
  - `expander.py:668-672` — `asa5505-no-dmz`: the 5505 has no third zone; DMZ
    needs the 5506-X.
  - `lint.py:224-231` — `asa5505-multi-site` blocks a 5505 in multi-site
    intents.
  - `expander.py:306-308` — `dmz-multi-site-unsupported`.
  - `expander.py:674-680` — `firewall-needs-single-vlan`: the ASA is the inside
    gateway (T07) and needs exactly one VLAN at the internet-facing site.
- **Effect**: "ASA 5505 with DMZ", "ASA across two sites", "DMZ + multi-site",
  "firewall on a multi-VLAN site" are all unrepresentable shapes.
- **Disposition**: named-refusal candidates citing the exact code; prompts are
  not weakened to dodge the refusal — the refusal *is* the example.
- **Closed by**: zone/throughput modelling for the 5505, multi-firewall
  intents, and a multi-VLAN firewall shape.

## CG-7 — Camera/IoT kind is `smart_led` only

- **Evidence**: `ai/intent.py:299` — `kind: Literal["smart_led"] = "smart_led"`.
- **Effect**: "IP cameras", "NVR", "sensors", "smart locks" etc. cannot be
  expressed; only smart LEDs exist as IoT things.
- **Disposition**: prompts keep only expressible kinds (inexpressible fragments
  dropped, rule 9); camera-specific asks become named refusals anchored to the
  schema limit (not to an invented policy).
- **Closed by**: a wider IoT kind enum with per-kind engine support.

## CG-9 — Wireless cells verified up to 8 clients

- **Evidence**: `lint.py:141-151` — `wireless-group-too-large` blocks any
  wireless host group with `count > 8`: "Packet Tracer radio cells are verified
  up to 8 clients; split the group into smaller cells."
- **Effect**: prompts with a 9+ client AP cell refuse by name; realistic large
  offices must be modelled as multiple cells.
- **Disposition**: refusal candidates cite `wireless-group-too-large`; buildable
  prompts keep ≤8 clients per cell and, where realism demands more devices,
  use several cells/areas.
- **Closed by**: multi-cell wireless modelling with per-cell verification.

## CG-10 — Fixed VLAN CIDRs must sit inside the site block

- **Evidence**: `expander.py:367-374` — `vlan-outside-site-block`: expander v1
  allocates site `N` from `10.N.0.0/16`; a VLAN with an explicit `cidr` outside
  that block refuses.
- **Effect**: prompts that dictate arbitrary addressing (e.g. "servers on
  192.168.50.0/24 at site 1") cannot be honoured as stated.
- **Disposition**: named refusal or the addressing constraint is expressed
  inside the site block (`required_hosts`, not a conflicting `cidr`).
- **Closed by**: address-planner support for user-fixed subnets per site.

---

## Affected-candidate index

Filled after the fix pass from the emitted `candidates/*.json`, so this section
reflects the shipped state, not the plan. Refusal-code source lines are verified
in `refusal_source_verification.md`.

### A. Explicit CG / UNKNOWN annotations shipped in `expected_behaviour`

| Candidate | Shape | CG cited | Clause marked UNKNOWN |
| --- | --- | --- | --- |
| oc-009 | buildable | — | "outside visitors reach the website" (auto-created `srv-dmz`, no per-server service evidence) |
| oc-011 | buildable | CG-2 | IoT "not mixed with staff PCs" (IoT has no IP/VLAN placement) |
| oc-028 | buildable | CG-1, CG-3 | inter-VLAN blocking ops/office/contractors; "contractors internet only"; one internet line |
| oc-036 | buildable | CG-3 | remote-site second internet line (one line built at `sites[0]`) |
| oc-040 | buildable | CG-2 | IoT "on its own network, separate from staff PCs" |
| oc-054 | refusal | CG-3 | (in the refusal) remote-site second line named as a gap |

### B. CG-1 applies but the isolation clause is not machine-verified

`isolation` / `security` requirements become `RequirementSpec(kind="isolation")`
(`ai/expander.py:933`), but the expander emits a blocking criterion **only** for
the firewall + public-server edge (`ai/expander.py:1054-1064`). Every other
inter-VLAN isolation requirement has *no criterion*, so acceptance reports it
`NOT_RUN` and `satisfied=None` (`acceptance/evaluator.py:328-346`) — never PASS.
These buildable candidates deliver separation as distinct subnets but do **not**
machine-verify the blocking clause; prompts stay faithful, per the CG-1
disposition:

oc-003, oc-005, oc-010, oc-012, oc-013, oc-024, oc-025, oc-033, oc-034,
oc-037, oc-039, oc-041, oc-042, oc-053, oc-056

(oc-009 and oc-011 carry the same limitation plus an explicit UNKNOWN note in
Table A; oc-010's perimeter shape attaches the one `srv-int → host` blocked
criterion the engine does verify.)

### C. Refusal candidates by capability gap

| CG | Refusal code (source-verified) | Candidates |
| --- | --- | --- |
| CG-4 | `guest-isolation-unsupported` | oc-007, oc-017, oc-026, oc-049, oc-054 |
| CG-5 | `server-dhcp-service` | oc-020, oc-052 |
| CG-6 | `asa5505-multi-site`, `asa5505-no-dmz`, `dmz-multi-site-unsupported`, `firewall-needs-single-vlan` | oc-019, oc-021, oc-048, oc-051 |
| CG-7 | schema: `kind: Literal["smart_led"]` only | oc-027, oc-054 |
| CG-9 | `wireless-group-too-large` | oc-050 |
| CG-10 | `vlan-outside-site-block` | oc-052 |
| CG-3 | one internet block at `sites[0]` | oc-054 |
| — | ambiguous (clarify-don't-invent, no engine code) | oc-014, oc-015, oc-016, oc-043, oc-044, oc-045 |
| — | contradictory (surface-the-conflict, no engine code) | oc-017, oc-018, oc-046, oc-047, oc-048, oc-049 |

Counts reconcile with `candidates/verification-report.json`: 34 buildable + 22
refusal = 56, and 22 refusals in Table C.
