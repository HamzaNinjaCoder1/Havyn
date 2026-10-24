"""Stage 4 fault-injection benchmark (S4.12).

For every library network, a deterministic simulated network is built by the
real compiler, one fault from a supported class is injected into its state and
configuration, and the REAL diagnosis engine (importer -> parser -> tracer ->
fault rules) is run against it. Predictions are compared with the injected
label; the result is a confusion matrix, per-class recall and an overall
root-cause accuracy.

Repairs are then planned and executed through the real `BrownfieldRepairEngine`
against the same simulated adapter. A repair that leaves the objective failing
without rolling back is counted as an incorrectly applied fix.

Nothing here is hand-written into the result: every number is derived from a
run. Live Packet Tracer evidence is a separate artifact and is never replaced
by this mode.
"""

from __future__ import annotations

import json
import ipaddress
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.network_spec import FlowTestSpec, NetworkSpec
from ..core.safety import MutationBudget, SafetyGate, SafetyMode
from .backup import BackupManager
from .faults import ExpectedNetwork, FaultDetector
from .importer import BrownfieldImporter, ImportOptions
from .intent import AsIsIntentBuilder
from .models import CAUSE_CLASSES, Diagnostic
from .objectives import ObjectiveRegistry
from .repair import BrownfieldRepairEngine
from .simulated import SimulatedAdapter, SimInterface

#: Only classes with a rule and an injector are scored. Unsupported classes
#: (for example a fault that PT exposes no evidence for) are reported
#: separately, never counted as a pass.
SUPPORTED_CLASSES = (
    "port-shutdown",
    "port-down-physical",
    "wrong-cable-endpoint",
    "missing-cable",
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
)

_LAYER_ORDER = {"L1": 0, "L2": 1, "L3": 2, "security": 3, "services": 4}
_CONFIDENCE_ORDER = {"high": 4, "medium": 3, "low": 2, "unknown": 1}
# Deeper causes outrank symptoms they explain (a changed router address
# explains a host's gateway complaint; the reverse is not true).
_CAUSE_ORDER = {name: index for index, name in enumerate(SUPPORTED_CLASSES)}


@dataclass
class FaultCase:
    library: str
    label: str
    description: str
    injected: Callable[[SimulatedAdapter], bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "library": self.library,
            "label": self.label,
            "description": self.description,
        }


@dataclass
class ScenarioResult:
    library: str
    label: str
    description: str
    injected: bool
    predicted: str | None = None
    detected_any: bool = False
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    repair_attempted: bool = False
    repaired: bool = False
    rolled_back: bool = False
    rollback_complete: bool = False
    incorrect_fix_left_in_place: bool = False
    repair_detail: str | None = None
    error: str | None = None

    @property
    def correct(self) -> bool:
        return self.injected and self.predicted == self.label

    def to_dict(self) -> dict[str, Any]:
        return {
            "library": self.library,
            "label": self.label,
            "description": self.description,
            "injected": self.injected,
            "predicted": self.predicted,
            "correct": self.correct,
            "detected_any": self.detected_any,
            "diagnostics": self.diagnostics,
            "repair_attempted": self.repair_attempted,
            "repaired": self.repaired,
            "rolled_back": self.rolled_back,
            "rollback_complete": self.rollback_complete,
            "incorrect_fix_left_in_place": self.incorrect_fix_left_in_place,
            "repair_detail": self.repair_detail,
            "error": self.error,
        }


@dataclass
class BenchmarkResult:
    mode: str
    scenarios: list[ScenarioResult] = field(default_factory=list)
    multi_fault_cases: list[dict[str, Any]] = field(default_factory=list)
    excluded_networks: list[dict[str, Any]] = field(default_factory=list)
    run_at: str = ""

    @property
    def scored(self) -> list[ScenarioResult]:
        return [s for s in self.scenarios if s.injected]

    @property
    def correct(self) -> int:
        return sum(1 for s in self.scored if s.correct)

    @property
    def overall_accuracy(self) -> float:
        return (
            self.correct / len(self.scored) if self.scored else 0.0
        )

    def per_class(self) -> dict[str, dict[str, float]]:
        totals: Counter[str] = Counter()
        correct: Counter[str] = Counter()
        detected: Counter[str] = Counter()
        for scenario in self.scored:
            totals[scenario.label] += 1
            if scenario.correct:
                correct[scenario.label] += 1
            if scenario.detected_any:
                detected[scenario.label] += 1
        return {
            label: {
                "cases": totals[label],
                "correct": correct[label],
                "recall": round(correct[label] / totals[label], 4),
                "detected_any_recall": round(detected[label] / totals[label], 4),
            }
            for label in sorted(totals)
        }

    def confusion_matrix(self) -> dict[str, dict[str, int]]:
        labels = sorted({s.label for s in self.scored})
        predictions = sorted({s.predicted or "unknown" for s in self.scored})
        matrix = {label: {pred: 0 for pred in predictions} for label in labels}
        for scenario in self.scored:
            matrix[scenario.label][scenario.predicted or "unknown"] += 1
        return matrix

    def summary(self) -> dict[str, Any]:
        repairs = [s for s in self.scenarios if s.repair_attempted]
        return {
            "mode": self.mode,
            "run_at": self.run_at,
            "scenarios": len(self.scored),
            "correct": self.correct,
            "overall_accuracy": round(self.overall_accuracy, 4),
            "per_class": self.per_class(),
            "incorrect_fixes_left_in_place": sum(
                1 for s in self.scenarios if s.incorrect_fix_left_in_place
            ),
            "repairs_attempted": len(repairs),
            "repairs_succeeded": sum(1 for s in repairs if s.repaired),
            "rollbacks": sum(1 for s in repairs if s.rolled_back),
            "multi_fault_cases": len(self.multi_fault_cases),
            "multi_faults_detected": sum(
                1 for case in self.multi_fault_cases if case.get("both_detected")
            ),
            "excluded_networks": len(self.excluded_networks),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "confusion_matrix": self.confusion_matrix(),
            "scenarios": [s.to_dict() for s in self.scenarios],
            "multi_fault_cases": list(self.multi_fault_cases),
            "excluded_networks": list(self.excluded_networks),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        summary = self.summary()
        lines = [
            "# Stage 4 fault-injection benchmark",
            "",
            f"_mode: {self.mode} · run {self.run_at}_",
            "",
            f"Scenarios **{summary['scenarios']}** · correct **{summary['correct']}** · "
            f"root-cause accuracy **{summary['overall_accuracy'] * 100:.1f}%** · "
            f"incorrect fixes left **{summary['incorrect_fixes_left_in_place']}**",
            "",
            "## Per class",
            "",
            "| fault class | cases | correct | recall | detected anywhere |",
            "| --- | --- | --- | --- | --- |",
        ]
        for label, row in summary["per_class"].items():
            lines.append(
                f"| `{label}` | {row['cases']} | {row['correct']} | "
                f"{row['recall'] * 100:.0f}% | {row['detected_any_recall'] * 100:.0f}% |"
            )
        lines.extend(
            [
                "",
                "## Confusion matrix",
                "",
            ]
        )
        matrix = self.confusion_matrix()
        predictions = sorted({p for row in matrix.values() for p in row})
        lines.append("| actual \\ predicted | " + " | ".join(predictions) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in predictions) + " |")
        for label in sorted(matrix):
            lines.append(
                f"| `{label}` | "
                + " | ".join(str(matrix[label].get(pred, 0)) for pred in predictions)
                + " |"
            )
        lines.extend(
            [
                "",
                "## Repairs",
                "",
                f"- attempted: {summary['repairs_attempted']}",
                f"- succeeded: {summary['repairs_succeeded']}",
                f"- rolled back: {summary['rollbacks']}",
                f"- incorrect fixes left in place: "
                f"{summary['incorrect_fixes_left_in_place']}",
                "",
                "## Multi-fault cases",
                "",
            ]
        )
        if not self.multi_fault_cases:
            lines.append("No simultaneous-fault case was injectable in this library set.")
        for case in self.multi_fault_cases:
            lines.append(
                f"- {case.get('library')}: injected {case.get('labels')} · "
                f"detected {case.get('detected')} · both detected: "
                f"{case.get('both_detected')}"
            )
        lines.extend(["", "## Excluded networks", ""])
        if not self.excluded_networks:
            lines.append("None: every library network had a healthy simulated baseline.")
        for entry in self.excluded_networks:
            lines.append(
                f"- {entry.get('library')}: {entry.get('reason')}"
            )
        return "\n".join(lines)


# -- injections ---------------------------------------------------------------


class _Faults:
    """Injectors over the simulated state. Each returns True when it applied."""

    def __init__(self, adapter: SimulatedAdapter, spec: NetworkSpec):
        self.adapter = adapter
        self.network = adapter.network
        self.spec = spec
        self.inventory = self.network.inventory()
        self.corpus = self.network.corpus(self.inventory)
        self.flow = next(
            (
                c.test for c in spec.acceptance_criteria
                if getattr(c.test, "kind", "") == "flow"
            ),
            None,
        )
        self._path: list[str] | None = None

    # -- target discovery ---------------------------------------------------

    def path(self) -> list[str]:
        if self._path is not None:
            return self._path
        if self.flow is None:
            self._path = []
            return self._path
        from .tracer import PathTracer

        trace = PathTracer(self.inventory, self.corpus).trace(
            self.flow.source, self.flow.destination
        )
        self._path = trace.path or []
        return self._path

    def network_ports(self) -> list[tuple[str, str]]:
        """The path's own ingress/egress ports on non-host devices."""
        path = self.path()
        out: list[tuple[str, str]] = []
        for index, device_name in enumerate(path):
            device = self.network.devices.get(device_name)
            if device is None or device.is_host:
                continue
            if index > 0:
                port = self._port_for(device_name, path[index - 1])
                if port:
                    out.append((device_name, port))
            if index + 1 < len(path):
                port = self._port_for(device_name, path[index + 1])
                if port:
                    out.append((device_name, port))
        return list(dict.fromkeys(out))

    def switch_access(self) -> list[tuple[str, str, int]]:
        out = []
        for device in self.network.devices.values():
            if device.category != "switch":
                continue
            for interface in device.interfaces.values():
                if interface.linked and interface.access_vlan is not None:
                    out.append((device.name, interface.name, interface.access_vlan))
        return out

    def switch_trunks(self) -> list[tuple[str, str]]:
        return [
            (device.name, interface.name)
            for device in self.network.devices.values()
            if device.category == "switch"
            for interface in device.interfaces.values()
            if interface.linked and interface.switchport_mode == "trunk"
        ]

    def static_routes(self) -> list[tuple[str, int]]:
        return [
            (device.name, index)
            for device in self.network.devices.values()
            for index, route in enumerate(device.static_routes)
            if route.get("next_hop")
        ]

    def dhcp_pools(self) -> list[tuple[str, str]]:
        return [
            (device.name, name)
            for device in self.network.devices.values()
            for name in device.dhcp_pools
        ]

    def hosts(self) -> list[str]:
        return [
            name for name, device in self.network.devices.items()
            if device.is_host and device.host is not None
        ]

    def asa_devices(self) -> list[str]:
        return [
            name for name, device in self.network.devices.items()
            if device.category == "firewall"
        ]

    # -- injectors ----------------------------------------------------------

    def port_shutdown(self) -> bool:
        ports = self.network_ports()
        if not ports:
            return False
        device, port = ports[0]
        self.network.devices[device].interfaces[port].shutdown = True
        return True

    def port_down_physical(self) -> bool:
        ports = self.network_ports()
        if not ports:
            return False
        device, port = ports[0]
        self.network.devices[device].interfaces[port].oper_up = False
        return True

    def missing_cable(self) -> bool:
        path = self.path()
        if len(path) < 2:
            return False
        link = next(
            (
                link for link in self.network.links
                if {link["a_device"], link["b_device"]} == {path[0], path[1]}
            ),
            None,
        )
        if link is None:
            return False
        self.adapter.delete_link(
            link["a_device"], link["a_port"], link["b_device"], link["b_port"]
        )
        return True

    def wrong_cable_endpoint(self) -> bool:
        path = self.path()
        if len(path) < 2:
            return False
        device_name = path[0]
        peer = path[1]
        other = next(
            (
                name for name in self.network.devices
                if name not in (device_name, peer)
                and not self.network.devices[name].is_host
            ),
            None,
        )
        if other is None:
            return False
        device = self.network.devices[device_name]
        port = next(
            (i.name for i in device.interfaces.values() if i.peers == [peer]),
            None,
        )
        if port is None:
            return False
        device.interfaces[port].peers = [other]
        return True

    def wrong_access_vlan(self) -> bool:
        ports = self.switch_access()
        if not ports:
            return False
        device, port, vlan = ports[0]
        wrong = 999 if vlan != 999 else 998
        item = self.network.devices[device]
        item.interfaces[port].access_vlan = wrong
        item.vlans.setdefault(wrong, "WRONG")
        return True

    def trunk_vlan_not_allowed(self) -> bool:
        trunks = self.switch_trunks()
        if not trunks:
            return False
        device, port = trunks[0]
        interface = self.network.devices[device].interfaces[port]
        if not interface.trunk_allowed:
            return False
        removed = interface.trunk_allowed[0]
        interface.trunk_allowed = [
            v for v in interface.trunk_allowed if v != removed
        ]
        return True

    def native_vlan_mismatch(self) -> bool:
        trunks = self.switch_trunks()
        if not trunks:
            return False
        device, port = trunks[0]
        interface = self.network.devices[device].interfaces[port]
        interface.trunk_native = 99
        peer = interface.peers[0] if interface.peers else None
        peer_port = interface.peer_ports[0] if interface.peer_ports else None
        peer_device = self.network.devices.get(peer or "")
        if peer_device is not None and peer_port in peer_device.interfaces:
            peer_device.interfaces[peer_port].trunk_native = 1
        return True

    def undefined_vlan(self) -> bool:
        ports = self.switch_access()
        if not ports:
            return False
        device, _port, vlan = ports[0]
        self.network.devices[device].vlans.pop(vlan, None)
        return True

    def stp_blocking(self) -> bool:
        ports = self.network_ports()
        switch = next(
            (name for name, _ in ports if self.network.devices[name].category == "switch"),
            None,
        )
        if switch is None:
            return False
        interface = next(
            (i for i in self.network.devices[switch].interfaces.values() if i.linked),
            None,
        )
        if interface is None:
            return False
        self.network.devices[switch].stp.append(
            {
                "vlan": interface.access_vlan or 1,
                "interface": interface.name,
                "role": "Altn",
                "state": "BLK",
            }
        )
        return True

    def wrong_ip(self) -> bool:
        # Prefer a static host or a router interface with a known address.
        for host_name in self.hosts():
            host = self.network.devices[host_name].host or {}
            if host.get("dhcp") or not host.get("ip"):
                continue
            try:
                network = ipaddress.IPv4Network(
                    f"{host['ip']}/{host['mask']}", strict=False
                )
                wrong = str(ipaddress.IPv4Address(host["ip"]) + 50)
                if ipaddress.IPv4Address(wrong) not in network:
                    continue
            except (ValueError, TypeError):
                continue
            host["ip"] = wrong
            interface = self.network.devices[host_name].interfaces.get(
                host.get("interface") or "FastEthernet0"
            )
            if interface is not None:
                interface.ip = wrong
            return True
        for device in self.network.devices.values():
            if device.category not in ("router", "firewall"):
                continue
            for interface in device.interfaces.values():
                if not (interface.ip and interface.mask):
                    continue
                try:
                    network = ipaddress.IPv4Network(
                        f"{interface.ip}/{interface.mask}", strict=False
                    )
                    wrong = str(ipaddress.IPv4Address(interface.ip) + 50)
                    if ipaddress.IPv4Address(wrong) not in network:
                        continue
                except ValueError:
                    continue
                interface.ip = wrong
                self.network._rebuild_routes()
                return True
        return False

    def wrong_mask(self) -> bool:
        for device in self.network.devices.values():
            if device.category not in ("router", "firewall"):
                continue
            for interface in device.interfaces.values():
                if not (interface.ip and interface.mask):
                    continue
                if interface.mask == "255.255.255.0":
                    interface.mask = "255.255.255.128"
                    self.network._rebuild_routes()
                    return True
        return False

    def wrong_gateway(self) -> bool:
        for host_name in self.hosts():
            host = self.network.devices[host_name].host or {}
            if not host.get("gateway"):
                continue
            try:
                network = ipaddress.IPv4Network(
                    f"{host['ip']}/{host['mask']}", strict=False
                )
                wrong = str(ipaddress.IPv4Address(host["gateway"]) + 50)
                if ipaddress.IPv4Address(wrong) not in network:
                    continue
            except (ValueError, TypeError):
                continue
            host["gateway"] = wrong
            return True
        return False

    def _route_in_use(self) -> tuple[str, int] | None:
        routes = self.static_routes()
        if not routes:
            return None
        destination_ip = self._flow_destination_ip()
        path = self.path()
        if destination_ip is not None:
            for device in path:
                item = self.network.devices.get(device)
                if item is None:
                    continue
                for index, route in enumerate(item.static_routes):
                    if not route.get("next_hop"):
                        continue
                    try:
                        network = ipaddress.IPv4Network(
                            f"{route['network']}/{route['mask']}", strict=False
                        )
                    except (KeyError, ValueError):
                        continue
                    if destination_ip in network:
                        return device, index
        return routes[0]

    def _flow_destination_ip(self):
        if self.flow is None:
            return None
        destination = self.network.devices.get(self.flow.destination)
        if destination is None or destination.host is None:
            return None
        value = destination.host.get("ip")
        try:
            return ipaddress.IPv4Address(value) if value else None
        except ValueError:
            return None

    def missing_route(self) -> bool:
        routes = self.static_routes()
        if not routes:
            return False
        chosen = self._route_in_use()
        if chosen is None:
            return False
        device, index = chosen
        self.network.devices[device].static_routes.pop(index)
        self.network._rebuild_routes()
        return True

    def wrong_next_hop(self) -> bool:
        routes = self.static_routes()
        if not routes:
            return False
        chosen = self._route_in_use()
        if chosen is None:
            return False
        device, index = chosen
        route = self.network.devices[device].static_routes[index]
        try:
            route["next_hop"] = str(ipaddress.IPv4Address(route["next_hop"]) + 1)
        except ValueError:
            return False
        self.network._rebuild_routes()
        return True

    def acl_block(self) -> bool:
        path = self.path()
        router = next(
            (
                name for name in path
                if self.network.devices.get(name)
                and self.network.devices[name].category
                in ("router", "firewall")
            ),
            None,
        )
        if router is None:
            return False
        device = self.network.devices[router]
        index = path.index(router)
        ingress_port = (
            self._port_for(router, path[index - 1]) if index > 0 else None
        )
        interface = (
            device.interfaces.get(ingress_port)
            if ingress_port is not None else None
        )
        if interface is None:
            interface = next((i for i in device.interfaces.values() if i.linked), None)
        if interface is None:
            return False
        device.acls.setdefault("BLOCK", []).append("deny ip any any")
        interface.acl_in = "BLOCK"
        return True

    def _port_for(self, device: str, peer: str) -> str | None:
        link = next(
            (
                link for link in self.network.links
                if {link["a_device"], link["b_device"]} == {device, peer}
            ),
            None,
        )
        if link is None:
            return None
        if link["a_device"] == device:
            return link["a_port"]
        return link["b_port"]

    def missing_asa_access_group(self) -> bool:
        for device_name in self.asa_devices():
            device = self.network.devices[device_name]
            interface = next(
                (
                    i for i in device.interfaces.values()
                    if i.ip and i.nameif
                ),
                None,
            )
            if interface is None:
                continue
            device.acls.setdefault("OUTSIDE-IN", []).append("extended deny ip any any")
            device.acl_bindings = [
                b for b in device.acl_bindings
                if b.get("name") != "OUTSIDE-IN"
            ]
            return True
        return False

    def wrong_asa_nameif(self) -> bool:
        for device_name in self.asa_devices():
            for interface in self.network.devices[device_name].interfaces.values():
                if interface.ip and interface.nameif:
                    interface.nameif = None
                    return True
        return False

    def wrong_security_level(self) -> bool:
        for device_name in self.asa_devices():
            for interface in self.network.devices[device_name].interfaces.values():
                if interface.ip and interface.security_level is not None:
                    interface.security_level = None
                    return True
        return False

    def dhcp_subnet_mismatch(self) -> bool:
        pools = self.dhcp_pools()
        if not pools:
            return False
        device, name = pools[0]
        pool = self.network.devices[device].dhcp_pools[name]
        if not pool.get("network"):
            return False
        try:
            network = ipaddress.IPv4Network(
                f"{pool['network']}/{pool['mask']}", strict=False
            )
        except ValueError:
            return False
        wrong = str(
            ipaddress.IPv4Address(
                int(network.network_address) + network.num_addresses + 10
            )
        )
        pool["default_router"] = [wrong]
        return True

    def dns_misconfiguration(self) -> bool:
        for host_name in self.hosts():
            host = self.network.devices[host_name].host or {}
            if host.get("dns"):
                host["dns"] = ["192.0.2.53"]
                return True
        return False


def _candidate_cases(spec: NetworkSpec) -> list[tuple[str, str, Callable[[_Faults], bool]]]:
    return [
        ("port-shutdown", "shut a path interface", lambda f: f.port_shutdown()),
        ("port-down-physical", "physical down on a path interface", lambda f: f.port_down_physical()),
        ("missing-cable", "remove a path cable", lambda f: f.missing_cable()),
        ("wrong-cable-endpoint", "path port cabled to the wrong peer", lambda f: f.wrong_cable_endpoint()),
        ("wrong-access-vlan", "access port in the wrong VLAN", lambda f: f.wrong_access_vlan()),
        ("trunk-vlan-not-allowed", "trunk missing a designed VLAN", lambda f: f.trunk_vlan_not_allowed()),
        ("native-vlan-mismatch", "native VLANs disagree", lambda f: f.native_vlan_mismatch()),
        ("undefined-vlan", "access port references an undefined VLAN", lambda f: f.undefined_vlan()),
        ("stp-blocking", "path port blocked by STP", lambda f: f.stp_blocking()),
        ("wrong-ip", "endpoint on a different host address", lambda f: f.wrong_ip()),
        ("wrong-mask", "interface with the wrong mask", lambda f: f.wrong_mask()),
        ("wrong-gateway", "host with the wrong default gateway", lambda f: f.wrong_gateway()),
        ("missing-route", "router missing a designed static route", lambda f: f.missing_route()),
        ("wrong-next-hop", "static route with the wrong next hop", lambda f: f.wrong_next_hop()),
        ("acl-block", "ACL denies the confirmed flow", lambda f: f.acl_block()),
        ("missing-asa-access-group", "ASA ACL defined but unbound", lambda f: f.missing_asa_access_group()),
        ("wrong-asa-nameif", "ASA addressed interface without nameif", lambda f: f.wrong_asa_nameif()),
        ("wrong-security-level", "ASA addressed interface without security-level", lambda f: f.wrong_security_level()),
        ("dhcp-subnet-mismatch", "DHCP pool default router outside its subnet", lambda f: f.dhcp_subnet_mismatch()),
        ("dns-misconfiguration", "host points at the wrong DNS server", lambda f: f.dns_misconfiguration()),
    ]


def _primary(diagnostics: list[Diagnostic]) -> Diagnostic | None:
    if not diagnostics:
        return None
    candidates = [d for d in diagnostics if d.cause_class != "unknown"] or diagnostics
    return sorted(
        candidates,
        key=lambda d: (
            _LAYER_ORDER.get(d.layer, 9),
            _CAUSE_ORDER.get(d.cause_class, 99),
            -_CONFIDENCE_ORDER.get(d.confidence, 0),
            0 if d.objective_id else 1,
        ),
    )[0]


def _dedupe(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    best: dict[tuple[Any, ...], Diagnostic] = {}
    for diagnostic in diagnostics:
        key = (
            diagnostic.cause_class,
            diagnostic.affected_device,
            diagnostic.affected_interface,
            diagnostic.vlan,
        )
        existing = best.get(key)
        if existing is None or (
            existing.objective_id is None and diagnostic.objective_id is not None
        ):
            best[key] = diagnostic
    return list(best.values())


class FaultInjectionBenchmark:
    def __init__(
        self,
        library_dir: Path | str,
        *,
        include_repair: bool = True,
        max_libraries: int | None = None,
        include_pending: bool = False,
    ):
        self.library_dir = Path(library_dir)
        self.include_repair = include_repair
        self.max_libraries = max_libraries
        self.include_pending = include_pending

    def run(self) -> BenchmarkResult:
        from datetime import datetime, timezone

        result = BenchmarkResult(
            mode="simulated",
            run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        manifest = json.loads(
            (self.library_dir / "manifest.json").read_text(encoding="utf-8")
        )
        specs = [
            entry for entry in manifest.get("specs", [])
            if entry.get("status") == "active"
            or (self.include_pending and entry.get("status") == "pending")
        ]
        if self.max_libraries is not None:
            specs = specs[: self.max_libraries]
        for entry in specs:
            path = self.library_dir / entry["file"]
            try:
                spec = NetworkSpec.from_json(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            healthy, reason = self._baseline_ok(spec)
            if not healthy:
                result.excluded_networks.append(
                    {"library": entry.get("id", path.stem), "reason": reason}
                )
                continue
            for label, description, injector in _candidate_cases(spec):
                scenario = self._run_one(spec, entry.get("id", path.stem), label, description, injector)
                if scenario is not None:
                    result.scenarios.append(scenario)
            multi = self._run_multi(spec, entry.get("id", path.stem))
            if multi is not None:
                result.multi_fault_cases.append(multi)
        return result

    # -- baseline -----------------------------------------------------------

    @staticmethod
    def _baseline_ok(spec: NetworkSpec) -> tuple[bool, str]:
        """A fault benchmark is meaningless on a network that starts broken.

        If the clean simulated network cannot satisfy its own flow criteria
        (an unmodelled wireless association, a cloud mapping the simulator
        does not carry), the network is excluded with the exact reason rather
        than scored as if every fault had caused the failure.
        """
        from .tracer import PathTracer

        try:
            adapter = SimulatedAdapter(spec)
            imported = BrownfieldImporter(
                adapter, options=ImportOptions(read_stp=True)
            ).import_network()
            tracer = PathTracer(imported.inventory, imported.corpus)
        except Exception as exc:
            return False, f"could not build the simulated baseline: {exc}"
        for criterion in spec.acceptance_criteria:
            test = criterion.test
            if not isinstance(test, FlowTestSpec):
                continue
            trace = tracer.trace(test.source, test.destination)
            expected = "blocked" if test.expect == "blocked" else "reachable"
            if expected == "reachable" and trace.status != "config_consistent":
                detail = (
                    trace.first_failing_check.detail
                    if trace.first_failing_check is not None else trace.status
                )
                return False, (
                    f"clean baseline fails {test.source}->{test.destination}: "
                    f"{detail}"
                )
            if expected == "blocked" and trace.status == "config_consistent":
                return False, (
                    f"clean baseline already allows {test.source}->"
                    f"{test.destination}, which the criterion expects blocked"
                )
        return True, ""

    # -- one scenario -------------------------------------------------------

    def _run_one(self, spec, library, label, description, injector) -> ScenarioResult | None:
        adapter = SimulatedAdapter(spec)
        clean = BrownfieldImporter(
            adapter, options=ImportOptions(read_stp=True)
        ).import_network()
        try:
            intent = AsIsIntentBuilder().build(clean)
            expected = ExpectedNetwork.from_spec(intent.spec)
        except Exception as exc:
            return ScenarioResult(
                library=library, label=label, description=description,
                injected=False, error=f"baseline intent failed: {exc}",
            )
        faults = _Faults(adapter, spec)
        try:
            injected = bool(injector(faults))
        except Exception as exc:
            return ScenarioResult(
                library=library, label=label, description=description,
                injected=False, error=f"injection raised: {type(exc).__name__}: {exc}",
            )
        if not injected:
            return None

        objectives = ObjectiveRegistry()
        objectives.add_explicit_from_spec(spec)
        imported = BrownfieldImporter(
            adapter, options=ImportOptions(read_stp=True)
        ).import_network()
        detector = FaultDetector(
            imported.inventory, imported.corpus, objectives, expected=expected
        )
        diagnostics: list[Diagnostic] = []
        for objective in objectives.confirmed():
            if objective.kind in ("reachability", "isolation") and objective.params.get(
                "source"
            ):
                diagnostics.extend(detector.diagnose(objective))
        diagnostics.extend(detector.detect_all())
        diagnostics = _dedupe(diagnostics)
        primary = _primary(diagnostics)
        scenario = ScenarioResult(
            library=library,
            label=label,
            description=description,
            injected=True,
            predicted=primary.cause_class if primary is not None else None,
            detected_any=any(d.cause_class == label for d in diagnostics),
            diagnostics=[d.to_dict() for d in diagnostics[:8]],
        )
        if self.include_repair and primary is not None:
            self._attempt_repair(
                spec, adapter, objectives, primary, diagnostics, scenario
            )
        return scenario

    def _attempt_repair(
        self,
        spec: NetworkSpec,
        adapter: SimulatedAdapter,
        objectives: ObjectiveRegistry,
        primary: Diagnostic,
        diagnostics: list[Diagnostic],
        scenario: ScenarioResult,
    ) -> None:
        if not primary.repairable or primary.objective_id is None:
            return
        objective = objectives.get(primary.objective_id)
        if objective is None or not objective.actionable:
            return
        try:
            with tempfile.TemporaryDirectory(prefix="havyn-bench-backup-") as directory:
                gate = SafetyGate(
                    SafetyMode.AUTONOMOUS,
                    MutationBudget(max_mutations=200, max_repair_attempts=5),
                )
                backup = BackupManager(adapter, root=Path(directory))
                engine = BrownfieldRepairEngine(
                    adapter, gate, objectives=objectives, backup=backup
                )
                plan = engine.plan([primary], objective=objective)
                scenario.repair_attempted = bool(plan.actions) and not plan.blocked
                if not scenario.repair_attempted:
                    scenario.repair_detail = plan.blocked_reason
                    return
                outcome = engine.execute(plan, objective=objective)
                scenario.repaired = outcome.repaired
                scenario.rolled_back = outcome.rolled_back
                scenario.rollback_complete = outcome.rollback_complete
                scenario.incorrect_fix_left_in_place = (
                    outcome.incorrect_fix_left_in_place
                )
                scenario.repair_detail = (
                    outcome.blocked_reason
                    or outcome.objective_detail
                    or outcome.rollback_detail
                )
        except Exception as exc:
            scenario.repair_detail = f"repair raised: {type(exc).__name__}: {exc}"

    # -- multiple simultaneous faults --------------------------------------

    def _run_multi(self, spec: NetworkSpec, library: str) -> dict[str, Any] | None:
        candidates = [
            ("port-shutdown", lambda f: f.port_shutdown()),
            ("wrong-access-vlan", lambda f: f.wrong_access_vlan()),
            ("missing-route", lambda f: f.missing_route()),
        ]
        adapter = SimulatedAdapter(spec)
        clean = BrownfieldImporter(adapter).import_network()
        try:
            expected = ExpectedNetwork.from_spec(
                AsIsIntentBuilder().build(clean).spec
            )
        except Exception:
            return None
        faults = _Faults(adapter, spec)
        injected: list[str] = []
        for label, injector in candidates:
            if len(injected) >= 2:
                break
            try:
                if injector(faults) and label not in injected:
                    injected.append(label)
            except Exception:
                continue
        if len(injected) < 2:
            return None
        objectives = ObjectiveRegistry()
        objectives.add_explicit_from_spec(spec)
        imported = BrownfieldImporter(adapter).import_network()
        detector = FaultDetector(
            imported.inventory, imported.corpus, objectives, expected=expected
        )
        diagnostics: list[Diagnostic] = []
        for objective in objectives.confirmed():
            if objective.kind in ("reachability", "isolation"):
                diagnostics.extend(detector.diagnose(objective))
        diagnostics.extend(detector.detect_all())
        detected = sorted({d.cause_class for d in _dedupe(diagnostics)})
        return {
            "library": library,
            "labels": injected,
            "detected": detected,
            "both_detected": all(label in detected for label in injected),
        }
