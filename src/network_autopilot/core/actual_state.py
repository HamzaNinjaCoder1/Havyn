"""ActualNetworkState — what Packet Tracer actually looks like, with explicit gaps.

Every field is an `Observable` (see core.observability). Merging observations
follows one rule: a KNOWN value is only replaced by a newer KNOWN value — an
UNKNOWN read never destroys a previous fact, it just records that the last
re-query failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .observability import Observable, ObservationStatus


@dataclass
class PortObservation:
    """Observed state of one interface."""

    name: str
    state: Observable[str] = field(default_factory=Observable.not_observed)
    ip: Observable[str] = field(default_factory=Observable.not_observed)
    mask: Observable[str] = field(default_factory=Observable.not_observed)
    gateway: Observable[str] = field(default_factory=Observable.not_observed)
    vlan: Observable[str] = field(default_factory=Observable.not_observed)
    trunk: Observable[bool] = field(default_factory=Observable.not_observed)
    description: Observable[str] = field(default_factory=Observable.not_observed)
    peers: Observable[list[str]] = field(default_factory=Observable.not_observed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.to_dict(),
            "ip": self.ip.to_dict(),
            "mask": self.mask.to_dict(),
            "gateway": self.gateway.to_dict(),
            "vlan": self.vlan.to_dict(),
            "trunk": self.trunk.to_dict(),
            "description": self.description.to_dict(),
            "peers": self.peers.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortObservation":
        return cls(
            name=data["name"],
            state=Observable.from_dict(data.get("state", {})),
            ip=Observable.from_dict(data.get("ip", {})),
            mask=Observable.from_dict(data.get("mask", {})),
            gateway=Observable.from_dict(data.get("gateway", {})),
            vlan=Observable.from_dict(data.get("vlan", {})),
            trunk=Observable.from_dict(data.get("trunk", {})),
            description=Observable.from_dict(data.get("description", {})),
            peers=Observable.from_dict(data.get("peers", {})),
        )


@dataclass
class DeviceObservation:
    """Observed state of one device.

    `name` is the observed Packet Tracer name (the key); `ref_id` is the
    Observable link back to the NetworkSpec device id — KNOWN only when we
    actually observed the correspondence.
    """

    name: str
    ref_id: Observable[str] = field(default_factory=Observable.not_observed)
    model: Observable[str] = field(default_factory=Observable.not_observed)
    role: Observable[str] = field(default_factory=Observable.not_observed)
    ports: dict[str, PortObservation] = field(default_factory=dict)
    # Control-plane read-back (Phase 3). Parsed structures carry their raw
    # console evidence; UNKNOWN/UNSUPPORTED is preserved, never collapsed.
    routes: Observable[list] = field(default_factory=Observable.not_observed)
    vlans: Observable[list] = field(default_factory=Observable.not_observed)
    notes: list[str] = field(default_factory=list)

    def port(self, name: str) -> PortObservation:
        """Get (creating an unobserved placeholder for) one port."""
        if name not in self.ports:
            self.ports[name] = PortObservation(name=name)
        return self.ports[name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ref_id": self.ref_id.to_dict(),
            "model": self.model.to_dict(),
            "role": self.role.to_dict(),
            "ports": {n: p.to_dict() for n, p in sorted(self.ports.items())},
            "routes": self.routes.to_dict(),
            "vlans": self.vlans.to_dict(),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceObservation":
        return cls(
            name=data["name"],
            ref_id=Observable.from_dict(data.get("ref_id", {})),
            model=Observable.from_dict(data.get("model", {})),
            role=Observable.from_dict(data.get("role", {})),
            ports={
                n: PortObservation.from_dict(p)
                for n, p in (data.get("ports") or {}).items()
            },
            routes=Observable.from_dict(data.get("routes", {})),
            vlans=Observable.from_dict(data.get("vlans", {})),
            notes=list(data.get("notes", [])),
        )


@dataclass
class LinkObservation:
    """Observed state of one link."""

    ref_id: Observable[str] = field(default_factory=Observable.not_observed)
    endpoint_a: Observable[str] = field(default_factory=Observable.not_observed)
    endpoint_b: Observable[str] = field(default_factory=Observable.not_observed)
    cable: Observable[str] = field(default_factory=Observable.not_observed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id.to_dict(),
            "endpoint_a": self.endpoint_a.to_dict(),
            "endpoint_b": self.endpoint_b.to_dict(),
            "cable": self.cable.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LinkObservation":
        return cls(
            ref_id=Observable.from_dict(data.get("ref_id", {})),
            endpoint_a=Observable.from_dict(data.get("endpoint_a", {})),
            endpoint_b=Observable.from_dict(data.get("endpoint_b", {})),
            cable=Observable.from_dict(data.get("cable", {})),
        )


@dataclass
class ActualNetworkState:
    """The normalized observed state of the Packet Tracer workspace."""

    source: str
    observed_at: str
    devices: dict[str, DeviceObservation] = field(default_factory=dict)
    links: list[LinkObservation] = field(default_factory=list)
    project_path: Observable[str] = field(default_factory=Observable.not_observed)
    simulation: Observable[str] = field(default_factory=Observable.not_observed)
    metadata: dict[str, Observable[str]] = field(default_factory=dict)

    def device(self, name: str) -> DeviceObservation | None:
        """A device absent from observed state is NOT the same as one with
        unknown properties — absence here means 'not seen in this observation'."""
        return self.devices.get(name)

    def observe_device(self, observation: DeviceObservation) -> DeviceObservation:
        self.devices[observation.name] = observation
        return observation

    def merge(self, other: "ActualNetworkState") -> "ActualNetworkState":
        """Fold another observation into this one. KNOWN wins only over
        non-KNOWN or older KNOWN; UNKNOWN never overwrites KNOWN."""
        for name, other_dev in other.devices.items():
            mine = self.devices.get(name)
            if mine is None:
                self.devices[name] = other_dev
                continue
            for attr in ("ref_id", "model", "role"):
                other_obs = getattr(other_dev, attr)
                mine_obs = getattr(mine, attr)
                if other_obs.is_known and (
                    not mine_obs.is_known
                    or (other_obs.observed_at or "") >= (mine_obs.observed_at or "")
                ):
                    setattr(mine, attr, other_obs)
            for port_name, other_port in other_dev.ports.items():
                my_port = mine.port(port_name)
                for attr in ("state", "ip", "mask", "gateway", "vlan", "trunk", "description", "peers"):
                    other_obs = getattr(other_port, attr)
                    mine_obs = getattr(my_port, attr)
                    if other_obs.is_known and (
                        not mine_obs.is_known
                        or (other_obs.observed_at or "") >= (mine_obs.observed_at or "")
                    ):
                        setattr(my_port, attr, other_obs)
        for other_link in other.links:
            self.links.append(other_link)
        for key, other_obs in other.metadata.items():
            mine_obs = self.metadata.get(key)
            if other_obs.is_known and (mine_obs is None or not mine_obs.is_known):
                self.metadata[key] = other_obs
        if other.project_path.is_known:
            self.project_path = other.project_path
        if other.simulation.is_known:
            self.simulation = other.simulation
        return self

    def unknown_summary(self) -> list[str]:
        """Human-readable paths of everything we do not KNOW.

        Used by reports so 'we never checked the VLANs' is visible instead of
        quietly rendering as 'no VLANs configured'.
        """
        out: list[str] = []
        for name, dev in sorted(self.devices.items()):
            for attr in ("ref_id", "model", "role"):
                obs = getattr(dev, attr)
                if not obs.is_known:
                    out.append(f"device({name}).{attr}: {obs.status.value}")
            for port_name, port in sorted(dev.ports.items()):
                for attr in ("state", "ip", "mask", "gateway", "vlan", "trunk", "description", "peers"):
                    obs = getattr(port, attr)
                    if not obs.is_known:
                        out.append(f"device({name}).port({port_name}).{attr}: {obs.status.value}")
        if not self.project_path.is_known:
            out.append(f"project_path: {self.project_path.status.value}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "observed_at": self.observed_at,
            "devices": {n: d.to_dict() for n, d in sorted(self.devices.items())},
            "links": [l.to_dict() for l in self.links],
            "project_path": self.project_path.to_dict(),
            "simulation": self.simulation.to_dict(),
            "metadata": {k: v.to_dict() for k, v in sorted(self.metadata.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActualNetworkState":
        status = ObservationStatus
        return cls(
            source=data.get("source", "unknown"),
            observed_at=data.get("observed_at", ""),
            devices={
                n: DeviceObservation.from_dict(d) for n, d in (data.get("devices") or {}).items()
            },
            links=[LinkObservation.from_dict(l) for l in data.get("links") or []],
            project_path=Observable.from_dict(data.get("project_path", {})),
            simulation=Observable.from_dict(data.get("simulation", {})),
            metadata={
                k: Observable.from_dict(v) for k, v in (data.get("metadata") or {}).items()
            },
        )
