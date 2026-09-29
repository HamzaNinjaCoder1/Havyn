"""Device catalog for the Packet Tracer integration — newly authored.

The engine needs to answer two questions about the hardware PT exposes:

  1. Given a model string PT reports (``getModel()``) or a role name the spec
     uses, what is it and which ports does it have?
  2. Given a model, what *category* does it belong to (router, switch, ...)?

Both answers come from a table keyed by PT's model names. The entries here
were written from the documented device behaviour of Packet Tracer's device
palette: which models exist per family, which fixed interfaces each chassis
presents, and how PT groups models into categories. Port names are IOS
interface names because that is how PT names ports in both its UI and its
script API.

Design notes that matter to callers:

- ``resolve_model`` returns ``None`` (never raises) for an unknown model so
  callers can treat a miss as "not found" and pick a default.
- ``category_of_model`` returns ``""`` (not ``None``) for an unknown model.
  Several call sites use the result as a truthiness gate and fall back to a
  default category, so ``None`` would break them.
- ``PortSpec.speed`` holds a :class:`~network_autopilot.pt.enums.PortSpeed`
  member. Because that enum subclasses ``str``, both the enum comparison
  ``port.speed == PortSpeed.WIRELESS`` and the string comparison
  ``port.speed.value`` paths keep working.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..enums import PortSpeed
from . import aliases


@dataclass(frozen=True)
class PortSpec:
    """One fixed interface of a device chassis.

    ``full_name`` is the IOS name PT uses everywhere (UI, ``getPortAt(i).
    getName()``, cable rules) and is derived from speed + slot. A device whose
    port name does not follow the ``Speed<slot>`` pattern (PT writes a modem's
    ports as "Port 0") supplies ``full_name`` explicitly.
    """
    speed: PortSpeed
    slot: str
    full_name: str = ""

    def __post_init__(self) -> None:
        if not self.full_name:
            object.__setattr__(self, "full_name", f"{self.speed.value}{self.slot}")


@dataclass(frozen=True)
class DeviceModel:
    """A hardware model as PT presents it."""
    pt_type: str
    category: str
    ports: tuple[PortSpec, ...] = ()
    display_name: str = ""
    # Slot ids a module can be installed into (e.g. ("0/0", "0/1") for HWIC).
    module_slots: tuple[str, ...] = ()
    # Base slot a HWIC/NM module occupies on this chassis. PT numbers the
    # generic Router-PT's module slots 2..3 (so a serial HWIC yields
    # Serial2/0), while ISR chassis number them 0/0..0/3. Defaults to the
    # ISR-style 0/0.
    module_slot_base: str = "0/0"


def _fe(slot: str) -> PortSpec:
    return PortSpec(PortSpeed.FAST_ETHERNET, slot)


def _ge(slot: str) -> PortSpec:
    return PortSpec(PortSpeed.GIGABIT_ETHERNET, slot)


def _eth(slot: str) -> PortSpec:
    return PortSpec(PortSpeed.ETHERNET, slot)


def _ser(slot: str) -> PortSpec:
    return PortSpec(PortSpeed.SERIAL, slot)


def _coax(slot: str) -> PortSpec:
    return PortSpec(PortSpeed.COAXIAL, slot)


def _modem(slot: str) -> PortSpec:
    return PortSpec(PortSpeed.MODEM, slot)


# ---------------------------------------------------------------------------
# The table itself. Keyed by PT's model string (what getModel() returns).
# ---------------------------------------------------------------------------

_ALL_MODELS: dict[str, DeviceModel] = {}


def _register(model: DeviceModel) -> DeviceModel:
    _ALL_MODELS[model.pt_type] = model
    return model


# -- Routers -----------------------------------------------------------------

# Onboard ports, verified live: a 2911 answers getPortCount()=4 and enumerates
# Vlan1 plus the three copper ports below (plus four HWIC slots). Only the
# copper ports are listed: PT's IOS parser ACCEPTS an unknown interface name
# and still reports success, so a phantom console port here would let a build
# "succeed" while carrying no traffic. The Vlan1 SVI is created by
# configuration, not by the chassis, so it is not an onboard port.
# 2921/2951 are deliberately absent: PT 9.0.1's lw.addDevice answers "" for
# both (measured live), so no build can place them. 4321/4331 place as
# "ISR4321"/"ISR4331" (see pt_model_string) with triple-index GigE ports,
# measured live after a publish settle.
for _pt_type, _onboard in (
    ("2911", (_ge("0/0"), _ge("0/1"), _ge("0/2"))),
    ("2901", (_ge("0/0"), _ge("0/1"))),
    ("1941", (_ge("0/0"), _ge("0/1"))),
    ("4321", (_ge("0/0/0"), _ge("0/0/1"))),
    ("4331", (_ge("0/0/0"), _ge("0/0/1"), _ge("0/0/2"))),
):
    _register(DeviceModel(
        pt_type=_pt_type,
        category="router",
        ports=_onboard,
        module_slots=tuple(f"0/{i}" for i in range(4)) + ("1", "2"),
    ))

# ASA appliances are registered below in the firewall section, where their
# port list and category are described as PT presents them.

# -- Switches ----------------------------------------------------------------

# Enumerated live (S1.12 full probe, PT 9.0.1): 24xFE on all four; the two
# uplink GEs exist on 2960-24TT/2950T-24/3560-24PS but NOT on 2950-24; NONE
# of them reports a Console0 on the port surface, and a console cable to it
# is refused live - so no Console0 anywhere. There is NO FastEthernet0/0:
# naming one here would have the port solver hand a link an interface PT
# does not have, and addLink refuses it with a bare 'false' that looks
# like a bad cable.
for _pt_type in ("2960-24TT", "2950T-24", "3560-24PS"):
    _register(DeviceModel(
        pt_type=_pt_type,
        category="switch",
        ports=tuple(_fe(f"0/{i}") for i in range(1, 25))
        + (_ge("0/1"), _ge("0/2")),
        module_slots=(),
    ))

_register(DeviceModel(
    pt_type="2950-24", category="switch",
    ports=tuple(_fe(f"0/{i}") for i in range(1, 25)),
    module_slots=(),
))

# Hub: a purely L1 device with six ports PT names "FastEthernet0".."FastEthernet5"
# (no slot suffix — a hub peer-check enumerates exactly these names, so a
# slashed variant would be a port PT cannot find).
_register(DeviceModel(
    pt_type="Hub-PT", category="hub",
    ports=tuple(
        PortSpec(PortSpeed.FAST_ETHERNET, str(i), f"FastEthernet{i}")
        for i in range(6)
    ),
    module_slots=(),
))

# -- End hosts ---------------------------------------------------------------

_register(DeviceModel(
    pt_type="PC-PT", category="pc",
    ports=(_fe("0"),),
    module_slots=("0",),
))
_register(DeviceModel(
    pt_type="Laptop-PT", category="laptop",
    ports=(_fe("0"),),
    module_slots=("0",),
))
_register(DeviceModel(
    pt_type="Server-PT", category="server",
    ports=(_fe("0"),),
    module_slots=("0", "1", "2", "3", "4", "5", "6"),
))
_register(DeviceModel(
    pt_type="Printer-PT", category="printer",
    ports=(_fe("0"),),
    module_slots=(),
))

# -- Wireless ----------------------------------------------------------------

def _ap_ports() -> tuple[PortSpec, ...]:
    """An AP's port list: "Port 0" is the wired uplink, "Port 1" is the radio.

    PT names an access point's ports positionally rather than after a speed
    family. "Port 0" is where a wired client cables in, and it is "Port 1"
    that the antenna attaches to. Enumerated live (S1.12): PT reports exactly
    these two - "Port 2".."Port 15" never exist, and handing one to addLink
    is refused.
    """
    return (
        PortSpec(PortSpeed.ETHERNET, "0", "Port 0"),
        PortSpec(PortSpeed.WIRELESS, "1", "Port 1"),
    )


_register(DeviceModel(
    pt_type="AccessPoint-PT", category="accesspoint",
    ports=_ap_ports(),
    module_slots=("0", "1", "2", "3", "4", "5", "6"),
))
# PT ships several AP chassis under AccessPoint-PT-<variant>; they behave the
# same for association purposes.
# AccessPoint-PT-AG is deliberately absent: PT 9.0.1 places its siblings but
# answers "" for -AG under every role tried (measured live, S1.12).
for _ap_type in ("AccessPoint-PT-AC", "AccessPoint-PT-A", "AccessPoint-PT-N"):
    _register(DeviceModel(
        pt_type=_ap_type, category="accesspoint",
        ports=_ap_ports(),
        module_slots=("0", "1", "2", "3", "4", "5", "6"),
    ))
# NOTE: there is deliberately no "WirelessRouter-PT" here. Enumerated live, PT
# refuses to add that model under any spelling tried ("Wireless-Router",
# "WRT300N", "WRT-300N", "WirelessRouter-PT"); its wireless-router model is
# Linksys-WRT300N, registered below. Declaring the phantom one let a spec ask
# for a device PT would not create: lwAddDevice returns the name it was given
# even when it adds nothing, so the build only failed at the read-back.
# Enumerated live (S1.12): a single GigabitEthernet0 (plus a Vlan1 the probe
# acknowledges as logical, never cableable).
_register(DeviceModel(
    pt_type="WLC-PT", category="wlc",
    ports=(_ge("0"),),
    module_slots=("0", "1", "2", "3", "4", "5", "6"),
))

# -- WAN / cloud / modem ------------------------------------------------------

# The cloud's own endpoints, as PT names them: four serial lines, two modem
# ports, one Ethernet and one coax. A cloud with no endpoints is a separate
# model — an "empty" cloud is how PT represents a bare WAN symbol.
_register(DeviceModel(
    pt_type="Cloud-PT", category="cloud",
    ports=(
        _ser("0"), _ser("1"), _ser("2"), _ser("3"),
        _modem("4"), _modem("5"),
        PortSpec(PortSpeed.ETHERNET, "6", "Ethernet6"),
        PortSpec(PortSpeed.COAXIAL, "7", "Coaxial7"),
    ),
    module_slots=("0", "1", "2", "3", "4", "5", "6"),
))
_register(DeviceModel(
    pt_type="Cloud-PT-Empty", category="cloud",
    ports=(),
    module_slots=(),
))
# A modem exposes exactly two endpoints: Port 0 is the provider line (a phone
# line) and Port 1 is the copper handoff to the router.
for _pt_type in ("DSL-Modem-PT", "Cable-Modem-PT"):
    _register(DeviceModel(
        pt_type=_pt_type, category="modem",
        ports=(
            PortSpec(PortSpeed.MODEM_PORT, "0", "Port 0"),
            PortSpec(PortSpeed.MODEM_PORT, "1", "Port 1"),
        ),
        module_slots=(),
    ))

# -- Phones ------------------------------------------------------------------

# IP-Phone-PT is deliberately absent: PT 9.0.1 refuses to place it (measured
# live, S1.12); the placeable phone is the 7960 below, which is also the
# "phone" alias default.
_register(DeviceModel(
    pt_type="Analog-Phone-PT", category="analog_phone",
    # Enumerated live (S1.12): a single "Port 0", not FastEthernet0.
    ports=(PortSpec(PortSpeed.MODEM_PORT, "0", "Port 0"),),
    module_slots=(),
))

# ASA 5505: a firewall chassis whose inside/outside ports are plain Ethernet
# switchports (Ethernet0/0, Ethernet0/1, ...), not GigabitEthernet. The port
# solver keys on that: a switchport, never a Vlan interface.
#
# Enumerated live: PT answers Vlan1, Vlan2 and Ethernet0/0..0/7. There is
# NO GigabitEthernet0/8 and NO console - the device object has no
# getCommandLine() at all, so a "Console0" here would only ever produce a
# link PT refuses. The two Vlan interfaces are the SVIs the routed config
# lives on, which is why the compiler joins each switchport to a vlan and
# then addresses the Vlan.
_register(DeviceModel(
    pt_type="5505", category="firewall",
    ports=(
        PortSpec(PortSpeed.SVI, "1", "Vlan1"),
        PortSpec(PortSpeed.SVI, "2", "Vlan2"),
    ) + tuple(_eth(f"0/{i}") for i in range(8)),
    module_slots=(),
))

# ASA 5506-X: Gigabit ports, first real data port GigabitEthernet1/1.
# Enumerated live: GigabitEthernet1/1..1/8 plus Management1/1. There is no
# GigabitEthernet1/9 and no console.
_register(DeviceModel(
    pt_type="5506-X", category="firewall",
    ports=(_ge("1/1"), _ge("1/2"), _ge("1/3"), _ge("1/4"), _ge("1/5"),
           _ge("1/6"), _ge("1/7"), _ge("1/8"),
           PortSpec(PortSpeed.SVI, "1", "Management1/1")),
    module_slots=(),
))

# -- Generic PT family chassis (the "-PT" placeholders of each device class) --

# These exist so a spec that names the family rather than a vendor chassis
# still resolves to a concrete category and a port list.
_register(DeviceModel(
    pt_type="Router-PT", category="router",
    # Enumerated live against a real device: PT's generic chassis answers with
    # FastEthernet0/0, FastEthernet1/0, FastEthernet4/0, FastEthernet5/0 and
    # two serial ports. It has NO GigabitEthernet ports — naming a phantom one
    # here would have the solver pick a port PT cannot cable, and the failure
    # only shows up as "PT reported 'false'" at link time.
    ports=(
        _fe("0/0"), _fe("1/0"), _fe("4/0"), _fe("5/0"),
        _ser("2/0"), _ser("3/0"),
    ),
    module_slots=tuple(f"0/{i}" for i in range(4)),
    module_slot_base="2",
))
_register(DeviceModel(
    pt_type="Switch-PT", category="switch",
    # Enumerated live: the generic chassis presents FastEthernet0/1 .. 5/1.
    # As with the 2960 there is no "0/0" and no Console0 (S1.12: PT neither
    # reports it nor accepts a console cable to it).
    ports=tuple(_fe(f"{i}/1") for i in range(0, 6)),
    module_slots=(),
))
# Enumerated live (S1.12): the bridge answers Ethernet0/1 + Ethernet1/1
# (plus a Vlan1 the probe acknowledges as logical), the repeater answers
# Ethernet0 + Ethernet1. The old FastEthernet guesses were never reported.
_register(DeviceModel(
    pt_type="Bridge-PT", category="bridge",
    ports=(_eth("0/1"), _eth("1/1")),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Repeater-PT", category="repeater",
    ports=(_eth("0"), _eth("1")),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="CoAxialSplitter-PT", category="splitter",
    ports=(_coax("0"), _coax("1"), _coax("2")),
    module_slots=(),
))
# Enumerated live (S1.12, PT 9.0.1 — created via the PT spellings in
# pt_model_string): the patch panel answers 24 Jacks + 24 PunchDowns (PT's
# "Copper Patch Panel", which also settles the roadmap's open type-code
# note: it places as a switch, type 1); the sniffer answers two Ethernets;
# the cell tower answers Coaxial0 (plus a "Cellular Server1" radio the probe
# acknowledges as logical); the central office answers six coax lines (plus
# Backbone/Cellular/Virtual-Access/Vlan1 the probe acknowledges); the
# controller answers two GigabitEthernets, not Ethernets. MCU-PT is
# deliberately absent: PT refuses to place it under any spelling tried.
_register(DeviceModel(
    pt_type="Copper-Patch-Panel", category="patch_panel",
    ports=tuple(
        PortSpec(PortSpeed.ETHERNET, str(i), f"Jack{i}") for i in range(1, 25)
    ) + tuple(
        PortSpec(PortSpeed.ETHERNET, str(i), f"PunchDown{i}")
        for i in range(1, 25)
    ),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Cell-Tower", category="cell_tower",
    ports=(PortSpec(PortSpeed.COAXIAL, "0", "Coaxial0"),),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Central-Office-Server", category="central_office",
    ports=tuple(
        PortSpec(PortSpeed.COAXIAL, f"0/{i}", f"Coaxial0/{i}")
        for i in range(6)
    ),
    module_slots=("0", "1", "2", "3", "4", "5", "6"),
))
_register(DeviceModel(
    pt_type="Sniffer-PT", category="sniffer",
    ports=(_eth("0"), _eth("1")),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="SBC-PT", category="sbc",
    ports=(),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Network-Controller", category="network_controller",
    ports=(_ge("0"), _ge("1")),
    module_slots=(),
))

# -- Meraki chassis: MX is a router, Meraki-Server is a server --------------

# Enumerated live (S1.12): the MX answers spaced-name Ethernets
# ("GigabitEthernet 3".."10", "Ethernet 11"/"12") plus Dialer/Internet/
# Virtual-Access/Wireless/Vlan1 the probe acknowledges as logical. The old
# GigabitEthernet0/0+0/1+Console0 were never reported.
_register(DeviceModel(
    pt_type="Meraki-MX65W", category="meraki",
    ports=tuple(
        PortSpec(PortSpeed.GIGABIT_ETHERNET, str(i), f"GigabitEthernet {i}")
        for i in range(3, 11)
    ) + tuple(
        PortSpec(PortSpeed.ETHERNET, str(i), f"Ethernet {i}")
        for i in (11, 12)
    ),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Meraki-Server", category="meraki",
    ports=(_fe("0"),),
    module_slots=(),
))

# -- IP phones ---------------------------------------------------------------

# 7960: enumerated live (S1.12), PT reports exactly "Switch" (the wall side)
# and "PC" (the passthrough) plus a Vlan1 the probe acknowledges as logical.
# The old FastEthernet0/1/2 names were never reported and made the solver
# hand T11-style phone links an interface PT refuses.
_register(DeviceModel(
    pt_type="7960", category="ip_phone",
    ports=(
        PortSpec(PortSpeed.FAST_ETHERNET, "0", "Switch"),
        PortSpec(PortSpeed.FAST_ETHERNET, "1", "PC"),
    ),
    module_slots=(),
))

# -- Smart-home / IoT endpoints (PT's "end device" family) -------------------

# Enumerated live (S1.12, PT 9.0.1): Tablet-PT and PDA-PT are not placeable
# under any spelling tried, so they are absent. The radio-only chassis carry
# no cableable port - their Wireless0/Bluetooth interfaces are acknowledged
# by the probe as logical, never diffed.
_register(DeviceModel(
    pt_type="TV-PT", category="end_device",
    ports=(PortSpec(PortSpeed.MODEM_PORT, "0", "Port 0"),),
    module_slots=("0",),
))
_register(DeviceModel(
    pt_type="WirelessEndDevice-PT", category="end_device",
    ports=(),
    module_slots=("0",),
))
_register(DeviceModel(
    pt_type="WiredEndDevice-PT", category="end_device",
    ports=(_fe("0"),),
    module_slots=("0",),
))
for _pt_type in ("TabletPC-PT", "SMARTPHONE-PT"):
    _register(DeviceModel(
        pt_type=_pt_type, category="end_device",
        ports=(),
        module_slots=("0",),
    ))

# PT's IoE family, measured live one by one (S1.12, PT 9.0.1): every one of
# these places via its PT spelling (see pt_model_string) and enumerates the
# ports below. "Smart LED", "Fire Monitor" and "Trip Sensor" expose real
# copper, so a wired smart-home build can use them; the rest are RF-only
# (Wireless0/Bluetooth, acknowledged by the probe as logical) or portless.
# Claiming FastEthernet0 for an RF-only thing made the port solver hand out
# a link PT refuses. Smart-Lock/Smart-Plug are deliberately absent: PT
# creates nothing observable for either spelling tried.
_IOT_PORTS: dict[str, tuple[PortSpec, ...]] = {
    "Thing": (),
    "Smart-LED": (_fe("0"),),
    "Fan": (PortSpec(PortSpeed.WIRELESS, "0"),),
    "Solar-Panel": (PortSpec(PortSpeed.WIRELESS, "0"),),
    "Fire-Monitor": (_fe("0"),),
    "Trip-Sensor": (_fe("0"),),
    "CO2-Sensor": (),
    "Humidity-Sensor": (),
    "Door-Sensor": (),
    "Motion-Sensor": (),
    "Garage-Door": (),
    "Web-Cam": (_ge("3"),),
}
for _iot_type, _iot_ports in _IOT_PORTS.items():
    _register(DeviceModel(
        pt_type=_iot_type, category="iot",
        ports=_iot_ports,
        module_slots=("0",),
    ))

# -- Home gateways: PT's wireless router family ------------------------------

# PT's smart-home gateways expose their LAN ports as "GigabitEthernet 1".."4"
# (with a space) and a WAN port; the LAN address itself lives on the Vlan1 SVI,
# which is what the gateway's IOS CLI configures.
for _gw_type in ("HomeRouter-PT-AC", "Linksys-WRT300N"):
    _register(DeviceModel(
        pt_type=_gw_type, category="home_gateway",
        ports=(
            # PT names the gateway's Internet-facing port "Internet"; the
            # compiler refuses to land a client there by accident.
            PortSpec(PortSpeed.MODEM_PORT, "0", "Internet"),
            PortSpec(PortSpeed.MODEM_PORT, "1", "GigabitEthernet 1"),
            PortSpec(PortSpeed.MODEM_PORT, "2", "GigabitEthernet 2"),
            PortSpec(PortSpeed.MODEM_PORT, "3", "GigabitEthernet 3"),
            PortSpec(PortSpeed.MODEM_PORT, "4", "GigabitEthernet 4"),
        ),
        module_slots=("0", "1", "2", "3", "4", "5", "6"),
    ))
# Linksys-WRT300N is PT's wireless-router model (device type 11) rather than a
# home gateway (type 28). Enumerated live, its four LAN ports are named
# "Ethernet 1".."4" - NOT "GigabitEthernet 1".."4" like the home gateways, and
# it also carries Vlan1, Virtual-Access1, Dialer1 and a Wireless radio.
_register(DeviceModel(
    pt_type="Linksys-WRT300N", category="wireless_router",
    ports=(
        # PT names the WAN-facing port "Internet"; the compiler refuses to land
        # a client there by accident.
        PortSpec(PortSpeed.MODEM_PORT, "0", "Internet"),
        PortSpec(PortSpeed.MODEM_PORT, "1", "Ethernet 1"),
        PortSpec(PortSpeed.MODEM_PORT, "2", "Ethernet 2"),
        PortSpec(PortSpeed.MODEM_PORT, "3", "Ethernet 3"),
        PortSpec(PortSpeed.MODEM_PORT, "4", "Ethernet 4"),
    ),
    module_slots=("0", "1", "2", "3", "4", "5", "6"),
))

# DLC100: PT's "digital light controller" — a server-class host.
# Enumerated live (S1.12): four spaced Ethernets plus an "Internet" WAN port
# (plus Virtual-Access1/Vlan1/Wireless the probe acknowledges as logical).
# The old FastEthernet0 was never reported.
_register(DeviceModel(
    pt_type="DLC100", category="server",
    ports=tuple(
        PortSpec(PortSpeed.ETHERNET, str(i), f"Ethernet {i}")
        for i in range(1, 5)
    ) + (PortSpec(PortSpeed.MODEM_PORT, "0", "Internet"),),
    module_slots=("0", "1", "2", "3", "4", "5", "6"),
))

# A generic model that maps a *role name* in a spec to a concrete default.
# The engine uses this when a spec says "router" without picking a chassis.
_ROLE_DEFAULTS: dict[str, str] = {
    "router": "2911",
    "switch": "2960-24TT",
    "pc": "PC-PT",
    "laptop": "Laptop-PT",
    "server": "Server-PT",
    "cloud": "Cloud-PT",
    "wan": "Cloud-PT",
    "accesspoint": "AccessPoint-PT",
    "access_point": "AccessPoint-PT",
    "wireless_router": "WirelessRouter-PT",
    "modem": "DSL-Modem-PT",
}


def resolve_model(name: str) -> DeviceModel | None:
    """Look up a device model by PT model string, alias, or role name.

    Returns ``None`` for anything unknown — callers use that to fall back to a
    default rather than crashing on an unexpected model string from PT.
    """
    if not name:
        return None
    key = aliases.canonical_model_name(name)
    match = _ALL_MODELS.get(key)
    if match is None:
        # PT's model strings carry case ("2960-24TT"); the table is keyed by
        # the exact string, so fall back to a case-insensitive probe before
        # treating the input as a role word.
        for candidate, model in _ALL_MODELS.items():
            if candidate.lower() == key:
                match = model
                break
    if match is not None:
        return match
    # A bare role name resolves to the family default chassis.
    default = _ROLE_DEFAULTS.get(key)
    if default is not None:
        return _ALL_MODELS[default]
    return None


def category_of_model(model_name: str) -> str:
    """Category of a model string; ``""`` when it is not recognised.

    The empty string is deliberate: call sites use the result as a truthiness
    gate and fall back to a default category, so ``None`` would break them.
    """
    model = resolve_model(model_name)
    return model.category if model else ""


def all_models() -> dict[str, DeviceModel]:
    """The full read-only model table (keyed by PT model string)."""
    return dict(_ALL_MODELS)


def get_ports_by_speed(model: DeviceModel, speed: PortSpeed | str) -> list[PortSpec]:
    """Onboard ports of ``model`` matching a speed family."""
    wanted = speed.value if isinstance(speed, PortSpeed) else str(speed)
    return [p for p in model.ports if p.speed.value == wanted]


def get_valid_ports(model_name: str) -> set[str]:
    """Every onboard port name a model exposes."""
    model = resolve_model(model_name)
    if model is None:
        return set()
    return {p.full_name for p in model.ports}


# Named chassis the tests and probes reach for directly, so a test can state
# "this router" without repeating a string literal everywhere.
ROUTER_2911 = _ALL_MODELS["2911"]
HUB_PT = _ALL_MODELS["Hub-PT"]
