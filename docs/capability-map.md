# Havyn Capability Map

_generated 2026-10-22T02:26:24+00:00_ — statuses change only with the evidence named in each row.

## Latest live runner evidence

| topology | status | run | elapsed (s) | evidence |
| --- | --- | --- | --- | --- |
| T01 | pass | run-7e254d7e7656 | 85.2 | autopilot_evidence\build-19145d4547ca |
| T02 | pass | run-8f78ca064894 | 96.9 | autopilot_evidence\build-eee1031f7df2 |
| T06 | pass | run-c4edb6e574c5 | 60.2 | autopilot_evidence\build-dfda411c725d |

## Verified (9)

- **router-acl-enforce** — IOS router numbered ACLs with address matching (router). Evidence: `tests/autopilot/test_live_policy.py::test_router_acl_renders_a_cidr_operand_and_enforces_it`.
- **asa-wildcard-permit** — ASA wildcard permit on the outside nameif passes the protocol (firewall:5506-X, firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced`.
- **asa-5505-svi-readback** — ASA 5505 Vlan SVIs read back pushed addresses (firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_5505_vlan_addresses_read_back_after_the_push`.
- **wireless-association** — Wireless clients associate automatically (Antenna link) (laptop, accesspoint). Evidence: `tests/autopilot/test_live_connectivity.py (wireless area: laptop associates, then reaches across the AP)`.
- **flow-reachable-blocked** — ICMP flows verify reachable and blocked end to end (router, switch, pc, firewall). Evidence: `tests/autopilot/test_live_connectivity.py`.
- **asa-address-scoped** — Address-scoped ASA rules (per-host/per-subnet permits) (firewall:5506-X, firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced`.
- **small-office-dhcp** — Single-router office with DHCP (router, switch, pc). Evidence: `library/T01-small-office.json (builds offline; live runner pass promotes this row); live runner pass run-7e254d7e7656 (autopilot_evidence\build-19145d4547ca)`.
  Latest live run `run-7e254d7e7656`: pass (85.2 s, `autopilot_evidence\build-19145d4547ca`).
  Status derived from that complete live runner pass.
- **router-on-a-stick** — Router-on-a-stick inter-VLAN routing (router, switch, pc). Evidence: `library/T02-two-vlan-office.json (builds offline; live runner pass promotes this row); live runner pass run-8f78ca064894 (autopilot_evidence\build-eee1031f7df2)`.
  Latest live run `run-8f78ca064894`: pass (96.9 s, `autopilot_evidence\build-eee1031f7df2`).
  Status derived from that complete live runner pass.
- **cable-modem-home** — Home cable-modem network (modem, router, pc). Evidence: `library/T06-home-cable.json (builds offline; live runner pass promotes this row); live runner pass run-c4edb6e574c5 (autopilot_evidence\build-dfda411c725d)`.
  Latest live run `run-c4edb6e574c5`: pass (60.2 s, `autopilot_evidence\build-dfda411c725d`).
  Status derived from that complete live runner pass.

## Unverified (14)

- **hsrp-failover** — HSRP gateway failover (router, l3_switch). Evidence: `library/T15-redundant-core.json (pending: no compiler render path; physical topology builds, redundancy unverified)`.
- **etherchannel** — EtherChannel bundling (switch, l3_switch). Evidence: `library/T15-redundant-core.json (pending: no compiler render path; physical topology builds, redundancy unverified)`.
- **ospf-backbone** — OSPF single-area routing (router). Evidence: `library/T09-ospf-backbone.json (offline lint+compile pass; no live lab yet)`.
- **nat-pat** — NAT/PAT internet access (router). Evidence: `library/T10-nat-internet.json (offline lint+compile pass; no live lab yet)`.
- **asa-dmz** — ASA three-zone DMZ policy (firewall:5506-X). Evidence: `library/T08-asa-dmz.json (pending S1.16 live proof)`.
- **campus-l3-core** — Three-floor campus with an L3 core (l3_switch, switch, pc). Evidence: `library/T03-campus-l3-core.json (builds offline; no live runner pass yet)`.
- **serial-wan** — HQ + branch over a serial WAN (router). Evidence: `library/T04-serial-wan.json (builds offline; no live runner pass yet)`.
- **dsl-internet** — DSL access through a cloud/modem pair (router, cloud, modem). Evidence: `library/T05-dsl-internet.json (builds offline; no live runner pass yet)`.
- **asa-perimeter** — ASA inside/outside perimeter (firewall:5506-X, firewall:5505). Evidence: `library/T07-asa-perimeter.json (pending S1.16 live proof)`.
- **wireless-office** — Wireless office with AP-attached laptops and phones (accesspoint, laptop, smartphone). Evidence: `library/T11-wireless-office.json (builds offline; no live runner pass yet)`.
- **linksys-home** — Linksys home gateway (home_router, pc). Evidence: `library/T12-linksys-home.json (builds offline; no live runner pass yet)`.
- **server-farm** — DNS + HTTP + DHCP server farm (server, switch, pc). Evidence: `library/T13-server-farm.json (builds offline; no live runner pass yet)`.
- **iot-smart-home** — IoT smart-home devices (iot, home_router). Evidence: `library/T14-iot-home.json (builds offline; no live runner pass yet)`.
- **enterprise-campus** — Enterprise campus reference build (router, switch, firewall, server, pc). Evidence: `library/T16-enterprise-campus.json (builds offline; no live runner pass in this library)`.

## Refused (3)

- **acl-icmp-port** — dest_port on an icmp ACL rule (firewall, router). Evidence: `tests/autopilot/test_asa_policy.py::test_dest_port_on_icmp_is_refused_before_pt_aborts_the_block`.
  Refusal: PT rejects the line and configureIosDevice aborts the rest of the block, which would cost the interface addresses.
- **asa-5505-third-zone** — Third security zone on the ASA 5505 (firewall:5505). Evidence: `tests/autopilot/test_asa_policy.py::test_5505_refuses_more_than_two_zones`.
  Refusal: PT's 5505 only exposes Vlan1/Vlan2; a third zone cannot be configured. Use a 5506-X.
- **policy-on-non-enforcer** — ACL policy on a device that enforces nothing (hosts, switches) (pc, server, switch). Evidence: `tests/autopilot/test_asa_policy.py::test_policy_on_a_device_with_no_enforcement_path_fails_the_build`.
  Refusal: The policy would compile and then do nothing. Only ASA firewalls and IOS routers enforce.
