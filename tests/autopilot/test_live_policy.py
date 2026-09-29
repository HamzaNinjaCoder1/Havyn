"""Live policy enforcement and ASA read-back probes (issues #16, #17).

The offline suite pins the shapes the compiler emits; these tests pin the PT
behaviour those shapes were derived from, against the open Packet Tracer:

* a router numbered ACL renders CIDR operands as wildcard pairs and ENFORCES
  them - the old refusal "PT enforces access-lists only on the ASA" was wrong
  for routers (issue #16). Proven ABA: reachable, then blocked by the deny,
  then reachable again once the deny is removed.
* PT's ASA DOES match addresses: a permit scoped to the pair carries the
  flow while a permit for a different pair does not. This is the S1.16 probe
  that overturned the older "address-scoped is accepted but matched
  nothing" measurement, and it is why firewall_policy.py renders scoped
  operands instead of refusing them (issue #16).
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


def test_asa_address_scoped_acl_is_enforced():
    """S1.16 live decision: PT's ASA DOES match addresses, so the compiler
    renders a scoped rule instead of refusing it.

    The earlier measurement said an address-scoped rule was accepted but
    matched nothing, and the compiler refused such a policy on that basis.
    This probe overturns it and is deliberately discriminating:

    1. push the COMPILER's own commands for a policy scoped to the pair
       (``host 192.168.10.10 host 192.168.20.10``) - the pair must pass,
       which proves both the render and the enforcement;
    2. re-bind a permit for a DIFFERENT pair - the same flow must die, which
       is what separates address matching from "any permit opens the
       protocol". Without step 2 a protocol-only PT would satisfy step 1;
    3. re-bind a wildcard permit - the flow must return, the control that
       keeps a dead lab from faking step 2.

    The ASA has no console (`getCommandLine()` is null), so the flow verdicts
    are the only observable - and they are enough.
    """
    adapter = live.make_adapter()
    tag = live.run_tag()
    spec = _asa_spec("5506-X")
    spec["policies"] = [{
        "id": "scoped",
        "apply_to": ["fw"],
        "rules": [
            {"action": "permit", "protocol": "icmp",
             "source": "192.168.10.10", "destination": "192.168.20.10"},
            {"action": "permit", "protocol": "icmp",
             "source": "192.168.20.10", "destination": "192.168.10.10"},
        ],
    }]
    result = _dag(spec)
    created: list[str] = []
    try:
        created = _build(adapter, result, tag)
        fw, pca, pcb = f"{tag}-fw", f"{tag}-pc-a", f"{tag}-pc-b"

        _cable(adapter, result, tag)
        _host_addressing(adapter, result, tag)

        commands = _one(result, "configure_ios", "fw").inputs.get("commands", [])
        assert (
            "access-list scoped extended permit icmp "
            "host 192.168.10.10 host 192.168.20.10"
        ) in commands, commands
        assert "access-group scoped in interface outside" in commands, commands

        receipt = adapter.configure_ios(fw, commands)
        assert receipt.result.value == "success", receipt.error
        _settle(3.0)

        # 1: the compiler's address-scoped permit carries the pair both ways.
        _assert_both_ways(adapter, pca, pcb, "asa address-scoped permit")

        # 2: a permit for a DIFFERENT pair must not open this flow.
        other = adapter.configure_ios(
            fw,
            [
                "access-list otherpair extended permit icmp "
                "host 10.9.9.9 host 10.8.8.8",
                "access-group otherpair in interface outside",
            ],
        )
        assert other.result.value == "success", other.error
        _settle(3.0)
        forward = adapter.run_flow_test(pca, pcb, transport="icmp")
        assert forward.outcome.value == BLOCKED, (
            "a permit for a non-matching pair must not open this flow - if it "
            f"does, PT ignores addresses and the scoped render is a lie, got "
            f"{forward.outcome.value}: {str(forward.detail)[:160]}"
        )

        # 3: control - the wildcard permit has to bring the flow back.
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
