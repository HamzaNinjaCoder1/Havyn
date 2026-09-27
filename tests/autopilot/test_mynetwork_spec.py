"""my-network.spec.json — the project's real 45-device spec (#14/#43).

Offline half of the live regression: the spec must validate with honest
roles and compile to a DAG where every device that can configure actually
receives a config op. The live half (build + ping, zero manual CLI) is #43.
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
    config path — three ASAs pushed nothing at all."""
    spec = _spec()
    others = [d.id for d in spec.devices if d.role.value == "other"]
    assert others == [], others
    by_id = {d.id: d for d in spec.devices}
    assert by_id["fwhq"].role.value == "firewall"
    assert by_id["fwdc"].role.value == "firewall"
    assert by_id["fwbr"].role.value == "firewall"
    assert by_id["dsl1"].role.value == "modem"
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
    assert "dsl1" not in configured  # a modem has no CLI by design (#13/#11)


def test_the_real_spec_solves_the_cloud_dsl_path_on_the_right_ports():
    """#11: the DSL path lands on the cloud's Modem port with a phone cable
    and the Ethernet side on a normal port. PT used to need a manual
    Cloud → Config → DSL step for that mapping; live it now derives from the
    cabling (getPortConnectionCount() auto-created the bridge, and traffic
    crossed with zero manual step) — this pins the solver side offline."""
    spec = _spec()
    plan = plan_addressing(spec)
    result = compile_spec(spec, plan)
    links = {
        node.operation.inputs["link_id"]: node.operation.inputs
        for node in result.dag.nodes.values()
        if node.operation.operation_type == "create_link"
    }
    dsl = links["l-cloud-dsl"]
    cloud_side = dsl["a_device"] if dsl["a_device"] == "cloud1" else dsl["b_device"]
    cloud_port = dsl["a_port"] if cloud_side == "cloud1" else dsl["b_port"]
    modem_side = dsl["b_port"] if cloud_side == "cloud1" else dsl["a_port"]
    assert cloud_port.startswith("Modem"), dsl
    assert modem_side.startswith("Port"), dsl
    assert dsl["media"] == "phone", dsl
    edge = links["l-edge-cloud"]
    assert edge["media"] == "straight", edge
    assert "cloud1" in (edge["a_device"], edge["b_device"])
    assert not str(
        edge["a_port"] if edge["a_device"] == "cloud1" else edge["b_port"]
    ).startswith("Modem"), edge
