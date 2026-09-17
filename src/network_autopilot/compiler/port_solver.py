"""Port/Media/Module solver (§9).

Before execution the system determines:
- required device interfaces
- available interfaces
- interface media/type
- module requirements
- compatible ports
- incompatible ports
- reserved ports
- already-used ports
- cable/media compatibility
- topology requirements

The solver is adapter-agnostic: it works from the NetworkSpec +
AddressPlan + the upstream catalog (device models, module specs, cable
rules). It produces a PortPlan that the compiler consumes and the
executor enforces via the reservation system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.network_spec import (
    DeviceSpec,
    DeviceRole,
    InterfaceRequirement,
    LinkSpec,
    NetworkSpec,
)
from ..ipam.planner import AddressPlan
from ...packet_tracer_mcp.infrastructure.catalog.devices import (
    ALL_MODELS,
    DeviceModel,
    PortSpec,
    resolve_model,
)
from ...packet_tracer_mcp.infrastructure.catalog.modules import (
    ALL_MODULES,
    ModuleSpec,
    get_serial_module,
    ports_for_slot,
)
from ...packet_tracer_mcp.infrastructure.catalog.cables import (
    CABLE_RULES,
    infer_cable,
)


class PortConstraint(str, Enum):
    """Why a port may or may not be usable."""
    COMPATIBLE = "compatible"
    INCOMPATIBLE_MEDIA = "incompatible_media"
    INCOMPATIBLE_MODEL = "incompatible_model"
    SLOT_MISMATCH = "slot_mismatch"
    PORT_ABSENT = "port_absent"
    RESERVED = "reserved"
    ALREADY_USED = "already_used"


@dataclass(frozen=True)
class PortResolution:
    """The resolved port for one link endpoint."""
    device: str
    role: DeviceRole
    interface: str
    media: str
    module_required: str | None
    speed: str
    slot: str | None
    constraint: PortConstraint = PortConstraint.COMPATIBLE
    reason: str = ""


@dataclass(frozen=True)
class ModuleRequirement:
    """A module that must be installed on a device before it can serve a link."""
    device: str
    pt_type: str
    module_name: str
    slot: str
    ports_added: tuple[str, ...]
    category: str


@dataclass
class PortPlan:
    """The complete port-resolution outcome for a compiled spec."""
    resolutions: dict[str, PortResolution] = field(default_factory=dict)
    module_requirements: list[ModuleRequirement] = field(default_factory=list)
    incompatible: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_ports: dict[str, set[str]] = field(default_factory=dict)

    def add_resolution(self, key: str, resolution: PortResolution) -> None:
        self.resolutions[key] = resolution
        dev = resolution.device
        if dev not in self.used_ports:
            self.used_ports[dev] = set()
        self.used_ports[dev].add(resolution.interface)

    def is_compatible(self, key: str) -> bool:
        r = self.resolutions.get(key)
        return r is not None and r.constraint == PortConstraint.COMPATIBLE

    def device_needs_module(self, device: str) -> list[ModuleRequirement]:
        return [m for m in self.module_requirements if m.device == device]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _model_for_device(spec: NetworkSpec, device_id: str) -> DeviceModel | None:
    """Resolve the DeviceModel for a spec device."""
    dev = spec.device_by_id(device_id)
    if not dev:
        return None
    if dev.model_hint:
        model = resolve_model(dev.model_hint)
        if model:
            return model
    # role-based default
    role_model: dict[str, str] = {
        DeviceRole.ROUTER: "2911",
        DeviceRole.SWITCH: "2960-24TT",
        DeviceRole.L3_SWITCH: "3560-24PS",
        DeviceRole.PC: "PC-PT",
        DeviceRole.SERVER: "Server-PT",
        DeviceRole.LAPTOP: "Laptop-PT",
        DeviceRole.ACCESS_POINT: "AccessPoint-PT",
        DeviceRole.CLOUD: "Cloud-PT",
        DeviceRole.OTHER: "Router-PT",
    }
    default = role_model.get(dev.role, "Router-PT")
    return resolve_model(default)


def _model_for_role(role: DeviceRole) -> DeviceModel | None:
    role_model: dict[str, str] = {
        DeviceRole.ROUTER: "2911",
        DeviceRole.SWITCH: "2960-24TT",
        DeviceRole.L3_SWITCH: "3560-24PS",
        DeviceRole.PC: "PC-PT",
        DeviceRole.SERVER: "Server-PT",
        DeviceRole.LAPTOP: "Laptop-PT",
        DeviceRole.ACCESS_POINT: "AccessPoint-PT",
        DeviceRole.CLOUD: "Cloud-PT",
        DeviceRole.OTHER: "Router-PT",
    }
    return resolve_model(role_model.get(role, "Router-PT"))


def _port_speed_name(speed) -> str:
    from ...packet_tracer_mcp.shared.enums import PortSpeed
    if hasattr(speed, "value"):
        return speed.value
    return str(speed)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def solve_ports(
    spec: NetworkSpec,
    plan: AddressPlan,
) -> PortPlan:
    """Resolve every link endpoint to a concrete port + media + module.

    Returns a PortPlan. Incompatible endpoints are recorded in
    `plan.incompatible` — the compiler must not emit link operations
    for those, and the preflight gate must block them.
    """
    plan_out = PortPlan()
    # Track per-device port usage across all links
    used: dict[str, set[str]] = {}

    for link in spec.links:
        a_dev = spec.device_by_id(link.a.device)
        b_dev = spec.device_by_id(link.b.device)
        if not a_dev or not b_dev:
            plan_out.incompatible.append(
                f"link {link.id}: unknown endpoint device"
            )
            continue

        a_model = _model_for_device(spec, link.a.device)
        b_model = _model_for_device(spec, link.b.device)
        if not a_model or not b_model:
            plan_out.incompatible.append(
                f"link {link.id}: cannot resolve model for endpoint"
            )
            continue

        a_role = a_dev.role
        b_role = b_dev.role

        # Determine which interface each endpoint uses
        a_iface = link.a.interface_hint or _default_port_for_role(a_role, used.get(link.a.device, set()))
        b_iface = link.b.interface_hint or _default_port_for_role(b_role, used.get(link.b.device, set()))

        # Check module requirements for serial/wan interfaces
        a_module = _module_for_interface(a_model, a_iface, link.a.device)
        b_module = _module_for_interface(b_model, b_iface, link.b.device)

        # Determine media from the link spec and the cable rules
        media = link.media if link.media != "auto" else _infer_media(a_role, b_role)

        # Validate port exists on the model
        a_valid = _port_exists(a_model, a_iface)
        b_valid = _port_exists(b_model, b_iface)

        # Validate media compatibility
        a_compat = _media_compatible(a_model, a_iface, media)
        b_compat = _media_compatible(b_model, b_iface, media)

        a_constraint = _constraint(a_valid, a_compat)
        b_constraint = _constraint(b_valid, b_compat)

        a_res = PortResolution(
            device=link.a.device,
            role=a_role,
            interface=a_iface,
            media=media,
            module_required=a_module.name if a_module else None,
            speed=_port_speed_name(_port_speed(a_model, a_iface)),
            slot=_slot_for(a_model, a_iface),
            constraint=a_constraint,
            reason=_reason(a_valid, a_compat),
        )
        b_res = PortResolution(
            device=link.b.device,
            role=b_role,
            interface=b_iface,
            media=media,
            module_required=b_module.name if b_module else None,
            speed=_port_speed_name(_port_speed(b_model, b_iface)),
            slot=_slot_for(b_model, b_iface),
            constraint=b_constraint,
            reason=_reason(b_valid, b_compat),
        )

        if a_constraint != PortConstraint.COMPATIBLE:
            plan_out.incompatible.append(
                f"link {link.id} {link.a.device}:{a_iface} — {a_constraint.value}: {a_res.reason}"
            )
        if b_constraint != PortConstraint.COMPATIBLE:
            plan_out.incompatible.append(
                f"link {link.id} {link.b.device}:{b_iface} — {b_constraint.value}: {b_res.reason}"
            )

        plan_out.add_resolution(f"{link.a.device}:{a_iface}", a_res)
        plan_out.add_resolution(f"{link.b.device}:{b_iface}", b_res)

        # Track module requirements
        if a_module:
            plan_out.module_requirements.append(ModuleRequirement(
                device=link.a.device,
                pt_type=a_model.pt_type,
                module_name=a_module.name,
                slot=_slot_str(a_model, a_iface),
                ports_added=tuple(ports_for_slot(a_module, _slot_str(a_model, a_iface))),
                category=a_module.category,
            ))
        if b_module:
            plan_out.module_requirements.append(ModuleRequirement(
                device=link.b.device,
                pt_type=b_model.pt_type,
                module_name=b_module.name,
                slot=_slot_str(b_model, b_iface),
                ports_added=tuple(ports_for_slot(b_module, _slot_str(b_model, b_iface))),
                category=b_module.category,
            ))

        # Mark used
        used.setdefault(link.a.device, set()).add(a_iface)
        used.setdefault(link.b.device, set()).add(b_iface)

    plan_out.used_ports = used
    return plan_out


def _default_port_for_role(role: DeviceRole, used: set[str]) -> str:
    candidates = _port_templates().get(role.value, _port_templates()["other"])
    for c in candidates:
        if c not in used:
            return c
    return candidates[0] if candidates else "FastEthernet0"


def _port_templates() -> dict[str, list[str]]:
    return {
        "router": ["GigabitEthernet0/0", "GigabitEthernet0/1", "GigabitEthernet0/2"],
        "switch": ["FastEthernet0/1", "FastEthernet0/2", "FastEthernet0/3", "FastEthernet0/4",
                   "FastEthernet0/5", "FastEthernet0/6", "FastEthernet0/7", "FastEthernet0/8",
                   "GigabitEthernet0/1", "GigabitEthernet0/2"],
        "l3_switch": ["FastEthernet0/1", "FastEthernet0/2", "GigabitEthernet0/1"],
        "pc": ["FastEthernet0"],
        "server": ["FastEthernet0"],
        "laptop": ["FastEthernet0"],
        "access_point": ["FastEthernet0"],
        "cloud": [],
        "other": ["FastEthernet0"],
    }


def _port_exists(model: DeviceModel, iface: str) -> bool:
    if not model:
        return False
    valid = {p.full_name for p in model.ports}
    return iface in valid or iface in _expanded_names(model, iface)


def _expanded_names(model: DeviceModel, iface: str) -> set[str]:
    """Expand abbreviated names like Fa0/1 -> FastEthernet0/1."""
    result: set[str] = set()
    for p in model.ports:
        if p.full_name.startswith(iface[:2]) and not p.full_name.startswith(iface):
            result.add(p.full_name)
    return result


def _port_speed(model: DeviceModel, iface: str) -> PortSpec | None:
    for p in model.ports:
        if p.full_name == iface:
            return p
    return None


def _slot_for(model: DeviceModel, iface: str) -> str | None:
    for p in model.ports:
        if p.full_name == iface and p.slot:
            return p.slot
    return None


def _slot_str(model: DeviceModel, iface: str) -> str:
    slot = _slot_for(model, iface)
    return slot if slot else "0"


def _module_for_interface(
    model: DeviceModel, iface: str, device_id: str
) -> ModuleSpec | None:
    """Check if an interface requires a module to be installed."""
    if not model or not iface:
        return None
    # Serial interfaces on ISR routers need HWIC-2T or NIM-2T
    if iface.startswith("Serial") or iface.startswith("Se"):
        serial_mod = get_serial_module(model.pt_type)
        if serial_mod:
            return serial_mod
    # Gigabit interfaces on 2911 might need HWIC-1GE-SFP
    if iface.startswith("GigabitEthernet") and model.pt_type in ("1941", "2901", "2911"):
        for m in ALL_MODULES.values():
            if m.name == "HWIC-1GE-SFP" and m.compatible_with and model.pt_type in m.compatible_with:
                return m
    return None


def _media_compatible(model: DeviceModel, iface: str, media: str) -> bool:
    """Check if a media type is compatible with a given port."""
    if not model or not iface:
        return False
    port = _port_speed(model, iface)
    if not port:
        return False
    speed = _port_speed_name(port.speed)
    # Serial ports only accept serial cable
    if speed == "Serial":
        return media == "serial"
    # Ethernet/Gigabit ports accept straight/cross/auto
    if speed in ("Ethernet", "FastEthernet", "GigabitEthernet"):
        return media in ("straight", "cross", "crossover", "auto")
    # Fiber ports
    if speed == "Fiber":
        return media == "fiber"
    # Wireless
    if speed == "Wireless":
        return media == "wireless"
    return media in ("auto", "straight", "cross")


def _infer_media(role_a: DeviceRole, role_b: DeviceRole) -> str:
    """Infer the cable media from the two roles (for media='auto')."""
    return infer_cable(role_a.value, role_b.value)


def _constraint(valid: bool, compatible: bool) -> PortConstraint:
    if not valid:
        return PortConstraint.PORT_ABSENT
    if not compatible:
        return PortConstraint.INCOMPATIBLE_MEDIA
    return PortConstraint.COMPATIBLE


def _reason(valid: bool, compatible: bool) -> str:
    if not valid:
        return "port not found on device model"
    if not compatible:
        return "media type incompatible with port speed"
    return "port is compatible"


def required_interfaces(spec: NetworkSpec) -> dict[str, list[str]]:
    """All interfaces required by the spec, per device."""
    needed: dict[str, list[str]] = {}
    for link in spec.links:
        dev_a = link.a.device
        dev_b = link.b.device
        needed.setdefault(dev_a, []).append(link.a.interface_hint or "")
        needed.setdefault(dev_b, []).append(link.b.interface_hint or "")
    for iface_reqs in spec.device_requirements if hasattr(spec, 'device_requirements') else []:
        pass
    return needed


def available_ports(device_id: str, spec: NetworkSpec) -> list[str]:
    """All available physical ports for a device in the spec."""
    dev = spec.device_by_id(device_id)
    if not dev:
        return []
    model = _model_for_device(spec, device_id)
    if not model:
        return []
    return [p.full_name for p in model.ports]


def compatible_cable(media: str, role_a: str, role_b: str) -> str | None:
    """Check if a cable media is valid between two role categories."""
    rule = CABLE_RULES.get((role_a, role_b))
    if rule:
        return rule
    return infer_cable(role_a, role_b) if (role_a and role_b) else None


def reserved_ports(plan: PortPlan) -> dict[str, set[str]]:
    """Return the set of ports already reserved across the plan."""
    return dict(plan.used_ports)
