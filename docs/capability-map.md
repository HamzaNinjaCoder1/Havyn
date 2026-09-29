# Havyn Capability Map

_generated 2026-09-29T17:17:16+00:00_ — statuses change only with the evidence named in each row.

## Verified (6)

- **router-acl-enforce** — IOS router numbered ACLs with address matching (router). Evidence: `tests/autopilot/test_live_policy.py::test_router_acl_renders_a_cidr_operand_and_enforces_it`.
- **asa-wildcard-permit** — ASA wildcard permit on the outside nameif passes the protocol (firewall:5506-X, firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced`.
- **asa-5505-svi-readback** — ASA 5505 Vlan SVIs read back pushed addresses (firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_5505_vlan_addresses_read_back_after_the_push`.
- **wireless-association** — Wireless clients associate automatically (Antenna link) (laptop, accesspoint). Evidence: `tests/autopilot/test_live_connectivity.py (wireless area: laptop associates, then reaches across the AP)`.
- **flow-reachable-blocked** — ICMP flows verify reachable and blocked end to end (router, switch, pc, firewall). Evidence: `tests/autopilot/test_live_connectivity.py`.
- **asa-address-scoped** — Address-scoped ASA rules (per-host/per-subnet permits) (firewall:5506-X, firewall:5505). Evidence: `tests/autopilot/test_live_policy.py::test_asa_address_scoped_acl_is_enforced`.

## Unverified (5)

- **hsrp-failover** — HSRP gateway failover (router, l3_switch). Evidence: `library/T15-redundant-core.json (pending: no compiler render path; physical topology builds, redundancy unverified)`.
- **etherchannel** — EtherChannel bundling (switch, l3_switch). Evidence: `library/T15-redundant-core.json (pending: no compiler render path; physical topology builds, redundancy unverified)`.
- **ospf-backbone** — OSPF single-area routing (router). Evidence: `library/T09-ospf-backbone.json (offline lint+compile pass; no live lab yet)`.
- **nat-pat** — NAT/PAT internet access (router). Evidence: `library/T10-nat-internet.json (offline lint+compile pass; no live lab yet)`.
- **asa-dmz** — ASA three-zone DMZ policy (firewall:5506-X). Evidence: `library/T08-asa-dmz.json (pending S1.16 live proof)`.

## Refused (3)

- **acl-icmp-port** — dest_port on an icmp ACL rule (firewall, router). Evidence: `tests/autopilot/test_asa_policy.py::test_dest_port_on_icmp_is_refused_before_pt_aborts_the_block`.
  Refusal: PT rejects the line and configureIosDevice aborts the rest of the block, which would cost the interface addresses.
- **asa-5505-third-zone** — Third security zone on the ASA 5505 (firewall:5505). Evidence: `tests/autopilot/test_asa_policy.py::test_5505_refuses_more_than_two_zones`.
  Refusal: PT's 5505 only exposes Vlan1/Vlan2; a third zone cannot be configured. Use a 5506-X.
- **policy-on-non-enforcer** — ACL policy on a device that enforces nothing (hosts, switches) (pc, server, switch). Evidence: `tests/autopilot/test_asa_policy.py::test_policy_on_a_device_with_no_enforcement_path_fails_the_build`.
  Refusal: The policy would compile and then do nothing. Only ASA firewalls and IOS routers enforce.
