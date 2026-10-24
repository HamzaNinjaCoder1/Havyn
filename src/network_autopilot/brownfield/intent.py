"""As-is intent builder and round-trip validation (S4.4).

The builder turns an observed network into the same typed `NetworkSpec` Havyn
compiles from greenfield intent, then proves the conversion did not change the
network: it plans addressing, compiles the spec, re-parses the compiler's own
configuration, and compares the represented semantics with the original
device configuration.

Two honesty rules shape the result:

* every generated field carries a confidence (`KNOWN` read from the device,
  `PARTIAL` configured-but-not-observed or inferred from a single signal,
  `UNKNOWN` not established);
* anything the spec cannot express (ACLs, interface descriptions, extra
  routing processes, ASA objects, ...) is listed as *unsupported*, never
  silently dropped, and a round-trip that only matches because unsupported
  statements were excluded is reported as EQUIVALENT — not EXACT.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..compiler.compiler import CompileError, compile_spec
from ..core.network_spec import (
    AcceptanceCriterionSpec,
    AclRuleSpec,
    AsaDhcpSpec,
    AsaInterfaceSpec,
    AsaNatSpec,
    AsaObjectGroupSpec,
    BgpDeviceSpec,
    BgpNeighborSpec,
    BgpSpec,
    DeviceRole,
    DeviceSpec,
    DhcpPoolSpec,
    DhcpRelaySpec,
    EndpointSpec,
    EigrpSpec,
    EtherChannelSpec,
    FlowTestSpec,
    HostDnsSpec,
    HsrpSpec,
    IpAssignmentSpec,
    LinkSpec,
    ManagementIpSpec,
    NatSpec,
    NetworkSpec,
    OspfNetworkSpec,
    OspfSpec,
    PolicySpec,
    PortSecuritySpec,
    ProvenanceSpec,
    RequirementSpec,
    RipSpec,
    RoutingSpec,
    StaticRouteSpec,
    StpSpec,
    SubnetSpec,
    SviSpec,
    SwitchingSpec,
    VlanSpec,
    VoiceVlanSpec,
    VtpSpec,
    FirewallFeatureSpec,
)
from ..ipam.planner import PlanningFailure, plan_addressing
from .config_reader import ConfigCorpus
from .importer import ImportResult
from .models import (
    BrownfieldInventory,
    Confidence,
    DeviceInventory,
    FieldConfidence,
    HOST_CATEGORIES,
    InterfaceInventory,
    Platform,
)
from .parser import IosConfigParser, ParsedConfig, semantic_projection


_ROLE_MAP: dict[str, DeviceRole] = {
    "router": DeviceRole.ROUTER,
    "switch": DeviceRole.SWITCH,
    "firewall": DeviceRole.FIREWALL,
    "wireless_router": DeviceRole.WIRELESS_ROUTER,
    "pc": DeviceRole.PC,
    "laptop": DeviceRole.LAPTOP,
    "server": DeviceRole.SERVER,
    "access_point": DeviceRole.ACCESS_POINT,
    "cloud": DeviceRole.CLOUD,
    "modem": DeviceRole.MODEM,
    "printer": DeviceRole.PRINTER,
    "ip_phone": DeviceRole.IP_PHONE,
    "tablet": DeviceRole.TABLET,
    "smartphone": DeviceRole.SMARTPHONE,
    "wlc": DeviceRole.WLC,
    "iot": DeviceRole.IOT,
    "other": DeviceRole.OTHER,
}


@dataclass
class AsIsIntent:
    """The spec built from an import, with its confidence and its gaps."""

    spec: NetworkSpec
    confidence: list[FieldConfidence] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    inventory_summary: dict[str, Any] = field(default_factory=dict)

    def confidence_for(self, path: str) -> Confidence:
        for entry in self.confidence:
            if entry.path == path:
                return entry.confidence
        return Confidence.UNKNOWN

    @property
    def counts(self) -> dict[str, int]:
        out = {c.value: 0 for c in Confidence}
        for entry in self.confidence:
            out[entry.confidence.value] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.model_dump(mode="json"),
            "confidence": [e.to_dict() for e in self.confidence],
            "confidence_counts": self.counts,
            "unsupported": list(self.unsupported),
            "contradictions": list(self.contradictions),
            "assumptions": list(self.assumptions),
            "inventory_summary": dict(self.inventory_summary),
        }


# -- helpers ------------------------------------------------------------------


def _wildcard_cidr(network: str | None, wildcard: str | None) -> str | None:
    if not network:
        return None
    try:
        if wildcard:
            mask_int = int(ipaddress.IPv4Address(wildcard))
            prefix = ipaddress.IPv4Address(0xFFFFFFFF ^ mask_int)
            return str(ipaddress.IPv4Network(f"{network}/{prefix}", strict=False))
        return str(ipaddress.IPv4Network(network, strict=False))
    except ValueError:
        return None


def _netmask(prefix: int) -> str:
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)


def _subnet_id(network: str, prefix: int) -> str:
    base = str(network).split("/", 1)[0].replace(".", "-")
    return f"sn-{base}-{prefix}"


def _role_for(device: DeviceInventory) -> tuple[DeviceRole, Confidence, str]:
    role_value = str(device.role.value) if device.role.is_known else ""
    if role_value == "l3_switch":
        return DeviceRole.L3_SWITCH, Confidence.KNOWN, "running-config: ip routing"
    role = _ROLE_MAP.get(device.category) or _ROLE_MAP.get(role_value)
    if role is not None:
        return role, Confidence.KNOWN, f"catalog category {device.category!r}"
    return DeviceRole.OTHER, Confidence.UNKNOWN, "category unknown"


def _configured_only(interface: InterfaceInventory, field_name: str) -> bool:
    obs = getattr(interface, field_name)
    return obs.is_known and (obs.source or "").startswith("running-config")


def _ipv4(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if "/" in text:
        text = text.split("/", 1)[0]
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        return None
    if str(address) == "0.0.0.0":
        return None
    return str(address)


@dataclass
class _Address:
    device: str
    interface: str
    ip: str
    mask: str
    network: str
    prefix: int
    source: str
    vlan: int | None
    category: str
    host_gateway: str | None = None
    dhcp: bool = False


class AsIsIntentBuilder:
    """Build a `NetworkSpec` (plus confidence) from a brownfield import."""

    def __init__(
        self,
        *,
        name: str | None = None,
        requirements: Iterable[RequirementSpec] | None = None,
        acceptance: Iterable[AcceptanceCriterionSpec] | None = None,
    ):
        self.name = name
        self.requirements = list(requirements or [])
        self.acceptance = list(acceptance or [])

    def build(self, imported: ImportResult) -> AsIsIntent:
        inventory = imported.inventory
        corpus = imported.corpus
        result = AsIsIntent(
            spec=NetworkSpec(
                name=self.name or inventory.name or "brownfield as-is",
                description=(
                    "As-is intent reconstructed from an existing Packet Tracer "
                    "network. Fields are annotated with their confidence in the "
                    "companion report."
                ),
                # A real spec needs at least one device. The placeholder is
                # replaced in the first model_copy below and never escapes
                # this method; the final spec is re-validated before return.
                devices=[
                    DeviceSpec(id="placeholder", role=DeviceRole.OTHER)
                ],
                provenance=ProvenanceSpec(
                    author="brownfield-importer",
                    generator="network_autopilot.brownfield",
                    notes=(
                        f"imported {inventory.observed_at} from "
                        f"{inventory.source}"
                    ),
                ),
            ),
            inventory_summary=inventory.summary(),
        )

        devices, device_confidence = self._devices(inventory, result)
        result.confidence.extend(device_confidence)
        result.spec = result.spec.model_copy(update={"devices": devices})

        addresses = self._addresses(inventory, result)
        subnets, vlan_ids, subnet_confidence = self._subnets(
            inventory, addresses, result
        )
        result.confidence.extend(subnet_confidence)
        vlans, vlan_confidence = self._vlans(inventory, corpus, vlan_ids, result)
        result.confidence.extend(vlan_confidence)
        result.spec = result.spec.model_copy(
            update={"subnets": subnets, "vlans": vlans}
        )

        links, link_confidence = self._links(inventory, result)
        result.confidence.extend(link_confidence)

        assignments, switching, switching_confidence = self._addressing_and_switching(
            inventory, corpus, addresses, subnets, result
        )
        result.confidence.extend(switching_confidence)

        routing, routing_confidence = self._routing(inventory, corpus, addresses, subnets, result)
        result.confidence.extend(routing_confidence)
        self._detect_unmodeled(corpus, result)
        # Unsupported statements have a hard ceiling so a pathological config
        # cannot make the report unusable; the raw file remains the full record.
        result.unsupported = list(dict.fromkeys(result.unsupported))

        host_dns, services, firewall, wireless = self._host_and_services(
            inventory, corpus, subnets, result
        )
        for entry in wireless:
            result.confidence.append(entry)

        result.spec = result.spec.model_copy(
            update={
                "links": links,
                "ip_assignments": assignments,
                "routing": routing,
                "switching": switching,
                "host_dns": host_dns,
                "services": services,
                "firewall": firewall,
                "requirements": self.requirements,
                "acceptance_criteria": self.acceptance,
                "assumptions": list(dict.fromkeys(result.assumptions)),
                "provenance": result.spec.provenance.model_copy(
                    update={
                        "notes": (
                            f"imported {inventory.observed_at} from "
                            f"{inventory.source}; "
                            f"{len(result.unsupported)} unsupported statement(s) "
                            "preserved outside the spec"
                        )
                    }
                ),
            }
        )
        # `model_copy` skips validation by design; the as-is spec must still
        # pass the same structural wall every other spec passes. A network
        # this builder cannot express raises here, with Pydantic's reason,
        # instead of producing a spec that fails later in the compiler.
        result.spec = NetworkSpec.model_validate(result.spec.model_dump(mode="json"))
        return result

    # -- devices ------------------------------------------------------------

    def _devices(
        self, inventory: BrownfieldInventory, result: AsIsIntent
    ) -> tuple[list[DeviceSpec], list[FieldConfidence]]:
        devices: list[DeviceSpec] = []
        confidence: list[FieldConfidence] = []
        for name in inventory.device_names():
            device = inventory.devices[name]
            role, role_conf, role_source = _role_for(device)
            model_hint = str(device.model.value) if device.model.is_known else None
            asa_interfaces: list[AsaInterfaceSpec] = []
            if role is DeviceRole.FIREWALL:
                for iface in device.interfaces.values():
                    if iface.nameif.is_known and iface.security_level.is_known:
                        asa_interfaces.append(
                            AsaInterfaceSpec(
                                interface=iface.name,
                                nameif=str(iface.nameif.value),
                                security_level=int(iface.security_level.value),
                            )
                        )
            devices.append(
                DeviceSpec(
                    id=name,
                    role=role,
                    model_hint=model_hint,
                    asa_interfaces=asa_interfaces,
                    description=None,
                )
            )
            if role_conf is not Confidence.KNOWN:
                confidence.append(
                    FieldConfidence(
                        f"device({name}).role", role_conf, role_source,
                        "device category could not be resolved",
                    )
                )
            if model_hint:
                confidence.append(
                    FieldConfidence(
                        f"device({name}).model", Confidence.KNOWN,
                        "topology read-back",
                    )
                )
            else:
                confidence.append(
                    FieldConfidence(
                        f"device({name}).model", Confidence.UNKNOWN,
                        "topology read-back",
                        "model was not reported",
                    )
                )
            if device.position.is_known:
                confidence.append(
                    FieldConfidence(
                        f"device({name}).position", Confidence.KNOWN, "canvas"
                    )
                )
            else:
                confidence.append(
                    FieldConfidence(
                        f"device({name}).position", Confidence.UNKNOWN, "canvas",
                        "position not reported",
                    )
                )
            if asa_interfaces:
                result.assumptions.append(
                    f"{name}: ASA nameif/security-level taken from running-config"
                )
        return devices, confidence

    # -- addresses and subnets ---------------------------------------------

    def _addresses(
        self, inventory: BrownfieldInventory, result: AsIsIntent
    ) -> list[_Address]:
        addresses: list[_Address] = []
        for device in inventory.devices.values():
            host_gateway = (
                str(device.host.gateway.value)
                if device.host is not None and device.host.gateway.is_known
                else None
            )
            host_dhcp = (
                bool(device.host.dhcp.value)
                if device.host is not None and device.host.dhcp.is_known
                else False
            )
            seen_primary = False
            for interface in device.interfaces.values():
                if device.is_host and host_dhcp:
                    # A lease is not a static assignment: the host's observed
                    # address is evidence that DHCP worked, not intent to pin
                    # that address. Representing it as static would silently
                    # change the design on the next build.
                    break
                ip = _ipv4(interface.ip.value if interface.ip.is_known else None)
                mask = (
                    str(interface.mask.value)
                    if interface.mask.is_known and interface.mask.value
                    else None
                )
                if not ip or not mask:
                    continue
                try:
                    prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                    network = str(
                        ipaddress.IPv4Network(f"{ip}/{mask}", strict=False).network_address
                    )
                except ValueError:
                    result.contradictions.append(
                        f"{device.name}:{interface.name} has invalid "
                        f"address {ip}/{mask}"
                    )
                    continue
                source = interface.ip.source or interface.mask.source or "observation"
                vlan = None
                if interface.vlan.is_known:
                    vlan = int(interface.vlan.value)
                elif interface.encapsulation_vlan.is_known:
                    vlan = int(interface.encapsulation_vlan.value)
                gateway = host_gateway if not seen_primary and device.is_host else None
                addresses.append(
                    _Address(
                        device=device.name,
                        interface=interface.name,
                        ip=ip,
                        mask=mask,
                        network=network,
                        prefix=prefix,
                        source=source,
                        vlan=vlan,
                        category=device.category,
                        host_gateway=gateway,
                        dhcp=False,
                    )
                )
                if device.is_host:
                    seen_primary = True
            if device.is_host and host_dhcp:
                interface = next(
                    (
                        i.name for i in device.interfaces.values()
                        if not i.name.lower().startswith(("bluetooth", "wireless"))
                    ),
                    "FastEthernet0",
                )
                addresses.append(
                    _Address(
                        device=device.name,
                        interface=interface,
                        ip="",
                        mask="",
                        network="",
                        prefix=0,
                        source="ipconfig:/all",
                        vlan=None,
                        category=device.category,
                        host_gateway=host_gateway,
                        dhcp=True,
                    )
                )
        return addresses

    def _subnets(
        self,
        inventory: BrownfieldInventory,
        addresses: list[_Address],
        result: AsIsIntent,
    ) -> tuple[list[SubnetSpec], set[int], list[FieldConfidence]]:
        groups: dict[tuple[str, int], list[_Address]] = {}
        for address in addresses:
            if address.dhcp:
                continue
            groups.setdefault((address.network, address.prefix), []).append(address)
        subnets: list[SubnetSpec] = []
        confidence: list[FieldConfidence] = []
        vlan_ids: set[int] = set()
        dhcp_subnets: set[tuple[str, int]] = set()
        for address in addresses:
            if address.dhcp and address.host_gateway:
                for key in groups:
                    try:
                        net = ipaddress.IPv4Network(f"{key[0]}/{key[1]}")
                        if ipaddress.IPv4Address(address.host_gateway) in net:
                            dhcp_subnets.add(key)
                    except ValueError:
                        continue
        for (network, prefix), records in sorted(groups.items()):
            cidr = f"{network}/{prefix}"
            vlan_values = {r.vlan for r in records if r.vlan is not None}
            vlan_id: int | None = None
            if len(vlan_values) == 1:
                vlan_id = vlan_values.pop()
            elif len(vlan_values) > 1:
                result.contradictions.append(
                    f"subnet {cidr} is associated with several VLANs "
                    f"{sorted(vlan_values)}; no VLAN is asserted for it"
                )
            # Gateway evidence: observed host default gateways first, then the
            # only routed address in the subnet. Two disagreeing gateways are
            # a contradiction, not a majority vote.
            observed_gateways = {
                r.host_gateway for r in records if r.host_gateway
            }
            device_addresses = {
                r.ip for r in records
                if r.category in ("router", "switch", "firewall", "wireless_router")
            }
            gateway: str | None = None
            gateway_conf = Confidence.UNKNOWN
            gateway_source = "not observed"
            if len(observed_gateways) == 1:
                gateway = observed_gateways.pop()
                if device_addresses and gateway not in device_addresses:
                    result.contradictions.append(
                        f"hosts in {cidr} use gateway {gateway}, which is not an "
                        f"observed interface address ({sorted(device_addresses)})"
                    )
                    gateway_conf = Confidence.PARTIAL
                else:
                    gateway_conf = Confidence.KNOWN
                    gateway_source = "host ipconfig /all"
            elif len(observed_gateways) > 1:
                result.contradictions.append(
                    f"hosts in {cidr} report different gateways "
                    f"{sorted(observed_gateways)}"
                )
            elif len(device_addresses) == 1:
                gateway = device_addresses.pop()
                gateway_conf = Confidence.PARTIAL
                gateway_source = "only routed address in the subnet"
                result.assumptions.append(
                    f"{cidr}: gateway inferred from the only routed interface "
                    f"address ({gateway})"
                )
            dhcp = (network, prefix) in dhcp_subnets
            subnets.append(
                SubnetSpec(
                    id=_subnet_id(network, prefix),
                    cidr=cidr,
                    vlan_id=vlan_id,
                    gateway=gateway,
                    dhcp=False,
                )
            )
            if vlan_id is not None:
                vlan_ids.add(vlan_id)
            all_observed = all(
                not r.source.startswith("running-config") for r in records
            )
            confidence.append(
                FieldConfidence(
                    f"subnet({cidr}).cidr",
                    Confidence.KNOWN if all_observed else Confidence.PARTIAL,
                    "topology read-back" if all_observed else "running-config",
                )
            )
            confidence.append(
                FieldConfidence(
                    f"subnet({cidr}).gateway", gateway_conf, gateway_source
                )
            )
            if dhcp:
                result.assumptions.append(
                    f"{cidr}: DHCP is in use (observed host lease state), but "
                    "the serving pool is represented separately if it was read"
                )
        return subnets, vlan_ids, confidence

    # -- VLANs --------------------------------------------------------------

    def _vlans(
        self,
        inventory: BrownfieldInventory,
        corpus: ConfigCorpus,
        referenced: set[int],
        result: AsIsIntent,
    ) -> tuple[list[VlanSpec], list[FieldConfidence]]:
        names: dict[int, str | None] = {}
        sources: dict[int, str] = {}
        for device in inventory.devices.values():
            if device.vlans_table.is_known:
                for row in device.vlans_table.value or []:
                    if not isinstance(row, dict):
                        continue
                    try:
                        vlan_id = int(row.get("id"))
                    except (TypeError, ValueError):
                        continue
                    names.setdefault(vlan_id, row.get("name"))
                    sources.setdefault(vlan_id, "show vlan brief")
        for device_name, parsed in corpus.parsed.items():
            for vlan_id, vlan in parsed.vlans.items():
                names.setdefault(vlan_id, vlan.name)
                sources.setdefault(vlan_id, f"running-config:{device_name}")
        for vlan_id in referenced:
            names.setdefault(vlan_id, None)
            sources.setdefault(vlan_id, "referenced by an interface")
        vlans: list[VlanSpec] = []
        confidence: list[FieldConfidence] = []
        for vlan_id in sorted(names):
            name = names[vlan_id]
            confidence_value = Confidence.KNOWN
            if not name:
                # VlanSpec requires a name. The placeholder is explicit and
                # reported as PARTIAL; the spec must not claim a name the
                # network never had.
                name = f"VLAN{vlan_id:04d}"
                confidence_value = Confidence.PARTIAL
                result.assumptions.append(
                    f"VLAN {vlan_id}: no name was observed; {name!r} is a "
                    "representation placeholder, not a configured name"
                )
            elif sources.get(vlan_id, "").startswith("referenced"):
                confidence_value = Confidence.PARTIAL
            vlans.append(VlanSpec(vlan_id=vlan_id, name=name))
            confidence.append(
                FieldConfidence(
                    f"vlan({vlan_id}).name", confidence_value,
                    sources.get(vlan_id, "not observed"),
                )
            )
            if vlan_id not in referenced and sources.get(vlan_id, "").startswith("referenced"):
                pass
        for vlan_id in sorted(referenced - set(names)):
            vlans.append(VlanSpec(vlan_id=vlan_id, name=f"VLAN{vlan_id:04d}"))
            confidence.append(
                FieldConfidence(
                    f"vlan({vlan_id}).name", Confidence.PARTIAL,
                    "referenced by an interface",
                )
            )
        return vlans, confidence

    # -- links --------------------------------------------------------------

    def _links(
        self, inventory: BrownfieldInventory, result: AsIsIntent
    ) -> tuple[list[LinkSpec], list[FieldConfidence]]:
        links: list[LinkSpec] = []
        confidence: list[FieldConfidence] = []
        seen: set[str] = set()
        for link in inventory.links:
            if link.id in seen:
                continue
            seen.add(link.id)
            a_iface = inventory.device(link.a_device).interface(link.a_port) if inventory.device(link.a_device) else None
            b_iface = inventory.device(link.b_device).interface(link.b_port) if inventory.device(link.b_device) else None
            if a_iface is None or b_iface is None:
                result.unsupported.append(
                    f"link {link.id}: an endpoint interface was not observed; "
                    "it is not represented in the spec"
                )
                continue
            media = "auto"
            for iface in (a_iface, b_iface):
                if iface.media.is_known and iface.media.value == "wireless":
                    media = "wireless"
                    break
                if iface.media.is_known and iface.media.value == "serial":
                    media = "serial"
            trunk = any(
                (i.trunk.is_known and bool(i.trunk.value))
                or (i.allowed_vlans.is_known)
                or (i.encapsulation_vlan.is_known)
                for i in (a_iface, b_iface)
            )
            allowed: list[int] | None = None
            native: int | None = None
            for iface in (a_iface, b_iface):
                if iface.allowed_vlans.is_known and allowed is None:
                    allowed = sorted({int(v) for v in iface.allowed_vlans.value or []})
                if iface.native_vlan.is_known and native is None:
                    native = int(iface.native_vlan.value)
            links.append(
                LinkSpec(
                    id=link.id,
                    a=EndpointSpec(device=link.a_device, interface_hint=link.a_port),
                    b=EndpointSpec(device=link.b_device, interface_hint=link.b_port),
                    media=media,
                    trunk=trunk,
                    native_vlan=native,
                    allowed_vlans=allowed,
                )
            )
            confidence.append(
                FieldConfidence(
                    f"link({link.id}).endpoints", Confidence.KNOWN,
                    "topology read-back: both cable endpoints",
                )
            )
            if link.media.is_known:
                confidence.append(
                    FieldConfidence(
                        f"link({link.id}).media", Confidence.PARTIAL,
                        "interface family",
                        "the cable medium itself is not exposed by PT",
                    )
                )
            else:
                confidence.append(
                    FieldConfidence(
                        f"link({link.id}).media", Confidence.UNKNOWN,
                        "topology read-back",
                    )
                )
            if allowed is not None:
                result.assumptions.append(
                    f"link {link.id}: trunk allowed VLANs taken from the "
                    "interface configuration"
                )
        return links, confidence

    # -- addressing + switching --------------------------------------------

    def _addressing_and_switching(
        self,
        inventory: BrownfieldInventory,
        corpus: ConfigCorpus,
        addresses: list[_Address],
        subnets: list[SubnetSpec],
        result: AsIsIntent,
    ) -> tuple[list[IpAssignmentSpec], SwitchingSpec, list[FieldConfidence]]:
        by_subnet: dict[str, SubnetSpec] = {s.id: s for s in subnets}
        network_to_subnet: dict[tuple[str, int], str] = {}
        for subnet in subnets:
            try:
                net = ipaddress.IPv4Network(subnet.cidr or "", strict=False)
            except ValueError:
                continue
            network_to_subnet[(str(net.network_address), net.prefixlen)] = subnet.id
        assignments: list[IpAssignmentSpec] = []
        svis: list[SviSpec] = []
        management: list[ManagementIpSpec] = []
        etherchannels: list[Any] = []
        stp_specs: list[Any] = []
        port_security: list[Any] = []
        vtp_specs: list[Any] = []
        voice_vlans: list[Any] = []
        confidence: list[FieldConfidence] = []
        handled_interfaces: set[tuple[str, str]] = set()

        for address in addresses:
            device = inventory.device(address.device)
            if device is None:
                continue
            role, _, _ = _role_for(device)
            key = (address.device, address.interface)
            if address.dhcp:
                if device.is_host:
                    host_ip = (
                        str(device.host.ip.value)
                        if device.host is not None and device.host.ip.is_known
                        else None
                    )
                    subnet_id = (
                        self._subnet_for_gateway(addresses, subnets, address.host_gateway)
                        or self._subnet_for_ip(subnets, host_ip)
                    )
                    if subnet_id is None:
                        result.unsupported.append(
                            f"{address.device}: DHCP client with no subnet that "
                            "matches its gateway or leased address"
                        )
                        continue
                    assignments.append(
                        IpAssignmentSpec(
                            device=address.device,
                            interface_hint=address.interface,
                            subnet=subnet_id,
                            ip=None,
                            dhcp=True,
                            gateway=address.host_gateway,
                        )
                    )
                    if address.host_gateway:
                        result.assumptions.append(
                            f"{address.device}: DHCP client; its observed gateway "
                            f"{address.host_gateway} is recorded on the assignment"
                        )
                continue
            subnet = None
            if address.host_gateway:
                subnet = self._subnet_for_gateway(addresses, subnets, address.host_gateway)
            subnet = subnet or network_to_subnet.get((address.network, address.prefix))
            if subnet is None:
                result.unsupported.append(
                    f"{address.device}:{address.interface} {address.ip}/{address.mask} "
                    "does not map to an imported subnet"
                )
                continue
            if key in handled_interfaces:
                continue
            handled_interfaces.add(key)
            if role is DeviceRole.SWITCH and address.interface.lower().startswith("vlan"):
                vlan_id = int(address.interface[4:] or 0)
                gateway = (
                    str(device.default_gateway.value)
                    if device.default_gateway.is_known
                    else None
                )
                management.append(
                    ManagementIpSpec(
                        device=address.device,
                        vlan=vlan_id,
                        ip=address.ip,
                        mask=address.mask,
                        gateway=gateway,
                    )
                )
                confidence.append(
                    FieldConfidence(
                        f"switch({address.device}).management",
                        Confidence.KNOWN if address.source != "running-config"
                        else Confidence.PARTIAL,
                        address.source,
                    )
                )
                continue
            if role is DeviceRole.L3_SWITCH and address.interface.lower().startswith("vlan"):
                vlan_id = int(address.interface[4:] or 0)
                svis.append(
                    SviSpec(device=address.device, vlan=vlan_id, ip=address.ip,
                            mask=address.mask, no_shutdown=True)
                )
                confidence.append(
                    FieldConfidence(
                        f"switch({address.device}).svi({vlan_id})",
                        Confidence.KNOWN if address.source != "running-config"
                        else Confidence.PARTIAL,
                        address.source,
                    )
                )
                continue
            if role is DeviceRole.SWITCH:
                result.unsupported.append(
                    f"{address.device}:{address.interface} is a routed port on a "
                    "switch; the spec represents switch addresses as SVIs"
                )
                continue
            assignments.append(
                IpAssignmentSpec(
                    device=address.device,
                    interface_hint=address.interface,
                    subnet=subnet,
                    ip=address.ip,
                    dhcp=False,
                    gateway=address.host_gateway,
                )
            )
            confidence.append(
                FieldConfidence(
                    f"device({address.device}).port({address.interface}).ip",
                    Confidence.KNOWN if address.source != "running-config"
                    else Confidence.PARTIAL,
                    address.source,
                )
            )
            if address.source.startswith("running-config"):
                result.assumptions.append(
                    f"{address.device}:{address.interface} address is configured "
                    "but was not read from the live port"
                )

        # Switch feature configuration, from parsed configs.
        for device_name, parsed in corpus.parsed.items():
            device = inventory.device(device_name)
            if device is None:
                continue
            role, _, _ = _role_for(device)
            # EtherChannel bundles: one per (group), members in interface order.
            groups: dict[int, list[str]] = {}
            modes: dict[int, str] = {}
            for iface in parsed.interfaces.values():
                if iface.channel_group is not None:
                    groups.setdefault(iface.channel_group, []).append(iface.name)
                    if iface.channel_mode:
                        modes[iface.channel_group] = iface.channel_mode
            for group, members in sorted(groups.items()):
                if len(members) >= 2:
                    etherchannels.append(
                        {
                            "device": device_name,
                            "group": group,
                            "members": sorted(members),
                            "mode": modes.get(group, "active"),
                        }
                    )
                else:
                    result.unsupported.append(
                        f"{device_name}: channel-group {group} has one member; "
                        "it is preserved in the raw config, not in the spec"
                    )
            for iface in parsed.interfaces.values():
                if iface.port_security:
                    port_security.append(
                        {
                            "device": device_name,
                            "interface": iface.name,
                            "maximum": iface.port_security_max or 1,
                            "violation": iface.port_security_violation or "shutdown",
                        }
                    )
                if iface.voice_vlan is not None:
                    voice_vlans.append(
                        {
                            "device": device_name,
                            "interface": iface.name,
                            "voice_vlan": iface.voice_vlan,
                            "access_vlan": iface.access_vlan,
                        }
                    )
                if iface.spanning_tree_portfast or iface.spanning_tree_bpduguard:
                    stp_specs.append(
                        {
                            "device": device_name,
                            "vlan": (iface.vlan if iface.vlan is not None else 1),
                            "portfast": [iface.name] if iface.spanning_tree_portfast else [],
                            "bpduguard": [iface.name] if iface.spanning_tree_bpduguard else [],
                        }
                    )
            for entry in parsed.globals.stp:
                if entry.get("root"):
                    stp_specs.append(
                        {
                            "device": device_name,
                            "vlan": entry.get("vlan") or 1,
                            "root": entry["root"],
                            "portfast": [],
                            "bpduguard": [],
                        }
                    )
            if parsed.globals.vtp.get("domain") or parsed.globals.vtp.get("mode"):
                vtp_specs.append(
                    {
                        "device": device_name,
                        "domain": parsed.globals.vtp.get("domain") or "default",
                        "mode": parsed.globals.vtp.get("mode") or "server",
                    }
                )

        # Merge duplicate STP records for the same device/VLAN.
        merged_stp: dict[tuple[str, int], dict[str, Any]] = {}
        for record in stp_specs:
            key = (record["device"], int(record["vlan"]))
            entry = merged_stp.setdefault(
                key,
                {
                    "device": record["device"],
                    "vlan": int(record["vlan"]),
                    "root": None,
                    "portfast": [],
                    "bpduguard": [],
                },
            )
            if record.get("root"):
                entry["root"] = record["root"]
            entry["portfast"].extend(record.get("portfast") or [])
            entry["bpduguard"].extend(record.get("bpduguard") or [])

        switching = SwitchingSpec(
            svis=svis,
            management_ips=management,
            etherchannels=[EtherChannelSpec(**r) for r in etherchannels],
            stp=[StpSpec(**r) for r in merged_stp.values()],
            port_security=[PortSecuritySpec(**r) for r in port_security],
            vtp=[VtpSpec(**r) for r in vtp_specs],
            voice_vlans=[VoiceVlanSpec(**r) for r in voice_vlans],
        )
        return assignments, switching, confidence

    @staticmethod
    def _subnet_for_gateway(
        addresses: list[_Address], subnets: list[SubnetSpec], gateway: str | None
    ) -> str | None:
        if not gateway:
            return None
        try:
            ip = ipaddress.IPv4Address(gateway)
        except ValueError:
            return None
        for subnet in subnets:
            try:
                if ip in ipaddress.IPv4Network(subnet.cidr or "", strict=False):
                    return subnet.id
            except ValueError:
                continue
        return None

    @staticmethod
    def _subnet_for_ip(subnets: list[SubnetSpec], address: str | None) -> str | None:
        if not address:
            return None
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            return None
        for subnet in subnets:
            try:
                if ip in ipaddress.IPv4Network(subnet.cidr or "", strict=False):
                    return subnet.id
            except ValueError:
                continue
        return None

    # -- routing ------------------------------------------------------------

    def _routing(
        self,
        inventory: BrownfieldInventory,
        corpus: ConfigCorpus,
        addresses: list[_Address],
        subnets: list[SubnetSpec],
        result: AsIsIntent,
    ) -> tuple[RoutingSpec, list[FieldConfidence]]:
        routing = RoutingSpec()
        confidence: list[FieldConfidence] = []
        # Static routes
        seen_static: set[tuple[str, str, str, str]] = set()
        static_routes: list[StaticRouteSpec] = []
        for device_name, parsed in corpus.parsed.items():
            for route in parsed.static_routes:
                cidr = route.cidr
                if not cidr:
                    result.unsupported.append(
                        f"{device_name}: static route {route.network} {route.mask} "
                        "could not be normalised"
                    )
                    continue
                if not route.next_hop and not route.interface:
                    result.unsupported.append(
                        f"{device_name}: static route to {cidr} has no next hop"
                    )
                    continue
                key = (device_name, cidr, route.next_hop or "", route.interface or "")
                if key in seen_static:
                    continue
                seen_static.add(key)
                static_routes.append(
                    StaticRouteSpec(
                        device=device_name,
                        destination=cidr,
                        next_hop=route.next_hop,
                        exit_interface=route.interface,
                    )
                )
                confidence.append(
                    FieldConfidence(
                        f"route({device_name},{cidr})", Confidence.PARTIAL,
                        "running-config",
                        "configured route, not an observed forwarding entry",
                    )
                )
        # Routing protocols, grouped by type. A NetworkSpec has one mode; the
        # others are preserved as unsupported, never silently dropped.
        protocols: dict[str, list[tuple[str, Any]]] = {}
        for device_name, parsed in corpus.parsed.items():
            for protocol in parsed.routing_protocols:
                protocols.setdefault(protocol.protocol, []).append((device_name, protocol))
        chosen = next(
            (mode for mode in ("ospf", "eigrp", "rip", "bgp") if protocols.get(mode)),
            None,
        )
        ospf = eigrp = rip = bgp = None
        if chosen == "ospf":
            entries = protocols["ospf"]
            process_id = int(entries[0][1].process or 1)
            area_networks: list[OspfNetworkSpec] = []
            devices: list[str] = []
            default_information = False
            for device_name, protocol in entries:
                devices.append(device_name)
                if int(protocol.process or 1) != process_id:
                    result.unsupported.append(
                        f"{device_name}: OSPF process {protocol.process} is not "
                        f"process {process_id}; preserved in the raw config"
                    )
                default_information = default_information or protocol.default_information
                for network in protocol.networks:
                    cidr = _wildcard_cidr(network.network, network.wildcard)
                    if cidr:
                        area_networks.append(
                            OspfNetworkSpec(network=cidr, area=network.area or "0")
                        )
            if area_networks:
                ospf = OspfSpec(
                    process_id=process_id,
                    area_networks=area_networks,
                    devices=sorted(devices),
                    default_information_originate=default_information,
                )
                confidence.append(
                    FieldConfidence(
                        "routing.ospf", Confidence.PARTIAL, "running-config",
                        "protocol presence is configured, neighbour state is not observed",
                    )
                )
        elif chosen == "eigrp":
            entries = protocols["eigrp"]
            as_number = int(entries[0][1].process or 1)
            networks: list[str] = []
            passive: list[str] = []
            devices = []
            for device_name, protocol in entries:
                devices.append(device_name)
                passive.extend(protocol.passive_interfaces)
                for network in protocol.networks:
                    cidr = _wildcard_cidr(network.network, network.wildcard)
                    if cidr:
                        networks.append(cidr)
            if networks:
                eigrp = EigrpSpec(
                    as_number=as_number, networks=sorted(set(networks)),
                    passive_interfaces=sorted(set(passive)), devices=sorted(devices),
                )
        elif chosen == "rip":
            entries = protocols["rip"]
            networks = []
            passive = []
            versions = [e[1].version for e in entries if e[1].version]
            no_auto = all(e[1].no_auto_summary for e in entries)
            devices = []
            for device_name, protocol in entries:
                devices.append(device_name)
                passive.extend(protocol.passive_interfaces)
                for network in protocol.networks:
                    cidr = _wildcard_cidr(network.network, network.wildcard)
                    if cidr:
                        networks.append(cidr)
            if networks:
                rip = RipSpec(
                    version=max(versions) if versions else 2,
                    networks=sorted(set(networks)),
                    no_auto_summary=no_auto,
                    passive_interfaces=sorted(set(passive)),
                    devices=sorted(devices),
                )
        elif chosen == "bgp":
            entries = protocols["bgp"]
            per_device = []
            for device_name, protocol in entries:
                per_device.append(
                    BgpDeviceSpec(
                        device=device_name,
                        as_number=int(protocol.process or 1),
                        neighbors=[
                            BgpNeighborSpec(
                                address=str(n.get("address")),
                                remote_as=int(n.get("remote_as") or 1),
                                description=n.get("description"),
                            )
                            for n in protocol.neighbors
                            if n.get("address") and n.get("remote_as")
                        ],
                        networks=[
                            cidr for cidr in (
                                _wildcard_cidr(n.network, n.wildcard)
                                for n in protocol.networks
                            ) if cidr
                        ],
                        router_id=protocol.router_id,
                    )
                )
            bgp = BgpSpec(
                as_number=per_device[0].as_number if per_device else 1,
                per_device=per_device,
                devices=[p.device for p in per_device],
            )
        for other in ("ospf", "eigrp", "rip", "bgp"):
            if other != chosen and protocols.get(other):
                result.unsupported.append(
                    f"{other.upper()} is also configured on "
                    f"{sorted({e[0] for e in protocols[other]})}; a NetworkSpec "
                    "carries one routing mode, so those processes are preserved "
                    "in the raw configuration and not represented in the spec"
                )

        # DHCP pools (router) and relays, HSRP and NAT.
        dhcp_pools: list[DhcpPoolSpec] = []
        relays: list[DhcpRelaySpec] = []
        hsrp: list[HsrpSpec] = []
        nat: list[NatSpec] = []
        for device_name, parsed in corpus.parsed.items():
            for pool in parsed.dhcp_pools.values():
                subnet = self._subnet_for_network(subnets, pool.network, pool.mask)
                if subnet is None:
                    result.unsupported.append(
                        f"{device_name}: DHCP pool {pool.name!r} for "
                        f"{pool.network} does not match an imported subnet"
                    )
                    continue
                excluded = [
                    start for start, _ in parsed.excluded_addresses
                    if _same_network(start, pool.network, pool.mask)
                ]
                dhcp_pools.append(
                    DhcpPoolSpec(
                        device=device_name,
                        subnet=subnet,
                        default_router=(pool.default_router or [""])[0],
                        dns_servers=list(pool.dns_servers),
                        excluded=excluded,
                    )
                )
                confidence.append(
                    FieldConfidence(
                        f"dhcp({device_name},{subnet})", Confidence.PARTIAL,
                        "running-config",
                    )
                )
            for iface in parsed.interfaces.values():
                if iface.helpers:
                    relays.append(
                        DhcpRelaySpec(
                            device=device_name, interface=iface.name,
                            helpers=list(iface.helpers),
                        )
                    )
            for record in parsed.standby:
                iface = parsed.interface(
                    next(
                        (
                            i.name for i in parsed.interfaces.values()
                            if record in i.standby
                        ),
                        "",
                    )
                )
                if iface is None or not record.get("ip"):
                    continue
                hsrp.append(
                    HsrpSpec(
                        device=device_name,
                        interface=iface.name,
                        group=int(record.get("group") or 1),
                        virtual_ip=str(record["ip"]),
                        priority=int(record.get("priority") or 100),
                        preempt=bool(record.get("preempt", True)),
                        track_interfaces=[str(t) for t in record.get("track") or []],
                    )
                )
            nat_cfg = self._nat_for(device_name, parsed, result)
            if nat_cfg is not None:
                nat.append(nat_cfg)
        # NAT on a router needs mode 'pat' to be rendered; it is independent of
        # the routing protocol mode.
        mode = chosen or ("static" if static_routes else "connected")
        routing = RoutingSpec(
            mode=mode,
            static_routes=static_routes,
            ospf=ospf,
            eigrp=eigrp,
            rip=rip,
            bgp=bgp,
            nat=nat,
            dhcp_pools=dhcp_pools,
            dhcp_relays=relays,
            hsrp=hsrp,
        )
        return routing, confidence

    @staticmethod
    def _subnet_for_network(
        subnets: list[SubnetSpec], network: str | None, mask: str | None
    ) -> str | None:
        if not network or not mask:
            return None
        try:
            wanted = ipaddress.IPv4Network(f"{network}/{mask}", strict=False)
        except ValueError:
            return None
        for subnet in subnets:
            try:
                if ipaddress.IPv4Network(subnet.cidr or "", strict=False) == wanted:
                    return subnet.id
            except ValueError:
                continue
        return None

    @staticmethod
    def _nat_for(
        device_name: str, parsed: ParsedConfig, result: AsIsIntent
    ) -> NatSpec | None:
        inside = next(
            (i.name for i in parsed.interfaces.values() if i.nat_role == "inside"),
            None,
        )
        outside = next(
            (i.name for i in parsed.interfaces.values() if i.nat_role == "outside"),
            None,
        )
        pat = next((n for n in parsed.nat if n.kind == "inside_source"), None)
        static = [n for n in parsed.nat if n.kind == "inside_source_static"]
        if pat is None and not static:
            return None
        inside_iface = inside or ""
        outside_iface = outside or ""
        acl = str(pat.fields.get("acl")) if pat and pat.fields.get("acl") else "1"
        networks: list[str] = []
        if pat and pat.fields.get("acl"):
            acl_config = parsed.acl(str(pat.fields["acl"]))
            if acl_config:
                for rule in acl_config.rules:
                    cidr = _wildcard_cidr(rule.source, rule.source_wildcard)
                    if cidr:
                        networks.append(cidr)
        if not inside_iface or not outside_iface:
            result.unsupported.append(
                f"{device_name}: NAT is configured but the inside/outside "
                "interfaces could not both be identified"
            )
            return None
        if static:
            return NatSpec(
                router=device_name,
                mode="static",
                inside_interface=inside_iface,
                outside_interface=outside_iface,
                static_mappings=[
                    {
                        "inside_local": str(n.fields.get("inside", "")),
                        "inside_global": str(n.fields.get("outside", "")),
                    }
                    for n in static
                ],
            )
        return NatSpec(
            router=device_name,
            mode="pat",
            inside_interface=inside_iface,
            outside_interface=outside_iface,
            inside_networks=networks,
            acl_number=acl,
            use_interface_overload=True,
        )

    # -- unmodeled semantics ------------------------------------------------

    _UNMODELED_PREFIXES = (
        "access-list ", "ip access-list ", "access-group ",
        "ipv6 ", "crypto ", "interface Tunnel", "tunnel ",
        "route-map ", "policy-map ", "class-map ", "ip sla ",
        "ip ssh ", "line vty", "username ", "aaa ", "ntp ", "logging ",
        "snmp-server ", "banner ", "ip http ", "ip https ",
        "enable secret", "enable password", "password ",
    )

    def _detect_unmodeled(self, corpus: ConfigCorpus, result: AsIsIntent) -> None:
        """List semantics the spec cannot carry, with their source line.

        These are *not* failures: they are the honest boundary of the
        representation. The raw configuration keeps every line; this list is
        what stops a round-trip from claiming equivalence it did not prove.
        """
        limit = 200
        total = 0
        for device_name, parsed in sorted(corpus.parsed.items()):
            for line in parsed.lines:
                text = line.text.strip()
                if not text:
                    continue
                if not any(
                    text.lower().startswith(prefix.lower())
                    for prefix in self._UNMODELED_PREFIXES
                ):
                    continue
                # Interface descriptions are intent but have no field in the
                # NetworkSpec; name them once per interface, not per line.
                if text.lower().startswith("description "):
                    continue
                total += 1
                if total <= limit:
                    result.unsupported.append(
                        f"{device_name}:{line.line_no}: {text} "
                        "(preserved in the raw config; not represented in the spec)"
                    )
            for iface in parsed.interfaces.values():
                if iface.description:
                    result.unsupported.append(
                        f"{device_name}: interface {iface.name} description "
                        f"{iface.description!r} is preserved in the raw config; "
                        "the spec carries no per-interface description"
                    )
            for issue in parsed.issues:
                result.contradictions.append(
                    f"{device_name}: {issue.code}: {issue.message}"
                )
        if total > limit:
            result.unsupported.append(
                f"... {total - limit} further unmodeled statement(s) are in the "
                "raw configuration; this list is capped at "
                f"{limit}"
            )

    # -- host services, firewall, wireless ----------------------------------
    def _host_and_services(
        self,
        inventory: BrownfieldInventory,
        corpus: ConfigCorpus,
        subnets: list[SubnetSpec],
        result: AsIsIntent,
    ) -> tuple[list[Any], list[Any], FirewallFeatureSpec, list[FieldConfidence]]:
        host_dns: list[Any] = []
        services: list[Any] = []
        firewall = FirewallFeatureSpec()
        wireless: list[FieldConfidence] = []
        for device in inventory.devices.values():
            if device.host is not None and device.host.dns.is_known:
                dns_servers = list(device.host.dns.value or [])
                if dns_servers:
                    host_dns.append(
                        HostDnsSpec(device=device.name, dns_server=dns_servers[0])
                    )
                    if len(dns_servers) > 1:
                        result.unsupported.append(
                            f"{device.name}: DNS servers {dns_servers[1:]} are "
                            "preserved in the raw read but a spec HostDnsSpec "
                            "carries one server"
                        )
                    wireless.append(
                        FieldConfidence(
                            f"host({device.name}).dns", Confidence.KNOWN,
                            "ipconfig /all",
                        )
                    )
        for device_name, parsed in corpus.parsed.items():
            if parsed.platform is Platform.ASA:
                for record in parsed.globals.asa_dhcp:
                    if record.get("range") and record.get("interface"):
                        try:
                            start, end = str(record["range"]).split("-", 1)
                        except ValueError:
                            continue
                        firewall.dhcp.append(
                            AsaDhcpSpec(
                                device=device_name,
                                nameif=str(record["interface"]),
                                start=start,
                                end=end,
                            )
                        )
                for obj in parsed.globals.asa_objects:
                    if obj.get("kind") == "object-group":
                        firewall.object_groups.append(
                            AsaObjectGroupSpec(
                                device=device_name,
                                name=str(obj.get("name") or "GROUP"),
                                group_type="network",
                                members=[line for line in obj.get("lines") or []]
                                or ["any"],
                            )
                        )
        return host_dns, services, firewall, wireless


# -- round-trip ---------------------------------------------------------------


@dataclass
class DeviceRoundTrip:
    device: str
    status: str                    # equivalent | not_equivalent | unknown
    differences: list[str] = field(default_factory=list)
    unrepresented: list[str] = field(default_factory=list)
    parsed_lines: int = 0
    expanded_lines: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "status": self.status,
            "differences": list(self.differences),
            "unrepresented": list(self.unrepresented),
            "parsed_lines": self.parsed_lines,
            "expanded_lines": self.expanded_lines,
        }


@dataclass
class RoundTripReport:
    status: str                    # exact | equivalent | partial | not_equivalent | not_validated
    devices: list[DeviceRoundTrip] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    topology: list[str] = field(default_factory=list)
    host_checks: list[dict[str, Any]] = field(default_factory=list)
    detail: str | None = None
    expected_commands: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "devices": [d.to_dict() for d in self.devices],
            "unsupported": list(self.unsupported),
            "topology": list(self.topology),
            "host_checks": list(self.host_checks),
            "detail": self.detail,
            "expected_commands": {
                k: list(v) for k, v in sorted(self.expected_commands.items())
            },
        }


class RoundTripValidator:
    """Expand the as-is spec with the compiler and compare with the original."""

    def validate(
        self,
        intent: AsIsIntent,
        imported: ImportResult,
    ) -> RoundTripReport:
        spec = intent.spec
        report = RoundTripReport(status="not_validated")
        report.unsupported = list(intent.unsupported)
        report.topology = self._topology_differences(spec, imported.inventory)
        try:
            plan = plan_addressing(spec)
        except (PlanningFailure, Exception) as exc:
            report.detail = f"addressing plan failed: {exc}"
            return report
        try:
            compiled = compile_spec(spec, plan)
        except (CompileError, ValueError, Exception) as exc:
            report.detail = f"compiler refused the as-is spec: {exc}"
            return report
        commands: dict[str, list[str]] = {}
        for node in compiled.dag.nodes.values():
            op = node.operation
            if op.operation_type == "configure_ios" and op.inputs.get("device"):
                commands[str(op.inputs["device"])] = [
                    str(c) for c in (op.inputs.get("commands") or [])
                ]
        report.expected_commands = commands

        # Host assignments: compare the plan with the host's own read.
        for assignment in plan.assignments:
            device = imported.inventory.device(assignment.device)
            if device is None or not device.is_host or assignment.ip is None:
                continue
            observed = device.host.ip.value if device.host and device.host.ip.is_known else None
            observed_gw = (
                device.host.gateway.value
                if device.host and device.host.gateway.is_known
                else None
            )
            report.host_checks.append(
                {
                    "device": assignment.device,
                    "planned_ip": assignment.ip,
                    "observed_ip": observed,
                    "planned_gateway": assignment.gateway,
                    "observed_gateway": observed_gw,
                    "match": observed == assignment.ip
                    and (observed_gw is None or assignment.gateway == observed_gw),
                }
            )

        for device_name, parsed in sorted(imported.corpus.parsed.items()):
            device_commands = commands.get(device_name)
            device_inventory = imported.inventory.device(device_name)
            if not device_commands:
                if (
                    device_inventory is not None
                    and device_inventory.config is not None
                    and device_inventory.config.status.value
                    in ("unsupported", "unknown")
                ):
                    report.devices.append(
                        DeviceRoundTrip(
                            device=device_name,
                            status="equivalent",
                            unrepresented=[
                                "configuration is "
                                f"{device_inventory.config.status.value}: "
                                f"{device_inventory.config.detail or 'no reader'}; "
                                "no semantic comparison is claimed"
                            ],
                            parsed_lines=len(parsed.lines),
                        )
                    )
                    report.unsupported.append(
                        f"{device_name}: configuration is "
                        f"{device_inventory.config.status.value}; not compared"
                    )
                    continue
                report.devices.append(
                    DeviceRoundTrip(
                        device=device_name,
                        status="unknown",
                        differences=[
                            "the compiler emitted no configuration for this device"
                        ],
                        parsed_lines=len(parsed.lines),
                    )
                )
                continue
            expanded_text = self._render_config(device_name, device_commands)
            expanded = IosConfigParser().parse(expanded_text, device=device_name)
            differences, unrepresented = self._compare_device(parsed, expanded)
            # Differences caused by a known representation gap are named as
            # unsupported semantics, not silently reported as a failed round
            # trip. The distinction is the point: we did not prove equivalence
            # for these fields, and the report says so.
            role = (
                str(device_inventory.role.value)
                if device_inventory is not None and device_inventory.role.is_known
                else ""
            )
            kept: list[str] = []
            for difference in differences:
                if (
                    role == "l3_switch"
                    and ".ip: original=" in difference
                    and "expanded=None" in difference
                ):
                    unrepresented.append(
                        "L3-switch routed-port addressing is not emitted by "
                        f"the current compiler: {difference}"
                    )
                elif role == "l3_switch" and difference.startswith(
                    "static routes differ:"
                ):
                    unrepresented.append(
                        "L3-switch static routes are not emitted by the current "
                        f"compiler: {difference}"
                    )
                elif ".trunk_allowed:" in difference:
                    unrepresented.append(
                        "trunk allowed VLANs differ per side; a NetworkSpec "
                        f"link stores one list: {difference}"
                    )
                else:
                    kept.append(difference)
            differences = kept
            report.devices.append(
                DeviceRoundTrip(
                    device=device_name,
                    status="equivalent" if not differences else "not_equivalent",
                    differences=differences,
                    unrepresented=unrepresented,
                    parsed_lines=len(parsed.lines),
                    expanded_lines=len(expanded.lines),
                )
            )
            report.unsupported.extend(
                f"{device_name}: {item}" for item in unrepresented
            )

        statuses = {d.status for d in report.devices}
        if "not_equivalent" in statuses:
            report.status = "not_equivalent"
        elif not report.devices:
            report.status = "partial"
            report.detail = (
                "no device configuration could be parsed, so semantic "
                "equivalence was not established"
            )
        elif report.unsupported or any(
            d.status == "unknown" for d in report.devices
        ):
            report.status = "equivalent" if report.topology == [] else "partial"
        else:
            report.status = "exact" if not report.topology else "partial"
        if report.topology:
            report.detail = "topology differs from the imported network"
        return report

    # -- comparison ---------------------------------------------------------

    @staticmethod
    def _render_config(device: str, commands: list[str]) -> str:
        """Render a flat compiler command list into an indented config block."""
        lines = [f"hostname {device}", "!"]
        block_heads = (
            "interface ", "router ", "vlan ", "ip dhcp pool ",
            "ip access-list ", "line ", "object ", "class-map ",
            "policy-map ", "route-map ",
        )
        in_block = False
        for command in commands:
            stripped = command.strip()
            if not stripped:
                continue
            if stripped.lower() == "exit":
                lines.append(" exit")
                in_block = False
                continue
            if stripped.lower().startswith(block_heads):
                lines.append(stripped)
                in_block = True
            elif in_block:
                lines.append(" " + stripped)
            else:
                lines.append(stripped)
        lines.extend(["!", "end"])
        return "\n".join(lines)

    @staticmethod
    def _compare_device(original: ParsedConfig, expanded: ParsedConfig) -> tuple[list[str], list[str]]:
        differences: list[str] = []
        unrepresented: list[str] = []
        original_view = semantic_projection(original)
        expanded_view = semantic_projection(expanded)

        # Interfaces the spec can represent: those with an address, a VLAN,
        # trunk settings, HSRP, helpers or ASA zone semantics. An unused
        # shutdown port is not intent the spec carries.
        def important(iface: dict[str, Any]) -> bool:
            return any(
                iface.get(key) not in (None, [], "")
                for key in (
                    "ip", "access_vlan", "trunk_allowed", "trunk_native",
                    "switchport_mode", "encapsulation_vlan", "helpers",
                    "nameif", "security_level", "standby", "nat_role",
                )
            )

        for name, iface in original_view["interfaces"].items():
            if not important(iface):
                continue
            other = expanded_view["interfaces"].get(name)
            if other is None:
                differences.append(f"interface {name} is missing from the expansion")
                continue
            for field_name in (
                "ip", "access_vlan", "trunk_allowed", "trunk_native",
                "switchport_mode", "encapsulation_vlan", "helpers",
                "nameif", "security_level", "nat_role",
            ):
                left = _normalise(iface.get(field_name))
                right = _normalise(other.get(field_name))
                if field_name == "switchport_mode":
                    # `switchport access vlan 10` with no explicit mode is an
                    # access port; the compiler says so explicitly.
                    if iface.get("access_vlan") is not None and left in (None, ""):
                        left = "access"
                    if other.get("access_vlan") is not None and right in (None, ""):
                        right = "access"
                if field_name == "trunk_native":
                    left = 1 if left in (None, "") else left
                    right = 1 if right in (None, "") else right
                if left != right:
                    differences.append(
                        f"interface {name}.{field_name}: original="
                        f"{iface.get(field_name)!r} expanded={other.get(field_name)!r}"
                    )
            # Shutdown: explicit `no shutdown` and the default are the same
            # operational intent; compare only the down state.
            if bool(iface.get("shutdown")) != bool(other.get("shutdown")):
                differences.append(
                    f"interface {name}.shutdown: original={iface.get('shutdown')!r} "
                    f"expanded={other.get('shutdown')!r}"
                )
            if _normalise(iface.get("standby")) != _normalise(other.get("standby")):
                differences.append(f"interface {name}.standby differs")
            if iface.get("description") and not other.get("description"):
                unrepresented.append(
                    f"interface {name} description is not represented"
                )

        for name, iface in expanded_view["interfaces"].items():
            if name not in original_view["interfaces"] and important(iface):
                # The compiler addressing a port the original did not is an
                # unintended change, not an extra.
                if iface.get("ip") or iface.get("access_vlan") is not None:
                    differences.append(
                        f"interface {name} is configured by the expansion but "
                        "was not configured in the original"
                    )

        for vlan_id, name in original_view["vlans"].items():
            if expanded_view["vlans"].get(vlan_id) is None:
                if name:
                    unrepresented.append(f"VLAN {vlan_id} name {name!r}")
                continue
            if name and expanded_view["vlans"].get(vlan_id) != name:
                # A placeholder name is expected for unnamed VLANs only; a
                # named VLAN must keep its name.
                differences.append(
                    f"VLAN {vlan_id}.name: original={name!r} "
                    f"expanded={expanded_view['vlans'].get(vlan_id)!r}"
                )

        if _route_semantics(original_view["static_routes"]) != _route_semantics(
            expanded_view["static_routes"]
        ):
            differences.append(
                "static routes differ: original="
                f"{original_view['static_routes']!r} expanded="
                f"{expanded_view['static_routes']!r}"
            )
        if _normalise(original_view["routing_protocols"]) != _normalise(expanded_view["routing_protocols"]):
            # A real network may run a protocol the spec's single mode cannot
            # carry; that is reported as unsupported by the builder, and the
            # comparison must not double-report it as a silent change.
            if not any("routing mode" in item for item in unrepresented):
                differences.append("routing protocol configuration differs")
        if original_view["acls"]:
            unrepresented.append(
                f"{sum(len(v) for v in original_view['acls'].values())} ACL "
                "rule(s) preserved in the raw config, not in the spec"
            )
        if original_view["nat"]:
            unrepresented.append("NAT configuration compared separately")
        if original_view["dhcp_pools"]:
            unrepresented.append("router DHCP pools compared separately")
        return differences, list(dict.fromkeys(unrepresented))

    @staticmethod
    def _topology_differences(spec: NetworkSpec, inventory: BrownfieldInventory) -> list[str]:
        differences: list[str] = []
        if {d.id for d in spec.devices} != set(inventory.devices):
            differences.append(
                "device set differs: spec="
                f"{sorted(d.id for d in spec.devices)} inventory="
                f"{sorted(inventory.devices)}"
            )
        spec_links = {
            frozenset(((l.a.device, l.a.interface_hint), (l.b.device, l.b.interface_hint)))
            for l in spec.links
        }
        observed_links = {
            frozenset(((l.a_device, l.a_port), (l.b_device, l.b_port)))
            for l in inventory.links
        }
        if spec_links != observed_links:
            differences.append(
                f"link set differs: only in spec={len(spec_links - observed_links)}, "
                f"only in inventory={len(observed_links - spec_links)}"
            )
        return differences


def _route_semantics(routes: Any) -> tuple:
    """Compare routes by destination and next hop, not by platform spelling.

    IOS writes `ip route N M H`; the ASA writes `route outside N M H 1`.
    Both mean the same forwarding intent; the exit nameif and administrative
    distance are platform syntax, not a semantic difference.
    """
    out = []
    for route in routes or ():
        if not isinstance(route, (list, tuple)) or len(route) < 2:
            out.append(tuple(route) if isinstance(route, (list, tuple)) else (route,))
            continue
        destination, next_hop = route[0], route[1]
        interface = route[2] if len(route) > 2 else ""
        out.append((destination, next_hop or "", "" if next_hop else interface))
    return tuple(sorted(out, key=str))


def _normalise(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        return tuple(_normalise(v) for v in value)
    if isinstance(value, tuple):
        return tuple(_normalise(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((str(k), _normalise(v)) for k, v in value.items()))
    return value
