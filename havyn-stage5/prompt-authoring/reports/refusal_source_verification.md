# Refusal source verification (Stage 5 prompt authoring)

Scope: every named refusal/limitation asserted by an `expect_refusal: true`
candidate in `../owner-candidates/`, traced to the engine site that produces it
and, where possible, executed with a minimal triggering intent.

The rule: a refusal must be a **real** engine behaviour (AGENTS.md rule 9), not a
reviewer's invention.

## Evidence classes

| Class | What was run | What it proves |
| --- | --- | --- |
| **Capability probe (engine-executed, offline)** | `havyn-stage5/scripts/probe_capability_gaps.py` builds a minimal triggering intent, runs schema → intent lint → expander, and records the stage + code. | The engine really refuses this shape / rejects this schema value. No Packet Tracer, no candidate wire-input. |
| **Source trace** | The code string and its guard condition at the cited line. | The claim exists in the source; it is consistent with the probe. |
| **Candidate check** | `verify_candidates.py` for `expect_refusal: true` only asserts `expected_intent is null`. | The candidate is shaped as a refusal. It does **not** execute the candidate's exact trigger. |

## Verification gap (rule 10 — stated, not hidden)

- **Proven:** each named capability gap fires for a minimal triggering intent
  (14/14 probes matched; `capability-gap-probes.json`).
- **Not proven:** that a specific candidate's exact wire-input routes to that
  code at runtime. That needs a model to emit the triggering shape; the
  candidate's `refused_intent_shape` is illustrative and is not expanded.
- **Not proven:** anything against live Packet Tracer. No live PT run backs
  this document.
- **Buildable half:** every buildable `expected_intent` is executed end-to-end
  through schema → lint → expand → spec-lint → IPAM → compile in the working
  tree (56/56). The clean-checkout limitation is recorded in the capability
  register.

## Capability probes (exact observed results)

| Probe | Gap | Expected code | Stage observed | Result |
| --- | --- | --- | --- | --- |
| `guest-isolation` | CG-4 | `guest-isolation-unsupported` | expander | matched |
| `guest-vlan-without-isolation` | CG-4 | `guest-vlan-without-isolation` | intent-lint | matched |
| `dmz-multi-site` | CG-6 | `dmz-multi-site-unsupported` | expander | matched |
| `asa5505-no-dmz` | CG-6 | `asa5505-no-dmz` | expander | matched |
| `firewall-needs-single-vlan` | CG-6 | `firewall-needs-single-vlan` | expander | matched |
| `asa5505-multi-site` | CG-6 | `asa5505-multi-site` | intent-lint | matched |
| `vlan-outside-site-block` | CG-10 | `vlan-outside-site-block` | expander | matched |
| `server-dhcp-service` | CG-5 | `server-dhcp-service` | intent-lint | matched |
| `wireless-group-too-large` | CG-9 | `wireless-group-too-large` | intent-lint | matched |
| `iot-camera-kind` | CG-7 | `ValidationError` | schema | matched |
| `bonded-access` | CG-7 | `ValidationError` | schema | matched |
| `cg1-no-inter-vlan-acl` | CG-1 | observation: 0 ACL policies, 0 blocked criteria | expander-success | matched |
| `cg2-iot-no-ip-placement` | CG-2 | observation: 2 IoT devices, 0 IP assignments | expander-success | matched |
| `cg3-one-internet-block` | CG-3 | observation: one `net-wan`, `s1-r1` + `isp` | expander-success | matched |

## Claim → engine site

| # | Refusal code / limit | Engine site | Raised when | Candidate citations |
| --- | --- | --- | --- | --- |
| 1 | `guest-isolation-unsupported` | `src/network_autopilot/ai/expander.py:299` (`_refuse_unsupported`) | `firewall.enabled` and `firewall.guest_isolation` | oc-007, oc-026, oc-049, oc-054 |
| 2 | `dmz-multi-site-unsupported` | `expander.py:307` (`_refuse_unsupported`) | `firewall.dmz` with `len(sites) > 1` | oc-019 |
| 3 | `asa5505-no-dmz` | `expander.py:670` (`_build_internet_behind_firewall`) | `firewall.model == "asa5505"` and `firewall.dmz` | oc-019, oc-051 |
| 4 | `firewall-needs-single-vlan` | `expander.py:676` (`_build_internet_behind_firewall`) | ASA inside site has ≠1 VLAN | oc-021 |
| 5 | `vlan-outside-site-block` | `expander.py:370` (address allocation) | explicit `vlan.cidr` outside `10.<site>.0.0/16` | oc-052 |
| 6 | `asa5505-multi-site` | `ai/lint.py:228` (`_check_firewall`) | `firewall.model == "asa5505"` with `len(sites) > 1` | oc-019 |
| 7 | `server-dhcp-service` | `ai/lint.py:265` (`_check_servers`) | a `servers[]` entry requests `dhcp` | oc-020, oc-052 |
| 8 | `wireless-group-too-large` | `ai/lint.py:146` (`_check_host_group_sizes`) | a wireless host group with `count > 8` | oc-050 |

**Corrected cross-references.** Two earlier citations were wrong and are fixed
here:

- **oc-048 is not an `asa5505-multi-site` case.** It is a single office; its
  refusal is the DSL+cable bonding ask (`InternetIntent.access` is a closed
  three-value enum). It belongs under the schema limit, not CG-6.
- **oc-054 is not a `dmz-multi-site` case.** It has no firewall/DMZ; its CG-3
  note is about one internet block at `sites[0]`.

## Schema limits (not refusal codes — inexpressible shapes)

| # | Limit | Engine site | Probe | Candidates |
| --- | --- | --- | --- | --- |
| 9 | IoT kind is `smart_led` only (no cameras/sensors) | `ai/intent.py:299` | `iot-camera-kind` → `ValidationError` | oc-027, oc-054 |
| 10 | `InternetIntent.access` is exactly one of `wan`/`dsl`/`cable` (no bonding) | `ai/intent.py:237` | `bonded-access` → `ValidationError` | oc-048 |
| 11 | `firewall` is a single object; internet built only at `sites[0]` | `ai/intent.py:327` + `ai/expander.py:622-631` | `cg3-one-internet-block` | oc-054 |

## Related lint that shaped buildable candidates

| Code | Site | Meaning |
| --- | --- | --- |
| `guest-vlan-without-isolation` | `ai/lint.py:242` | an `is_guest` VLAN without `firewall.guest_isolation`; combined with #1, no buildable intent can carry a guest flag **and** an isolation guarantee |

## Candidate citation reconciliation

- **22 distinct refusal candidates.** The tables above intentionally overlap:
  oc-019 cites three ASA codes, oc-052 cites two codes, oc-054 cites three gaps,
  oc-049 cites the guest code plus a contradiction, and oc-048 cites the schema
  limit plus a contradiction.
- Every refusal candidate's `expected_behaviour` names at least one code above,
  or is an explicit ambiguity/contradiction response that asserts no engine code
  (the ambiguous/contradictory rows in the capability register).
- `oc-017` is a contradiction candidate that **overlaps** CG-4 in prose but does
  not name `guest-isolation-unsupported`; it is not counted as a CG-4 citation.
- `oc-016` is an ambiguity candidate that names the access enum as the reason it
  must ask; it is not a refusal citation.

## Source state

- Traced in the working tree at engine commit
  `e8503cdfb32a83817a2fbc84ad4245057acf75a2` plus pre-existing uncommitted Stage
  3/4 changes. `src/network_autopilot/ai/` was untracked at that commit; it is
  Stage-5-only and is committed in the follow-up Stage-5 commit.
- The probes are offline. They do not represent executed Packet Tracer
  verification, and no live PT run supports any claim here.

## Reproduce

```bash
python havyn-stage5/scripts/probe_capability_gaps.py
grep -n "guest-isolation-unsupported\|server-dhcp-service\|wireless-group-too-large" \
  src/network_autopilot/ai/expander.py src/network_autopilot/ai/lint.py
```
