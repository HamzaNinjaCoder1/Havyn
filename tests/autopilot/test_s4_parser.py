"""Stage 4 parser tests (S4.3)."""

from __future__ import annotations

import json
from pathlib import Path

from network_autopilot.brownfield.parser import (
    IosConfigParser,
    acl_rule_matches,
    semantic_projection,
)

LIBRARY = Path(__file__).resolve().parents[2] / "library"

IOS = """\
Building configuration...

Current configuration : 2200 bytes
!
hostname edge1
!
vlan 10
 name USERS
 exit
vlan 20
 name GUESTS
 exit
!
interface GigabitEthernet0/0
 no shutdown
 exit
interface GigabitEthernet0/0.10
 encapsulation dot1Q 10
 ip address 10.0.10.1 255.255.255.0
 exit
interface GigabitEthernet0/0.20
 encapsulation dot1Q 20
 ip address 10.0.20.1 255.255.255.0
 exit
interface FastEthernet0/1
 switchport mode access
 switchport access vlan 10
 exit
interface FastEthernet0/2
 switchport mode trunk
 switchport trunk native vlan 99
 switchport trunk allowed vlan 10,20
 exit
!
ip route 10.9.0.0 255.255.255.0 10.0.20.2
!
router ospf 1
 network 10.0.10.0 0.0.0.255 area 0
 default-information originate
 exit
router eigrp 7
 network 10.0.99.0 0.0.0.255
 passive-interface FastEthernet0/1
 exit
router rip
 version 2
 network 10.0.98.0
 no auto-summary
 exit
!
ip access-list extended WEB
 permit tcp 10.0.10.0 0.0.0.255 any eq 80
 deny ip any any
 exit
access-list 10 permit 10.0.10.0 0.0.0.255
interface FastEthernet0/3
 ip access-group WEB in
 exit
!
ip nat inside source list 1 interface GigabitEthernet0/0 overload
!
ip dhcp pool USERS
 network 10.0.10.0 255.255.255.0
 default-router 10.0.10.1
 dns-server 10.0.10.53
 exit
ip dhcp excluded-address 10.0.10.1 10.0.10.10
!
interface GigabitEthernet0/1
 standby 1 ip 10.0.20.254
 standby 1 priority 110
 standby 1 preempt
 exit
!
end
"""


def test_ios_parser_structures_and_traceability() -> None:
    parsed = IosConfigParser().parse(IOS, device="edge1")
    assert parsed.globals.hostname == "edge1"
    assert parsed.vlans[10].name == "USERS"
    assert parsed.vlans[20].name == "GUESTS"
    sub = parsed.interface("GigabitEthernet0/0.10")
    assert sub is not None
    assert sub.ip == "10.0.10.1"
    assert sub.encapsulation_vlan == 10
    assert sub.subinterface_of == "GigabitEthernet0/0"
    access = parsed.interface("FastEthernet0/1")
    assert access is not None
    assert access.access_vlan == 10
    trunk = parsed.interface("FastEthernet0/2")
    assert trunk is not None
    assert trunk.switchport_mode == "trunk"
    assert trunk.trunk_allowed == [10, 20]
    assert trunk.trunk_native == 99
    assert [(r.network, r.next_hop) for r in parsed.static_routes] == [
        ("10.9.0.0", "10.0.20.2")
    ]
    ospf = next(p for p in parsed.routing_protocols if p.protocol == "ospf")
    assert ospf.networks[0].area == "0"
    assert ospf.default_information is True
    assert parsed.acl("WEB") is not None
    assert len(parsed.acl("WEB").rules) == 2
    assert parsed.interface("FastEthernet0/3").acl_in == "WEB"
    assert parsed.dhcp_pools["USERS"].default_router == ["10.0.10.1"]
    assert parsed.interface("GigabitEthernet0/1").standby[0]["ip"] == "10.0.20.254"
    # Every parsed line keeps its original line number.
    assert all(line.line_no > 0 for line in parsed.lines)
    assert parsed.unparsed == []


def test_parser_preserves_unparsed_statements() -> None:
    raw = """\
hostname r1
!
ip policy route-map DOOM
route-map DOOM permit 10
 match ip address 1
 exit
banner motd ^C no ^C
!
end
"""
    parsed = IosConfigParser().parse(raw, device="r1")
    texts = [line.text for line in parsed.unparsed]
    assert "ip policy route-map DOOM" in texts
    assert "route-map DOOM permit 10" in texts
    assert "match ip address 1" in texts
    # A banner is known boilerplate: preserved in `ignored`, not dropped.
    assert any("banner motd" in line.text for line in parsed.ignored)
    # Partial parsing is explicit, not silent.
    assert parsed.status.value == "partial"


def test_asa_parser_reads_zones_policy_and_route() -> None:
    raw = """\
ASA Version 9.6(1)
!
hostname asa1
!
interface GigabitEthernet1/1
 nameif inside
 security-level 100
 ip address 10.0.1.1 255.255.255.0
 exit
interface GigabitEthernet1/2
 nameif outside
 security-level 0
 ip address 203.0.113.1 255.255.255.0
 exit
!
access-list OUTSIDE-IN extended permit icmp any any echo-reply
access-list OUTSIDE-IN extended deny ip any any
access-group OUTSIDE-IN in interface outside
!
route outside 0.0.0.0 0.0.0.0 203.0.113.2 1
!
end
"""
    parsed = IosConfigParser().parse(raw, device="asa1")
    assert parsed.platform.value == "asa"
    inside = parsed.interface("GigabitEthernet1/1")
    assert inside.nameif == "inside"
    assert inside.security_level == 100
    assert parsed.globals.acl_bindings[0]["interface"] == "outside"
    assert parsed.static_routes[0].cidr == "0.0.0.0/0"
    assert parsed.static_routes[0].next_hop == "203.0.113.2"


def test_acl_matching_wildcard_and_host() -> None:
    parsed = IosConfigParser().parse(IOS, device="edge1")
    acl = parsed.acl("WEB")
    permit, deny = acl.rules
    assert acl_rule_matches(permit, "10.0.10.5", "198.51.100.1", "tcp")
    assert not acl_rule_matches(permit, "10.0.11.5", "198.51.100.1", "tcp")
    assert acl_rule_matches(deny, "10.0.11.5", "198.51.100.1", "tcp")


def test_parser_reports_contradictions() -> None:
    raw = """\
hostname r1
!
interface GigabitEthernet0/0
 ip address 10.0.0.1 255.255.255.0
 exit
interface GigabitEthernet0/1
 ip address 10.0.0.1 255.255.255.0
 ip access-group MISSING in
 exit
interface GigabitEthernet0/9.5
 ip address 10.0.5.1 255.255.255.0
 exit
end
"""
    parsed = IosConfigParser().parse(raw, device="r1")
    codes = {issue.code for issue in parsed.issues}
    assert "duplicate-ip" in codes
    assert "acl-binding-missing" in codes
    assert "orphan-subinterface" in codes


def test_parse_compiled_library_configs() -> None:
    """Configs generated by the compiler for real library specs parse cleanly."""
    from network_autopilot.core.network_spec import NetworkSpec
    from network_autopilot.brownfield.simulated import SimulatedNetwork

    for name in ("T01-small-office.json", "T02-two-vlan-office.json",
                 "T09-ospf-backbone.json", "T10-nat-internet.json"):
        spec = NetworkSpec.from_json((LIBRARY / name).read_text(encoding="utf-8"))
        network = SimulatedNetwork(spec)
        for device_name, device in network.devices.items():
            if device.is_host:
                continue
            parsed = IosConfigParser().parse(
                network.render_config(device_name), device=device_name
            )
            assert parsed.unparsed == [], (name, device_name, [
                line.text for line in parsed.unparsed
            ])
            assert semantic_projection(parsed)["interfaces"] is not None
