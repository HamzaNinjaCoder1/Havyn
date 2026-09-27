"""Port reservation system (§9).

Adds port reservation BEFORE execution. Prevents two operations from
consuming the same physical/interface resource. A failed reservation
blocks dependent operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.ids import utc_now_iso
from .port_solver import (
    ModuleRequirement,
    PortPlan,
    PortResolution,
    PortConstraint,
    solve_ports,
)


class ReservationStatus(str, Enum):
    RESERVED = "reserved"
    FAILED = "failed"
    RELEASED = "released"
    EXPIRED = "expired"


class ReservationReason(str, Enum):
    OK = "ok"
    PORT_IN_USE = "port_in_use"
    INCOMPATIBLE_MEDIA = "incompatible_media"
    MISSING_MODULE = "missing_module"
    SLOT_UNAVAILABLE = "slot_unavailable"
    DEVICE_UNKNOWN = "device_unknown"


@dataclass(frozen=True)
class PortReservation:
    """One port reservation record."""
    reservation_id: str
    device: str
    interface: str
    media: str
    status: ReservationStatus = ReservationStatus.RESERVED
    reason: ReservationReason = ReservationReason.OK
    module_name: str | None = None
    slot: str | None = None
    reserved_at: str = ""
    released_at: str | None = None
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "device": self.device,
            "interface": self.interface,
            "media": self.media,
            "status": self.status.value,
            "reason": self.reason.value,
            "module_name": self.module_name,
            "slot": self.slot,
            "reserved_at": self.reserved_at,
            "released_at": self.released_at,
            "operation_id": self.operation_id,
        }


@dataclass
class ReservationResult:
    """Outcome of a reservation attempt."""
    success: bool
    reservation: PortReservation | None = None
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.success


@dataclass
class PortReservationSystem:
    """Tracks all port reservations across the build lifecycle."""
    reservations: dict[str, PortReservation] = field(default_factory=dict)
    device_reservations: dict[str, dict[str, str]] = field(default_factory=dict)
    # device -> {interface -> operation_id}

    # -- pre-execution reservation --------------------------------------

    def reserve(
        self,
        plan: PortPlan,
        operation_id: str = "",
    ) -> tuple[list[PortReservation], list[str]]:
        """Reserve all ports in a PortPlan BEFORE execution.

        Returns (reservations, blocked_reasons). If any reservation
        fails, the blocked_reason list has entries explaining why.
        """
        reservations: list[PortReservation] = []
        blocked: list[str] = []

        for key, resolution in plan.resolutions.items():
            device = resolution.device
            iface = resolution.interface

            # Check if port is already reserved by another operation
            if self._is_reserved(device, iface):
                existing_op = self.device_reservations[device][iface]
                blocked.append(
                    f"PORT CONFLICT: {device}:{iface} already reserved by {existing_op}"
                )
                res = PortReservation(
                    reservation_id=f"res-{key}",
                    device=device,
                    interface=iface,
                    media=resolution.media,
                    status=ReservationStatus.FAILED,
                    reason=ReservationReason.PORT_IN_USE,
                    module_name=resolution.module_required,
                    slot=resolution.slot,
                    reserved_at=utc_now_iso(),
                    operation_id=operation_id,
                )
                self.reservations[f"res-{key}"] = res
                continue

            # Check module requirement
            if resolution.module_required:
                mod = self._check_module(
                    resolution.device, resolution.module_required
                )
                if not mod:
                    blocked.append(
                        f"MISSING MODULE: {device}:{iface} requires "
                        f"{resolution.module_required}"
                    )
                    res = PortReservation(
                        reservation_id=f"res-{key}",
                        device=device,
                        interface=iface,
                        media=resolution.media,
                        status=ReservationStatus.FAILED,
                        reason=ReservationReason.MISSING_MODULE,
                        module_name=resolution.module_required,
                        slot=resolution.slot,
                        reserved_at=utc_now_iso(),
                        operation_id=operation_id,
                    )
                    self.reservations[f"res-{key}"] = res
                    continue

            # Check media compatibility
            if resolution.constraint != PortConstraint.COMPATIBLE:
                blocked.append(
                    f"INCOMPATIBLE: {device}:{iface} — "
                    f"{resolution.constraint.value}: {resolution.reason}"
                )
                res = PortReservation(
                    reservation_id=f"res-{key}",
                    device=device,
                    interface=iface,
                    media=resolution.media,
                    status=ReservationStatus.FAILED,
                    reason=ReservationReason.INCOMPATIBLE_MEDIA,
                    module_name=resolution.module_required,
                    slot=resolution.slot,
                    reserved_at=utc_now_iso(),
                    operation_id=operation_id,
                )
                self.reservations[f"res-{key}"] = res
                continue

            # Reserve it
            res = PortReservation(
                reservation_id=f"res-{key}",
                device=device,
                interface=iface,
                media=resolution.media,
                status=ReservationStatus.RESERVED,
                reason=ReservationReason.OK,
                module_name=resolution.module_required,
                slot=resolution.slot,
                reserved_at=utc_now_iso(),
                operation_id=operation_id,
            )
            self.reservations[f"res-{key}"] = res
            self._mark_reserved(device, iface, operation_id)
            reservations.append(res)

        return reservations, blocked

    def reserve_single(
        self,
        device: str,
        interface: str,
        media: str,
        module_name: str | None = None,
        slot: str | None = None,
        operation_id: str = "",
    ) -> ReservationResult:
        """Reserve a single port for one operation."""
        if self._is_reserved(device, interface):
            return ReservationResult(
                success=False,
                reason=f"{device}:{interface} already reserved by "
                f"{self.device_reservations.get(device, {}).get(interface, 'unknown')}",
            )
        res = PortReservation(
            reservation_id=f"res-{device}-{interface}",
            device=device,
            interface=interface,
            media=media,
            status=ReservationStatus.RESERVED,
            reason=ReservationReason.OK,
            module_name=module_name,
            slot=slot,
            reserved_at=utc_now_iso(),
            operation_id=operation_id,
        )
        self.reservations[res.reservation_id] = res
        self._mark_reserved(device, interface, operation_id)
        return ReservationResult(success=True, reservation=res)

    def release(self, device: str, interface: str) -> bool:
        """Release a previously reserved port."""
        key = f"{device}:{interface}"
        for rid, res in self.reservations.items():
            if res.device == device and res.interface == interface:
                if res.status == ReservationStatus.RESERVED:
                    updated = PortReservation(
                        **{**res.to_dict(), "status": ReservationStatus.RELEASED.value,
                           "released_at": utc_now_iso()}
                    )
                    self.reservations[rid] = updated
                if device in self.device_reservations:
                    self.device_reservations[device].pop(interface, None)
                return True
        return False

    def release_operation(self, operation_id: str) -> int:
        """Release all reservations tied to an operation."""
        count = 0
        for rid, res in list(self.reservations.items()):
            if res.operation_id == operation_id and res.status == ReservationStatus.RESERVED:
                self.reservations[rid] = PortReservation(
                    **{**res.to_dict(), "status": ReservationStatus.RELEASED.value,
                       "released_at": utc_now_iso()}
                )
                if res.device in self.device_reservations:
                    self.device_reservations[res.device].pop(res.interface, None)
                count += 1
        return count

    def is_reserved(self, device: str, interface: str) -> bool:
        return self._is_reserved(device, interface)

    def get_reservation(self, device: str, interface: str) -> PortReservation | None:
        for res in self.reservations.values():
            if res.device == device and res.interface == interface and res.status == ReservationStatus.RESERVED:
                return res
        return None

    def unresolved(self) -> list[PortReservation]:
        return [r for r in self.reservations.values() if r.status == ReservationStatus.FAILED]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.reservations.values():
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        return counts

    # -- internal ---------------------------------------------------------

    def _is_reserved(self, device: str, interface: str) -> bool:
        return (
            device in self.device_reservations
            and interface in self.device_reservations[device]
        )

    def _mark_reserved(self, device: str, interface: str, op_id: str) -> None:
        self.device_reservations.setdefault(device, {})[interface] = op_id

    def _check_module(
        self, device: str, module_name: str
    ) -> bool:
        """Check if a module exists in the catalog."""
        from ..pt.catalog import resolve_module
        return resolve_module(module_name) is not None
