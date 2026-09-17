"""Addressing validation: every check is evidence-backed and severity-tagged.

Validation failures are ERRORS that block compilation. Nothing here is
downgraded to a warning to make a build pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, ip_network
from typing import Any

from ..core.network_spec import NetworkSpec
from .planner import AddressPlan, PlannedSubnet

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class AddressingIssue:
    resource: str
    problem: str
    evidence: str
    severity: str
    suggested_resolution: str

    def to_dict(self) -> dict[str, str]:
        return {
            "resource": self.resource,
            "problem": self.problem,
            "evidence": self.evidence,
            "severity": self.severity,
            "suggested_resolution": self.suggested_resolution,
        }


def validate_plan(plan: AddressPlan, spec: NetworkSpec) -> list[AddressingIssue]:
    issues: list[AddressingIssue] = []
    issues.extend(_check_allocations(plan))
    issues.extend(_check_assignments(plan))
    issues.extend(_check_gateways(plan))
    return issues


def has_errors(issues: list[AddressingIssue]) -> bool:
    return any(i.severity == ERROR for i in issues)


def _check_allocations(plan: AddressPlan) -> list[AddressingIssue]:
    issues: list[AddressingIssue] = []
    nets: list[tuple[PlannedSubnet, IPv4Network]] = []
    for allocation in plan.allocations:
        try:
            network = ip_network(f"{allocation.network}/{allocation.prefix_len}", strict=True)
        except ValueError as exc:
            issues.append(
                AddressingIssue(
                    allocation.subnet_id, "allocation is not a strict network address",
                    str(exc), ERROR, "fix the allocation algorithm input",
                )
            )
            continue
        nets.append((allocation, network))
    # pairwise overlap
    for i in range(len(nets)):
        for j in range(i + 1, len(nets)):
            a, net_a = nets[i]
            b, net_b = nets[j]
            if net_a.overlaps(net_b):
                issues.append(
                    AddressingIssue(
                        f"{a.subnet_id} vs {b.subnet_id}",
                        "subnets overlap",
                        f"{a.network}/{a.prefix_len} overlaps {b.network}/{b.prefix_len}",
                        ERROR,
                        "widen the pool or reduce requested host counts",
                    )
                )
        # pool containment (planned allocations only; fixed CIDRs declare
        # their own network)
        allocation, network = nets[i]
        if allocation.requested_hosts is not None:
            pool = ip_network(plan.pool, strict=False)
            if not network.subnet_of(pool):  # type: ignore[attr-defined]
                issues.append(
                    AddressingIssue(
                        allocation.subnet_id, "allocation outside the pool",
                        f"{allocation.network}/{allocation.prefix_len} not inside {plan.pool}",
                        ERROR, "use a larger pool",
                    )
                )
    return issues


def _check_assignments(plan: AddressPlan) -> list[AddressingIssue]:
    issues: list[AddressingIssue] = []
    seen: dict[str, str] = {}  # ip -> "device(iface)"
    for assignment in plan.assignments:
        if assignment.ip is None:
            continue
        allocation = plan.allocation_by_subnet(assignment.subnet_id)
        if allocation is None:
            issues.append(
                AddressingIssue(assignment.device, "assignment on unknown subnet",
                                assignment.subnet_id, ERROR, "fix the spec reference")
            )
            continue
        network = IPv4Network(f"{allocation.network}/{allocation.prefix_len}")
        try:
            address = IPv4Address(assignment.ip)
        except ValueError as exc:
            issues.append(
                AddressingIssue(assignment.device, "invalid ip format", str(exc), ERROR,
                                "use a dotted-quad IPv4 address")
            )
            continue
        if address == network.network_address:
            issues.append(
                AddressingIssue(assignment.device, "network address assigned to a host",
                                assignment.ip, ERROR, f"use a host address from {allocation.usable_start}-{allocation.usable_end}")
            )
        if address == network.broadcast_address:
            issues.append(
                AddressingIssue(assignment.device, "broadcast address assigned to a host",
                                assignment.ip, ERROR, f"use a host address from {allocation.usable_start}-{allocation.usable_end}")
            )
        if address not in network:
            issues.append(
                AddressingIssue(assignment.device, "host outside its subnet",
                                f"{assignment.ip} vs {allocation.network}/{allocation.prefix_len}",
                                ERROR, "correct the address or the subnet")
            )
        where = f"{assignment.device}({assignment.interface_hint or '?'})"
        if assignment.ip in seen:
            issues.append(
                AddressingIssue(
                    where, "duplicate ip address",
                    f"{assignment.ip} already used by {seen[assignment.ip]}",
                    ERROR, "assign distinct addresses",
                )
            )
        else:
            seen[assignment.ip] = where
    return issues


def _check_gateways(plan: AddressPlan) -> list[AddressingIssue]:
    issues: list[AddressingIssue] = []
    for allocation in plan.allocations:
        network = IPv4Network(f"{allocation.network}/{allocation.prefix_len}")
        try:
            gateway = IPv4Address(allocation.gateway)
        except ValueError as exc:
            issues.append(
                AddressingIssue(allocation.subnet_id, "invalid gateway format", str(exc), ERROR,
                                "use a dotted-quad IPv4 address")
            )
            continue
        if gateway not in network:
            issues.append(
                AddressingIssue(allocation.subnet_id, "gateway outside its subnet",
                                f"{allocation.gateway} vs {allocation.network}/{allocation.prefix_len}",
                                ERROR, f"pick a gateway from {allocation.usable_start}-{allocation.usable_end}")
            )
        elif gateway in (network.network_address, network.broadcast_address):
            issues.append(
                AddressingIssue(allocation.subnet_id, "gateway on network/broadcast address",
                                allocation.gateway, ERROR, "pick a usable host address")
            )
    return issues
