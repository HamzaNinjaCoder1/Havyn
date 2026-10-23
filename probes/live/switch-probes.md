# Stage 3 switch probes (S3.B1–B8)

Live probes on a 2960-24TT (and a 3560-24PS for the SVI/routing case) through
the Script Engine file channel. Each probe pushed one feature, read
`show running-config` and the named operational `show`, then deleted the
device. Raw captures are under `probes/raw/S3.B*-switch.txt`.

| Track | Feature | Live observation | Status |
| --- | --- | --- | --- |
| S3.B1 | Trunk read-back | `switchport mode trunk`, native VLAN and `allowed vlan 10,20,99` read back; `show interfaces trunk` accepted. | **Verified (config/read-back)** |
| S3.B2 | L3 switch SVI + `ip routing` | `ip routing`, `interface Vlan10`, address and `no shutdown` read back on a 3560-24PS. | **Verified (config/read-back)** |
| S3.B3 | EtherChannel (LACP) | `channel-group 1 mode active` read back; `show etherchannel summary` shows `Po1` with `LACP` and both members. | **Verified (config/read-back)** |
| S3.B4 | STP settings | `spanning-tree vlan 1 root primary` is normalized by IOS to `priority 24576`; both `portfast` and `bpduguard enable` read back; `show spanning-tree` accepted. | **Verified (config/read-back)** |
| S3.B5 | Port-security | `switchport port-security` read back; `show port-security` shows `Fa0/1`, max 1, action `Shutdown` (IOS omits default-valued lines from running-config). | **Verified (config/read-back)** |
| S3.B6 | VTP | `show vtp status` shows `VTP Operating Mode: Server` and `VTP Domain Name: S3LAB` (VTP state lives in vlan.dat, not running-config). | **Verified (config/read-back)** |
| S3.B7 | Voice VLAN | `switchport voice vlan 20` (with access VLAN 10) read back. | **Verified (config/read-back)** |
| S3.B8 | Switch management IP | `interface Vlan1` address, `no shutdown` and `ip default-gateway` read back. | **Verified (config/read-back)** |

## What "config/read-back" means here

The push was accepted **and** the live device's own `show` output proves the
feature is present. These rows do not claim the failure-mode behaviours that
some track descriptions mention (cable pull on an EtherChannel, a deliberate
L2 loop, an unknown-MAC violation). Those behaviours are not yet exercised by a
live lab and are therefore not part of the status.

## Reproduction

```bash
python probes/stage3_probe.py switch
```
