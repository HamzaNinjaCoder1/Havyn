# Havyn Capability Map

_generated 2026-10-23T17:02:44+00:00_ — statuses change only with the evidence named in each row.

## Status meanings

- **Verified** — live Packet Tracer evidence in this repository proves the capability (a live test or a verbatim probe capture). Implemented code alone is not Verified.
- **Refused** — Packet Tracer lacks or blocks the required capability and Havyn intentionally refuses it at compile-time/runtime instead of emitting a config that would half-work or report a false PASS. A manual GUI path may exist; that is not automation proof.
- **Unverified** — not proven; no claim is made.

Offline tests prove internal consistency, not PT behaviour; only the live evidence named in a row proves that. Refused is an evidence-backed capability gap, not a failed test run.

## Verified (48)

- **router-acl-enforce** — IOS router numbered ACLs with address matching (router). Evidence: `tests/autopilot/test_live_policy.py::test_router_acl_renders_a_cidr_operand_and_enforces_it`.
- **asa-wildcard-permit** — ASA wildcard permit on the outside nameif passes the protocol (firewall:5506-X, firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced`.
- **asa-5505-svi-readback** — ASA 5505 Vlan SVIs read back pushed addresses (firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_5505_vlan_addresses_read_back_after_the_push`.
- **wireless-association** — Wireless clients associate automatically (Antenna link) (laptop, accesspoint). Evidence: `tests/autopilot/test_live_connectivity.py (wireless area: laptop associates, then reaches across the AP)`.
- **flow-reachable-blocked** — ICMP flows verify reachable and blocked end to end (router, switch, pc, firewall). Evidence: `tests/autopilot/test_live_connectivity.py`.
- **asa-address-scoped** — Address-scoped ASA rules (per-host/per-subnet permits) (firewall:5506-X, firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced`.
- **S3.A1-ospf-single** — OSPF single-area routing (router). Evidence: `probes/live/S3-A1-ospf-single.md`.
- **S3.A3-eigrp** — EIGRP routing (router). Evidence: `probes/live/S3-A3-eigrp.md`.
- **S3.A4-ripv2** — RIP v2 routing (router). Evidence: `probes/live/S3-A4-ripv2.md`.
- **S3.A5-default-redistribute** — Default route + default-information originate (router). Evidence: `probes/live/S3-A5-default-redistribute.md (flow PASS; static-route read-back still reports missing)`.
- **S3.A6-nat-pat** — NAT/PAT overload (router). Evidence: `probes/live/T10-nat-internet.md (flow PASS; default-route read-back still reports missing)`.
- **S3.A7-dhcp-relay** — DHCP relay to a central server (router). Evidence: `probes/live/S3-A7-dhcp-relay.md (remote DHCP lease + flow PASS; static-route read-back still reports missing)`.
- **S3.A8-hsrp** — HSRP gateway failover (router). Evidence: `probes/live/S3-A8-hsrp.md`.
- **S3.A9-serial-wan** — Serial WAN end-to-end (DCE clock) (router). Evidence: `probes/live/T04-serial-wan.md (flow PASS; static-route read-back still reports missing)`.
- **S3.A10-gre-tunnel** — GRE tunnel (router). Evidence: `probes/raw/gre-tunnel.txt (interface source + 'tunnel mode gre ip'; live ping 4/5)`.
- **S3.A12-bgp** — Basic BGP (router). Evidence: `probes/live/S3-A12-bgp.md`.
- **S3.A13-ipv6** — IPv6 addressing and static routes (router). Evidence: `probes/raw/ipv6-ospfv3.txt (live IPv6 ping 5/5 and connected route read-back)`.
- **S3.A14-ospfv3** — OSPFv3 (router). Evidence: `probes/raw/ipv6-ospfv3.txt (live OSPFv3 neighbors FULL in both directions)`.
- **S3.A15-ssh** — SSH and local AAA (router, pc). Evidence: `probes/raw/S3.ssh-login.txt (PC reached Password:, logged in; show users vty 0 admin and show ssh Session Started)`.
- **S3.A16-ntp-syslog-snmp** — NTP, Syslog, SNMP (router, server). Evidence: `probes/raw/S3.ntp-syslog.txt (router syslog entries arrived at the server; PT does not synchronise the router clock, so NTP time is unobservable)`.
- **S3.B1-trunk** — Trunk read-back (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B2-svi-routing** — L3 switch SVI + ip routing (l3_switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B3-etherchannel** — EtherChannel (LACP) (switch, l3_switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B4-stp** — STP root/PortFast/BPDU guard (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B5-port-security** — Port-security (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B6-vtp** — VTP domain/mode (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B7-voice-vlan** — Voice VLAN (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B8-mgmt-ip** — Switch management IP (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.C1-address-scoped-acl** — Address-scoped ASA ACL (firewall:5506-X, firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced`.
- **S3.C2-dmz-zone** — ASA DMZ third zone (firewall:5506-X). Evidence: `probes/raw/S3.asa-lab.txt (outside->DMZ reachable; outside->inside blocked)`.
- **S3.C4-asa-routes** — ASA static/default routes (firewall:5506-X). Evidence: `probes/raw/S3.asa-lab.txt (inside->remote network reachable through the static route)`.
- **S3.C5-asa-dhcp** — ASA DHCP server (firewall:5506-X). Evidence: `probes/raw/S3.asa-lab.txt (inside PC leased 10.99.0.100 from the ASA)`.
- **S3.D0-server-api** — Server-PT services API probe (server). Evidence: `probes/raw/S3.processes.txt (DnsServer, HttpServer, DhcpServerMain, EmailServer, FtpServer, NtpServer, SyslogServer and AcsServer are exposed)`.
- **S3.D1-app-dns-http** — App-level DNS/HTTP proof (server, pc). Evidence: `probes/raw/S3.dns-http.txt (nslookup/ping server.local and an HTTP body returned)`.
- **S3.D2-dns** — DNS service (server, pc). Evidence: `probes/raw/S3.dns-http.txt (PC resolved server.local through 10.88.0.2)`.
- **S3.D3-http** — HTTP/HTTPS service (server, pc). Evidence: `probes/raw/S3.dns-http.txt (PC fetched the configured index.html body over HTTP; the HTTPS path was not separately exercised)`.
- **S3.D4-server-dhcp** — DHCP server (Server-PT) (server, pc). Evidence: `probes/raw/S3.server-dhcp.txt (POOL1 lease 10.77.0.100; DHCP server 10.77.0.2)`.
- **S3.D6-ftp** — FTP service (server, pc). Evidence: `probes/raw/S3.ftp-login.txt (PC console `ftp 10.89.0.2` -> `230- Logged in`, `ftp>` prompt, `221- Service closing` on quit; the server's FTP process is read back enabled with the user account)`.
- **S3.D8-ntp-syslog-server** — NTP/Syslog server (server, router). Evidence: `probes/raw/S3.ntp-syslog.txt (NTP server configured and read back; five syslog entries from the router arrived)`.
- **S3.E1-ap-association** — AccessPoint-PT association (access_point, laptop). Evidence: `tests/autopilot/test_live_connectivity.py`.
- **S3.E3-phone-tablet** — Smartphone/tablet wireless clients (smartphone, tablet, access_point). Evidence: `probes/raw/S3.e3-clients.txt (both associated, DHCP .2/.3, flows both ways)`.
- **S3.F1-cable-modem** — Cable modem (modem, cloud, pc). Evidence: `library/T06-home-cable.json (live runner pass run-d0c3c88b4e28)`.
- **S3.F2-cloud-mapping** — Explicit cloud mapping driver (cloud, modem, router). Evidence: `probes/raw/S3.cloud-lab.txt (two Frame Relay circuits on one cloud; customer A/B isolation blocked)`.
- **S3.F3-isp-template** — ISP template (cloud, modem, router, server). Evidence: `probes/raw/S3.cloud-lab.txt (home->internet and internet->home reachable)`.
- **S3.F4-cloud-serial** — Cloud serial / Frame Relay (cloud, router). Evidence: `probes/raw/S3.cloud-lab.txt (DLCI 100<->200 Frame Relay through the cloud; ping reachable)`.
- **S3.G1-iot-probe** — IoT registration API probe (iot). Evidence: `probes/raw/S3.iot-api.txt (Smart-LED IoeClient exposes setupRemoteApi/reportStates; no IoT server process on the HomeRouter)`.
- **S3.H2-host-dns** — Host DNS setting (pc, laptop, server). Evidence: `probes/raw/S3.dns-http.txt (DnsClient server 10.88.0.2 and ipconfig /all DNS 10.88.0.2)`.
- **S3.Z1-capability-map** — Capability Map publish + refusal review (docs). Evidence: `docs/capability-map.md (live capability map publish)`.

## Unverified (17)

- **hsrp-failover** — HSRP gateway failover (router, l3_switch). Evidence: `library/T15-redundant-core.json (pending: no compiler render path; physical topology builds, redundancy unverified)`.
- **etherchannel** — EtherChannel bundling (switch, l3_switch). Evidence: `library/T15-redundant-core.json (pending: no compiler render path; physical topology builds, redundancy unverified)`.
- **ospf-backbone** — OSPF single-area routing (router). Evidence: `library/T09-ospf-backbone.json (offline lint+compile pass; no live lab yet)`.
- **nat-pat** — NAT/PAT internet access (router). Evidence: `library/T10-nat-internet.json (offline lint+compile pass; no live lab yet)`.
- **asa-dmz** — ASA three-zone DMZ policy (firewall:5506-X). Evidence: `library/T08-asa-dmz.json (pending S1.16 live proof)`.
- **small-office-dhcp** — Single-router office with DHCP (router, switch, pc). Evidence: `library/T01-small-office.json (builds offline; live runner pass promotes this row)`.
- **router-on-a-stick** — Router-on-a-stick inter-VLAN routing (router, switch, pc). Evidence: `library/T02-two-vlan-office.json (builds offline; live runner pass promotes this row)`.
- **campus-l3-core** — Three-floor campus with an L3 core (l3_switch, switch, pc). Evidence: `library/T03-campus-l3-core.json (builds offline; no live runner pass yet)`.
- **serial-wan** — HQ + branch over a serial WAN (router). Evidence: `library/T04-serial-wan.json (builds offline; no live runner pass yet)`.
- **dsl-internet** — DSL access through a cloud/modem pair (router, cloud, modem). Evidence: `library/T05-dsl-internet.json (builds offline; no live runner pass yet)`.
- **cable-modem-home** — Home cable-modem network (modem, router, pc). Evidence: `library/T06-home-cable.json (builds offline; live runner pass promotes this row)`.
- **asa-perimeter** — ASA inside/outside perimeter (firewall:5506-X, firewall:5505). Evidence: `library/T07-asa-perimeter.json (pending S1.16 live proof)`.
- **wireless-office** — Wireless office with AP-attached laptops and phones (accesspoint, laptop, smartphone). Evidence: `library/T11-wireless-office.json (builds offline; no live runner pass yet)`.
- **linksys-home** — Linksys home gateway (home_router, pc). Evidence: `library/T12-linksys-home.json (builds offline; no live runner pass yet)`.
- **server-farm** — DNS + HTTP + DHCP server farm (server, switch, pc). Evidence: `library/T13-server-farm.json (builds offline; no live runner pass yet)`.
- **iot-smart-home** — IoT smart-home devices (iot, home_router). Evidence: `library/T14-iot-home.json (builds offline; no live runner pass yet)`.
- **enterprise-campus** — Enterprise campus reference build (router, switch, firewall, server, pc). Evidence: `library/T16-enterprise-campus.json (builds offline; no live runner pass in this library)`.

## Refused (14)

- **acl-icmp-port** — dest_port on an icmp ACL rule (firewall, router). Evidence: `tests/autopilot/test_asa_policy.py::test_dest_port_on_icmp_is_refused_before_pt_aborts_the_block`.
  Refusal: PT rejects the line and configureIosDevice aborts the rest of the block, which would cost the interface addresses.
- **asa-5505-third-zone** — Third security zone on the ASA 5505 (firewall:5505). Evidence: `tests/autopilot/test_asa_policy.py::test_5505_refuses_more_than_two_zones`.
  Refusal: PT's 5505 only exposes Vlan1/Vlan2; a third zone cannot be configured. Use a 5506-X.
- **policy-on-non-enforcer** — ACL policy on a device that enforces nothing (hosts, switches) (pc, server, switch). Evidence: `tests/autopilot/test_asa_policy.py::test_policy_on_a_device_with_no_enforcement_path_fails_the_build`.
  Refusal: The policy would compile and then do nothing. Only ASA firewalls and IOS routers enforce.
- **S3.A2-ospf-multi-area** — OSPF multi-area (ABR, inter-area routes) (router). Evidence: `probes/a2-multi-area-refusal.md`.
  Refusal: PT accepts the ABR config and both neighbors reach FULL, but it does not advertise area-0 prefixes into area 1; the cross-area flow stays blocked. Use single-area OSPF (S3.A1) or static inter-area routes.
- **S3.A11-ipsec** — IPsec site-to-site (router). Evidence: `probes/raw/S3.A11-router.txt`.
  Refusal: PT accepted the push but stored none of the crypto isakmp / ipsec transform-set / crypto map lines; no live tunnel is possible on this model. Use GRE if its follow-up probe succeeds, or terminate IPsec outside PT.
- **S3.C3-asa-nat** — ASA object NAT/PAT (firewall:5506-X). Evidence: `probes/raw/S3.asa-lab.txt`.
  Refusal: PT's ASA 9.6 image rejects every NAT form tested (object NAT, twice NAT, classic nat/global) with CLI status 2, so no translation can be configured. Use a router for NAT (S3.A6) or terminate outside PT.
- **S3.C6-object-group** — ASA object groups (firewall:5506-X). Evidence: `probes/raw/S3.asa-lab.txt`.
  Refusal: PT's ASA 9.6 image rejects `object-group network` and any ACL that references a group with CLI status 2. Use a plain address-scoped ACL (S3.C1).
- **S3.D5-email** — Email service (server, pc). Evidence: `probes/raw/S3.server-misc.txt`.
  Refusal: The EmailServer configuration call (`updateAllAccounts`) blocks the Script Engine and closes Packet Tracer, so no mail flow can be driven. Use an external mail server.
- **S3.D7-aaa** — AAA (RADIUS/TACACS) server (server, router). Evidence: `probes/raw/S3.aaa-login.txt`.
  Refusal: The ACS server accepts the user and client records and the router accepts the radius-server and tacacs-server configuration, but an SSH login against either path times out; PT does not complete the AAA exchange. Use local AAA (S3.A15) or an external AAA server.
- **S3.E2-ssid-wpa2** — Custom SSID / WPA2 (access_point, laptop). Evidence: `probes/raw/S3.e2-custom-ssid.txt`.
  Refusal: The AP accepts and reads back a custom SSID/WPA2, but every documented client profile call (addProfile, setCurrentProfile, setCurrentProfileStringIPs) is rejected by this PT build and no antenna link forms. Use the default SSID (S3.E1/S3.E3).
- **S3.E4-wlc** — WLC + lightweight APs (wlc, access_point). Evidence: `probes/raw/S3.wireless.txt`.
  Refusal: PT has no placeable lightweight AP model (LAP-PT/3702i are rejected) and WLC-PT exposes only a base CapwapAC process, so a WLC join cannot be built. Use autonomous APs.
- **S3.E5-multi-ssid** — Multiple SSIDs to VLANs (access_point). Evidence: `probes/raw/S3.wireless-lab.txt`.
  Refusal: The AP's WirelessServer has one SSID and no SSID-to-VLAN mapping method; PT offers no multi-SSID/VLAN API. Use separate APs/VLANs or the default SSID.
- **S3.G2-iot-control** — IoT registration + control (iot). Evidence: `probes/raw/S3.iot-api.txt`.
  Refusal: The Smart-LED exposes IoeClient, but the HomeRouter has no IoeServer and no registration/control path is exposed, so a registered on/off control cannot be driven. Configure the IoT service in the PT GUI.
- **S3.H1-end-devices** — Printer/IP phone/tablet/smartphone endpoints (printer, ip_phone, tablet, smartphone). Evidence: `probes/raw/S3.h1-end-devices.txt`.
  Refusal: Printer-PT, tablet and smartphone are proven endpoints, but the 7960's Vlan1 never comes up (no usable CLI, isPortUp false with the wall port linked and powered), so the IP phone cannot be a flow endpoint. Use a PC/laptop as the voice endpoint or a downstream PC behind the phone.
