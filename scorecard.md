# Havyn nightly scorecard

_generated 2026-10-22T11:07:16+00:00_

Cases: **3** · pass 3

| metric | value |
| --- | --- |
| false PASS rate | 0.0 (0/5) (PASS on a proven-broken lab) |
| criteria pass % | 100.0 (40/40) |
| same-network consistency | 1.0 (3/3) |
| build time total / mean / max (s) | 252.0 / 84.0 / 103.0 |
| diagnosis accuracy | 1.0 (4/4) |
| refusal (blocked) rate | 0.0 |

## Cases

| topology | case | run | mode | outcome | criteria | time (s) | evidence | reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T01 | T01 | run-5d0219b0774c | live | PASS | 15/15 pass | 88.9 | autopilot_evidence\build-4351c9e5f0df | — |
| T02 | T02 | run-a08c98a4f999 | live | PASS | 16/16 pass | 103.0 | autopilot_evidence\build-1037e02326c8 | — |
| T06 | T06 | run-d0c3c88b4e28 | live | PASS | 9/9 pass | 60.1 | autopilot_evidence\build-906dba4b7450 | — |

## False PASS detail

0 of 5 proven-broken labs still read as PASS.

## Diagnosis detail

4 of 4 required causes named.
Unknown by design (not scored): wrong-gateway.

## Consistency detail

- `T01`: 2 run(s), statuses pass — consistent
- `T02`: 2 run(s), statuses pass — consistent
- `T06`: 2 run(s), statuses pass — consistent

## Skipped (pending specs, not silently dropped)

- **T07**: awaiting S1.16 live proof (ASA criteria + address-scoped ACL decision)
- **T08**: awaiting S1.16 live proof (ASA criteria + address-scoped ACL decision)
- **T15**: HSRP failover and EtherChannel bundling have no compiler render path; physical topology builds, redundancy goal unverified
