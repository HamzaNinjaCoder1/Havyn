"""Catalog package: PT device, module and cable knowledge.

Re-exports the individual catalog modules so callers can ``from
network_autopilot.pt.catalog import resolve_model`` while the data itself
lives in focused modules (``devices``, ``modules``, ``cables``).
"""

from __future__ import annotations

from .devices import (
    LOGICAL_PORT_RE,
    DeviceModel,
    PortSpec,
    all_models,
    category_of_model,
    get_ports_by_speed,
    get_valid_ports,
    is_logical_port,
    resolve_model,
)
from .modules import (
    ModuleSpec,
    all_modules,
    expansion_module_for_speed,
    get_serial_module,
    module_category_fits,
    ports_for_slot,
    resolve_module,
    speed_of_port_name,
)
from .cables import CABLE_RULES, infer_cable, infer_cable_for_models

__all__ = [
    "LOGICAL_PORT_RE",
    "DeviceModel",
    "PortSpec",
    "all_models",
    "category_of_model",
    "get_ports_by_speed",
    "get_valid_ports",
    "is_logical_port",
    "resolve_model",
    "ModuleSpec",
    "all_modules",
    "expansion_module_for_speed",
    "get_serial_module",
    "module_category_fits",
    "ports_for_slot",
    "resolve_module",
    "speed_of_port_name",
    "CABLE_RULES",
    "infer_cable",
    "infer_cable_for_models",
]
