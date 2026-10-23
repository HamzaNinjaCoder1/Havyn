# T10 NAT internet — live lab

- spec: `library\T10-nat-internet.json`
- tag: `q448695`
- started: 2026-10-23T00:43:16+00:00
- finished: 2026-10-23T00:45:16+00:00
- problems: 1

| criterion | source | destination | expected | observed | verdict |
| --- | --- | --- | --- | --- | --- |
| A-1 | pc1 | srv | reachable | reachable | PASS |

## Problems

- ios edge: configuration did not take effect - routes still missing after retry: 0.0.0.0/0 via 203.0.113.2
