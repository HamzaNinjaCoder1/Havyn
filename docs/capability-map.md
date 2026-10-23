# Havyn Capability Map

_generated 2026-10-23T01:22:02+00:00_ — statuses change only with the evidence named in each row.

## Verified (30)

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
- **S3.B1-trunk** — Trunk read-back (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B2-svi-routing** — L3 switch SVI + ip routing (l3_switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B3-etherchannel** — EtherChannel (LACP) (switch, l3_switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B4-stp** — STP root/PortFast/BPDU guard (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B5-port-security** — Port-security (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B6-vtp** — VTP domain/mode (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B7-voice-vlan** — Voice VLAN (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.B8-mgmt-ip** — Switch management IP (switch). Evidence: `probes/live/switch-probes.md`.
- **S3.C1-address-scoped-acl** — Address-scoped ASA ACL (firewall:5506-X, firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced`.
- **S3.E1-ap-association** — AccessPoint-PT association (access_point, laptop). Evidence: `tests/autopilot/test_live_connectivity.py`.
- **S3.F1-cable-modem** — Cable modem (modem, cloud, pc). Evidence: `library/T06-home-cable.json (live runner pass run-d0c3c88b4e28)`.
- **S3.Z1-capability-map** — Capability Map publish + refusal review (docs). Evidence: `docs/capability-map.md (live capability map publish)`.

## Unverified (34)

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
- **S3.A15-ssh** — SSH and local AAA (router). Evidence: `probes/raw/S3.A15-router.txt (server config read back; PC SSH login not observable)`.
- **S3.A16-ntp-syslog-snmp** — NTP, Syslog, SNMP (router). Evidence: `probes/raw/S3.A16-router.txt (router config read back; server effect unobservable)`.
- **S3.C2-dmz-zone** — ASA DMZ third zone (firewall:5506-X). Evidence: `probes/asa.md`.
- **S3.C3-asa-nat** — ASA object NAT/PAT (firewall:5506-X). Evidence: `probes/asa.md`.
- **S3.C4-asa-routes** — ASA static/default routes (firewall:5506-X). Evidence: `probes/asa.md`.
- **S3.C5-asa-dhcp** — ASA DHCP server (firewall:5506-X). Evidence: `probes/asa.md`.
- **S3.C6-object-group** — ASA object groups (firewall:5506-X). Evidence: `probes/asa.md`.
- **S3.D1-app-dns-http** — App-level DNS/HTTP proof (server, pc). Evidence: `probes/server-services.md`.
- **S3.E2-ssid-wpa2** — Custom SSID / WPA2 (access_point). Evidence: `probes/wireless.md`.
- **S3.E3-phone-tablet** — Smartphone/tablet wireless clients (smartphone, tablet). Evidence: `probes/wireless.md`.
- **S3.E4-wlc** — WLC + lightweight APs (wlc, access_point). Evidence: `probes/wireless.md`.
- **S3.E5-multi-ssid** — Multiple SSIDs to VLANs (access_point). Evidence: `probes/wireless.md`.
- **S3.F2-cloud-mapping** — Explicit cloud mapping driver (cloud, modem). Evidence: `probes/cloud.md`.
- **S3.F3-isp-template** — ISP template (cloud, modem, router). Evidence: `probes/cloud.md`.
- **S3.F4-cloud-serial** — Cloud serial / Frame Relay (cloud). Evidence: `probes/cloud.md`.
- **S3.H1-end-devices** — Printer/IP phone/tablet/smartphone endpoints (printer, ip_phone, tablet, smartphone). Evidence: `probes/end-devices.md`.
- **S3.H2-host-dns** — Host DNS setting (pc, laptop, server). Evidence: `probes/end-devices.md`.

## Refused (15)

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
- **S3.D0-server-api** — Server-PT services API probe (server). Evidence: `probes/server-services.md`.
  Refusal: No service-configuration method is exposed: getProcess() rejects every service name and the prototype has no DNS/DHCP/HTTP/email/FTP method. The limitation is the MCP/Script-Engine API layer; configure services in the PT GUI.
- **S3.D2-dns** — DNS service (server). Evidence: `probes/server-services.md`.
  Refusal: No Script-Engine API to add DNS records.
- **S3.D3-http** — HTTP/HTTPS service (server). Evidence: `probes/server-services.md`.
  Refusal: No Script-Engine API to enable HTTP or set content.
- **S3.D4-server-dhcp** — DHCP server (Server-PT) (server). Evidence: `probes/server-services.md`.
  Refusal: No Script-Engine API to add pools. Use an IOS router pool instead (supported, incl. S3.A7 relay).
- **S3.D5-email** — Email service (server). Evidence: `probes/server-services.md`.
  Refusal: No Script-Engine API for email users/domain.
- **S3.D6-ftp** — FTP service (server). Evidence: `probes/server-services.md`.
  Refusal: No Script-Engine API for FTP users/files.
- **S3.D7-aaa** — AAA (RADIUS/TACACS) server (server, router). Evidence: `probes/server-services.md`.
  Refusal: No API to configure the AAA server; router-side AAA can be rendered but cannot be proven without the server.
- **S3.D8-ntp-syslog-server** — NTP/Syslog server (server). Evidence: `probes/server-services.md`.
  Refusal: No API to enable the service; router-side NTP/Syslog is supported but the server effect is unobservable.
- **S3.G1-iot-probe** — IoT registration API probe (iot). Evidence: `probes/iot.md`.
  Refusal: The Smart LED prototype has no server-address/registration method; digitalWrite is not registration. Configure in the PT GUI.
- **S3.G2-iot-control** — IoT registration + control (iot). Evidence: `probes/iot.md`.
  Refusal: Registration is the prerequisite and has no API, so a registered on/off control cannot be proven.
