# S3.A8 HSRP failover — live lab

- spec: `library\stage3\S3-A8-hsrp.json`
- tag: `q16861d`
- started: 2026-10-23T00:25:55+00:00
- finished: 2026-10-23T00:29:46+00:00
- problems: 0

| criterion | source | destination | expected | observed | verdict |
| --- | --- | --- | --- | --- | --- |
| A-1 | pc | srv | reachable | reachable | PASS |

## After failover

| criterion | expected | observed | verdict |
| --- | --- | --- | --- |
| A-1 | reachable | reachable | PASS |
