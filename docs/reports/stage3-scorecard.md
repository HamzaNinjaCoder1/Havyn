# Havyn Stage 3 scorecard

_generated 2026-10-23T17:02:45+00:00_

Tracks: **53** · Verified **42** · Refused **11** · Unverified **0**

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
| S3.A15-ssh | Verified | `probes/raw/S3.ssh-login.txt (PC reached Password:, logged in; show users vty 0 admin and show ssh Session Started)` |
| S3.A16-ntp-syslog-snmp | Verified | `probes/raw/S3.ntp-syslog.txt (router syslog entries arrived at the server; PT does not synchronise the router clock, so NTP time is unobservable)` |
| S3.B1-trunk | Verified | `probes/live/switch-probes.md` |
| S3.B2-svi-routing | Verified | `probes/live/switch-probes.md` |
| S3.B3-etherchannel | Verified | `probes/live/switch-probes.md` |
| S3.B4-stp | Verified | `probes/live/switch-probes.md` |
| S3.B5-port-security | Verified | `probes/live/switch-probes.md` |
| S3.B6-vtp | Verified | `probes/live/switch-probes.md` |
| S3.B7-voice-vlan | Verified | `probes/live/switch-probes.md` |
| S3.B8-mgmt-ip | Verified | `probes/live/switch-probes.md` |
| S3.C1-address-scoped-acl | Verified | `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced` |
| S3.C2-dmz-zone | Verified | `probes/raw/S3.asa-lab.txt (outside->DMZ reachable; outside->inside blocked)` |
| S3.C3-asa-nat | Refused | `probes/raw/S3.asa-lab.txt` |
| S3.C4-asa-routes | Verified | `probes/raw/S3.asa-lab.txt (inside->remote network reachable through the static route)` |
| S3.C5-asa-dhcp | Verified | `probes/raw/S3.asa-lab.txt (inside PC leased 10.99.0.100 from the ASA)` |
| S3.C6-object-group | Refused | `probes/raw/S3.asa-lab.txt` |
| S3.D0-server-api | Verified | `probes/raw/S3.processes.txt (DnsServer, HttpServer, DhcpServerMain, EmailServer, FtpServer, NtpServer, SyslogServer and AcsServer are exposed)` |
| S3.D1-app-dns-http | Verified | `probes/raw/S3.dns-http.txt (nslookup/ping server.local and an HTTP body returned)` |
| S3.D2-dns | Verified | `probes/raw/S3.dns-http.txt (PC resolved server.local through 10.88.0.2)` |
| S3.D3-http | Verified | `probes/raw/S3.dns-http.txt (PC fetched the configured index.html body over HTTP; the HTTPS path was not separately exercised)` |
| S3.D4-server-dhcp | Verified | `probes/raw/S3.server-dhcp.txt (POOL1 lease 10.77.0.100; DHCP server 10.77.0.2)` |
| S3.D5-email | Refused | `probes/raw/S3.server-misc.txt` |
| S3.D6-ftp | Verified | `probes/raw/S3.ftp-login.txt (PC console `ftp 10.89.0.2` -> `230- Logged in`, `ftp>` prompt, `221- Service closing` on quit; the server's FTP process is read back enabled with the user account)` |
| S3.D7-aaa | Refused | `probes/raw/S3.aaa-login.txt` |
| S3.D8-ntp-syslog-server | Verified | `probes/raw/S3.ntp-syslog.txt (NTP server configured and read back; five syslog entries from the router arrived)` |
| S3.E1-ap-association | Verified | `tests/autopilot/test_live_connectivity.py` |
| S3.E2-ssid-wpa2 | Refused | `probes/raw/S3.e2-custom-ssid.txt` |
| S3.E3-phone-tablet | Verified | `probes/raw/S3.e3-clients.txt (both associated, DHCP .2/.3, flows both ways)` |
| S3.E4-wlc | Refused | `probes/raw/S3.wireless.txt` |
| S3.E5-multi-ssid | Refused | `probes/raw/S3.wireless-lab.txt` |
| S3.F1-cable-modem | Verified | `library/T06-home-cable.json (live runner pass run-d0c3c88b4e28)` |
| S3.F2-cloud-mapping | Verified | `probes/raw/S3.cloud-lab.txt (two Frame Relay circuits on one cloud; customer A/B isolation blocked)` |
| S3.F3-isp-template | Verified | `probes/raw/S3.cloud-lab.txt (home->internet and internet->home reachable)` |
| S3.F4-cloud-serial | Verified | `probes/raw/S3.cloud-lab.txt (DLCI 100<->200 Frame Relay through the cloud; ping reachable)` |
| S3.G1-iot-probe | Verified | `probes/raw/S3.iot-api.txt (Smart-LED IoeClient exposes setupRemoteApi/reportStates; no IoT server process on the HomeRouter)` |
| S3.G2-iot-control | Refused | `probes/raw/S3.iot-api.txt` |
| S3.H1-end-devices | Refused | `probes/raw/S3.h1-end-devices.txt` |
| S3.H2-host-dns | Verified | `probes/raw/S3.dns-http.txt (DnsClient server 10.88.0.2 and ipconfig /all DNS 10.88.0.2)` |
| S3.Z1-capability-map | Verified | `docs/capability-map.md (live capability map publish)` |

## How to read this

- **Verified** — a live lab or live probe in this repository proves the
  capability that the row names. Rows that say `config/read-back` prove
  the configuration and the device's own `show` output, not a
  failure-mode behaviour. Existing code is not, by itself, verification.
- **Refused** — PT was probed and lacks/blocks the required capability
  (or it did not survive the push); Havyn intentionally refuses it at
  compile-time/runtime rather than producing a false PASS. The exact
  reason and the alternative are in the row's evidence file.
- **Unverified** — not proven. The evidence pointer says exactly what is
  missing. No status is inferred from a compile or a push receipt.
- **Refused is not Failed** — Refused is an evidence-backed capability
  gap in PT; a Failed run is a test/build that did not pass.
- **GUI vs automation** — a feature that can be configured manually in
  the PT GUI is still Refused here when Havyn's Script Engine/API cannot
  prove it. Havyn only claims what its automation path observed.

## Known evidence defect

The A5/A6/A7/A9 live labs show a PASSing flow while the adapter's
static-route read-back still reports the route missing after retry.
The flow is the stronger proof (traffic crossed); the read-back defect
is recorded rather than hidden and is a separate adapter fix.
