# Brownfield diagnosis — stage4-live-two-vlan

_generated 2026-10-24T00:04:53+00:00_

Diagnoses **1** · diagnosed **1** · unknown **0** · repairable **1** · schema issues **0**

| # | layer | cause | device | port | vlan | confidence | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | L2 | `wrong-access-vlan` | s4l-s1 | FastEthernet1/1 | 10 | high | DIAGNOSED |

## 1. s4l-s1:FastEthernet1/1 is in VLAN 20; the objective needs VLAN 10

- **Cause class:** `wrong-access-vlan`
- **Layer:** L2
- **Device:** s4l-s1
- **Interface:** FastEthernet1/1
- **VLAN / subnet:** 10 / n/a
- **Confidence:** high
- **Status:** DIAGNOSED
- **Objective:** `OBJ-USER-1` (confirmed)
- **Impact:** the port forwards the flow into the wrong broadcast domain

**Evidence**

- objective OBJ-USER-1: s4l-pc1 must communicate with s4l-pc2
- first failing hop 1: s4l-s1 port FastEthernet1/1
- s4l-s1:FastEthernet1/1 (ingress) is in VLAN 20, but the flow needs VLAN 10
- observed access VLAN 20
- flow VLAN 10

**Recommended minimal fix**

- `s4l-s1` (configure_ios): `interface FastEthernet1/1`; `switchport access vlan 10`; `exit`
  - rollback: `interface FastEthernet1/1`; `switchport access vlan 20`; `exit`

**Verification requirements**

- re-check objective OBJ-USER-1: s4l-pc1 must communicate with s4l-pc2
- re-run every criterion that passed before the change
