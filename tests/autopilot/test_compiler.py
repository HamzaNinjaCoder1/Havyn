"""Compiler: deterministic DAG generation from spec + addressing plan."""

from __future__ import annotations

import pytest

from src.network_autopilot.compiler import CompileError, compile_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing

GOLDEN_SPEC = {
    "name": "two-routers-lab",
    "devices": [
        {"id": "r1", "role": "router"},
        {"id": "r2", "role": "router"},
        {"id": "s1", "role": "switch"},
        {"id": "s2", "role": "switch"},
        {"id": "pc1", "role": "pc"},
        {"id": "pc2", "role": "pc"},
        {"id": "pc3", "role": "pc"},
        {"id": "pc4", "role": "pc"},
    ],
    "links": [
        {"id": "l-r1-r2", "a": {"device": "r1"}, "b": {"device": "r2"}},
        {"id": "l-r1-s1", "a": {"device": "r1"}, "b": {"device": "s1"}},
        {"id": "l-r2-s2", "a": {"device": "r2"}, "b": {"device": "s2"}},
        {"id": "l-s1-pc1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
        {"id": "l-s1-pc2", "a": {"device": "s1"}, "b": {"device": "pc2"}},
        {"id": "l-s2-pc3", "a": {"device": "s2"}, "b": {"device": "pc3"}},
        {"id": "l-s2-pc4", "a": {"device": "s2"}, "b": {"device": "pc4"}},
    ],
    "subnets": [
        {"id": "net-wan", "cidr": "10.0.0.0/30"},
        {"id": "net-lan1", "cidr": "10.0.1.0/24"},
        {"id": "net-lan2", "cidr": "10.0.2.0/24"},
    ],
    "ip_assignments": [
        {"device": "r1", "interface_hint": "GigabitEthernet0/0", "subnet": "net-wan", "ip": "10.0.0.1"},
        {"device": "r2", "interface_hint": "GigabitEthernet0/0", "subnet": "net-wan", "ip": "10.0.0.2"},
        {"device": "r1", "interface_hint": "GigabitEthernet0/1", "subnet": "net-lan1", "ip": "10.0.1.1"},
        {"device": "r2", "interface_hint": "GigabitEthernet0/1", "subnet": "net-lan2", "ip": "10.0.2.1"},
        {"device": "pc1", "subnet": "net-lan1"},
        {"device": "pc2", "subnet": "net-lan1"},
        {"device": "pc3", "subnet": "net-lan2"},
        {"device": "pc4", "subnet": "net-lan2"},
    ],
    "routing": {
        "mode": "static",
        "static_routes": [
            {"device": "r1", "destination": "10.0.2.0/24", "next_hop": "10.0.0.2"},
            {"device": "r2", "destination": "10.0.1.0/24", "next_hop": "10.0.0.1"},
        ],
    },
    "requirements": [
        {"id": "R-1", "statement": "pc1 reaches pc4"},
    ],
    "acceptance_criteria": [
        {
            "id": "A-1",
            "requirement_ids": ["R-1"],
            "test": {"kind": "flow", "source": "pc1", "destination": "pc4", "expect": "reachable"},
        }
    ],
}


def _golden() -> tuple[NetworkSpec, object]:
    spec = NetworkSpec.model_validate(GOLDEN_SPEC)
    return spec, plan_addressing(spec)


def test_compile_produces_expected_operation_shape():
    spec, plan = _golden()
    result = compile_spec(spec, plan)
    types = [
        node.operation.operation_type
        for node in result.dag.nodes.values()
    ]
    assert types.count("create_device") == 8
    assert types.count("create_link") == 7
    assert types.count("configure_ios") == 2
    assert types.count("configure_host") == 4
    assert types.count("verify_flow") == 1
    assert types.count("verify_state") == 1
    assert types.count("save_project") == 1


def test_compile_ordering_devices_before_links_before_config():
    spec, plan = _golden()
    result = compile_spec(spec, plan)
    order = list(result.dag.topo_order())
    pos = {op_id: i for i, op_id in enumerate(order)}
    assert pos["op-create-r1"] < pos["op-link-l-r1-s1"]
    assert pos["op-link-l-s1-pc1"] < pos["op-config-r1"]
    assert pos["op-config-r1"] < pos["op-flow-A-1"]
    assert pos["op-flow-A-1"] < pos["op-save"]


def test_compile_is_deterministic():
    spec, plan = _golden()
    first = compile_spec(spec, plan).to_dict()
    second = compile_spec(spec, plan).to_dict()
    assert first == second


def test_router_config_contains_addresses_and_routes():
    spec, plan = _golden()
    result = compile_spec(spec, plan)
    r1 = result.dag.nodes["op-config-r1"].operation
    commands = r1.inputs["commands"]
    assert "interface GigabitEthernet0/0" in commands
    assert "ip address 10.0.0.1 255.255.255.252" in commands
    assert "ip address 10.0.1.1 255.255.255.0" in commands
    assert "ip route 10.0.2.0/24 10.0.0.2" in commands
    assert r1.inputs["static_routes"] == [
        {"destination": "10.0.2.0/24", "next_hop": "10.0.0.2", "exit_interface": ""}
    ]


def test_host_configs_use_gateway_and_auto_ips():
    spec, plan = _golden()
    result = compile_spec(spec, plan)
    pc1 = result.dag.nodes["op-host-pc1"].operation
    assert pc1.inputs["ip"] == "10.0.1.2"      # gateway .1, first host .2
    assert pc1.inputs["gateway"] == "10.0.1.1"
    assert pc1.inputs["mask"] == "255.255.255.0"
    pc4 = result.dag.nodes["op-host-pc4"].operation
    assert pc4.inputs["ip"] == "10.0.2.3"
    assert pc4.inputs["gateway"] == "10.0.2.1"


def test_idempotency_keys_are_stable_and_distinct():
    spec, plan = _golden()
    result = compile_spec(spec, plan)
    keys = [
        node.operation.idempotency_key
        for node in result.dag.nodes.values()
    ]
    assert all(keys)
    assert len(keys) == len(set(keys))


def test_vlan_specs_are_refused_by_name():
    raw = dict(GOLDEN_SPEC)
    raw["vlans"] = [{"vlan_id": 10, "name": "X"}]
    spec = NetworkSpec.model_validate(raw)
    plan = plan_addressing(spec)
    with pytest.raises(CompileError, match="vlans"):
        compile_spec(spec, plan)


def test_dhcp_is_refused_in_phase_2():
    raw = dict(GOLDEN_SPEC)
    raw["ip_assignments"] = [
        {"device": "pc1", "subnet": "net-lan1"},   # auto -> static, fine
        {"device": "pc2", "subnet": "net-lan1", "dhcp": True},
    ]
    spec = NetworkSpec.model_validate(raw)
    plan = plan_addressing(spec)
    with pytest.raises(CompileError, match="DHCP"):
        compile_spec(spec, plan)


def test_subinterfaces_are_refused():
    raw = dict(GOLDEN_SPEC)
    raw["ip_assignments"] = [
        {"device": "r1", "interface_hint": "GigabitEthernet0/0.10", "subnet": "net-wan", "ip": "10.0.0.1"},
    ]
    spec = NetworkSpec.model_validate(raw)
    plan = plan_addressing(spec)
    with pytest.raises(CompileError, match="subinterface"):
        compile_spec(spec, plan)


def test_positions_derive_from_layout_seed():
    spec, plan = _golden()
    a = compile_spec(spec, plan)
    spec_b = NetworkSpec.model_validate({**GOLDEN_SPEC, "layout_seed": 7})
    b = compile_spec(spec_b, plan_addressing(spec_b))
    pos_a = a.dag.nodes["op-create-r1"].operation.inputs["position"]
    pos_b = b.dag.nodes["op-create-r1"].operation.inputs["position"]
    assert pos_a != pos_b  # seed actually shifts the layout
    again = compile_spec(spec_b, plan_addressing(spec_b))
    assert again.dag.nodes["op-create-r1"].operation.inputs["position"] == pos_b


def test_fingerprint_binds_spec_compiler_and_seed():
    spec, plan = _golden()
    result = compile_spec(spec, plan, packet_tracer_version="9.0.0")
    assert result.fingerprint["spec_hash"] == spec.spec_hash()
    assert result.fingerprint["components"]["compiler"] == "2.0.0"
    assert result.fingerprint["layout_seed"] == str(spec.layout_seed)
