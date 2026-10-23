# T04 HQ branch serial WAN — live lab

- spec: `library\T04-serial-wan.json`
- tag: `q091d6c`
- started: 2026-10-23T00:13:31+00:00
- finished: 2026-10-23T00:15:44+00:00
- problems: 1

| criterion | source | destination | expected | observed | verdict |
| --- | --- | --- | --- | --- | --- |
| A-1 | pc2 | pc1 | reachable | reachable | PASS |

## Problems

- ios hq: configuration did not take effect - routes still missing after retry: 10.0.2.0/24 via 10.0.0.2
