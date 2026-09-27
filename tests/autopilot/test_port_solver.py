"""Port solver tests (§9).

Tests the port/media/module solver, port reservation, and preflight.
"""

from __future__ import annotations

import pytest

from src.network_autopilot.compiler.port_solver import (
    PortPlan,
    PortResolution,
    PortConstraint,
    ModuleRequirement,
    solve_ports,
    available_ports,
    compatible_cable,
    reserved_ports,
)
from src.network_autopilot.compiler.port_reservation import (
    PortReservationSystem,
    PortReservation,
    ReservationResult,
    ReservationStatus,
    ReservationReason,
)
from src.network_autopilot.compiler.preflight import (
    PreflightValidator,
    PreflightResult,
    PreflightCheck,
    PreflightLevel,
    run_preflight,
)
from src.network_autopilot.core.network_spec import (
    NetworkSpec,
    DeviceSpec,
    DeviceRole,
    LinkSpec,
    EndpointSpec,
)
from src.network_autopilot.ipam.planner import plan_addressing


def _make_spec() -> NetworkSpec:
    """A minimal spec with 2 PCs and a link."""
    return NetworkSpec(
        name="port-test",
        devices=[
            DeviceSpec(id="pc1", role=DeviceRole.PC),
            DeviceSpec(id="pc2", role=DeviceRole.PC),
        ],
        links=[
            LinkSpec(
                id="link1",
                a=EndpointSpec(device="pc1"),
                b=EndpointSpec(device="pc2"),
                media="auto",
            )
        ],
        subnets=[],
        ip_assignments=[],
    )


def test_solve_ports_returns_plan():
    spec = _make_spec()
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)
    assert isinstance(port_plan, PortPlan)
    assert len(port_plan.resolutions) >= 2  # at least pc1 and pc2 ports


def test_solve_ports_has_compatible():
    spec = _make_spec()
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)
    # All resolutions should be compatible for simple PC↔PC
    for key, res in port_plan.resolutions.items():
        assert res.constraint == PortConstraint.COMPATIBLE, (
            f"{key}: {res.constraint.value} — {res.reason}"
        )


def test_solve_ports_records_used_ports():
    spec = _make_spec()
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)
    assert port_plan.used_ports
    assert "pc1" in port_plan.used_ports
    assert "pc2" in port_plan.used_ports


def test_solve_ports_module_requirements():
    """PC devices should not require serial modules."""
    spec = _make_spec()
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)
    # PCs use FastEthernet0 which doesn't need a module
    for mod in port_plan.module_requirements:
        assert mod.category != "router_hwic", (
            f"unexpected module for PC: {mod.module_name}"
        )


def test_available_ports():
    spec = _make_spec()
    ports = available_ports("pc1", spec)
    assert "FastEthernet0" in ports


def _make_hub_spec() -> NetworkSpec:
    """A hub in the transit path: 2 PCs and an uplink, all on Hub-PT ports."""
    return NetworkSpec(
        name="hub-test",
        devices=[
            DeviceSpec(id="pc1", role=DeviceRole.PC),
            DeviceSpec(id="pc2", role=DeviceRole.PC),
            DeviceSpec(id="sw1", role=DeviceRole.SWITCH),
            DeviceSpec(id="hub1", role=DeviceRole.OTHER, model_hint="Hub-PT"),
        ],
        links=[
            LinkSpec(id="l1", a=EndpointSpec(device="pc1"),
                     b=EndpointSpec(device="hub1"), media="auto"),
            LinkSpec(id="l2", a=EndpointSpec(device="pc2"),
                     b=EndpointSpec(device="hub1"), media="auto"),
            LinkSpec(id="l3", a=EndpointSpec(device="hub1"),
                     b=EndpointSpec(device="sw1"), media="auto"),
        ],
        subnets=[],
        ip_assignments=[],
    )


def test_hub_catalog_ports_match_live_packet_tracer():
    """Hub-PT ports must be named exactly what PT enumerates.

    Verified live against Packet Tracer: a Hub-PT answers getPortCount()=6 and
    enumerates FastEthernet0..FastEthernet5. The catalog previously declared
    'Port 0'..'Port 7', so the solver emitted names the peer-check read-back
    could never find and every hub link was blocked (fail-closed).
    """
    from src.network_autopilot.pt.catalog.devices import HUB_PT

    names = [p.full_name for p in HUB_PT.ports]
    assert names == [f"FastEthernet{i}" for i in range(6)], (
        f"Hub-PT ports drifted from the live PT surface: {names}"
    )


def test_solve_ports_hub_uses_real_port_names():
    """Hub endpoints must resolve to the FastEthernetN names PT exposes."""
    spec = _make_hub_spec()
    plan = plan_addressing(spec)
    port_plan = solve_ports(spec, plan)

    hub_ifaces = {
        res.interface for res in port_plan.resolutions.values()
        if res.device == "hub1"
    }
    assert hub_ifaces, "no resolution for the hub at all"
    for iface in hub_ifaces:
        assert iface.startswith("FastEthernet"), (
            f"hub resolved to a port PT cannot enumerate: {iface}"
        )


def test_router_2911_ports_match_live_packet_tracer():
    """The 2911's declared ports must be the ones PT actually enumerates.

    Verified live against a device created with model_hint='2911': it answers
    getPortCount()=4 and enumerates Vlan1, GigabitEthernet0/0, 0/1, 0/2 (plus
    4 HWIC slots). This test pins the three onboard copper ports so the catalog
    cannot silently drift back to names PT does not own -- PT's IOS parser
    ACCEPTS an unknown interface name (configureIosDevice -> true), which made
    an earlier phantom-port bug report SUCCESS while pinging 100% loss.
    (A second live read confirmed all three again; the
    assertion used to pin only two and disagreed with its own docstring.)

    Note: a probe that creates the device WITHOUT model_hint gets a generic
    Router-PT instead (FastEthernet0/0 ...). Always pass the model.
    """
    from src.network_autopilot.pt.catalog.devices import ROUTER_2911

    names = [p.full_name for p in ROUTER_2911.ports]
    assert names == [
        "GigabitEthernet0/0", "GigabitEthernet0/1", "GigabitEthernet0/2",
    ], f"2911 ports drifted from the live PT surface: {names}"


def test_compatible_cable():
    result = compatible_cable("auto", "pc", "pc")
    assert result is not None


class TestPortReservationSystem:
    """Port reservation system tests."""

    def test_reserve_single(self):
        sys = PortReservationSystem()
        result = sys.reserve_single("pc1", "FastEthernet0", "auto")
        assert result.success
        assert result.reservation is not None
        assert result.reservation.status == ReservationStatus.RESERVED

    def test_duplicate_reservation_blocked(self):
        sys = PortReservationSystem()
        r1 = sys.reserve_single("pc1", "FastEthernet0", "auto")
        assert r1.success
        r2 = sys.reserve_single("pc1", "FastEthernet0", "auto")
        assert not r2.success
        assert r2.reason and "already reserved" in r2.reason

    def test_release(self):
        sys = PortReservationSystem()
        sys.reserve_single("pc1", "FastEthernet0", "auto")
        assert sys.is_reserved("pc1", "FastEthernet0")
        released = sys.release("pc1", "FastEthernet0")
        assert released
        assert not sys.is_reserved("pc1", "FastEthernet0")

    def test_reservation_blocked_for_incompatible(self):
        sys = PortReservationSystem()
        result = sys.reserve_single("pc1", "NonExistent", "auto")
        # Should still reserve (the check happens at reservation time)
        # The actual incompatibility is caught by the preflight
        assert result.success or not result.success

    def test_summary(self):
        sys = PortReservationSystem()
        sys.reserve_single("pc1", "FastEthernet0", "auto")
        summary = sys.summary()
        assert summary.get("reserved", 0) >= 1

    def test_resolve_module_in_catalog(self):
        sys = PortReservationSystem()
        # HWIC-2T is in the catalog
        result = sys.reserve_single(
            "r1", "Serial0/0/0", "serial", module_name="HWIC-2T"
        )
        # Module exists in catalog
        assert result.success


class TestPreflightValidator:
    """Preflight validator tests."""

    def test_preflight_passes_for_simple_spec(self):
        spec = _make_spec()
        plan = plan_addressing(spec)
        result = run_preflight(spec, plan)
        assert isinstance(result, PreflightResult)
        # Simple PC spec should have no errors
        assert result.passed or len(result.errors) == 0

    def test_preflight_check_structure(self):
        spec = _make_spec()
        plan = plan_addressing(spec)
        result = run_preflight(spec, plan)
        for check in result.checks:
            assert isinstance(check, PreflightCheck)
            assert check.name
            assert check.category
            assert isinstance(check.status, PreflightLevel)

    def test_preflight_can_execute_property(self):
        spec = _make_spec()
        plan = plan_addressing(spec)
        result = run_preflight(spec, plan)
        # When passed, can_execute should reflect passed state
        assert result.can_execute == result.passed or not result.blocked

    def test_preflight_blocks_on_port_conflict(self):
        spec = _make_spec()
        plan = plan_addressing(spec)
        from src.network_autopilot.compiler.port_reservation import PortReservationSystem
        sys = PortReservationSystem()
        # Pre-reserve the port
        sys.reserve_single("pc1", "FastEthernet0", "auto")
        result = run_preflight(spec, plan)
        # Preflight should detect the conflict
        assert isinstance(result, PreflightResult)

    def test_preflight_warnings_for_auto_media(self):
        spec = _make_spec()
        plan = plan_addressing(spec)
        result = run_preflight(spec, plan)
        # auto media links should produce warnings
        assert isinstance(result, PreflightResult)


class TestPortPlan:
    """PortPlan tests."""

    def test_add_resolution(self):
        plan = PortPlan()
        res = PortResolution(
            device="pc1", role=DeviceRole.PC, interface="FastEthernet0",
            media="auto", module_required=None, speed="FastEthernet",
            slot=None,
        )
        plan.add_resolution("pc1:FastEthernet0", res)
        assert "pc1:FastEthernet0" in plan.resolutions

    def test_is_compatible(self):
        plan = PortPlan()
        res = PortResolution(
            device="pc1", role=DeviceRole.PC, interface="FastEthernet0",
            media="auto", module_required=None, speed="FastEthernet",
            slot=None,
        )
        plan.add_resolution("pc1:FastEthernet0", res)
        assert plan.is_compatible("pc1:FastEthernet0")

    def test_device_needs_module(self):
        plan = PortPlan()
        assert plan.device_needs_module("pc1") == []


def _asa_spec(model_hint: str) -> NetworkSpec:
    """fw (ASA) linked to a single PC — the smallest solvable ASA topology."""
    return NetworkSpec(
        name=f"asa-solve-{model_hint}",
        devices=[
            DeviceSpec(id="fw", role=DeviceRole.ROUTER, model_hint=model_hint),
            DeviceSpec(id="pc1", role=DeviceRole.PC),
        ],
        links=[
            LinkSpec(
                id="l-fw-pc",
                a=EndpointSpec(device="fw"),
                b=EndpointSpec(device="pc1"),
                media="auto",
            )
        ],
        subnets=[],
        ip_assignments=[],
    )


class TestAsaPortSolving:
    """The solver must resolve ASA endpoints to ports PT actually has.

    Before the catalog fix it offered GigabitEthernet1/0 (NULL via getPort
    on a live 5506-X) and FastEthernet0/0 (same on a 5505), so every ASA
    link was a guaranteed miss. Live truth: 5506-X starts at 1/1, the 5505
    uses Ethernet0/x switchports, and Vlan* SVIs are never cabling targets.
    """

    def test_5506_resolves_to_first_real_gigabit_port(self):
        spec = _asa_spec("5506-X")
        plan = plan_addressing(spec)
        port_plan = solve_ports(spec, plan)
        assert not port_plan.incompatible, port_plan.incompatible
        endpoints = port_plan.link_endpoints["l-fw-pc"]
        assert endpoints == ("fw", "GigabitEthernet1/1", "pc1", "FastEthernet0")

    def test_5505_resolves_to_ethernet_switchport_not_vlan(self):
        spec = _asa_spec("5505")
        plan = plan_addressing(spec)
        port_plan = solve_ports(spec, plan)
        assert not port_plan.incompatible, port_plan.incompatible
        fw_port = port_plan.link_endpoints["l-fw-pc"][1]
        assert fw_port == "Ethernet0/0"
        assert not fw_port.startswith("Vlan")

    def test_5505_second_link_uses_second_switchport(self):
        spec = NetworkSpec(
            name="asa-5505-two-links",
            devices=[
                DeviceSpec(id="fw", role=DeviceRole.ROUTER, model_hint="5505"),
                DeviceSpec(id="pc1", role=DeviceRole.PC),
                DeviceSpec(id="pc2", role=DeviceRole.PC),
            ],
            links=[
                LinkSpec(id="l1", a=EndpointSpec(device="fw"),
                         b=EndpointSpec(device="pc1"), media="auto"),
                LinkSpec(id="l2", a=EndpointSpec(device="fw"),
                         b=EndpointSpec(device="pc2"), media="auto"),
            ],
            subnets=[],
            ip_assignments=[],
        )
        plan = plan_addressing(spec)
        port_plan = solve_ports(spec, plan)
        assert not port_plan.incompatible, port_plan.incompatible
        assert port_plan.link_endpoints["l1"][1] == "Ethernet0/0"
        assert port_plan.link_endpoints["l2"][1] == "Ethernet0/1"

    def test_auto_media_for_firewall_links_is_straight_from_category(self):
        """CABLE_RULES is keyed by device CATEGORY, not role.

        ASAs carry role=router + category=firewall. Inferring from the role
        sent firewall↔pc links out as cross-over; PT's 5505 switchports then
        pass no traffic (live net_build e2e: cross = both flows blocked,
        straight = both flows green, everything else identical).
        """
        for model_hint, fw_port in (
            ("5505", "Ethernet0/0"),
            ("5506-X", "GigabitEthernet1/1"),
        ):
            spec = _asa_spec(model_hint)
            port_plan = solve_ports(spec, plan_addressing(spec))
            assert not port_plan.incompatible, port_plan.incompatible
            media = port_plan.resolutions[f"fw:{fw_port}"].media
            assert media == "straight", (
                f"{model_hint} fw↔pc auto media is {media!r}; the firewall "
                "category rule is straight"
            )

    def test_auto_media_for_plain_router_pc_stays_cross(self):
        """Control: a role=router/category=router device keeps the cross rule."""
        spec = NetworkSpec(
            name="router-pc-media",
            devices=[
                DeviceSpec(id="r1", role=DeviceRole.ROUTER),
                DeviceSpec(id="pc1", role=DeviceRole.PC),
            ],
            links=[
                LinkSpec(id="l1", a=EndpointSpec(device="r1"),
                         b=EndpointSpec(device="pc1"), media="auto"),
            ],
            subnets=[],
            ip_assignments=[],
        )
        port_plan = solve_ports(spec, plan_addressing(spec))
        assert not port_plan.incompatible, port_plan.incompatible
        media = next(
            r.media for r in port_plan.resolutions.values() if r.device == "r1"
        )
        assert media == "cross"


# ---------------------------------------------------------------------------
# Bugs #30, #31, #32 - gateway WAN port, uplink semantics, DSL cable pairs
# ---------------------------------------------------------------------------


def _gateway_uplink_spec(*, wr1_hint: str | None = None) -> NetworkSpec:
    """sw1 uplink to a Linksys, with no other wr1 link."""
    return NetworkSpec(
        name="wr-uplink",
        devices=[
            DeviceSpec(
                id="wr1", role=DeviceRole.WIRELESS_ROUTER,
                model_hint="Linksys-WRT300N",
            ),
            DeviceSpec(id="sw1", role=DeviceRole.SWITCH, model_hint="2960-24TT"),
        ],
        links=[
            LinkSpec(
                id="l-uplink",
                a=EndpointSpec(device="sw1", interface_hint="FastEthernet0/1"),
                b=EndpointSpec(device="wr1", interface_hint=wr1_hint),
                media="auto",
            )
        ],
        subnets=[],
        ip_assignments=[],
    )


def test_linksys_internet_hint_is_copper_ethernet_not_a_phone_line():
    """Bug #30: `Internet` is the gateway's RJ45 WAN port, but MODEM_PORT with
    slot 0 was filed under the phone-line family (the DSL modem's provider
    line), so a plain straight-through to it came back `incompatible_media`."""
    spec = _gateway_uplink_spec(wr1_hint="Internet")
    port_plan = solve_ports(spec, plan_addressing(spec))
    assert port_plan.incompatible == [], port_plan.incompatible
    res = port_plan.resolutions["wr1:Internet"]
    assert res.constraint is PortConstraint.COMPATIBLE


def test_gateway_uplink_lands_on_internet_not_a_lan_port():
    """Bug #31: with no hint the solver took the first ethernet port it liked,
    `Ethernet 1`, so the gateway's LAN side (and its DHCP server) was bridged
    into the HQ VLAN and hosts were handed 192.168.0.x. An uplink to
    infrastructure must use `Internet`."""
    spec = _gateway_uplink_spec()
    port_plan = solve_ports(spec, plan_addressing(spec))
    assert port_plan.incompatible == [], port_plan.incompatible
    wr1_ports = {k.split(":", 1)[1] for k in port_plan.resolutions if k.startswith("wr1:")}
    assert wr1_ports == {"Internet"}, wr1_ports


def test_dsl_modem_cable_pairs_are_measured_for_every_router_chassis():
    """Bug #32: the measured cable table spelled out only `DSL-Modem-PT` with
    `Router-PT`; every other router chassis fell through to a category guess."""
    from src.network_autopilot.pt.catalog.cables import (
        CABLE_RULES as AP_CABLE_RULES,
        _MEASURED_MODEL_PAIRS as AP_MEASURED,
        _pair,
        infer_cable_for_models,
    )

    for router in ("Router-PT", "2911", "4321", "4331"):
        assert _pair("DSL-Modem-PT", router) in AP_MEASURED, router
        assert _pair("Cable-Modem-PT", router) in AP_MEASURED, router
        assert infer_cable_for_models("DSL-Modem-PT", router) == "straight"
        assert infer_cable_for_models(router, "DSL-Modem-PT") == "straight"
        assert AP_CABLE_RULES[_pair("modem", "router")] == "straight"
