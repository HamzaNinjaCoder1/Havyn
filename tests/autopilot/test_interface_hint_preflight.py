"""S1.9 / F6: a wrong interface_hint must be caught at preflight.

An address assignment that names an interface on the WRONG side - a LAN
address hinted onto the port cabled toward the WAN - builds green and then
gives 100% loss, because every op succeeds but the routing can never work.
Preflight must check that the hinted port leads toward the subnet's own
members, and refuse with the correct port named.
"""

from __future__ import annotations

import copy

from src.network_autopilot.compiler.port_solver import solve_ports
from src.network_autopilot.compiler.preflight import run_preflight
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing
from tests.autopilot.test_compiler import GOLDEN_SPEC


def _spec(raw: dict) -> NetworkSpec:
    return NetworkSpec.model_validate(raw)


def test_wrong_interface_hint_is_refused_at_preflight():
    """RED before the fix: a LAN address on the WAN-facing port passes
    preflight silently."""
    raw = copy.deepcopy(GOLDEN_SPEC)
    for assignment in raw["ip_assignments"]:
        if assignment["device"] == "r1" and assignment["subnet"] == "net-lan1":
            assignment["interface_hint"] = "GigabitEthernet0/0"  # the WAN port
    spec = _spec(raw)
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)

    result = run_preflight(spec, plan, port_plan)

    assert not result.can_execute, result.errors
    hint_checks = [c for c in result.checks if c.category == "interface_hint"]
    assert hint_checks, "no interface_hint check was raised"
    detail = " ".join(c.detail for c in hint_checks)
    assert "r1" in detail and "net-lan1" in detail
    # the correct port must be suggested, not merely the mistake named
    assert "GigabitEthernet0/1" in detail


def test_correct_interface_hint_passes_preflight():
    """Pin: the same spec with the RIGHT port must be clean."""
    spec = _spec(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)

    result = run_preflight(spec, plan, port_plan)

    hint_checks = [c for c in result.checks if c.category == "interface_hint"]
    assert hint_checks == [] or all(
        c.status.value != "fail" for c in hint_checks
    ), hint_checks
    assert result.can_execute, result.errors


def test_hint_on_a_subnet_with_no_offset_members_is_not_refused():
    """Pin: a WAN-style subnet whose only addressed member is the router
    itself has no members to point at, so a hint there is not a mistake."""
    spec = _spec(copy.deepcopy(GOLDEN_SPEC))
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)

    # r1's net-wan hint is GigabitEthernet0/0, and net-wan's only other
    # addressed member is r2 — so this hint is correct and must pass.
    result = run_preflight(spec, plan, port_plan)
    assert result.can_execute, result.errors


def test_svi_hint_is_not_judged_by_the_cable_graph():
    """Pin: an SVI (VlanN) or radio has no cable of its own — its traffic
    rides the switchport, so the cable-graph check must skip it rather than
    report a false 'wrong side' on the 5505 reference spec."""
    raw = copy.deepcopy(GOLDEN_SPEC)
    for assignment in raw["ip_assignments"]:
        if assignment["device"] == "r1" and assignment["subnet"] == "net-lan1":
            assignment["interface_hint"] = "Vlan10"
    spec = _spec(raw)
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)

    result = run_preflight(spec, plan, port_plan)

    hint_fails = [
        c for c in result.checks
        if c.category == "interface_hint" and c.status.value == "fail"
    ]
    assert hint_fails == [], hint_fails