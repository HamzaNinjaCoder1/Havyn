"""Interface speed families as Packet Tracer and IOS name them.

The engine reasons about ports by their IOS speed family ("FastEthernet",
"GigabitEthernet", ...). These names come from the IOS CLI itself, which is
why they appear here as an enum of string values rather than as arbitrary
internal codes.

``PortSpeed`` subclasses ``str`` on purpose: port names are built by
concatenating ``speed.value`` with a slot, and catalog entries are compared
both against enum members (``port.speed == PortSpeed.WIRELESS``) and against
plain strings. Keeping the members string-valued makes both work without a
conversion layer.
"""

from __future__ import annotations

from enum import Enum


class PortSpeed(str, Enum):
    FAST_ETHERNET = "FastEthernet"
    GIGABIT_ETHERNET = "GigabitEthernet"
    SERIAL = "Serial"
    CONSOLE = "Console"
    ETHERNET = "Ethernet"
    COAXIAL = "Coaxial"
    MODEM = "Modem"
    # PT names a DSL/Cable modem's own ports "Port 0", "Port 1", ... rather
    # than after a speed family, so they carry their own speed value.
    MODEM_PORT = "Port"
    USB = "USB"
    WIRELESS = "Wireless"
    FIBER = "Fiber"
    # A switched virtual interface: no wire of its own. PT carries an ASA's
    # routed addresses on these (5505 Vlan1/Vlan2, 5506-X Management1/1), and
    # they must never be handed out as a cablable endpoint - nothing can plug
    # a cable into them.
    SVI = "Vlan"
