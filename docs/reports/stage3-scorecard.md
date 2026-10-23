# Havyn Stage 3 scorecard

_generated 2026-10-23T01:22:03+00:00_

Tracks: **53** · Verified **24** · Refused **12** · Unverified **17**

| track | status | evidence |
| --- | --- | --- |
| S3.A1-ospf-single | Verified | `probes/live/S3-A1-ospf-single.md` |
| S3.A2-ospf-multi-area | Refused | `probes/a2-multi-area-refusal.md` |
| S3.A3-eigrp | Verified | `probes/live/S3-A3-eigrp.md` |
| S3.A4-ripv2 | Verified | `probes/live/S3-A4-ripv2.md` |
| S3.A5-default-redistribute | Verified | `probes/live/S3-A5-default-redistribute.md (flow PASS; static-route read-back still reports missing)` |
| S3.A6-nat-pat | Verified | `probes/live/T10-nat-internet.md (flow PASS; default-route read-back still reports missing)` |
| S3.A7-dhcp-relay | Verified | `probes/live/S3-A7-dhcp-relay.md (remote DHCP lease + flow PASS; static-route read-back still reports missing)` |
| S3.A8-hsrp | Verified | `probes/live/S3-A8-hsrp.md` |
| S3.A9-serial-wan | Verified | `probes/live/T04-serial-wan.md (flow PASS; static-route read-back still reports missing)` |
| S3.A10-gre-tunnel | Verified | `probes/raw/gre-tunnel.txt (interface source + 'tunnel mode gre ip'; live ping 4/5)` |
| S3.A11-ipsec | Refused | `probes/raw/S3.A11-router.txt` |
| S3.A12-bgp | Verified | `probes/live/S3-A12-bgp.md` |
| S3.A13-ipv6 | Verified | `probes/raw/ipv6-ospfv3.txt (live IPv6 ping 5/5 and connected route read-back)` |
| S3.A14-ospfv3 | Verified | `probes/raw/ipv6-ospfv3.txt (live OSPFv3 neighbors FULL in both directions)` |
| S3.A15-ssh | Unverified | `probes/raw/S3.A15-router.txt (server config read back; PC SSH login not observable)` |
| S3.A16-ntp-syslog-snmp | Unverified | `probes/raw/S3.A16-router.txt (router config read back; server effect unobservable)` |
| S3.B1-trunk | Verified | `probes/live/switch-probes.md` |
| S3.B2-svi-routing | Verified | `probes/live/switch-probes.md` |
| S3.B3-etherchannel | Verified | `probes/live/switch-probes.md` |
| S3.B4-stp | Verified | `probes/live/switch-probes.md` |
| S3.B5-port-security | Verified | `probes/live/switch-probes.md` |
| S3.B6-vtp | Verified | `probes/live/switch-probes.md` |
| S3.B7-voice-vlan | Verified | `probes/live/switch-probes.md` |
| S3.B8-mgmt-ip | Verified | `probes/live/switch-probes.md` |
| S3.C1-address-scoped-acl | Verified | `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced` |
| S3.C2-dmz-zone | Unverified | `probes/asa.md` |
| S3.C3-asa-nat | Unverified | `probes/asa.md` |
| S3.C4-asa-routes | Unverified | `probes/asa.md` |
| S3.C5-asa-dhcp | Unverified | `probes/asa.md` |
| S3.C6-object-group | Unverified | `probes/asa.md` |
| S3.D0-server-api | Refused | `probes/server-services.md` |
| S3.D1-app-dns-http | Unverified | `probes/server-services.md` |
| S3.D2-dns | Refused | `probes/server-services.md` |
| S3.D3-http | Refused | `probes/server-services.md` |
| S3.D4-server-dhcp | Refused | `probes/server-services.md` |
| S3.D5-email | Refused | `probes/server-services.md` |
| S3.D6-ftp | Refused | `probes/server-services.md` |
| S3.D7-aaa | Refused | `probes/server-services.md` |
| S3.D8-ntp-syslog-server | Refused | `probes/server-services.md` |
| S3.E1-ap-association | Verified | `tests/autopilot/test_live_connectivity.py` |
| S3.E2-ssid-wpa2 | Unverified | `probes/wireless.md` |
| S3.E3-phone-tablet | Unverified | `probes/wireless.md` |
| S3.E4-wlc | Unverified | `probes/wireless.md` |
| S3.E5-multi-ssid | Unverified | `probes/wireless.md` |
| S3.F1-cable-modem | Verified | `library/T06-home-cable.json (live runner pass run-d0c3c88b4e28)` |
| S3.F2-cloud-mapping | Unverified | `probes/cloud.md` |
| S3.F3-isp-template | Unverified | `probes/cloud.md` |
| S3.F4-cloud-serial | Unverified | `probes/cloud.md` |
| S3.G1-iot-probe | Refused | `probes/iot.md` |
| S3.G2-iot-control | Refused | `probes/iot.md` |
| S3.H1-end-devices | Unverified | `probes/end-devices.md` |
| S3.H2-host-dns | Unverified | `probes/end-devices.md` |
| S3.Z1-capability-map | Verified | `docs/capability-map.md (live capability map publish)` |

## How to read this

- **Verified** — a live lab or live probe in this repository proves the
  capability that the row names. Rows that say `config/read-back` prove
  the configuration and the device's own `show` output, not a
  failure-mode behaviour.
- **Refused** — PT was probed and the required capability is absent or
  did not survive the push; the reason and alternative are in the row's
  evidence file.
- **Unverified** — not proven. The evidence pointer says exactly what is
  missing. No status is inferred from a compile or a push receipt.

## Known evidence defect

The A5/A6/A7/A9 live labs show a PASSing flow while the adapter's
static-route read-back still reports the route missing after retry.
The flow is the stronger proof (traffic crossed); the read-back defect
is recorded rather than hidden and is a separate adapter fix.
