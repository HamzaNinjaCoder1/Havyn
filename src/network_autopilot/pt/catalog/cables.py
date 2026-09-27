"""Cable selection rules — newly authored.

When two devices are connected, PT needs to know the cable. For copper
Ethernet the correct choice depends on the device pair (switch↔router uses a
straight-through cable; router↔router needs a crossover). Non-Ethernet
families have exactly one sensible cable type.

The table below is keyed by the *unordered* device-category pair and yields a
cable name. ``infer_cable`` never raises: an unknown pairing falls back to
``"straight"``, which is the PT default and the safe choice for a copper link
whose exact type could not be decided.
"""

from __future__ import annotations

from .devices import category_of_model

# Cable names are the keys PT's IPC API and the script engine accept.
STRAIGHT = "straight"
CROSS = "cross"
SERIAL = "serial"
FIBER = "fiber"
PHONE = "phone"
COAXIAL = "coaxial"
CONSOLE = "console"
ROLL = "roll"
AUTO = "auto"
WIRELESS = "wireless"

CABLE_NAMES = frozenset({
    STRAIGHT, CROSS, SERIAL, FIBER, PHONE, COAXIAL, CONSOLE, ROLL, AUTO,
    WIRELESS,
})

# (category_a, category_b) -> cable. Both orderings are present so a lookup
# never depends on argument order.
def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


_CABLE_RULES_RAW: dict[tuple[str, str], str] = {
    # Copper Ethernet: the crossover cases first (they are the exceptions),
    # everything else defaults to straight-through.
    _pair("router", "router"): CROSS,
    _pair("router", "switch"): STRAIGHT,
    _pair("switch", "switch"): CROSS,
    _pair("switch", "pc"): STRAIGHT,
    _pair("switch", "server"): STRAIGHT,
    _pair("pc", "pc"): CROSS,
    _pair("pc", "server"): CROSS,
    _pair("server", "server"): CROSS,
    _pair("router", "pc"): CROSS,
    _pair("router", "server"): CROSS,
    _pair("firewall", "pc"): STRAIGHT,
    _pair("firewall", "server"): STRAIGHT,
    _pair("firewall", "switch"): STRAIGHT,
    _pair("firewall", "router"): CROSS,
    _pair("switch", "laptop"): STRAIGHT,
    _pair("router", "laptop"): CROSS,
    _pair("laptop", "laptop"): CROSS,
    # Fibre links.
    _pair("switch", "fiber"): FIBER,
    # WAN edge. The provider line (cloud↔modem) is a phone line; the modem's
    # copper handoff to the router and a direct cloud↔router serial link are
    # both straight-through in PT.
    _pair("cloud", "modem"): PHONE,
    _pair("modem", "router"): STRAIGHT,
    _pair("cloud", "router"): STRAIGHT,
    _pair("modem", "modem"): STRAIGHT,
    # Telephony.
    _pair("ip_phone", "switch"): STRAIGHT,
    _pair("analog_phone", "modem"): PHONE,
    # An access point's wired port only comes up on a crossover cable; a
    # straight-through leaves the port down even though PT reports the link.
    _pair("accesspoint", "pc"): CROSS,
    _pair("accesspoint", "server"): CROSS,
    _pair("accesspoint", "laptop"): CROSS,
    # Wireless is a medium, not a cable: reported for completeness.
    _pair("accesspoint", "wireless_end_device"): WIRELESS,
}

# Frozen public view, both orderings normalised.
CABLE_RULES: dict[tuple[str, str], str] = {
    _pair(a, b): c for (a, b), c in _CABLE_RULES_RAW.items()
}


def infer_cable(cat_a: str, cat_b: str) -> str:
    """Cable name for a category pair; ``"straight"`` when undecided.

    Never raises — an unknown pairing is not an error worth aborting a build
    over, and straight-through is PT's own default.
    """
    if not cat_a or not cat_b:
        return STRAIGHT
    return CABLE_RULES.get(_pair(cat_a, cat_b), STRAIGHT)


# Measured model pairs whose cable differs from what their categories imply.
# These come from observing which link PT actually brings up, so they are
# per-model facts rather than a category rule: a cloud reaches a cable modem
# over coax but a DSL modem over a phone line, and both are category "modem".
MEASURED_MODEL_PAIRS: dict[tuple[str, str], str] = {
    _pair("Cloud-PT", "DSL-Modem-PT"): PHONE,
    _pair("Cloud-PT", "Cable-Modem-PT"): COAXIAL,
    _pair("Cloud-PT", "Cell-Tower"): COAXIAL,
    _pair("Cloud-PT", "Central-Office-Server"): COAXIAL,
    _pair("Central-Office-Server", "Cell-Tower"): COAXIAL,
    _pair("Cloud-PT", "Router-PT"): STRAIGHT,
    _pair("Cloud-PT", "2911"): STRAIGHT,
    _pair("Cloud-PT", "4321"): STRAIGHT,
    _pair("Cloud-PT", "4331"): STRAIGHT,
    # #32: the modem's copper handoff is spelled out for every router chassis,
    # not just the generic Router-PT - a category guess is what let a 2911
    # link pick up the wrong cable when the category table changed.
    _pair("DSL-Modem-PT", "Router-PT"): STRAIGHT,
    _pair("DSL-Modem-PT", "2911"): STRAIGHT,
    _pair("DSL-Modem-PT", "4321"): STRAIGHT,
    _pair("DSL-Modem-PT", "4331"): STRAIGHT,
    _pair("Cable-Modem-PT", "Router-PT"): STRAIGHT,
    _pair("Cable-Modem-PT", "2911"): STRAIGHT,
    _pair("Cable-Modem-PT", "4321"): STRAIGHT,
    _pair("Cable-Modem-PT", "4331"): STRAIGHT,
}


def infer_cable_for_models(model_a: str, model_b: str) -> str:
    """Cable for two PT model strings.

    A measured pair wins; otherwise the pair's categories decide. Direction is
    irrelevant — the same cable has to work whichever end PT looks at first.
    """
    a, b = model_a or "", model_b or ""
    measured = MEASURED_MODEL_PAIRS.get(_pair(a, b))
    if measured:
        return measured
    return infer_cable(
        category_of_model(a) or a,
        category_of_model(b) or b,
    )

# Single source of truth for measured cable pairs (#33): the packet_tracer_mcp
# catalog builds its table from this dict instead of keeping a second copy.
_MEASURED_MODEL_PAIRS = MEASURED_MODEL_PAIRS
