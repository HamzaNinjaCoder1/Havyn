"""Brownfield network importer (S4.1).

The importer builds one inventory from the real device surfaces: PT's topology
snapshot (devices, models, ports, link peers), the device's own configuration,
the host's own `ipconfig /all`, and the operational tables (`show ip route`,
`show vlan brief`, `show interfaces trunk`, `show spanning-tree`) where the
platform exposes them.

Rules that decide the shape:

* Every field is an `Observable`. A value that was not read is UNKNOWN or
  UNSUPPORTED — never a default that silently looks like a fact.
* A configured value and an observed value are different claims. The observed
  value wins when both exist; a configured-only value is kept with
  `source="running-config"` and is reported as PARTIAL by the intent builder.
* A Linksys/ASA/host limitation is recorded against the device, so reports can
  say *what was not visible*, not merely that something was missing.
* Link endpoints come from both sides of PT's link object, so a cable that is
  plugged into the wrong port is visible as fact, not inferred.
"""

from __future__ import annotations

import json
import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..adapter.base import PacketTracerAdapter
from ..core.actual_state import ActualNetworkState, DeviceObservation, PortObservation
from ..core.observability import Observable, ObservationStatus
from ..evidence.store import EvidenceStore
from ..pt.catalog import category_of_model, is_logical_port
from .config_reader import ConfigCorpus, RunningConfigReader, safe_component
from .models import (
    BrownfieldInventory,
    ConfigExtract,
    ConfigStatus,
    DeviceInventory,
    HostInventory,
    InterfaceInventory,
    LinkInventory,
    NETWORK_CATEGORIES,
    HOST_CATEGORIES,
    Platform,
    platform_for_category,
    role_for_category,
    sha256_text,
)
from .parser import ParsedConfig, ParsedInterface


@dataclass
class ImportOptions:
    """What to read beyond the topology snapshot.

    The defaults are "deep": an import is diagnostic-grade. A caller that only
    wants the topology can turn the console reads off, but the result then
    says what it skipped instead of looking complete.
    """

    read_configs: bool = True
    read_host_configs: bool = True
    read_routes: bool = True
    read_vlans: bool = True
    read_trunks: bool = True
    read_stp: bool = True
    read_arp: bool = True
    read_positions: bool = True
    parse_configs: bool = True


@dataclass
class ImportResult:
    inventory: BrownfieldInventory
    corpus: ConfigCorpus
    platforms: dict[str, Platform] = field(default_factory=dict)
    state: ActualNetworkState | None = None
    paths: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inventory": self.inventory.to_dict(),
            "corpus": self.corpus.to_dict(),
            "platforms": {n: p.value for n, p in sorted(self.platforms.items())},
            "paths": dict(self.paths),
        }


def _link_id(a_dev: str, a_port: str, b_dev: str, b_port: str) -> str:
    """A `LinkSpec`-valid id: lowercase, [a-z0-9_-], max 64 chars."""
    raw = f"l-{a_dev}-{a_port}-{b_dev}-{b_port}".lower()
    safe = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-")
    if len(safe) > 64:
        suffix = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        safe = safe[:55].rstrip("-") + "-" + suffix
    return safe


def media_for_interface(name: str, link_class: str | None = None) -> str | None:
    """Interface medium from its IOS name and observed link class.

    PT's topology read reports the link class (`Cable`/`Antenna`), not the
    cable medium, so this never claims fiber vs copper. `serial` and
    `wireless` ARE observable from the port name/link class and are returned;
    everything else is `ethernet` only because the interface family is
    Ethernet, with the cable medium left unclaimed.
    """
    if (link_class or "").lower() == "antenna":
        return "wireless"
    lowered = name.lower()
    if lowered.startswith("serial"):
        return "serial"
    if lowered.startswith(("gigabitethernet", "fastethernet", "ethernet",
                           "tengigabitethernet", "port-channel")):
        return "ethernet"
    if lowered.startswith(("vlan", "loopback", "tunnel", "dialer")):
        return "virtual"
    return None


class BrownfieldImporter:
    """Read a live (or adapter-backed) network into a `BrownfieldInventory`."""

    def __init__(
        self,
        adapter: PacketTracerAdapter,
        *,
        evidence: EvidenceStore | None = None,
        artifacts_root: Path | str | None = None,
        options: ImportOptions | None = None,
        reader: RunningConfigReader | None = None,
    ):
        self.adapter = adapter
        self.evidence = evidence
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.options = options or ImportOptions()
        self.reader = reader or RunningConfigReader(
            adapter,
            evidence=evidence,
            artifacts_root=self.artifacts_root,
        )

    # -- public -------------------------------------------------------------

    def import_network(
        self,
        name: str | None = None,
        device_names: Iterable[str] | None = None,
    ) -> ImportResult:
        started = datetime.now(timezone.utc)
        names = list(device_names) if device_names is not None else None
        if names is None:
            listed = self.adapter.list_device_names()
            if not listed.is_known:
                inventory = BrownfieldInventory(
                    name=name or "brownfield",
                    source=f"adapter:{self.adapter.name}",
                    observed_at=started.isoformat(timespec="seconds"),
                    notes=[
                        "device enumeration failed: "
                        f"{listed.detail or listed.status.value}"
                    ],
                    incomplete=["device list: unknown"],
                )
                return ImportResult(inventory=inventory, corpus=ConfigCorpus())
            names = list(listed.value or [])

        state = self.adapter.observe_state(names)
        observed_at = state.observed_at or started.isoformat(timespec="seconds")
        inventory = BrownfieldInventory(
            name=name or "brownfield",
            source=f"adapter:{self.adapter.name}",
            observed_at=observed_at,
            build_id=self.evidence.build_id if self.evidence is not None else None,
        )
        paths: dict[str, str] = {}

        positions: dict[str, tuple[int, int]] = {}
        if self.options.read_positions:
            positions_obs = self._call(self.adapter.read_device_positions)
            if positions_obs is not None and positions_obs.is_known:
                positions = dict(positions_obs.value or {})
            else:
                inventory.incomplete.append(
                    "device positions: "
                    + (
                        (positions_obs.detail or positions_obs.status.value)
                        if positions_obs is not None
                        else "read failed"
                    )
                )

        platforms: dict[str, Platform] = {}
        for device_name in sorted(state.devices):
            observation = state.devices[device_name]
            device = self._device_from_observation(
                device_name, observation, positions, inventory, platforms
            )
            inventory.devices[device_name] = device

        # Configuration before links: parsed interfaces complete the topology
        # (subinterfaces/SVIs are not always enumerated as ports).
        config_targets = {
            n: platforms.get(n, Platform.UNKNOWN)
            for n in inventory.devices
            if platforms.get(n) in (Platform.IOS, Platform.ASA, Platform.LINKSYS)
        }
        corpus = ConfigCorpus()
        if self.options.read_configs:
            corpus = self.reader.read_many(
                config_targets, parse=self.options.parse_configs
            )
            for device_name, extract in corpus.extracts.items():
                device = inventory.devices.get(device_name)
                if device is not None:
                    device.config = extract
            for device_name, parsed in corpus.parsed.items():
                device = inventory.devices.get(device_name)
                if device is not None:
                    self._apply_parsed(device, parsed, inventory)
        else:
            inventory.incomplete.append("configurations: not requested")

        if self.options.read_host_configs:
            for device in inventory.devices.values():
                if device.category in HOST_CATEGORIES:
                    self._read_host(device, inventory)
        if self.options.read_routes:
            self._read_routes(inventory)
        if self.options.read_vlans or self.options.read_trunks or self.options.read_stp:
            self._read_switch_tables(inventory)
        if self.options.read_arp:
            self._read_arp(inventory)

        self._build_links(inventory)
        self._finish(inventory, state)

        if self.artifacts_root is not None:
            path = self._persist(inventory)
            if path is not None:
                paths["inventory"] = str(path)
        self._record(inventory)
        return ImportResult(
            inventory=inventory,
            corpus=corpus,
            platforms=platforms,
            state=state,
            paths=paths,
        )

    # -- device construction ------------------------------------------------

    def _device_from_observation(
        self,
        name: str,
        observation: DeviceObservation,
        positions: dict[str, tuple[int, int]],
        inventory: BrownfieldInventory,
        platforms: dict[str, Platform],
    ) -> DeviceInventory:
        model = observation.model
        category = category_of_model(str(model.value)) if model.is_known else ""
        platform = platform_for_category(category)
        platforms[name] = platform
        observed_role = (
            str(observation.role.value) if observation.role.is_known else ""
        )
        role_value = observed_role or role_for_category(category)
        device = DeviceInventory(
            name=name,
            category=category,
            model=model,
            role=(
                Observable.known(
                    role_value,
                    source=(
                        observation.role.source
                        if observed_role else "catalog"
                    ),
                )
                if role_value else
                Observable[str].unknown("model is unknown, so the role is unknown")
            ),
            platform=platform,
            source=f"adapter:{self.adapter.name}",
        )
        if name in positions:
            device.position = Observable.known(positions[name], source="canvas")
        else:
            device.position = Observable[tuple].unknown(
                "canvas position not reported"
            )
        if not category:
            inventory.incomplete.append(
                f"device({name}).category: model {model.value!r} is not in the catalog"
            )
            device.notes.append(
                f"model {model.value!r} is not in the catalog; its role and "
                "configuration surface are unknown"
            )
        for port_name, port in observation.ports.items():
            device.interfaces[port_name] = self._interface_from_observation(
                port_name, port
            )
        return device

    def _interface_from_observation(
        self, name: str, port: PortObservation
    ) -> InterfaceInventory:
        link_class = port.link_class.value if port.link_class.is_known else None
        media = media_for_interface(name, link_class)
        interface = InterfaceInventory(name=name)
        interface.state = port.state
        interface.linked = port.linked
        interface.ip = port.ip
        interface.mask = port.mask
        interface.vlan = _as_int(port.vlan)
        interface.trunk = port.trunk
        interface.description = port.description
        interface.peers = port.peers
        interface.peer_ports = port.peer_ports
        interface.link_class = port.link_class
        if media:
            interface.media = Observable.known(
                media, source="interface family" if media != "wireless" else "link class"
            )
        else:
            interface.media = Observable[str].unknown(
                "interface medium is not observable from its name"
            )
        return interface

    # -- parsed configuration ----------------------------------------------

    def _apply_parsed(
        self,
        device: DeviceInventory,
        parsed: ParsedConfig,
        inventory: BrownfieldInventory,
    ) -> None:
        if parsed.globals.hostname and parsed.globals.hostname != device.name:
            device.notes.append(
                f"configured hostname {parsed.globals.hostname!r} differs from "
                f"the canvas name {device.name!r}"
            )
        if parsed.globals.default_gateway:
            device.default_gateway = Observable.known(
                parsed.globals.default_gateway, source="running-config"
            )
        if parsed.platform is Platform.ASA or device.category == "switch":
            # A switch/router that runs `ip routing` is an L3 switch for
            # intent purposes; this is configuration evidence, not a guess.
            if device.category == "switch" and parsed.globals.ip_routing:
                device.category = "switch"
                device.role = Observable.known(
                    "l3_switch", source="running-config:ip routing"
                )
        for name, parsed_iface in parsed.interfaces.items():
            interface = device.interface(name)
            if interface is None:
                interface = InterfaceInventory(name=name)
                interface.subinterface_of = parsed_iface.subinterface_of
                media = media_for_interface(name)
                if media:
                    interface.media = Observable.known(
                        media, source="interface family"
                    )
                # A configured-only interface (SVI/subinterface) has no
                # topology port, so its operational state is not observable.
                interface.state = Observable[str].unknown(
                    "configured interface is not enumerated as a port"
                )
                device.interfaces[name] = interface
            self._merge_parsed_interface(interface, parsed_iface)
        # A config with unparsed statements is PARTIAL by definition; the
        # import already says so through config.status, but the inventory
        # carries the explicit gap too.
        if parsed.unparsed:
            inventory.incomplete.append(
                f"config({device.name}): {len(parsed.unparsed)} unparsed statement(s)"
            )
        for issue in parsed.issues:
            device.notes.append(f"{issue.code}: {issue.message}")

    @staticmethod
    def _merge_parsed_interface(
        interface: InterfaceInventory, parsed: ParsedInterface
    ) -> None:
        source = "running-config"
        if parsed.ip and parsed.ip.lower() != "dhcp":
            if not interface.ip.is_known:
                interface.ip = Observable.known(parsed.ip, source=source)
                if parsed.mask:
                    interface.mask = Observable.known(parsed.mask, source=source)
        if parsed.shutdown is not None:
            interface.admin_state = Observable.known(
                "shutdown" if parsed.shutdown else "up", source=source
            )
        if parsed.description:
            interface.description = Observable.known(parsed.description, source=source)
        if parsed.access_vlan is not None:
            interface.vlan = Observable.known(parsed.access_vlan, source=source)
        if parsed.voice_vlan is not None:
            interface.voice_vlan = Observable.known(parsed.voice_vlan, source=source)
        if parsed.trunk_allowed is not None:
            interface.allowed_vlans = Observable.known(
                list(parsed.trunk_allowed), source=source
            )
        if parsed.trunk_native is not None:
            interface.native_vlan = Observable.known(parsed.trunk_native, source=source)
        if parsed.switchport_mode is not None:
            interface.switchport_mode = Observable.known(
                parsed.switchport_mode, source=source
            )
            if parsed.switchport_mode == "trunk":
                interface.trunk = Observable.known(True, source=source)
        if parsed.encapsulation_vlan is not None:
            interface.encapsulation_vlan = Observable.known(
                parsed.encapsulation_vlan, source=source
            )
        if parsed.mtu is not None:
            interface.mtu = Observable.known(parsed.mtu, source=source)
        if parsed.acl_in:
            interface.acl_in = Observable.known(parsed.acl_in, source=source)
        if parsed.acl_out:
            interface.acl_out = Observable.known(parsed.acl_out, source=source)
        if parsed.nameif:
            interface.nameif = Observable.known(parsed.nameif, source=source)
        if parsed.security_level is not None:
            interface.security_level = Observable.known(
                parsed.security_level, source=source
            )
        if parsed.helpers:
            interface.helpers = Observable.known(list(parsed.helpers), source=source)
        if parsed.ipv6:
            interface.ipv6 = Observable.known(list(parsed.ipv6), source=source)
        if parsed.subinterface_of and not interface.subinterface_of:
            interface.subinterface_of = parsed.subinterface_of
        if parsed.ip and parsed.ip.lower() != "dhcp" and parsed.mask:
            # The subinterface's VLAN is its dot1Q tag; that is what decides
            # which VLAN's traffic the interface serves.
            if parsed.encapsulation_vlan is not None and not interface.vlan.is_known:
                interface.vlan = Observable.known(
                    parsed.encapsulation_vlan, source=source
                )

    # -- hosts --------------------------------------------------------------

    def _read_host(self, device: DeviceInventory, inventory: BrownfieldInventory) -> None:
        answer = self._call(self.adapter.read_host_ip_config, device.name)
        host = HostInventory()
        if answer is None or not answer.is_known:
            detail = (
                answer.detail or answer.status.value
                if answer is not None
                else "read raised an exception"
            )
            host.raw = None
            inventory.incomplete.append(f"host({device.name}): {detail}")
            # Fall back to the topology's IP/mask, which IS observed; gateway
            # and DNS stay unknown rather than being inferred from the subnet.
            for interface in device.interfaces.values():
                if interface.ip.is_known and str(interface.ip.value) not in ("", "0.0.0.0"):
                    host.ip = interface.ip
                    host.mask = interface.mask
                    break
            device.host = host
            return
        data = dict(answer.value or {})
        host = HostInventory(
            ip=_obs_from_raw(data.get("ip"), source=answer.source),
            mask=_obs_from_raw(data.get("mask"), source=answer.source),
            gateway=_obs_from_raw(data.get("gateway"), source=answer.source),
            dns=_obs_list(data.get("dns"), source=answer.source),
            dhcp=_obs_bool(data.get("dhcp"), source=answer.source),
            dhcp_server=_obs_from_raw(data.get("dhcp_server"), source=answer.source),
            interface=_obs_from_raw(data.get("interface"), source=answer.source),
            raw=data.get("raw"),
        )
        device.host = host
        if host.ip.is_known:
            device.default_gateway = host.gateway
            device.dns = host.dns
            # Reflect the host's own read back onto its primary interface.
            iface_name = host.interface.value if host.interface.is_known else None
            interface = device.interface(iface_name) if iface_name else None
            if interface is None:
                interface = next(
                    (
                        i for i in device.interfaces.values()
                        if not is_logical_port(i.name) and i.ip.is_known
                    ),
                    None,
                )
            if interface is not None:
                interface.ip = host.ip
                interface.mask = host.mask

    # -- operational tables -------------------------------------------------

    def _read_routes(self, inventory: BrownfieldInventory) -> None:
        for device in inventory.devices.values():
            role = str(device.role.value) if device.role.is_known else ""
            if device.category not in ("router", "firewall") and role != "l3_switch":
                continue
            answer = self._call(self.adapter.read_routing_table, device.name)
            if answer is not None and answer.is_known:
                device.routes = answer
            else:
                device.routes = Observable[list].unknown(
                    (answer.detail or answer.status.value)
                    if answer is not None
                    else "route read raised an exception"
                )
                inventory.incomplete.append(
                    f"device({device.name}).routes: "
                    f"{device.routes.detail or device.routes.status.value}"
                )

    def _read_arp(self, inventory: BrownfieldInventory) -> None:
        for device in inventory.devices.values():
            if device.category in ("cloud", "modem", "hub", "accesspoint"):
                continue
            answer = self._call(self.adapter.read_arp_table, device.name)
            if answer is not None and answer.is_known:
                device.arp = answer
            else:
                detail = (
                    (answer.detail or answer.status.value)
                    if answer is not None else "ARP read raised an exception"
                )
                device.arp = Observable[list].unknown(detail)
                # An ARP cache is opportunistic evidence; its absence is not
                # an incomplete import of the configuration.

    def _read_switch_tables(self, inventory: BrownfieldInventory) -> None:
        for device in inventory.devices.values():
            if device.category != "switch":
                continue
            if self.options.read_vlans:
                answer = self._call(self.adapter.read_vlans, device.name)
                if answer is not None and answer.is_known:
                    device.vlans_table = answer
                else:
                    device.vlans_table = Observable[list].unknown(
                        (answer.detail or answer.status.value)
                        if answer is not None
                        else "vlan read raised an exception"
                    )
                    inventory.incomplete.append(
                        f"device({device.name}).vlans: "
                        f"{device.vlans_table.detail or device.vlans_table.status.value}"
                    )
            if self.options.read_trunks:
                answer = self._call(self.adapter.read_trunk_allowed, device.name)
                if answer is not None and answer.is_known:
                    device.trunk_allowed = answer
                    # Enrich the ports with the observed allowed lists.
                    for port_name, vlans in (answer.value or {}).items():
                        interface = device.interface(str(port_name))
                        if interface is not None:
                            interface.trunk = Observable.known(
                                True, source="show interfaces trunk"
                            )
                            interface.allowed_vlans = Observable.known(
                                list(vlans), source="show interfaces trunk"
                            )
                else:
                    device.trunk_allowed = Observable[dict].unknown(
                        (answer.detail or answer.status.value)
                        if answer is not None
                        else "trunk read raised an exception"
                    )
            if self.options.read_stp:
                answer = self._call(self.adapter.read_spanning_tree, device.name)
                if answer is not None and answer.is_known:
                    device.stp = answer
                else:
                    device.stp = Observable[list].unknown(
                        (answer.detail or answer.status.value)
                        if answer is not None
                        else "spanning-tree read raised an exception"
                    )
                    # STP is not a required import; a device that cannot report
                    # it is recorded without being called "incomplete" - the
                    # STP rule will report UNKNOWN when it needs it.

    # -- links --------------------------------------------------------------

    def _build_links(self, inventory: BrownfieldInventory) -> None:
        seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
        for device in inventory.devices.values():
            for interface in device.interfaces.values():
                if not (interface.peers.is_known and interface.peers.value):
                    continue
                peer_device = str(interface.peers.value[0])
                peer_port = ""
                if interface.peer_ports.is_known and interface.peer_ports.value:
                    peer_port = str(interface.peer_ports.value[0])
                if peer_device == device.name:
                    continue
                if peer_device not in inventory.devices:
                    inventory.incomplete.append(
                        f"link({device.name}:{interface.name}): peer "
                        f"{peer_device!r} is not in the imported device set"
                    )
                    continue
                if not peer_port:
                    inventory.incomplete.append(
                        f"link({device.name}:{interface.name}): peer port on "
                        f"{peer_device!r} was not reported"
                    )
                    continue
                endpoints = tuple(sorted(((device.name, interface.name), (peer_device, peer_port))))
                if endpoints in seen:
                    continue
                seen.add(endpoints)
                (a_dev, a_port), (b_dev, b_port) = endpoints
                media = media_for_interface(
                    a_port, interface.link_class.value if interface.link_class.is_known else None
                ) or media_for_interface(b_port)
                link = LinkInventory(
                    id=_link_id(a_dev, a_port, b_dev, b_port),
                    a_device=a_dev,
                    a_port=a_port,
                    b_device=b_dev,
                    b_port=b_port,
                    media=(
                        Observable.known(media, source="interface family")
                        if media
                        else Observable[str].unknown(
                            "cable medium is not reported by the topology read"
                        )
                    ),
                    link_class=interface.link_class,
                )
                inventory.links.append(link)

    # -- completeness -------------------------------------------------------

    def _finish(self, inventory: BrownfieldInventory, state: ActualNetworkState) -> None:
        if not inventory.devices:
            inventory.incomplete.append("no devices were observed")
        for name, device in inventory.devices.items():
            if not device.model.is_known:
                inventory.incomplete.append(f"device({name}).model: unknown")
            if device.config is None and device.platform in (
                Platform.IOS, Platform.ASA
            ):
                inventory.incomplete.append(f"device({name}).config: not read")
            if device.config is not None and device.config.status in (
                ConfigStatus.UNSUPPORTED, ConfigStatus.UNKNOWN
            ):
                inventory.incomplete.append(
                    f"device({name}).config: {device.config.status.value}"
                    f" ({device.config.detail or 'no detail'})"
                )
            if device.category in HOST_CATEGORIES:
                if device.host is None or not device.host.ip.is_known:
                    inventory.incomplete.append(
                        f"host({name}).ip: not observed"
                    )
            for interface in device.interfaces.values():
                if is_logical_port(interface.name):
                    # PT enumerates virtual ports (Bluetooth, Wireless, Vlan)
                    # that are not cableable; a down/unknown state on one is
                    # not an incomplete import of the physical network.
                    continue
                if interface.state.status is ObservationStatus.UNKNOWN:
                    inventory.incomplete.append(
                        f"device({name}).port({interface.name}).state: unknown"
                    )
        # De-duplicate while preserving order: one missing fact can be found
        # by two passes (config and topology), and repeating it makes the
        # inventory look worse than it is.
        inventory.incomplete = list(dict.fromkeys(inventory.incomplete))
        if inventory.incomplete:
            inventory.notes.append(
                f"{len(inventory.incomplete)} field(s) could not be established "
                "from the supported Packet Tracer surfaces"
            )

    # -- persistence / evidence --------------------------------------------

    def _persist(self, inventory: BrownfieldInventory) -> Path | None:
        if self.artifacts_root is None:
            return None
        build = safe_component(
            self.evidence.build_id if self.evidence is not None else "import"
        )
        directory = self.artifacts_root / build
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "inventory.json"
        payload = json.dumps(inventory.to_dict(), indent=2, sort_keys=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        tmp.replace(path)
        return path

    def _record(self, inventory: BrownfieldInventory) -> None:
        if self.evidence is None:
            return
        payload = inventory.to_dict()
        self.evidence.record(
            "brownfield_import",
            {
                "name": inventory.name,
                "source": inventory.source,
                "summary": inventory.summary(),
                "incomplete": inventory.incomplete,
                "links": len(inventory.links),
                "devices": sorted(inventory.devices),
            },
            component="brownfield.importer",
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _call(method, *args):
        try:
            return method(*args)
        except Exception:
            return None


def _obs_from_raw(value: Any, *, source: str | None) -> Observable[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return Observable[str].unknown("not reported", source=source)
    return Observable.known(str(value), source=source)


def _obs_list(value: Any, *, source: str | None) -> Observable[list[str]]:
    if not value:
        return Observable[list[str]].unknown("not reported", source=source)
    return Observable.known([str(v) for v in value], source=source)


def _obs_bool(value: Any, *, source: str | None) -> Observable[bool]:
    if value is None:
        return Observable[bool].unknown("not reported", source=source)
    return Observable.known(bool(value), source=source)


def _as_int(observable: Observable[str]) -> Observable[int]:
    if not observable.is_known:
        return Observable[int].unknown(observable.detail or observable.status.value)
    try:
        return Observable.known(int(str(observable.value)), source=observable.source)
    except (TypeError, ValueError):
        return Observable[int].unknown(
            f"not an integer: {observable.value!r}", source=observable.source
        )
