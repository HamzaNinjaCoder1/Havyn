"""Diagnosis layer: evidence-based root causes, never speculation."""

from __future__ import annotations

from .engine import Diagnosis, diagnose, fill_missing_routes
from .fault_injection import (
    FIRST_FIVE,
    PORT_SHUTDOWN,
    ROUTE_MISSING,
    TRUNK_VLAN_NOT_ALLOWED,
    WRONG_ACCESS_VLAN,
    WRONG_GATEWAY,
)

__all__ = [
    "Diagnosis",
    "diagnose",
    "fill_missing_routes",
    "FIRST_FIVE",
    "PORT_SHUTDOWN",
    "ROUTE_MISSING",
    "TRUNK_VLAN_NOT_ALLOWED",
    "WRONG_ACCESS_VLAN",
    "WRONG_GATEWAY",
]
