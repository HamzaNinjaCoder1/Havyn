# S3.A5 default route and redistribution — live lab

- spec: `library\stage3\S3-A5-default-redistribute.json`
- tag: `qc86342`
- started: 2026-10-23T00:08:34+00:00
- finished: 2026-10-23T00:11:01+00:00
- problems: 1

| criterion | source | destination | expected | observed | verdict |
| --- | --- | --- | --- | --- | --- |
| A-1 | pc | srv | reachable | reachable | PASS |

## Problems

- ios edge: configuration did not take effect - routes still missing after retry: 0.0.0.0/0 via 203.0.113.2
