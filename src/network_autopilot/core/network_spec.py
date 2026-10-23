"""The canonical, versioned, typed NetworkSpec.

This is the wall between natural language and Packet Tracer. An LLM may
propose, but what reaches the engine is one of these typed objects: every
field is declared, unknown fields are rejected (`extra="forbid"`), and every
cross-reference (requirements ↔ acceptance criteria, devices ↔ links ↔
subnets) is validated structurally. Free-form dictionaries never reach the
compiler.

The spec is immutable (`frozen`), serializable, and hash-stable: the canonical
JSON hash of a spec is the seed of the build fingerprint, so two builds claim
to be "the same build" only when they really are.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import canonical_json, validate_id

SPEC_VERSION = "1"


class SpecModel(BaseModel):
    """Base for all spec models: strict, frozen, no free-form extras."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DeviceRole(str, Enum):
    ROUTER = "router"
    SWITCH = "switch"
    L3_SWITCH = "l3_switch"
    PC = "pc"
    SERVER = "server"
    LAPTOP = "laptop"
    ACCESS_POINT = "access_point"
    CLOUD = "cloud"
    # Roles the PT catalog needs but this enum never had: without
    # them a firewall / wireless router / modem / IoT device could
    # only be spelled `other`, which the compiler routes past every
    # config path - an ASA silently left unconfigured (#14, #36).
    FIREWALL = "firewall"
    WIRELESS_ROUTER = "wireless_router"
    MODEM = "modem"
    IOT = "iot"
    # Stage 3 end devices and wireless controller. They were catalogued but
    # unreachable: a spec could only spell them `other`, which the compiler
    # routes past every config path.
    PRINTER = "printer"
    IP_PHONE = "ip_phone"
    TABLET = "tablet"
    SMARTPHONE = "smartphone"
    WLC = "wlc"
    OTHER = "other"


# The single source of truth for "which PT model does this role build by
# default". The compiler, the port solver and the semantic diff MUST all read
# this map: when they each kept their own copy they drifted (diff expected
# "PC" while the compiler creates "PC-PT", so every clean build reported
# spurious model drift).
DEFAULT_MODEL_FOR_ROLE: dict[str, str] = {
    DeviceRole.ROUTER.value: "2911",
    DeviceRole.SWITCH.value: "2960-24TT",
    DeviceRole.L3_SWITCH.value: "3560-24PS",
    DeviceRole.PC.value: "PC-PT",
    DeviceRole.SERVER.value: "Server-PT",
    DeviceRole.LAPTOP.value: "Laptop-PT",
    DeviceRole.ACCESS_POINT.value: "AccessPoint-PT",
    DeviceRole.CLOUD.value: "Cloud-PT",
    DeviceRole.FIREWALL.value: "5506-X",
    DeviceRole.WIRELESS_ROUTER.value: "Linksys-WRT300N",
    DeviceRole.MODEM.value: "DSL-Modem-PT",
    DeviceRole.IOT.value: "Smart LED",
    DeviceRole.PRINTER.value: "Printer-PT",
    DeviceRole.IP_PHONE.value: "7960",
    DeviceRole.TABLET.value: "TabletPC-PT",
    DeviceRole.SMARTPHONE.value: "SMARTPHONE-PT",
    DeviceRole.WLC.value: "WLC-PT",
    DeviceRole.OTHER.value: "Router-PT",
}


def default_model_for_role(role: str | DeviceRole) -> str:
    """The PT model a role defaults to (single source of truth)."""
    key = role.value if isinstance(role, DeviceRole) else str(role)
    return DEFAULT_MODEL_FOR_ROLE.get(key, "Router-PT")


class ProvenanceSpec(SpecModel):
    """Where this spec came from. Honest provenance starts at the spec itself."""

    author: str = "user"
    generator: str = "network_autopilot"
    source_prompt: str | None = None
    notes: str | None = None


class RequirementSpec(SpecModel):
    """A user requirement in the requirement→acceptance traceability chain."""

    id: str
    statement: str = Field(min_length=1)
    kind: Literal[
        "connectivity", "isolation", "addressing", "service", "constraint", "operational"
    ] = "connectivity"

    @model_validator(mode="after")
    def _check_id(self) -> "RequirementSpec":
        if not self.id.startswith("R-"):
            raise ValueError(f"requirement id must start with 'R-': {self.id!r}")
        return self


# --- acceptance tests -------------------------------------------------------


class FlowTestSpec(SpecModel):
    """An expected traffic flow: reachability, or the *negative* case (blocked)."""

    kind: Literal["flow"] = "flow"
    source: str
    destination: str
    expect: Literal["reachable", "blocked"]
    transport: Literal["icmp", "tcp", "udp"] = "icmp"
    port: int | None = Field(default=None, ge=1, le=65535)
    # #24: a router or an ASA answers on more than one address, and the
    # adapter refuses to guess ("destination 'rbr' has several IPs"), so the
    # probe must be told which one - or which interface to read it from.
    destination_ip: str | None = None
    destination_interface: str | None = None


class StructuralTestSpec(SpecModel):
    """A structural expectation: a device/link/ip/gateway/vlan/trunk exists."""

    kind: Literal["structural"] = "structural"
    check: Literal[
        "device_exists",
        "link_exists",
        "ip_assigned",
        "gateway_assigned",
        "vlan_defined",
        "trunk_configured",
    ]
    target: str
    detail: str | None = None


class ConfigTestSpec(SpecModel):
    """A configuration expectation on one field of one interface."""

    kind: Literal["config"] = "config"
    device: str
    interface: str | None = None
    field: Literal["ip", "mask", "vlan", "state", "description"]
    expected: str


class WirelessTestSpec(SpecModel):
    """A wireless association expectation.

    Passes only when Packet Tracer itself shows the antenna link between the
    client's radio and that access point - the same `Antenna` object the
    executor reads. A client that merely has a Wireless0, or an AP that
    exists, is not an association.
    """

    kind: Literal["wireless"] = "wireless"
    client: str
    access_point: str
    interface: str = "Wireless0"
    expect: Literal["associated", "not_associated"] = "associated"


AcceptanceTest = Annotated[
    Union[
        FlowTestSpec, StructuralTestSpec, ConfigTestSpec, WirelessTestSpec,
    ],
    Field(discriminator="kind"),
]


class AcceptanceCriterionSpec(SpecModel):
    """One checkable criterion, linked back to the requirements it verifies."""

    id: str
    requirement_ids: list[str] = Field(default_factory=list)
    test: AcceptanceTest
    title: str | None = None

    @model_validator(mode="after")
    def _check_id(self) -> "AcceptanceCriterionSpec":
        if not self.id.startswith("A-"):
            raise ValueError(f"acceptance criterion id must start with 'A-': {self.id!r}")
        return self


# --- topology ---------------------------------------------------------------


class InterfaceRequirement(SpecModel):
    """A capability requirement: 'needs N interfaces of this media'."""

    count: int = Field(default=1, ge=1)
    media: Literal["ethernet", "gigabit", "serial", "fiber", "wireless"] = "ethernet"
    hint: str | None = None


class AsaInterfaceSpec(SpecModel):
    """One ASA interface, as the spec wants it named and levelled (#37).

    PT's ASA assigns `inside`/`security-level 100` to the first interface and
    `outside`/`0` to the second, so a design that needs a third zone - a DMZ,
    say - had nowhere to say so. Naming an interface here overrides the
    derived nameif and level; the address still comes from `ip_assignments`,
    because that is where the subnet lives.
    """

    interface: str = Field(min_length=1)
    nameif: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    security_level: int = Field(ge=0, le=100)


class DeviceSpec(SpecModel):
    id: str
    role: DeviceRole
    model_hint: str | None = None
    interface_requirements: list[InterfaceRequirement] = Field(default_factory=list)
    floor: str | None = None
    description: str | None = None
    # #37: ASA zones, stated rather than inferred. Ignored for every other
    # role, so putting it on a router is a no-op rather than an error.
    asa_interfaces: list[AsaInterfaceSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_id(self) -> "DeviceSpec":
        validate_id(self.id, "device id")
        names = [a.interface for a in self.asa_interfaces]
        if len(set(names)) != len(names):
            raise ValueError(
                f"device {self.id!r}: asa_interfaces lists "
                f"{sorted(n for n in names if names.count(n) > 1)} twice"
            )
        return self


class EndpointSpec(SpecModel):
    device: str
    interface_hint: str | None = None
    # #37: the VLAN this one port belongs in. The compiler can infer it from
    # the endpoint's subnet, but a trunking host or a port in a VLAN with no
    # address has no subnet to infer from, and stating it here is the only way
    # to say it.
    vlan: int | None = Field(default=None, ge=1, le=4094)


class LinkSpec(SpecModel):
    id: str
    a: EndpointSpec
    b: EndpointSpec
    media: Literal[
        "auto", "straight", "crossover", "fiber", "serial", "wireless"
    ] = "auto"
    trunk: bool = False
    # #37: the trunk's native VLAN and the list it is allowed to carry. Both
    # are inferred from the VLANs on each end when omitted; stating them is how
    # a design that does not match the inference is expressed.
    native_vlan: int | None = Field(default=None, ge=1, le=4094)
    allowed_vlans: list[int] | None = None

    @model_validator(mode="after")
    def _check_id(self) -> "LinkSpec":
        validate_id(self.id, "link id")
        if self.allowed_vlans is not None:
            bad = [v for v in self.allowed_vlans if not 1 <= v <= 4094]
            if bad:
                raise ValueError(
                    f"link {self.id!r}: allowed_vlans out of range: {bad}"
                )
            if len(set(self.allowed_vlans)) != len(self.allowed_vlans):
                raise ValueError(
                    f"link {self.id!r}: allowed_vlans has duplicates: "
                    f"{self.allowed_vlans}"
                )
        return self


class VlanSpec(SpecModel):
    vlan_id: int = Field(ge=1, le=4094)
    name: str = Field(min_length=1)
    purpose: str | None = None


class SubnetSpec(SpecModel):
    id: str
    vlan_id: int | None = None
    purpose: str | None = None
    cidr: str | None = None
    required_hosts: int | None = Field(default=None, ge=1)
    gateway: str | None = None
    dhcp: bool = False

    @model_validator(mode="after")
    def _check_size_source(self) -> "SubnetSpec":
        if (self.cidr is None) == (self.required_hosts is None):
            raise ValueError(
                f"subnet {self.id!r}: exactly one of 'cidr' or 'required_hosts' "
                "(a planned subnet asks for a size; a fixed subnet states it)"
            )
        validate_id(self.id, "subnet id")
        return self


class IpAssignmentSpec(SpecModel):
    device: str
    interface_hint: str | None = None
    subnet: str
    ip: str | None = None
    dhcp: bool | None = None
    # A host behind an HSRP/VRRP virtual gateway needs a gateway that is not
    # the router's own interface address. None keeps the planner's default.
    gateway: str | None = None

    @property
    def uses_dhcp(self) -> bool:
        if self.dhcp is not None:
            return self.dhcp
        return self.ip is None


class StaticRouteSpec(SpecModel):
    device: str
    destination: str
    next_hop: str | None = None
    exit_interface: str | None = None

    @model_validator(mode="after")
    def _check_target(self) -> "StaticRouteSpec":
        if not (self.next_hop or self.exit_interface):
            raise ValueError(f"static route on {self.device!r} needs next_hop or exit_interface")
        return self


class OspfNetworkSpec(SpecModel):
    """One `network ... area ...` statement.

    S3.A2 needs two networks in two different areas on the same process; the
    original single `area` field could not express that without lying about
    one of them.
    """

    network: str
    area: str = "0"


class RedistributeSpec(SpecModel):
    protocol: Literal["static", "connected", "eigrp", "rip", "bgp"]
    process_id: int | None = None
    metric: int | None = None
    subnets: bool = False


class OspfSpec(SpecModel):
    process_id: int = Field(default=1, ge=1)
    networks: list[str] = Field(default_factory=list)
    area: str = "0"
    # Per-network areas. When present they take precedence over `networks`
    # for rendering; `networks` stays for every existing spec.
    area_networks: list[OspfNetworkSpec] = Field(default_factory=list)
    default_information_originate: bool = False
    redistribute: list[RedistributeSpec] = Field(default_factory=list)
    # Empty = every router, the pre-Stage-3 behaviour. An ISP router that must
    # stay outside the OSPF domain is named explicitly here.
    devices: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_networks(self) -> "OspfSpec":
        if not self.networks and not self.area_networks:
            raise ValueError("ospf needs at least one of 'networks' or 'area_networks'")
        return self


class EigrpSpec(SpecModel):
    as_number: int = Field(default=1, ge=1)
    networks: list[str] = Field(default_factory=list)
    passive_interfaces: list[str] = Field(default_factory=list)
    redistribute: list[RedistributeSpec] = Field(default_factory=list)
    devices: list[str] = Field(default_factory=list)


class RipSpec(SpecModel):
    version: Literal[1, 2] = 2
    networks: list[str] = Field(default_factory=list)
    no_auto_summary: bool = True
    passive_interfaces: list[str] = Field(default_factory=list)
    redistribute: list[RedistributeSpec] = Field(default_factory=list)
    devices: list[str] = Field(default_factory=list)


class BgpNeighborSpec(SpecModel):
    address: str
    remote_as: int = Field(ge=1)
    description: str | None = None


class BgpSpec(SpecModel):
    as_number: int = Field(ge=1)
    neighbors: list[BgpNeighborSpec] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    router_id: str | None = None
    devices: list[str] = Field(default_factory=list)
    # A real eBGP lab gives each AS its own local-AS, peers and networks.
    # The global fields above remain for a homogeneous two-router lab.
    per_device: list["BgpDeviceSpec"] = Field(default_factory=list)


class BgpDeviceSpec(SpecModel):
    device: str
    as_number: int = Field(ge=1)
    neighbors: list[BgpNeighborSpec] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    router_id: str | None = None


class DhcpPoolSpec(SpecModel):
    """A pool placed on a chosen router, not necessarily the subnet gateway.

    S3.A7 needs the pool on the central DHCP server while the remote router
    only relays; the existing per-subnet `dhcp: true` always put the pool on
    the gateway, which is the wrong device for a relayed LAN.
    """

    device: str
    subnet: str
    default_router: str
    dns_servers: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)


class HsrpSpec(SpecModel):
    """One HSRP group on one interface (S3.A8)."""

    device: str
    interface: str
    group: int = Field(default=1, ge=0, le=255)
    virtual_ip: str
    priority: int = Field(default=100, ge=0, le=255)
    preempt: bool = True
    track_interfaces: list[str] = Field(default_factory=list)


class DhcpRelaySpec(SpecModel):
    device: str
    interface: str
    helpers: list[str] = Field(min_length=1)


class TunnelSpec(SpecModel):
    device: str
    tunnel_id: int = Field(default=0, ge=0, le=2147483647)
    ip: str
    source: str
    destination: str
    mode: Literal["gre", "ipsec"] = "gre"


class IpsecSpec(SpecModel):
    device: str
    peer: str
    pre_shared_key: str
    local_network: str
    remote_network: str
    acl_id: int = Field(default=100, ge=1, le=199)
    transform_set: str = "TS"
    crypto_map: str = "CM"
    interface: str


class Ipv6InterfaceSpec(SpecModel):
    device: str
    interface: str
    address: str
    ospf_process: int | None = Field(default=None, ge=1)
    ospf_area: str = "0"


class Ospfv3Spec(SpecModel):
    device: str
    process_id: int = Field(default=1, ge=1)
    router_id: str | None = None


class SshSpec(SpecModel):
    device: str
    domain_name: str = "lab.local"
    username: str = "admin"
    secret: str
    vty_lines: str = "0 4"
    version: Literal[1, 2] = 2


class ManagementSpec(SpecModel):
    device: str
    ntp_servers: list[str] = Field(default_factory=list)
    syslog_hosts: list[str] = Field(default_factory=list)
    snmp_community: str | None = None
    snmp_access: Literal["RO", "RW"] = "RO"


class SviSpec(SpecModel):
    device: str
    vlan: int = Field(ge=1, le=4094)
    ip: str
    mask: str
    no_shutdown: bool = True


class EtherChannelSpec(SpecModel):
    device: str
    group: int = Field(ge=1)
    members: list[str] = Field(min_length=2)
    mode: Literal["active", "passive", "desirable", "auto", "on"] = "active"


class StpSpec(SpecModel):
    device: str
    vlan: int = Field(default=1, ge=1, le=4094)
    root: Literal["primary", "secondary"] | None = None
    portfast_interfaces: list[str] = Field(default_factory=list)
    bpduguard_interfaces: list[str] = Field(default_factory=list)


class PortSecuritySpec(SpecModel):
    device: str
    interface: str
    maximum: int = Field(default=1, ge=1)
    violation: Literal["shutdown", "restrict", "protect"] = "shutdown"


class VtpSpec(SpecModel):
    device: str
    domain: str
    mode: Literal["server", "client", "transparent"] = "server"
    password: str | None = None


class VoiceVlanSpec(SpecModel):
    device: str
    interface: str
    voice_vlan: int = Field(ge=1, le=4094)
    access_vlan: int | None = Field(default=None, ge=1, le=4094)


class ManagementIpSpec(SpecModel):
    device: str
    vlan: int = Field(default=1, ge=1, le=4094)
    ip: str
    mask: str
    gateway: str | None = None


class AsaNatSpec(SpecModel):
    device: str
    object_name: str
    subnet: str
    inside_nameif: str = "inside"
    outside_nameif: str = "outside"
    dynamic_interface: bool = True


class AsaDhcpSpec(SpecModel):
    device: str
    nameif: str = "inside"
    start: str
    end: str


class AsaObjectGroupSpec(SpecModel):
    device: str
    name: str
    group_type: Literal["network", "service"] = "network"
    members: list[str] = Field(min_length=1)


class ServerServiceSpec(SpecModel):
    """A Server-PT service intent (S3.D2-D8)."""

    device: str
    service: Literal[
        "dns", "http", "https", "dhcp", "email", "ftp", "aaa", "ntp", "syslog", "iot"
    ]
    enabled: bool = True


class WirelessSsidSpec(SpecModel):
    device: str
    ssid: str
    security: Literal["open", "wpa2"] = "wpa2"
    passphrase: str | None = None
    channel: int | None = Field(default=None, ge=1, le=196)


class CloudMappingSpec(SpecModel):
    cloud: str
    modem: str
    cloud_port: str
    modem_port: str
    connection_type: Literal["dsl", "cable", "serial"] = "dsl"


class IotRegistrationSpec(SpecModel):
    device: str
    server: str = ""
    gateway: str = ""


class HostDnsSpec(SpecModel):
    device: str
    dns_server: str


class NatSpec(SpecModel):
    """NAT/PAT configuration for a router."""
    router: str
    mode: Literal["static", "dynamic", "pat"] = "pat"
    inside_interface: str = ""
    outside_interface: str = ""
    inside_networks: list[str] = Field(default_factory=list)
    pool_name: str = ""
    start_ip: str = ""
    end_ip: str = ""
    netmask: str = ""
    acl_number: str = "1"
    use_interface_overload: bool = True
    static_mappings: list[dict[str, str]] = Field(default_factory=list)


class Ipv6Spec(SpecModel):
    """IPv6 unicast-routing configuration."""
    enabled: bool = False
    addresses: dict[str, str] = Field(default_factory=dict)  # iface -> ipv6 cidr
    global_eui_64: bool = False
    # Per-device interfaces (S3.A13) and OSPFv3 processes (S3.A14).
    interfaces: list[Ipv6InterfaceSpec] = Field(default_factory=list)
    ospf: list[Ospfv3Spec] = Field(default_factory=list)


class RoutingSpec(SpecModel):
    mode: Literal[
        "connected", "static", "ospf", "eigrp", "rip", "bgp"
    ] = "connected"
    static_routes: list[StaticRouteSpec] = Field(default_factory=list)
    ospf: OspfSpec | None = None
    eigrp: EigrpSpec | None = None
    rip: RipSpec | None = None
    bgp: BgpSpec | None = None
    nat: list[NatSpec] = Field(default_factory=list)
    ipv6: Ipv6Spec | None = None
    # Stage 3 per-device intents. Each names its device; the compiler applies
    # them in the same push as the device's addresses (one CLI transaction).
    hsrp: list[HsrpSpec] = Field(default_factory=list)
    dhcp_relays: list[DhcpRelaySpec] = Field(default_factory=list)
    dhcp_pools: list[DhcpPoolSpec] = Field(default_factory=list)
    tunnels: list[TunnelSpec] = Field(default_factory=list)
    ipsec: list[IpsecSpec] = Field(default_factory=list)
    ssh: list[SshSpec] = Field(default_factory=list)
    management: list[ManagementSpec] = Field(default_factory=list)


class SwitchingSpec(SpecModel):
    svis: list[SviSpec] = Field(default_factory=list)
    etherchannels: list[EtherChannelSpec] = Field(default_factory=list)
    stp: list[StpSpec] = Field(default_factory=list)
    port_security: list[PortSecuritySpec] = Field(default_factory=list)
    vtp: list[VtpSpec] = Field(default_factory=list)
    voice_vlans: list[VoiceVlanSpec] = Field(default_factory=list)
    management_ips: list[ManagementIpSpec] = Field(default_factory=list)


class FirewallFeatureSpec(SpecModel):
    nat: list[AsaNatSpec] = Field(default_factory=list)
    dhcp: list[AsaDhcpSpec] = Field(default_factory=list)
    object_groups: list[AsaObjectGroupSpec] = Field(default_factory=list)


class AclRuleSpec(SpecModel):
    action: Literal["permit", "deny"]
    protocol: Literal["ip", "icmp", "tcp", "udp"] = "ip"
    source: str
    destination: str
    dest_port: int | None = Field(default=None, ge=1, le=65535)
    # An ASA/IOS ICMP type operand (``echo-reply``, ``echo``, ... or a number
    # 0-255), rendered after the addresses. Only meaningful on icmp, which the
    # policy compiler enforces (never here - this is shape, not policy).
    #
    # It exists because PT's ASA is stateless (live probe S1.19): with a plain
    # ``permit icmp <inside> any`` the echo-reply of an inside-initiated ping
    # arrives with an outside source and is dropped, so the ping fails. The
    # only way to let that return path back in while still blocking an
    # outside-initiated request is to match on ICMP type -
    # ``permit icmp any any echo-reply`` was measured to pass A-010 and block
    # A-024 on the same firewall at the same time.
    icmp_type: str | None = None
    description: str | None = None


class PolicySpec(SpecModel):
    id: str
    kind: Literal["acl"] = "acl"
    rules: list[AclRuleSpec] = Field(min_length=1)
    apply_to: list[str] = Field(min_length=1)
    # Honest capability signalling: 'planned' marks a policy the spec may carry
    # before the engine can enforce/verify it — verification must report it as
    # UNKNOWN, never as satisfied.
    enforcement: Literal["available", "planned"] = "available"

    @model_validator(mode="after")
    def _check_id(self) -> "PolicySpec":
        validate_id(self.id, "policy id")
        return self


class DesignDecisionSpec(SpecModel):
    id: str
    statement: str = Field(min_length=1)
    rationale: str | None = None
    source: Literal["user", "engine"] = "engine"

    @model_validator(mode="after")
    def _check_id(self) -> "DesignDecisionSpec":
        if not self.id.startswith("D-"):
            raise ValueError(f"design decision id must start with 'D-': {self.id!r}")
        return self


class NetworkSpec(SpecModel):
    """The complete, deterministic description of the intended network."""

    spec_version: Literal["1"] = SPEC_VERSION
    name: str = Field(min_length=1)
    description: str | None = None
    provenance: ProvenanceSpec = Field(default_factory=ProvenanceSpec)

    requirements: list[RequirementSpec] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterionSpec] = Field(default_factory=list)

    devices: list[DeviceSpec] = Field(min_length=1)
    links: list[LinkSpec] = Field(default_factory=list)
    vlans: list[VlanSpec] = Field(default_factory=list)
    subnets: list[SubnetSpec] = Field(default_factory=list)
    ip_assignments: list[IpAssignmentSpec] = Field(default_factory=list)
    routing: RoutingSpec = Field(default_factory=RoutingSpec)
    policies: list[PolicySpec] = Field(default_factory=list)
    switching: SwitchingSpec = Field(default_factory=SwitchingSpec)
    firewall: FirewallFeatureSpec = Field(default_factory=FirewallFeatureSpec)
    services: list[ServerServiceSpec] = Field(default_factory=list)
    wireless_config: list[WirelessSsidSpec] = Field(default_factory=list)
    cloud_mappings: list[CloudMappingSpec] = Field(default_factory=list)
    iot: list[IotRegistrationSpec] = Field(default_factory=list)
    host_dns: list[HostDnsSpec] = Field(default_factory=list)

    assumptions: list[str] = Field(default_factory=list)
    design_decisions: list[DesignDecisionSpec] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    layout_seed: int = 0

    # -- integrity ----------------------------------------------------------

    @model_validator(mode="after")
    def _check_references(self) -> "NetworkSpec":
        device_ids = [d.id for d in self.devices]
        if len(device_ids) != len(set(device_ids)):
            raise ValueError("duplicate device ids in spec")
        devices = set(device_ids)

        link_ids = [l.id for l in self.links]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("duplicate link ids in spec")

        for link in self.links:
            for endpoint in (link.a, link.b):
                if endpoint.device not in devices:
                    raise ValueError(
                        f"link {link.id!r} references unknown device {endpoint.device!r}"
                    )

        requirement_ids = {r.id for r in self.requirements}
        for criterion in self.acceptance_criteria:
            for rid in criterion.requirement_ids:
                if rid not in requirement_ids:
                    raise ValueError(
                        f"acceptance criterion {criterion.id!r} references unknown "
                        f"requirement {rid!r}"
                    )

        subnet_ids = {s.id for s in self.subnets}
        for assignment in self.ip_assignments:
            if assignment.device not in devices:
                raise ValueError(
                    f"ip assignment for unknown device {assignment.device!r}"
                )
            if assignment.subnet not in subnet_ids:
                raise ValueError(
                    f"ip assignment for {assignment.device!r} references unknown "
                    f"subnet {assignment.subnet!r}"
                )

        vlan_ids = {v.vlan_id for v in self.vlans}
        for subnet in self.subnets:
            if subnet.vlan_id is not None and subnet.vlan_id not in vlan_ids:
                raise ValueError(
                    f"subnet {subnet.id!r} references undefined vlan {subnet.vlan_id}"
                )

        for flow_source in self._flow_endpoints():
            if flow_source not in devices:
                raise ValueError(f"flow references unknown device {flow_source!r}")

        for policy in self.policies:
            for target in policy.apply_to:
                if target not in devices:
                    raise ValueError(
                        f"policy {policy.id!r} applies to unknown device {target!r}"
                    )

        for owner in self._feature_devices():
            if owner not in devices:
                raise ValueError(f"feature references unknown device {owner!r}")

        return self

    def _feature_devices(self) -> list[str]:
        """Every device id named by a Stage 3 feature block."""
        out: list[str] = []
        routing = self.routing
        for group in (
            routing.hsrp, routing.dhcp_relays, routing.tunnels,
            routing.ipsec, routing.ssh, routing.management,
        ):
            out.extend(item.device for item in group)
        out.extend(item.device for item in routing.dhcp_pools)
        if routing.bgp is not None:
            out.extend(item.device for item in routing.bgp.per_device)
        if routing.ipv6 is not None:
            out.extend(item.device for item in routing.ipv6.interfaces)
            out.extend(item.device for item in routing.ipv6.ospf)
        switching = self.switching
        for group in (
            switching.svis, switching.etherchannels, switching.stp,
            switching.port_security, switching.vtp, switching.voice_vlans,
            switching.management_ips,
        ):
            out.extend(item.device for item in group)
        for group in (
            self.firewall.nat, self.firewall.dhcp, self.firewall.object_groups,
            self.services, self.wireless_config, self.iot, self.host_dns,
        ):
            out.extend(item.device for item in group)
        out.extend(item.cloud for item in self.cloud_mappings)
        out.extend(item.modem for item in self.cloud_mappings)
        return out

    def _flow_endpoints(self) -> list[str]:
        out: list[str] = []
        for criterion in self.acceptance_criteria:
            test = criterion.test
            if isinstance(test, FlowTestSpec):
                out.extend((test.source, test.destination))
        return out

    # -- identity -----------------------------------------------------------

    def spec_hash(self) -> str:
        """Stable content hash of the spec (the seed of the build fingerprint)."""
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()

    # -- serialization ------------------------------------------------------

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "NetworkSpec":
        """Parse and validate a spec. Free-form LLM output fails here, loudly."""
        import pydantic

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"spec is not valid JSON: {exc}") from exc
        try:
            return cls.model_validate(data)
        except pydantic.ValidationError as exc:
            raise ValueError(f"spec failed typed validation: {exc}") from exc

    # -- lookups ------------------------------------------------------------

    def device_by_id(self, device_id: str) -> DeviceSpec | None:
        for device in self.devices:
            if device.id == device_id:
                return device
        return None

    def requirement_by_id(self, requirement_id: str) -> RequirementSpec | None:
        for requirement in self.requirements:
            if requirement.id == requirement_id:
                return requirement
        return None

    def subnet_by_id(self, subnet_id: str) -> SubnetSpec | None:
        for subnet in self.subnets:
            if subnet.id == subnet_id:
                return subnet
        return None
