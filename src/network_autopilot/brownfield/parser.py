"""Structured IOS/ASA configuration parser (S4.3).

The parser is deliberately total: every line either lands in a typed structure
(with its original line number), in `ignored` (known boilerplate that carries
no network intent), or in `unparsed` (a statement this parser does not model,
preserved verbatim with its section). Nothing is silently dropped — that is
the difference between "we parsed the config" and "we parsed the config we
understood and quietly lost the rest".

Partial parsing is the normal case. A line the parser cannot place never
aborts the document; it is reported, the rest is parsed, and any consumer that
needs the missing semantics must treat it as unknown.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import ConfigExtract, ConfigStatus, Platform


# -- line-level records ------------------------------------------------------


@dataclass(frozen=True)
class ParsedLine:
    """One raw configuration line, with where it came from."""

    line_no: int
    text: str
    section: str
    relationship: str = "statement"  # statement | unparsed | ignored

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "text": self.text,
            "section": self.section,
            "relationship": self.relationship,
        }


@dataclass
class ParserIssue:
    """An ambiguity or contradiction observed while parsing."""

    severity: str  # warning | error
    code: str
    message: str
    line_no: int | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "line_no": self.line_no,
            "path": self.path,
        }


# -- typed structures --------------------------------------------------------


@dataclass
class ParsedInterface:
    name: str
    line_no: int
    ip: str | None = None
    mask: str | None = None
    secondary: list[tuple[str, str]] = field(default_factory=list)
    shutdown: bool | None = None
    description: str | None = None
    switchport_mode: str | None = None
    access_vlan: int | None = None
    voice_vlan: int | None = None
    trunk_native: int | None = None
    trunk_allowed: list[int] | None = None
    trunk_allowed_ops: list[tuple[str, list[int]]] = field(default_factory=list)
    encapsulation_vlan: int | None = None
    mtu: int | None = None
    helpers: list[str] = field(default_factory=list)
    acl_in: str | None = None
    acl_out: str | None = None
    nameif: str | None = None
    security_level: int | None = None
    standby: list[dict[str, Any]] = field(default_factory=list)
    subinterface_of: str | None = None
    ipv6: list[str] = field(default_factory=list)
    vrf: str | None = None
    nat_role: str | None = None  # inside | outside
    channel_group: int | None = None
    channel_mode: str | None = None
    port_security: bool | None = None
    port_security_max: int | None = None
    port_security_violation: str | None = None
    spanning_tree_portfast: bool | None = None
    spanning_tree_bpduguard: bool | None = None
    vtp_domain: str | None = None
    vtp_mode: str | None = None
    lines: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "line_no": self.line_no,
            "ip": self.ip,
            "mask": self.mask,
            "secondary": [list(pair) for pair in self.secondary],
            "shutdown": self.shutdown,
            "description": self.description,
            "switchport_mode": self.switchport_mode,
            "access_vlan": self.access_vlan,
            "voice_vlan": self.voice_vlan,
            "trunk_native": self.trunk_native,
            "trunk_allowed": list(self.trunk_allowed) if self.trunk_allowed is not None else None,
            "trunk_allowed_ops": [[op, list(v)] for op, v in self.trunk_allowed_ops],
            "encapsulation_vlan": self.encapsulation_vlan,
            "mtu": self.mtu,
            "helpers": list(self.helpers),
            "acl_in": self.acl_in,
            "acl_out": self.acl_out,
            "nameif": self.nameif,
            "security_level": self.security_level,
            "standby": [dict(s) for s in self.standby],
            "subinterface_of": self.subinterface_of,
            "ipv6": list(self.ipv6),
            "vrf": self.vrf,
            "nat_role": self.nat_role,
            "channel_group": self.channel_group,
            "channel_mode": self.channel_mode,
            "port_security": self.port_security,
            "port_security_max": self.port_security_max,
            "port_security_violation": self.port_security_violation,
            "spanning_tree_portfast": self.spanning_tree_portfast,
            "spanning_tree_bpduguard": self.spanning_tree_bpduguard,
            "vtp_domain": self.vtp_domain,
            "vtp_mode": self.vtp_mode,
            "lines": list(self.lines),
        }


@dataclass
class ParsedVlan:
    vlan_id: int
    name: str | None = None
    line_no: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"vlan_id": self.vlan_id, "name": self.name, "line_no": self.line_no}


@dataclass
class ParsedStaticRoute:
    network: str
    mask: str
    next_hop: str | None = None
    interface: str | None = None
    distance: int | None = None
    line_no: int | None = None
    raw: str = ""

    @property
    def cidr(self) -> str | None:
        try:
            return str(ipaddress.IPv4Network(f"{self.network}/{self.mask}", strict=False))
        except ValueError:
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "mask": self.mask,
            "cidr": self.cidr,
            "next_hop": self.next_hop,
            "interface": self.interface,
            "distance": self.distance,
            "line_no": self.line_no,
        }


@dataclass
class ParsedRoutingNetwork:
    network: str | None = None
    wildcard: str | None = None
    area: str | None = None
    line_no: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "wildcard": self.wildcard,
            "area": self.area,
            "line_no": self.line_no,
        }


@dataclass
class ParsedRoutingProtocol:
    protocol: str  # ospf | eigrp | rip | bgp
    process: str
    line_no: int
    networks: list[ParsedRoutingNetwork] = field(default_factory=list)
    redistribute: list[str] = field(default_factory=list)
    passive_interfaces: list[str] = field(default_factory=list)
    neighbors: list[dict[str, Any]] = field(default_factory=list)
    default_information: bool = False
    no_auto_summary: bool = False
    version: int | None = None
    router_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "process": self.process,
            "line_no": self.line_no,
            "networks": [n.to_dict() for n in self.networks],
            "redistribute": list(self.redistribute),
            "passive_interfaces": list(self.passive_interfaces),
            "neighbors": [dict(n) for n in self.neighbors],
            "default_information": self.default_information,
            "no_auto_summary": self.no_auto_summary,
            "version": self.version,
            "router_id": self.router_id,
        }


@dataclass
class ParsedAclRule:
    action: str              # permit | deny
    protocol: str = "ip"
    source: str = "any"
    source_wildcard: str | None = None
    source_kind: str = "any"  # any | host | network | object | object-group
    destination: str = "any"
    destination_wildcard: str | None = None
    destination_kind: str = "any"
    operands: tuple[str, ...] = ()
    line_no: int | None = None
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "protocol": self.protocol,
            "source": self.source,
            "source_wildcard": self.source_wildcard,
            "source_kind": self.source_kind,
            "destination": self.destination,
            "destination_wildcard": self.destination_wildcard,
            "destination_kind": self.destination_kind,
            "operands": list(self.operands),
            "line_no": self.line_no,
        }


@dataclass
class ParsedAcl:
    name: str
    kind: str = "extended"  # standard | extended | asa
    rules: list[ParsedAclRule] = field(default_factory=list)
    line_no: int | None = None
    bindings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "rules": [r.to_dict() for r in self.rules],
            "line_no": self.line_no,
            "bindings": [dict(b) for b in self.bindings],
        }


@dataclass
class ParsedNat:
    kind: str  # inside_source | inside | outside | pool | asa_dynamic
    line_no: int | None = None
    raw: str = ""
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "line_no": self.line_no, "fields": dict(self.fields)}


@dataclass
class ParsedDhcpPool:
    name: str
    line_no: int | None = None
    network: str | None = None
    mask: str | None = None
    default_router: list[str] = field(default_factory=list)
    dns_servers: list[str] = field(default_factory=list)
    lease: str | None = None
    excluded: list[tuple[str, str | None]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "line_no": self.line_no,
            "network": self.network,
            "mask": self.mask,
            "default_router": list(self.default_router),
            "dns_servers": list(self.dns_servers),
            "lease": self.lease,
            "excluded": [list(e) for e in self.excluded],
        }


@dataclass
class ParsedGlobal:
    hostname: str | None = None
    domain_name: str | None = None
    default_gateway: str | None = None
    ip_routing: bool | None = None
    ipv6_unicast_routing: bool | None = None
    acl_bindings: list[dict[str, Any]] = field(default_factory=list)
    asa_routes: list[ParsedStaticRoute] = field(default_factory=list)
    asa_dhcp: list[dict[str, Any]] = field(default_factory=list)
    asa_objects: list[dict[str, Any]] = field(default_factory=list)
    vtp: dict[str, Any] = field(default_factory=dict)
    stp: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "domain_name": self.domain_name,
            "default_gateway": self.default_gateway,
            "ip_routing": self.ip_routing,
            "ipv6_unicast_routing": self.ipv6_unicast_routing,
            "acl_bindings": [dict(b) for b in self.acl_bindings],
            "asa_routes": [r.to_dict() for r in self.asa_routes],
            "asa_dhcp": [dict(d) for d in self.asa_dhcp],
            "asa_objects": [dict(o) for o in self.asa_objects],
            "vtp": dict(self.vtp),
            "stp": [dict(s) for s in self.stp],
        }


@dataclass
class ParsedConfig:
    device: str
    raw: str
    platform: Platform = Platform.IOS
    globals: ParsedGlobal = field(default_factory=ParsedGlobal)
    interfaces: dict[str, ParsedInterface] = field(default_factory=dict)
    vlans: dict[int, ParsedVlan] = field(default_factory=dict)
    static_routes: list[ParsedStaticRoute] = field(default_factory=list)
    routing_protocols: list[ParsedRoutingProtocol] = field(default_factory=list)
    acls: dict[str, ParsedAcl] = field(default_factory=dict)
    nat: list[ParsedNat] = field(default_factory=list)
    dhcp_pools: dict[str, ParsedDhcpPool] = field(default_factory=dict)
    excluded_addresses: list[tuple[str, str | None]] = field(default_factory=list)
    standby: list[dict[str, Any]] = field(default_factory=list)
    lines: list[ParsedLine] = field(default_factory=list)
    unparsed: list[ParsedLine] = field(default_factory=list)
    ignored: list[ParsedLine] = field(default_factory=list)
    issues: list[ParserIssue] = field(default_factory=list)
    status: ConfigStatus = ConfigStatus.KNOWN

    def interface(self, name: str) -> ParsedInterface | None:
        if name in self.interfaces:
            return self.interfaces[name]
        lowered = name.lower()
        for key, value in self.interfaces.items():
            if key.lower() == lowered:
                return value
        return None

    def acl(self, name: str) -> ParsedAcl | None:
        if name in self.acls:
            return self.acls[name]
        lowered = name.lower()
        for key, value in self.acls.items():
            if key.lower() == lowered:
                return value
        return None

    @property
    def interfaces_with_addresses(self) -> list[ParsedInterface]:
        return [i for i in self.interfaces.values() if i.ip]

    def all_acl_rules(self) -> list[ParsedAclRule]:
        out: list[ParsedAclRule] = []
        for acl in self.acls.values():
            out.extend(acl.rules)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "platform": self.platform.value,
            "status": self.status.value,
            "globals": self.globals.to_dict(),
            "interfaces": {n: i.to_dict() for n, i in sorted(self.interfaces.items())},
            "vlans": {str(v): v.to_dict() for v, v in sorted(self.vlans.items())},
            "static_routes": [r.to_dict() for r in self.static_routes],
            "routing_protocols": [p.to_dict() for p in self.routing_protocols],
            "acls": {n: a.to_dict() for n, a in sorted(self.acls.items())},
            "nat": [n.to_dict() for n in self.nat],
            "dhcp_pools": {n: p.to_dict() for n, p in sorted(self.dhcp_pools.items())},
            "excluded_addresses": [list(e) for e in self.excluded_addresses],
            "standby": [dict(s) for s in self.standby],
            "unparsed": [l.to_dict() for l in self.unparsed],
            "ignored": [l.to_dict() for l in self.ignored],
            "issues": [i.to_dict() for i in self.issues],
            "line_count": len(self.lines),
        }


# -- parser ------------------------------------------------------------------

_NOISE_EXACT = {
    "!", "end", "exit", "quit",
}
_NOISE_PREFIXES = (
    "Building configuration",
    "Current configuration",
    "ASA Version",
    ":: end",
    "Cryptochecksum",
)
_IGNORABLE_PREFIXES = (
    "service ", "no service ", "line ", " exec-timeout", " login", " password ",
    "banner ", "enable secret", "enable password", "username ", "crypto ",
    "ip ssh ", "logging ", "ntp ", "snmp-server ", "clock ", "no cdp ",
    "cdp ", "archive ", "memory-size", "boot ", "aaa ", "tacacs-server ",
    "radius-server ", "license ", "hostname ", "no ip domain-lookup",
    "ip domain-lookup", "ip domain name", "ip http ", "ip https ",
    "ip cef", "no ip cef", "ipv6 unicast-routing", "no ipv6 unicast-routing",
    "ip forwarding", "no ip forwarding", "ip routing", "no ip routing",
    "transport input", "transport output", "exec-timeout", "privilege level",
    "stopbits", "speed ", "flowcontrol", "logging synchronous",
    "password ", "secret ", "authorization ", "accounting ",
    "ip access-list", "access-list",  # handled before this list; safety net
)

_IFACE_RE = re.compile(r"^interface\s+(\S+)$", re.IGNORECASE)
_SUBIF_RE = re.compile(r"^(.*)\.(\d+)$")
_VLAN_BLOCK_RE = re.compile(r"^vlan\s+(\d{1,4})$", re.IGNORECASE)
_ROUTER_RE = re.compile(r"^router\s+(ospf|eigrp|rip|bgp)\s*(\S*)$", re.IGNORECASE)
_ACL_MODE_RE = re.compile(r"^ip\s+access-list\s+(standard|extended)\s+(\S+)$", re.IGNORECASE)
_ACL_LINE_RE = re.compile(
    r"^access-list\s+(\S+)\s+(standard|extended)\s+(permit|deny)\s+(.+)$",
    re.IGNORECASE,
)
_ACL_IOS_RE = re.compile(
    r"^access-list\s+(\S+)\s+(permit|deny)\s+(.+)$", re.IGNORECASE
)
_DHCP_POOL_RE = re.compile(r"^ip\s+dhcp\s+pool\s+(\S+)$", re.IGNORECASE)
_NET_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_MASK_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _parse_network(ip: str, mask: str) -> str | None:
    try:
        return str(ipaddress.IPv4Network(f"{ip}/{mask}", strict=False))
    except ValueError:
        return None


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _split_allowed(tokens: Iterable[str]) -> list[int]:
    from ..adapter.packet_tracer.js_commands import expand_vlan_list

    ordered = [t for t in tokens if not t.lower() in ("add", "remove", "all", "none", "except")]
    if len(ordered) == 0:
        return []
    return expand_vlan_list(",".join(ordered))


def _acl_address(tokens: list[str], index: int, *, asa: bool) -> tuple[str, str | None, str, int]:
    """Parse one ACL address operand -> (value, wildcard, kind, next_index)."""
    if index >= len(tokens):
        return "any", None, "any", index
    token = tokens[index].lower()
    if token == "any":
        return "any", None, "any", index + 1
    if token == "host" and index + 1 < len(tokens):
        return tokens[index + 1], None, "host", index + 2
    if asa and token in ("object", "object-group") and index + 1 < len(tokens):
        return tokens[index + 1], None, token, index + 2
    value = tokens[index]
    index += 1
    if _NET_RE.match(value) and index < len(tokens) and _MASK_RE.match(tokens[index]):
        wildcard = tokens[index]
        # An ASA/standard ACL sometimes writes an address plus a normal mask;
        # both are preserved; the matcher accepts either shape.
        return value, wildcard, "network", index + 1
    return value, None, "host", index


def parse_acl_rule(text: str, *, line_no: int | None = None) -> ParsedAclRule | None:
    """Parse one access-list rule body (without the `access-list NAME` head)."""
    raw = text.strip()
    tokens = raw.split()
    if len(tokens) < 2:
        return None
    action = tokens[0].lower()
    if action not in ("permit", "deny"):
        return None
    protocol = tokens[1].lower()
    asa = "object-group" in raw.lower() or "object " in raw.lower()
    index = 2
    source, source_wild, source_kind, index = _acl_address(tokens, index, asa=asa)
    destination, destination_wild, destination_kind, index = _acl_address(tokens, index, asa=asa)
    return ParsedAclRule(
        action=action,
        protocol=protocol,
        source=source,
        source_wildcard=source_wild,
        source_kind=source_kind,
        destination=destination,
        destination_wildcard=destination_wild,
        destination_kind=destination_kind,
        operands=tuple(tokens[index:]),
        line_no=line_no,
        raw=raw,
    )


class IosConfigParser:
    """A total, line-traceable parser for IOS and ASA `show running-config`."""

    def parse(self, raw: str, *, device: str = "", platform: Platform | None = None) -> ParsedConfig:
        text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
        detected = platform or self._detect_platform(text)
        config = ParsedConfig(device=device, raw=text, platform=detected)
        section = "global"
        current_iface: ParsedInterface | None = None
        current_router: ParsedRoutingProtocol | None = None
        current_vlan: ParsedVlan | None = None
        current_acl: ParsedAcl | None = None
        current_pool: ParsedDhcpPool | None = None
        current_object: dict[str, Any] | None = None
        last_standby: dict[str, Any] | None = None

        for line_no, raw_line in enumerate(text.split("\n"), start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.lower() in ("exit", "quit"):
                # `exit` closes the current submode. Without this, every
                # command after the first `exit` was treated as a subcommand
                # of the block it left - the compiler emits flat command lists
                # that rely on `exit` for exactly this.
                current_iface = None
                current_router = None
                current_vlan = None
                current_acl = None
                current_pool = None
                current_object = None
                last_standby = None
                section = "global"
                config.ignored.append(ParsedLine(line_no, stripped, section, "ignored"))
                continue
            if self._is_noise(stripped):
                config.ignored.append(ParsedLine(line_no, stripped, section, "ignored"))
                continue
            indented = raw_line[:1].isspace()

            # -- block heads (unindented) --------------------------------
            if not indented:
                head = self._head(stripped)
                if head is not None:
                    kind, value = head
                    if kind == "interface":
                        current_iface = self._new_interface(config, value, line_no)
                        current_router = None
                        current_vlan = None
                        current_acl = None
                        current_pool = None
                        current_object = None
                        last_standby = None
                        section = f"interface:{value}"
                        config.lines.append(ParsedLine(line_no, stripped, section))
                        continue
                    if kind == "vlan":
                        current_vlan = ParsedVlan(vlan_id=value, line_no=line_no)
                        config.vlans[value] = current_vlan
                        current_iface = None
                        current_router = None
                        current_acl = None
                        current_pool = None
                        section = f"vlan:{value}"
                        config.lines.append(ParsedLine(line_no, stripped, section))
                        continue
                    if kind == "router":
                        protocol, process = value
                        current_router = ParsedRoutingProtocol(
                            protocol=protocol, process=process, line_no=line_no
                        )
                        config.routing_protocols.append(current_router)
                        current_iface = None
                        current_vlan = None
                        current_acl = None
                        current_pool = None
                        section = f"router:{protocol}:{process}"
                        config.lines.append(ParsedLine(line_no, stripped, section))
                        continue
                    if kind == "acl_mode":
                        acl_kind, name = value
                        current_acl = config.acls.setdefault(
                            name, ParsedAcl(name=name, kind=acl_kind, line_no=line_no)
                        )
                        current_iface = None
                        current_router = None
                        current_vlan = None
                        current_pool = None
                        section = f"acl:{name}"
                        config.lines.append(ParsedLine(line_no, stripped, section))
                        continue
                    if kind == "dhcp_pool":
                        current_pool = ParsedDhcpPool(name=value, line_no=line_no)
                        config.dhcp_pools[value] = current_pool
                        current_iface = None
                        current_router = None
                        current_vlan = None
                        current_acl = None
                        section = f"dhcp-pool:{value}"
                        config.lines.append(ParsedLine(line_no, stripped, section))
                        continue
                    if kind == "object":
                        current_object = {"kind": "object", "name": value, "lines": []}
                        config.globals.asa_objects.append(current_object)
                        current_iface = None
                        current_router = None
                        current_vlan = None
                        current_acl = None
                        current_pool = None
                        section = f"object:{value}"
                        config.lines.append(ParsedLine(line_no, stripped, section))
                        continue

            # -- statements inside a block ------------------------------
            handled = False
            if current_iface is not None:
                handled = self._iface_statement(config, current_iface, stripped, line_no)
                if handled and stripped.startswith("standby "):
                    last_standby = current_iface.standby[-1]
                elif handled and last_standby is not None and self._standby_continuation(stripped):
                    self._apply_standby(last_standby, stripped, line_no)
            if current_router is not None and not handled:
                handled = self._router_statement(current_router, stripped, line_no)
                if handled:
                    section = f"router:{current_router.protocol}:{current_router.process}"
            if current_vlan is not None and not handled:
                if stripped.lower().startswith("name "):
                    current_vlan.name = stripped.split(None, 1)[1].strip()
                    handled = True
            if current_pool is not None and not handled:
                handled = self._pool_statement(current_pool, stripped, line_no)
            if current_acl is not None and not handled and not indented:
                # an unindented global line ends the named-ACL block
                current_acl = None
            if current_acl is not None and not handled and indented:
                rule = parse_acl_rule(stripped, line_no=line_no)
                if rule is not None:
                    current_acl.rules.append(rule)
                    handled = True
            if current_object is not None and not handled:
                current_object["lines"].append(stripped)
                handled = True
            if not handled:
                handled = self._global_statement(config, stripped, line_no, section)

            if handled:
                config.lines.append(ParsedLine(line_no, stripped, section))
            else:
                config.unparsed.append(ParsedLine(line_no, stripped, section, "unparsed"))
                config.lines.append(ParsedLine(line_no, stripped, section, "unparsed"))

        self._finish(config)
        return config

    # -- head recognisers ---------------------------------------------------

    def _detect_platform(self, text: str) -> Platform:
        lowered = text.lower()
        if (
            "asa version" in lowered
            or re.search(r"(?m)^\s*nameif\s+\S+", text)
            or re.search(r"(?m)^\s*security-level\s+\d+", text)
            or re.search(r"(?m)^\s*access-group\s+\S+\s+(in|out)\s+interface\s+", text)
            or re.search(r"(?m)^\s*route\s+(inside|outside|dmz)\s+", text)
        ):
            return Platform.ASA
        return Platform.IOS

    @staticmethod
    def _is_noise(line: str) -> bool:
        if line in _NOISE_EXACT:
            return True
        if line.startswith("!"):
            return True
        # Console echo: `r1#show running-config` is not a configuration line.
        if re.match(r"^\S+[#>]\s*(show|terminal|enable|configure)\b", line):
            return True
        return any(line.startswith(prefix) for prefix in _NOISE_PREFIXES)

    @staticmethod
    def _head(line: str) -> tuple[str, Any] | None:
        match = _IFACE_RE.match(line)
        if match:
            return "interface", match.group(1)
        match = _VLAN_BLOCK_RE.match(line)
        if match:
            return "vlan", int(match.group(1))
        match = _ROUTER_RE.match(line)
        if match:
            return "router", (match.group(1).lower(), match.group(2) or "1")
        match = _ACL_MODE_RE.match(line)
        if match:
            return "acl_mode", (match.group(1).lower(), match.group(2))
        match = _DHCP_POOL_RE.match(line)
        if match:
            return "dhcp_pool", match.group(1)
        if line.lower().startswith("object network "):
            return "object", line.split(None, 2)[2]
        return None

    @staticmethod
    def _standby_continuation(line: str) -> bool:
        lowered = line.lower()
        return lowered.startswith(("priority ", "preempt", "track ", "timers "))

    @staticmethod
    def _new_interface(config: ParsedConfig, name: str, line_no: int) -> ParsedInterface:
        interface = ParsedInterface(name=name, line_no=line_no)
        sub = _SUBIF_RE.match(name)
        if sub:
            interface.subinterface_of = sub.group(1)
        config.interfaces[name] = interface
        return interface

    # -- statement handlers -------------------------------------------------

    def _iface_statement(
        self, config: ParsedConfig, iface: ParsedInterface, line: str, line_no: int
    ) -> bool:
        lowered = line.lower()
        iface.lines.append(line_no)
        if lowered == "shutdown":
            iface.shutdown = True
            return True
        if lowered == "no shutdown":
            iface.shutdown = False
            return True
        if lowered.startswith("description "):
            iface.description = line.split(None, 1)[1].strip()
            return True
        if lowered.startswith("ip address "):
            parts = line.split()
            if len(parts) >= 4 and parts[2].lower() not in ("dhcp", "negotiated"):
                if len(parts) >= 5 and parts[4].lower() == "secondary":
                    iface.secondary.append((parts[2], parts[3]))
                elif "secondary" in lowered:
                    iface.secondary.append((parts[2], parts[3]))
                else:
                    iface.ip, iface.mask = parts[2], parts[3]
                return True
            if len(parts) >= 3 and parts[2].lower() == "dhcp":
                iface.ip = "dhcp"
                return True
        if lowered.startswith("ipv6 address "):
            iface.ipv6.append(line.split(None, 2)[2])
            return True
        if lowered.startswith("switchport mode "):
            iface.switchport_mode = lowered.split()[-1]
            return True
        if lowered.startswith("switchport access vlan "):
            iface.access_vlan = _int_or_none(lowered.split()[-1])
            return True
        if lowered.startswith("switchport voice vlan "):
            iface.voice_vlan = _int_or_none(lowered.split()[-1])
            return True
        if lowered.startswith("switchport trunk native vlan "):
            iface.trunk_native = _int_or_none(lowered.split()[-1])
            return True
        if lowered.startswith("switchport trunk allowed vlan "):
            rest = lowered.split("vlan ", 1)[1].strip()
            if rest in ("all", "none"):
                iface.trunk_allowed = [] if rest == "none" else list(range(1, 4095))
                return True
            op = "set"
            if rest.startswith("add "):
                op, rest = "add", rest[4:]
            elif rest.startswith("remove "):
                op, rest = "remove", rest[7:]
            elif rest.startswith("except "):
                op, rest = "except", rest[7:]
            values = _split_allowed(rest.replace(",", " ").split())
            if op == "except":
                excluded = set(values)
                iface.trunk_allowed = [v for v in range(1, 4095) if v not in excluded]
            else:
                iface.trunk_allowed_ops.append((op, values))
                if op == "set" or iface.trunk_allowed is None:
                    iface.trunk_allowed = list(values)
                elif op == "add":
                    iface.trunk_allowed = sorted(set(iface.trunk_allowed) | set(values))
                elif op == "remove":
                    iface.trunk_allowed = sorted(set(iface.trunk_allowed) - set(values))
            return True
        if lowered.startswith("encapsulation dot1q "):
            iface.encapsulation_vlan = _int_or_none(lowered.split()[-1])
            return True
        if lowered.startswith("mtu "):
            iface.mtu = _int_or_none(lowered.split()[-1])
            return True
        if lowered.startswith("ip helper-address "):
            iface.helpers.append(line.split()[-1])
            return True
        if lowered in ("ip nat inside", "ip nat outside"):
            iface.nat_role = lowered.split()[-1]
            return True
        if lowered.startswith("ip access-group "):
            parts = line.split()
            if len(parts) >= 4:
                if parts[3].lower() == "in":
                    iface.acl_in = parts[2]
                elif parts[3].lower() == "out":
                    iface.acl_out = parts[2]
                return True
        if lowered.startswith("nameif "):
            iface.nameif = line.split(None, 1)[1].strip()
            return True
        if lowered.startswith("security-level "):
            iface.security_level = _int_or_none(line.split()[-1])
            return True
        if lowered.startswith("standby "):
            parts = line.split()
            group = _int_or_none(parts[1]) if len(parts) > 1 else None
            if group is None:
                return False
            record = next(
                (r for r in iface.standby if r.get("group") == group), None
            )
            if record is None:
                record = {"group": group, "line_no": line_no}
                iface.standby.append(record)
                config.standby.append(record)
            if len(parts) >= 4 and parts[2].lower() == "ip":
                record["ip"] = parts[3]
            elif len(parts) >= 4 and parts[2].lower() == "priority":
                record["priority"] = _int_or_none(parts[3])
            elif lowered.endswith(" preempt"):
                record["preempt"] = True
            elif len(parts) >= 4 and parts[2].lower() == "track":
                record.setdefault("track", []).append(" ".join(parts[3:]))
            return True
        if lowered.startswith("ip vrf "):
            iface.vrf = line.split(None, 2)[2]
            return True
        if lowered.startswith("channel-group "):
            parts = line.split()
            iface.channel_group = _int_or_none(parts[1])
            if "mode" in lowered:
                iface.channel_mode = parts[-1]
            return True
        if lowered.startswith("switchport port-security"):
            iface.port_security = True
            if "maximum " in lowered:
                iface.port_security_max = _int_or_none(lowered.split("maximum ")[1].split()[0])
            if "violation " in lowered:
                iface.port_security_violation = lowered.split("violation ")[1].split()[0]
            return True
        if lowered in ("switchport port-security", "no switchport port-security"):
            iface.port_security = lowered.startswith("switchport")
            return True
        if lowered.startswith("spanning-tree portfast"):
            iface.spanning_tree_portfast = True
            return True
        if lowered.startswith("spanning-tree bpduguard"):
            iface.spanning_tree_bpduguard = True
            return True
        if lowered.startswith("vtp domain "):
            iface.vtp_domain = line.split(None, 2)[2]
            return True
        if lowered.startswith("vtp mode "):
            iface.vtp_mode = lowered.split()[-1]
            return True
        if lowered.startswith("no ip address"):
            iface.ip = iface.mask = None
            return True
        return False

    def _apply_standby(self, record: dict[str, Any], line: str, line_no: int) -> bool:
        lowered = line.lower()
        if lowered.startswith("priority "):
            record["priority"] = _int_or_none(line.split()[-1])
            return True
        if lowered == "preempt":
            record["preempt"] = True
            return True
        if lowered.startswith("track "):
            record.setdefault("track", []).append(line.split(None, 1)[1])
            return True
        return False

    def _router_statement(
        self, router: ParsedRoutingProtocol, line: str, line_no: int
    ) -> bool:
        lowered = line.lower()
        if lowered.startswith("network "):
            parts = line.split()
            network = ParsedRoutingNetwork(line_no=line_no)
            if len(parts) >= 3:
                network.network, network.wildcard = parts[1], parts[2]
                if "area" in lowered:
                    network.area = parts[4] if len(parts) > 4 else "0"
            elif len(parts) == 2 and router.protocol in ("rip", "eigrp", "bgp"):
                network.network = parts[1]
            router.networks.append(network)
            return True
        if lowered.startswith("redistribute"):
            router.redistribute.append(line)
            return True
        if lowered.startswith("passive-interface "):
            router.passive_interfaces.append(line.split(None, 1)[1].strip())
            return True
        if lowered.startswith("neighbor "):
            parts = line.split()
            if len(parts) >= 3:
                record: dict[str, Any] = {"address": parts[1]}
                if parts[2].lower() == "remote-as" and len(parts) >= 4:
                    record["remote_as"] = _int_or_none(parts[3])
                elif parts[2].lower() == "description":
                    record["description"] = " ".join(parts[3:])
                router.neighbors.append(record)
            return True
        if lowered.startswith("default-information originate"):
            router.default_information = True
            return True
        if lowered == "no auto-summary":
            router.no_auto_summary = True
            return True
        if lowered.startswith("version "):
            router.version = _int_or_none(lowered.split()[-1])
            return True
        if lowered.startswith("router-id "):
            router.router_id = line.split()[-1]
            return True
        return False

    def _pool_statement(
        self, pool: ParsedDhcpPool, line: str, line_no: int
    ) -> bool:
        lowered = line.lower()
        if lowered.startswith("network "):
            parts = line.split()
            if len(parts) >= 3:
                pool.network, pool.mask = parts[1], parts[2]
            return True
        if lowered.startswith("default-router "):
            pool.default_router.extend(line.split()[1:])
            return True
        if lowered.startswith("dns-server "):
            pool.dns_servers.extend(line.split()[1:])
            return True
        if lowered.startswith("lease "):
            pool.lease = " ".join(line.split()[1:])
            return True
        return False

    def _global_statement(
        self, config: ParsedConfig, line: str, line_no: int, section: str
    ) -> bool:
        lowered = line.lower()
        globals_ = config.globals
        if lowered.startswith("hostname "):
            globals_.hostname = line.split(None, 1)[1].strip()
            return True
        if lowered.startswith("ip domain-name ") or lowered.startswith("ip domain name "):
            globals_.domain_name = line.split(None, 2)[-1]
            return True
        if lowered.startswith("ip default-gateway "):
            globals_.default_gateway = line.split()[-1]
            return True
        if lowered == "ip routing":
            globals_.ip_routing = True
            return True
        if lowered == "no ip routing":
            globals_.ip_routing = False
            return True
        if lowered == "ipv6 unicast-routing":
            globals_.ipv6_unicast_routing = True
            return True
        if lowered.startswith("ip route "):
            route = self._static_route(line, line_no)
            if route is not None:
                config.static_routes.append(route)
                return True
            return False
        if lowered.startswith("route ") and config.platform is Platform.ASA:
            route = self._asa_route(line, line_no)
            if route is not None:
                globals_.asa_routes.append(route)
                config.static_routes.append(route)
                return True
            return False
        if lowered.startswith("access-group "):
            parts = line.split()
            # access-group NAME in interface IFACE
            binding: dict[str, Any] = {"name": parts[1] if len(parts) > 1 else "", "line_no": line_no}
            if len(parts) >= 4 and parts[2].lower() in ("in", "out"):
                binding["direction"] = parts[2].lower()
            if "interface" in [p.lower() for p in parts]:
                idx = [p.lower() for p in parts].index("interface")
                if idx + 1 < len(parts):
                    binding["interface"] = parts[idx + 1]
            globals_.acl_bindings.append(binding)
            acl = config.acls.get(binding["name"])
            if acl is not None:
                acl.bindings.append(binding)
            return True
        if lowered.startswith("access-list "):
            return self._acl_statement(config, line, line_no)
        if lowered.startswith("ip nat "):
            nat = self._nat_statement(line, line_no)
            if nat is not None:
                config.nat.append(nat)
                return True
            return False
        if lowered.startswith("ip dhcp excluded-address"):
            parts = line.split()
            if len(parts) >= 3:
                config.excluded_addresses.append(
                    (parts[2], parts[3] if len(parts) > 3 else None)
                )
                return True
        if lowered.startswith("dhcpd address "):
            parts = line.split()
            record = {"line_no": line_no}
            if len(parts) >= 4:
                record["range"] = parts[2]
                record["interface"] = parts[3]
            globals_.asa_dhcp.append(record)
            return True
        if lowered.startswith("dhcpd enable "):
            globals_.asa_dhcp.append(
                {"line_no": line_no, "enabled": True, "interface": line.split()[-1]}
            )
            return True
        if lowered.startswith("vtp domain "):
            globals_.vtp["domain"] = line.split(None, 2)[2]
            return True
        if lowered.startswith("vtp mode "):
            globals_.vtp["mode"] = lowered.split()[-1]
            return True
        if lowered.startswith("object network ") or lowered.startswith("object-group "):
            config.globals.asa_objects.append(
                {"kind": "object-group" if lowered.startswith("object-group") else "object",
                 "name": line.split(None, 2)[2], "lines": []}
            )
            return True
        if lowered.startswith("spanning-tree "):
            entry: dict[str, Any] = {"line_no": line_no, "raw": line}
            match = re.match(
                r"^spanning-tree\s+vlan\s+(\d+)\s+root\s+(primary|secondary)",
                lowered,
            )
            if match:
                entry["vlan"] = int(match.group(1))
                entry["root"] = match.group(2)
            elif lowered.startswith("spanning-tree portfast default"):
                entry["portfast_default"] = True
            elif lowered.startswith("spanning-tree portfast bpduguard default"):
                entry["bpduguard_default"] = True
            globals_.stp.append(entry)
            return True
        if any(lowered.startswith(prefix) for prefix in _IGNORABLE_PREFIXES):
            config.ignored.append(ParsedLine(line_no, line, section, "ignored"))
            return True
        return False

    def _static_route(self, line: str, line_no: int) -> ParsedStaticRoute | None:
        parts = line.split()
        if len(parts) < 4:
            return None
        network, mask = parts[2], parts[3]
        if not (_NET_RE.match(network) and _MASK_RE.match(mask)):
            return None
        rest = parts[4:]
        next_hop = interface = None
        distance = None
        for token in rest:
            if _NET_RE.match(token):
                next_hop = token
            elif _int_or_none(token) is not None:
                distance = _int_or_none(token)
            else:
                interface = token
        return ParsedStaticRoute(
            network=network,
            mask=mask,
            next_hop=next_hop,
            interface=interface,
            distance=distance,
            line_no=line_no,
            raw=line,
        )

    def _asa_route(self, line: str, line_no: int) -> ParsedStaticRoute | None:
        # route outside 0.0.0.0 0.0.0.0 10.99.1.254 1
        parts = line.split()
        if len(parts) < 5:
            return None
        network, mask = parts[2], parts[3]
        next_hop = parts[4] if _NET_RE.match(parts[4]) else None
        interface = parts[1] if parts[1].lower() in ("inside", "outside", "dmz") else None
        distance = _int_or_none(parts[5]) if len(parts) > 5 else None
        if next_hop is None:
            return None
        return ParsedStaticRoute(
            network=network,
            mask=mask,
            next_hop=next_hop,
            interface=interface,
            distance=distance,
            line_no=line_no,
            raw=line,
        )

    def _acl_statement(self, config: ParsedConfig, line: str, line_no: int) -> bool:
        match = _ACL_LINE_RE.match(line)
        if match:
            name, kind, action, body = match.groups()
            acl = config.acls.setdefault(name, ParsedAcl(name=name, kind=kind.lower(), line_no=line_no))
            rule = parse_acl_rule(f"{action} {body}", line_no=line_no)
            if rule is None:
                return False
            acl.rules.append(rule)
            return True
        match = _ACL_IOS_RE.match(line)
        if match:
            name, action, body = match.groups()
            kind = "standard" if re.fullmatch(r"\d+", name) and int(name) < 100 else "extended"
            acl = config.acls.setdefault(name, ParsedAcl(name=name, kind=kind, line_no=line_no))
            if kind == "standard":
                # A standard ACL has no protocol operand: the body starts with
                # the source address (`access-list 10 permit 10.0.0.0 0.0.0.255`).
                tokens = body.split()
                source, wildcard, source_kind, rest = _acl_address(tokens, 0, asa=False)
                rule = ParsedAclRule(
                    action=action.lower(),
                    protocol="ip",
                    source=source,
                    source_wildcard=wildcard,
                    source_kind=source_kind,
                    destination="any",
                    destination_kind="any",
                    operands=tuple(tokens[rest:]),
                    line_no=line_no,
                    raw=f"{action} {body}",
                )
            else:
                rule = parse_acl_rule(f"{action} {body}", line_no=line_no)
            if rule is None:
                return False
            acl.rules.append(rule)
            return True
        return False

    def _nat_statement(self, line: str, line_no: int) -> ParsedNat | None:
        lowered = line.lower()
        fields: dict[str, Any] = {}
        parts = line.split()
        if lowered.startswith("ip nat inside source list "):
            # ip nat inside source list ACL interface IF overload
            if len(parts) >= 8:
                fields = {"acl": parts[5], "type": parts[6], "target": parts[7]}
                if "overload" in lowered:
                    fields["overload"] = True
            return ParsedNat("inside_source", line_no, line, fields)
        if lowered.startswith("ip nat inside source static "):
            if len(parts) >= 7:
                fields = {"inside": parts[5], "outside": parts[6]}
            return ParsedNat("inside_source_static", line_no, line, fields)
        if lowered.startswith("ip nat inside") and len(parts) == 3:
            return ParsedNat("inside", line_no, line, {})
        if lowered.startswith("ip nat outside") and len(parts) == 3:
            return ParsedNat("outside", line_no, line, {})
        if lowered.startswith("ip nat pool "):
            if len(parts) >= 6:
                fields = {"name": parts[3], "start": parts[4], "end": parts[5]}
            return ParsedNat("pool", line_no, line, fields)
        return None

    # -- post-processing ----------------------------------------------------

    def _finish(self, config: ParsedConfig) -> None:
        # A bound ACL that was never defined is a real configuration defect.
        for iface in config.interfaces.values():
            for acl_name in (iface.acl_in, iface.acl_out):
                if acl_name and config.acl(acl_name) is None:
                    config.issues.append(
                        ParserIssue(
                            "error",
                            "acl-binding-missing",
                            f"{iface.name} binds ACL {acl_name!r} which is not defined",
                            line_no=iface.line_no,
                            path=f"interface({iface.name}).acl",
                        )
                    )
        for binding in config.globals.acl_bindings:
            name = str(binding.get("name") or "")
            if name and config.acl(name) is None:
                config.issues.append(
                    ParserIssue(
                        "error",
                        "acl-binding-missing",
                        f"access-group binds ACL {name!r} which is not defined",
                        line_no=binding.get("line_no"),
                        path="globals.acl_bindings",
                    )
                )
        # Duplicate addresses are contradictory intent, not a valid two-host
        # interface; report both lines so the user can see which is stale.
        seen: dict[tuple[str, str], ParsedInterface] = {}
        for iface in config.interfaces.values():
            if not iface.ip or iface.ip == "dhcp" or not iface.mask:
                continue
            network = _parse_network(iface.ip, iface.mask)
            if network is None:
                continue
            key = (network, iface.ip)
            other = seen.get(key)
            if other is not None:
                config.issues.append(
                    ParserIssue(
                        "error",
                        "duplicate-ip",
                        f"{iface.name} and {other.name} both configure {iface.ip}/{iface.mask}",
                        line_no=iface.line_no,
                        path=f"interface({iface.name}).ip",
                    )
                )
            else:
                seen[key] = iface
        # A subinterface whose parent interface is absent cannot be reached.
        for iface in config.interfaces.values():
            if iface.subinterface_of and iface.subinterface_of not in config.interfaces:
                config.issues.append(
                    ParserIssue(
                        "warning",
                        "orphan-subinterface",
                        f"{iface.name} is a subinterface of absent {iface.subinterface_of}",
                        line_no=iface.line_no,
                        path=f"interface({iface.name})",
                    )
                )
        if config.unparsed:
            config.status = ConfigStatus.PARTIAL

    # -- public helpers -----------------------------------------------------

    def parse_extract(self, extract: ConfigExtract) -> ParsedConfig:
        config = self.parse(
            extract.raw, device=extract.device, platform=extract.platform
        )
        if extract.status is ConfigStatus.PARTIAL:
            config.status = ConfigStatus.PARTIAL
        return config


def acl_address_matches(
    value: str, wildcard: str | None, kind: str, address: str
) -> bool:
    """Does one ACL address operand match `address`?

    `kind` is the parser's classification. An object/object-group operand is
    *not* matched here: resolving it needs the ASA object table, and guessing
    that it matches would turn an unknown into a permit.
    """
    if kind == "any" or value.lower() == "any":
        return True
    if kind in ("object", "object-group"):
        return False
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    if kind == "host" or wildcard in (None, "0.0.0.0"):
        try:
            return ip == ipaddress.IPv4Address(value)
        except ValueError:
            return False
    if kind == "network" and wildcard:
        try:
            # A wildcard mask is the bitwise inverse of a subnet mask.
            mask_int = int(ipaddress.IPv4Address(wildcard))
            network = ipaddress.IPv4Network(
                f"{value}/{ipaddress.IPv4Address(0xFFFFFFFF ^ mask_int)}",
                strict=False,
            )
            return ip in network
        except ValueError:
            return False
    return False


def acl_rule_matches(
    rule: ParsedAclRule, source_ip: str, destination_ip: str, protocol: str = "icmp"
) -> bool:
    if rule.protocol not in ("ip", protocol.lower()):
        return False
    return acl_address_matches(
        rule.source, rule.source_wildcard, rule.source_kind, source_ip
    ) and acl_address_matches(
        rule.destination, rule.destination_wildcard, rule.destination_kind, destination_ip
    )


def semantic_projection(config: ParsedConfig) -> dict[str, Any]:
    """A normalized semantic view used by round-trip and repair comparison.

    It deliberately keeps only intent-bearing fields; a `service timestamps`
    line or a banner difference is not a semantic change. Unparsed lines are
    *not* included — they travel separately so an equivalence claim can never
    silently absorb the statements the parser could not model.
    """
    interfaces: dict[str, Any] = {}
    for name, iface in sorted(config.interfaces.items()):
        interfaces[name] = {
            "ip": iface.ip,
            "mask": iface.mask,
            "shutdown": iface.shutdown,
            "description": iface.description,
            "switchport_mode": iface.switchport_mode,
            "access_vlan": iface.access_vlan,
            "voice_vlan": iface.voice_vlan,
            "trunk_native": iface.trunk_native,
            "trunk_allowed": sorted(iface.trunk_allowed)
            if iface.trunk_allowed is not None
            else None,
            "encapsulation_vlan": iface.encapsulation_vlan,
            "helpers": sorted(iface.helpers),
            "acl_in": iface.acl_in,
            "acl_out": iface.acl_out,
            "nameif": iface.nameif,
            "security_level": iface.security_level,
            "nat_role": iface.nat_role,
            "standby": sorted(
                (
                    s.get("group"),
                    s.get("ip"),
                    s.get("priority"),
                    bool(s.get("preempt")),
                )
                for s in iface.standby
            ),
        }
    acls = {
        name.lower(): [
            (
                rule.action,
                rule.protocol,
                rule.source,
                rule.source_wildcard,
                rule.destination,
                rule.destination_wildcard,
                rule.operands,
            )
            for rule in acl.rules
        ]
        for name, acl in sorted(config.acls.items())
    }
    return {
        "interfaces": interfaces,
        "vlans": {v: (vlan.name or "") for v, vlan in sorted(config.vlans.items())},
        "static_routes": sorted(
            (r.cidr or "", r.next_hop or "", r.interface or "", r.distance or 0)
            for r in config.static_routes
        ),
        "routing_protocols": sorted(
            (
                p.protocol,
                str(p.process),
                tuple(sorted((n.network or "", n.wildcard or "", n.area or "") for n in p.networks)),
                tuple(sorted(p.passive_interfaces)),
            )
            for p in config.routing_protocols
        ),
        "acls": acls,
        "acl_bindings": sorted(
            (str(b.get("name")), str(b.get("direction")), str(b.get("interface")))
            for b in config.globals.acl_bindings
        ),
        "nat": sorted((n.kind, tuple(sorted(n.fields.items()))) for n in config.nat),
        "dhcp_pools": {
            name: (p.network, p.mask, tuple(sorted(p.default_router)), tuple(sorted(p.dns_servers)))
            for name, p in sorted(config.dhcp_pools.items())
        },
        "globals": {
            "default_gateway": config.globals.default_gateway,
            "ip_routing": config.globals.ip_routing,
        },
    }
