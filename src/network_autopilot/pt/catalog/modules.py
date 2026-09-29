"""Expansion-module catalog — newly authored.

Routers and some hosts accept hardware modules that add ports (HWIC/NM/NM
families). The port solver needs to answer:

  1. Which modules exist and which ports would each one add?
  2. Which of those modules are physically compatible with a given chassis?
  3. Once installed in a specific slot, what are the resulting port names?

Port naming follows PT's slot rewriting: a module's ``ports_added`` are
written relative to its *first* slot, and installing into a chassis slot of
the form ``chassis/subslot`` rewrites the middle component. For example
HWIC-2T defines ``Serial0/0/0`` and ``Serial0/0/1``; installed into slot
``0/1`` it yields ``Serial0/1/0`` and ``Serial0/1/1``. A slot without a
``/`` (NM-style ``"1"``) leaves the names untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..enums import PortSpeed
from . import aliases
from . import devices
from .devices import DeviceModel


@dataclass(frozen=True)
class ModuleSpec:
    """A hardware module PT can install into a chassis slot."""
    name: str
    # PT's internal module class id (used by addModule).
    module_type: int
    # Which host families this module fits: router_nm, router_hwic, switch_nm,
    # pt_cloud_nm, pt_host_nm, pt_laptop_nm, pt_modem_nm, ...
    category: str
    # Port names as they appear when installed in the module's first slot.
    ports_added: tuple[str, ...]
    description: str = ""
    # pt_type strings of chassis this module is known to fit.
    compatible_with: tuple[str, ...] = ()


_ALL_MODULES: dict[str, ModuleSpec] = {}


def _register(spec: ModuleSpec) -> ModuleSpec:
    _ALL_MODULES[spec.name.upper()] = spec
    return spec


def _ports(family: PortSpeed, count: int, base: str = "0/0/0") -> tuple[str, ...]:
    """Generate ``count`` consecutive port names of a family.

    HWIC-style names are three-level (``Serial0/0/0``); the middle component
    is the one PT rewrites on install, so the base here uses 0 for it.
    """
    parts = base.split("/")
    names = []
    for i in range(count):
        parts[-1] = str(i)
        names.append(f"{family.value}{'/'.join(parts)}")
    return tuple(names)


# -- Serial modules ----------------------------------------------------------

_register(ModuleSpec(
    name="HWIC-2T", module_type=2, category="router_hwic",
    ports_added=_ports(PortSpeed.SERIAL, 2),
    description="2-port high-speed serial (HWIC)",
    compatible_with=("2911", "2901", "1941", "Router-PT"),
))
_register(ModuleSpec(
    name="HWIC-4T", module_type=3, category="router_hwic",
    ports_added=_ports(PortSpeed.SERIAL, 4),
    description="4-port high-speed serial (HWIC)",
    compatible_with=("2911", "Router-PT"),
))
_register(ModuleSpec(
    name="HWIC-8A", module_type=4, category="router_hwic",
    ports_added=_ports(PortSpeed.SERIAL, 8),
    description="8-port async serial (HWIC)",
    compatible_with=("2911", "Router-PT"),
))

# -- Ethernet modules --------------------------------------------------------

_register(ModuleSpec(
    name="HWIC-1GE-SFP", module_type=5, category="router_hwic",
    ports_added=(f"{PortSpeed.GIGABIT_ETHERNET.value}0/0/0",),
    description="1-port Gigabit SFP (HWIC)",
    compatible_with=("2911", "2901", "1941"),
))
_register(ModuleSpec(
    name="HWIC-4ESW", module_type=6, category="router_hwic",
    ports_added=tuple(
        f"{PortSpeed.FAST_ETHERNET.value}0/0/{i}" for i in range(4)),
    description="4-port Ethernet switch (HWIC)",
    compatible_with=("2911", "2901", "1941"),
))
_register(ModuleSpec(
    name="NME-16ES-2P", module_type=7, category="router_nm",
    ports_added=tuple(
        f"{PortSpeed.FAST_ETHERNET.value}1/{i}" for i in range(16)),
    description="16-port Ethernet switch (NME)",
    compatible_with=("2911"),
))

# -- Wireless modules --------------------------------------------------------

_register(ModuleSpec(
    name="HWIC-AP-AG-B", module_type=8, category="router_hwic",
    ports_added=(f"{PortSpeed.WIRELESS.value}0/0/0",),
    description="802.11a/b/g access point (HWIC)",
    compatible_with=("2911", "2901", "1941"),
))

# -- PT-family modules (hosts, cloud, laptop) --------------------------------

_register(ModuleSpec(
    name="PT-LAPTOP-NM-1W", module_type=11, category="pt_laptop_nm",
    ports_added=(f"{PortSpeed.WIRELESS.value}0",),
    description="Laptop wireless NIC",
    compatible_with=("Laptop-PT",),
))
_register(ModuleSpec(
    name="PT-LAPTOP-NM-1W-A", module_type=12, category="pt_laptop_nm",
    ports_added=(f"{PortSpeed.WIRELESS.value}0",),
    description="Laptop wireless NIC (802.11a)",
    compatible_with=("Laptop-PT",),
))
_register(ModuleSpec(
    name="PT-HOST-NM-1GE", module_type=13, category="pt_host_nm",
    ports_added=(f"{PortSpeed.GIGABIT_ETHERNET.value}0",),
    description="Host Gigabit NIC",
    compatible_with=("PC-PT", "Server-PT"),
))
_register(ModuleSpec(
    name="PT-HOST-NM-1W", module_type=14, category="pt_host_nm",
    ports_added=(f"{PortSpeed.WIRELESS.value}0",),
    description="Host wireless NIC",
    compatible_with=("PC-PT", "Server-PT"),
))
_register(ModuleSpec(
    name="PT-PT-NM-1S", module_type=15, category="pt_cloud_nm",
    ports_added=(f"{PortSpeed.SERIAL.value}0",),
    description="Cloud serial port module",
    compatible_with=("Cloud-PT",),
))
_register(ModuleSpec(
    name="PT-PT-NM-1CE", module_type=16, category="pt_cloud_nm",
    ports_added=(f"{PortSpeed.ETHERNET.value}0",),
    description="Cloud Ethernet port module",
    compatible_with=("Cloud-PT",),
))

# Serial modules used when a router needs a serial port it does not have
# onboard. Keyed by router pt_type -> module name.
_SERIAL_MODULES: dict[str, str] = {
    "2911": "HWIC-2T",
    "2901": "HWIC-2T",
    "1941": "HWIC-2T",
}

# Chassis category -> module categories that physically fit.
_SLOT_FAMILIES: dict[str, frozenset[str]] = {
    "router": frozenset({"router_nm", "router_hwic", "router_wic"}),
    "switch": frozenset({"switch_nm"}),
    "cloud": frozenset({"pt_cloud_nm"}),
    "modem": frozenset({"pt_modem_nm"}),
    "pc": frozenset({"pt_host_nm"}),
    "server": frozenset({"pt_host_nm"}),
    "laptop": frozenset({"pt_laptop_nm"}),
    "end_device": frozenset({"pt_host_nm", "pt_laptop_nm"}),
}


def resolve_module(name: str) -> ModuleSpec | None:
    """Look up a module by name. Returns ``None`` when unknown."""
    if not name:
        return None
    key = name.strip().upper()
    return _ALL_MODULES.get(key)


def all_modules() -> dict[str, ModuleSpec]:
    return dict(_ALL_MODULES)


def get_serial_module(router_model: str) -> ModuleSpec | None:
    """The serial expansion module to install in a router, if any.

    A specific chassis may name its own module; any other router with slots
    takes the generic 2-port HWIC. Returns ``None`` for unknown models and for
    chassis that cannot accept a module.
    """
    canonical = aliases.canonical_model_name(router_model)
    name = _SERIAL_MODULES.get(canonical)
    if name:
        return resolve_module(name)
    model = devices.resolve_model(canonical)
    if model is None or not model.module_slots:
        return None
    return resolve_module("HWIC-2T")


# Rewrites the middle component of a slotted port name: "Serial0/0/0" with
# slot "0/1" -> "Serial0/1/0". A slot without a "/" is NM-style and leaves the
# name untouched.
_SLOTTED_PORT = re.compile(r"^([A-Za-z]+)(\d+)/(\d+)(/.*)?$")


def ports_for_slot(spec: ModuleSpec, slot: str) -> list[str]:
    """Port names a module yields once installed in ``slot``.

    ``spec.ports_added`` are written for the module's own first slot. Two slot
    shapes produce two rewritings, both observed in PT:

    - ``chassis/subslot`` (HWIC on an ISR, e.g. ``0/1``): the middle numeric
      component is rewritten — ``Serial0/0/0`` becomes ``Serial0/1/0``.
    - a bare number with a three-level module port name (PT's generic
      Router-PT, whose module slots are numbered 2..3): the name collapses to
      two levels keyed by that slot — ``Serial0/0/0`` becomes ``Serial2/0``.

    A bare number with a two-level module port name (NM-style modules such as
    NME) leaves the names untouched, because PT numbers those ports per module.
    """
    if not slot:
        return list(spec.ports_added)
    if "/" in slot:
        slot_middle = slot.split("/")[1]
        out: list[str] = []
        for port in spec.ports_added:
            match = _SLOTTED_PORT.match(port)
            if match:
                speed, head, _middle, tail = match.groups()
                out.append(f"{speed}{head}/{slot_middle}{tail or ''}")
            else:
                out.append(port)
        return out
    # Bare slot number. Only rewrite when the module's own port names are
    # three-level (HWIC-style); NM-style two-level names are untouched.
    out = []
    for port in spec.ports_added:
        match = _SLOTTED_PORT.match(port)
        if match and match.group(4):
            speed, _head, _middle, tail = match.groups()
            trailing = tail.lstrip("/")
            out.append(
                f"{speed}{slot}/{trailing}" if trailing else f"{speed}{slot}"
            )
        else:
            out.append(port)
    return out


def speed_of_port_name(name: str) -> str:
    """Speed family of an IOS port name ("GigabitEthernet0/0" -> the enum's
    value). Unknown prefixes fall back to Ethernet."""
    for speed in PortSpeed:
        if name.startswith(speed.value):
            return speed.value
    return PortSpeed.ETHERNET.value


def module_category_fits(model_category: str, module_category: str) -> bool:
    """Whether a module family can be installed in a chassis category."""
    if not model_category or not module_category:
        return False
    return module_category in _SLOT_FAMILIES.get(model_category, frozenset())


def expansion_module_for_speed(model: "DeviceModel | None", speed: PortSpeed | str) -> ModuleSpec | None:
    """Pick a module that adds a port of ``speed`` to ``model``.

    Returns ``None`` when the chassis is unknown, has no slots, or nothing
    compatible exists. Used by the port solver when a device runs out of
    onboard ports of the required speed.
    """
    if model is None or not model.module_slots:
        return None
    wanted = speed.value if isinstance(speed, PortSpeed) else str(speed)
    for spec in _ALL_MODULES.values():
        if model.pt_type not in spec.compatible_with:
            continue
        if not module_category_fits(model.category, spec.category):
            continue
        if any(speed_of_port_name(p) == wanted for p in spec.ports_added):
            return spec
    return None
