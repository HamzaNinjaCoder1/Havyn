"""Deterministic IPv4 address management.

The IPAM engine owns ALL subnet arithmetic. An LLM (or a human) may state
requirements — "40 hosts", "192.168.10.0/24" — but the network address,
broadcast, usable range, gateway position and prefix sizing are computed here,
from `ipaddress`, with a fixed deterministic algorithm. The same input always
produces the same plan; impossible requirements produce a structured failure,
never a silently adjusted one.

Allocation algorithm (deterministic "next-fit with size alignment"):
- subnets are allocated in the order the spec declares them;
- each allocation starts at the first offset >= previous end that is aligned
  to its own block size;
- a fixed-CIDR subnet takes exactly the block its CIDR states;
- a planned subnet gets the smallest prefix whose usable capacity
  (2^(32-p) - 2) covers `required_hosts` (the gateway is one of those hosts).
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, ip_network
from typing import Any

from ..core.network_spec import IpAssignmentSpec, NetworkSpec, SubnetSpec

DEFAULT_POOL = "10.0.0.0/16"


class PlanningFailure(Exception):
    """Addressing cannot be produced as requested. Structured, never silent."""

    def __init__(self, resource: str, problem: str, detail: str):
        super().__init__(f"{resource}: {problem} ({detail})")
        self.resource = resource
        self.problem = problem
        self.detail = detail

    def to_dict(self) -> dict[str, str]:
        return {"resource": self.resource, "problem": self.problem, "detail": self.detail}


@dataclass(frozen=True)
class PlannedSubnet:
    """One allocated subnet with every address the compiler will need."""

    allocation_id: str
    subnet_id: str
    vlan_id: int | None
    purpose: str | None
    prefix_len: int
    network: str
    broadcast: str
    netmask: str
    usable_start: str
    usable_end: str
    capacity: int
    requested_hosts: int | None
    gateway: str
    dhcp: bool

    def contains(self, address: str) -> bool:
        return IPv4Address(address) in IPv4Network(
            f"{self.network}/{self.prefix_len}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocation_id": self.allocation_id,
            "subnet_id": self.subnet_id,
            "vlan_id": self.vlan_id,
            "purpose": self.purpose,
            "prefix_len": self.prefix_len,
            "network": self.network,
            "broadcast": self.broadcast,
            "netmask": self.netmask,
            "usable_start": self.usable_start,
            "usable_end": self.usable_end,
            "capacity": self.capacity,
            "requested_hosts": self.requested_hosts,
            "gateway": self.gateway,
            "dhcp": self.dhcp,
        }


@dataclass(frozen=True)
class IpAssignment:
    """One device interface placed on a subnet.

    `ip` is None for DHCP clients — unknown-until-observed by design.
    """

    device: str
    interface_hint: str | None
    subnet_id: str
    ip: str | None
    gateway: str | None
    dhcp: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "interface_hint": self.interface_hint,
            "subnet_id": self.subnet_id,
            "ip": self.ip,
            "gateway": self.gateway,
            "dhcp": self.dhcp,
        }


@dataclass(frozen=True)
class AddressPlan:
    """The complete, deterministic addressing outcome for a spec."""

    pool: str
    allocations: tuple[PlannedSubnet, ...]
    assignments: tuple[IpAssignment, ...]

    def allocation_by_subnet(self, subnet_id: str) -> PlannedSubnet | None:
        for allocation in self.allocations:
            if allocation.subnet_id == subnet_id:
                return allocation
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "allocations": [a.to_dict() for a in self.allocations],
            "assignments": [a.to_dict() for a in self.assignments],
        }


def _prefix_for_hosts(hosts: int) -> int:
    """Smallest prefix whose usable capacity covers `hosts` (gateway included)."""
    if hosts < 1:
        raise PlanningFailure("required_hosts", "must be >= 1", f"got {hosts}")
    for prefix in range(30, 7, -1):
        if (1 << (32 - prefix)) - 2 >= hosts:
            return prefix
    raise PlanningFailure("required_hosts", "no IPv4 prefix can fit this many hosts", str(hosts))


def _aligned_start(after: IPv4Address, block_size: int, pool_net: IPv4Network) -> IPv4Address:
    """First address >= `after` whose network-offset is aligned to block_size."""
    offset = int(after) - int(pool_net.network_address)
    aligned = ((offset + block_size - 1) // block_size) * block_size
    return IPv4Address(int(pool_net.network_address) + aligned)


def plan_addressing(spec: NetworkSpec, pool: str = DEFAULT_POOL) -> AddressPlan:
    """Turn a spec's subnets + ip_assignments into a deterministic AddressPlan."""
    try:
        pool_net = ip_network(pool, strict=False)
    except ValueError as exc:
        raise PlanningFailure("pool", "invalid pool CIDR", str(exc)) from exc
    if pool_net.version != 4:
        raise PlanningFailure("pool", "only IPv4 pools are supported in Phase 2", pool)

    # -- allocate subnets ---------------------------------------------------
    allocations: list[PlannedSubnet] = []
    cursor = pool_net.network_address  # first candidate offset (may be in use)
    for index, subnet in enumerate(spec.subnets):
        if subnet.cidr is not None:
            try:
                block = ip_network(subnet.cidr, strict=False)
            except ValueError as exc:
                raise PlanningFailure(subnet.id, "invalid cidr", str(exc)) from exc
            if block.version != 4:
                raise PlanningFailure(subnet.id, "only IPv4 subnets are supported", subnet.cidr)
            prefix = block.prefixlen
            network = block.network_address
            broadcast = block.broadcast_address
            capacity = block.num_addresses - 2
        else:
            assert subnet.required_hosts is not None  # spec model guarantees xor
            prefix = _prefix_for_hosts(subnet.required_hosts)
            block_size = 1 << (32 - prefix)
            start = _aligned_start(cursor, block_size, pool_net)
            network = start
            broadcast = IPv4Address(int(network) + block_size - 1)
            capacity = block_size - 2

        # Pool containment constrains PLANNED (auto-sized) allocations only:
        # a fixed-CIDR subnet states its own exact network and is honored as
        # declared, wherever it lives.
        if subnet.cidr is None and (
            int(network) < int(pool_net.network_address)
            or int(broadcast) > int(pool_net.broadcast_address)
        ):
            raise PlanningFailure(
                subnet.id,
                "subnet does not fit inside the address pool",
                f"{network}/{prefix} vs pool {pool}",
            )

        block_net = IPv4Network(f"{network}/{prefix}")
        for earlier in allocations:
            if block_net.overlaps(
                IPv4Network(f"{earlier.network}/{earlier.prefix_len}")
            ):
                raise PlanningFailure(
                    subnet.id,
                    "subnet overlaps an earlier allocation",
                    f"{block_net} overlaps {earlier.network}/{earlier.prefix_len}",
                )

        usable_start = IPv4Address(int(network) + 1)
        usable_end = IPv4Address(int(broadcast) - 1)
        if capacity < 1:
            raise PlanningFailure(subnet.id, "subnet has no usable host capacity", str(block))

        gateway = subnet.gateway or str(usable_start)
        allocations.append(
            PlannedSubnet(
                allocation_id=f"alloc-{index + 1:02d}",
                subnet_id=subnet.id,
                vlan_id=subnet.vlan_id,
                purpose=subnet.purpose,
                prefix_len=prefix,
                network=str(network),
                broadcast=str(broadcast),
                netmask=str(IPv4Network(f"0.0.0.0/{prefix}").netmask),
                usable_start=str(usable_start),
                usable_end=str(usable_end),
                capacity=capacity,
                requested_hosts=subnet.required_hosts,
                gateway=gateway,
                dhcp=subnet.dhcp,
            )
        )
        cursor = IPv4Address(int(broadcast) + 1)

    # -- place addresses ----------------------------------------------------
    next_free: dict[str, IPv4Address] = {}
    assignments: list[IpAssignment] = []
    for assignment in spec.ip_assignments:
        allocation = next((a for a in allocations if a.subnet_id == assignment.subnet), None)
        if allocation is None:  # spec model already rejects this; belt and braces
            raise PlanningFailure(assignment.device, "unknown subnet", assignment.subnet)

        if assignment.ip is not None and assignment.dhcp:
            raise PlanningFailure(
                assignment.device, "an assignment cannot be both static and DHCP", assignment.subnet
            )

        if assignment.ip is not None:
            try:
                address = IPv4Address(assignment.ip)
            except ValueError as exc:
                raise PlanningFailure(assignment.device, "invalid ip", str(exc)) from exc
            if address not in IPv4Network(f"{allocation.network}/{allocation.prefix_len}"):
                raise PlanningFailure(
                    assignment.device,
                    "ip outside its subnet",
                    f"{assignment.ip} vs {allocation.network}/{allocation.prefix_len}",
                )
            ip_str: str | None = str(address)
            dhcp_flag = False
        elif assignment.dhcp is True or (assignment.dhcp is None and allocation.dhcp):
            ip_str = None
            dhcp_flag = True
        else:
            # Auto-assign the next free usable host address, per subnet, in
            # spec order. The gateway holds usable_start, so host assignment
            # starts right AFTER it.
            if allocation.subnet_id not in next_free:
                next_free[allocation.subnet_id] = IPv4Address(
                    int(IPv4Address(allocation.usable_start)) + 1
                )
            candidate = next_free[allocation.subnet_id]
            if int(candidate) > int(IPv4Address(allocation.usable_end)):
                raise PlanningFailure(
                    assignment.device, "subnet exhausted by auto-assignment", allocation.subnet_id
                )
            next_free[allocation.subnet_id] = IPv4Address(int(candidate) + 1)
            ip_str = str(candidate)
            dhcp_flag = False

        assignments.append(
            IpAssignment(
                device=assignment.device,
                interface_hint=assignment.interface_hint,
                subnet_id=assignment.subnet,
                ip=ip_str,
                gateway=allocation.gateway if not dhcp_flag else None,
                dhcp=dhcp_flag,
            )
        )

    return AddressPlan(pool=pool, allocations=tuple(allocations), assignments=tuple(assignments))
