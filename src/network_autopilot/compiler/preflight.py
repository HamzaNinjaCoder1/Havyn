"""Preflight validator (§9 + §G).

The preflight validator runs before execution and must become a HARD
execution gate. If preflight fails, execution must not continue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.ids import utc_now_iso
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
    ) -> PreflightResult:
        """Run all preflight checks. Returns a result where
        `can_execute` is the hard gate.
        """
        checks: list[PreflightCheck] = []
        errors: list[str] = []
        warnings: list[str] = []

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


def run_preflight(
    spec,
    plan,
    port_plan: PortPlan | None = None,
    reservation_system: PortReservationSystem | None = None,
) -> PreflightResult:
    """Convenience function: create validator and run preflight."""
    validator = PreflightValidator(reservation_system)
    return validator.preflight(spec, plan, port_plan)