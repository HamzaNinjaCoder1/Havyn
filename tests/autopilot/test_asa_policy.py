"""ASA policy compilation, offline.

Packet Tracer's ASA does not implement a real firewall's policy engine, and
these tests pin the limits that were measured live rather than assumed:

* with no access-group every flow is blocked, both directions;
* ``permit <proto> any any`` on the ``outside`` nameif passes that protocol
  both ways, and enforcement IS protocol-selective;
* an access-group on ``inside`` passes nothing;
* address-scoped rules ARE enforced: S1.16's live A/B/C probe bound a permit
  for the matching pair (the flow passed) and a permit for a non-matching
  pair (the flow stayed blocked), which is address matching. The compiler
  therefore renders address-scoped ASA rules in the ASA's netmask syntax
  instead of refusing them; only junk operands and a bogus netmask are
  refused, because PT would accept those and match nothing.
"""

from __future__ import annotations

import copy

import pytest

from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler.compiler import CompileError
from src.network_autopilot.compiler.firewall_policy import (
    ENFORCING_NAMEIF,
    PolicyCompileError,
    build_policy_commands,
    build_router_policy_commands,
    describe_enforcement,
    policies_for_device,
    rule_to_cli,
    rule_to_ios_cli,
)
from src.network_autopilot.core.network_spec import AclRuleSpec, NetworkSpec
from src.network_autopilot.ipam import plan_addressing

ASA_LAB = {
    "name": "asa-policy-lab",
    "devices": [
        {"id": "fw", "role": "router", "model_hint": "5506-X"},
        {"id": "r1", "role": "router", "model_hint": "2911"},
        {"id": "pc-a", "role": "pc"},
        {"id": "pc-b", "role": "pc"},
    ],
    "links": [
        {"id": "l-in", "a": {"device": "fw"}, "b": {"device": "pc-a"}},
        {"id": "l-out", "a": {"device": "fw"}, "b": {"device": "pc-b"}},
        {"id": "l-r", "a": {"device": "r1"}, "b": {"device": "pc-b"}},
    ],
    "subnets": [
        {"id": "net-in", "cidr": "192.168.10.0/24"},
        {"id": "net-out", "cidr": "192.168.20.0/24"},
    ],
    "ip_assignments": [
        {"device": "fw", "subnet": "net-in", "ip": "192.168.10.1"},
        {"device": "fw", "subnet": "net-out", "ip": "192.168.20.1"},
        {"device": "pc-a", "subnet": "net-in", "ip": "192.168.10.10"},
        {"device": "pc-b", "subnet": "net-out", "ip": "192.168.20.10"},
    ],
    "routing": {"mode": "connected"},
}


def _spec(**overrides) -> NetworkSpec:
    raw = copy.deepcopy(ASA_LAB)
    raw.update(copy.deepcopy(overrides))
    return NetworkSpec.model_validate(raw)


def _compile(spec: NetworkSpec):
    return compile_spec(spec, plan_addressing(spec))


def _ios_commands(spec: NetworkSpec, device: str = "fw") -> list[str]:
    compiled = _compile(spec)
    for node in compiled.dag.nodes.values():
        op = node.operation
        if op.operation_type == "configure_ios" and op.inputs.get("device") == device:
            return list(op.inputs["commands"])
    raise AssertionError(f"no configure_ios op for {device}")


# -- rule shape ---------------------------------------------------------------


def test_wildcard_rule_renders_the_shape_proven_live():
    rule = AclRuleSpec(action="permit", protocol="icmp", source="any",
                       destination="any")
    assert rule_to_cli("p", 0, rule) == "access-list p extended permit icmp any any"


def test_deny_rule_keeps_its_action():
    rule = AclRuleSpec(action="deny", protocol="ip", source="any",
                       destination="any")
    assert rule_to_cli("p", 0, rule) == "access-list p extended deny ip any any"


def test_dest_port_is_rendered_for_a_transport_protocol():
    rule = AclRuleSpec(action="permit", protocol="tcp", source="any",
                       destination="any", dest_port=443)
    assert rule_to_cli("p", 0, rule) == (
        "access-list p extended permit tcp any any eq 443"
    )


def test_each_protocol_maps_to_its_own_cli_keyword():
    for proto in ("ip", "icmp", "tcp", "udp"):
        rule = AclRuleSpec(action="permit", protocol=proto, source="any",
                           destination="any")
        assert rule_to_cli("p", 0, rule).split()[4] == proto


# -- address-scoped rules ARE enforced (S1.16 live probe A/B/C) ---------------


def test_host_scoped_source_renders_the_asa_host_form():
    """S1.16 live A/B/C: a permit for the pair passes, a permit for a
    non-matching pair does not - PT's ASA matches addresses. So a host-scoped
    rule renders instead of being refused."""
    rule = AclRuleSpec(action="permit", protocol="icmp", source="192.168.10.10",
                       destination="any")
    assert rule_to_cli("p", 0, rule) == (
        "access-list p extended permit icmp host 192.168.10.10 any"
    )


def test_subnet_scoped_destination_renders_the_asa_netmask_form():
    rule = AclRuleSpec(action="permit", protocol="icmp", source="any",
                       destination="192.168.20.0 255.255.255.0")
    assert rule_to_cli("p", 0, rule) == (
        "access-list p extended permit icmp any 192.168.20.0 255.255.255.0"
    )


def test_deny_with_a_host_source_renders_too():
    rule = AclRuleSpec(action="deny", protocol="icmp", source="10.0.0.5",
                       destination="any")
    assert rule_to_cli("p", 0, rule) == (
        "access-list p extended deny icmp host 10.0.0.5 any"
    )


def test_asa_accepts_cidr_netmask_host_and_wildcard_operand_forms():
    cidr = AclRuleSpec(action="permit", protocol="ip",
                       source="10.0.0.0/8", destination="any")
    assert rule_to_cli("p", 0, cidr) == (
        "access-list p extended permit ip 10.0.0.0 255.0.0.0 any"
    )
    netmask_host = AclRuleSpec(action="permit", protocol="icmp",
                               source="10.8.8.2 255.255.255.255",
                               destination="any")
    assert rule_to_cli("p", 1, netmask_host) == (
        "access-list p extended permit icmp host 10.8.8.2 any"
    )
    wildcard = AclRuleSpec(action="permit", protocol="ip",
                           source="10.0.0.0 0.0.0.255", destination="any")
    assert rule_to_cli("p", 2, wildcard) == (
        "access-list p extended permit ip 10.0.0.0 255.255.255.0 any"
    )


def test_asa_address_junk_and_bogus_netmask_are_refused():
    junk = AclRuleSpec(action="permit", protocol="ip",
                       source="not-an-address", destination="any")
    with pytest.raises(PolicyCompileError):
        rule_to_cli("p", 0, junk)
    bogus = AclRuleSpec(action="permit", protocol="ip",
                        source="10.0.0.0 255.0.255.0", destination="any")
    with pytest.raises(PolicyCompileError) as exc:
        rule_to_cli("p", 0, bogus)
    assert "not a valid netmask" in str(exc.value)


def test_wildcard_in_long_form_is_accepted():
    rule = AclRuleSpec(action="permit", protocol="ip", source="0.0.0.0 0.0.0.0",
                       destination="any")
    assert rule_to_cli("p", 0, rule).endswith("permit ip any any")


def test_dest_port_on_icmp_is_refused_before_pt_aborts_the_block():
    """configureIosDevice aborts at the first invalid line, so a bad line here
    would cost the interface addresses emitted above it."""
    rule = AclRuleSpec(action="permit", protocol="icmp", source="any",
                       destination="any", dest_port=80)
    with pytest.raises(PolicyCompileError) as exc:
        rule_to_cli("p", 0, rule)
    assert "abort" in str(exc.value)


# -- target selection ---------------------------------------------------------


def test_policy_is_bound_to_the_device_the_spec_named():
    spec = _spec(policies=[{
        "id": "fwpolicy", "apply_to": ["fw"],
        "rules": [{"action": "permit", "protocol": "icmp",
                   "source": "any", "destination": "any"}],
    }])
    assert [p.id for p in policies_for_device(spec, "fw")] == ["fwpolicy"]
    assert policies_for_device(spec, "r1") == []


def test_planned_policies_are_carried_but_not_enforced():
    """'planned' is the spec's own honesty switch: the engine must leave it
    alone so verification can report it UNKNOWN rather than satisfied."""
    spec = _spec(policies=[{
        "id": "planned-pol", "apply_to": ["fw"], "enforcement": "planned",
        "rules": [{"action": "permit", "protocol": "icmp",
                   "source": "10.1.1.1", "destination": "any"}],
    }])
    assert policies_for_device(spec, "fw") == []
    notes = list(describe_enforcement(spec))
    assert any("planned" in n for n in notes)


def test_policy_on_a_device_with_no_enforcement_path_fails_the_build():
    """Routers are verified enforcers since #16; a host is not."""
    spec = _spec(policies=[{
        "id": "onhost", "apply_to": ["pc-a"],
        "rules": [{"action": "permit", "protocol": "icmp",
                   "source": "any", "destination": "any"}],
    }])
    with pytest.raises(CompileError) as exc:
        _compile(spec)
    assert "not an ASA firewall" in str(exc.value)


def test_address_scoped_policy_compiles_and_emits_the_scoped_line():
    """S1.16 live A/B/C: PT's ASA matches addresses, so an address-scoped
    policy compiles to a scoped line instead of failing the build."""
    spec = _spec(policies=[{
        "id": "toospecific", "apply_to": ["fw"],
        "rules": [{"action": "permit", "protocol": "icmp",
                   "source": "192.168.10.10", "destination": "192.168.20.10"}],
    }])
    cmds = _ios_commands(spec)
    assert (
        "access-list toospecific extended permit icmp "
        "host 192.168.10.10 host 192.168.20.10"
    ) in cmds
    assert f"access-group toospecific in interface {ENFORCING_NAMEIF}" in cmds


# -- end-to-end command emission ----------------------------------------------


def test_policy_binds_to_the_outside_nameif():
    spec = _spec(policies=[{
        "id": "fwpolicy", "apply_to": ["fw"],
        "rules": [{"action": "permit", "protocol": "tcp",
                   "source": "any", "destination": "any"}],
    }])
    cmds = _ios_commands(spec)
    assert "access-list fwpolicy extended permit tcp any any" in cmds
    assert f"access-group fwpolicy in interface {ENFORCING_NAMEIF}" in cmds


def test_policy_replaces_the_default_icmp_permit_rather_than_widening_it():
    """The default permit exists so an unconfigured ASA does not black-hole the
    topology. When the spec states a policy, keeping the default too would
    silently re-allow exactly what the policy was written to stop."""
    spec = _spec(policies=[{
        "id": "fwpolicy", "apply_to": ["fw"],
        "rules": [{"action": "permit", "protocol": "tcp",
                   "source": "any", "destination": "any"}],
    }])
    cmds = _ios_commands(spec)
    assert not any("permit icmp any any" in c for c in cmds)


def test_without_a_policy_the_default_icmp_permit_is_still_emitted():
    spec = _spec()
    cmds = _ios_commands(spec)
    assert "access-list out_in extended permit icmp any any" in cmds
    assert f"access-group out_in in interface {ENFORCING_NAMEIF}" in cmds


def test_access_group_is_bound_after_the_list_is_defined():
    """configureIosDevice aborts at the first bad line; binding an access-group
    to an access-list that does not exist yet would leave the interface
    addresses above it unapplied."""
    spec = _spec(policies=[{
        "id": "ordered", "apply_to": ["fw"],
        "rules": [{"action": "permit", "protocol": "udp",
                   "source": "any", "destination": "any"}],
    }])
    cmds = _ios_commands(spec)
    assert cmds.index("access-list ordered extended permit udp any any") < cmds.index(
        f"access-group ordered in interface {ENFORCING_NAMEIF}"
    )


def test_interface_addresses_precede_the_acl():
    """Ordering that protects the addresses: the first interface block must come
    before any access-list work, so a later ACL problem cannot cost them."""
    spec = _spec(policies=[{
        "id": "fwpolicy", "apply_to": ["fw"],
        "rules": [{"action": "permit", "protocol": "icmp",
                   "source": "any", "destination": "any"}],
    }])
    cmds = _ios_commands(spec)
    assert cmds.index("interface GigabitEthernet1/1") < cmds.index(
        "access-list fwpolicy extended permit icmp any any"
    )


def test_build_policy_commands_is_empty_without_a_policy():
    spec = _spec()
    assert build_policy_commands(spec, "fw") == ([], [])


# -- model-specific behaviour -------------------------------------------------


def test_5505_policy_lands_on_its_vlan2_nameif():
    """The 5505 carries addresses on Vlan SVIs, not on its Ethernet
    switchports, so the interface block differs from the 5506-X even though
    the policy binding is identical."""
    raw = {k: v for k, v in ASA_LAB.items()}
    raw["name"] = "asa-5505-policy"
    raw["devices"] = [dict(d) for d in ASA_LAB["devices"]]
    raw["devices"][0] = {"id": "fw", "role": "router", "model_hint": "5505"}
    raw["links"] = [
        {"id": "l-in", "a": {"device": "fw", "interface_hint": "Ethernet0/0"},
         "b": {"device": "pc-a"}},
        {"id": "l-out", "a": {"device": "fw", "interface_hint": "Ethernet0/1"},
         "b": {"device": "pc-b"}},
    ]
    raw["subnets"] = [dict(s) for s in ASA_LAB["subnets"]]
    raw["ip_assignments"] = [dict(a) for a in ASA_LAB["ip_assignments"]]
    raw["ip_assignments"][0]["interface_hint"] = "Ethernet0/0"
    raw["ip_assignments"][1]["interface_hint"] = "Ethernet0/1"
    raw["policies"] = [{
        "id": "fwpolicy", "apply_to": ["fw"],
        "rules": [{"action": "permit", "protocol": "icmp",
                   "source": "any", "destination": "any"}],
    }]
    spec = NetworkSpec.model_validate(raw)
    cmds = _ios_commands(spec)
    # the switchport join, then the SVI that carries the address
    assert "interface Ethernet0/0" in cmds
    assert "switchport access vlan 1" in cmds
    assert "interface Vlan1" in cmds
    assert "interface Vlan2" in cmds
    assert "switchport access vlan 2" in cmds
    assert f"access-group fwpolicy in interface {ENFORCING_NAMEIF}" in cmds


def test_5505_refuses_more_than_two_zones():
    """PT's 5505 only exposes Vlan1/Vlan2, so a third zone cannot be configured
    and the build must say so instead of emitting a config that half-applies.

    The third zone's peer must be a HOST, not a router: since 9848c9f a
    5505-Ethernet <-> router link is refused even earlier, at port preflight
    (`unbuildable_link`), which would shadow this zone refusal. A 5505 <-> host
    link is buildable (measured working), so preflight passes and the zone
    limit is what names the failure."""
    raw = {k: v for k, v in ASA_LAB.items()}
    raw["name"] = "asa-5505-three"
    raw["devices"] = [dict(d) for d in ASA_LAB["devices"]]
    raw["devices"][0] = {"id": "fw", "role": "router", "model_hint": "5505"}
    # host peer for the third zone (router peer would trip unbuildable_link first)
    raw["devices"].append({"id": "pc-c", "role": "pc"})
    raw["links"] = [
        {"id": "l-in", "a": {"device": "fw", "interface_hint": "Ethernet0/0"},
         "b": {"device": "pc-a"}},
        {"id": "l-out", "a": {"device": "fw", "interface_hint": "Ethernet0/1"},
         "b": {"device": "pc-b"}},
        {"id": "l-third", "a": {"device": "fw", "interface_hint": "Ethernet0/2"},
         "b": {"device": "pc-c"}},
    ]
    raw["subnets"] = [
        {"id": "net-in", "cidr": "192.168.10.0/24"},
        {"id": "net-out", "cidr": "192.168.20.0/24"},
        {"id": "net-third", "cidr": "192.168.30.0/24"},
    ]
    raw["ip_assignments"] = [
        {"device": "fw", "subnet": "net-in", "ip": "192.168.10.1",
         "interface_hint": "Ethernet0/0"},
        {"device": "fw", "subnet": "net-out", "ip": "192.168.20.1",
         "interface_hint": "Ethernet0/1"},
        {"device": "fw", "subnet": "net-third", "ip": "192.168.30.1",
         "interface_hint": "Ethernet0/2"},
        {"device": "pc-a", "subnet": "net-in", "ip": "192.168.10.10"},
        {"device": "pc-b", "subnet": "net-out", "ip": "192.168.20.10"},
    ]
    spec = NetworkSpec.model_validate(raw)
    with pytest.raises(CompileError) as exc:
        _compile(spec)
    assert "Vlan1/Vlan2" in str(exc.value)


# -- router policies (#16): IOS numbered ACLs, address matching allowed -------


def _router_policy_spec(rules, *, apply_to="r1", address_r1=True):
    raw = copy.deepcopy(ASA_LAB)
    raw["name"] = "router-policy-lab"
    raw["policies"] = [{
        "id": "rtrpolicy", "apply_to": [apply_to], "rules": rules,
    }]
    if address_r1:
        raw["ip_assignments"] = raw["ip_assignments"] + [
            {"device": "r1", "subnet": "net-out", "ip": "192.168.20.2"}
        ]
    return NetworkSpec.model_validate(raw)


def test_policy_on_a_router_compiles_and_binds_an_inbound_acl():
    """#16: the build used to refuse router targets with 'PT enforces
    access-lists only on the ASA'. A router policy must compile and emit
    both the numbered ACL definition and the interface binding."""
    from src.network_autopilot.compiler.spec_lint import policy_issues

    spec = _router_policy_spec([
        {"action": "permit", "protocol": "icmp",
         "source": "any", "destination": "any"},
    ])
    # the lint must not flag a verified enforcer either
    assert policy_issues(spec) == [], policy_issues(spec)
    cmds = _ios_commands(spec, "r1")
    assert "access-list 100 permit icmp any any" in cmds, cmds
    binding = cmds.index("ip access-group 100 in")
    assert cmds[binding - 1].startswith("interface "), cmds[binding - 1]
    assert cmds.index("access-list 100 permit icmp any any") < binding


def test_address_scoped_rules_are_allowed_on_a_router():
    """Live probe (issue #16): PT's IOS router matches addresses, unlike the
    ASA, so the ASA-only wildcard refusal must not apply to router targets."""
    spec = _router_policy_spec([
        {"action": "deny", "protocol": "icmp",
         "source": "10.8.8.0/24", "destination": "10.9.9.0/24"},
        {"action": "permit", "protocol": "ip",
         "source": "any", "destination": "any"},
    ])
    cmds = _ios_commands(spec, "r1")
    assert "access-list 100 deny icmp 10.8.8.0 0.0.0.255 10.9.9.0 0.0.0.255" in cmds, cmds
    assert "access-list 100 permit ip any any" in cmds, cmds


def test_a_router_policy_without_an_addressed_interface_fails_the_build():
    """`ip access-group` is an interface command: with no addressed interface
    there is nowhere to bind, and a policy that binds nowhere is a no-op."""
    spec = _router_policy_spec(
        [{"action": "permit", "protocol": "icmp",
          "source": "any", "destination": "any"}],
        address_r1=False,
    )
    with pytest.raises(CompileError) as exc:
        _compile(spec)
    assert "no addressed interface" in str(exc.value)


def test_rule_to_ios_cli_renders_the_probe_backed_operand_forms():
    deny = AclRuleSpec(action="deny", protocol="icmp",
                       source="10.8.8.2", destination="10.9.9.2")
    assert rule_to_ios_cli("100", 0, deny) == (
        "access-list 100 deny icmp host 10.8.8.2 host 10.9.9.2"
    )
    cidr = AclRuleSpec(action="permit", protocol="tcp",
                       source="any", destination="10.1.1.0/24", dest_port=443)
    assert rule_to_ios_cli("100", 1, cidr) == (
        "access-list 100 permit tcp any 10.1.1.0 0.0.0.255 eq 443"
    )
    wildcard_pair = AclRuleSpec(action="permit", protocol="ip",
                                source="10.0.0.0 0.0.0.255", destination="any")
    assert rule_to_ios_cli("100", 2, wildcard_pair) == (
        "access-list 100 permit ip 10.0.0.0 0.0.0.255 any"
    )


def test_rule_to_ios_cli_refuses_junk_instead_of_guessing():
    junk = AclRuleSpec(action="permit", protocol="ip",
                       source="not-an-address", destination="any")
    with pytest.raises(PolicyCompileError):
        rule_to_ios_cli("100", 0, junk)


def test_host_in_netmask_form_renders_as_host():
    """S1.16 probe: `host X` written as `X 255.255.255.255` must render the
    canonical host form, not a redundant `X 0.0.0.0` wildcard pair."""
    rule = AclRuleSpec(action="permit", protocol="icmp",
                       source="10.8.8.2 255.255.255.255",
                       destination="any")
    assert rule_to_ios_cli("100", 0, rule) == (
        "access-list 100 permit icmp host 10.8.8.2 any"
    )


def test_bogus_netmask_is_refused_by_name():
    """S1.16 probe S4: a mask that is no valid netmask but sits in the
    netmask position (leading 255) is a broken netmask, not a wildcard -
    refuse it instead of emitting a line PT accepts and never matches."""
    rule = AclRuleSpec(action="permit", protocol="ip",
                       source="10.0.0.0 255.0.255.0", destination="any")
    with pytest.raises(PolicyCompileError) as exc:
        rule_to_ios_cli("100", 0, rule)
    assert "not a valid netmask" in str(exc.value)


def test_build_router_policy_commands_is_empty_without_a_policy():
    spec = _spec()
    assert build_router_policy_commands(spec, "r1", ["GigabitEthernet0/0"]) == ([], [])


# -- ASA static routes: ASA syntax, not IOS (S1.19 live root cause) -----------
#
# Live probe on PT 9.0.1 (clean canvas, ASA 5506-X between a router and a
# server LAN): the compiler emitted ``ip route 0.0.0.0 0.0.0.0 <nh>`` and
# EVERY flow across the ASA came back 100% loss. Pushing the same route in the
# ASA's own form - ``route inside 0.0.0.0 0.0.0.0 <nh> 1`` - made the flow pass
# both ways on the first probe. ``ip route`` is silently accepted and never
# installed, which is exactly the "green build, no traffic" failure this
# project exists to prevent. Routers keep the IOS form; only the ASA path
# changes.

_ASA_ROUTE_LAB = {
    "name": "asa-route-lab",
    "devices": [
        {"id": "fw", "role": "firewall", "model_hint": "5506-X"},
        {"id": "r1", "role": "router", "model_hint": "2911"},
        {"id": "srv", "role": "server"},
        {"id": "pc", "role": "pc"},
    ],
    "links": [
        {"id": "l-r-fw", "a": {"device": "r1", "interface_hint": "GigabitEthernet0/1"},
         "b": {"device": "fw", "interface_hint": "GigabitEthernet1/1"}},
        {"id": "l-fw-srv", "a": {"device": "fw", "interface_hint": "GigabitEthernet1/2"},
         "b": {"device": "srv"}},
        {"id": "l-r-pc", "a": {"device": "r1", "interface_hint": "GigabitEthernet0/0"},
         "b": {"device": "pc"}},
    ],
    "subnets": [
        {"id": "transit", "cidr": "10.0.0.8/30"},
        {"id": "lan", "cidr": "10.10.10.0/24"},
        {"id": "dc", "cidr": "10.20.40.0/24"},
    ],
    "ip_assignments": [
        {"device": "r1", "interface_hint": "GigabitEthernet0/1", "subnet": "transit", "ip": "10.0.0.9"},
        {"device": "fw", "interface_hint": "GigabitEthernet1/1", "subnet": "transit", "ip": "10.0.0.10"},
        {"device": "r1", "interface_hint": "GigabitEthernet0/0", "subnet": "lan", "ip": "10.10.10.1"},
        {"device": "pc", "subnet": "lan", "ip": "10.10.10.2"},
        {"device": "fw", "interface_hint": "GigabitEthernet1/2", "subnet": "dc", "ip": "10.20.40.1"},
        {"device": "srv", "subnet": "dc", "ip": "10.20.40.10"},
    ],
    "routing": {
        "mode": "static",
        "static_routes": [
            {"device": "r1", "destination": "10.20.40.0/24", "next_hop": "10.0.0.10"},
            {"device": "fw", "destination": "0.0.0.0/0", "next_hop": "10.0.0.9"},
        ],
    },
    "requirements": [{"id": "R-1", "statement": "pc reaches srv"}],
    "acceptance_criteria": [
        {"id": "A-1", "requirement_ids": ["R-1"],
         "test": {"kind": "flow", "source": "pc", "destination": "srv",
                  "expect": "reachable"}},
    ],
}


def _asa_route_spec() -> NetworkSpec:
    return NetworkSpec.model_validate(copy.deepcopy(_ASA_ROUTE_LAB))


def test_asa_static_route_uses_asa_syntax_not_ip_route():
    """PT's ASA silently ignores `ip route`; it needs
    `route <nameif> <net> <mask> <next_hop> <metric>` with the nameif the
    next hop is reachable through (live root cause, S1.19).

    The nameif is `outside` because the default route is what *decides* which
    interface is the outside (compiler._asa_outside_by_default_route). This
    spec's fw reaches the default route's next hop 10.0.0.9 through r1's
    subnet, so that side is the edge the device guards. The line used to read
    `route inside ...` - a default route leaving through the trusted
    interface, which is the defect that fix removes, not a detail to restore.
    """
    commands = _ios_commands(_asa_route_spec(), "fw")
    assert "route outside 0.0.0.0 0.0.0.0 10.0.0.9 1" in commands, commands
    assert not any(c.startswith("ip route") for c in commands), commands
    assert not any(
        c.startswith("route inside ") and c.split()[2] == "0.0.0.0" for c in commands
    ), "a default route must never leave through the trusted interface"


def test_router_static_route_keeps_the_ios_form():
    """The ASA fix must not leak into routers: IOS still wants `ip route`."""
    commands = _ios_commands(_asa_route_spec(), "r1")
    assert "ip route 10.20.40.0 255.255.255.0 10.0.0.10" in commands, commands
    assert not any(c.startswith("route ") for c in commands), commands
