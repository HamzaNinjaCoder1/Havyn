# S3.A7 DHCP relay — live lab

- spec: `library\stage3\S3-A7-dhcp-relay.json`
- tag: `q4959d2`
- started: 2026-10-23T00:11:04+00:00
- finished: 2026-10-23T00:13:28+00:00
- problems: 1

| criterion | source | destination | expected | observed | verdict |
| --- | --- | --- | --- | --- | --- |
| A-1 | pc2 | pc1 | reachable | reachable | PASS |

## Problems

- ios central: configuration did not take effect - routes still missing after retry: 10.0.2.0/24 via 10.0.12.2
