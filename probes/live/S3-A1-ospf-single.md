# S3.A1 OSPF single-area — live lab

- spec: `library\stage3\S3-A1-ospf-single.json`
- tag: `qf7e6b9`
- started: 2026-10-23T00:03:23+00:00
- finished: 2026-10-23T00:05:09+00:00
- problems: 0

| criterion | source | destination | expected | observed | verdict |
| --- | --- | --- | --- | --- | --- |
| A-1 | pc1 | pc2 | reachable | reachable | PASS |
| A-2 | pc2 | pc3 | reachable | reachable | PASS |
| A-3 | pc1 | pc3 | reachable | reachable | PASS |
