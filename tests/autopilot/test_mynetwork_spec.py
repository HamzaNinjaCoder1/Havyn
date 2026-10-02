"""my-network.spec.json — the project's real 43-device reference spec (#14/#43).

Offline half of the live regression: the spec must validate with honest
roles and compile to a DAG where every device that can configure actually
receives a config op. The live half (build + ping, zero manual CLI) is #43.

S1.19 removed the Cloud-PT + DSL-modem leg from the canonical spec: live A/B
proved the Cloud-PT DSL path carries no traffic through any API, and the
perimeter now sits on a direct fwhq <-> risp WAN link. The old cloud/dsl
assertions are replaced by the direct-link ones below.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing

_SPEC_PATH = Path(__file__).resolve().parents[2] / "my-network.spec.json"


def _spec() -> NetworkSpec:
    return NetworkSpec.model_validate(
        json.loads(_SPEC_PATH.read_text(encoding="utf-8"))
    )


def test_the_real_spec_spells_every_device_with_an_honest_role():
    """#14: ten devices used to be `role: other`, which falls past every
    config path — three ASAs pushed nothing at all. S1.19 dropped the
    cloud/dsl leg, so the spec carries neither device."""
    spec = _spec()
    others = [d.id for d in spec.devices if d.role.value == "other"]
    assert others == [], others
    by_id = {d.id: d for d in spec.devices}
    assert by_id["fwhq"].role.value == "firewall"
    assert by_id["fwdc"].role.value == "firewall"
    assert by_id["fwbr"].role.value == "firewall"
    assert "cloud1" not in by_id and "dsl1" not in by_id
    assert by_id["wr1"].role.value == "wireless_router"
    assert all(by_id[f"iot{i}"].role.value == "iot" for i in range(1, 6))


def test_the_real_spec_compiles_and_the_asas_get_their_config_ops():
    spec = _spec()
    plan = plan_addressing(spec)
    result = compile_spec(spec, plan)
    configured = {
        node.operation.inputs.get("device")
        for node in result.dag.nodes.values()
        if node.operation.operation_type
        in ("configure_ios", "configure_gateway_lan")
    }
    # three ASAs must receive their block (#14); wr1 carries no
    # ip_assignments in this spec, so it honestly takes no LAN op.
    for device in ("fwhq", "fwdc", "fwbr"):
        assert device in configured, configured
    assert "wr1" not in configured
    # S1.19: the cloud/dsl leg is gone, so nothing there can take a config op.
    assert "cloud1" not in configured and "dsl1" not in configured


def test_the_real_spec_solves_the_direct_perimeter_on_the_right_ports():
    """S1.19: the Cloud-PT + DSL-modem leg is gone; the perimeter is a direct
    fwhq <-> risp WAN link and the branch hangs off a serial DCE wire. This
    pins the solver side offline (the live half is the flow criteria A-010 /
    A-011 / A-024)."""
    spec = _spec()
    plan = plan_addressing(spec)
    result = compile_spec(spec, plan)
    links = {
        node.operation.inputs["link_id"]: node.operation.inputs
        for node in result.dag.nodes.values()
        if node.operation.operation_type == "create_link"
    }
    # No cloud/dsl leg survives the rewire.
    assert "l-cloud-dsl" not in links
    assert "l-edge-cloud" not in links

    wan = links["l-fwhq-risp"]
    assert {wan["a_device"], wan["b_device"]} == {"fwhq", "risp"}, wan
    fw_port = wan["a_port"] if wan["a_device"] == "fwhq" else wan["b_port"]
    assert fw_port == "GigabitEthernet1/1", wan

    serial = links["l-serial"]
    assert serial["media"] == "serial", serial
    assert {serial["a_device"], serial["b_device"]} == {"redge", "rbr"}, serial
