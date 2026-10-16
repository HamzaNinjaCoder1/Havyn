"""A link PT cannot carry must be refused by name, before any mutation.

Rule 9. Measured live on PT 9.0.1 (2026-10-17): an ASA 5505's Ethernet ports
are switchports, and they will not carry a link to a Router-PT - whatever the
cable. The straight-through is refused outright by PT; the crossover, the only
media PT accepts there, is accepted, the link reads `up down`, and no ping
crosses it. Re-configuring the port did nothing through any variation
(`switchport access vlan 1`/`vlan 2` before and after the vlan existed,
`ip address` on the port, `no switchport`), each of which answered `true` and
landed nowhere. Meanwhile the very same switchport carries traffic to a 2960
normally, and a 5506-X's routed ports link to routers normally.

So the reference spec's original `rbr --copper--> fwbr` (5505) could never
have come up, which is why `rbr`'s static route never installed. The spec now
uses a 5506-X for that role; this file pins the refusal so the shape cannot
come back quietly. Nothing here is refused on suspicion: the switch and
router peers that were measured working are asserted to still compile.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler.compiler import CompileError
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing


def _raw(fw_model: str) -> dict:
    """`rtr -- copper -- fw -- copper -- sw -- pc`, the branch shape.

    The firewall's model is the only variable: a 5505's ports are switchports,
    a 5506-X's are routed - so the port names vary with it, exactly as the
    spec does when the chassis changes.
    """
    port_to_rtr = "Ethernet0/1" if fw_model == "5505" else "GigabitEthernet1/1"
    port_to_sw = "Ethernet0/0" if fw_model == "5505" else "GigabitEthernet1/2"
    return {
        "name": f"unbuildable-{fw_model}",
        "devices": [
            {"id": "rtr", "role": "router", "model_hint": "Router-PT"},
            {"id": "fw", "role": "firewall", "model_hint": fw_model},
            {"id": "sw", "role": "switch"},
            {"id": "pc1", "role": "pc"},
        ],
        "links": [
            {"id": "l-rtr-fw", "a": {"device": "rtr", "interface_hint": "FastEthernet0/0"},
                               "b": {"device": "fw", "interface_hint": port_to_rtr}},
            {"id": "l-fw-sw", "a": {"device": "fw", "interface_hint": port_to_sw},
                             "b": {"device": "sw"}},
            {"id": "l-sw-pc1", "a": {"device": "sw"}, "b": {"device": "pc1"}},
        ],
        "subnets": [
            {"id": "net-t", "cidr": "10.0.0.0/30"},
            {"id": "net-lan", "cidr": "10.10.0.0/24"},
        ],
        "ip_assignments": [
            {"device": "rtr", "interface_hint": "FastEthernet0/0", "subnet": "net-t", "ip": "10.0.0.1"},
            {"device": "fw", "interface_hint": port_to_rtr, "subnet": "net-t", "ip": "10.0.0.2"},
            {"device": "fw", "interface_hint": port_to_sw, "subnet": "net-lan", "ip": "10.10.0.1"},
            {"device": "pc1", "subnet": "net-lan"},
        ],
        "routing": {"mode": "connected"},
        "requirements": [{"id": "R-1", "statement": "the branch reaches the campus"}],
        "acceptance_criteria": [],
    }


def _compile(raw: dict):
    spec = NetworkSpec.model_validate(raw)
    return compile_spec(spec, plan_addressing(spec))


def test_a_5505_cannot_be_cabled_to_a_router_and_is_refused_by_name():
    """The measured-unbuildable link fails the compile, naming the reason.

    Without this the compiler would happily emit both cable attempts, PT
    would answer `true` for one and `false` for the other, and the build
    would report a half-configured network - the failure mode this project
    exists to prevent.
    """
    with pytest.raises(CompileError) as excinfo:
        _compile(_raw("5505"))

    message = str(excinfo.value)
    assert "unbuildable_link" in message, message
    # The reason a reader can act on, not just a code.
    assert "5505" in message and "5506-X" in message, message
    assert "switch" in message, message
    # Both endpoints are named so the link can be found without searching.
    assert "l-rtr-fw" in message and "rtr" in message, message


def test_the_refusal_is_not_wider_than_the_measurement():
    """A 5506-X on the same link compiles: the refusal is about the chassis.

    The 5506-X's routed ports link to routers as `fwhq <-> risp` did on that
    canvas (`up up`), so it must not be caught by a rule that only knows
    "firewall meets router".
    """
    result = _compile(_raw("5506-X"))
    endpoints = result.dag.nodes and {
        op_id: node.operation.inputs
        for op_id, node in result.dag.nodes.items()
        if node.operation.operation_type == "create_link"
    }
    rtr_fw = [i for i in endpoints.values() if i.get("link_id") == "l-rtr-fw"]
    assert rtr_fw, endpoints
    assert rtr_fw[0]["a_device"] == "rtr"
    assert rtr_fw[0]["b_device"] == "fw"


def _raw_switch_only() -> dict:
    """Just `fw -- copper -- sw -- pc`: the measured-working 5505 pairing.

    Kept separate from `_raw` because one refused link would fail the whole
    compile - which is the point of the rule - and would therefore never
    reach the assertion this test is here to make.
    """
    return {
        "name": "unbuildable-switch-only",
        "devices": [
            {"id": "fw", "role": "firewall", "model_hint": "5505"},
            {"id": "sw", "role": "switch"},
            {"id": "pc1", "role": "pc"},
        ],
        "links": [
            {"id": "l-fw-sw", "a": {"device": "fw", "interface_hint": "Ethernet0/0"},
                             "b": {"device": "sw"}},
            {"id": "l-sw-pc1", "a": {"device": "sw"}, "b": {"device": "pc1"}},
        ],
        "subnets": [{"id": "net-lan", "cidr": "10.10.0.0/24"}],
        "ip_assignments": [
            {"device": "fw", "interface_hint": "Ethernet0/0", "subnet": "net-lan", "ip": "10.10.0.1"},
            {"device": "pc1", "subnet": "net-lan"},
        ],
        "routing": {"mode": "connected"},
        "requirements": [{"id": "R-1", "statement": "the branch reaches the campus"}],
        "acceptance_criteria": [],
    }


def test_a_5505_terminating_on_a_switch_still_compiles():
    """The measured-working pairing must survive the rule that refuses one.

    `pc13 -> 10.30.50.1` crossed a 5505 switchport talking to a 2960 on the
    live canvas and was reachable, so the firewall's inside port is not
    collateral damage of the router refusal.
    """
    result = _compile(_raw_switch_only())

    fw_sw = [
        node.operation.inputs
        for node in result.dag.nodes.values()
        if node.operation.operation_type == "create_link"
        and node.operation.inputs.get("link_id") == "l-fw-sw"
    ]
    assert fw_sw, "the switch-side link must still be planned"
    assert fw_sw[0]["media"] == "straight"


def test_the_refusal_blocks_the_build_not_one_operation():
    """`solve_ports` reports it and `compile_spec` refuses the whole plan.

    A per-op skip is the half-configuration rule 9 forbids: the caller has to
    decide with every failure in hand, so the error carries the whole
    incompatibility list rather than dropping the link silently.
    """
    spec = NetworkSpec.model_validate(_raw("5505"))
    from src.network_autopilot.compiler.port_solver import solve_ports

    plan_out = solve_ports(spec, plan_addressing(spec))
    assert any("unbuildable_link" in entry for entry in plan_out.incompatible)
    assert len(plan_out.incompatible) == 1, plan_out.incompatible
