"""Intent lint: semantic checks over an already schema-valid intent.

The intent model describes shape; this module describes policy (the same
split as ``core/network_spec.py`` vs ``compiler/spec_lint.py``). Every check
returns a structured :class:`IntentFinding` so the caller — the AI layer, a
clarification loop, a test — decides what to do. A lint never mutates the
intent and never raises for a policy question.

Finding categories:

- ``missing_information`` — the user must answer before expansion (the
  expander would otherwise have to invent a fact the user never gave).
- ``contradiction`` — two stated requirements conflict.
- ``unsupported`` — the deterministic engine cannot build this today;
  expansion would refuse. Reported by name, never silently adjusted.
- ``warning`` — expandable, but the caller should know.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Literal

from .intent import (
    HavynIntent,
    HostGroupIntent,
    WIRELESS_DEVICES,
)

FindingKind = Literal[
    "missing_information", "contradiction", "unsupported", "warning"
]


@dataclass
class IntentFinding:
    """One lint result. ``ref`` names the intent element at fault."""

    kind: FindingKind
    code: str
    message: str
    ref: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"kind": self.kind, "code": self.code, "message": self.message, "ref": self.ref}


@dataclass
class IntentLintResult:
    findings: list[IntentFinding] = field(default_factory=list)

    def add(self, kind: FindingKind, code: str, message: str, ref: str | None = None) -> None:
        self.findings.append(IntentFinding(kind=kind, code=code, message=message, ref=ref))

    @property
    def missing_information(self) -> list[IntentFinding]:
        return [f for f in self.findings if f.kind == "missing_information"]

    @property
    def contradictions(self) -> list[IntentFinding]:
        return [f for f in self.findings if f.kind == "contradiction"]

    @property
    def unsupported(self) -> list[IntentFinding]:
        return [f for f in self.findings if f.kind == "unsupported"]

    @property
    def warnings(self) -> list[IntentFinding]:
        return [f for f in self.findings if f.kind == "warning"]

    @property
    def blocking(self) -> list[IntentFinding]:
        """Findings that must be resolved before expansion is meaningful."""
        return [
            f
            for f in self.findings
            if f.kind in ("missing_information", "contradiction", "unsupported")
        ]

    def to_dicts(self) -> list[dict[str, str | None]]:
        return [f.to_dict() for f in self.findings]


def lint_intent(intent: HavynIntent) -> IntentLintResult:
    """Run every semantic check. Deterministic: same intent, same findings."""
    result = IntentLintResult()
    _check_vlan_refs(result, intent)
    _check_vlan_sizing(result, intent)
    _check_host_group_sizes(result, intent)
    _check_wireless_cells(result, intent)
    _check_requirements(result, intent)
    _check_internet_firewall(result, intent)
    _check_servers(result, intent)
    return result


# --- checks ---------------------------------------------------------------


def _check_vlan_refs(result: IntentLintResult, intent: HavynIntent) -> None:
    vlan_ids = {v.id for v in intent.vlans}
    for _site, _area, group in intent.host_groups():
        if group.vlan is not None and group.vlan not in vlan_ids:
            result.add(
                "missing_information",
                "unknown-vlan-ref",
                f"host group {group.id!r} references VLAN {group.vlan!r}, which is "
                "not defined in the intent.",
                ref=group.id,
            )


def _check_vlan_sizing(result: IntentLintResult, intent: HavynIntent) -> None:
    """Fixed-CIDR VLANs must be large enough for the hosts that use them."""
    demand: dict[str, int] = {}
    for _site, _area, group in intent.host_groups():
        if group.vlan is None:
            continue
        demand[group.vlan] = demand.get(group.vlan, 0) + group.count
    for server in intent.servers:
        if server.vlan is not None:
            demand[server.vlan] = demand.get(server.vlan, 0) + 1
    for vlan in intent.vlans:
        if vlan.cidr is None:
            continue
        network = ipaddress.ip_network(vlan.cidr, strict=False)
        usable = max(network.num_addresses - 2, 0)
        need = demand.get(vlan.id, 0) + 1  # + gateway
        if need > usable:
            result.add(
                "contradiction",
                "vlan-too-small",
                f"VLAN {vlan.id!r} fixes {vlan.cidr} ({usable} usable addresses) "
                f"but {need} devices are placed in it. Either enlarge the CIDR "
                "or move devices to another VLAN.",
                ref=vlan.id,
            )


def _check_host_group_sizes(result: IntentLintResult, intent: HavynIntent) -> None:
    for _site, _area, group in intent.host_groups():
        if group.device.value in WIRELESS_DEVICES and group.count > 8:
            result.add(
                "unsupported",
                "wireless-group-too-large",
                f"host group {group.id!r} asks for {group.count} wireless "
                f"{group.device.value}s in one cell. Packet Tracer radio cells are "
                "verified up to 8 clients; split the group into smaller cells.",
                ref=group.id,
            )


def _check_wireless_cells(result: IntentLintResult, intent: HavynIntent) -> None:
    group_by_id = {g.id: g for _s, _a, g in intent.host_groups()}
    claimed: dict[str, str] = {}
    for cell in intent.wireless:
        if not cell.client_group_ids:
            result.add(
                "warning",
                "wireless-without-clients",
                f"wireless cell {cell.id!r} serves no client groups; the AP will "
                "exist with nothing associated to it.",
                ref=cell.id,
            )
        for group_id in cell.client_group_ids:
            group = group_by_id.get(group_id)
            if group is not None and group.device.value not in WIRELESS_DEVICES:
                result.add(
                    "contradiction",
                    "wired-device-on-wireless",
                    f"wireless cell {cell.id!r} claims host group {group_id!r}, "
                    f"but that group's device type ({group.device.value}) has no "
                    "wireless radio. Use laptop/smartphone/tablet groups.",
                    ref=cell.id,
                )
            if group_id in claimed:
                result.add(
                    "contradiction",
                    "client-in-two-cells",
                    f"host group {group_id!r} is claimed by wireless cells "
                    f"{claimed[group_id]!r} and {cell.id!r}; a client associates "
                    "to exactly one AP.",
                    ref=group_id,
                )
            claimed[group_id] = cell.id
    # Wireless clients nobody serves: the expander cannot guess an AP.
    for _site, _area, group in intent.host_groups():
        if group.device.value in WIRELESS_DEVICES and group.id not in claimed:
            result.add(
                "missing_information",
                "wireless-client-without-ap",
                f"host group {group.id!r} uses a wireless device type but no "
                "wireless cell claims it. Define a wireless cell or switch the "
                "group to wired devices.",
                ref=group.id,
            )


def _check_requirements(result: IntentLintResult, intent: HavynIntent) -> None:
    req_ids = {r.id for r in intent.requirements}
    for requirement in intent.requirements:
        for dep in requirement.dependencies:
            if dep not in req_ids:
                result.add(
                    "contradiction",
                    "unknown-requirement-dependency",
                    f"requirement {requirement.id!r} depends on {dep!r}, which is "
                    "not in the ledger.",
                    ref=requirement.id,
                )


def _check_internet_firewall(result: IntentLintResult, intent: HavynIntent) -> None:
    if intent.firewall is not None and intent.firewall.enabled:
        if intent.internet is None:
            result.add(
                "missing_information",
                "firewall-without-internet",
                "A firewall perimeter is requested but the intent defines no "
                "internet access; an ASA with nothing on its outside is almost "
                "never what the user means. Confirm, or remove the firewall.",
                ref="firewall",
            )
        if intent.firewall.model == "asa5505" and len(intent.sites) > 1:
            result.add(
                "unsupported",
                "asa5505-multi-site",
                "The ASA 5505 has two VLAN-capable ports and is verified only as a "
                "single inside/outside perimeter; a multi-site deployment needs "
                "the 5506-X.",
                ref="firewall",
            )
    guest_vlans = [v for v in intent.vlans if v.is_guest]
    if guest_vlans and not (
        intent.firewall is not None
        and intent.firewall.enabled
        and intent.firewall.guest_isolation
    ):
        result.add(
            "missing_information",
            "guest-vlan-without-isolation",
            f"Guest VLAN(s) {[v.id for v in guest_vlans]} are declared but "
            "firewall.guest_isolation is not enabled, so guests would be able to "
            "reach internal networks. Confirm the isolation policy or drop the "
            "guest flag.",
            ref="firewall",
        )


def _check_servers(result: IntentLintResult, intent: HavynIntent) -> None:
    vlan_ids = {v.id for v in intent.vlans}
    for server in intent.servers:
        if server.vlan is not None and server.vlan not in vlan_ids:
            result.add(
                "missing_information",
                "unknown-server-vlan-ref",
                f"server {server.id!r} references VLAN {server.vlan!r}, which is "
                "not defined in the intent.",
                ref=server.id,
            )
    if "dhcp" in [s for server in intent.servers for s in server.services]:
        result.add(
            "unsupported",
            "server-dhcp-service",
            "A Server-PT DHCP service is requested. The verified engine path for "
            "DHCP is per-VLAN pools on the gateway; server-side DHCP pools are "
            "not exposed through the intent yet.",
            ref="servers",
        )
