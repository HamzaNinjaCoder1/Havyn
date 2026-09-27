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


def _con(slot: str) -> PortSpec:
    return PortSpec(PortSpeed.CONSOLE, slot)


def _coax(slot: str) -> PortSpec:
    return PortSpec(PortSpeed.COAXIAL, slot)


def _modem(slot: str) -> PortSpec:
    return PortSpec(PortSpeed.MODEM, slot)


# Ports every router chassis exposes regardless of model: the fixed console
# plus the onboard switching ports. Built once and reused per model.
_CONSOLE = (_con("0"),)


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
for _pt_type, _onboard in (
    ("2911", (_ge("0/0"), _ge("0/1"), _ge("0/2"))),
    ("2901", (_ge("0/0"), _ge("0/1"))),
    ("2921", (_ge("0/0"), _ge("0/1"), _ge("0/2"), _ge("0/3"))),
    ("2951", (_ge("0/0"), _ge("0/1"), _ge("0/2"))),
    ("1941", (_ge("0/0"), _ge("0/1"))),
    ("4321", (_ge("0/0"), _ge("0/1"))),
    ("4331", (_ge("0/0"), _ge("0/1"), _ge("0/2"))),
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

for _pt_type in ("2960-24TT", "2950-24", "2950T-24"):
    # Enumerated live: PT numbers these chassis' copper ports 0/1..0/24. There
    # is NO FastEthernet0/0 — naming one here would have the port solver hand
    # a link an interface PT does not have, and addLink refuses it with a bare
    # 'false' that looks like a bad cable.
    _register(DeviceModel(
        pt_type=_pt_type,
        category="switch",
        ports=tuple(_fe(f"0/{i}") for i in range(1, 25))
        + (_ge("0/1"), _ge("0/2")) + _CONSOLE,
        module_slots=(),
    ))

_register(DeviceModel(
    pt_type="3560-24PS", category="switch",
    ports=tuple(_fe(f"0/{i}") for i in range(1, 25))
    + (_ge("0/1"), _ge("0/2")) + _CONSOLE,
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
    that the antenna attaches to.
    """
    return (
        PortSpec(PortSpeed.ETHERNET, "0", "Port 0"),
        PortSpec(PortSpeed.WIRELESS, "1", "Port 1"),
    ) + tuple(
        PortSpec(PortSpeed.ETHERNET, str(i), f"Port {i}") for i in range(2, 16)
    )


_register(DeviceModel(
    pt_type="AccessPoint-PT", category="accesspoint",
    ports=_ap_ports(),
    module_slots=("0", "1", "2", "3", "4", "5", "6"),
))
# PT ships several AP chassis under AccessPoint-PT-<variant>; they behave the
# same for association purposes.
for _ap_type in ("AccessPoint-PT-AC", "AccessPoint-PT-A", "AccessPoint-PT-N",
                 "AccessPoint-PT-AG"):
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
_register(DeviceModel(
    pt_type="WLC-PT", category="wlc",
    ports=(_ge("0"), _ge("1"), _ge("2"), _ge("3"), _ge("4"), _ge("5")),
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

_register(DeviceModel(
    pt_type="IP-Phone-PT", category="ip_phone",
    ports=(_fe("0"), _fe("1")),
    module_slots=(),
))

# -- Security appliance represented as an independent firewall ----------------

_register(DeviceModel(
    pt_type="Cisco-PT-Firewall", category="firewall",
    ports=(_ge("0/0"), _ge("0/1")) + _CONSOLE,
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
    # As with the 2960 there is no "0/0".
    ports=tuple(_fe(f"{i}/1") for i in range(0, 6)) + _CONSOLE,
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Bridge-PT", category="bridge",
    ports=tuple(_fe(f"0/{i}") for i in range(8)),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Repeater-PT", category="repeater",
    ports=tuple(_fe(f"0/{i}") for i in range(4)),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="CoAxialSplitter-PT", category="splitter",
    ports=(_coax("0"), _coax("1"), _coax("2")),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Copper-Patch-Panel", category="patch_panel",
    ports=tuple(_fe(f"0/{i}") for i in range(12)),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Cell-Tower", category="cell_tower",
    ports=(_eth("0"), _eth("1")),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Central-Office-Server", category="central_office",
    ports=tuple(_eth(str(i)) for i in range(4)),
    module_slots=("0", "1", "2", "3", "4", "5", "6"),
))
_register(DeviceModel(
    pt_type="Sniffer-PT", category="sniffer",
    ports=(_eth("0"),),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="MCU-PT", category="mcu",
    ports=(_eth("0"),),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="SBC-PT", category="sbc",
    ports=(_eth("0"),),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Network-Controller", category="network_controller",
    ports=(_eth("0"), _eth("1")),
    module_slots=(),
))

# -- Meraki chassis: MX is a router, Meraki-Server is a server --------------

_register(DeviceModel(
    pt_type="Meraki-MX65W", category="meraki",
    ports=(_ge("0/0"), _ge("0/1")) + _CONSOLE,
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Meraki-Server", category="meraki",
    ports=(_fe("0"),),
    module_slots=(),
))

# -- IP phones ---------------------------------------------------------------

# 7960 carries an integrated 3-port switch: two phone-side FE plus a PC port.
_register(DeviceModel(
    pt_type="7960", category="ip_phone",
    ports=(_fe("0"), _fe("1"), _fe("2")),
    module_slots=(),
))
_register(DeviceModel(
    pt_type="Analog-Phone-PT", category="analog_phone",
    ports=(_fe("0"),),
    module_slots=(),
))

# -- Smart-home / IoT endpoints (PT's "end device" family) -------------------

for _pt_type in ("TV-PT", "Tablet-PT", "PDA-PT", "WirelessEndDevice-PT",
                 "WiredEndDevice-PT", "TabletPC-PT", "SMARTPHONE-PT"):
    _register(DeviceModel(
        pt_type=_pt_type, category="end_device",
        ports=(_eth("0"),),
        module_slots=("0",),
    ))

# PT's IoE family. Enumerated live: add each model and read the workspace back.
# "Smart LED" is the only one of these that PT will actually create *and* that
# exposes a copper port, so it is the one a wired smart-home build can use.
# "Fan" is RF-only (Wireless0 + Bluetooth) and "Thing" exposes no ports at all,
# so neither can be cabled to a switch - claiming FastEthernet0 for them made
# the port solver hand out a link PT refuses.
_IOT_PORTS: dict[str, tuple[PortSpec, ...]] = {
    "Thing": (),
    "Smart-LED": (_fe("0"),),
    "Fan": (PortSpec(PortSpeed.WIRELESS, "0"),),
    "Solar-Panel": (PortSpec(PortSpeed.WIRELESS, "0"),),
    "Fire-Monitor": (PortSpec(PortSpeed.WIRELESS, "0"),),
    "CO2-Sensor": (PortSpec(PortSpeed.WIRELESS, "0"),),
    "Humidity-Sensor": (PortSpec(PortSpeed.WIRELESS, "0"),),
}
# Declared for completeness, but PT refused to add every one of these in the
# live probe, so their port shape is unverified and must not be relied on.
_IOT_UNVERIFIED = (
    "Trip-Sensor", "Door-Sensor", "Motion-Sensor", "Smart-Lock",
    "Smart-Plug", "Web-Cam", "Garage-Door",
)
_IOT_TYPES = tuple(_IOT_PORTS) + _IOT_UNVERIFIED
for _iot_type in _IOT_TYPES:
    _register(DeviceModel(
        pt_type=_iot_type, category="iot",
        ports=_IOT_PORTS.get(_iot_type, (_fe("0"),)),
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
_register(DeviceModel(
    pt_type="DLC100", category="server",
    ports=(_fe("0"),),
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
