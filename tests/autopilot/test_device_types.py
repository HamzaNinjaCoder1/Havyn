"""Device-type IDs and home-gateway compilation.

Two things the audit found broken and live Packet Tracer disproved:

1. `device_type_code_for` keys off the ROLE. An IoT thing is spelled
   role=router/other in the spec, so it would be created as a router; and
   `l3_switch` had no entry at all and silently fell back to
   wired_end_device (21). PT's own device-type table is the source of truth
   (upstream Device-Types.md), and the catalog category - not the role - is
   what identifies a model.

2. The home gateway (HomeRouter-PT-AC) is the automatable backbone of any
   smart-home / smart-city / IoT lab: its LAN ports really cable and it takes
   IOS CLI (`Vlan1` reads back 192.168.0.1 after
   `ip address 192.168.0.1 255.255.255.0` plus a DHCP pool). Its LAN address
   lives on the Vlan1 SVI regardless of which LAN port is cabled.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.adapter.packet_tracer import js_commands as js
from src.network_autopilot.pt.catalog.devices import (
    all_models,
    category_of_model,
)


@pytest.mark.parametrize(
    "category,expected",
    [
        ("router", 0), ("switch", 1), ("cloud", 2), ("bridge", 3), ("hub", 4),
        ("repeater", 5), ("accesspoint", 7), ("pc", 8), ("server", 9),
        ("printer", 10), ("wireless_router", 11), ("ip_phone", 12),
        ("modem", 13), ("laptop", 17), ("wireless_end_device", 20),
        ("wired_end_device", 21), ("tv", 22), ("voip", 23),
        ("analog_phone", 24), ("firewall", 26), ("iot", 27),
        ("home_gateway", 28), ("cell_tower", 29), ("central_office", 31),
        ("sniffer", 33), ("mcu", 34), ("sbc", 35), ("wlc", 7),
    ],
)
def test_catalog_categories_map_to_pt_device_type_ids(category: str, expected: int):
    assert js.device_type_code_for(category) == expected


def test_l3_switch_uses_the_multilayer_switch_type():
    """The spec spells the role `l3_switch`; PT's type id for it is 16."""
    assert js.device_type_code_for("l3_switch") == 16


@pytest.mark.parametrize(
    "model,expected",
    [
        # MCU-PT is deliberately absent from the catalog (S1.12: PT 9.0.1
        # refuses to place it), so it is not a placeable model to type-map.
        ("HomeRouter-PT-AC", 28), ("Linksys-WRT300N", 11),
        ("Smart LED", 27), ("Solar Panel", 27), ("Thing", 27),
        ("SBC-PT", 35), ("5506-X", 26),
        ("Switch-PT", 1), ("Router-PT", 0), ("PC-PT", 8),
        ("WLC-PT", 7), ("Cell-Tower", 29),
        ("Meraki-MX65W", 0), ("Meraki-Server", 9), ("DLC100", 9),
        ("Copper Patch Panel", 1), ("7960", 12), ("CoAxialSplitter-PT", 6),
    ],
)
def test_model_category_selects_the_device_type(model: str, expected: int):
    """The model identifies the device, so its category must decide the type."""
    assert js.device_type_code_for_model(model) == expected


def test_unknown_model_falls_back_to_the_role_code():
    assert js.device_type_code_for_model("Not-A-Real-Model", role="switch") == 1


def test_unknown_model_without_a_known_role_fails_closed():
    with pytest.raises(ValueError):
        js.device_type_code_for_model("Not-A-Real-Model", role="mystery-role")


def test_every_catalogued_category_has_a_type_id():
    categories = {model.category for model in all_models().values()}
    for category in sorted(categories):
        assert js.device_type_code_for(category) is not None, category


def test_unknown_role_has_no_type_id_instead_of_a_silent_fallback():
    """A typo must not become a wired end device behind the caller's back."""
    assert js.device_type_code_for("mystery-role") is None


def test_home_gateway_category_is_reachable_from_the_catalog():
    assert category_of_model("HomeRouter-PT-AC") == "home_gateway"


class TestLegacyDeviceTypeTable:
    """The 61-tool path builds its JS from PT_DEVICE_TYPE by category.

    A category missing from that table silently became a wired end device
    (21), and `central_office` pointed at 30 - which is PT's Cisco access
    point, not the central office server (31). Both were caught by reading
    real PT answers.
    """

    def test_every_catalogued_category_maps_to_a_type_id(self):
        from src.network_autopilot.adapter.packet_tracer.js_commands import DEVICE_TYPE_CODES as PT_DEVICE_TYPE

        for category in sorted({m.category for m in all_models().values()}):
            assert category in PT_DEVICE_TYPE, f"{category} falls back to 21"

    def test_central_office_is_31_not_the_access_point_id(self):
        from src.network_autopilot.adapter.packet_tracer.js_commands import DEVICE_TYPE_CODES as PT_DEVICE_TYPE

        assert PT_DEVICE_TYPE["central_office"] == 31

    def test_meraki_server_overrides_its_category(self):
        from src.network_autopilot.adapter.packet_tracer.js_commands import MODEL_TYPE_OVERRIDES as PT_DEVICE_TYPE_MODEL

        assert PT_DEVICE_TYPE_MODEL["Meraki-Server"] == 9
        assert PT_DEVICE_TYPE_MODEL["Meraki-MX65W"] == 0


def test_pt_has_no_iot_server_type_so_cabling_is_the_full_capability():
    """#13: live probes found no IoT-registration device among PT's 152
    enumerated types and no register/iot/server method on any prototype —
    cabling the things IS the full capability. Pin the three places that
    could otherwise start pretending: the catalog, the JS surface, and the
    documented limit (ISSUES section H)."""
    from pathlib import Path

    from src.network_autopilot.adapter.packet_tracer import js_commands as js

    # 1. the catalog must not invent an IoT registration server
    for name in all_models():
        lowered = name.lower()
        assert not (lowered.startswith("iot") and "server" in lowered), name
        assert "registration" not in lowered, name

    # 2. the JS builder surface must not offer a registration call
    for public in (n for n in dir(js) if not n.startswith("_")):
        assert "register" not in public.lower(), public

    # 3. the limit stays documented, with the refuse-never-half-configure rule
    issues = Path(__file__).resolve().parents[2] / "ISSUES.md"
    text = issues.read_text(encoding="utf-8")
    assert "IoT server" in text
    assert "never half-configured" in text
