# Wireless probes (S3.E1–E5)

## The API that the first probe missed

The AP/station expose a real wireless process; the earlier probe asked for
`setSsid` on the device itself and for `getProcess("Wireless")`:

* `AccessPoint-PT.getProcess("WirelessServer")` —
  `setSsid`, `getSsid`, `setAuthenType`, `setEncryptType`,
  `getWpaProcess().setKey`, `setStandardChannel`, `setPort`,
  `setSsidBrdCastEnabled`, `resetAllAssociations`.
* `Laptop/Tablet/Smartphone.getProcess("WirelessClient")` — the same settings
  plus `addProfile`, `setCurrentProfile`, `setCurrentProfileStringIPs`,
  `getCurrentProfile`, `getCurrentApMac`.

## Verdict

| Track | Feature | Status | Evidence |
| --- | --- | --- | --- |
| S3.E1 | AccessPoint-PT association | **Verified** | `tests/autopilot/test_live_connectivity.py` (laptop antenna link + traffic). |
| S3.E2 | Custom SSID / WPA2 | **Refused** | `raw/S3.e2-custom-ssid.txt` — the AP accepts and reads back SSID/WPA2; every client profile call is rejected (`Invalid arguments` / `invalid vector subscript`) and no antenna link forms. |
| S3.E3 | Smartphone/tablet clients | **Verified** | `raw/S3.e3-clients.txt` — both associate, DHCP .2/.3, flows both ways. |
| S3.E4 | WLC + lightweight APs | **Refused** | `raw/S3.wireless.txt` — no placeable LAP-PT/3702i; WLC-PT exposes only a base CapwapAC process. |
| S3.E5 | Multiple SSIDs → VLANs | **Refused** | `raw/S3.wireless-lab.txt` — one SSID, no SSID-to-VLAN method. |

Measured detail: the client profile API is the blocker for E2, not the AP. The
simple setters (`setSsid`/`setAuthenType`/`setEncryptType`) update the process
state but leave the profile at `Default`, so PT does not associate to the
custom SSID. The default SSID path (E1/E3) is the one that works.

## Reproduction

```bash
python probes/stage3_e3_lab.py
python probes/stage3_e2_probe.py
python probes/stage3_wireless_probe.py   # WLC / LAP availability
```
