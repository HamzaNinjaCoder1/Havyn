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
    default_model_for_role,
)
from ..ipam.planner import AddressPlan
from ..pt.catalog import (
    DeviceModel,
    PortSpec,
    ModuleSpec,
    CABLE_RULES,
    expansion_module_for_speed,
    infer_cable,
    module_category_fits,
    ports_for_slot,
    resolve_model,
)
from ..pt.enums import PortSpeed


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
    link_endpoints: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    # RF links have no cabling endpoints; the ids live here so the compiler
    # and preflight can see that the link was considered, not forgotten.
    wireless_links: list[str] = field(default_factory=list)

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
    # role-based default - the canonical source, shared with the compiler
    return resolve_model(default_model_for_role(dev.role))


def _model_for_role(role: DeviceRole) -> DeviceModel | None:
    return resolve_model(default_model_for_role(role))


def _port_speed_name(speed) -> str:
    from ..pt.enums import PortSpeed
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
        if link.media == "wireless":
            # An RF association is not a cable: it consumes no physical port,
            # so it must not reserve one. The wireless operations install the
            # client radio and read the association back from PT instead.
            plan_out.wireless_links.append(link.id)
            continue
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

        # Determine the media first: port selection depends on it.
        media = (
            link.media
            if link.media != "auto"
            else _infer_media(
                a_model.category, b_model.category,
                _pt_type_of(a_model), _pt_type_of(b_model),
            )
        )

        # Determine which interface each endpoint uses. Hints win; otherwise
        # the port is chosen from the device's REAL catalog ports (and, for
        # media the device has no onboard port for, from an installable module).
        a_iface, a_module = _resolve_endpoint(
            link.a.device, a_model, a_role, link.a.interface_hint, media, used,
            wan_side=_wants_wan_port(a_model, b_model),
        )
        b_iface, b_module = _resolve_endpoint(
            link.b.device, b_model, b_role, link.b.interface_hint, media, used,
            wan_side=_wants_wan_port(b_model, a_model),
        )
        for dev_id, iface, model in (
            (link.a.device, a_iface, a_model),
            (link.b.device, b_iface, b_model),
        ):
            if iface is None:
                plan_out.incompatible.append(
                    f"link {link.id} {dev_id}: no free port on "
                    f"{_model_label(spec, dev_id, model)} for media {media!r} "
                    f"(all matching ports already used by other links)"
                    f"{_exhaustion_hint(model)}"
                )

        if a_iface is None or b_iface is None:
            # An unresolvable endpoint is recorded above; nothing more to
            # plan for this link. Ports are not reserved.
            continue

        if a_iface in used.get(link.a.device, set()):
            plan_out.incompatible.append(
                f"link {link.id} {link.a.device}:{a_iface} - already_used: "
                "interface is reserved by another link"
            )
        if b_iface in used.get(link.b.device, set()):
            plan_out.incompatible.append(
                f"link {link.id} {link.b.device}:{b_iface} - already_used: "
                "interface is reserved by another link"
            )

        # Refuse before anything is created: PT cannot carry this link.
        #
        # Measured live on PT 9.0.1 (2026-10-17), six ways, on a spare port
        # pair so the built topology was never disturbed:
        #   - a 5505's Ethernet ports are switchports. `switchport access vlan
        #     1` and `vlan 2`, applied before and after the vlan was created,
        #     never raised the peer's line protocol.
        #   - `ip address` and `no switchport` on the port are answered
        #     `true` and land nowhere; only the Vlan SVIs take addresses.
        #   - a straight-through cable is refused outright by PT.
        #   - a crossover - the only media PT accepts here - stays `up down`
        #     and carries no ping (`pc13 -> 10.30.50.50` blocked).
        #   - the same switchport, talking to a 2960, carries traffic normally
        #     (`pc13 -> 10.30.50.1` reachable on that canvas), and a 5506-X's
        #     routed ports link to routers normally (`fwhq <-> risp`, `up up`).
        #
        # So both cable choices are already proven unusable for a router peer,
        # and no third option exists. The switch and pc peers are NOT refused:
        # their straight-through pairing was measured working. This names the
        # one shape that cannot be built rather than guessing about the rest.
        a_is_switched_asa = (
            a_model.category == "firewall" and _pt_type_of(a_model) == "5505"
            and a_iface is not None and a_iface.startswith("Ethernet")
        )
        b_is_switched_asa = (
            b_model.category == "firewall" and _pt_type_of(b_model) == "5505"
            and b_iface is not None and b_iface.startswith("Ethernet")
        )
        if a_is_switched_asa or b_is_switched_asa:
            if a_model.category == "router" or b_model.category == "router":
                dev_id, iface = (
                    (link.a.device, a_iface) if a_is_switched_asa
                    else (link.b.device, b_iface)
                )
                peer = link.b.device if dev_id == link.a.device else link.a.device
                plan_out.incompatible.append(
                    f"link {link.id} {dev_id}:{iface} - unbuildable_link: "
                    f"PT's ASA 5505 carries no link to {peer}, whatever the "
                    "cable (measured 2026-10-17, PT 9.0.1: the straight-"
                    "through is refused outright, the crossover - the only "
                    "media PT accepts - never raises line protocol and "
                    "carries no ping, while the same switchport reaches a "
                    "switch normally). Terminate this end on a switch, or "
                    "use a firewall whose ports are routed (5506-X)."
                )

        # Validate port exists on the model (onboard or via the module it needs)
        a_valid = _port_exists(a_model, a_iface) or (a_module is not None)
        b_valid = _port_exists(b_model, b_iface) or (b_module is not None)

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
                f"link {link.id} {link.a.device}:{a_iface} - {a_constraint.value}: {a_res.reason}"
            )
        if b_constraint != PortConstraint.COMPATIBLE:
            plan_out.incompatible.append(
                f"link {link.id} {link.b.device}:{b_iface} - {b_constraint.value}: {b_res.reason}"
            )

        plan_out.add_resolution(f"{link.a.device}:{a_iface}", a_res)
        plan_out.add_resolution(f"{link.b.device}:{b_iface}", b_res)
        plan_out.link_endpoints[link.id] = (
            link.a.device, a_iface, link.b.device, b_iface
        )

        # Track module requirements (one per device+slot; deduped by the
        # executor so a device needing two serial ports installs HWIC-2T once)
        if a_module:
            slot = _slot_str_for_module(a_model, a_module)
            plan_out.module_requirements.append(ModuleRequirement(
                device=link.a.device,
                pt_type=a_model.pt_type,
                module_name=a_module.name,
                slot=slot,
                ports_added=tuple(ports_for_slot(a_module, slot)),
                category=a_module.category,
            ))
        if b_module:
            slot = _slot_str_for_module(b_model, b_module)
            plan_out.module_requirements.append(ModuleRequirement(
                device=link.b.device,
                pt_type=b_model.pt_type,
                module_name=b_module.name,
                slot=slot,
                ports_added=tuple(ports_for_slot(b_module, slot)),
                category=b_module.category,
            ))

        # Mark used
        used.setdefault(link.a.device, set()).add(a_iface)
        used.setdefault(link.b.device, set()).add(b_iface)

    plan_out.used_ports = used
    return plan_out


def _exhaustion_hint(model: DeviceModel | None) -> str:
    """Extra guidance when a device has run out of usable ports.

    A home gateway is the case that matters: it has four LAN ports and a WAN
    port, and the solver takes ports in catalog order, so a client beyond the
    fourth would silently land on the WAN. Say that here, at the point the
    failure is reported, instead of leaving a bare "no free port".
    """
    if model is not None and model.category == "home_gateway":
        return (
            "; a home gateway has four LAN ports and they are all taken — "
            "put the extra device behind a switch instead of its WAN port"
        )
    return ""


def _model_label(spec, dev_id: str, model: DeviceModel | None) -> str:
    """The model name to show in an error: the spec's own hint if it gave one.

    The author wrote "Solar Panel"; echoing the catalog's canonical spelling
    back at them would be less recognisable than what they typed.
    """
    if spec is not None:
        device = spec.device_by_id(dev_id) if hasattr(spec, "device_by_id") else None
        hint = (getattr(device, "model_hint", "") or "").strip()
        if hint:
            return hint
    return model.pt_type if model is not None else dev_id


# Categories that are plainly a LAN client. A gateway port facing one of these
# is a LAN port; anything else is the uplink (#31).
_HOST_CATEGORIES = frozenset({
    "pc", "laptop", "server", "printer", "iot", "wireless_end_device",
    "analog_phone", "ip_phone", "accesspoint",
})


def _wants_wan_port(model: DeviceModel | None, peer_model: DeviceModel | None) -> bool:
    """True when this endpoint is the WAN side of a device that has an
    `Internet` port.

    Without this the solver took the first copper port it liked, so a
    Linksys uplink landed on `Ethernet 1` - the gateway's LAN side, DHCP
    server and all - was bridged into the customer VLAN and hosts were
    handed 192.168.0.x (#31). Hints still win: this only decides which port
    an unhinted endpoint may pick.
    """
    if model is None:
        return False
    if not any((p.full_name or "").lower() == "internet" for p in model.ports):
        return False
    peer_category = peer_model.category if peer_model is not None else ""
    return peer_category not in _HOST_CATEGORIES


def _resolve_endpoint(
    device_id: str,
    model: DeviceModel | None,
    role: DeviceRole,
    hint: str | None,
    media: str,
    used: dict[str, set[str]],
    *,
    wan_side: bool = False,
) -> tuple[str | None, ModuleSpec | None]:
    """Pick the interface for one link endpoint.

    Returns (interface, module_required). A module is only returned when the
    chosen interface does not exist onboard and comes from an installable
    module (serial/WAN/fiber/wireless expansion).

    Selection order:
      1. an explicit interface_hint (validated to exist onboard or via module);
      2. the first UNUSED onboard port whose media family matches;
      3. the first port of an installable module matching the media family.

    Exhaustion returns (None, None) so the caller reports a real resource
    error instead of silently double-booking an occupied port.
    """
    if model is None:
        return None, None
    device_used = used.get(device_id, set())

    if hint:
        resolved = _canonical_iface(model, hint)
        if resolved is not None:
            return resolved, _module_for_interface(model, resolved)
        # Hint names a port no module can provide either: let the caller fail
        # loudly on the (validated) hint instead of silently rerouting.
        return hint, _module_for_interface(model, hint)

    # 2. onboard port matching the media family
    internet = next(
        (p for p in model.ports if (p.full_name or "").lower() == "internet"),
        None,
    )
    if internet is not None and wan_side and internet.full_name not in device_used:
        # #31: an unhinted uplink takes the gateway's WAN port first, before
        # the LAN bank can claim it.
        return internet.full_name, None

    for port in model.ports:
        if port.full_name in device_used:
            continue
        # A switched virtual interface (an ASA's Vlan1/Vlan2/Management1/1) is
        # where a routed address is configured, not something a cable plugs
        # into. Offering one here hands addLink an endpoint PT cannot cable,
        # which comes back as a bare "false" that reads like a bad cable.
        if port.speed is PortSpeed.SVI:
            continue
        if internet is not None and not wan_side and port.full_name == internet.full_name:
            # #31: a LAN client never lands on the gateway's WAN port.
            continue
        if _port_supports_media(port, media, model):
            return port.full_name, None

    # 3. installable module providing a matching, still-free port
    module = _module_for_media(model, media)
    if module is not None:
        module_ports = _module_port_names(model, module)
        consumed = used.get(f"__module__{device_id}", set())
        for added in module_ports:
            if added in device_used or added in consumed:
                continue
            # Installing the module consumes ALL of its ports, not just the one
            # this link picks: otherwise the next link on the same device would
            # re-resolve the same slot and collide — PT numbers a module's
            # ports once, at install time.
            used.setdefault(f"__module__{device_id}", set()).update(module_ports)
            return added, module

    return None, None


def _canonical_iface(model: DeviceModel, iface: str) -> str | None:
    """Resolve an interface name to its canonical onboard form.

    Accepts full names (GigabitEthernet0/0) and common abbreviations
    (Fa0/1, Gi0/1, Se0/0/0), matching against the model's real ports.
    """
    if not model:
        return None
    names = [p.full_name for p in model.ports]
    if iface in names:
        return iface
    # Abbreviation -> prefix of the full technology name.
    prefix = _ABBREV_PREFIXES.get(iface[:2])
    if not prefix:
        return None
    # The part after the 2-char abbreviation (e.g., "0/0" from "Gi0/0").
    rest = iface[2:]
    # Try exact match against port names starting with the prefix.
    for name in names:
        if name.startswith(prefix) and name[len(prefix):] == rest:
            return name
    # Try case-insensitive match (handles separator differences).
    for name in names:
        if name.lower().startswith(prefix.lower()) and name[len(prefix):].replace(" ", "") == rest.replace(" ", ""):
            return name
    return None


_ABBREV_PREFIXES: dict[str, str] = {
    "Fa": "FastEthernet",
    "Gi": "GigabitEthernet",
    "Et": "Ethernet",
    "Se": "Serial",
    "Co": "Console",
    "Fi": "Fiber",
}


def _media_family(media: str) -> str:
    """Group a media/cable name into a port-family bucket."""
    m = (media or "auto").lower()
    if m in ("serial",):
        return "serial"
    if m in ("fiber",):
        return "fiber"
    if m in ("coaxial", "coax"):
        return "coaxial"
    if m in ("wireless",):
        return "wireless"
    if m in ("console",):
        return "console"
    if m in ("phone", "modem"):
        # A DSL modem's line port is a phone line, and PT only accepts a phone
        # cable there (measured in _probe_wan2: the same port refuses coax).
        return "phone"
    return "ethernet"


def _port_family(port, model=None) -> str:
    """The media family a physical port belongs to."""
    from ..pt.enums import PortSpeed
    speed = port.speed
    speed_name = speed.value if hasattr(speed, "value") else str(speed)
    if speed_name == PortSpeed.SERIAL.value:
        return "serial"
    if speed_name == PortSpeed.FIBER.value or speed_name == "Fiber":
        return "fiber"
    if speed_name == PortSpeed.COAXIAL.value:
        return "coaxial"
    if speed_name == PortSpeed.MODEM.value:
        # The line port of a DSL modem: a phone line, not copper.
        return "phone"
    if speed_name == PortSpeed.MODEM_PORT.value:
        # #30: MODEM_PORT covers two very different things. On a DSL modem
        # "Port 0" is a phone line, but a Cable-Modem-PT's provider side is
        # coax (measured live: coaxial to the cloud's Coaxial7 works, a phone
        # cable does not). On a gateway the same slot is `Internet` - ordinary
        # RJ45 copper.
        model_name = getattr(model, "pt_type", "") or ""
        name = (getattr(port, "full_name", "") or "").strip().lower()
        if name == "internet" or name.startswith(("ethernet", "gigabitethernet")):
            return "ethernet"
        if name == "port 0" and model_name == "Cable-Modem-PT":
            return "coaxial"
        return "phone" if port.slot == "0" else "ethernet"
    if speed_name == PortSpeed.WIRELESS.value:
        return "wireless"
    if speed_name == PortSpeed.CONSOLE.value:
        return "console"
    return "ethernet"


def _port_supports_media(port, media: str, model=None) -> bool:
    family = _port_family(port, model)
    want = _media_family(media)
    if want == "ethernet":
        return family == "ethernet" or media == "auto"
    return family == want


def _module_for_media(model: DeviceModel, media: str) -> ModuleSpec | None:
    """A module installable on this device that provides the media family.

    Delegates to the catalog's single expansion lookup so the planner and the
    autopilot solver can't drift on which module fits which chassis. Ethernet
    is expandable too (HWIC-1GE-SFP on a 2911): without that, a router was
    capped at its onboard ethernet count no matter how many slots were free
    (S-1).
    """
    from ..pt.enums import PortSpeed
    want = _media_family(media)
    speed = _FAMILY_TO_SPEED.get(want)
    if speed is None:
        return None
    return expansion_module_for_speed(model, speed)


# Media family -> the PortSpeed a port of that family carries. Ethernet maps to
# GigabitEthernet: that is what an expansion module adds on an ISR G2 chassis.
_FAMILY_TO_SPEED: dict[str, str] = {
    "ethernet": PortSpeed.GIGABIT_ETHERNET,
    "serial": PortSpeed.SERIAL,
    "fiber": PortSpeed.FIBER,
    "coaxial": PortSpeed.COAXIAL,
    "wireless": PortSpeed.WIRELESS,
    "console": PortSpeed.CONSOLE,
}


def _module_category_matches(model: DeviceModel, module: ModuleSpec) -> bool:
    """Only offer modules whose slot family fits the device category.

    Thin wrapper over the catalog rule — kept so the solver's call sites read
    clearly while the decision itself lives in one place.
    """
    return module_category_fits(model.category, module.category or "")


def _speed_of_port_name(name: str) -> str:
    """Infer the speed name of a port from its name (for module ports)."""
    n = name or ""
    if n.startswith("Serial"):
        return "Serial"
    if n.startswith("Fiber"):
        return "Fiber"
    if n.startswith("Coaxial"):
        return "Coaxial"
    if n.startswith("Modem"):
        return "Modem"
    if n.startswith("GigabitEthernet"):
        return "GigabitEthernet"
    if n.startswith("FastEthernet"):
        return "FastEthernet"
    if n.startswith("Ethernet"):
        return "Ethernet"
    return "Ethernet"


def _module_port_names(model: DeviceModel, module: ModuleSpec) -> list[str]:
    """The concrete port names a module exposes on this device."""
    return ports_for_slot(module, _slot_str_for_module(model, module))


def _slot_str_for_module(model: DeviceModel, module: ModuleSpec) -> str:
    """First valid slot for a module on the device (module-port based).

    Slot numbering is chassis-specific: ISR routers number HWIC slots 0/0..0/3,
    but PT's generic Router-PT numbers them from 2, so a serial HWIC yields
    Serial2/0 there. The base comes from the device model, not the module
    category, so both are correct.
    """
    cat = module.category or ""
    base = getattr(model, "module_slot_base", "0/0")
    if cat == "router_hwic":
        return base if base else "0/0"
    if cat == "router_nim":
        return "0/1"
    if cat in ("router_nm",):
        return "1"
    return "0"


def _port_exists(model: DeviceModel, iface: str) -> bool:
    if not model or not iface:
        return False
    return _canonical_iface(model, iface) is not None


def _port_speed(model: DeviceModel, iface: str) -> PortSpec | None:
    canonical = _canonical_iface(model, iface) if model else None
    if canonical is None:
        canonical = iface
    for p in model.ports:
        if p.full_name == canonical:
            return p
    return None


def _slot_for(model: DeviceModel, iface: str) -> str | None:
    port = _port_speed(model, iface)
    return port.slot if port and port.slot else None


def _slot_str(model: DeviceModel, iface: str) -> str:
    slot = _slot_for(model, iface)
    return slot if slot else "0"


def _module_for_interface(
    model: DeviceModel, iface: str
) -> ModuleSpec | None:
    """The module a NON-onboard interface would require, or None.

    Only meaningful for expansion media (serial/fiber/wireless): onboard
    Ethernet/Gigabit ports exist already and never need a module. The old
    rule demanded HWIC-1GE-SFP for the 2911's onboard GE0/0, producing a
    bogus module requirement and a non-existent port name on every link.
    """
    if not model or not iface:
        return None
    family = _media_family(_speed_of_port_name(iface))
    if family == "ethernet":
        return None
    if _canonical_iface(model, iface) is not None:
        return None  # already onboard
    return _module_for_media(model, _speed_of_port_name(iface))


def _media_compatible(model: DeviceModel, iface: str, media: str) -> bool:
    """Check if a media type is compatible with a given port."""
    if not model or not iface:
        return False
    port = _port_speed(model, iface)
    if port:
        return _port_supports_media(port, media, model)
    # Port comes from a module: infer its family from the name.
    return _media_family(_speed_of_port_name(iface)) == _media_family(media)


def _pt_type_of(model) -> str:
    return getattr(model, "pt_type", "") or ""


def _infer_media(category_a: str, category_b: str, model_a: str = "", model_b: str = "") -> str:
    """Infer the cable media from the two devices (for media='auto').

    CABLE_RULES is keyed by category, not by role: ASAs are role=router but
    category=firewall, and the firewall rules differ (firewall↔pc is straight
    while router↔pc is cross — live net_build e2e on a 5505: cross blocks
    both flows, straight passes both).

    WAN gear needs the model, not just the category: PT only accepts a phone
    cable between a Cloud-PT and a DSL modem, and coax between a Cloud-PT and
    a cable modem, although both modems share the "modem" category (measured
    live in _probe_wan2). The per-model rules are consulted first and the
    category table remains the default.
    """
    if model_a and model_b:
        from ..pt.catalog import infer_cable_for_models

        return infer_cable_for_models(model_a, model_b)
    return infer_cable(category_a, category_b)


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
