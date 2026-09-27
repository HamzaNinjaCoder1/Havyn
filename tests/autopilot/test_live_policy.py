"""Live policy enforcement and ASA read-back probes (issues #16, #17).

The offline suite pins the shapes the compiler emits; these tests pin the PT
behaviour those shapes were derived from, against the open Packet Tracer:

* a router numbered ACL renders CIDR operands as wildcard pairs and ENFORCES
  them - the old refusal "PT enforces access-lists only on the ASA" was wrong
  for routers (issue #16). Proven ABA: reachable, then blocked by the deny,
  then reachable again once the deny is removed.
* PT's ASA cannot carry traffic through an address-scoped rule while the
  wildcard form on the same nameif can - the fresh probe the address-scoped
  refusal in firewall_policy.py rests on (issue #16).
* an ASA 5505's Vlan SVIs read back the addresses the push asked for instead
  of 0.0.0.0, and the configure_ios receipt says so through its
  configured_addresses_present postcondition (issue #17).

The adapter only has a verified ICMP probe (tcp/udp honestly return UNKNOWN),
which is why the router test uses an ABA sequence rather than a second
transport as its control.

Every device is namespaced with this run's tag and removed in a finally
block, so a failure never pollutes the workspace.
"""

from __future__ import annotations

import copy
import sys

import pytest

sys.path.insert(0, "tests/autopilot")
import _live_harness as live

from test_live_connectivity import (
    BLOCKED,
    _asa_spec,
    _assert_both_ways,
    _assert_reaches,
    _build,
    _cable,
    _dag,
    _host_addressing,
    _ios,
    _one,
    _settle,
)

pytestmark = pytest.mark.skipif(
    not live.live_is_reachable(),
    reason=(
        "LIVE TEST BLOCKED: Packet Tracer is not reachable on either channel "
        "(HTTP bridge on 127.0.0.1:54321, or the Script Engine file mailbox). "
        "Open PT with the MCP Control Center extension and re-run."
    ),
)


ROUTER_LAB = {
    "name": "router-acl-live",
    "devices": [
        {"id": "rt", "role": "router", "model_hint": "2911"},
        {"id": "pc-a", "role": "pc"},
        {"id": "pc-b", "role": "pc"},
    ],
    "links": [
        {"id": "l-a", "a": {"device": "rt"}, "b": {"device": "pc-a"}},
        {"id": "l-b", "a": {"device": "rt"}, "b": {"device": "pc-b"}},
    ],
    "subnets": [
        {"id": "net-a", "cidr": "10.8.8.0/24"},
        {"id": "net-b", "cidr": "10.9.9.0/24"},
    ],
    "ip_assignments": [
        {"device": "rt", "subnet": "net-a", "ip": "10.8.8.1"},
        {"device": "rt", "subnet": "net-b", "ip": "10.9.9.1"},
        {"device": "pc-a", "subnet": "net-a", "ip": "10.8.8.10"},
        {"device": "pc-b", "subnet": "net-b", "ip": "10.9.9.10"},
    ],
    "routing": {"mode": "connected"},
}

#: The exact line rule_to_ios_cli renders for the spec's CIDR source/dest -
#: the shape this test pushes live, so a renderer change breaks it here.
RENDERED_DENY = (
    "access-list 100 deny icmp 10.8.8.0 0.0.0.255 10.9.9.0 0.0.0.255"
)


def _router_policy_spec() -> dict:
    spec = copy.deepcopy(ROUTER_LAB)
    spec["policies"] = [
        {
            "id": "rtrpolicy",
            "apply_to": ["rt"],
            "rules": [
                {
                    "action": "deny",
                    "protocol": "icmp",
                    "source": "10.8.8.0/24",
                    "destination": "10.9.9.0/24",
                    "description": "CIDR operands, rendered as wildcard pairs",
                },
                {"action": "permit", "protocol": "ip",
                 "source": "any", "destination": "any"},
            ],
        }
    ]
    return spec


def test_router_acl_renders_a_cidr_operand_and_enforces_it():
    """#16: the CIDR -> wildcard ACL line must match traffic on a real router.

    ABA design: without the ACL the pair is reachable; with the compiled deny
    first-match bound `in` on both interfaces the ICMP flow must die; remove
    only the deny and the very same flow must come back - which is what
    separates "the ACL matches this address pair" from "the push broke the
    lab". The router has no other transit flow, and the adapter has no TCP
    probe, so the restore step is the control.
    """
    adapter = live.make_adapter()
    tag = live.run_tag()
    baseline = _dag(dict(ROUTER_LAB))
    policy = _dag(_router_policy_spec())
    created: list[str] = []
    try:
        created = _build(adapter, baseline, tag)
        rt, pca, pcb = f"{tag}-rt", f"{tag}-pc-a", f"{tag}-pc-b"

        _cable(adapter, baseline, tag)
        _host_addressing(adapter, baseline, tag)
        _ios(adapter, baseline, tag, "rt")
        _settle(3.0)

        # A: the lab works before any ACL exists.
        _assert_both_ways(adapter, pca, pcb, "router lab before the ACL")

        # The compiler's own block: the rendered deny, the permit, and the
        # `ip access-group 100 in` binding on every addressed interface.
        commands = _one(policy, "configure_ios", "rt").inputs.get("commands", [])
        assert RENDERED_DENY in commands, commands
        assert "access-list 100 permit ip any any" in commands, commands
        assert "ip access-group 100 in" in commands, commands
        _ios(adapter, policy, tag, "rt")
        _settle(3.0)

        # B: the deny matched - the ICMP flow that fits the wildcard pair dies.
        flow = adapter.run_flow_test(pca, pcb, transport="icmp")
        assert flow.outcome.value == BLOCKED, (
            f"the CIDR-rendered deny must block pc-a -> pc-b, got "
            f"{flow.outcome.value}: {str(flow.detail)[:160]}"
        )

        # Controls under the same binding: traffic that does not fit the
        # deny's source/destination pair still passes (local deliveries on
        # the same interfaces the ACL is bound `in` on).
        _assert_reaches(
            adapter, pca, rt, "non-matching flow under the ACL",
            destination_ip="10.8.8.1",
        )
        _assert_reaches(
            adapter, pcb, rt, "non-matching flow on the other interface",
            destination_ip="10.9.9.1",
        )

        # A': remove only the deny, keep the binding - the flow must return.
        restore = adapter.configure_ios(
            rt,
            [
                "no access-list 100",
                "access-list 100 permit icmp any any",
            ],
        )
        assert restore.result.value == "success", restore.error
        _settle(3.0)
        _assert_both_ways(adapter, pca, pcb, "router lab after the deny is gone")
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"


def test_asa_address_scoped_acl_cannot_carry_traffic_but_wildcard_can():
    """#16 (fresh probe): PT's ASA cannot enforce address-scoped rules.

    The refusal in firewall_policy.py rests on this, so it is re-probed here
    from a clean state instead of trusting the older measurement - and there
    is no teardown trick involved, because an invalid `no` line in a
    configureIosDevice block aborts everything after it and would fake the
    result. Instead:

    1. push the compiler's firewall config with the ACL lines stripped, so
       the interfaces are addressed and named but nothing is bound: PT's ASA
       passes no flow without an access-group (asserted as the baseline);
    2. bind ONLY address-scoped permit lines for the pair: if PT matched
       addresses the pair would pass - it must not;
    3. bind the wildcard permit on the same nameif: traffic must return,
       which is what keeps a dead lab from satisfying step 2.

    The ASA has no console (`getCommandLine()` is null), so `show` read-back
    is impossible; the flow verdicts are the only observable. Whether PT
    rejects the host lines or accepts-but-unmatches them, the engineering
    outcome is the same - an address-scoped policy cannot be enforced on PT's
    ASA - and that is exactly what this test pins.
    """
    adapter = live.make_adapter()
    tag = live.run_tag()
    spec = _asa_spec("5506-X")
    result = _dag(spec)
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        fw, pca, pcb = f"{tag}-fw", f"{tag}-pc-a", f"{tag}-pc-b"

        _cable(adapter, result, tag)
        _host_addressing(adapter, result, tag)

        commands = _one(result, "configure_ios", "fw").inputs.get("commands", [])
        acl_free = [
            c for c in commands
            if not c.startswith(("access-list ", "access-group "))
        ]
        assert len(acl_free) < len(commands), (
            "the compiler's firewall block should carry the default ACL"
        )
        receipt = adapter.configure_ios(fw, acl_free)
        assert receipt.result.value == "success", receipt.error
        addr_checks = [
            c for c in receipt.postconditions
            if c.name == "configured_addresses_present"
        ]
        assert addr_checks and addr_checks[0].passed is True, (
            f"interface config without ACL did not land: {receipt.postconditions}"
        )
        _settle(3.0)

        # 1: interfaces up, no access-group - PT's ASA passes nothing.
        forward = adapter.run_flow_test(pca, pcb, transport="icmp")
        assert forward.outcome.value == BLOCKED, (
            "with no access-group PT's ASA must pass no flow (it has no "
            f"implicit inside->outside permit), got {forward.outcome.value}: "
            f"{str(forward.detail)[:160]}"
        )

        # 2: ONLY address-scoped lines, both orientations, freshly bound.
        address_scoped = adapter.configure_ios(
            fw,
            [
                "access-list addr-scoped extended permit icmp "
                "host 192.168.10.10 host 192.168.20.10",
                "access-list addr-scoped extended permit icmp "
                "host 192.168.20.10 host 192.168.10.10",
                "access-group addr-scoped in interface outside",
            ],
        )
        assert address_scoped.result.value == "success", address_scoped.error
        _settle(3.0)

        forward = adapter.run_flow_test(pca, pcb, transport="icmp")
        assert forward.outcome.value == BLOCKED, (
            "an address-scoped ACL must not carry the pair's traffic on "
            f"PT's ASA (the claim behind the refusal), got "
            f"{forward.outcome.value}: {str(forward.detail)[:160]} - if PT "
            "now matches addresses, the compiler's refusal is too "
            "conservative and must be relaxed"
        )
        reverse = adapter.run_flow_test(pcb, pca, transport="icmp")
        assert reverse.outcome.value == BLOCKED, (
            "the reverse direction must be blocked by the same unmatched "
            f"ACL, got {reverse.outcome.value}: {str(reverse.detail)[:160]}"
        )

        # 3: control - the wildcard form on the same nameif has to work,
        # otherwise steps 1-2 only prove the lab died along the way.
        wildcard = adapter.configure_ios(
            fw,
            [
                "access-list wildcard extended permit icmp any any",
                "access-group wildcard in interface outside",
            ],
        )
        assert wildcard.result.value == "success", wildcard.error
        _settle(3.0)
        _assert_both_ways(adapter, pca, pcb, "asa wildcard permit restored")
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"


def test_asa_5505_vlan_addresses_read_back_after_the_push():
    """#17: the 5505's factory Vlan SVIs must read back the pushed addresses.

    The original bug: the CLI accepted `ip address` on Vlan1 and inspect
    reported 0.0.0.0, so a silent config failure was invisible. Now the
    configure_ios receipt only confirms when configured_addresses_present
    passes, and inspect has to show both SVI addresses for real.
    """
    adapter = live.make_adapter()
    tag = live.run_tag()
    spec = _asa_spec("5505")
    result = _dag(spec)
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        fw = f"{tag}-fw"

        _cable(adapter, result, tag)
        _host_addressing(adapter, result, tag)

        commands = _one(result, "configure_ios", "fw").inputs.get("commands", [])
        receipt = adapter.configure_ios(fw, commands)
        assert receipt.result.value == "success", receipt.error

        addr_checks = [
            c for c in receipt.postconditions
            if c.name == "configured_addresses_present"
        ]
        assert addr_checks, (
            "no address read-back ran for a block that pushes addresses: "
            f"{[c.name for c in receipt.postconditions]}"
        )
        assert addr_checks[0].passed is True, (
            f"5505 addresses not read back: {addr_checks[0].actual}"
        )
        _settle(3.0)

        obs = adapter.inspect_device(fw)
        vlan_ips = {
            name: port.ip
            for name, port in obs.ports.items()
            if name.startswith("Vlan")
        }
        assert vlan_ips, f"no Vlan SVI on the 5505: {sorted(obs.ports)}"
        wrong = {
            name: ip.value if ip.is_known else f"<{ip.status.value}>"
            for name, ip in vlan_ips.items()
            if not ip.is_known or ip.value in ("", "0.0.0.0")
        }
        assert not wrong, f"5505 Vlan SVI read back wrong: {wrong}"
        values = {ip.value for ip in vlan_ips.values() if ip.is_known}
        assert {"192.168.10.1", "192.168.20.1"} <= values, (
            f"5505 Vlan SVIs hold {sorted(values)}, expected both spec addresses"
        )
    finally:
        outcomes = live.delete_devices(created)
        leaked = {k: v for k, v in outcomes.items() if v != "success"}
        assert not leaked, f"leaked devices: {leaked}"
