"""Stage 4 brownfield models: what an existing network *is*, with its gaps.

Stage 4 reads a network Havyn did not build. Every fact it keeps therefore
carries its epistemic status (`Observable`) and every device carries the
platform class that decides what could be read at all. Nothing here invents a
missing value: an unread gateway is UNKNOWN, an unreadable Linksys is
UNSUPPORTED, and a configuration statement with no parser rule is preserved
verbatim instead of being dropped.

The models are dataclasses (not Pydantic) because they are observations, not
intent: the wall that rejects malformed user input is `NetworkSpec`, and
brownfield data must be able to represent a real network that violates it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.observability import ObservationStatus, Observable
from ..core.ids import canonical_json


class Confidence(str, Enum):
    """How much of one fact the importer actually observed."""

    KNOWN = "known"        # read from the device/runtime
    PARTIAL = "partial"    # some part read, some unsupported
    UNKNOWN = "unknown"    # queried and not answered, or not queried


#: Failure/uncertainty keeps the Stage 1-3 vocabulary so a Stage 4 report can
#: be read beside a Stage 3 one without a translation table.
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}

#: The root-cause classes the Stage 4 rules and the benchmark share. A class
#: is a *kind* of fault, not a device: the benchmark's label and the engine's
#: prediction must use the same string or the confusion matrix is fiction.
CAUSE_CLASSES: tuple[str, ...] = (
    "missing-cable",
    "wrong-cable-endpoint",
    "port-shutdown",
    "port-down-physical",
    "wrong-access-vlan",
    "trunk-vlan-not-allowed",
    "native-vlan-mismatch",
    "undefined-vlan",
    "stp-blocking",
    "wrong-ip",
    "wrong-mask",
    "wrong-gateway",
    "missing-route",
    "wrong-next-hop",
    "acl-block",
    "missing-asa-access-group",
    "wrong-asa-nameif",
    "wrong-security-level",
    "dhcp-subnet-mismatch",
    "dns-misconfiguration",
    "unknown",
)


class ConfigStatus(str, Enum):
    """Outcome of one configuration extraction."""

    KNOWN = "known"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class Platform(str, Enum):
    """What kind of configuration surface a device has."""

    IOS = "ios"
    ASA = "asa"
    LINKSYS = "linksys"
    HOST = "host"
    SERVER = "server"
    NONE = "none"
    UNKNOWN = "unknown"


#: Catalog category -> the closest Havyn device role. A role is an intent
#: concept; the category is what Packet Tracer says the chassis is.
CATEGORY_TO_ROLE: dict[str, str] = {
    "router": "router",
    "switch": "switch",
    "firewall": "firewall",
    "wireless_router": "wireless_router",
    "home_gateway": "wireless_router",
    "pc": "pc",
    "laptop": "laptop",
    "server": "server",
    "printer": "printer",
    "ip_phone": "ip_phone",
    "accesspoint": "access_point",
    "cloud": "cloud",
    "modem": "modem",
    "wlc": "wlc",
    "iot": "iot",
    "end_device": "other",
    "tablet": "tablet",
    "smartphone": "smartphone",
}

HOST_CATEGORIES = frozenset(
    {"pc", "laptop", "server", "printer", "ip_phone", "end_device", "tablet", "smartphone"}
)
NETWORK_CATEGORIES = frozenset({"router", "switch", "firewall", "wireless_router", "home_gateway"})


def role_for_category(category: str) -> str:
    return CATEGORY_TO_ROLE.get(str(category or ""), "other")


def platform_for_category(category: str) -> Platform:
    if category in ("router", "switch"):
        return Platform.IOS
    if category == "firewall":
        return Platform.ASA
    if category in ("wireless_router", "home_gateway"):
        return Platform.LINKSYS
    if category == "server":
        return Platform.SERVER
    if category in HOST_CATEGORIES:
        return Platform.HOST
    return Platform.NONE


@dataclass(frozen=True)
class FieldConfidence:
    """One path's confidence, kept beside the spec the builder produces."""

    path: str
    confidence: Confidence
    source: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "confidence": self.confidence.value,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass
class ConfigExtract:
    """One raw configuration read, with its evidence and its limits."""

    device: str
    platform: Platform
    status: ConfigStatus
    raw: str = ""
    command: str = "show running-config"
    attempts: int = 0
    elapsed_s: float = 0.0
    detail: str | None = None
    raw_path: str | None = None
    sha256: str | None = None
    read_at: str | None = None
    source: str | None = None

    @property
    def readable(self) -> bool:
        return self.status in (ConfigStatus.KNOWN, ConfigStatus.PARTIAL) and bool(self.raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "platform": self.platform.value,
            "status": self.status.value,
            "command": self.command,
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed_s, 3),
            "detail": self.detail,
            "raw_path": self.raw_path,
            "sha256": self.sha256,
            "read_at": self.read_at,
            "source": self.source,
            "raw_chars": len(self.raw),
        }


@dataclass
class HostInventory:
    """A host's own observed IP configuration.

    `dhcp` is tri-state: True/False are observations, None means PT did not
    print the DHCP-servers row. A static-looking `0.0.0.0` DHCP server is
    evidence of a static address, not a missing read.
    """

    ip: Observable[str] = field(default_factory=Observable.not_observed)
    mask: Observable[str] = field(default_factory=Observable.not_observed)
    gateway: Observable[str] = field(default_factory=Observable.not_observed)
    dns: Observable[list[str]] = field(default_factory=Observable.not_observed)
    dhcp: Observable[bool] = field(default_factory=Observable.not_observed)
    dhcp_server: Observable[str] = field(default_factory=Observable.not_observed)
    interface: Observable[str] = field(default_factory=Observable.not_observed)
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip.to_dict(),
            "mask": self.mask.to_dict(),
            "gateway": self.gateway.to_dict(),
            "dns": self.dns.to_dict(),
            "dhcp": self.dhcp.to_dict(),
            "dhcp_server": self.dhcp_server.to_dict(),
            "interface": self.interface.to_dict(),
        }


@dataclass
class InterfaceInventory:
    """One observed interface, physical or logical.

    `admin_state` is configuration (`shutdown` / `up`), `state` is operation
    (`up` / `down`). Keeping them apart is what lets the L1 rules distinguish
    "someone disabled it" from "the link is down" instead of reporting both as
    a generic outage.
    """

    name: str
    media: Observable[str] = field(default_factory=Observable.not_observed)
    ip: Observable[str] = field(default_factory=Observable.not_observed)
    mask: Observable[str] = field(default_factory=Observable.not_observed)
    state: Observable[str] = field(default_factory=Observable.not_observed)
    linked: Observable[bool] = field(default_factory=Observable.not_observed)
    admin_state: Observable[str] = field(default_factory=Observable.not_observed)
    description: Observable[str] = field(default_factory=Observable.not_observed)
    vlan: Observable[int] = field(default_factory=Observable.not_observed)
    trunk: Observable[bool] = field(default_factory=Observable.not_observed)
    allowed_vlans: Observable[list[int]] = field(default_factory=Observable.not_observed)
    native_vlan: Observable[int] = field(default_factory=Observable.not_observed)
    voice_vlan: Observable[int] = field(default_factory=Observable.not_observed)
    switchport_mode: Observable[str] = field(default_factory=Observable.not_observed)
    peers: Observable[list[str]] = field(default_factory=Observable.not_observed)
    peer_ports: Observable[list[str]] = field(default_factory=Observable.not_observed)
    link_class: Observable[str] = field(default_factory=Observable.not_observed)
    subinterface_of: str | None = None
    encapsulation_vlan: Observable[int] = field(default_factory=Observable.not_observed)
    mtu: Observable[int] = field(default_factory=Observable.not_observed)
    acl_in: Observable[str] = field(default_factory=Observable.not_observed)
    acl_out: Observable[str] = field(default_factory=Observable.not_observed)
    nameif: Observable[str] = field(default_factory=Observable.not_observed)
    security_level: Observable[int] = field(default_factory=Observable.not_observed)
    helpers: Observable[list[str]] = field(default_factory=Observable.not_observed)
    ipv6: Observable[list[str]] = field(default_factory=Observable.not_observed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "media": self.media.to_dict(),
            "ip": self.ip.to_dict(),
            "mask": self.mask.to_dict(),
            "state": self.state.to_dict(),
            "linked": self.linked.to_dict(),
            "admin_state": self.admin_state.to_dict(),
            "description": self.description.to_dict(),
            "vlan": self.vlan.to_dict(),
            "trunk": self.trunk.to_dict(),
            "allowed_vlans": self.allowed_vlans.to_dict(),
            "native_vlan": self.native_vlan.to_dict(),
            "voice_vlan": self.voice_vlan.to_dict(),
            "switchport_mode": self.switchport_mode.to_dict(),
            "peers": self.peers.to_dict(),
            "peer_ports": self.peer_ports.to_dict(),
            "link_class": self.link_class.to_dict(),
            "subinterface_of": self.subinterface_of,
            "encapsulation_vlan": self.encapsulation_vlan.to_dict(),
            "mtu": self.mtu.to_dict(),
            "acl_in": self.acl_in.to_dict(),
            "acl_out": self.acl_out.to_dict(),
            "nameif": self.nameif.to_dict(),
            "security_level": self.security_level.to_dict(),
            "helpers": self.helpers.to_dict(),
            "ipv6": self.ipv6.to_dict(),
        }


@dataclass
class DeviceInventory:
    """Everything observed about one device."""

    name: str
    category: str = ""
    model: Observable[str] = field(default_factory=Observable.not_observed)
    role: Observable[str] = field(default_factory=Observable.not_observed)
    position: Observable[tuple[int, int]] = field(default_factory=Observable.not_observed)
    interfaces: dict[str, InterfaceInventory] = field(default_factory=dict)
    host: HostInventory | None = None
    platform: Platform = Platform.UNKNOWN
    config: ConfigExtract | None = None
    routes: Observable[list] = field(default_factory=Observable.not_observed)
    vlans_table: Observable[list] = field(default_factory=Observable.not_observed)
    trunk_allowed: Observable[dict] = field(default_factory=Observable.not_observed)
    stp: Observable[list] = field(default_factory=Observable.not_observed)
    arp: Observable[list] = field(default_factory=Observable.not_observed)
    default_gateway: Observable[str] = field(default_factory=Observable.not_observed)
    dns: Observable[list[str]] = field(default_factory=Observable.not_observed)
    notes: list[str] = field(default_factory=list)
    source: str = ""

    def interface(self, name: str) -> InterfaceInventory | None:
        if name in self.interfaces:
            return self.interfaces[name]
        lowered = name.lower()
        for key, value in self.interfaces.items():
            if key.lower() == lowered:
                return value
        return None

    def physical_interfaces(self) -> list[InterfaceInventory]:
        from ..pt.catalog import is_logical_port

        return [i for i in self.interfaces.values() if not is_logical_port(i.name)]

    @property
    def is_host(self) -> bool:
        return self.category in HOST_CATEGORIES

    @property
    def is_switch(self) -> bool:
        return self.category == "switch"

    @property
    def is_router(self) -> bool:
        return self.category in ("router", "switch", "firewall")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "model": self.model.to_dict(),
            "role": self.role.to_dict(),
            "position": self.position.to_dict(),
            "interfaces": {n: i.to_dict() for n, i in sorted(self.interfaces.items())},
            "host": self.host.to_dict() if self.host is not None else None,
            "platform": self.platform.value,
            "config": self.config.to_dict() if self.config is not None else None,
            "routes": self.routes.to_dict(),
            "vlans_table": self.vlans_table.to_dict(),
            "trunk_allowed": self.trunk_allowed.to_dict(),
            "stp": self.stp.to_dict(),
            "arp": self.arp.to_dict(),
            "default_gateway": self.default_gateway.to_dict(),
            "dns": self.dns.to_dict(),
            "notes": list(self.notes),
            "source": self.source,
        }


@dataclass
class LinkInventory:
    """One observed physical link, from both endpoints' peer evidence."""

    id: str
    a_device: str
    a_port: str
    b_device: str
    b_port: str
    media: Observable[str] = field(default_factory=Observable.not_observed)
    link_class: Observable[str] = field(default_factory=Observable.not_observed)
    notes: list[str] = field(default_factory=list)

    def endpoints(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return (self.a_device, self.a_port), (self.b_device, self.b_port)

    def involves(self, device: str) -> bool:
        return device in (self.a_device, self.b_device)

    def peer_of(self, device: str, port: str | None = None) -> tuple[str, str] | None:
        if device == self.a_device and (port is None or port == self.a_port):
            return self.b_device, self.b_port
        if device == self.b_device and (port is None or port == self.b_port):
            return self.a_device, self.a_port
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "a_device": self.a_device,
            "a_port": self.a_port,
            "b_device": self.b_device,
            "b_port": self.b_port,
            "media": self.media.to_dict(),
            "link_class": self.link_class.to_dict(),
            "notes": list(self.notes),
        }


@dataclass
class BrownfieldInventory:
    """The imported network: topology + host config + raw config per device."""

    name: str
    source: str
    observed_at: str
    devices: dict[str, DeviceInventory] = field(default_factory=dict)
    links: list[LinkInventory] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    build_id: str | None = None

    def device(self, name: str) -> DeviceInventory | None:
        return self.devices.get(name)

    def device_names(self) -> list[str]:
        return sorted(self.devices)

    def neighbors(self, device: str) -> list[tuple[str, str, str]]:
        """(peer_device, local_port, peer_port) for every observed link."""
        out: list[tuple[str, str, str]] = []
        for link in self.links:
            if device == link.a_device:
                out.append((link.b_device, link.a_port, link.b_port))
            elif device == link.b_device:
                out.append((link.a_device, link.b_port, link.a_port))
        return sorted(out)

    def link_between(self, a: str, b: str) -> LinkInventory | None:
        for link in self.links:
            if {link.a_device, link.b_device} == {a, b}:
                return link
        return None

    def all_interfaces(self):
        for device in self.devices.values():
            for interface in device.interfaces.values():
                yield device, interface

    def configs(self) -> dict[str, ConfigExtract]:
        return {
            name: device.config
            for name, device in self.devices.items()
            if device.config is not None
        }

    def summary(self) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        for device in self.devices.values():
            status = device.config.status.value if device.config else "not_read"
            statuses[status] = statuses.get(status, 0) + 1
        return {
            "devices": len(self.devices),
            "links": len(self.links),
            "config_status": statuses,
            "incomplete": len(self.incomplete),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "observed_at": self.observed_at,
            "build_id": self.build_id,
            "devices": {n: d.to_dict() for n, d in sorted(self.devices.items())},
            "links": [link.to_dict() for link in self.links],
            "notes": list(self.notes),
            "incomplete": list(self.incomplete),
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrownfieldInventory":
        inventory = cls(
            name=str(data.get("name") or ""),
            source=str(data.get("source") or ""),
            observed_at=str(data.get("observed_at") or ""),
            notes=list(data.get("notes") or []),
            incomplete=list(data.get("incomplete") or []),
            build_id=data.get("build_id"),
        )
        for name, raw in (data.get("devices") or {}).items():
            inventory.devices[name] = _device_from_dict(name, raw)
        for raw in data.get("links") or []:
            inventory.links.append(
                LinkInventory(
                    id=str(raw.get("id") or ""),
                    a_device=str(raw.get("a_device") or ""),
                    a_port=str(raw.get("a_port") or ""),
                    b_device=str(raw.get("b_device") or ""),
                    b_port=str(raw.get("b_port") or ""),
                    media=Observable.from_dict(raw.get("media", {})),
                    link_class=Observable.from_dict(raw.get("link_class", {})),
                    notes=list(raw.get("notes") or []),
                )
            )
        return inventory


def _device_from_dict(name: str, raw: dict[str, Any]) -> DeviceInventory:
    config_raw = raw.get("config")
    config = None
    if config_raw:
        config = ConfigExtract(
            device=str(config_raw.get("device") or name),
            platform=Platform(config_raw.get("platform") or "unknown"),
            status=ConfigStatus(config_raw.get("status") or "unknown"),
            command=str(config_raw.get("command") or "show running-config"),
            attempts=int(config_raw.get("attempts") or 0),
            elapsed_s=float(config_raw.get("elapsed_s") or 0),
            detail=config_raw.get("detail"),
            raw_path=config_raw.get("raw_path"),
            sha256=config_raw.get("sha256"),
            read_at=config_raw.get("read_at"),
            source=config_raw.get("source"),
        )
    host = None
    host_raw = raw.get("host")
    if host_raw:
        host = HostInventory(
            ip=Observable.from_dict(host_raw.get("ip", {})),
            mask=Observable.from_dict(host_raw.get("mask", {})),
            gateway=Observable.from_dict(host_raw.get("gateway", {})),
            dns=Observable.from_dict(host_raw.get("dns", {})),
            dhcp=Observable.from_dict(host_raw.get("dhcp", {})),
            dhcp_server=Observable.from_dict(host_raw.get("dhcp_server", {})),
            interface=Observable.from_dict(host_raw.get("interface", {})),
        )
    device = DeviceInventory(
        name=name,
        category=str(raw.get("category") or ""),
        model=Observable.from_dict(raw.get("model", {})),
        role=Observable.from_dict(raw.get("role", {})),
        position=Observable.from_dict(raw.get("position", {})),
        host=host,
        platform=Platform(raw.get("platform") or "unknown"),
        config=config,
        routes=Observable.from_dict(raw.get("routes", {})),
        vlans_table=Observable.from_dict(raw.get("vlans_table", {})),
        trunk_allowed=Observable.from_dict(raw.get("trunk_allowed", {})),
        stp=Observable.from_dict(raw.get("stp", {})),
        arp=Observable.from_dict(raw.get("arp", {})),
        default_gateway=Observable.from_dict(raw.get("default_gateway", {})),
        dns=Observable.from_dict(raw.get("dns", {})),
        notes=list(raw.get("notes") or []),
        source=str(raw.get("source") or ""),
    )
    for iface_name, iface_raw in (raw.get("interfaces") or {}).items():
        device.interfaces[iface_name] = InterfaceInventory(
            name=str(iface_raw.get("name") or iface_name),
            media=Observable.from_dict(iface_raw.get("media", {})),
            ip=Observable.from_dict(iface_raw.get("ip", {})),
            mask=Observable.from_dict(iface_raw.get("mask", {})),
            state=Observable.from_dict(iface_raw.get("state", {})),
            linked=Observable.from_dict(iface_raw.get("linked", {})),
            admin_state=Observable.from_dict(iface_raw.get("admin_state", {})),
            description=Observable.from_dict(iface_raw.get("description", {})),
            vlan=Observable.from_dict(iface_raw.get("vlan", {})),
            trunk=Observable.from_dict(iface_raw.get("trunk", {})),
            allowed_vlans=Observable.from_dict(iface_raw.get("allowed_vlans", {})),
            native_vlan=Observable.from_dict(iface_raw.get("native_vlan", {})),
            voice_vlan=Observable.from_dict(iface_raw.get("voice_vlan", {})),
            switchport_mode=Observable.from_dict(iface_raw.get("switchport_mode", {})),
            peers=Observable.from_dict(iface_raw.get("peers", {})),
            peer_ports=Observable.from_dict(iface_raw.get("peer_ports", {})),
            link_class=Observable.from_dict(iface_raw.get("link_class", {})),
            subinterface_of=iface_raw.get("subinterface_of"),
            encapsulation_vlan=Observable.from_dict(iface_raw.get("encapsulation_vlan", {})),
            mtu=Observable.from_dict(iface_raw.get("mtu", {})),
            acl_in=Observable.from_dict(iface_raw.get("acl_in", {})),
            acl_out=Observable.from_dict(iface_raw.get("acl_out", {})),
            nameif=Observable.from_dict(iface_raw.get("nameif", {})),
            security_level=Observable.from_dict(iface_raw.get("security_level", {})),
            helpers=Observable.from_dict(iface_raw.get("helpers", {})),
            ipv6=Observable.from_dict(iface_raw.get("ipv6", {})),
        )
    return device


# -- diagnosis ---------------------------------------------------------------


@dataclass(frozen=True)
class FixAction:
    """The smallest change a diagnosis recommends for one device.

    `rollback_commands` is what puts the prior configuration back. A repair
    with no rollback path is not silently applied: the repair engine refuses
    it before the SafetyGate, because a fix you cannot undo is a lab you can
    lose.
    """

    device: str
    operation: str = "configure_ios"  # configure_ios | configure_host | configure_gateway_lan
    commands: tuple[str, ...] = ()
    reason: str = ""
    rollback_commands: tuple[str, ...] = ()
    host_config: dict[str, Any] = field(default_factory=dict)
    expected_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "operation": self.operation,
            "commands": list(self.commands),
            "reason": self.reason,
            "rollback_commands": list(self.rollback_commands),
            "host_config": dict(self.host_config),
            "expected_state": dict(self.expected_state),
        }


@dataclass
class Diagnostic:
    """The standardized Stage 4 diagnosis record (S4.8).

    Every field the brief requires is present: root cause, device, interface,
    VLAN/subnet, evidence, confidence, fix, impact and verification. A
    diagnosis whose evidence cannot support a cause is `status ==
    "UNKNOWN - ADDITIONAL VERIFICATION REQUIRED"` and carries no fix; a guess
    is never dressed as a diagnosis.
    """

    diagnosis_id: str
    cause_class: str
    layer: str                     # L1 | L2 | L3 | security | services
    root_cause: str
    affected_device: str | None = None
    affected_interface: str | None = None
    vlan: int | None = None
    subnet: str | None = None
    evidence: tuple[str, ...] = ()
    confidence: str = "unknown"    # high | medium | low | unknown
    recommended_fix: tuple[FixAction, ...] = ()
    impact: str = ""
    verification_requirements: tuple[str, ...] = ()
    objective_id: str | None = None
    objective_confirmed: bool = False
    suspected: bool = False
    uncertainty: str | None = None
    first_failing_hop: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def has_fix(self) -> bool:
        return bool(self.recommended_fix)

    @property
    def status(self) -> str:
        if self.confidence == "unknown" or not self.evidence:
            return "UNKNOWN - ADDITIONAL VERIFICATION REQUIRED"
        return "DIAGNOSED"

    @property
    def repairable(self) -> bool:
        return (
            self.status == "DIAGNOSED"
            and self.has_fix
            and self.confidence in ("high", "medium")
            and not self.suspected
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnosis_id": self.diagnosis_id,
            "cause_class": self.cause_class,
            "layer": self.layer,
            "root_cause": self.root_cause,
            "affected_device": self.affected_device,
            "affected_interface": self.affected_interface,
            "vlan": self.vlan,
            "subnet": self.subnet,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "recommended_fix": [fix.to_dict() for fix in self.recommended_fix],
            "impact": self.impact,
            "verification_requirements": list(self.verification_requirements),
            "objective_id": self.objective_id,
            "objective_confirmed": self.objective_confirmed,
            "suspected": self.suspected,
            "uncertainty": self.uncertainty,
            "first_failing_hop": self.first_failing_hop,
            "status": self.status,
            "repairable": self.repairable,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class Objective:
    """One network objective, explicit or inferred.

    An inferred objective is a hypothesis. It may guide diagnosis, but Stage 4
    refuses to apply a repair for it until `confirmed` is True — acting on
    inferred intent is how an engine "fixes" a network nobody asked it to
    change.
    """

    id: str
    statement: str
    kind: str                     # reachability | isolation | addressing | service | dns
    source: str                   # explicit | inferred
    confirmed: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    confirmed_at: str | None = None

    @property
    def actionable(self) -> bool:
        return self.source == "explicit" or self.confirmed

    def with_confirmation(self, when: str) -> "Objective":
        return Objective(
            id=self.id,
            statement=self.statement,
            kind=self.kind,
            source=self.source,
            confirmed=True,
            params=dict(self.params),
            evidence=self.evidence,
            confirmed_at=when,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "kind": self.kind,
            "source": self.source,
            "confirmed": self.confirmed,
            "actionable": self.actionable,
            "params": dict(self.params),
            "evidence": list(self.evidence),
            "confirmed_at": self.confirmed_at,
        }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def stable_json(value: Any) -> str:
    return canonical_json(value)
