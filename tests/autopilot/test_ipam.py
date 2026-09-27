"""IPAM: deterministic arithmetic, structured failures, no silent adjustment."""

from __future__ import annotations

import pytest

from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import (
    PlanningFailure,
    has_errors,
    plan_addressing,
    validate_plan,
)


def _spec_with(subnets: list[dict], assignments: list[dict] | None = None) -> NetworkSpec:
    raw = {
        "name": "ipam-test",
        "devices": [{"id": "r1", "role": "router"}, {"id": "pc1", "role": "pc"}],
        "subnets": subnets,
        "ip_assignments": assignments or [],
    }
    return NetworkSpec.model_validate(raw)


def test_fixed_cidr_plan_is_exact():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24", "gateway": "192.168.10.1"}],
        [{"device": "pc1", "subnet": "net-a", "ip": "192.168.10.5"}],
    )
    plan = plan_addressing(spec)
    alloc = plan.allocation_by_subnet("net-a")
    assert alloc.network == "192.168.10.0"
    assert alloc.broadcast == "192.168.10.255"
    assert alloc.netmask == "255.255.255.0"
    assert alloc.usable_start == "192.168.10.1"
    assert alloc.usable_end == "192.168.10.254"
    assert alloc.capacity == 254
    assert plan.assignments[0].ip == "192.168.10.5"
    assert plan.assignments[0].gateway == "192.168.10.1"


def test_vlsm_sizes_deterministically():
    spec = _spec_with(
        [
            {"id": "it", "required_hosts": 100},
            {"id": "finance", "required_hosts": 40},
            {"id": "hr", "required_hosts": 20},
        ]
    )
    plan = plan_addressing(spec)
    sizes = {a.subnet_id: a.prefix_len for a in plan.allocations}
    assert sizes["it"] == 25          # 126 usable >= 100
    assert sizes["finance"] == 26     # 62 usable >= 40
    assert sizes["hr"] == 27          # 30 usable >= 20
    # sequential, aligned, non-overlapping (after a /25, a /26 fits at .128)
    nets = sorted((a.network, a.prefix_len) for a in plan.allocations)
    assert nets == [("10.0.0.0", 25), ("10.0.0.128", 26), ("10.0.0.192", 27)]
    assert all(a.gateway == a.usable_start for a in plan.allocations)


def test_same_input_same_plan():
    spec = _spec_with(
        [
            {"id": "a", "required_hosts": 50},
            {"id": "b", "cidr": "10.0.200.0/24"},
        ]
    )
    first = plan_addressing(spec).to_dict()
    second = plan_addressing(spec).to_dict()
    assert first == second


def test_insufficient_pool_is_structured_failure():
    # 300000 hosts need a /13 (524288 addresses) — larger than a /16 pool.
    spec = _spec_with([{"id": "huge", "required_hosts": 300000}])
    with pytest.raises(PlanningFailure) as excinfo:
        plan_addressing(spec, pool="10.0.0.0/16")
    assert excinfo.value.problem == "subnet does not fit inside the address pool"


def test_fixed_subnet_colliding_with_previous_allocation_fails():
    # 10.0.0.0/25 then 10.0.0.0/24 (overlaps and starts before cursor)
    spec = _spec_with(
        [
            {"id": "a", "cidr": "10.0.0.0/25"},
            {"id": "b", "cidr": "10.0.0.0/24"},
        ]
    )
    with pytest.raises(PlanningFailure):
        plan_addressing(spec)


def test_invalid_cidr_rejected():
    spec = _spec_with([{"id": "bad", "cidr": "not-a-cidr"}])
    with pytest.raises(PlanningFailure, match="invalid cidr"):
        plan_addressing(spec)


def test_host_outside_subnet_rejected_at_plan_time():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24"}],
        [{"device": "pc1", "subnet": "net-a", "ip": "10.9.9.9"}],
    )
    with pytest.raises(PlanningFailure, match="outside its subnet"):
        plan_addressing(spec)


def test_static_and_dhcp_mutually_exclusive():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24", "dhcp": True}],
        [{"device": "pc1", "subnet": "net-a", "ip": "192.168.10.9", "dhcp": True}],
    )
    with pytest.raises(PlanningFailure, match="both static and DHCP"):
        plan_addressing(spec)


def test_dhcp_clients_have_no_static_ip():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24", "dhcp": True}],
        [{"device": "pc1", "subnet": "net-a"}],
    )
    plan = plan_addressing(spec)
    assert plan.assignments[0].dhcp is True
    assert plan.assignments[0].ip is None
    assert plan.assignments[0].gateway is None  # gateway arrives via DHCP


def test_auto_assignment_is_deterministic_and_after_gateway():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24"}],
        [
            {"device": "pc1", "subnet": "net-a"},
            {"device": "r1", "subnet": "net-a"},
        ],
    )
    plan = plan_addressing(spec)
    ips = [a.ip for a in plan.assignments]
    assert ips == ["192.168.10.2", "192.168.10.3"]  # gateway is .1, auto starts at .2


def test_validator_detects_duplicate_ip():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24"}],
        [
            {"device": "pc1", "subnet": "net-a", "ip": "192.168.10.5"},
            {"device": "r1", "subnet": "net-a", "ip": "192.168.10.5"},
        ],
    )
    plan = plan_addressing(spec)
    issues = validate_plan(plan, spec)
    assert any("duplicate ip" in i.problem for i in issues)
    assert has_errors(issues)


def test_validator_detects_gateway_outside_subnet():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24", "gateway": "192.168.99.1"}]
    )
    plan = plan_addressing(spec)
    issues = validate_plan(plan, spec)
    assert any("gateway outside" in i.problem for i in issues)
    assert has_errors(issues)


def test_validator_detects_network_address_as_host():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24"}],
        [{"device": "pc1", "subnet": "net-a", "ip": "192.168.10.0"}],
    )
    plan = plan_addressing(spec)
    issues = validate_plan(plan, spec)
    assert any("network address" in i.problem for i in issues)


def test_validator_detects_broadcast_as_host():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24"}],
        [{"device": "pc1", "subnet": "net-a", "ip": "192.168.10.255"}],
    )
    plan = plan_addressing(spec)
    issues = validate_plan(plan, spec)
    assert any("broadcast address" in i.problem for i in issues)


def test_clean_plan_has_no_issues():
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24", "gateway": "192.168.10.1"}],
        [{"device": "pc1", "subnet": "net-a", "ip": "192.168.10.5"}],
    )
    plan = plan_addressing(spec)
    assert validate_plan(plan, spec) == []
    assert not has_errors([])


# ---------------------------------------------------------------------------
# Bug #34 - an address is written either `10.0.0.5` or `10.0.0.5/24`
# ---------------------------------------------------------------------------


def test_ip_accepts_the_cidr_style_as_well_as_the_bare_style():
    """`ip: "192.168.10.5/24"` is how an address is normally written, but the
    planner fed the raw string to `IPv4Address()`, which rejects anything with
    a slash - so the whole plan died with "invalid ip" instead of planning."""
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24", "gateway": "192.168.10.1"}],
        [{"device": "pc1", "subnet": "net-a", "ip": "192.168.10.5/24"}],
    )
    plan = plan_addressing(spec)
    assert plan.assignments[0].ip == "192.168.10.5"


def test_ip_prefix_that_disagrees_with_the_subnet_is_refused():
    """The address is inside the subnet but claims another prefix - that is a
    contradiction in the spec, not something to silently normalise away."""
    spec = _spec_with(
        [{"id": "net-a", "cidr": "192.168.10.0/24", "gateway": "192.168.10.1"}],
        [{"device": "pc1", "subnet": "net-a", "ip": "192.168.10.5/16"}],
    )
    with pytest.raises(PlanningFailure) as exc:
        plan_addressing(spec)
    assert "prefix" in str(exc.value)
