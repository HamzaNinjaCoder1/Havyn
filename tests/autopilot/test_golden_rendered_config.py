"""Golden rendered-config tests: the CLI each device type actually receives.

Issue #42: the suite never asserted on the rendered command lists as a whole,
so the invalid DHCP `network` line, the missing `switchport mode trunk`, the
access-port VLAN bug and the missing ASA config all shipped behind green
tests. Each test here pins the exact strings the compiler emits for one
device type; any regression in the renderer breaks the pin.

Everything is offline: spec dict -> NetworkSpec -> plan_addressing ->
compile_spec -> configure_ios commands. No Packet Tracer, no bridge, no
imports from packet_tracer_mcp (tests/independence_blocker.py enforces that).
"""

from __future__ import annotations

import pytest

from tests.autopilot.test_compiler import _compile_raw, _configure_cmds, _gateway_config_op


# -- helpers -----------------------------------------------------------------


def _interface_blocks(commands: list[str]) -> dict[str, list[str]]:
    """interface name -> the stripped lines rendered inside it.

    Unlike a plain scan, repeated `interface X` blocks (the ASA 5505 pushes
    its factory Vlan1 three times) accumulate into one entry, so a lookup
    sees every line ever rendered for that interface.
    """
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in commands:
        stripped = line.strip()
        if stripped.startswith("interface "):
            current = stripped[len("interface "):]
            blocks.setdefault(current, [])
        elif current is not None:
            blocks[current].append(stripped)
    return blocks


def _configure_ops(result) -> list:
    return [
        node.operation
        for node in result.dag.nodes.values()
        if node.operation.operation_type == "configure_ios"
    ]


# -- specs (small, one purpose each) -----------------------------------------


def _router_static_raw() -> dict:
    """Two routers back to back, a LAN per router, static routes both ways."""
    return {
        "name": "golden-router",
        "devices": [
            {"id": "r1", "role": "router", "model_hint": "2911"},
            {"id": "r2", "role": "router", "model_hint": "2911"},
            {"id": "pc1", "role": "pc"},
            {"id": "pc2", "role": "pc"},
        ],
        "links": [
            {"id": "l-wan", "a": {"device": "r1"}, "b": {"device": "r2"}},
            {"id": "l-lan1", "a": {"device": "r1"}, "b": {"device": "pc1"}},
            {"id": "l-lan2", "a": {"device": "r2"}, "b": {"device": "pc2"}},
        ],
        "subnets": [
            {"id": "wan", "cidr": "10.0.0.0/30"},
            {"id": "lan1", "cidr": "10.0.1.0/24"},
            {"id": "lan2", "cidr": "10.0.2.0/24"},
        ],
        "ip_assignments": [
            {"device": "r1", "interface_hint": "GigabitEthernet0/0",
             "subnet": "wan", "ip": "10.0.0.1"},
            {"device": "r2", "interface_hint": "GigabitEthernet0/0",
             "subnet": "wan", "ip": "10.0.0.2"},
            {"device": "r1", "interface_hint": "GigabitEthernet0/1",
             "subnet": "lan1", "ip": "10.0.1.1"},
            {"device": "r2", "interface_hint": "GigabitEthernet0/1",
             "subnet": "lan2", "ip": "10.0.2.1"},
            {"device": "pc1", "subnet": "lan1"},
            {"device": "pc2", "subnet": "lan2"},
        ],
        "routing": {
            "mode": "static",
            "static_routes": [
                {"device": "r1", "destination": "10.0.2.0/24",
                 "next_hop": "10.0.0.2"},
                {"device": "r2", "destination": "10.0.1.0/24",
                 "next_hop": "10.0.0.1"},
            ],
        },
    }


ROUTER_R1_GOLDEN = [
    "hostname r1",
    "interface GigabitEthernet0/0",
    "ip address 10.0.0.1 255.255.255.252",
    "no shutdown",
    "exit",
    "interface GigabitEthernet0/1",
    "ip address 10.0.1.1 255.255.255.0",
    "no shutdown",
    "exit",
    "ip route 10.0.2.0 255.255.255.0 10.0.0.2",
]


def test_router_ios_golden_rendered_config():
    """The full command list of a plain IOS router, line for line.

    Note on hostname position: the generic IOS path renders `hostname` FIRST
    (the command list starts with it); only the ASA and home-gateway paths
    append it last, where a rejected line must stay cosmetic. What matters on
    IOS is that it is rendered exactly once, attributed to the device id.
    """
    result = _compile_raw(_router_static_raw())
    cmds = _configure_cmds(result, "r1")
    assert cmds == ROUTER_R1_GOLDEN
    assert cmds.count("hostname r1") == 1
    # dotted masks only: `ip address 10.0.0.1/24` is not IOS syntax
    assert not any("/" in line for line in cmds if line.startswith("ip address"))
    # every addressed interface brings its own `no shutdown`
    for iface, lines in _interface_blocks(cmds).items():
        if any(line.startswith("ip address") for line in lines):
            assert "no shutdown" in lines, iface
    # the static route expands the CIDR into `network mask next-hop`
    assert "ip route 10.0.2.0 255.255.255.0 10.0.0.2" in cmds


def _router_ospf_nat_raw() -> dict:
    """Same two routers, but the spec asks for OSPF and PAT on r1."""
    raw = _router_static_raw()
    raw["name"] = "golden-ospf-nat"
    raw["routing"] = {
        "mode": "ospf",
        "ospf": {"process_id": 1, "networks": ["10.0.0.0/30", "10.0.1.0/24"],
                 "area": "0"},
        "nat": [
            {"router": "r1", "mode": "pat",
             "inside_interface": "GigabitEthernet0/1",
             "outside_interface": "GigabitEthernet0/0",
             "acl_number": "1", "use_interface_overload": True},
        ],
    }
    return raw


OSPF_NAT_R1_GOLDEN = [
    "hostname r1",
    "interface GigabitEthernet0/0",
    "ip address 10.0.0.1 255.255.255.252",
    "no shutdown",
    "exit",
    "interface GigabitEthernet0/1",
    "ip address 10.0.1.1 255.255.255.0",
    "no shutdown",
    "exit",
    "router ospf 1",
    " router-id r1",
    " network 10.0.0.0 0.0.0.3 area 0",
    " network 10.0.1.0 0.0.0.255 area 0",
    " exit",
    "interface GigabitEthernet0/1",
    " ip nat inside",
    " exit",
    "interface GigabitEthernet0/0",
    " ip nat outside",
    " exit",
    "ip nat inside source list 1 interface GigabitEthernet0/0 overload",
]

OSPF_NAT_R2_GOLDEN = [
    "hostname r2",
    "interface GigabitEthernet0/0",
    "ip address 10.0.0.2 255.255.255.252",
    "no shutdown",
    "exit",
    "interface GigabitEthernet0/1",
    "ip address 10.0.2.1 255.255.255.0",
    "no shutdown",
    "exit",
    "router ospf 1",
    " router-id r2",
    " network 10.0.0.0 0.0.0.3 area 0",
    " network 10.0.1.0 0.0.0.255 area 0",
    " exit",
]


def test_router_ios_renders_ospf_and_nat_when_the_spec_asks_for_them():
    result = _compile_raw(_router_ospf_nat_raw())
    r1 = _configure_cmds(result, "r1")
    assert r1 == OSPF_NAT_R1_GOLDEN
    # OSPF advertises with wildcard masks, never the CIDR form
    assert not any("10.0.0.0/30" in line or "10.0.1.0/24" in line for line in r1)
    # NAT stays on the router the spec names
    r2 = _configure_cmds(result, "r2")
    assert r2 == OSPF_NAT_R2_GOLDEN
    assert not any("ip nat" in line for line in r2)


def _switch_lab_raw() -> dict:
    """Router-on-a-stick lab: a trunk to the router, a trunk between the
    switches, one host on the declared VLAN 10 and one on a subnet with no
    VLAN at all."""
    return {
        "name": "golden-switch",
        "devices": [
            {"id": "r1", "role": "router", "model_hint": "2911"},
            {"id": "s1", "role": "switch", "model_hint": "2960-24TT"},
            {"id": "s2", "role": "switch", "model_hint": "2960-24TT"},
            {"id": "pc1", "role": "pc"},
            {"id": "pc2", "role": "pc"},
        ],
        "links": [
            {"id": "l-r1-s1", "a": {"device": "r1"}, "b": {"device": "s1"},
             "trunk": True},
            {"id": "l-s1-s2", "a": {"device": "s1"}, "b": {"device": "s2"},
             "trunk": True},
            {"id": "l-s1-pc1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
            {"id": "l-s2-pc2", "a": {"device": "s2"}, "b": {"device": "pc2"}},
        ],
        "vlans": [{"vlan_id": 10, "name": "USERS"}],
        "subnets": [
            {"id": "net-users", "cidr": "10.0.10.0/24", "vlan_id": 10,
             "dhcp": True},
            {"id": "net-guest", "cidr": "10.0.20.0/24"},
        ],
        "ip_assignments": [
            {"device": "r1", "interface_hint": "GigabitEthernet0/0.10",
             "subnet": "net-users", "ip": "10.0.10.1"},
            {"device": "pc1", "subnet": "net-users"},
            {"device": "pc2", "subnet": "net-guest"},
        ],
    }


def test_l2_switch_renders_access_and_trunk_ports_without_an_ip():
    """Bugs #1-#3, #7: switch-side ports, per-subnet VLAN, a real trunk.

    The rendered strings keep the renderer's leading space (the sub-mode
    indentation PT receives); a change to either content or shape breaks
    this pin.
    """
    result = _compile_raw(_switch_lab_raw())

    s1 = _configure_cmds(result, "s1")
    assert s1, "the switch must receive a configure_ios op"
    assert " switchport mode trunk" in s1
    assert " switchport trunk allowed vlan 10" in s1
    assert " switchport mode access" in s1
    assert " switchport access vlan 10" in s1
    assert "vlan 10" in s1
    assert " name USERS" in s1
    assert sum(1 for line in s1 if line.strip().startswith("hostname ")) == 1

    s1_blocks = _interface_blocks(s1)
    # every port block closes back to global config, or the next port's
    # lines land in the wrong interface context
    for iface, lines in s1_blocks.items():
        assert lines and lines[-1] == "exit", (iface, lines)

    # exactly one access port carries the declared VLAN 10 (the host on
    # net-users); it names the port the SWITCH side of the link owns
    access = {
        port: lines for port, lines in s1_blocks.items()
        if "switchport mode access" in lines
    }
    tagged = [
        port for port, lines in access.items()
        if any(l.startswith("switchport access vlan") for l in lines)
    ]
    assert len(tagged) == 1, access
    assert "switchport access vlan 10" in access[tagged[0]]

    # the guest host's subnet declares no VLAN: its access port gets no
    # vlan line at all, and the other switch still renders its trunk
    s2 = _configure_cmds(result, "s2")
    s2_blocks = _interface_blocks(s2)
    guest = [
        port for port, lines in s2_blocks.items()
        if "switchport mode access" in lines
        and not any(l.startswith("switchport access vlan") for l in lines)
    ]
    assert guest, s2_blocks
    assert " switchport mode trunk" in s2
    assert " switchport trunk allowed vlan 10" in s2

    # a pure L2 switch renders no address anywhere - not on an access port,
    # not on anything else
    for cmds in (s1, s2):
        assert not any(line.strip().startswith("ip address") for line in cmds)


def test_an_svi_carries_an_ip_address_only_when_the_device_is_l3():
    """The address lives on the SVI only where the compiler treats the
    device as L3 (ASA 5505 by CLI, the Linksys by its port-API op since #10);
    a plain 2960 never renders one."""
    # pure L2: no address line at all
    s1 = _configure_cmds(_compile_raw(_switch_lab_raw()), "s1")
    assert not any(line.strip().startswith("ip address") for line in s1)

    # ASA 5505: both subnet addresses ride the Vlan SVIs
    asa = _configure_cmds(_compile_raw(_asa_raw("5505")), "fw")
    asa_blocks = _interface_blocks(asa)
    assert "ip address 192.168.10.1 255.255.255.0" in asa_blocks["Vlan1"]
    assert "ip address 192.168.20.1 255.255.255.0" in asa_blocks["Vlan2"]

    # Linksys: the LAN address rides Vlan1 through the port-API op (#10) —
    # PT's Linksys answers configureIosDevice with `true` and applies nothing
    # (live probe), so there is no CLI block to inspect; the op itself is
    # what carries ip+mask onto Vlan1.
    result = _compile_raw(_wireless_router_raw())
    gw_op = _gateway_config_op(result, "wr")
    assert gw_op is not None
    assert gw_op.operation_type == "configure_gateway_lan"
    assert gw_op.inputs["ip"] == "192.168.0.1"
    assert gw_op.inputs["mask"] == "255.255.255.0"
    assert _configure_cmds(result, "wr") == []


# -- ASA firewalls ------------------------------------------------------------


def _asa_raw(model: str) -> dict:
    """pc-a -- fw(ASA) -- pc-b on two subnets, static route to a far net.

    Every assignment states its interface explicitly, the way a spec must
    spell an ASA (see my-network.spec.json): `GigabitEthernet1/x` ports on
    the 5506-X, `Vlan` SVIs (and the matching `Ethernet0/x` switchports on
    the links) on the 5505.
    """
    if model == "5506-X":
        link_hints = {"l-a": "GigabitEthernet1/1", "l-b": "GigabitEthernet1/2"}
        iface_hints = {"net-a": "GigabitEthernet1/1", "net-b": "GigabitEthernet1/2"}
    else:
        link_hints = {"l-a": "Ethernet0/0", "l-b": "Ethernet0/1"}
        iface_hints = {"net-a": "Vlan1", "net-b": "Vlan2"}
    return {
        "name": f"asa-golden-{model}",
        "devices": [
            {"id": "pc-a", "role": "pc"},
            {"id": "fw", "role": "firewall", "model_hint": model},
            {"id": "pc-b", "role": "pc"},
        ],
        "links": [
            {"id": "l-a", "a": {"device": "fw",
                                "interface_hint": link_hints["l-a"]},
             "b": {"device": "pc-a"}},
            {"id": "l-b", "a": {"device": "fw",
                                "interface_hint": link_hints["l-b"]},
             "b": {"device": "pc-b"}},
        ],
        "subnets": [
            {"id": "net-a", "cidr": "192.168.10.0/24"},
            {"id": "net-b", "cidr": "192.168.20.0/24"},
        ],
        "ip_assignments": [
            {"device": "fw", "interface_hint": iface_hints["net-a"],
             "subnet": "net-a", "ip": "192.168.10.1"},
            {"device": "fw", "interface_hint": iface_hints["net-b"],
             "subnet": "net-b", "ip": "192.168.20.1"},
            {"device": "pc-a", "subnet": "net-a"},
            {"device": "pc-b", "subnet": "net-b"},
        ],
        "routing": {
            "mode": "static",
            "static_routes": [
                {"device": "fw", "destination": "10.9.0.0/24",
                 "next_hop": "192.168.10.254"},
            ],
        },
    }


ASA_GOLDEN = {
    "5506-X": [
        "interface GigabitEthernet1/1",
        "ip address 192.168.10.1 255.255.255.0",
        "nameif inside",
        "security-level 100",
        "no shutdown",
        "exit",
        "interface GigabitEthernet1/2",
        "ip address 192.168.20.1 255.255.255.0",
        "nameif outside",
        "security-level 0",
        "no shutdown",
        "exit",
        "access-list out_in extended permit icmp any any",
        "access-group out_in in interface outside",
        "route inside 10.9.0.0 255.255.255.0 192.168.10.254 1",
        "hostname fw",
    ],
    "5505": [
        "interface Ethernet0/0",
        "switchport access vlan 1",
        # A 5505's physical ports default to shutdown: the switchport line
        # files the port in the right VLAN but leaves the link down, so the
        # inside Vlan SVI never reaches the outside neighbour (measured - the
        # port reported up=False despite being cabled). Bring it up here.
        "no shutdown",
        "exit",
        "interface Ethernet0/1",
        "switchport access vlan 2",
        "no shutdown",
        "exit",
        "interface Vlan1",
        "ip address 192.168.10.1 255.255.255.0",
        "exit",
        "interface Vlan1",
        "ip address 192.168.10.1 255.255.255.0",
        "exit",
        "interface Vlan1",
        "security-level 100",
        "no shutdown",
        "exit",
        "interface Vlan2",
        "ip address 192.168.20.1 255.255.255.0",
        "nameif outside",
        "security-level 0",
        "no shutdown",
        "exit",
        "access-list out_in extended permit icmp any any",
        "access-group out_in in interface outside",
        "route inside 10.9.0.0 255.255.255.0 192.168.10.254 1",
        "hostname fw",
    ],
}


@pytest.mark.parametrize("model", ["5506-X", "5505"], ids=["5506-x", "5505"])
def test_asa_firewall_golden_rendered_config(model: str):
    """Issue #14: an ASA used to receive no config op at all.

    Both models must render nameif + security-level on the interfaces that
    carry the subnets, the spec's static route in dotted form, and
    `hostname` LAST (configureIosDevice aborts at the first bad line, so an
    abort on the never-proven hostname line must stay cosmetic).
    """
    result = _compile_raw(_asa_raw(model))
    cmds = _configure_cmds(result, "fw")
    assert cmds, f"the {model} must receive a configure_ios op"
    assert any(line.startswith("nameif ") for line in cmds), cmds
    assert any(line.startswith("security-level ") for line in cmds), cmds
    assert "route inside 10.9.0.0 255.255.255.0 192.168.10.254 1" in cmds
    assert cmds[-1] == "hostname fw"
    assert sum(1 for line in cmds if line.startswith("hostname ")) == 1
    assert not any("/" in line for line in cmds if line.startswith("ip address"))
    assert cmds == ASA_GOLDEN[model]


# -- DHCP ---------------------------------------------------------------------


def _dhcp_router_raw() -> dict:
    """One router serving one LAN over a switch, DHCP on the subnet."""
    return {
        "name": "golden-dhcp",
        "devices": [
            {"id": "r1", "role": "router", "model_hint": "2911"},
            {"id": "s1", "role": "switch", "model_hint": "2960-24TT"},
            {"id": "pc1", "role": "pc"},
            {"id": "pc2", "role": "pc"},
        ],
        "links": [
            {"id": "l-r1-s1",
             "a": {"device": "r1", "interface_hint": "GigabitEthernet0/1"},
             "b": {"device": "s1"}},
            {"id": "l-s1-pc1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
            {"id": "l-s1-pc2", "a": {"device": "s1"}, "b": {"device": "pc2"}},
        ],
        "subnets": [{"id": "lan", "cidr": "10.0.0.0/24", "dhcp": True}],
        "ip_assignments": [
            {"device": "r1", "interface_hint": "GigabitEthernet0/1",
             "subnet": "lan", "ip": "10.0.0.1"},
            {"device": "pc1", "subnet": "lan"},
            {"device": "pc2", "subnet": "lan", "dhcp": True},
        ],
        "routing": {"mode": "connected"},
    }


DHCP_R1_GOLDEN = [
    "hostname r1",
    "interface GigabitEthernet0/1",
    "ip address 10.0.0.1 255.255.255.0",
    "no shutdown",
    "exit",
    " ip dhcp excluded-address 10.0.0.1",
    " ip dhcp pool lan",
    " network 10.0.0.0 255.255.255.0",
    " default-router 10.0.0.1",
    " exit",
]


def test_router_dhcp_pool_renders_valid_ios_syntax():
    """Issue #4: the pool shipped as `network 10.0.0.0/24`, which IOS
    rejects, so the hosts never got a lease. Pin the exact strings: dotted
    network + mask, the gateway excluded, and the pool order."""
    result = _compile_raw(_dhcp_router_raw())
    cmds = _configure_cmds(result, "r1")
    assert cmds == DHCP_R1_GOLDEN
    # the broken shape must never come back
    assert not any(
        line.strip().startswith("network ") and "/" in line for line in cmds
    ), cmds
    excluded = [
        line.strip() for line in cmds
        if line.strip().startswith("ip dhcp excluded-address")
    ]
    assert excluded == ["ip dhcp excluded-address 10.0.0.1"]
    # order: exclude the gateway, open the pool, state net+mask, then the router
    stripped = [line.strip() for line in cmds]
    assert (
        stripped.index("ip dhcp excluded-address 10.0.0.1")
        < stripped.index("ip dhcp pool lan")
        < stripped.index("network 10.0.0.0 255.255.255.0")
        < stripped.index("default-router 10.0.0.1")
    )


# -- wireless router ----------------------------------------------------------


def _wireless_router_raw() -> dict:
    """A Linksys-WRT300N serving one LAN to one PC over DHCP."""
    return {
        "name": "golden-linksys",
        "devices": [
            {"id": "wr", "role": "wireless_router",
             "model_hint": "Linksys-WRT300N"},
            {"id": "pc1", "role": "pc"},
        ],
        "links": [{"id": "l1", "a": {"device": "wr"}, "b": {"device": "pc1"}}],
        "subnets": [{"id": "lan", "cidr": "192.168.0.0/24", "dhcp": True}],
        "ip_assignments": [
            {"device": "wr", "subnet": "lan", "ip": "192.168.0.1"},
            {"device": "pc1", "subnet": "lan"},
        ],
        "routing": {"mode": "connected"},
    }


def test_wireless_router_gets_a_port_api_lan_op_not_dead_cli():
    """`role: wireless_router` (issue #36/#10): the Linksys must reach the
    gateway path — and since #10 that path is the port-API op, because PT's
    Linksys answers configureIosDevice with `true` and applies nothing (live
    probe). The LAN address and mask ride the op inputs; no CLI block and no
    hostname push (PT names the device at creation)."""
    result = _compile_raw(_wireless_router_raw())
    op = _gateway_config_op(result, "wr")
    assert op is not None, "a wireless router must receive a config op"
    assert op.operation_type == "configure_gateway_lan"
    assert op.inputs == {
        "device": "wr",
        "ip": "192.168.0.1",
        "mask": "255.255.255.0",
    }
    # neither the generic router path (GigabitEthernet0/x does not exist on
    # a Linksys) nor the dead CLI push may appear for this device
    assert _configure_cmds(result, "wr") == []
    assert not any(
        node.operation.operation_type == "configure_ios"
        and node.operation.inputs.get("device") == "wr"
        for node in result.dag.nodes.values()
    )
    # the factory-LAN DHCP client behind it stays legal (#10)
    assert "op-wait-dhcp" in result.dag.nodes


# -- modem / IoT --------------------------------------------------------------


def _modem_iot_raw() -> dict:
    """ISP cloud -- DSL modem -- router, plus an IoE thing and a PC."""
    return {
        "name": "golden-modem-iot",
        "devices": [
            {"id": "isp", "role": "cloud", "model_hint": "Cloud-PT"},
            {"id": "modem", "role": "modem", "model_hint": "DSL-Modem-PT"},
            {"id": "r1", "role": "router", "model_hint": "2911"},
            {"id": "iot", "role": "iot", "model_hint": "Smart LED"},
            {"id": "pc1", "role": "pc"},
        ],
        "links": [
            {"id": "l-line", "a": {"device": "isp"}, "b": {"device": "modem"}},
            {"id": "l-modem", "a": {"device": "modem"}, "b": {"device": "r1"}},
            {"id": "l-iot", "a": {"device": "r1"}, "b": {"device": "iot"}},
            {"id": "l-pc", "a": {"device": "r1"}, "b": {"device": "pc1"}},
        ],
        "subnets": [
            {"id": "wan", "cidr": "203.0.113.0/24"},
            {"id": "lan", "cidr": "192.168.1.0/24"},
        ],
        "ip_assignments": [
            {"device": "r1", "subnet": "wan", "ip": "203.0.113.2"},
            {"device": "r1", "subnet": "lan", "ip": "192.168.1.1"},
            {"device": "pc1", "subnet": "lan"},
        ],
        "routing": {"mode": "connected"},
    }


def test_modem_and_iot_are_created_and_cabled():
    """The `modem` and `iot` roles (issue #36) reach the build: once they
    could only be spelled `other`, which fell past every path.

    PT exposes no IOS CLI on a DSL modem or an IoE thing (see
    test_wan_cloud_modem.py and test_iot_device_gets_no_cli_config), so the
    ops these two receive are create_device plus their links - this pins
    that they are present and cabled instead of silently dropped."""
    result = _compile_raw(_modem_iot_raw())
    for device in ("modem", "iot"):
        assert f"op-create-{device}" in result.dag.nodes, device
    link_ids = {
        node.operation.inputs["link_id"]
        for node in result.dag.nodes.values()
        if node.operation.operation_type == "create_link"
    }
    assert {"l-line", "l-modem", "l-iot", "l-pc"} <= link_ids, link_ids
    # the router behind them is the device that gets configured
    assert _configure_cmds(result, "r1"), "the edge router must be configured"


# -- verdicts: one rich spec, every device type -------------------------------


def _rich_raw() -> dict:
    """ASA + router + L2 switch + Linksys, two DHCP subnets, static routes."""
    return {
        "name": "golden-rich",
        "devices": [
            {"id": "fw", "role": "firewall", "model_hint": "5506-X"},
            {"id": "r1", "role": "router", "model_hint": "2911"},
            {"id": "s1", "role": "switch", "model_hint": "2960-24TT"},
            {"id": "wr", "role": "wireless_router",
             "model_hint": "Linksys-WRT300N"},
            {"id": "pc1", "role": "pc"},
            {"id": "pc2", "role": "pc"},
            {"id": "pc3", "role": "pc"},
        ],
        "links": [
            {"id": "l-fw-r1",
             "a": {"device": "fw", "interface_hint": "GigabitEthernet1/1"},
             "b": {"device": "r1", "interface_hint": "GigabitEthernet0/0"}},
            {"id": "l-r1-s1",
             "a": {"device": "r1", "interface_hint": "GigabitEthernet0/1"},
             "b": {"device": "s1"}, "trunk": True},
            {"id": "l-s1-pc1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
            {"id": "l-s1-pc2", "a": {"device": "s1"}, "b": {"device": "pc2"}},
            {"id": "l-wr-pc3", "a": {"device": "wr"}, "b": {"device": "pc3"}},
        ],
        "vlans": [{"vlan_id": 10, "name": "USERS"}],
        "subnets": [
            {"id": "net-edge", "cidr": "192.168.10.0/24"},
            {"id": "net-lan", "cidr": "10.0.10.0/24", "vlan_id": 10,
             "dhcp": True},
            {"id": "net-guest", "cidr": "10.0.20.0/24"},
            {"id": "net-home", "cidr": "192.168.0.0/24", "dhcp": True},
        ],
        "ip_assignments": [
            {"device": "fw", "interface_hint": "GigabitEthernet1/1",
             "subnet": "net-edge", "ip": "192.168.10.1"},
            {"device": "r1", "interface_hint": "GigabitEthernet0/0",
             "subnet": "net-edge", "ip": "192.168.10.254"},
            {"device": "r1", "interface_hint": "GigabitEthernet0/1",
             "subnet": "net-lan", "ip": "10.0.10.1"},
            {"device": "wr", "subnet": "net-home", "ip": "192.168.0.1"},
            {"device": "pc1", "subnet": "net-lan"},
            {"device": "pc2", "subnet": "net-guest"},
            {"device": "pc3", "subnet": "net-home"},
        ],
        "routing": {
            "mode": "static",
            "static_routes": [
                {"device": "fw", "destination": "10.0.10.0/24",
                 "next_hop": "192.168.10.254"},
                {"device": "r1", "destination": "192.168.0.0/24",
                 "next_hop": "192.168.10.1"},
            ],
        },
    }


def test_rich_spec_renders_sane_commands_for_every_device_type():
    """Verdicts on the whole build: no empty list, no duplicated hostname,
    no stray `None`, no raw CIDR left in an address/route/pool line."""
    result = _compile_raw(_rich_raw())
    ops = _configure_ops(result)
    # the Linksys takes no CLI block since #10 - it renders the port-API op
    assert {op.inputs["device"] for op in ops} == {"fw", "r1", "s1"}

    for op in ops:
        cmds = op.inputs["commands"]
        assert cmds, f"{op.operation_id} rendered an empty command list"
        hostnames = [
            line for line in cmds if line.strip().startswith("hostname ")
        ]
        assert len(hostnames) == 1, (op.operation_id, cmds)
        for line in cmds:
            assert line.strip(), (op.operation_id, line)
            assert "None" not in line, (op.operation_id, line)
            stripped = line.strip()
            if stripped.startswith(
                ("ip address ", "ip route ", "route ", "network ",
                 "default-router ")
            ):
                assert "/" not in stripped, (op.operation_id, line)

    # and each device type still renders its signature lines
    by_device = {op.inputs["device"]: op.inputs["commands"] for op in ops}
    assert "nameif inside" in by_device["fw"]
    # an ASA static route renders in the ASA's own form (S1.19 live root
    # cause: PT's ASA silently ignores `ip route`)
    assert "route inside 10.0.10.0 255.255.255.0 192.168.10.254 1" in by_device["fw"]
    assert by_device["fw"][-1] == "hostname fw"
    assert "ip address 10.0.10.1 255.255.255.0" in by_device["r1"]
    assert " ip dhcp pool net-lan" in by_device["r1"]
    assert " switchport mode trunk" in by_device["s1"]
    assert " switchport access vlan 10" in by_device["s1"]
    # the Linksys: factory-LAN assignment, port-API push, no dead CLI (#10)
    wr_op = _gateway_config_op(result, "wr")
    assert wr_op is not None and wr_op.operation_type == "configure_gateway_lan"
    assert wr_op.inputs["ip"] == "192.168.0.1"
    assert wr_op.inputs["mask"] == "255.255.255.0"
