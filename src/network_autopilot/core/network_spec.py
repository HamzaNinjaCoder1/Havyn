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
    OTHER = "other"


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


AcceptanceTest = Annotated[
    Union[FlowTestSpec, StructuralTestSpec, ConfigTestSpec],
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


class DeviceSpec(SpecModel):
    id: str
    role: DeviceRole
    model_hint: str | None = None
    interface_requirements: list[InterfaceRequirement] = Field(default_factory=list)
    floor: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _check_id(self) -> "DeviceSpec":
        validate_id(self.id, "device id")
        return self


class EndpointSpec(SpecModel):
    device: str
    interface_hint: str | None = None


class LinkSpec(SpecModel):
    id: str
    a: EndpointSpec
    b: EndpointSpec
    media: Literal["auto", "straight", "crossover", "fiber", "serial"] = "auto"
    trunk: bool = False

    @model_validator(mode="after")
    def _check_id(self) -> "LinkSpec":
        validate_id(self.id, "link id")
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


class OspfSpec(SpecModel):
    process_id: int = Field(default=1, ge=1)
    networks: list[str] = Field(min_length=1)
    area: str = "0"


class RoutingSpec(SpecModel):
    mode: Literal["connected", "static", "ospf", "eigrp", "rip"] = "connected"
    static_routes: list[StaticRouteSpec] = Field(default_factory=list)
    ospf: OspfSpec | None = None


class AclRuleSpec(SpecModel):
    action: Literal["permit", "deny"]
    protocol: Literal["ip", "icmp", "tcp", "udp"] = "ip"
    source: str
    destination: str
    dest_port: int | None = Field(default=None, ge=1, le=65535)
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

        return self

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
