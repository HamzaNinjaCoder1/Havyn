"""Cloud + modem support: ports, cables, and what can actually be configured.

Live Packet Tracer truth this module pins, measured on a running PT:

  * Cloud-PT enumerates Serial0-3, Modem4-5, Ethernet6, Coaxial7;
    Cloud-PT-Empty enumerates nothing; both modems enumerate "Port 0" and
    "Port 1" - the catalog already matched, and these tests keep it there.
  * Cables that REALLY form (nothing else does):
        Cloud Ethernet6  <-> router Ethernet   straight
        Cloud Serial0    <-> router Serial     serial
        Cloud Coaxial7   <-> Cell-Tower        coaxial
        DSL modem Port 0 <-> Cloud Modem4     phone
        Cable modem Port 0 <-> Cloud Coaxial7  coaxial
        DSL modem Port 1 <-> router Ethernet   straight
    A category-only cable table cannot express those: a modem's line port is
    a phone line for DSL and coax for cable, both under the same "modem"
    category - hence a per-model-pair rule.
  * None of them takes IOS CLI, and their ports cannot hold an address, so
    they are link endpoints, never ping targets. The address belongs on the
    router; pretending otherwise would produce a topology that reports an
    address nobody can read.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.compiler import CompileError, compile_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing
from src.network_autopilot.pt.catalog.cables import (
    infer_cable,
    infer_cable_for_models,
)
from src.network_autopilot.pt.catalog.devices import resolve_model

WAN_MODELS = ["Cloud-PT", "Cloud-PT-Empty", "DSL-Modem-PT", "Cable-Modem-PT"]


@pytest.mark.parametrize("pt_type", WAN_MODELS)
def test_wan_models_are_catalogued(pt_type: str):
    assert resolve_model(pt_type) is not None, f"{pt_type} missing from the catalog"


def test_cloud_pt_exposes_every_live_port():
    ports = {p.full_name for p in resolve_model("Cloud-PT").ports}
    assert ports == {
        "Serial0", "Serial1", "Serial2", "Serial3",
        "Modem4", "Modem5", "Ethernet6", "Coaxial7",
    }


def test_empty_cloud_has_no_ports():
    assert resolve_model("Cloud-PT-Empty").ports == ()


@pytest.mark.parametrize("pt_type", ["DSL-Modem-PT", "Cable-Modem-PT"])
def test_modems_expose_line_and_lan_ports(pt_type: str):
    ports = {p.full_name for p in resolve_model(pt_type).ports}
    assert ports == {"Port 0", "Port 1"}


@pytest.mark.parametrize(
    "model_a,model_b,media",
    [
        ("Cloud-PT", "Router-PT", "straight"),
        ("Cloud-PT", "2911", "straight"),
        ("Cloud-PT", "DSL-Modem-PT", "phone"),
        ("Cloud-PT", "Cable-Modem-PT", "coaxial"),
        ("Cloud-PT", "Cell-Tower", "coaxial"),
        ("DSL-Modem-PT", "Router-PT", "straight"),
        ("Cell-Tower", "Cloud-PT", "coaxial"),
    ],
)
def test_measured_cables_for_real_model_pairs(model_a: str, model_b: str, media: str):
    assert infer_cable_for_models(model_a, model_b) == media
    # and the reverse direction must be the same cable
    assert infer_cable_for_models(model_b, model_a) == media


def test_category_rules_still_apply_without_a_model():
    assert infer_cable("cloud", "router") == "straight"
    assert infer_cable("modem", "router") == "straight"


def test_cable_modem_device_type_is_its_own():
    from src.network_autopilot.adapter.packet_tracer import js_commands as js
    from src.network_autopilot.adapter.packet_tracer.js_commands import MODEL_TYPE_OVERRIDES as PT_DEVICE_TYPE_MODEL

    assert js.device_type_code_for_model("Cable-Modem-PT") == 14
    assert js.device_type_code_for_model("DSL-Modem-PT") == 13
    assert PT_DEVICE_TYPE_MODEL["Cable-Modem-PT"] == 14


# -- compilation ------------------------------------------------------------


def _spec(devices: list[dict], links: list[dict], subnets: list[dict],
          assignments: list[dict]) -> dict:
    return {
        "name": "wan-lab", "devices": devices, "links": links,
        "subnets": subnets, "ip_assignments": assignments,
        "routing": {"mode": "connected"},
    }


def _compile(raw: dict):
    return compile_spec(NetworkSpec.model_validate(raw), plan_addressing(
        NetworkSpec.model_validate(raw)
    ))


def test_isp_chain_compiles_with_real_ports_and_cables():
    """Cloud --(phone)-- DSL modem --(copper)-- router -- LAN -- PC."""
    raw = _spec(
        devices=[
            {"id": "isp", "role": "cloud", "model_hint": "Cloud-PT"},
            {"id": "modem", "role": "other", "model_hint": "DSL-Modem-PT"},
            {"id": "r1", "role": "router", "model_hint": "2911"},
            {"id": "pc1", "role": "pc"},
        ],
        links=[
            {"id": "l-line", "a": {"device": "isp"}, "b": {"device": "modem"}},
            {"id": "l-modem", "a": {"device": "modem"}, "b": {"device": "r1"}},
            {"id": "l-lan", "a": {"device": "r1"}, "b": {"device": "pc1"}},
        ],
        subnets=[
            {"id": "wan", "cidr": "203.0.113.0/24"},
            {"id": "lan", "cidr": "192.168.1.0/24"},
        ],
        assignments=[
            {"device": "r1", "subnet": "wan", "ip": "203.0.113.2"},
            {"device": "r1", "subnet": "lan", "ip": "192.168.1.1"},
            {"device": "pc1", "subnet": "lan"},
        ],
    )
    result = _compile(raw)
    links = {
        n.operation.inputs["link_id"]: n.operation.inputs
        for n in result.dag.nodes.values()
        if n.operation.operation_type == "create_link"
    }
    assert set(links) == {"l-line", "l-modem", "l-lan"}
    assert links["l-line"]["a_port"] == "Modem4", links["l-line"]
    assert links["l-line"]["b_port"] == "Port 0", links["l-line"]
    assert links["l-line"]["media"] == "phone", links["l-line"]
    assert links["l-modem"]["media"] == "straight", links["l-modem"]
    assert links["l-modem"]["a_port"] == "Port 1", links["l-modem"]


def test_cloud_and_modem_get_no_cli_configuration():
    raw = _spec(
        devices=[
            {"id": "isp", "role": "cloud", "model_hint": "Cloud-PT"},
            {"id": "r1", "role": "router", "model_hint": "2911"},
        ],
        links=[{"id": "l1", "a": {"device": "isp"}, "b": {"device": "r1"}}],
        subnets=[{"id": "wan", "cidr": "203.0.113.0/24"}],
        assignments=[{"device": "r1", "subnet": "wan", "ip": "203.0.113.2"}],
    )
    result = _compile(raw)
    configured = {
        n.operation.inputs["device"]
        for n in result.dag.nodes.values()
        if n.operation.operation_type == "configure_ios"
    }
    assert configured == {"r1"}, (
        "Cloud-PT and the modems take no IOS CLI in PT, so emitting a config "
        "op for them would push commands PT refuses"
    )


def test_cloud_address_assignment_is_refused_with_a_reason():
    """A cloud cannot hold an address; saying so beats a silent unknown read."""
    raw = _spec(
        devices=[
            {"id": "isp", "role": "cloud", "model_hint": "Cloud-PT"},
            {"id": "r1", "role": "router", "model_hint": "2911"},
        ],
        links=[{"id": "l1", "a": {"device": "isp"}, "b": {"device": "r1"}}],
        subnets=[{"id": "wan", "cidr": "203.0.113.0/24"}],
        assignments=[
            {"device": "isp", "subnet": "wan", "ip": "203.0.113.1"},
            {"device": "r1", "subnet": "wan", "ip": "203.0.113.2"},
        ],
    )
    with pytest.raises(CompileError, match="Cloud-PT"):
        _compile(raw)


def test_serial_wan_link_uses_the_serialization_cable():
    raw = _spec(
        devices=[
            {"id": "isp", "role": "cloud", "model_hint": "Cloud-PT"},
            {"id": "r1", "role": "router", "model_hint": "Router-PT"},
        ],
        links=[{"id": "l1", "a": {"device": "isp"}, "b": {"device": "r1"},
                "media": "serial"}],
        subnets=[{"id": "wan", "cidr": "203.0.113.0/24"}],
        assignments=[{"device": "r1", "subnet": "wan", "ip": "203.0.113.2"}],
    )
    result = _compile(raw)
    link = next(
        n.operation.inputs for n in result.dag.nodes.values()
        if n.operation.operation_type == "create_link"
    )
    assert link["a_port"] == "Serial0", link
    assert link["b_port"] == "Serial2/0", link
    assert link["media"] == "serial", link
