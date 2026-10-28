# Refusal source verification (Stage 5 prompt authoring)

Scope: every named refusal/limitation asserted by a `expect_refusal: true`
candidate in `../owner-candidates/`, traced to the exact engine site that
produces it. The point is that a refusal must be a **real** engine behaviour
(AGENTS.md rule 9), not a reviewer's invention.

Evidence commit: `06c57ced050cda532915c15b1ce772b0a0eaf607`.

## Verification gap (rule 10 — stated, not hidden)

`owner-candidates/verify_candidates.py` verifies refusals ONLY by asserting
`expected_intent is null` (`verify_candidates.py:79-86`); it does **not** run a
refusal candidate through the engine to observe the refusal fire. So the
refusal *codes* below are verified by **source inspection** (the code string
exists at the cited line and is raised on the described condition), not by an
end-to-end execution of each candidate. What that means:

- **Verified here**: each code exists, is raised by the named function, and the
  condition it guards matches the candidate's `expected_behaviour`.
- **Not verified here**: that a particular candidate's exact wire-input would
  route to that code at runtime (that needs the model to actually emit the
  triggering shape). This half is disclosed rather than assumed.

Every buildable `expected_intent` *is* executed end-to-end through
schema → lint → expand → spec-lint → IPAM → compile (`verify_candidates.py`),
so the buildable half has stronger evidence than the refusal half.

## Claim → engine site

| # | Refusal code / limit | Engine site | Raised when | Cited by |
| --- | --- | --- | --- | --- |
| 1 | `guest-isolation-unsupported` | `src/network_autopilot/ai/expander.py:299` (`_refuse_unsupported`) | `firewall.enabled` and `firewall.guest_isolation` | oc-007, oc-017, oc-026, oc-049, oc-054 |
| 2 | `dmz-multi-site-unsupported` | `src/network_autopilot/ai/expander.py:307` (`_refuse_unsupported`) | `firewall.dmz` with `len(sites) > 1` | oc-019, oc-054 (CG-3 half) |
| 3 | `asa5505-no-dmz` | `src/network_autopilot/ai/expander.py:670` (`_build_internet_behind_firewall`) | `firewall.model == "asa5505"` and `firewall.dmz` | oc-019, oc-051 |
| 4 | `firewall-needs-single-vlan` | `src/network_autopilot/ai/expander.py:676` (`_build_internet_behind_firewall`) | ASA inside site has ≠1 VLAN | oc-021 |
| 5 | `vlan-outside-site-block` | `src/network_autopilot/ai/expander.py:370` (address allocation) | explicit `vlan.cidr` not inside `10.<site>.0.0/16` | oc-052 |
| 6 | `asa5505-multi-site` | `src/network_autopilot/ai/lint.py:228` (`_check_firewall`) | `firewall.model == "asa5505"` with `len(sites) > 1` | oc-019, oc-048 |
| 7 | `server-dhcp-service` | `src/network_autopilot/ai/lint.py:265` (`_check_servers`) | a `servers[]` entry requests the `dhcp` service | oc-020, oc-052 |
| 8 | `wireless-group-too-large` | `src/network_autopilot/ai/lint.py:146` (`_check_host_group_sizes`) | a wireless host group with `count > 8` | oc-050 |

## Schema limits (not refusal codes — inexpressible shapes)

| # | Limit | Engine site | Cited by |
| --- | --- | --- | --- |
| 9 | IoT kind is `smart_led` only (no cameras/sensors) | `src/network_autopilot/ai/intent.py:299` (`kind: Literal["smart_led"]`) | oc-027, oc-054 |
| 10 | `InternetIntent.access` is exactly one of `wan`/`dsl`/`cable` (no bonded link) | `src/network_autopilot/ai/intent.py:237` (`access: Literal["wan","dsl","cable"]`) | oc-048 |
| 11 | `firewall` is a single object; internet built only at `sites[0]` | `ai/intent.py` (singular field) + `ai/expander.py:622-631` | oc-054 |

## Related lint that shaped buildable candidates

These are not refusals but constrain what a buildable candidate may set; they
explain why buildable guest-looking VLANs omit the guest flag:

| Code | Site | Meaning |
| --- | --- | --- |
| `guest-vlan-without-isolation` | `ai/lint.py:242` | an `is_guest` VLAN without `firewall.guest_isolation`; combined with #1, no buildable intent can carry a guest flag **and** an isolation guarantee |

## Cross-checks run

```text
# every code string resolved to a single source line (grep over src/)
guest-isolation-unsupported   -> expander.py:299
asa5505-multi-site            -> lint.py:228
asa5505-no-dmz                -> expander.py:670
dmz-multi-site-unsupported    -> expander.py:307
server-dhcp-service           -> lint.py:265
firewall-needs-single-vlan    -> expander.py:676
wireless-group-too-large      -> lint.py:146
vlan-outside-site-block       -> expander.py:370
kind: Literal["smart_led"]    -> intent.py:299
access: Literal[wan/dsl/cable]-> intent.py:237
```

No candidate cites a code that does not exist at the cited site. Every refusal
candidate's `expected_behaviour` names at least one of the codes above (or an
ASK/surface-contradiction behaviour for the ambiguous/contradictory categories,
which assert no engine code).
