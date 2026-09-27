"""Model-name normalisation for the device catalog.

PT reports model strings through ``getModel()`` and the spec may use either a
role word ("router") or a shorthand. This module maps the spellings seen in
practice onto the canonical key the catalog table uses.

It only normalises spelling — it does not decide defaults; that is
``resolve_model``'s job.

``MODEL_SPELLINGS`` is the single source of truth (#33) for every alias the
two catalogs in this repo share. The two device tables key some models
differently — this package reads ``Smart-LED`` where PT itself reports
``Smart LED`` — so each accepted spelling maps to the key in *each* catalog:
``(autopilot key, packet_tracer_mcp key)``. The MCP package builds its
``MODEL_ALIASES`` from this table instead of keeping a second copy; two
hand-maintained copies is exactly how they drifted.
"""

from __future__ import annotations

# Accepted spelling (lowercase) -> (autopilot catalog key, PT /
# packet_tracer_mcp catalog key). Both columns must name a model that exists
# in their own device table.
#
# Anything that is NOT shared knowledge stays package-local below: shorthands
# only this package accepts, and aliases where the two packages deliberately
# pick a different chassis ("asa" means the 5505 here and the 5506-X in
# packet_tracer_mcp) — those are different defaults, not spellings of one
# model, so pretending otherwise here would silently move one package's
# default.
MODEL_SPELLINGS: dict[str, tuple[str, str]] = {
    # Same key in both catalogs.
    "2950": ("2950-24", "2950-24"),
    "2960": ("2960-24TT", "2960-24TT"),
    "3560": ("3560-24PS", "3560-24PS"),
    "access_point": ("AccessPoint-PT", "AccessPoint-PT"),
    "ap": ("AccessPoint-PT", "AccessPoint-PT"),
    "cell_tower": ("Cell-Tower", "Cell-Tower"),
    "central_office": ("Central-Office-Server", "Central-Office-Server"),
    "cloud": ("Cloud-PT", "Cloud-PT"),
    "dsl": ("DSL-Modem-PT", "DSL-Modem-PT"),
    "home_gateway": ("HomeRouter-PT-AC", "HomeRouter-PT-AC"),
    "hub": ("Hub-PT", "Hub-PT"),
    "laptop": ("Laptop-PT", "Laptop-PT"),
    "linksys": ("Linksys-WRT300N", "Linksys-WRT300N"),
    "mcu": ("MCU-PT", "MCU-PT"),
    "modem": ("DSL-Modem-PT", "DSL-Modem-PT"),
    "pc": ("PC-PT", "PC-PT"),
    "printer": ("Printer-PT", "Printer-PT"),
    "sbc": ("SBC-PT", "SBC-PT"),
    "server": ("Server-PT", "Server-PT"),
    "splitter": ("CoAxialSplitter-PT", "CoAxialSplitter-PT"),
    "thing": ("Thing", "Thing"),
    "wan": ("Cloud-PT", "Cloud-PT"),
    "wlc": ("WLC-PT", "WLC-PT"),
    # One model, two catalog keys: alias -> (autopilot key, PT key). Every
    # accepted spelling of the model in either dialect is a key here, so both
    # packages resolve both spellings from this one table.
    "smart-led": ("Smart-LED", "Smart LED"),
    "smart led": ("Smart-LED", "Smart LED"),
    "solar-panel": ("Solar-Panel", "Solar Panel"),
    "solar panel": ("Solar-Panel", "Solar Panel"),
    "fire-monitor": ("Fire-Monitor", "Fire Monitor"),
    "fire monitor": ("Fire-Monitor", "Fire Monitor"),
    "trip-sensor": ("Trip-Sensor", "Trip Sensor"),
    "trip sensor": ("Trip-Sensor", "Trip Sensor"),
    "garage-door": ("Garage-Door", "Garage Door"),
    "garage door": ("Garage-Door", "Garage Door"),
    "motion-sensor": ("Motion-Sensor", "Motion Detector"),
    "motion detector": ("Motion-Sensor", "Motion Detector"),
    "humidity-sensor": ("Humidity-Sensor", "Humidity Sensor"),
    "humidity sensor": ("Humidity-Sensor", "Humidity Sensor"),
    "door-sensor": ("Door-Sensor", "Door"),
    "door": ("Door-Sensor", "Door"),
    "co2-sensor": ("CO2-Sensor", "Carbon Dioxide Detector"),
    "carbon dioxide detector": ("CO2-Sensor", "Carbon Dioxide Detector"),
    "web-cam": ("Web-Cam", "Webcam"),
    "webcam": ("Web-Cam", "Webcam"),
    "network-controller": ("Network-Controller", "NetworkController"),
    "networkcontroller": ("Network-Controller", "NetworkController"),
    "sniffer-pt": ("Sniffer-PT", "Sniffer"),
    "sniffer": ("Sniffer-PT", "Sniffer"),
    "patch_panel": ("Copper-Patch-Panel", "Copper Patch Panel"),
    "patch-panel": ("Copper-Patch-Panel", "Copper Patch Panel"),
    "patch panel": ("Copper-Patch-Panel", "Copper Patch Panel"),
    "copper-patch-panel": ("Copper-Patch-Panel", "Copper Patch Panel"),
    "copper patch panel": ("Copper-Patch-Panel", "Copper Patch Panel"),
    "isr4321": ("4321", "ISR4321"),
    "4321": ("4321", "ISR4321"),
    "isr4331": ("4331", "ISR4331"),
    "4331": ("4331", "ISR4331"),
}

# Autopilot-only aliases: shorthands this package accepts (packet_tracer_mcp
# covers the same models with its own names) and defaults only this package
# makes. The shared table is merged over this dict so a key defined twice can
# never shadow the single source (#33).
_MODEL_ALIASES: dict[str, str] = {
    # Router families
    "isr": "2911",
    "2900": "2911",
    "29": "2911",
    # ASA
    "asa5505": "5505",
    "asa5506": "5506-X",
    "5506": "5505",
    "asa": "5505",
    # Wireless
    "accesspoint": "AccessPoint-PT",
    # WAN
    "dsl-modem": "DSL-Modem-PT",
    "cable-modem": "Cable-Modem-PT",
    # Generic PT family chassis
    "router-pt": "Router-PT",
    "switch-pt": "Switch-PT",
    "bridge-pt": "Bridge-PT",
    "repeater-pt": "Repeater-PT",
    # Meraki
    "meraki-mx65w": "Meraki-MX65W",
    "meraki-server": "Meraki-Server",
    # Home gateways
    "homerouter": "HomeRouter-PT-AC",
    "homerouter-pt-ac": "HomeRouter-PT-AC",
    "linksys-wrt300n": "Linksys-WRT300N",
    # Splitter
    "coaxial-splitter": "CoAxialSplitter-PT",
    # Cell tower / central office
    "cell-tower": "Cell-Tower",
    "central-office": "Central-Office-Server",
    # Phone: this package's default is PT's generic IP Phone; the MCP catalog
    # does not carry that model and defaults to a smartphone / the 7960.
    "phone": "IP-Phone-PT",
    "ip_phone": "IP-Phone-PT",
}
_MODEL_ALIASES.update(
    {key: ap_key for key, (ap_key, _mc_key) in MODEL_SPELLINGS.items()}
)


def canonical_model_name(name: str) -> str:
    """Lowercase, trimmed, alias-resolved model or role name."""
    if not name:
        return ""
    key = name.strip().lower()
    return _MODEL_ALIASES.get(key, key)
