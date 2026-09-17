"""IPAM layer: deterministic planning + validation."""

from __future__ import annotations

from .planner import AddressPlan, IpAssignment, PlannedSubnet, PlanningFailure, plan_addressing
from .validator import AddressingIssue, has_errors, validate_plan

__all__ = [
    "AddressPlan",
    "IpAssignment",
    "PlannedSubnet",
    "PlanningFailure",
    "plan_addressing",
    "AddressingIssue",
    "has_errors",
    "validate_plan",
]
