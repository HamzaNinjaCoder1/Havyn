# Havyn nightly scorecard

_generated 2026-10-22T02:59:13+00:00_

Cases: **3** · pass 3

| metric | value |
| --- | --- |
| false PASS rate | 0.0 (0/5) (PASS on a proven-broken lab) |
| criteria pass % | 100.0 (38/38) |
| same-network consistency | 1.0 (3/3) |
| build time total / mean / max (s) | 242.3 / 80.8 / 96.9 |
| diagnosis accuracy | 1.0 (4/4) |
| refusal (blocked) rate | 0.0 |

## Cases

| topology | case | run | mode | outcome | criteria | time (s) | evidence | reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T01 | T01 | run-7e254d7e7656 | live | PASS | 15/15 pass | 85.2 | autopilot_evidence\build-19145d4547ca | — |
| T02 | T02 | run-8f78ca064894 | live | PASS | 14/14 pass | 96.9 | autopilot_evidence\build-eee1031f7df2 | — |
| T06 | T06 | run-c4edb6e574c5 | live | PASS | 9/9 pass | 60.2 | autopilot_evidence\build-dfda411c725d | — |

## False PASS detail

0 of 5 proven-broken labs still read as PASS.

## Diagnosis detail

4 of 4 required causes named.
Unknown by design (not scored): wrong-gateway.

## Consistency detail

- `T01`: 2 run(s), statuses pass — consistent
- `T02`: 2 run(s), statuses pass — consistent
- `T06`: 2 run(s), statuses pass — consistent
