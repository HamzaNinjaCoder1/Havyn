# Cloud, modem and WAN probes (S3.F1–F4)

## What the cloud can do

`Cloud-PT` exposes the mapping API the earlier note said the compiler did not
emit:

* `addPortConnection(portName1, portName2)` — maps two cloud ports
  (e.g. `Coaxial7` ↔ `Ethernet6`). String names, not indices.
* `setDslConnection(portName, isDsl)` — marks a port as DSL (`true`) or
  cable (`false`).
* `getPortStrConnections()` / `getPortConnectionCount()` — read the mapping
  back.
* Serial ports are Frame Relay: `addSubLink(name, dlci)` per serial port,
  then `addSubLinkConnection(port1, sub1, port2, sub2)`.

## Verdict

| Track | Feature | Status | Evidence |
| --- | --- | --- | --- |
| S3.F1 | Cable modem | **Verified** | `library/T06-home-cable.json` (live runner pass run-d0c3c88b4e28). |
| S3.F2 | Explicit cloud mapping driver | **Verified** | `raw/S3.cloud-lab.txt` — two Frame Relay circuits on one cloud; each customer pair reaches itself and cannot reach the other. |
| S3.F3 | ISP template | **Verified** | same file — customer router → cable modem → cloud (coax↔Ethernet) → ISP router → internet server; flows both ways. |
| S3.F4 | Cloud serial / Frame Relay | **Verified** | same file — DLCI 100↔200 through the cloud, `show frame-relay map` active, ping reachable. |

Measured detail: the cable modem's provider side is **Port 0** (coax to the
cloud) and its copper handoff is **Port 1** (to the customer router). The
reverse pairing fails.

## Reproduction

```bash
python probes/stage3_cloud_probe.py   # signatures + mapping read-back
python probes/stage3_cloud_lab.py     # F2/F4 Frame Relay and F3 ISP template
```
