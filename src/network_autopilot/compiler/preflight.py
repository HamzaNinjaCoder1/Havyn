"""Preflight validator (§9 + §G).

The preflight validator runs before execution and must become a HARD
execution gate. If preflight fails, execution must not continue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.ids import utc_now_iso
from ..core.observability import Observable
from .port_solver import PortPlan, PortConstraint
from .port_reservation import PortReservationSystem, ReservationResult


class PreflightLevel(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    WARNING = "warning"


@dataclass(frozen=True)
class PreflightCheck:
    """One preflight check result."""
    name: str
    category: str
    status: PreflightLevel
    detail: str
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status.value,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass
class PreflightResult:
    """Complete preflight outcome."""
    passed: bool
    blocked: bool
    checks: list[PreflightCheck]
    errors: list[str]
    warnings: list[str]
    timestamp: str = ""

    @property
    def can_execute(self) -> bool:
        """Hard gate: must pass all checks and have no blocked items."""
        return self.passed and not self.blocked

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocked": self.blocked,
            "checks": [c.to_dict() for c in self.checks],
            "errors": self.errors,
            "warnings": self.warnings,
            "timestamp": self.timestamp,
        }


class PreflightValidator:
    """Runs preflight checks before execution."""

    def __init__(self, reservation_system: PortReservationSystem | None = None):
        self.reservation_system = reservation_system or PortReservationSystem()

    def preflight(
        self,
        spec,
        plan,
        port_plan: PortPlan | None = None,
        workspace_names: Observable[list[str]] | None = None,
    ) -> PreflightResult:
        """Run all preflight checks. Returns a result where
        `can_execute` is the hard gate.

        `workspace_names` (S1.4) is the device-name read of the live PT
        workspace. `None` means the caller has no workspace to compare
        (lint, net_preflight, repair) and skips the check; a read that came
        back UNKNOWN fails closed - an unreadable canvas is never treated
        as an empty one.
        """
        checks: list[PreflightCheck] = []
        errors: list[str] = []
        warnings: list[str] = []

        # 0. Workspace collision (S1.4): a device that already answers to a
        # spec name may be a leftover from another topology or a crashed
        # run, and blind reuse would make the report lie about it. Refuse
        # before execution, naming every colliding device.
        if workspace_names is not None:
            if not workspace_names.is_known:
                detail = workspace_names.detail or workspace_names.status.value
                checks.append(PreflightCheck(
                    name="workspace_readable",
                    category="workspace",
                    status=PreflightLevel.FAIL,
                    detail=f"workspace unreadable: {detail}",
                    evidence=(f"status={workspace_names.status.value}",),
                ))
                errors.append(
                    "workspace unreadable: cannot rule out a name collision "
                    f"({detail})"
                )
            else:
                spec_names = {d.id for d in spec.devices}
                colliding = sorted(
                    spec_names & {str(n) for n in (workspace_names.value or [])}
                )
                if colliding:
                    checks.append(PreflightCheck(
                        name="workspace_collision",
                        category="workspace",
                        status=PreflightLevel.FAIL,
                        detail="workspace already has: " + ", ".join(colliding),
                        evidence=tuple(colliding),
                    ))
                    errors.append(
                        "workspace collision: "
                        + ", ".join(colliding)
                        + " already exist in the Packet Tracer workspace; "
                        "delete them first or build with --clean-workspace"
                    )

        # 1. Device existence check
        for device in spec.devices:
            if device.role not in _known_roles():
                checks.append(PreflightCheck(
                    name=f"device_{device.id}_role",
                    category="device",
                    status=PreflightLevel.WARNING,
                    detail=f"device {device.id} has role {device.role.value}",
                    evidence=(f"role={device.role.value}",),
                ))

        # 2. Port compatibility check
        if port_plan is not None:
            for key, resolution in port_plan.resolutions.items():
                if resolution.constraint != PortConstraint.COMPATIBLE:
                    checks.append(PreflightCheck(
                        name=f"port_{key}",
                        category="port",
                        status=PreflightLevel.FAIL,
                        detail=f"{key}: {resolution.constraint.value} — {resolution.reason}",
                        evidence=(f"constraint={resolution.constraint.value}",),
                    ))
                    errors.append(f"port {key} incompatible")

            # Module requirements
            for mod_req in port_plan.module_requirements:
                from ..pt.catalog import resolve_module
                if not resolve_module(mod_req.module_name):
                    checks.append(PreflightCheck(
                        name=f"module_{mod_req.device}_{mod_req.module_name}",
                        category="module",
                        status=PreflightLevel.FAIL,
                        detail=f"module {mod_req.module_name} not found in catalog",
                        evidence=(f"module={mod_req.module_name}",),
                    ))
                    errors.append(f"module {mod_req.module_name} not found")

        # 2b. interface_hint subnet fit (S1.9/F6): a hint that names a port on
        # the WRONG side (a LAN address on the WAN-facing port) builds green and
        # then gives 100% loss. The hinted port must lead, across the solved
        # cable graph, to the subnet's own members; if it does not, refuse with
        # the correct port named. A subnet whose only addressed member is the
        # device itself (a WAN pointed at an unaddressed ISP endpoint) has no
        # members to reach, so it is not judged here.
        if port_plan is not None:
            subnet_members: dict[str, set[str]] = {}
            for assignment in plan.assignments:
                if assignment.ip:
                    subnet_members.setdefault(assignment.subnet_id, set()).add(
                        assignment.device
                    )
            neighbours: dict[str, list[tuple[str, str]]] = {}
            for endpoints in port_plan.link_endpoints.values():
                a_dev, a_port, b_dev, b_port = endpoints
                neighbours.setdefault(a_dev, []).append((a_port, b_dev))
                neighbours.setdefault(b_dev, []).append((b_port, a_dev))

            for assignment in plan.assignments:
                if not assignment.interface_hint or not assignment.ip:
                    continue
                hint = assignment.interface_hint
                if _is_virtual_interface(hint):
                    # An SVI (Vlan1/Vlan2) or radio has no cable of its own:
                    # its traffic rides the switchport the VLAN joins, so the
                    # cable-graph test does not apply. The compiler's own SVI
                    # resolution (ASA 5505 / home gateway) already owns that
                    # mapping.
                    continue
                members = subnet_members.get(assignment.subnet_id, set()) - {
                    assignment.device
                }
                if not members:
                    continue
                if _interface_leads_to_member(
                    assignment.device, hint, members, neighbours
                ):
                    continue
                interfaces = sorted({
                    resolution.interface
                    for resolution in port_plan.resolutions.values()
                    if resolution.device == assignment.device
                })
                correct = [
                    iface
                    for iface in interfaces
                    if _interface_leads_to_member(
                        assignment.device, iface, members, neighbours
                    )
                ]
                suggestion = (
                    "use " + ", ".join(correct) if correct
                    else "no interface on this device reaches that subnet"
                )
                detail = (
                    f"{assignment.device}:{hint} does not lead to subnet "
                    f"{assignment.subnet_id} ({assignment.ip}) — {suggestion}"
                )
                checks.append(PreflightCheck(
                    name=(
                        f"interface_hint_{assignment.device}_"
                        f"{assignment.subnet_id}"
                    ),
                    category="interface_hint",
                    status=PreflightLevel.FAIL,
                    detail=detail,
                    evidence=(f"hint={hint}", f"subnet={assignment.subnet_id}"),
                ))
                errors.append(
                    f"interface_hint {assignment.device}:{hint} is on the wrong "
                    f"side for subnet {assignment.subnet_id} ({assignment.ip}); "
                    f"{suggestion}"
                )

        # 3. Reservation check (if reservation system is available)
        if self.reservation_system and self.reservation_system.reservations:
            unresolved = self.reservation_system.unresolved()
            if unresolved:
                for r in unresolved:
                    checks.append(PreflightCheck(
                        name=f"reservation_{r.reservation_id}",
                        category="reservation",
                        status=PreflightLevel.FAIL,
                        detail=f"{r.device}:{r.interface} reservation failed: {r.reason.value}",
                        evidence=(f"reason={r.reason.value}",),
                    ))
                    errors.append(f"reservation {r.reservation_id} failed")

        # 4. Link cable compatibility
        for link in spec.links:
            if link.media == "auto":
                warnings.append(
                    f"link {link.id}: media auto — will be inferred at execution"
                )
                checks.append(PreflightCheck(
                    name=f"link_{link.id}_media",
                    category="link",
                    status=PreflightLevel.WARNING,
                    detail=f"link {link.id} media auto",
                    evidence=("media=auto",),
                ))

        has_errors = len(errors) > 0
        has_fatal_fails = any(
            c.status == PreflightLevel.FAIL for c in checks
        )
        unresolved_count = 0
        if self.reservation_system and self.reservation_system.reservations:
            unresolved_count = len(self.reservation_system.unresolved())

        result = PreflightResult(
            passed=not has_errors,
            blocked=has_fatal_fails or unresolved_count > 0,
            checks=checks,
            errors=errors,
            warnings=warnings,
            timestamp=utc_now_iso(),
        )
        return result


def _known_roles() -> set:
    from ..core.network_spec import DeviceRole
    return set(DeviceRole)


def _is_virtual_interface(iface: str) -> bool:
    """An interface with no cable of its own: an SVI or a radio.

    `Vlan12` must be matched as a prefix but not confused with a physical
    `Vlan`-named port; PT has no such port, so the prefix rule is exact here.
    """
    name = (iface or "").strip().lower()
    return (
        name.startswith("vlan")
        or name.startswith("wireless")
        or name.startswith("wlan")
        or name.startswith("dot11")
        or name.startswith("loopback")
    )


def _interface_leads_to_member(
    device: str,
    iface: str,
    members: set[str],
    neighbours: dict[str, list[tuple[str, str]]],
) -> bool:
    """Does the cable from `device:iface` reach any of `members`?

    BFS across the solved cable graph, never back through `device` itself, so
    an interface is judged by where it actually leads — the LAN-facing port
    reaches the LAN members, the WAN port does not.
    """
    start = next(
        (nb for my_iface, nb in neighbours.get(device, []) if my_iface == iface),
        None,
    )
    if start is None:
        return False
    seen = {device}
    frontier = [start]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            if node in seen:
                continue
            seen.add(node)
            if node in members:
                return True
            nxt.extend(nb for _, nb in neighbours.get(node, []))
        frontier = nxt
    return False


def run_preflight(
    spec,
    plan,
    port_plan: PortPlan | None = None,
    reservation_system: PortReservationSystem | None = None,
    workspace_names: Observable[list[str]] | None = None,
) -> PreflightResult:
    """Convenience function: create validator and run preflight."""
    validator = PreflightValidator(reservation_system)
    return validator.preflight(spec, plan, port_plan, workspace_names)