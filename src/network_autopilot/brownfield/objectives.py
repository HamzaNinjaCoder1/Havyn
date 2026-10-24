"""Objectives: explicit requirements vs inferred hypotheses (S4.5).

A network has two kinds of goals. The user states some ("PC5 must reach
Server1"); others are inferred from what the network obviously tries to do (a
host has a default gateway, so it presumably must reach it). The difference is
not cosmetic:

* an explicit objective is a requirement — diagnosis may act on it;
* an inferred objective is a hypothesis — diagnosis may *report* against it,
  but a repair based on it is refused until a person confirms it.

`ObjectiveRegistry` is the only place that distinction is decided, so a repair
cannot bypass it by inventing its own `confirmed=True`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

from ..core.network_spec import (
    ConfigTestSpec,
    FlowTestSpec,
    NetworkSpec,
    StructuralTestSpec,
    WirelessTestSpec,
)
from .models import BrownfieldInventory, Objective

_INFERRED_PREFIX = "inferred-"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ObjectiveRegistry:
    """Confirmed requirements and unconfirmed hypotheses, in one place."""

    def __init__(self, objectives: Iterable[Objective] = ()):
        self._objectives: dict[str, Objective] = {
            objective.id: objective for objective in objectives
        }
        self._seq = len(self._objectives)

    # -- access -------------------------------------------------------------

    def all(self) -> list[Objective]:
        return sorted(self._objectives.values(), key=lambda o: o.id)

    def get(self, objective_id: str | None) -> Objective | None:
        if objective_id is None:
            return None
        return self._objectives.get(objective_id)

    def confirmed(self) -> list[Objective]:
        return [o for o in self.all() if o.actionable]

    def inferred_pending(self) -> list[Objective]:
        return [o for o in self.all() if not o.actionable]

    def covering(self, kind: str, **params: Any) -> Objective | None:
        """An actionable objective that covers a proposed diagnosis."""
        for objective in self.all():
            if not objective.actionable or objective.kind != kind:
                continue
            if all(objective.params.get(k) == v for k, v in params.items()):
                return objective
        return None

    # -- mutation -----------------------------------------------------------

    def add(self, objective: Objective) -> Objective:
        self._objectives[objective.id] = objective
        return objective

    def confirm(self, objective_id: str, when: str | None = None) -> Objective:
        objective = self._objectives.get(objective_id)
        if objective is None:
            raise KeyError(f"no objective {objective_id!r}")
        if objective.source == "explicit":
            return objective
        confirmed = objective.with_confirmation(when or _now())
        self._objectives[objective_id] = confirmed
        return confirmed

    def confirm_all(self, when: str | None = None) -> list[Objective]:
        for objective_id in [o.id for o in self.inferred_pending()]:
            self.confirm(objective_id, when)
        return self.all()

    def reject_inferred(self) -> None:
        """Drop unconfirmed hypotheses (a user said they are not goals)."""
        for objective_id in [o.id for o in self.inferred_pending()]:
            del self._objectives[objective_id]

    # -- sources ------------------------------------------------------------

    def add_explicit_from_spec(self, spec: NetworkSpec) -> list[Objective]:
        """One explicit objective per acceptance criterion in the user's spec."""
        out: list[Objective] = []
        for criterion in spec.acceptance_criteria:
            test = criterion.test
            statement = criterion.title or ""
            if isinstance(test, FlowTestSpec):
                kind = "isolation" if test.expect == "blocked" else "reachability"
                params = {
                    "source": test.source,
                    "destination": test.destination,
                    "expect": test.expect,
                    "transport": test.transport,
                    "port": test.port,
                    "criterion_id": criterion.id,
                }
                statement = statement or (
                    f"{test.source} must be {'blocked from' if test.expect == 'blocked' else 'able to reach'} "
                    f"{test.destination}"
                )
            elif isinstance(test, ConfigTestSpec):
                kind = "addressing"
                params = {
                    "device": test.device,
                    "interface": test.interface,
                    "field": test.field,
                    "expected": test.expected,
                    "criterion_id": criterion.id,
                }
                statement = statement or (
                    f"{test.device} {test.interface or ''} {test.field} must be "
                    f"{test.expected}"
                )
            elif isinstance(test, StructuralTestSpec):
                kind = "service" if test.check in ("vlan_defined", "trunk_configured") else "addressing"
                params = {
                    "check": test.check,
                    "target": test.target,
                    "criterion_id": criterion.id,
                }
                statement = statement or f"{test.check} {test.target} must hold"
            elif isinstance(test, WirelessTestSpec):
                kind = "service"
                params = {
                    "client": test.client,
                    "access_point": test.access_point,
                    "criterion_id": criterion.id,
                }
                statement = statement or f"{test.client} must associate with {test.access_point}"
            else:  # pragma: no cover - discriminated union
                continue
            objective = Objective(
                id=f"OBJ-{criterion.id}",
                statement=statement,
                kind=kind,
                source="explicit",
                confirmed=True,
                params=params,
                evidence=(f"acceptance criterion {criterion.id}",),
                confirmed_at=_now(),
            )
            self._objectives[objective.id] = objective
            out.append(objective)
        # An explicit requirement with no criterion is still a requirement;
        # keep it visible even though it cannot be probed yet.
        for requirement in spec.requirements:
            objective = Objective(
                id=f"OBJ-{requirement.id}",
                statement=requirement.statement,
                kind=requirement.kind if requirement.kind != "connectivity" else "reachability",
                source="explicit",
                confirmed=True,
                params={"requirement_id": requirement.id},
                evidence=(f"requirement {requirement.id}",),
                confirmed_at=_now(),
            )
            self._objectives.setdefault(objective.id, objective)
        return out

    def infer_from_inventory(self, inventory: BrownfieldInventory) -> list[Objective]:
        """Hypotheses a working network almost certainly has, never confirmed."""
        out: list[Objective] = []
        seq = 0

        def add(statement: str, kind: str, params: dict[str, Any], evidence: str) -> None:
            nonlocal seq
            seq += 1
            objective = Objective(
                id=f"{_INFERRED_PREFIX}{seq}",
                statement=statement,
                kind=kind,
                source="inferred",
                confirmed=False,
                params={**params, "requires_confirmation": True},
                evidence=(evidence,),
            )
            # Do not duplicate an explicit objective with the same shape.
            for existing in self._objectives.values():
                if existing.kind != kind:
                    continue
                if all(existing.params.get(k) == v for k, v in params.items()):
                    return
            self._objectives[objective.id] = objective
            out.append(objective)

        for device in inventory.devices.values():
            if device.host is not None:
                gateway = (
                    str(device.host.gateway.value)
                    if device.host.gateway.is_known
                    else None
                )
                lease = device.host.dhcp.is_known and bool(device.host.dhcp.value)
                if gateway:
                    add(
                        f"{device.name} must reach its default gateway {gateway}",
                        "reachability",
                        {"source": device.name, "destination_ip": gateway},
                        "host ipconfig /all reports a default gateway",
                    )
                if lease:
                    add(
                        f"{device.name} must receive a DHCP lease",
                        "addressing",
                        {"device": device.name, "dhcp": True},
                        "host ipconfig /all reports a DHCP server",
                    )
                if device.host.dns.is_known and device.host.dns.value:
                    add(
                        f"{device.name} must resolve names through its DNS server",
                        "dns",
                        {
                            "device": device.name,
                            "dns": list(device.host.dns.value or []),
                        },
                        "host ipconfig /all reports a DNS server",
                    )
        for device in inventory.devices.values():
            if device.is_switch and device.vlans_table.is_known:
                for vlan in device.vlans_table.value or []:
                    if not isinstance(vlan, dict):
                        continue
                    members = [
                        p for p in vlan.get("ports") or [] if str(p).strip()
                    ]
                    if not members:
                        continue
                    add(
                        f"VLAN {vlan.get('id')} on {device.name} must reach its gateway",
                        "reachability",
                        {
                            "vlan": int(vlan.get("id") or 0),
                            "switch": device.name,
                        },
                        "VLAN has active member ports",
                    )
        return out

    # -- user statements ----------------------------------------------------

    _REACH = re.compile(
        r"^\s*(\S+)\s+must\s+(?:be able to\s+)?(?:communicate with|reach|talk to)\s+(\S+)\s*$",
        re.IGNORECASE,
    )
    _ISOLATED = re.compile(
        r"^\s*(\S+)\s+must\s+(?:remain\s+)?(?:be\s+)?isolated from\s+(\S+)\s*$",
        re.IGNORECASE,
    )
    _VLAN_GATEWAY = re.compile(
        r"^\s*VLAN\s+(\d+)\s+must\s+reach\s+its\s+gateway\s*$", re.IGNORECASE
    )
    _VLAN_ISOLATED = re.compile(
        r"^\s*VLAN\s+(\d+)\s+must\s+remain\s+isolated\s+from\s+VLAN\s+(\d+)\s*$",
        re.IGNORECASE,
    )

    def add_user_requirement(self, statement: str) -> Objective:
        """Turn one explicit user sentence into a confirmed objective.

        Only shapes the engine can actually probe become objectives; anything
        else raises, rather than being stored as a requirement nobody can
        check (which would make an unverifiable claim look covered).
        """
        text = statement.strip().rstrip(".")
        match = self._VLAN_ISOLATED.match(text)
        if match:
            params = {
                "vlan_a": int(match.group(1)),
                "vlan_b": int(match.group(2)),
                "expect": "blocked",
            }
            kind = "isolation"
        else:
            match = self._VLAN_GATEWAY.match(text)
            if match:
                params = {"vlan": int(match.group(1)), "expect": "reachable"}
                kind = "reachability"
            else:
                match = self._ISOLATED.match(text)
                if match:
                    params = {
                        "source": match.group(1),
                        "destination": match.group(2),
                        "expect": "blocked",
                    }
                    kind = "isolation"
                else:
                    match = self._REACH.match(text)
                    if match:
                        params = {
                            "source": match.group(1),
                            "destination": match.group(2),
                            "expect": "reachable",
                        }
                        kind = "reachability"
                    elif re.search(
                        r"dhcp clients must (?:receive|get) (?:correct )?addresses",
                        text,
                        re.IGNORECASE,
                    ):
                        params = {"dhcp": True}
                        kind = "addressing"
                    elif re.search(
                        r"dns resolution must work", text, re.IGNORECASE
                    ):
                        params = {"dns": True}
                        kind = "dns"
                    else:
                        raise ValueError(
                            f"cannot express {statement!r} as a probeable "
                            "objective; state a source/destination pair, a VLAN "
                            "gateway, an isolation pair, DHCP addressing or DNS "
                            "resolution"
                        )
        objective = Objective(
            id=f"OBJ-USER-{len(self._objectives) + 1}",
            statement=text,
            kind=kind,
            source="explicit",
            confirmed=True,
            params=params,
            evidence=("stated by the user",),
            confirmed_at=_now(),
        )
        self._objectives[objective.id] = objective
        return objective

    # -- enforcement --------------------------------------------------------

    def actionable_for(self, diagnostic) -> Objective | None:
        """The objective a diagnosis may repair against, or None.

        A diagnosis with no objective is not actionable: repairing an
        unasked-for change is exactly the failure mode the confirmation gate
        exists to prevent.
        """
        objective = self.get(diagnostic.objective_id)
        if objective is None:
            return None
        return objective if objective.actionable else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "objectives": [o.to_dict() for o in self.all()],
            "confirmed": len(self.confirmed()),
            "pending_confirmation": len(self.inferred_pending()),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ObjectiveRegistry":
        registry = cls()
        for raw in data.get("objectives") or []:
            registry.add(
                Objective(
                    id=str(raw.get("id")),
                    statement=str(raw.get("statement")),
                    kind=str(raw.get("kind")),
                    source=str(raw.get("source")),
                    confirmed=bool(raw.get("confirmed")),
                    params=dict(raw.get("params") or {}),
                    evidence=tuple(raw.get("evidence") or ()),
                    confirmed_at=raw.get("confirmed_at"),
                )
            )
        return registry
