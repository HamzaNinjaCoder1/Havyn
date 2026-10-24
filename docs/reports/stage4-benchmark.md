# Stage 4 fault-injection benchmark

_mode: simulated · run 2026-10-23T23:39:33+00:00_

Scenarios **96** · correct **96** · root-cause accuracy **100.0%** · incorrect fixes left **0**

## Per class

| fault class | cases | correct | recall | detected anywhere |
| --- | --- | --- | --- | --- |
| `acl-block` | 5 | 5 | 100% | 100% |
| `dhcp-subnet-mismatch` | 2 | 2 | 100% | 100% |
| `missing-asa-access-group` | 1 | 1 | 100% | 100% |
| `missing-cable` | 9 | 9 | 100% | 100% |
| `missing-route` | 5 | 5 | 100% | 100% |
| `native-vlan-mismatch` | 1 | 1 | 100% | 100% |
| `port-down-physical` | 9 | 9 | 100% | 100% |
| `port-shutdown` | 9 | 9 | 100% | 100% |
| `stp-blocking` | 8 | 8 | 100% | 100% |
| `trunk-vlan-not-allowed` | 1 | 1 | 100% | 100% |
| `undefined-vlan` | 1 | 1 | 100% | 100% |
| `wrong-access-vlan` | 1 | 1 | 100% | 100% |
| `wrong-asa-nameif` | 1 | 1 | 100% | 100% |
| `wrong-cable-endpoint` | 9 | 9 | 100% | 100% |
| `wrong-gateway` | 10 | 10 | 100% | 100% |
| `wrong-ip` | 10 | 10 | 100% | 100% |
| `wrong-mask` | 8 | 8 | 100% | 100% |
| `wrong-next-hop` | 5 | 5 | 100% | 100% |
| `wrong-security-level` | 1 | 1 | 100% | 100% |

## Confusion matrix

| actual \ predicted | acl-block | dhcp-subnet-mismatch | missing-asa-access-group | missing-cable | missing-route | native-vlan-mismatch | port-down-physical | port-shutdown | stp-blocking | trunk-vlan-not-allowed | undefined-vlan | wrong-access-vlan | wrong-asa-nameif | wrong-cable-endpoint | wrong-gateway | wrong-ip | wrong-mask | wrong-next-hop | wrong-security-level |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `acl-block` | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `dhcp-subnet-mismatch` | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `missing-asa-access-group` | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `missing-cable` | 0 | 0 | 0 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `missing-route` | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `native-vlan-mismatch` | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `port-down-physical` | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `port-shutdown` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `stp-blocking` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `trunk-vlan-not-allowed` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `undefined-vlan` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `wrong-access-vlan` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `wrong-asa-nameif` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| `wrong-cable-endpoint` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 0 | 0 | 0 | 0 | 0 |
| `wrong-gateway` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 0 | 0 | 0 |
| `wrong-ip` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 0 | 0 |
| `wrong-mask` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 |
| `wrong-next-hop` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 0 |
| `wrong-security-level` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |

## Repairs

- attempted: 57
- succeeded: 36
- rolled back: 0
- incorrect fixes left in place: 0

## Multi-fault cases

- T02: injected ['port-shutdown', 'wrong-access-vlan'] · detected ['port-shutdown', 'wrong-access-vlan'] · both detected: True
- T03: injected ['port-shutdown', 'missing-route'] · detected ['missing-route', 'port-shutdown'] · both detected: True
- T04: injected ['port-shutdown', 'missing-route'] · detected ['missing-route', 'port-shutdown'] · both detected: True
- T05: injected ['port-shutdown', 'missing-route'] · detected ['missing-route', 'port-shutdown'] · both detected: True
- T08: injected ['port-shutdown', 'missing-route'] · detected ['missing-route', 'port-shutdown'] · both detected: True
- T10: injected ['port-shutdown', 'missing-route'] · detected ['missing-route', 'port-shutdown'] · both detected: True

## Excluded networks

- T06: clean baseline fails pc1->lap1: no cabled path exists between 'pc1' and 'lap1'
- T07: clean baseline already allows srv->pc1, which the criterion expects blocked
- T11: clean baseline fails lap1->pc1: no cabled path exists between 'lap1' and 'pc1'
- T12: clean baseline fails pc1->lap1: no cabled path exists between 'pc1' and 'lap1'
- T14: clean baseline fails lap1->led1: no cabled path exists between 'lap1' and 'led1'
- T16: clean baseline fails pc1->srv1: unknown
