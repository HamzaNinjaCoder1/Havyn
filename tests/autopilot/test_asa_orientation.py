"""An ASA's default route leaves through its `outside` - not its `inside`.

The `inside`/`outside` names are not decoration. PT's ASA is directional
(`compiler.py::_asa_outside_subnet` records the live A/B: swapping only the
order of two `ip_assignments` flipped the same flow PASS->FAIL), so the name
decides which direction is filtered, and the spec's ACL binds to `outside` by
name. A firewall that calls its guarded edge `inside` has its ACL on the wrong
side of the network it is meant to protect.

Two of the three ASAs in the reference campus shipped that way. Both emitted
`route inside 0.0.0.0 0.0.0.0 <next hop>` - a default route leaving through
the trusted interface - because the only signal available was a count of the
end devices reachable through each neighbour. That count names the ISP edge
correctly for a perimeter firewall (fwhq: the ISP side reaches one host, the
HQ side reaches the campus) and backwards everywhere else, because at a branch
or a data centre the *smaller* side is the site's own LAN, which is precisely
what the device exists to protect. fwbr then called its branch LAN
`outside`/security-level 0 and its transit to the branch router `inside`/100,
which inverts the trust model and blocks the very flows A-005/A-008/A-011
require (the branch has to reach the campus, i.e. outside -> inside).

The default route is the answer the code already documented and never
implemented: the port it exits is the edge. These tests pin it, and pin the
fallback for the cases where a default route cannot decide.
"""

from __future__ import annotations

from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing

#: A branch firewall: a small site LAN behind `sw`, and a transit to `rtr`
#: that reaches a bigger campus. The end-device count therefore names the
#: SITE LAN as the edge - exactly the inversion that shipped.
_BRANCH_RAW = {
    "name": "branch-fw",
    "devices": [
        {"id": "rtr", "role": "router", "model_hint": "Router-PT"},
        {"id": "sw2", "role": "switch"},
        {"id": "pc2", "role": "pc"},
        {"id": "pc3", "role": "pc"},
        {"id": "fw", "role": "firewall", "model_hint": "5506-X"},
        {"id": "sw", "role": "switch"},
        {"id": "pc1", "role": "pc"},
    ],
    "links": [
        {"id": "l-fw-rtr", "a": {"device": "fw", "interface_hint": "GigabitEthernet1/1"},
                           "b": {"device": "rtr", "interface_hint": "FastEthernet0/0"}},
        {"id": "l-fw-sw", "a": {"device": "fw", "interface_hint": "GigabitEthernet1/2"},
                          "b": {"device": "sw"}},
        {"id": "l-rtr-sw2", "a": {"device": "rtr", "interface_hint": "FastEthernet1/0"},
                            "b": {"device": "sw2"}},
        {"id": "l-sw2-pc2", "a": {"device": "sw2"}, "b": {"device": "pc2"}},
        {"id": "l-sw2-pc3", "a": {"device": "sw2"}, "b": {"device": "pc3"}},
        {"id": "l-sw-pc1", "a": {"device": "sw"}, "b": {"device": "pc1"}},
    ],
    "subnets": [
        {"id": "net-t", "cidr": "10.0.0.0/30"},
        {"id": "net-campus", "cidr": "10.1.0.0/24"},
        {"id": "net-lan", "cidr": "10.10.0.0/24"},
    ],
    "ip_assignments": [
        {"device": "fw", "interface_hint": "GigabitEthernet1/1", "subnet": "net-t", "ip": "10.0.0.2"},
        {"device": "rtr", "interface_hint": "FastEthernet0/0", "subnet": "net-t", "ip": "10.0.0.1"},
        {"device": "rtr", "interface_hint": "FastEthernet1/0", "subnet": "net-campus", "ip": "10.1.0.1"},
        {"device": "pc2", "subnet": "net-campus"},
        {"device": "pc3", "subnet": "net-campus"},
        {"device": "fw", "interface_hint": "GigabitEthernet1/2", "subnet": "net-lan", "ip": "10.10.0.1"},
        {"device": "pc1", "subnet": "net-lan"},
    ],
    "routing": {
        "mode": "static",
        "static_routes": [
            {"device": "fw", "destination": "0.0.0.0/0", "next_hop": "10.0.0.1"},
        ],
    },
    "requirements": [{"id": "R-1", "statement": "the branch reaches the campus"}],
    "acceptance_criteria": [],
}


def _compile(raw: dict):
    spec = NetworkSpec.model_validate(raw)
    return compile_spec(spec, plan_addressing(spec))


def _fw_commands(result) -> list[str]:
    return result.dag.nodes["op-config-fw"].operation.inputs["commands"]


def _nameif_for_port(cmds: list[str], port: str) -> str:
    start = cmds.index(f"interface {port}")
    for line in cmds[start:start + 8]:
        if line.startswith("nameif "):
            return line.split(" ", 1)[1]
    raise AssertionError(f"no nameif after interface {port} in {cmds}")


def test_a_default_route_decides_the_outside_even_when_the_site_is_smaller():
    """The transit the default route leaves through is `outside`.

    The end-device count would name the site LAN the edge here: one PC sits
    behind `sw`, two sit behind `rtr`. Calling the site's own LAN the
    untrusted side inverts the trust model, so the spec's ACL would filter the
    LAN instead of the edge, and the branch could not reach the campus.
    """
    cmds = _fw_commands(_compile(_BRANCH_RAW))

    assert _nameif_for_port(cmds, "GigabitEthernet1/1") == "outside", (
        "the interface the default route leaves through must be the outside"
    )
    assert _nameif_for_port(cmds, "GigabitEthernet1/2") == "inside"


def test_the_default_route_is_rendered_out_of_the_outside():
    """`route inside 0.0.0.0 ...` is the defect itself; it must not reappear."""
    cmds = _fw_commands(_compile(_BRANCH_RAW))

    assert "route outside 0.0.0.0 0.0.0.0 10.0.0.1 1" in cmds, cmds
    assert not any(
        line.startswith("route inside ") and line.split()[2] == "0.0.0.0"
        for line in cmds
    ), "a default route must never be rendered out of the trusted interface"


def test_the_security_levels_follow_the_names():
    """The edge the default route leaves through is the level-0 interface."""
    cmds = _fw_commands(_compile(_BRANCH_RAW))

    edge = cmds.index("interface GigabitEthernet1/1")
    assert "security-level 0" in cmds[edge:edge + 8]
    site = cmds.index("interface GigabitEthernet1/2")
    assert "security-level 100" in cmds[site:site + 8]


def test_without_a_default_route_the_end_device_rule_still_decides():
    """The fallback is untouched: no default route means no route signal."""
    raw = dict(_BRANCH_RAW)
    raw["routing"] = {"mode": "connected"}
    cmds = _fw_commands(_compile(raw))

    # One PC behind `sw`, two behind `rtr`: the site LAN is the guarded edge.
    assert _nameif_for_port(cmds, "GigabitEthernet1/2") == "outside"
    assert _nameif_for_port(cmds, "GigabitEthernet1/1") == "inside"


def test_a_default_route_that_cannot_be_placed_is_refused_not_mis_oriented():
    """Both unusable shapes are refused by name, so neither can mis-orient.

    An exit-interface-only `route` has no gateway for PT's ASA to reach, and
    a next hop on none of the device's own subnets has no interface to leave
    by. Both are compile errors already - the point here is that neither is
    ever allowed to fall through to the end-device count and quietly name the
    wrong interface as the untrusted edge.
    """
    for routes in (
        [{"device": "fw", "destination": "0.0.0.0/0", "exit_interface": "GigabitEthernet1/2"}],
        [{"device": "fw", "destination": "0.0.0.0/0", "next_hop": "192.0.2.1"}],
    ):
        raw = dict(_BRANCH_RAW)
        raw["routing"] = {"mode": "static", "static_routes": routes}
        try:
            _compile(raw)
        except Exception as exc:
            assert "refused by name" in str(exc) or "no interface to reach" in str(exc), exc
        else:
            raise AssertionError(f"an unplaceable default route was accepted: {routes}")
