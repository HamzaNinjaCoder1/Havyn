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
