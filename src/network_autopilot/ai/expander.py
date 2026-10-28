"""Deterministic intent -> NetworkSpec expander (S5.2).

The expander is the wall between the AI layer and the engine: the intent says
WHAT the user wants, this module says exactly how Havyn builds it. Same
intent + same EXPANDER_VERSION -> the same NetworkSpec, byte for byte
(deterministic by construction: no randomness, no environment reads, no
dict-order dependence).

Every choice the expander makes that the user did not state is recorded as a
``DesignDecisionSpec`` (source ``engine``) in the produced spec, and every
ledger requirement (REQ-xxx) is carried into the spec as an ``R-xxx``
requirement with the mapping returned alongside.

v1 site shape (single verified pattern for every site):

    router (gateway, router-on-a-stick for multi-VLAN: T16 sub-interfaces)
      -> access switches (daisy-chained per area, T01/T16 shape)
      -> hosts / servers / APs / IoT

The L3-core-with-SVI shape (T03) is deliberately NOT used by v1: the spec
lint counts DHCP servers by role and an l3_switch is not one, so a DHCP
campus on SVIs would refuse. Router-on-a-stick keeps every VLAN's pool on
the router, which is the live-verified DHCP shape (S3.A7/S3.D4).

Other v1 shapes:

- Internet: WAN handoff from the first site's router (T10 / S3.A6 NAT-PAT),
  optionally behind an ASA perimeter (T07 / S3.C1) with an optional DMZ
  (S3.C2), with an optional public test server for flow criteria.
- Multi-site: serial WAN star from the first site's router (T04 / S3.A9).
- Wireless: AP + wireless clients (S3.E1/E3). The custom-SSID configuration
  is NOT emitted: PT's client profile API rejects custom SSIDs (measured),
  so expansion ignores intent SSIDs and records an assumption.
- IoT: cabled Smart LEDs (T14/T16). No registration spec: PT has no
  server-side IoT registration process (measured), so expansion ignores
  registration and records an assumption.

Address allocation inside a subnet is deterministic and collision-free:
gateway .1, servers .10+, static hosts .20+, DHCP pools cover the rest.

Anything else refuses by name (rule 9): ``ExpansionRefusal`` carries the
reason so the AI layer can explain or ask.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from ..core.ids import canonical_json
from ..core.network_spec import (
    AcceptanceCriterionSpec,
    AclRuleSpec,
    DeviceSpec,
    DesignDecisionSpec,
    EndpointSpec,
    FlowTestSpec,
    HostDnsSpec,
    IpAssignmentSpec,
    LinkSpec,
    NetworkSpec,
    PolicySpec,
    ProvenanceSpec,
    RequirementSpec,
    RoutingSpec,
    ServerServiceSpec,
    StaticRouteSpec,
    StructuralTestSpec,
    SubnetSpec,
    SwitchingSpec,
    VlanSpec,
    WirelessTestSpec,
)
from .intent import (
    HavynIntent,
    HostDevice,
    HostGroupIntent,
    SiteIntent,
    VlanIntent,
)

EXPANDER_VERSION = "1.1.0"

# Deterministic sizing rules (recorded as design decisions, not secrets).
ACCESS_PORTS_PER_SWITCH = 20  # 24-port switch minus uplink/AP reserve
WIRELESS_RADIO = frozenset(
    {HostDevice.LAPTOP.value, HostDevice.SMARTPHONE.value, HostDevice.TABLET.value}
)


class ExpansionRefusal(ValueError):
    """The intent asks for something expander v1 will not build. By name."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class VlanInstance:
    """One concrete (site, vlan) pair after instantiation."""

    site: str
    vlan: VlanIntent
    subnet_id: str
    cidr: str
    gateway: str
    vlan_number: int
    dhcp: bool


@dataclass
class ExpansionResult:
    """The spec plus everything the traceability chain needs."""

    spec: NetworkSpec
    requirement_map: dict[str, str] = field(default_factory=dict)  # REQ -> R
    vlan_instances: list[VlanInstance] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "expander_version": EXPANDER_VERSION,
            "spec_name": self.spec.name,
            "spec_hash": self.spec.spec_hash(),
            "devices": len(self.spec.devices),
            "links": len(self.spec.links),
            "subnets": len(self.spec.subnets),
            "requirements": len(self.spec.requirements),
            "acceptance_criteria": len(self.spec.acceptance_criteria),
            "requirement_map": dict(sorted(self.requirement_map.items())),
        }


class _AddressBook:
    """Deterministic, collision-free IPv4 allocation inside one subnet.

    gateway=.1, servers from .10, static hosts from .20. DHCP hosts get no
    address here; the pool covers everything not handed out statically.
    """

    def __init__(self, network: ipaddress.IPv4Network, gateway: str):
        self.network = network
        self.used: set[ipaddress.IPv4Address] = {ipaddress.ip_address(gateway)}

    def take_from(self, start_offset: int) -> str:
        """The first free address at or after `start_offset`."""
        hosts = list(self.network.hosts())
        for offset in range(start_offset, len(hosts)):
            addr = hosts[offset]
            if addr not in self.used:
                self.used.add(addr)
                return str(addr)
        raise ExpansionRefusal(
            "subnet-address-exhausted",
            f"{self.network} has no free address from offset {start_offset}",
        )


def _prefix_for_hosts(hosts: int) -> int:
    """Smallest prefix whose usable capacity covers hosts (+ gateway)."""
    need = hosts + 1
    for prefix in range(24, 15, -1):
        if (2 ** (32 - prefix) - 2) >= need:
            return prefix
    raise ExpansionRefusal(
        "vlan-too-large",
        f"a VLAN needs {hosts} hosts; expander v1 sizes VLANs up to a /16",
    )


def _next_network(after_broadcast: ipaddress.IPv4Address, prefix: int) -> ipaddress.IPv4Network:
    """The next aligned network of `prefix` strictly after this broadcast."""
    block = 2 ** (32 - prefix)
    aligned = (int(after_broadcast) + 1 + block - 1) // block * block
    return ipaddress.ip_network((aligned, prefix))


class IntentExpander:
    """Builds a complete NetworkSpec from a HavynIntent. Deterministic."""

    def __init__(self, intent: HavynIntent):
        self.intent = intent
        self.devices: list[DeviceSpec] = []
        self.links: list[LinkSpec] = []
        self.vlans_out: list[VlanSpec] = []
        self.subnets: list[SubnetSpec] = []
        self.ip_assignments: list[IpAssignmentSpec] = []
        self.host_dns: list[HostDnsSpec] = []
        self.policies: list[PolicySpec] = []
        self.services: list[ServerServiceSpec] = []
        self.criteria: list[AcceptanceCriterionSpec] = []
        self.decisions: list[DesignDecisionSpec] = []
        self.assumptions: list[str] = []
        self.static_routes: list[StaticRouteSpec] = []
        self.routing_mode: str = intent.routing.mode
        self.instances: list[VlanInstance] = []
        self.address_books: dict[str, _AddressBook] = {}
        self.requirement_map: dict[str, str] = {}
        self._decision_seq = 0
        self._link_seq = 0
        self._criterion_seq = 0

    # -- small helpers ---------------------------------------------------------

    def _decision(self, statement: str, rationale: str | None = None) -> None:
        self._decision_seq += 1
        self.decisions.append(
            DesignDecisionSpec(
                id=f"D-{self._decision_seq:03d}",
                statement=statement,
                rationale=rationale,
                source="engine",
            )
        )

    def _link(self, a: str, b: str, *, trunk: bool = False, media: str = "auto",
              a_hint: str | None = None) -> str:
        self._link_seq += 1
        lid = f"l-{self._link_seq:03d}"
        self.links.append(
            LinkSpec(
                id=lid,
                a=EndpointSpec(device=a, interface_hint=a_hint),
                b=EndpointSpec(device=b),
                trunk=trunk,
                media=media,  # type: ignore[arg-type]
            )
        )
        return lid

    def _has_device(self, device_id: str) -> bool:
        return any(d.id == device_id for d in self.devices)

    def _host_device_id(self, group: HostGroupIntent, index: int) -> str:
        return f"{group.id}-{index + 1}"

    def _instance_for(self, vlan_ref: str | None, site_id: str) -> VlanInstance:
        for inst in self.instances:
            if inst.site == site_id and (vlan_ref is None or inst.vlan.id == vlan_ref):
                return inst
        raise ExpansionRefusal(
            "vlan-not-in-site",
            f"vlan {vlan_ref!r} has no instance in site {site_id!r}",
        )

    def _site_area_anchor(self, site: SiteIntent, area_id: str | None) -> str:
        """The switch a server/AP/IoT hangs off: its area's first switch, or
        the site router."""
        target_area = area_id or (site.areas[0].id if site.areas else None)
        if target_area and self._has_device(f"{site.id}-{target_area}-sw1"):
            return f"{site.id}-{target_area}-sw1"
        if self._has_device(f"{site.id}-r1"):
            return f"{site.id}-r1"
        raise ExpansionRefusal(
            "site-without-anchor",
            f"site {site.id!r} has no switch or router to attach devices to; "
            "does it have any areas or host groups?",
        )

    # -- main ------------------------------------------------------------------

    def expand(self) -> ExpansionResult:
        intent = self.intent
        self._refuse_unsupported()
        # Multi-site builds a serial WAN star whose routers must be Router-PT
        # (built-in serial, T04). Router-PT has no GigabitEthernet ports, so
        # those sites' router-on-a-stick sub-interfaces ride FastEthernet0/0.
        self._serial_routers: set[str] = (
            {f"{s.id}-r1" for s in intent.sites} if len(intent.sites) > 1 else set()
        )
        self._instantiate_vlans()
        self._requirement_specs()
        for site in intent.sites:
            self._build_site(site)
        if intent.internet is not None:
            self._build_internet()
        if len(intent.sites) > 1:
            self._build_site_wan()
        self._build_routing()
        self._build_dns()
        self._build_criteria()
        spec = self._assemble()
        return ExpansionResult(
            spec=spec,
            requirement_map=dict(self.requirement_map),
            vlan_instances=list(self.instances),
            decisions=[d.id for d in self.decisions],
        )

    # -- refusals (rule 9: refuse by name) --------------------------------------

    def _refuse_unsupported(self) -> None:
        intent = self.intent
        if intent.firewall is not None and intent.firewall.enabled:
            if intent.firewall.guest_isolation:
                raise ExpansionRefusal(
                    "guest-isolation-unsupported",
                    "Guest-VLAN isolation is not supported by expander v1: no "
                    "live-verified shape puts a guest VLAN behind a verified "
                    "ACL enforcement point yet. Remove guest_isolation or wait "
                    "for a capability update.",
                )
            if intent.firewall.dmz and len(intent.sites) > 1:
                raise ExpansionRefusal(
                    "dmz-multi-site-unsupported",
                    "A DMZ is only verified on a single-site ASA perimeter "
                    "(S3.C2); expander v1 does not combine DMZ with a "
                    "multi-site WAN.",
                )

    # -- VLAN instantiation ------------------------------------------------------

    def _vlan_instances_for_site(self, site_id: str) -> list[VlanIntent]:
        return [v for v in self.intent.vlans if v.site in (None, site_id)]

    def _demand(self) -> dict[tuple[str, str], int]:
        demand: dict[tuple[str, str], int] = {}
        for site, _area, group in self.intent.host_groups():
            vlan_id = group.vlan or "__default__"
            demand[(site.id, vlan_id)] = demand.get((site.id, vlan_id), 0) + group.count
        for server in self.intent.servers:
            vlan_id = server.vlan or "__default__"
            demand[(server.site, vlan_id)] = demand.get((server.site, vlan_id), 0) + 1
        return demand

    def _instantiate_vlans(self) -> None:
        demand = self._demand()
        site_index = 0
        for site in self.intent.sites:
            site_index += 1
            site_vlans = self._vlan_instances_for_site(site.id)
            needed_default = demand.get((site.id, "__default__"), 0) > 0
            synthesized = False
            if not site_vlans and needed_default:
                site_vlans = [
                    VlanIntent(
                        id=f"{site.id}-users",
                        name=f"{site.id} users",
                        required_hosts=demand[(site.id, "__default__")],
                    )
                ]
                synthesized = True
                self.assumptions.append(
                    f"site {site.id}: no VLAN declared; a default user VLAN "
                    f"'{site.id}-users' was synthesized sized for "
                    f"{demand[(site.id, '__default__')]} hosts"
                )
            elif site_vlans and needed_default:
                self.assumptions.append(
                    f"site {site.id}: host groups without a vlan ref land in "
                    f"'{site_vlans[0].id}' (the site's first VLAN)"
                )

            allocations: list[tuple[VlanIntent, int]] = []
            for vlan in site_vlans:
                hosts = demand.get((site.id, vlan.id), 0)
                if hosts == 0 and (vlan.is_guest or vlan.is_management):
                    hosts = 5  # minimum useful pool for an empty special VLAN
                allocations.append((vlan, hosts))

            cursor_broadcast: ipaddress.IPv4Address | None = None
            for vlan, host_count in allocations:
                if vlan.cidr is not None:
                    network = ipaddress.ip_network(vlan.cidr, strict=False)
                    site_block = ipaddress.ip_network(f"10.{site_index}.0.0/16")
                    if not network.subnet_of(site_block):
                        raise ExpansionRefusal(
                            "vlan-outside-site-block",
                            f"VLAN {vlan.id!r} fixes {vlan.cidr}, outside the site "
                            f"block {site_block} expander v1 allocates from. Give "
                            "a cidr inside the block or use required_hosts.",
                        )
                else:
                    prefix = _prefix_for_hosts(host_count)
                    if cursor_broadcast is None:
                        network = ipaddress.ip_network(f"10.{site_index}.0.0/{prefix}")
                    else:
                        network = _next_network(cursor_broadcast, prefix)
                gateway = str(next(network.hosts()))
                subnet_id = f"net-{site.id}-{vlan.id}"
                vlan_number = len(self.vlans_out) + 1
                self.instances.append(
                    VlanInstance(
                        site=site.id,
                        vlan=vlan,
                        subnet_id=subnet_id,
                        cidr=str(network),
                        gateway=gateway,
                        vlan_number=vlan_number,
                        dhcp=vlan.dhcp,
                    )
                )
                self.address_books[subnet_id] = _AddressBook(network, gateway)
                self.vlans_out.append(
                    VlanSpec(vlan_id=vlan_number, name=vlan.name[:32], purpose=vlan.purpose)
                )
                self.subnets.append(
                    SubnetSpec(
                        id=subnet_id,
                        vlan_id=vlan_number,
                        purpose=vlan.purpose or vlan.name,
                        cidr=str(network),
                        gateway=gateway,
                        dhcp=vlan.dhcp,
                    )
                )
                cursor_broadcast = network.broadcast_address
            if synthesized and site_vlans[0].cidr is None:
                self._decision(
                    f"site {site.id}: synthesized VLAN '{site_vlans[0].id}' in "
                    f"{self.instances[-1].cidr}",
                    "the user named no VLANs; sizing follows the host count",
                )

    # -- site shape: router + access switches --------------------------------------

    def _build_site(self, site: SiteIntent) -> None:
        router_id = f"{site.id}-r1"
        self.devices.append(DeviceSpec(id=router_id, role="router"))
        instances = [i for i in self.instances if i.site == site.id]
        multi_vlan = len(instances) > 1
        base = (
            "FastEthernet0/0" if router_id in self._serial_routers
            else "GigabitEthernet0/0"
        )
        if multi_vlan:
            # Router-on-a-stick: one sub-interface per VLAN (T16 shape).
            for inst in instances:
                self.ip_assignments.append(
                    IpAssignmentSpec(
                        device=router_id,
                        subnet=inst.subnet_id,
                        ip=inst.gateway,
                        interface_hint=f"{base}.{inst.vlan_number}",
                    )
                )
            self._decision(
                f"site {site.id}: router-on-a-stick, {len(instances)} sub-"
                f"interfaces on {base}",
                "T16 sub-interface shape; DHCP pools stay on the router",
            )
        else:
            inst = instances[0]
            self.ip_assignments.append(
                IpAssignmentSpec(device=router_id, subnet=inst.subnet_id, ip=inst.gateway)
            )
            self._decision(
                f"site {site.id}: gateway router {router_id} holds {inst.gateway} "
                f"in {inst.subnet_id}",
                "single-VLAN site shape (library T01)",
            )

        # Core switch only when several access switches need a common trunk.
        area_switches = self._area_switch_counts(site)
        total_switches = sum(area_switches.values())
        core_id: str | None = None
        if multi_vlan and total_switches > 1:
            core_id = f"{site.id}-swcore"
            self.devices.append(DeviceSpec(id=core_id, role="switch"))
            self._link(router_id, core_id, trunk=True, a_hint=base)
            self._decision(
                f"site {site.id}: core switch {core_id} carries all VLAN trunks",
                "T16 core shape",
            )

        first_switch: str | None = None
        area_switch_ids: dict[str, list[str]] = {}
        for area in site.areas:
            count = area_switches.get(area.id, 1)
            ids: list[str] = []
            for n in range(1, count + 1):
                sid = f"{site.id}-{area.id}-sw{n}"
                ids.append(sid)
                self.devices.append(DeviceSpec(id=sid, role="switch"))
                if n > 1:
                    self._link(ids[-2], sid)
            area_switch_ids[area.id] = ids
            if first_switch is None:
                first_switch = ids[0]
            uplink = core_id or router_id
            self._link(
                uplink, ids[0], trunk=multi_vlan,
                a_hint=base if multi_vlan and uplink == router_id else None,
            )
            if count > 1:
                self._decision(
                    f"site {site.id}/{area.id}: {count} access switch(es) "
                    "daisy-chained, 20 access ports per switch",
                    "2960-24TT port budget with uplink reserve",
                )

        if not site.areas:
            # No areas declared: servers/APs/IoT hang directly off the router.
            first_switch = None

        self._place_hosts(site, area_switch_ids, first_switch or router_id)
        self._place_servers(site)
        self._place_wireless(site)
        self._place_iot(site)

    def _area_switch_counts(self, site: SiteIntent) -> dict[str, int]:
        counts: dict[str, int] = {}
        for area in site.areas:
            ports = 0
            for group in area.host_groups:
                if group.device.value in WIRELESS_RADIO:
                    continue
                ports += group.count
            for thing in self.intent.iot:
                if thing.site == site.id and thing.area == area.id:
                    ports += thing.count
            counts[area.id] = max(1, -(-ports // ACCESS_PORTS_PER_SWITCH))
        return counts

    def _place_hosts(
        self,
        site: SiteIntent,
        area_switch_ids: dict[str, list[str]],
        default_switch: str,
    ) -> None:
        for area in site.areas:
            switch_ids = area_switch_ids.get(area.id, [default_switch])
            for group in area.host_groups:
                inst = self._instance_for(group.vlan, site.id)
                wired = group.device.value not in WIRELESS_RADIO
                for index in range(group.count):
                    device_id = self._host_device_id(group, index)
                    self.devices.append(DeviceSpec(id=device_id, role=group.device.value))
                    if group.addressing == "static" or (
                        not inst.dhcp and group.addressing is None
                    ):
                        self.ip_assignments.append(
                            IpAssignmentSpec(
                                device=device_id,
                                subnet=inst.subnet_id,
                                ip=self.address_books[inst.subnet_id].take_from(20),
                            )
                        )
                    else:
                        self.ip_assignments.append(
                            IpAssignmentSpec(
                                device=device_id, subnet=inst.subnet_id, dhcp=True
                            )
                        )
                    if wired:
                        switch = switch_ids[index % len(switch_ids)]
                        self._link(switch, device_id)

    def _place_servers(self, site: SiteIntent) -> None:
        for server in self.intent.servers:
            if server.site != site.id:
                continue
            inst = self._instance_for(server.vlan, site.id)
            self.devices.append(DeviceSpec(id=server.id, role="server"))
            self.ip_assignments.append(
                IpAssignmentSpec(
                    device=server.id,
                    subnet=inst.subnet_id,
                    ip=self.address_books[inst.subnet_id].take_from(10),
                )
            )
            anchor = self._site_area_anchor(site, None)
            self._link(anchor, server.id)
            for service in self._server_service_specs(server):
                self.services.append(service)

    def _server_service_specs(self, server) -> list[ServerServiceSpec]:
        out: list[ServerServiceSpec] = []
        for service in server.services:
            spec = ServerServiceSpec(device=server.id, service=service)
            if service == "dns" and server.records:
                spec = spec.model_copy(update={"records": server.records})
            if service in ("http", "https") and server.pages:
                spec = spec.model_copy(update={"pages": server.pages})
            out.append(spec)
        return out

    def _place_wireless(self, site: SiteIntent) -> None:
        for cell in self.intent.wireless:
            if cell.site != site.id:
                continue
            ap_id = f"{cell.id}-ap"
            self.devices.append(DeviceSpec(id=ap_id, role="access_point"))
            anchor = self._site_area_anchor(site, cell.area)
            self._link(anchor, ap_id)
            for s2, _a2, group in self.intent.host_groups():
                if s2.id != site.id or group.id not in cell.client_group_ids:
                    continue
                for index in range(group.count):
                    self._link(ap_id, self._host_device_id(group, index),
                               media="wireless")
            self.assumptions.append(
                f"wireless cell {cell.id}: SSID {cell.ssid!r} is not configurable "
                "in Packet Tracer (client profile API rejects custom SSIDs); "
                "the AP ships with the default profile"
            )
            self._decision(
                f"wireless cell {cell.id}: AP {ap_id} on {anchor} serving "
                f"{len(cell.client_group_ids)} client group(s)",
                "AP + wireless clients shape (S3.E1/E3)",
            )

    def _place_iot(self, site: SiteIntent) -> None:
        for thing in self.intent.iot:
            if thing.site != site.id:
                continue
            anchor = self._site_area_anchor(site, thing.area)
            for index in range(thing.count):
                device_id = f"{thing.id}-{index + 1}"
                self.devices.append(DeviceSpec(id=device_id, role="iot"))
                self._link(anchor, device_id)
            self.assumptions.append(
                f"iot {thing.id}: devices are created and cabled only; Packet "
                "Tracer exposes no server-side IoT registration process, so "
                "no registration is emitted"
            )

    # -- internet -----------------------------------------------------------------

    def _build_internet(self) -> None:
        internet = self.intent.internet
        first_site = self.intent.sites[0]
        behind_firewall = (
            self.intent.firewall is not None and self.intent.firewall.enabled
        )
        if behind_firewall:
            self._build_internet_behind_firewall(internet, first_site)
        else:
            self._build_internet_direct(internet, first_site)

    def _build_internet_direct(self, internet, first_site: SiteIntent) -> None:
        """T10 shape: site edge router <-> ISP router over 203.0.113.0/30."""
        self.subnets.append(
            SubnetSpec(id="net-wan", purpose="internet WAN", cidr="203.0.113.0/30")
        )
        edge = f"{first_site.id}-r1"
        self.ip_assignments.append(
            IpAssignmentSpec(device=edge, subnet="net-wan", ip="203.0.113.1")
        )
        self.devices.append(DeviceSpec(id="isp", role="router", model_hint="Router-PT"))
        self.ip_assignments.append(
            IpAssignmentSpec(device="isp", subnet="net-wan", ip="203.0.113.2")
        )
        self._link(edge, "isp")
        if internet.public_server:
            self.subnets.append(
                SubnetSpec(
                    id="net-pub",
                    purpose="public server segment",
                    cidr="198.51.100.0/24",
                    gateway="198.51.100.1",
                )
            )
            self.devices.append(DeviceSpec(id="srv-int", role="server"))
            self.ip_assignments.append(
                IpAssignmentSpec(device="isp", subnet="net-pub", ip="198.51.100.1")
            )
            self.ip_assignments.append(
                IpAssignmentSpec(device="srv-int", subnet="net-pub", ip="198.51.100.10")
            )
            self._link("isp", "srv-int")

    def _build_internet_behind_firewall(self, internet, first_site: SiteIntent) -> None:
        """T07 shape: the ASA is the inside gateway; the ISP sits outside."""
        firewall = self.intent.firewall
        if firewall.model == "asa5505" and firewall.dmz:
            raise ExpansionRefusal(
                "asa5505-no-dmz",
                "the ASA 5505 has no third zone; a DMZ needs the 5506-X.",
            )
        instances = [i for i in self.instances if i.site == first_site.id]
        if len(instances) != 1:
            raise ExpansionRefusal(
                "firewall-needs-single-vlan",
                "expander v1 places the ASA as the inside gateway (T07 shape), "
                "which requires the internet-facing site to have exactly one "
                f"VLAN; site {first_site.id!r} has {len(instances)}.",
            )
        if not first_site.areas:
            raise ExpansionRefusal(
                "firewall-needs-access-switch",
                "the ASA inside hangs off an access switch (T07 shape); the "
                "internet-facing site defines no areas.",
            )
        inside = instances[0]
        router_id = f"{first_site.id}-r1"
        # The site router is replaced by the ASA as inside gateway.
        self.devices = [d for d in self.devices if d.id != router_id]
        self.ip_assignments = [
            a for a in self.ip_assignments if a.device != router_id
        ]
        self.links = [
            l for l in self.links
            if router_id not in (l.a.device, l.b.device)
        ]
        model = "5505" if firewall.model == "asa5505" else "5506-X"
        self.devices.append(DeviceSpec(id="fw", role="firewall", model_hint=model))
        self.ip_assignments.append(
            IpAssignmentSpec(device="fw", subnet=inside.subnet_id, ip=inside.gateway,
                             interface_hint="GigabitEthernet1/1")
        )
        anchor = f"{first_site.id}-{first_site.areas[0].id}-sw1"
        self._link("fw", anchor, a_hint="GigabitEthernet1/1")
        self._decision(
            f"firewall: ASA {model} is the inside gateway at {inside.gateway} "
            f"on {inside.subnet_id}",
            "ASA perimeter shape (T07)",
        )
        self.subnets.append(
            SubnetSpec(id="net-wan", purpose="internet WAN", cidr="203.0.113.0/30")
        )
        self.ip_assignments.append(
            IpAssignmentSpec(device="fw", subnet="net-wan", ip="203.0.113.1",
                             interface_hint="GigabitEthernet1/3")
        )
        self.devices.append(DeviceSpec(id="isp", role="router", model_hint="Router-PT"))
        self.ip_assignments.append(
            IpAssignmentSpec(device="isp", subnet="net-wan", ip="203.0.113.2")
        )
        self._link("fw", "isp", a_hint="GigabitEthernet1/3")
        if internet.public_server:
            self.subnets.append(
                SubnetSpec(
                    id="net-pub",
                    purpose="public server segment",
                    cidr="198.51.100.0/24",
                    gateway="198.51.100.1",
                )
            )
            self.devices.append(DeviceSpec(id="srv-int", role="server"))
            self.ip_assignments.append(
                IpAssignmentSpec(device="isp", subnet="net-pub", ip="198.51.100.1")
            )
            self.ip_assignments.append(
                IpAssignmentSpec(device="srv-int", subnet="net-pub", ip="198.51.100.10")
            )
            self._link("isp", "srv-int")
        if firewall.dmz:
            self.subnets.append(
                SubnetSpec(
                    id="net-dmz",
                    purpose="DMZ server segment",
                    cidr="192.168.100.0/24",
                    gateway="192.168.100.1",
                )
            )
            self.devices.append(DeviceSpec(id="srv-dmz", role="server"))
            self.ip_assignments.append(
                IpAssignmentSpec(device="fw", subnet="net-dmz", ip="192.168.100.1",
                                 interface_hint="GigabitEthernet1/2")
            )
            self.ip_assignments.append(
                IpAssignmentSpec(device="srv-dmz", subnet="net-dmz", ip="192.168.100.10")
            )
            self._link("fw", "srv-dmz", a_hint="GigabitEthernet1/2")
            self._decision(
                "firewall: DMZ zone 192.168.100.0/24 with srv-dmz at .10",
                "ASA third-zone shape (S3.C2)",
            )
        # T07's verified stateless-ASA policy: inside may ping out, the
        # echo-reply may return, outside-initiated traffic stays blocked.
        self.policies.append(
            PolicySpec(
                id="acl-perimeter",
                kind="acl",
                apply_to=["fw"],
                rules=[
                    AclRuleSpec(
                        action="permit", protocol="icmp",
                        source=inside.cidr, destination="any",
                        description="inside-initiated ICMP may leave",
                    ),
                    AclRuleSpec(
                        action="permit", protocol="icmp",
                        source="any", destination="any", icmp_type="echo-reply",
                        description="return path for inside-initiated pings "
                        "(PT's ASA is stateless)",
                    ),
                ],
            )
        )

    # -- multi-site WAN -------------------------------------------------------------

    def _build_site_wan(self) -> None:
        first_router = f"{self.intent.sites[0].id}-r1"
        # Router-PT carries built-in serial ports (T04's verified shape); the
        # default 2911 would need a serial module the solver cannot assume.
        self._set_model_hint(first_router, "Router-PT")
        for index, site in enumerate(self.intent.sites[1:], start=2):
            peer = f"{site.id}-r1"
            self._set_model_hint(peer, "Router-PT")
            peer = f"{site.id}-r1"
            cidr = f"10.255.{index}.0/30"
            subnet_id = f"net-wan-{index}"
            self.subnets.append(
                SubnetSpec(id=subnet_id, purpose=f"serial WAN to {site.id}", cidr=cidr)
            )
            self.ip_assignments.append(
                IpAssignmentSpec(device=first_router, subnet=subnet_id,
                                 ip=f"10.255.{index}.1")
            )
            self.ip_assignments.append(
                IpAssignmentSpec(device=peer, subnet=subnet_id,
                                 ip=f"10.255.{index}.2")
            )
            self._link(first_router, peer, media="serial")
            self._decision(
                f"multi-site: serial WAN {cidr} between {first_router} and {peer}",
                "serial WAN star shape (T04/S3.A9)",
            )

    # -- routing ----------------------------------------------------------------------

    def _set_model_hint(self, device_id: str, model_hint: str) -> None:
        for i, device in enumerate(self.devices):
            if device.id == device_id:
                self.devices[i] = device.model_copy(update={"model_hint": model_hint})
                return
        raise ExpansionRefusal(
            "wan-device-missing",
            f"the serial WAN needs {device_id!r}, which was never created",
        )

    def _build_routing(self) -> None:
        intent = self.intent
        has_internet = intent.internet is not None
        behind_firewall = intent.firewall is not None and intent.firewall.enabled
        multi_site = len(intent.sites) > 1

        if self.routing_mode == "connected" and (has_internet or multi_site
                                                 or len(self.instances) > 1):
            self.routing_mode = "static"
            self.assumptions.append(
                "routing mode 'connected' was upgraded to 'static' because the "
                "design has more than one layer-3 domain"
            )

        if self.routing_mode == "static":
            self._build_static_routes(has_internet, behind_firewall, multi_site)

        self.routing_out = RoutingSpec(
            mode=self.routing_mode,  # type: ignore[arg-type]
            static_routes=self.static_routes,
        )

    def _build_static_routes(
        self, has_internet: bool, behind_firewall: bool, multi_site: bool
    ) -> None:
        intent = self.intent
        first_site = intent.sites[0]
        first_router = f"{first_site.id}-r1"

        if behind_firewall:
            # T07: the ASA holds the default route; the ISP needs the way back.
            self.static_routes.append(
                StaticRouteSpec(device="fw", destination="0.0.0.0/0",
                                next_hop="203.0.113.2")
            )
            if intent.internet.public_server:
                inside = next(i for i in self.instances if i.site == first_site.id)
                self.static_routes.append(
                    StaticRouteSpec(device="isp", destination=inside.cidr,
                                    next_hop="203.0.113.1")
                )
            return

        if has_internet:
            edge = first_router
            self.static_routes.append(
                StaticRouteSpec(device=edge, destination="0.0.0.0/0",
                                next_hop="203.0.113.2")
            )
            if intent.internet.public_server:
                self.static_routes.append(
                    StaticRouteSpec(device="isp", destination="10.0.0.0/8",
                                    next_hop="203.0.113.1")
                )
            for other_site in intent.sites[1:]:
                for inst in self.instances:
                    if inst.site == other_site.id:
                        self.static_routes.append(
                            StaticRouteSpec(device="isp", destination=inst.cidr,
                                            next_hop="203.0.113.1")
                        )

        for index, other_site in enumerate(intent.sites[1:], start=2):
            peer = f"{other_site.id}-r1"
            for inst in self.instances:
                if inst.site == first_site.id:
                    self.static_routes.append(
                        StaticRouteSpec(device=peer, destination=inst.cidr,
                                        next_hop=f"10.255.{index}.1")
                    )
                if inst.site == other_site.id:
                    self.static_routes.append(
                        StaticRouteSpec(device=first_router, destination=inst.cidr,
                                        next_hop=f"10.255.{index}.2")
                    )
            if has_internet:
                self.static_routes.append(
                    StaticRouteSpec(device=peer, destination="0.0.0.0/0",
                                    next_hop=f"10.255.{index}.1")
                )

    # -- DNS ---------------------------------------------------------------------------

    def _build_dns(self) -> None:
        for server in self.intent.servers:
            if "dns" not in server.services:
                continue
            ip = next(
                (a.ip for a in self.ip_assignments if a.device == server.id), None
            )
            if ip is None:
                continue
            for site, _area, group in self.intent.host_groups():
                if group.device.value in WIRELESS_RADIO:
                    continue
                self.host_dns.append(
                    HostDnsSpec(
                        device=self._host_device_id(group, 0), dns_server=ip
                    )
                )

    # -- criteria ------------------------------------------------------------------------

    # NetworkSpec requirement kinds are the engine's own vocabulary; the
    # intent's "security" maps onto "isolation", which is how the engine
    # expresses protection requirements.
    _KIND_MAP = {"security": "isolation"}

    def _requirement_specs(self) -> list[RequirementSpec]:
        out: list[RequirementSpec] = []
        used: set[str] = set()
        for requirement in self.intent.requirements:
            number = requirement.id.split("-", 1)[1]
            rid = f"R-{number}"
            while rid in used:
                rid = f"R-{number}-alt{len(used)}"
            used.add(rid)
            self.requirement_map[requirement.id] = rid
            kind = self._KIND_MAP.get(requirement.type.value, requirement.type.value)
            out.append(
                RequirementSpec(
                    id=rid,
                    statement=requirement.statement,
                    kind=kind,  # type: ignore[arg-type]
                )
            )
        if not out:
            self.requirement_map["__default__"] = "R-001"
            out.append(
                RequirementSpec(
                    id="R-001",
                    statement="The intent's topology builds and its host "
                    "groups reach each other as designed",
                    kind="connectivity",
                )
            )
        return out

    def _rid(self, *kinds: str) -> str:
        """Deterministic requirement attachment for a generated criterion."""
        for kind in kinds:
            for requirement in self.intent.requirements:
                if requirement.type.value == kind:
                    return self.requirement_map[requirement.id]
        if "__default__" in self.requirement_map:
            return self.requirement_map["__default__"]
        return next(iter(self.requirement_map.values()))

    def _criterion(self, title: str, test, *rid_kinds: str) -> None:
        self._criterion_seq += 1
        self.criteria.append(
            AcceptanceCriterionSpec(
                id=f"A-{self._criterion_seq:03d}",
                requirement_ids=[self._rid(*rid_kinds)],
                title=title,
                test=test,
            )
        )

    def _build_criteria(self) -> None:
        groups = [
            (s, a, g)
            for s, a, g in self.intent.host_groups()
            if g.device.value not in ("printer", "ip_phone")
        ]

        if groups and groups[0][2].count >= 2:
            base_group = groups[0][2]
            self._criterion(
                f"{base_group.id}-1 reaches {base_group.id}-2 (reachable)",
                FlowTestSpec(
                    source=self._host_device_id(base_group, 0),
                    destination=self._host_device_id(base_group, 1),
                    expect="reachable",
                ),
                "connectivity",
            )

        made = 0
        if groups:
            base_site, _base_area, base_group = groups[0]
            for site, _area, group in groups[1:]:
                if made >= 6:
                    break
                base_vlan = base_group.vlan or "__default__"
                group_vlan = group.vlan or "__default__"
                if site.id == base_site.id and group_vlan == base_vlan:
                    continue
                self._criterion(
                    f"{group.id}-1 reaches {base_group.id}-1 (reachable)",
                    FlowTestSpec(
                        source=self._host_device_id(group, 0),
                        destination=self._host_device_id(base_group, 0),
                        expect="reachable",
                    ),
                    "connectivity",
                )
                made += 1

        for server in self.intent.servers[:3]:
            if groups:
                self._criterion(
                    f"{groups[0][2].id}-1 reaches {server.id} (reachable)",
                    FlowTestSpec(
                        source=self._host_device_id(groups[0][2], 0),
                        destination=server.id,
                        expect="reachable",
                    ),
                    "connectivity",
                    "service",
                )

        if (
            self.intent.internet is not None
            and self.intent.internet.public_server
            and groups
        ):
            self._criterion(
                f"{groups[0][2].id}-1 reaches srv-int through the internet "
                "path (reachable)",
                FlowTestSpec(
                    source=self._host_device_id(groups[0][2], 0),
                    destination="srv-int",
                    expect="reachable",
                ),
                "connectivity",
            )
            if self.intent.firewall is not None and self.intent.firewall.enabled:
                self._criterion(
                    f"srv-int cannot initiate to {groups[0][2].id}-1 (blocked)",
                    FlowTestSpec(
                        source="srv-int",
                        destination=self._host_device_id(groups[0][2], 0),
                        expect="blocked",
                    ),
                    "security",
                    "isolation",
                )

        for cell in self.intent.wireless[:4]:
            for group_id in cell.client_group_ids[:2]:
                for _s2, _a2, group in self.intent.host_groups():
                    if group.id == group_id:
                        self._criterion(
                            f"{group.id}-1 associated to {cell.id}-ap",
                            WirelessTestSpec(
                                client=self._host_device_id(group, 0),
                                access_point=f"{cell.id}-ap",
                            ),
                            "connectivity",
                        )
                        break

        for lid in [l.id for l in self.links[:3]]:
            self._criterion(
                f"link {lid} exists",
                StructuralTestSpec(check="link_exists", target=lid),
                "operational",
                "connectivity",
            )

    # -- assembly --------------------------------------------------------------------------

    def _assemble(self) -> NetworkSpec:
        intent = self.intent
        return NetworkSpec(
            name=intent.name,
            description=intent.description,
            provenance=ProvenanceSpec(
                generator=f"havyn-intent-expander/{EXPANDER_VERSION}",
                source_prompt=intent.source_excerpt,
                notes=f"intent v{intent.intent_version}",
            ),
            requirements=self._requirement_specs(),
            acceptance_criteria=self.criteria,
            devices=self.devices,
            links=self.links,
            vlans=self.vlans_out,
            subnets=self.subnets,
            ip_assignments=self.ip_assignments,
            routing=RoutingSpec(
                mode=self.routing_mode,  # type: ignore[arg-type]
                static_routes=self.static_routes,
            ),
            policies=self.policies,
            switching=SwitchingSpec(),
            services=self.services,
            host_dns=self.host_dns,
            assumptions=self.assumptions,
            design_decisions=self.decisions,
            constraints=list(intent.constraints),
        )


def expand_intent(intent: HavynIntent) -> ExpansionResult:
    """Expand a validated intent into a complete NetworkSpec."""
    return IntentExpander(intent).expand()


def expansion_key(intent: HavynIntent) -> str:
    """A stable key for same-intent determinism checks."""
    return canonical_json(intent.model_dump(mode="json"))
