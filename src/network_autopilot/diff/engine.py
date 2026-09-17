"""Semantic expected-vs-actual diff.

Compiles the intended model (spec + addressing plan) against the observed
ActualNetworkState and reports MATCH / ADD / REMOVE / CHANGE / UNKNOWN per
semantic path. Core honesty rule: NOT OBSERVED is not MISSING — absence in a
possibly-partial observation is UNKNOWN; absence in a complete observation is
REMOVE (or CHANGE for wrong values).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..core.actual_state import ActualNetworkState
from ..core.network_spec import NetworkSpec
from ..ipam.planner import AddressPlan

_EMPTY_READ = "runtime returned an empty value"


class DiffKind(str, Enum):
    MATCH = "match"
    ADD = "add"          # expected references it, actual has something extra
    REMOVE = "remove"    # expected has it, complete observation says it's gone
    CHANGE = "change"    # present but wrong
    UNKNOWN = "unknown"  # cannot judge: partial observation / unreadable


@dataclass(frozen=True)
class DiffEntry:
    path: str            # e.g. "device(pc1)", "device(r1).port(Gi0/0).ip"
    kind: DiffKind
    expected: str | None
    actual: str | None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind.value,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DiffResult:
    entries: tuple[DiffEntry, ...]
    drift_count: int     # CHANGE + REMOVE + ADD
    unknown_count: int

    @property
    def clean(self) -> bool:
        return self.drift_count == 0 and self.unknown_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "drift_count": self.drift_count,
            "unknown_count": self.unknown_count,
            "clean": self.clean,
        }


def diff_expected_vs_actual(
    spec: NetworkSpec,
    plan: AddressPlan,
    state: ActualNetworkState,
    *,
    observations_complete: bool = False,
    observed_names: set[str] | None = None,
) -> DiffResult:
    """Full semantic diff of intended vs observed state.

    `observed_names` lets the caller state exactly which devices were part of
    the observation sweep; anything outside it is UNKNOWN, never MISSING.
    """
    entries: list[DiffEntry] = []
    names = observed_names if observed_names is not None else set(state.devices)

    # -- devices ------------------------------------------------------------
    for device in spec.devices:
        path = f"device({device.id})"
        if device.id in names:
            observed = state.devices.get(device.id)
            expected_model = device.model_hint or _role_default(device.role.value)
            if observed is not None and observed.model.is_known:
                if observed.model.value == expected_model:
                    entries.append(
                        DiffEntry(path, DiffKind.MATCH, expected_model, observed.model.value)
                    )
                else:
                    entries.append(
                        DiffEntry(
                            path, DiffKind.CHANGE, expected_model, observed.model.value,
                            "wrong model",
                        )
                    )
            else:
                entries.append(
                    DiffEntry(
                        path, DiffKind.UNKNOWN, expected_model, None,
                        observed.detail if observed else "device listed but not inspected",
                    )
                )
        elif device.id in state.devices:
            # present in state (inspected) but not in the observation sweep
            entries.append(
                DiffEntry(path, DiffKind.UNKNOWN, _role_default(device.role.value), None,
                          "present in state but outside the observation sweep")
            )
        else:
            entries.append(
                DiffEntry(
                    path,
                    DiffKind.REMOVE if observations_complete else DiffKind.UNKNOWN,
                    _role_default(device.role.value),
                    None,
                    "missing" if observations_complete else "not observed",
                )
            )

    # -- addresses ------------------------------------------------------------
    for assignment in plan.assignments:
        if assignment.ip is None:
            continue
        allocation = plan.allocation_by_subnet(assignment.subnet_id)
        iface = assignment.interface_hint or "FastEthernet0"
        path = f"device({assignment.device}).port({iface}).ip"
        device = state.devices.get(assignment.device)
        if device is None:
            entries.append(
                DiffEntry(
                    path,
                    DiffKind.REMOVE if observations_complete else DiffKind.UNKNOWN,
                    assignment.ip, None,
                    "device absent from observation" if observations_complete else "device not observed",
                )
            )
            continue
        port = device.ports.get(iface)
        if port is None:
            entries.append(
                DiffEntry(path, DiffKind.UNKNOWN, assignment.ip, None,
                          f"interface {iface} not observed")
            )
            continue
        if port.ip.is_known and port.mask.is_known:
            actual = f"{port.ip.value}/{port.mask.value}"
            expected = f"{assignment.ip}/{allocation.netmask}" if allocation else assignment.ip
            if port.ip.value == assignment.ip and (allocation is None or port.mask.value == allocation.netmask):
                entries.append(DiffEntry(path, DiffKind.MATCH, expected, actual))
            else:
                entries.append(
                    DiffEntry(path, DiffKind.CHANGE, expected, actual, "wrong addressing")
                )
        elif port.ip.is_known:
            # IP readable but mask not: the IP comparison alone still decides.
            matches = port.ip.value == assignment.ip
            entries.append(
                DiffEntry(
                    path,
                    DiffKind.MATCH if matches else DiffKind.CHANGE,
                    assignment.ip,
                    port.ip.value,
                    None if matches else "wrong ip (mask not observed)",
                )
            )
        elif port.ip.status.value == "unknown" and port.ip.detail == _EMPTY_READ:
            entries.append(
                DiffEntry(path, DiffKind.CHANGE, assignment.ip, None, "no ip configured")
            )
        else:
            entries.append(
                DiffEntry(path, DiffKind.UNKNOWN, assignment.ip, None,
                          port.ip.detail or port.ip.status.value)
            )

    # -- links ------------------------------------------------------------------
    for link in spec.links:
        path = f"link({link.id})"
        matching = [
            l for l in state.links
            if l.ref_id.is_known and l.ref_id.value == link.id
        ]
        if matching:
            observed = matching[0]
            a = observed.endpoint_a.value if observed.endpoint_a.is_known else "?"
            b = observed.endpoint_b.value if observed.endpoint_b.is_known else "?"
            entries.append(DiffEntry(path, DiffKind.MATCH, "present", f"{a} <-> {b}"))
        elif link.id in {l.ref_id.value for l in state.links if l.ref_id.is_known}:
            entries.append(DiffEntry(path, DiffKind.MATCH, "present", "present"))
        elif observations_complete:
            entries.append(DiffEntry(path, DiffKind.REMOVE, "present", None, "missing"))
        else:
            entries.append(
                DiffEntry(path, DiffKind.UNKNOWN, "present", None,
                          "link not observed (peer/cable observation not supported by adapter)")
            )

    drift = sum(1 for e in entries if e.kind in (DiffKind.CHANGE, DiffKind.REMOVE, DiffKind.ADD))
    unknown = sum(1 for e in entries if e.kind is DiffKind.UNKNOWN)
    return DiffResult(entries=tuple(entries), drift_count=drift, unknown_count=unknown)


def _role_default(role: str) -> str:
    return {
        "router": "2911",
        "switch": "2960-24TT",
        "l3_switch": "3560-24PS",
        "pc": "PC",
        "server": "Server-PT",
        "laptop": "Laptop-PT",
    }.get(role, "Generic")
