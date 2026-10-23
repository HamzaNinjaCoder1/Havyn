# Wireless probes (S3.E1–E5)

## What is already proven

`wireless-association` is a Verified capability in the existing map: a laptop's
radio associates to an AccessPoint-PT (PT shows the Antenna link) and traffic
crosses the AP (`tests/autopilot/test_live_connectivity.py`).

## What was probed for Stage 3

An `AccessPoint-PT` was created live and its JavaScript prototype was dumped.
It exposes only generic device/module methods (`getPorts`, `getModel`,
`addModule`, `getCommandLine`, …). It does **not** expose `setSsid`, `setWpa`,
`setSecurity`, `setChannel` or a `getProcess(<wireless service>)` object. A
`WLC-PT` is catalogued, but no WLC configuration API was found in the same
surface.

## Verdict

| Track | Feature | Status | Reason |
| --- | --- | --- | --- |
| S3.E1 | AccessPoint-PT association | **Verified** | Existing live association + traffic test. |
| S3.E2 | Custom SSID / WPA2 | **Unverified** | No Script-Engine method for SSID/security was found; the AP can associate at its defaults, but a custom SSID/WPA2 was not proven. |
| S3.E3 | Smartphone/tablet clients | **Unverified** | The roles and catalogued models are now accepted by the schema; no live association/flow test for these clients yet. |
| S3.E4 | WLC + lightweight APs | **Unverified** | WLC-PT is catalogued but no join/config API was proven. |
| S3.E5 | Multiple SSIDs → VLANs | **Unverified** | Depends on E2; no SSID-to-VLAN API was found. |

These are reported as **Unverified**, not Refused, because the failure is a
missing probe/API path rather than a demonstrated PT refusal. A human can set
SSID/WPA2 in the AP GUI; the engine cannot yet read it back.

## Practical alternative

Use the default AP association for automated builds (Verified), and set custom
SSID/security manually in PT. Do not claim a custom SSID was applied until a
read-back method exists.
