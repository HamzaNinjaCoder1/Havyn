"""Wireless association for Packet Tracer, driven by real PT state.

Packet Tracer associates a wireless client on its own: once a host with a
wireless NIC and an access point share the workspace, PT creates a link whose
class is `Antenna`, and that object IS the association. There is no
`associate()` call to make - what there is, and what this module owns, is:

  1. giving the client a radio (PT-LAPTOP-NM-1W on a laptop/tablet/phone; the
     wired NIC has to leave the slot first),
  2. reading the association back from PT instead of assuming it,
  3. refusing to claim success when PT shows no antenna link.

Everything asserted here was measured against a live PT: the antenna appears
within seconds, `Wireless0.getLink()` returns it, and `link.getPort().getOwner
Device()` names the AP. An association that cannot be read back is an error,
not a warning - a drawn antenna is not a working link, and a working link is
not a drawn antenna.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

WIRELESS_INTERFACE = "Wireless0"
ANTENNA_LINK_CLASS = "Antenna"
# A non-default channel is what keeps the client away from radio objects left
# over from earlier (or deleted) topologies; PT pairs radios by channel.
DEFAULT_CHANNEL = 11

# PT's wireless host modules, by host model. The NIC they install creates
# `Wireless0`; the wired NIC has to be removed first or addModule refuses.
HOST_WIRELESS_MODULES: dict[str, tuple[str, str]] = {
    # model -> (module, slot)
    "Laptop-PT": ("PT-LAPTOP-NM-1W", "0"),
    "TabletPC-PT": ("PT-TABLETPC-NM-1W", "0"),
    "SMARTPHONE-PT": ("PT-PDA-NM-1W", "0"),
    "WirelessEndDevice-PT": ("PT-WIRELESSENDDEVICE-NM-1W", "0"),
}


class WirelessAssociationError(RuntimeError):
    """The client is not associated with that access point, per PT's state."""


@dataclass(frozen=True)
class WirelessAssociation:
    """An association read back from Packet Tracer."""

    client: str
    access_point: str
    client_interface: str = WIRELESS_INTERFACE
    ap_interface: str | None = None
    link_class: str = ANTENNA_LINK_CLASS
    port_up: bool = False
    associated: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "access_point": self.access_point,
            "client_interface": self.client_interface,
            "ap_interface": self.ap_interface,
            "link_class": self.link_class,
            "port_up": self.port_up,
            "associated": self.associated,
            "evidence": self.evidence,
        }


def module_for_model(model: str | None) -> tuple[str, str] | None:
    """The (module, slot) that gives this host model a `Wireless0`."""
    if not model:
        return None
    return HOST_WIRELESS_MODULES.get(model)


def install_wireless_radio(
    adapter: Any,
    client: str,
    model: str | None = None,
    interface: str = WIRELESS_INTERFACE,
) -> dict[str, Any]:
    """Put a wireless NIC in the client, replacing its wired one.

    Idempotent: if the interface already exists the module is left alone.
    PT answers false when `addModule` targets the slot the wired NIC still
    occupies, which is why the removal comes first - the same sequence the
    extension's swapLaptopToWireless uses.
    """
    spec = module_for_model(model) or ("PT-LAPTOP-NM-1W", "0")
    module, slot = spec
    return adapter.ensure_wireless_radio(
        client, module=module, slot=slot, interface=interface
    )


def read_association(
    adapter: Any, client: str, interface: str = WIRELESS_INTERFACE
) -> WirelessAssociation | None:
    """What PT says about this client's association, or None if it has none."""
    return adapter.wireless_association(client, interface)


def wireless_radio_interface(adapter: Any, device: str) -> str | None:
    """The radio interface of an access point, from the catalog.

    APs expose their radio as a wireless-speed port (AccessPoint-PT* "Port 1",
    LAP-PT/3702i "Dot11Radio0", HomeRouter "Wireless ..."), which is also the
    port the antenna attaches to.
    """
    from .pt.catalog import resolve_model
    from .pt.enums import PortSpeed

    obs = adapter.inspect_device(device)
    if obs.model.is_known and obs.model.value:
        model = resolve_model(obs.model.value)
        if model is not None:
            for port in model.ports:
                if port.speed == PortSpeed.WIRELESS and port.full_name in obs.ports:
                    return port.full_name
    for name in sorted(obs.ports):
        if name.lower().startswith(("wireless", "dot11", "wlan")):
            return name
    return None


def set_wireless_channel(
    adapter: Any, device: str, port: str | None, channel: int
) -> dict[str, Any]:
    """Put one radio on `channel`; returns what PT answered."""
    if not port:
        return {"ok": False, "device": device, "error": "no wireless interface found"}
    setter = getattr(adapter, "set_wireless_channel", None)
    if setter is None:
        return {"ok": False, "device": device, "error": "adapter cannot set channels"}
    return setter(device, port, channel)


def associate_wireless_client(
    adapter: Any,
    client: str,
    access_point: str,
    *,
    model: str | None = None,
    interface: str = WIRELESS_INTERFACE,
    channel: int | None = DEFAULT_CHANNEL,
    settle_seconds: float = 1.0,
    timeout_seconds: float = 20.0,
) -> WirelessAssociation:
    """Associate `client` with `access_point` and prove it from PT's state.

    PT performs the association itself once both devices are alive, but it
    does not necessarily pick the AP the topology asked for: a fresh client
    joins whatever radio PT offers first, including antenna objects left
    behind by devices that were deleted (measured live).
    So the flow is the deterministic one:

      1. install the radio (the wired NIC leaves the slot first),
      2. put the AP radio and the client radio on the same explicit channel,
         which is what separates the intended AP from any stale radio,
      3. power-cycle the client so it re-scans,
      4. read the antenna link back and require it to name that AP.

    A client that merely has a Wireless0, or an antenna that points at some
    other device, is not an association and raises.
    """
    install_wireless_radio(adapter, client, model=model, interface=interface)
    if channel is not None:
        ap_radio = wireless_radio_interface(adapter, access_point)
        set_wireless_channel(adapter, access_point, ap_radio, channel)
        set_wireless_channel(adapter, client, interface, channel)
        cycler = getattr(adapter, "power_cycle", None)
        if cycler is not None:
            cycler(client)
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    deadline = time.time() + max(timeout_seconds, 0.0)
    last_detail = "no association observed"
    while True:
        association = read_association(adapter, client, interface)
        if association is not None and association.associated:
            if association.access_point and association.access_point != access_point:
                last_detail = (
                    f"associated to {association.access_point}, not {access_point}"
                )
            else:
                return association
        else:
            last_detail = f"PT shows no antenna link on {client}:{interface}"
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    raise WirelessAssociationError(
        f"wireless association failed: {client} is not associated with "
        f"{access_point} ({last_detail})"
    )


def association_evidence(adapter: Any, clients: list[str]) -> dict[str, Any]:
    """Read-back summary for several clients at once (report/debug helper)."""
    out: dict[str, Any] = {}
    for client in clients:
        association = read_association(adapter, client)
        out[client] = association.to_dict() if association else None
    return out


def list_associations_for(adapter: Any) -> list[str]:
    """Every `device:interface` PT currently shows associated (antenna)."""
    lister = getattr(adapter, "list_wireless_associations", None)
    if lister is None:
        return []
    return lister()


__all__ = [
    "ANTENNA_LINK_CLASS",
    "DEFAULT_CHANNEL",
    "HOST_WIRELESS_MODULES",
    "WIRELESS_INTERFACE",
    "WirelessAssociation",
    "WirelessAssociationError",
    "associate_wireless_client",
    "association_evidence",
    "install_wireless_radio",
    "list_associations_for",
    "module_for_model",
    "read_association",
]
