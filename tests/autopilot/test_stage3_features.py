"""Stage 3 feature rendering and honest refusals.

These tests are intentionally written against the public compiler surface:
they assert the exact IOS/ASA lines a feature produces, and that a feature
PT cannot configure is refused by name instead of silently dropped.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.compiler import CompileError, compile_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing


def _base(**overrides) -> dict:
    data = {
        "name": "stage3-unit",
        "devices": [
            {"id": "r1", "role": "router"},
            {"id": "r2", "role": "router"},
        ],
        "links": [
            {"id": "l1", "a": {"device": "r1"}, "b": {"device": "r2"},
             "media": "serial"},
        ],
        "subnets": [{"id": "wan", "cidr": "10.0.0.0/30"}],
        "ip_assignments": [
            {"device": "r1", "subnet": "wan", "ip": "10.0.0.1"},
            {"device": "r2", "subnet": "wan", "ip": "10.0.0.2"},
        ],
    }
    data.update(overrides)
    return data


def _commands(data: dict) -> dict[str, list[str]]:
    spec = NetworkSpec.model_validate(data)
    result = compile_spec(spec, plan_addressing(spec))
    return {
        node.operation.inputs["device"]: node.operation.inputs["commands"]
        for node in result.dag.nodes.values()
        if node.operation.operation_type == "configure_ios"
    }


def test_ospf_multi_area_renders_each_network_in_its_own_area():
    commands = _commands(_base(routing={
        "mode": "ospf",
        "ospf": {
            "process_id": 7,
            "area_networks": [
                {"network": "10.0.0.0/30", "area": "0"},
                {"network": "10.10.0.0/24", "area": "1"},
            ],
            "default_information_originate": True,
            "redistribute": [{"protocol": "static"}],
        },
    }))
    text = "\n".join(commands["r1"])
    assert "router ospf 7" in text
    assert "network 10.0.0.0 0.0.0.3 area 0" in text
    assert "network 10.10.0.0 0.0.0.255 area 1" in text
    assert "default-information originate" in text
    assert "redistribute static" in text


def test_eigrp_renders_as_number_and_wildcard_networks():
    text = "\n".join(_commands(_base(routing={
        "mode": "eigrp",
        "eigrp": {"as_number": 42, "networks": ["10.0.0.0/30"]},
    }))["r1"])
    assert "router eigrp 42" in text
    assert "network 10.0.0.0 0.0.0.3" in text


def test_rip_v2_renders_version_and_no_auto_summary():
    text = "\n".join(_commands(_base(routing={
        "mode": "rip",
        "rip": {"version": 2, "networks": ["10.0.0.0/30"]},
    }))["r1"])
    assert "router rip" in text
    assert "version 2" in text
    assert "no auto-summary" in text


def test_bgp_renders_neighbor_and_network_mask():
    text = "\n".join(_commands(_base(routing={
        "mode": "bgp",
        "bgp": {
            "as_number": 65001,
            "neighbors": [{"address": "10.0.0.2", "remote_as": 65002}],
            "networks": ["10.10.0.0/24"],
        },
    }))["r1"])
    assert "router bgp 65001" in text
    assert "neighbor 10.0.0.2 remote-as 65002" in text
    assert "network 10.10.0.0 mask 255.255.255.0" in text


def test_hsrp_and_dhcp_relay_render_on_the_named_interface():
    text = "\n".join(_commands(_base(routing={
        "hsrp": [{
            "device": "r1", "interface": "GigabitEthernet0/0",
            "group": 5, "virtual_ip": "10.0.0.254", "priority": 120,
        }],
        "dhcp_relays": [{
            "device": "r2", "interface": "GigabitEthernet0/0",
            "helpers": ["10.9.9.9"],
        }],
    }))["r1"])
    assert "standby 5 ip 10.0.0.254" in text
    assert "standby 5 priority 120" in text
    assert "standby 5 preempt" in text
    relay = "\n".join(_commands(_base(routing={
        "dhcp_relays": [{
            "device": "r2", "interface": "GigabitEthernet0/0",
            "helpers": ["10.9.9.9"],
        }],
    }))["r2"])
    assert "ip helper-address 10.9.9.9" in relay


def test_switch_features_render_svi_etherchannel_stp_and_security():
    data = _base()
    data["devices"] = [
        {"id": "r1", "role": "router"},
        {"id": "r2", "role": "router"},
        {"id": "sw1", "role": "switch"},
        {"id": "sw2", "role": "switch"},
        {"id": "sw3", "role": "switch"},
        {"id": "sw4", "role": "switch"},
        {"id": "sw5", "role": "switch"},
        {"id": "sw6", "role": "switch"},
        {"id": "sw7", "role": "l3_switch"},
    ]
    data["links"] = [{"id": "l1", "a": {"device": "r1"}, "b": {"device": "r2"}, "media": "serial"}]
    data["switching"] = {
        "svis": [{"device": "sw7", "vlan": 10, "ip": "10.9.0.1", "mask": "255.255.255.0"}],
        "etherchannels": [{"device": "sw1", "group": 1, "members": ["FastEthernet0/1", "FastEthernet0/2"]}],
        "stp": [{"device": "sw2", "vlan": 10, "root": "primary",
                 "portfast_interfaces": ["FastEthernet0/1"]}],
        "port_security": [{"device": "sw3", "interface": "FastEthernet0/1",
                           "maximum": 2, "violation": "restrict"}],
        "vtp": [{"device": "sw4", "domain": "LAB", "mode": "server"}],
        "voice_vlans": [{"device": "sw5", "interface": "FastEthernet0/1",
                         "voice_vlan": 20, "access_vlan": 10}],
        "management_ips": [{"device": "sw6", "vlan": 1, "ip": "10.9.0.2",
                            "mask": "255.255.255.0", "gateway": "10.9.0.1"}],
    }
    commands = {key: "\n".join(lines) for key, lines in _commands(data).items()}
    assert "ip routing" in commands["sw7"]
    assert "interface Vlan10" in commands["sw7"]
    assert "channel-group 1 mode active" in commands["sw1"]
    assert "spanning-tree vlan 10 root primary" in commands["sw2"]
    assert "switchport port-security maximum 2" in commands["sw3"]
    assert "vtp domain LAB" in commands["sw4"]
    assert "switchport voice vlan 20" in commands["sw5"]
    assert "ip default-gateway 10.9.0.1" in commands["sw6"]


def test_asa_features_render_object_nat_dhcp_and_object_group():
    data = _base()
    data["devices"] = [
        {"id": "r1", "role": "router"},
        {"id": "r2", "role": "router"},
        {"id": "fw1", "role": "firewall"},
    ]
    data["links"] = [{"id": "l1", "a": {"device": "r1"}, "b": {"device": "r2"}, "media": "serial"}]
    data["firewall"] = {
        "nat": [{"device": "fw1", "object_name": "INSIDE", "subnet": "10.0.0.0 255.255.255.0"}],
        "dhcp": [{"device": "fw1", "nameif": "inside", "start": "10.0.0.100", "end": "10.0.0.150"}],
        "object_groups": [{"device": "fw1", "name": "WEB", "members": ["10.0.0.10"]}],
    }
    commands = _commands(data)
    text = "\n".join(commands["fw1"])
    assert "object network INSIDE" in text
    assert "nat (inside,outside) dynamic interface" in text
    assert "dhcpd address 10.0.0.100-10.0.0.150 inside" in text
    assert "object-group network WEB" in text


def test_server_services_are_refused_by_name():
    data = _base(services=[{"device": "r1", "service": "dns"}])
    with pytest.raises(CompileError, match="Server-PT services"):
        _commands(data)


def test_iot_registration_is_refused_by_name():
    data = _base(iot=[{"device": "r1", "server": "srv1"}])
    with pytest.raises(CompileError, match="IoT registration"):
        _commands(data)


def test_feature_referencing_an_unknown_device_is_rejected():
    with pytest.raises(ValueError, match="feature references unknown device"):
        NetworkSpec.model_validate(_base(
            routing={"hsrp": [{"device": "ghost", "interface": "Gi0/0",
                                "virtual_ip": "10.0.0.1"}]},
        ))


def test_new_end_device_roles_resolve_to_catalogued_models():
    from src.network_autopilot.core.network_spec import default_model_for_role

    assert default_model_for_role("printer") == "Printer-PT"
    assert default_model_for_role("ip_phone") == "7960"
    assert default_model_for_role("tablet") == "TabletPC-PT"
    assert default_model_for_role("smartphone") == "SMARTPHONE-PT"
    assert default_model_for_role("wlc") == "WLC-PT"


def test_host_dns_reaches_the_configure_host_operation():
    data = _base()
    data["devices"].append({"id": "pc1", "role": "pc"})
    data["links"].append({"id": "l2", "a": {"device": "r1"}, "b": {"device": "pc1"}})
    data["subnets"].append({"id": "lan", "cidr": "10.10.0.0/24"})
    data["ip_assignments"].append({"device": "pc1", "subnet": "lan"})
    data["host_dns"] = [{"device": "pc1", "dns_server": "10.10.0.53"}]
    spec = NetworkSpec.model_validate(data)
    result = compile_spec(spec, plan_addressing(spec))
    op = next(
        node.operation for node in result.dag.nodes.values()
        if node.operation.operation_type == "configure_host"
        and node.operation.inputs["device"] == "pc1"
    )
    assert op.inputs["dns"] == "10.10.0.53"


def test_a_failed_flow_on_a_feature_device_proposes_a_recompile_repair():
    """The diagnosis must point at the feature block, not guess a line."""
    from src.network_autopilot.core.actual_state import ActualNetworkState
    from src.network_autopilot.core.requirements import AcceptanceReport, Verdict
    from src.network_autopilot.diagnosis import diagnose
    from src.network_autopilot.verification.engine import (
        VerificationReport,
        VerificationTest,
    )

    data = _base(routing={
        "hsrp": [{"device": "r1", "interface": "GigabitEthernet0/0",
                  "virtual_ip": "10.0.0.254"}],
    })
    spec = NetworkSpec.model_validate(data)
    test = VerificationTest(
        test_id="VT-flow-A-1",
        level="connectivity",
        source_requirement="R-1",
        source_acceptance_criterion="A-1",
        source="r1",
        target="r2",
        expected_result="reachable (icmp)",
        observed_result="blocked",
        evidence=("100% loss",),
        timestamp="2026-10-23T00:00:00+00:00",
        verdict=Verdict.FAIL,
    )
    report = VerificationReport(
        tests=(test,),
        summary={"fail": 1},
        acceptance=AcceptanceReport(
            results=(), requirement_results=(), summary={}, satisfied=False
        ),
        observations_complete=True,
    )
    diagnoses = diagnose(
        spec, report, ActualNetworkState(source="test", observed_at="2026-10-23")
    )
    repairs = [
        diagnosis for diagnosis in diagnoses
        if diagnosis.proposed_repair
        and diagnosis.proposed_repair[0]["operation_type"] == "ios_command"
    ]
    assert repairs, [d.to_dict() for d in diagnoses]
    assert repairs[0].affected_resources == ("r1",)
    assert repairs[0].proposed_repair[0]["inputs"]["source"] == "spec"


def test_gre_tunnel_renders_interface_source_and_gre_ip_mode():
    text = "\n".join(_commands(_base(routing={
        "tunnels": [{
            "device": "r1", "tunnel_id": 0, "ip": "172.16.0.1/30",
            "source": "GigabitEthernet0/0", "destination": "10.0.0.2",
            "mode": "gre",
        }],
    }))["r1"])
    assert "interface Tunnel0" in text
    assert "tunnel source GigabitEthernet0/0" in text
    assert "tunnel destination 10.0.0.2" in text
    assert "tunnel mode gre ip" in text


def test_bgp_per_device_renders_each_autonomous_system():
    text = {key: "\n".join(lines) for key, lines in _commands(_base(routing={
        "mode": "bgp",
        "bgp": {"as_number": 65001, "per_device": [
            {"device": "r1", "as_number": 65001,
             "neighbors": [{"address": "10.0.0.2", "remote_as": 65002}],
             "networks": ["10.10.0.0/24"]},
            {"device": "r2", "as_number": 65002,
             "neighbors": [{"address": "10.0.0.1", "remote_as": 65001}],
             "networks": ["10.20.0.0/24"]},
        ]},
    })).items()}
    assert "router bgp 65001" in text["r1"]
    assert "neighbor 10.0.0.2 remote-as 65002" in text["r1"]
    assert "router bgp 65002" in text["r2"]
    assert "neighbor 10.0.0.1 remote-as 65001" in text["r2"]


def test_dhcp_pool_renders_on_the_chosen_device():
    data = _base()
    data["devices"].append({"id": "sw1", "role": "switch"})
    data["links"].append({"id": "l2", "a": {"device": "r2"}, "b": {"device": "sw1"}})
    data["subnets"].append({"id": "remote", "cidr": "10.0.2.0/24"})
    data["ip_assignments"].append(
        {"device": "r2", "subnet": "remote", "ip": "10.0.2.1"}
    )
    data["routing"] = {
        "mode": "static",
        "dhcp_pools": [{"device": "r1", "subnet": "remote",
                        "default_router": "10.0.2.1",
                        "excluded": ["10.0.2.1"],
                        "dns_servers": ["10.0.0.53"]}],
    }
    text = "\n".join(_commands(data)["r1"])
    assert "ip dhcp excluded-address 10.0.2.1" in text
    assert "ip dhcp pool remote" in text
    assert "network 10.0.2.0 255.255.255.0" in text
    assert "default-router 10.0.2.1" in text
    assert "dns-server 10.0.0.53" in text


def test_host_gateway_override_reaches_the_host_operation():
    data = _base()
    data["devices"].append({"id": "pc1", "role": "pc"})
    data["links"].append({"id": "l2", "a": {"device": "r1"}, "b": {"device": "pc1"}})
    data["subnets"].append({"id": "lan", "cidr": "10.10.0.0/24"})
    data["ip_assignments"].append(
        {"device": "pc1", "subnet": "lan", "gateway": "10.10.0.254"}
    )
    spec = NetworkSpec.model_validate(data)
    result = compile_spec(spec, plan_addressing(spec))
    op = next(
        node.operation for node in result.dag.nodes.values()
        if node.operation.operation_type == "configure_host"
        and node.operation.inputs["device"] == "pc1"
    )
    assert op.inputs["gateway"] == "10.10.0.254"
