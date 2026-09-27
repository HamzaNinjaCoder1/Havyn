"""Back-to-back serial links: DCE clocking, cables, ports and read-back.

Live Packet Tracer truth pinned by this module, all captured on a running
PT, not inferred:

  * "serial" (PT connect type 8106) is the ONLY serial cable PT accepts.
    The names in the UI ("Serial DCE", "Serial DTE") are not addressable:
    `dce`, `dte`, `serial-dce`, `serial-dte` are all rejected by addLink.
  * A back-to-back serial link between two routers must carry a clock rate on
    exactly ONE end. With a clock on one end traffic passes both ways; with a
    clock on the other end only, traffic also passes; with the clock REMOVED
    from a working link the far end reports
    `%LINEPROTO-5-UPDOWN: Line protocol on Interface Serial2/0, changed state
    to down` and the ping is BLOCKED. So a build that leaves both ends
    unclocked is not "fine", it is one edit away from a dead link.
  * PT names the roles itself, and this is the read-back that proves the
    build: the clocked end answers
        Interface Serial2/0
        Hardware is PowerQUICC MPC860
        DCE V.35, clock rate 64000
    and the other end answers
        Interface Serial2/0
        Hardware is PowerQUICC MPC860
        DTE V.35 TX and RX clocks detected
    A clock on both ends still reports DCE on one and DTE on the other, so the
    "exactly one clock" rule is the compiler's to enforce, not PT's.
  * `show controllers serial Serial2/0` is INVALID INPUT in PT; the command
    needs the abbreviated form (`show controllers serial 2/0`).
  * A 2911 with HWIC-2T in slot 0/0 exposes Serial0/0/0 and answers the same
    command, and a Router-PT cabled to that module port passes traffic.
  * Router-PT enumerates Serial2/0 and Serial3/0, so two serial links on one
    router must not collide.

Anything unparseable (invalid input, no prompt, no role line) must surface as
UNKNOWN: per the evidence rules, UNKNOWN is never turned into a pass.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.adapter.packet_tracer import js_commands as js
from src.network_autopilot.compiler import CompileError, compile_spec
from src.network_autopilot.core.actual_state import Observable
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing
from src.network_autopilot.serial import (
    DCE_CLOCK_RATE,
    read_serial_clock_evidence,
    serial_dce_sides,
)
from src.network_autopilot.pt.catalog.modules import ports_for_slot
from src.network_autopilot.pt.catalog.devices import resolve_model

# -- golden PT console output (verbatim, captured live) ------------------------

DCE_ROUTER_PT = """
Router#show controllers serial 2/0

Interface Serial2/0
Hardware is PowerQUICC MPC860
DCE V.35, clock rate 64000
idb at 0x81081AC4, driver data structure at 0x81084AC0
SCC Registers:
General [GSMR]=0x2:0x00000000, Protocol-specific [PSMR]=0x8
Events [SCCE]=0x00000000, Mask [SCCM]=0x00000000, Status [SCCS]=0x00
Transmit on Demand [TODR]=0x0, Data Sync [DSR]=0x7E7E
Interrupt Registers:
Config [CICR]=0x00367F80, Pending [CIPR]=0x0000C000
Mask   [CIMR]=0x00200000, In-srv  [CISR]=0x00000000
Command register [CR]=0x580
Router#
"""

DTE_ROUTER_PT = """
Router#show controllers serial 2/0

Interface Serial2/0
Hardware is PowerQUICC MPC860
DTE V.35 TX and RX clocks detected
idb at 0x81081AC4, driver data structure at 0x81084AC0
SCC Registers:
General [GSMR]=0x2:0x00000000, Protocol-specific [PSMR]=0x8
Router#
"""

DCE_MODULE_PORT = """
Router#show controllers serial 0/0/0

Interface Serial0/0/0
Hardware is PowerQUICC MPC860
DCE V.35, clock rate 128000
idb at 0x81081AC4, driver data structure at 0x81084AC0
Router#
"""

INVALID_INPUT = """
Router#show controllers serial Serial2/0
                                     ^
% Invalid input detected at '^' marker.
Router#
"""

NO_ROLE_LINE = """
Router#show controllers serial 2/0

Interface Serial2/0
Hardware is PowerQUICC MPC860
idb at 0x81081AC4, driver data structure at 0x81084AC0
Router#
"""


# -- parser ------------------------------------------------------------------


def test_parse_dce_end_reports_role_and_clock():
    parsed = js.parse_show_controllers_serial(DCE_ROUTER_PT)
    assert parsed == {
        "interface": "Serial2/0",
        "role": "dce",
        "clock_rate": 64000,
        "signaling": "V.35",
    }


def test_parse_dte_end_reports_no_clock():
    parsed = js.parse_show_controllers_serial(DTE_ROUTER_PT)
    assert parsed is not None
    assert parsed["role"] == "dte"
    assert parsed["clock_rate"] is None, (
        "the DTE end must never report a clock rate of its own"
    )
    assert parsed["interface"] == "Serial2/0"


def test_parse_module_port_dce():
    parsed = js.parse_show_controllers_serial(DCE_MODULE_PORT)
    assert parsed is not None
    assert parsed["interface"] == "Serial0/0/0"
    assert parsed["role"] == "dce"
    assert parsed["clock_rate"] == 128000


@pytest.mark.parametrize("output", [INVALID_INPUT, NO_ROLE_LINE, "", "junk"])
def test_unparseable_controller_output_is_rejected(output: str):
    assert js.parse_show_controllers_serial(output) is None


@pytest.mark.parametrize(
    "interface,expected",
    [
        ("Serial2/0", "2/0"),
        ("Serial3/0", "3/0"),
        ("Serial0/0/0", "0/0/0"),
        ("Serial0/1/1", "0/1/1"),
    ],
)
def test_serial_iface_arg_uses_pts_abbreviated_form(interface: str, expected: str):
    # `show controllers serial Serial2/0` is invalid input in PT.
    assert js.serial_iface_arg(interface) == expected


# -- cable -------------------------------------------------------------------


def test_serial_cable_is_the_only_serial_cable_pt_accepts():
    assert js.cable_code_for("serial") == 8106
    assert js.cable_name_for("serial") == "serial"
    for bogus in ("dce", "dte", "serial-dce", "serial-dte"):
        with pytest.raises(ValueError):
            js.cable_code_for(bogus)


# -- compilation -------------------------------------------------------------


def _spec(devices: list[dict], links: list[dict], subnets: list[dict],
          assignments: list[dict]) -> dict:
    return {
        "name": "serial-lab", "devices": devices, "links": links,
        "subnets": subnets, "ip_assignments": assignments,
        "routing": {"mode": "connected"},
    }


def _compile(raw: dict):
    spec = NetworkSpec.model_validate(raw)
    return spec, compile_spec(spec, plan_addressing(spec))


def _commands(result, device: str) -> list[str]:
    for node in result.dag.nodes.values():
        if (
            node.operation.operation_type == "configure_ios"
            and node.operation.inputs.get("device") == device
        ):
            return list(node.operation.inputs["commands"])
    raise AssertionError(f"no configure_ios op for {device}")


def _link_inputs(result, link_id: str) -> dict:
    for node in result.dag.nodes.values():
        if (
            node.operation.operation_type == "create_link"
            and node.operation.inputs.get("link_id") == link_id
        ):
            return dict(node.operation.inputs)
    raise AssertionError(f"no create_link op for {link_id}")


def test_back_to_back_serial_clocks_exactly_one_end():
    """Two routers, one serial link: one clock, on the link's A end."""
    raw = _spec(
        devices=[
            {"id": "r1", "role": "router", "model_hint": "Router-PT"},
            {"id": "r2", "role": "router", "model_hint": "Router-PT"},
        ],
        links=[{"id": "l1", "a": {"device": "r1"}, "b": {"device": "r2"},
                "media": "serial"}],
        subnets=[{"id": "wan", "cidr": "10.10.10.0/30"}],
        assignments=[
            {"device": "r1", "subnet": "wan", "ip": "10.10.10.1"},
            {"device": "r2", "subnet": "wan", "ip": "10.10.10.2"},
        ],
    )
    _spec_obj, result = _compile(raw)
    link = _link_inputs(result, "l1")
    assert link["media"] == "serial"
    assert link["a_port"] == "Serial2/0", link
    assert link["b_port"] == "Serial2/0", link

    r1, r2 = _commands(result, "r1"), _commands(result, "r2")
    assert [c for c in r1 if c.startswith("clock rate")] == [
        f"clock rate {DCE_CLOCK_RATE}"
    ], r1
    assert not [c for c in r2 if c.startswith("clock rate")], (
        "two clocking ends is not a serial link, it is two labs fighting over "
        "the same wire"
    )


def test_serial_wan_to_a_cloud_is_never_clocked_by_the_router():
    """The service side provides the timing; a clock here is wrong."""
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
    _spec_obj, result = _compile(raw)
    assert not [
        c for c in _commands(result, "r1") if c.startswith("clock rate")
    ]


def test_module_router_serial_link_installs_hwic_and_clocks_the_a_end():
    raw = _spec(
        devices=[
            {"id": "m1", "role": "router", "model_hint": "2911"},
            {"id": "r2", "role": "router", "model_hint": "Router-PT"},
        ],
        links=[{"id": "l1", "a": {"device": "m1"}, "b": {"device": "r2"},
                "media": "serial"}],
        subnets=[{"id": "wan", "cidr": "10.20.20.0/30"}],
        assignments=[
            {"device": "m1", "subnet": "wan", "ip": "10.20.20.1"},
            {"device": "r2", "subnet": "wan", "ip": "10.20.20.2"},
        ],
    )
    _spec_obj, result = _compile(raw)
    link = _link_inputs(result, "l1")
    assert link["a_port"] == "Serial0/0/0", (
        "live PT names the first HWIC-2T port Serial0/0/0, not Serial0/0"
    )
    modules = [
        (req.device, req.module_name, req.slot, tuple(req.ports_added))
        for req in result.port_plan.module_requirements
    ]
    assert modules == [("m1", "HWIC-2T", "0/0", ("Serial0/0/0", "Serial0/0/1"))], modules
    assert f"clock rate {DCE_CLOCK_RATE}" in _commands(result, "m1")
    assert not [c for c in _commands(result, "r2") if c.startswith("clock rate")]


def test_two_serial_links_on_one_router_use_distinct_ports_and_clocks():
    """r1 -- r2 -- r3: the middle router is DTE on one hop, DCE on the other."""
    raw = _spec(
        devices=[
            {"id": "r1", "role": "router", "model_hint": "Router-PT"},
            {"id": "r2", "role": "router", "model_hint": "Router-PT"},
            {"id": "r3", "role": "router", "model_hint": "Router-PT"},
        ],
        links=[
            {"id": "l1", "a": {"device": "r1"}, "b": {"device": "r2"},
             "media": "serial"},
            {"id": "l2", "a": {"device": "r2"}, "b": {"device": "r3"},
             "media": "serial"},
        ],
        subnets=[
            {"id": "s1", "cidr": "10.1.1.0/30"},
            {"id": "s2", "cidr": "10.2.2.0/30"},
        ],
        assignments=[
            {"device": "r1", "subnet": "s1", "ip": "10.1.1.1"},
            {"device": "r2", "subnet": "s1", "ip": "10.1.1.2"},
            {"device": "r2", "subnet": "s2", "ip": "10.2.2.1"},
            {"device": "r3", "subnet": "s2", "ip": "10.2.2.2"},
        ],
    )
    _spec_obj, result = _compile(raw)
    l1, l2 = _link_inputs(result, "l1"), _link_inputs(result, "l2")
    assert l1["b_port"] == "Serial2/0", l1
    assert l2["a_port"] == "Serial3/0", l2
    assert l1["b_port"] != l2["a_port"], "a port cannot carry two links"
    r1, r2, r3 = (
        _commands(result, "r1"), _commands(result, "r2"), _commands(result, "r3")
    )
    assert [c for c in r1 if c.startswith("clock rate")] == [
        f"clock rate {DCE_CLOCK_RATE}"
    ]
    assert [c for c in r2 if c.startswith("clock rate")] == [
        f"clock rate {DCE_CLOCK_RATE}"
    ], r2
    assert not [c for c in r3 if c.startswith("clock rate")]


def test_serial_link_to_a_device_without_serial_is_refused():
    raw = _spec(
        devices=[
            {"id": "r1", "role": "router", "model_hint": "Router-PT"},
            {"id": "pc1", "role": "pc"},
        ],
        links=[{"id": "l1", "a": {"device": "r1"}, "b": {"device": "pc1"},
                "media": "serial"}],
        subnets=[{"id": "wan", "cidr": "10.30.30.0/30"}],
        assignments=[{"device": "r1", "subnet": "wan", "ip": "10.30.30.1"}],
    )
    with pytest.raises(CompileError, match="serial"):
        _compile(raw)


def test_dce_sides_helper_matches_the_compiler_emission():
    raw = _spec(
        devices=[
            {"id": "r1", "role": "router", "model_hint": "Router-PT"},
            {"id": "r2", "role": "router", "model_hint": "Router-PT"},
        ],
        links=[{"id": "l1", "a": {"device": "r1"}, "b": {"device": "r2"},
                "media": "serial"}],
        subnets=[{"id": "wan", "cidr": "10.10.10.0/30"}],
        assignments=[
            {"device": "r1", "subnet": "wan", "ip": "10.10.10.1"},
            {"device": "r2", "subnet": "wan", "ip": "10.10.10.2"},
        ],
    )
    spec_obj, result = _compile(raw)
    sides = serial_dce_sides(spec_obj, result.port_plan)
    assert sides == {("r1", "Serial2/0"): DCE_CLOCK_RATE}, sides


def test_catalog_module_slot_naming_is_the_live_one():
    from src.network_autopilot.pt.catalog.modules import resolve_module

    hwic = resolve_module("HWIC-2T")
    assert hwic is not None
    assert ports_for_slot(hwic, "0/0") == ["Serial0/0/0", "Serial0/0/1"]
    assert ports_for_slot(hwic, "0/1") == ["Serial0/1/0", "Serial0/1/1"]


def test_router_pt_has_two_onboard_serial_ports():
    ports = {p.full_name for p in resolve_model("Router-PT").ports}
    assert {"Serial2/0", "Serial3/0"} <= ports, ports


# -- executor read-back ------------------------------------------------------


class _StubAdapter:
    def __init__(self, answers: dict[tuple[str, str], Observable]):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def read_serial_controllers(self, device: str, interface: str) -> Observable:
        self.calls.append((device, interface))
        return self.answers[(device, interface)]


def _entry(device: str, interface: str, role: str, rate: int | None) -> dict:
    return {
        "device": device, "interface": interface,
        "expected_role": role, "expected_clock_rate": rate,
    }


def test_read_back_passes_when_pt_agrees_with_the_plan():
    adapter = _StubAdapter({
        ("r1", "Serial2/0"): Observable[dict].known(
            {"interface": "Serial2/0", "role": "dce", "clock_rate": 64000,
             "signaling": "V.35"},
            source="console:r1",
        ),
        ("r2", "Serial2/0"): Observable[dict].known(
            {"interface": "Serial2/0", "role": "dte", "clock_rate": None,
             "signaling": "V.35"},
            source="console:r2",
        ),
    })
    evidence = read_serial_clock_evidence(adapter, [
        _entry("r1", "Serial2/0", "dce", DCE_CLOCK_RATE),
        _entry("r2", "Serial2/0", "dte", None),
    ])
    assert [e["status"] for e in evidence] == ["pass", "pass"], evidence
    assert adapter.calls == [("r1", "Serial2/0"), ("r2", "Serial2/0")]


def test_read_back_fails_when_the_clock_did_not_land():
    adapter = _StubAdapter({
        ("r1", "Serial2/0"): Observable[dict].known(
            {"interface": "Serial2/0", "role": "dte", "clock_rate": None,
             "signaling": "V.35"},
            source="console:r1",
        ),
    })
    evidence = read_serial_clock_evidence(
        adapter, [_entry("r1", "Serial2/0", "dce", DCE_CLOCK_RATE)]
    )
    assert evidence[0]["status"] == "fail", evidence
    assert "dce" in evidence[0]["detail"].lower()


def test_read_back_fails_when_both_ends_ended_up_clocked():
    adapter = _StubAdapter({
        ("r2", "Serial2/0"): Observable[dict].known(
            {"interface": "Serial2/0", "role": "dte", "clock_rate": 128000,
             "signaling": "V.35"},
            source="console:r2",
        ),
    })
    evidence = read_serial_clock_evidence(
        adapter, [_entry("r2", "Serial2/0", "dte", None)]
    )
    assert evidence[0]["status"] == "fail", evidence


def test_read_back_is_unknown_when_pt_cannot_answer():
    adapter = _StubAdapter({
        ("m1", "Serial0/0/0"): Observable[dict].unknown("no prompt within 14s"),
    })
    evidence = read_serial_clock_evidence(
        adapter, [_entry("m1", "Serial0/0/0", "dce", DCE_CLOCK_RATE)]
    )
    assert evidence[0]["status"] == "unknown", evidence
    assert "no prompt" in evidence[0]["detail"]


def test_read_back_is_unknown_without_adapter_support():
    class _Bare:
        pass

    evidence = read_serial_clock_evidence(
        _Bare(), [_entry("r1", "Serial2/0", "dce", DCE_CLOCK_RATE)]
    )
    assert evidence[0]["status"] == "unknown", evidence


# -- bridge adapter ----------------------------------------------------------


def test_serial_collect_anchors_on_the_command_echo_not_on_buffer_length():
    """A saturated console never grows past the base, so length is useless."""
    expr = js.js_console_collect_serial_expr("R1", "show controllers serial 2/0")
    assert "lastIndexOf" in expr, (
        "completion must not depend on the console buffer growing: PT drops a "
        "queued command's output on a busy console (measured live against PT)"
    )
    assert "o.slice(i)" in expr
    assert "o.length>" not in expr and "o.length<" not in expr


def test_strict_collect_is_left_alone_for_routing_and_vlan():
    # The routing/VLAN read-backs depend on strict growth: a trimmed buffer
    # there would hand back a partial table, so they keep the old rule.
    expr = js.js_console_collect_expr("R1", 5000)
    assert "o.length>5000" in expr
    assert "o.length<5000" not in expr


def test_bridge_read_serial_controllers_parses_live_console(monkeypatch):
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import (
        BridgeAdapter,
    )

    adapter = BridgeAdapter.__new__(BridgeAdapter)
    seen: list[tuple[str, float]] = []

    def _fake_show(device: str, command: str, *, timeout: float = 12.0,
                   collect_expr=None):
        seen.append((command, timeout))
        return Observable[str].known(DCE_ROUTER_PT, source=f"console:{device}")

    monkeypatch.setattr(adapter, "_console_show", _fake_show)
    out = adapter.read_serial_controllers("R1", "Serial2/0")
    assert out.is_known
    assert out.value["role"] == "dce"
    assert out.value["clock_rate"] == 64000
    assert "DCE V.35" in (out.detail or ""), "raw PT output stays in the evidence"
    assert seen[0][0] == "show controllers serial 2/0", seen


def test_bridge_read_serial_controllers_is_unknown_on_invalid_input(monkeypatch):
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import (
        BridgeAdapter,
    )

    adapter = BridgeAdapter.__new__(BridgeAdapter)
    monkeypatch.setattr(
        adapter, "_console_show",
        lambda device, command, timeout=12.0, collect_expr=None:
            Observable[str].known(INVALID_INPUT),
    )
    out = adapter.read_serial_controllers("R1", "Serial2/0")
    assert not out.is_known
    assert out.status.value == "unknown"
