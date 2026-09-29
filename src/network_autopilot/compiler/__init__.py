"""Compiler layer: operations, DAG, deterministic compilation.

Includes the Port/Media/Module solver (§9), preflight hard gate (§G),
and port reservation system.
"""

from __future__ import annotations

from .compiler import CompileError, CompileResult, COMPILER_VERSION, compile_spec, net_build, preflight_blocked_ops
from .dag import DagError, OperationDag, OpStatus
from .operations import NetworkOperation, Risk
from .port_solver import PortPlan, PortResolution, PortConstraint, ModuleRequirement, solve_ports
from .port_reservation import PortReservationSystem, PortReservation, ReservationResult, ReservationStatus, ReservationReason
from .preflight import PreflightValidator, PreflightResult, PreflightCheck, PreflightLevel, run_preflight

__all__ = [
    "CompileError",
    "CompileResult",
    "COMPILER_VERSION",
    "compile_spec",
    "net_build",
    "preflight_blocked_ops",
    "DagError",
    "OperationDag",
    "OpStatus",
    "NetworkOperation",
    "Risk",
    "PortPlan",
    "PortResolution",
    "PortConstraint",
    "ModuleRequirement",
    "solve_ports",
    "PortReservationSystem",
    "PortReservation",
    "ReservationResult",
    "ReservationStatus",
    "ReservationReason",
    "PreflightValidator",
    "PreflightResult",
    "PreflightCheck",
    "PreflightLevel",
    "run_preflight",
]
