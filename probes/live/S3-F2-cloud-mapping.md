# S3.F2 Two customers on one cloud — live lab

- spec: `library\stage3\S3-F2-cloud-mapping.json`
- tag: `q3219e9`
- started: 2026-10-23T16:25:03+00:00
- finished: 2026-10-23T16:27:01+00:00
- problems: 0

| criterion | source | destination | expected | observed | verdict |
| --- | --- | --- | --- | --- | --- |
| A-1 | r1 | r2 | reachable | reachable | PASS |
| A-2 | r3 | r4 | reachable | reachable | PASS |
| A-3 | r1 | r4 | blocked | blocked | PASS |
