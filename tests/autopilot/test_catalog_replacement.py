"""Catalog replacement: device, module and cable lookups.

Written against the interface contracts the engine relies on — these pin the
behaviour callers depend on, not the upstream table's contents.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.pt.catalog import (
    devices,
    modules,
    cables,
)
from src.network_autopilot.pt.enums import PortSpeed


class TestDeviceCatalog:
    def test_known_model_resolves(self):
        m = devices.resolve_model("2911")
        assert m is not None
        assert m.category == "router"
        assert any(p.full_name == "GigabitEthernet0/0" for p in m.ports)

    def test_unknown_returns_none_not_raise(self):
        assert devices.resolve_model("not-a-model") is None
        assert devices.resolve_model("") is None

    def test_role_word_resolves_to_a_chassis(self):
        m = devices.resolve_model("router")
        assert m is not None and m.pt_type == "2911"
        assert devices.resolve_model("pc").pt_type == "PC-PT"

    def test_alias_normalises(self):
        assert devices.resolve_model("ISR").pt_type == devices.resolve_model("2911").pt_type
        assert devices.resolve_model("ap").pt_type == "AccessPoint-PT"

    def test_category_of_model_returns_empty_string_for_unknown(self):
        # "" not None: call sites use it as a truthiness gate.
        assert devices.category_of_model("zzz") == ""

    def test_category_of_known(self):
        assert devices.category_of_model("2960-24TT") == "switch"
        assert devices.category_of_model("PC-PT") == "pc"

    def test_port_speed_is_a_str_enum_member(self):
        m = devices.resolve_model("2911")
        fe = next(p for p in m.ports if p.speed == PortSpeed.GIGABIT_ETHERNET)
        assert fe.full_name.startswith("GigabitEthernet")
        # both comparison styles must work
        assert fe.speed == PortSpeed.GIGABIT_ETHERNET
        assert fe.speed.value == "GigabitEthernet"

    def test_2911_lists_only_its_copper_ports(self):
        """A phantom port is worse than a missing one: PT's IOS parser accepts
        an unknown interface name and still reports success, so a build would
        "pass" while carrying no traffic."""
        names = [p.full_name for p in devices.resolve_model("2911").ports]
        assert names == [
            "GigabitEthernet0/0", "GigabitEthernet0/1", "GigabitEthernet0/2",
        ]

    def test_hub_uses_its_unslashed_port_names(self):
        names = [p.full_name for p in devices.resolve_model("Hub-PT").ports]
        assert names == [f"FastEthernet{i}" for i in range(6)]

    def test_generic_router_has_no_phantom_gigabit_ports(self):
        """Enumerated live: PT's Router-PT answers FastEthernet0/0, 1/0, 4/0,
        5/0 and two serials — and no GigabitEthernet at all. A phantom port
        here would have the solver pick a port PT cannot cable, and the only
        symptom is "PT reported 'false'" at link time."""
        names = [p.full_name for p in devices.resolve_model("Router-PT").ports]
        assert names == [
            "FastEthernet0/0", "FastEthernet1/0", "FastEthernet4/0",
            "FastEthernet5/0", "Serial2/0", "Serial3/0",
        ]

    def test_switch_copper_ports_start_at_one(self):
        """Enumerated live: PT numbers these chassis' copper ports 0/1..0/24.
        There is no FastEthernet0/0 — a solver that hands a link 0/0 gets a bare
        'false' from addLink that looks like a bad cable."""
        for model in ("2960-24TT", "2950-24", "3560-24PS"):
            names = [p.full_name for p in devices.resolve_model(model).ports]
            assert "FastEthernet0/0" not in names, model
            assert names[0] == "FastEthernet0/1", model
            assert "FastEthernet0/24" in names, model

    def test_generic_switch_ports_match_the_live_chassis(self):
        names = [p.full_name for p in devices.resolve_model("Switch-PT").ports]
        assert names[:6] == [
            "FastEthernet0/1", "FastEthernet1/1", "FastEthernet2/1",
            "FastEthernet3/1", "FastEthernet4/1", "FastEthernet5/1",
        ]

    def test_asa_5505_ports_match_the_live_chassis(self):
        """Enumerated live: Vlan1, Vlan2 and Ethernet0/0..0/7. The catalog used
        to claim a GigabitEthernet0/8 and a Console0 - neither exists, and the
        ASA's device object has no getCommandLine() at all, so a console port
        could only ever produce a link PT refuses. The two Vlan SVIs are the
        other half of the truth: the 5505 routes on them, which is why the
        compiler joins a switchport to a vlan and addresses the Vlan."""
        names = [p.full_name for p in devices.resolve_model("5505").ports]
        assert names == ["Vlan1", "Vlan2"] + [
            f"Ethernet0/{i}" for i in range(8)
        ]

    def test_asa_5506x_ports_match_the_live_chassis(self):
        """Enumerated live: GigabitEthernet1/1..1/8 plus Management1/1. There is
        no GigabitEthernet1/9 and no console."""
        names = [p.full_name for p in devices.resolve_model("5506-X").ports]
        assert names == [f"GigabitEthernet1/{i}" for i in range(1, 9)] + [
            "Management1/1"
        ]

    def test_asa_vlan_interfaces_are_svis_not_cablable_ports(self):
        """A Vlan interface is where a routed address lives, not something a
        cable plugs into. If the port solver may offer one, addLink is handed an
        endpoint PT cannot cable and answers a bare 'false' that reads like a
        bad cable - which is exactly what happened before the SVI speed existed.
        """
        from src.network_autopilot.pt.enums import PortSpeed

        for model in ("5505", "5506-X"):
            m = devices.resolve_model(model)
            for port in m.ports:
                if port.full_name.startswith(("Vlan", "Management")):
                    assert port.speed is PortSpeed.SVI, (model, port.full_name)
                else:
                    assert port.speed is not PortSpeed.SVI, (model, port.full_name)

    def test_get_valid_ports(self):
        names = devices.get_valid_ports("2911")
        assert "GigabitEthernet0/0" in names
        assert devices.get_valid_ports("zzz") == set()

    def test_get_ports_by_speed(self):
        m = devices.resolve_model("2911")
        ge = devices.get_ports_by_speed(m, PortSpeed.GIGABIT_ETHERNET)
        assert len(ge) >= 2
        assert devices.get_ports_by_speed(m, "GigabitEthernet") == ge


class TestModuleCatalog:
    def test_resolve_known_and_unknown(self):
        assert modules.resolve_module("HWIC-2T") is not None
        assert modules.resolve_module("nope") is None
        assert modules.resolve_module("") is None

    def test_case_insensitive(self):
        assert modules.resolve_module("hwic-2t").name == "HWIC-2T"

    def test_slot_rewrite_is_exact(self):
        spec = modules.resolve_module("HWIC-2T")
        assert modules.ports_for_slot(spec, "0/0") == ["Serial0/0/0", "Serial0/0/1"]
        assert modules.ports_for_slot(spec, "0/1") == ["Serial0/1/0", "Serial0/1/1"]

    def test_nm_slot_leaves_names(self):
        spec = modules.resolve_module("NME-16ES-2P")
        out = modules.ports_for_slot(spec, "1")
        assert out == list(spec.ports_added)

    def test_speed_of_port_name(self):
        assert modules.speed_of_port_name("GigabitEthernet0/0") == "GigabitEthernet"
        assert modules.speed_of_port_name("Serial0/0/0") == "Serial"
        assert modules.speed_of_port_name("Weird0") == "Ethernet"

    def test_category_fit(self):
        assert modules.module_category_fits("router", "router_hwic") is True
        assert modules.module_category_fits("switch", "router_hwic") is False
        assert modules.module_category_fits("laptop", "pt_laptop_nm") is True
        # empty categories never fit
        assert modules.module_category_fits("", "router_nm") is False
        assert modules.module_category_fits("router", "") is False

    def test_expansion_module_for_speed(self):
        m = devices.resolve_model("2911")
        got = modules.expansion_module_for_speed(m, PortSpeed.SERIAL)
        assert got is not None and got.name == "HWIC-2T"

    def test_expansion_none_when_no_slots(self):
        m = devices.resolve_model("Hub-PT")
        assert modules.expansion_module_for_speed(m, PortSpeed.SERIAL) is None

    def test_serial_module_for_router(self):
        got = modules.get_serial_module("2911")
        assert got is not None and got.name == "HWIC-2T"
        assert modules.get_serial_module("not-a-router") is None


class TestCableRules:
    def test_router_router_is_cross(self):
        assert cables.infer_cable("router", "router") == "cross"

    def test_switch_pc_is_straight(self):
        assert cables.infer_cable("switch", "pc") == "straight"

    def test_order_does_not_matter(self):
        assert cables.infer_cable("pc", "switch") == cables.infer_cable("switch", "pc")

    def test_unknown_pair_defaults_to_straight_and_never_raises(self):
        assert cables.infer_cable("zzz", "qqq") == "straight"
        assert cables.infer_cable("", "router") == "straight"

    def test_models_route_through_categories(self):
        assert cables.infer_cable_for_models("2911", "2960-24TT") == "straight"
        assert cables.infer_cable_for_models("2911", "2911") == "cross"

    def test_rule_table_covers_both_orderings(self):
        key = ("router", "switch")
        assert cables.CABLE_RULES.get(key) == "straight"
