"""Enhancement benchmark: add a host to real library networks (S4.11).

Each case starts from a library spec, derives a desired spec that adds one
host, one cable and one static assignment on an existing subnet, runs the real
planner/enhancer against the deterministic simulated network, and records
whether the enhancement applied and every previously passing criterion still
passes.

The additions are genuinely new resources in an existing network — not a
rebuild — so the "only necessary changes" claim is measured by the planner's
changed-operation count.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.network_spec import NetworkSpec, default_model_for_role
from ..core.safety import MutationBudget, SafetyGate, SafetyMode
from ..pt.catalog import resolve_model
from .backup import BackupManager
from .enhance import BrownfieldEnhancer, EnhancementPlanner
from .simulated import SimulatedAdapter, SimulatedNetwork


@dataclass
class EnhancementCase:
    library: str
    added_host: str | None = None
    added_to: str | None = None
    added_port: str | None = None
    added_ip: str | None = None
    planned: bool = False
    executed: bool = False
    preserved: bool = False
    desired_satisfied: bool = False
    success: bool = False
    changes: int = 0
    operations: int = 0
    conflicts: list[str] = field(default_factory=list)
    original_before: dict[str, str] = field(default_factory=dict)
    original_after: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "library": self.library,
            "added_host": self.added_host,
            "added_to": self.added_to,
            "added_port": self.added_port,
            "added_ip": self.added_ip,
            "planned": self.planned,
            "executed": self.executed,
            "preserved": self.preserved,
            "desired_satisfied": self.desired_satisfied,
            "success": self.success,
            "changes": self.changes,
            "operations": self.operations,
            "conflicts": list(self.conflicts),
            "original_before": dict(self.original_before),
            "original_after": dict(self.original_after),
            "problems": list(self.problems),
            "error": self.error,
        }


@dataclass
class EnhancementBenchmarkResult:
    cases: list[EnhancementCase] = field(default_factory=list)
    mode: str = "simulated"

    @property
    def enhanced(self) -> int:
        return sum(1 for case in self.cases if case.success)

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "cases": len(self.cases),
            "enhanced": self.enhanced,
            "preserved": sum(1 for case in self.cases if case.preserved),
            "all_original_criteria_preserved": all(
                case.preserved for case in self.cases if case.executed
            ) if any(case.executed for case in self.cases) else False,
            "operations": sum(case.operations for case in self.cases),
            "details": [case.to_dict() for case in self.cases],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary()}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def _add_host(raw: dict[str, Any], count: int) -> dict[str, Any] | None:
    """Add one PC + cable + address to the first switch with a free port."""
    devices = {d["id"]: d for d in raw["devices"]}
    specs = {d["id"]: d for d in raw["devices"]}
    # Simulated network tells us which ports the compiler actually used.
    spec = NetworkSpec.model_validate(raw)
    network = SimulatedNetwork(spec)
    used: dict[str, set[str]] = {name: set() for name in devices}
    for link in network.links:
        used[link["a_device"]].add(link["a_port"])
        used[link["b_device"]].add(link["b_port"])
    switch = next(
        (
            name for name, device in network.devices.items()
            if device.category == "switch"
        ),
        None,
    )
    if switch is None:
        return None
    model = resolve_model(network.devices[switch].model)
    if model is None:
        return None
    free_port = next(
        (
            port.full_name for port in model.ports
            if port.full_name not in used[switch]
        ),
        None,
    )
    if free_port is None:
        return None
    subnet = next(
        (
            s for s in raw["subnets"]
            if s.get("cidr") and s.get("vlan_id") is not None
        ),
        raw["subnets"][0] if raw.get("subnets") else None,
    )
    if subnet is None or not subnet.get("cidr"):
        return None
    try:
        block = ipaddress.IPv4Network(subnet["cidr"], strict=False)
    except ValueError:
        return None
    used_ips = {
        a.get("ip") for a in raw.get("ip_assignments", []) if a.get("ip")
    }
    gateway = subnet.get("gateway")
    candidate = None
    for offset in range(2, min(block.num_addresses - 1, 200)):
        value = str(block.network_address + offset)
        if value in used_ips or value == gateway:
            continue
        candidate = value
        break
    if candidate is None:
        return None
    host = f"pc-enh{count}"
    raw["devices"].append({"id": host, "role": "pc"})
    raw["links"].append(
        {
            "id": f"l-{switch}-{host}".lower(),
            "a": {"device": switch, "interface_hint": free_port},
            "b": {"device": host},
        }
    )
    raw["ip_assignments"].append(
        {
            "device": host,
            "subnet": subnet["id"],
            "ip": candidate,
        }
    )
    return {
        "host": host,
        "switch": switch,
        "port": free_port,
        "ip": candidate,
    }


def run_enhancement_benchmark(
    library_dir: Path | str,
    *,
    target: int = 5,
    max_networks: int = 8,
) -> EnhancementBenchmarkResult:
    import tempfile

    library_dir = Path(library_dir)
    manifest = json.loads(
        (library_dir / "manifest.json").read_text(encoding="utf-8")
    )
    result = EnhancementBenchmarkResult()
    for entry in manifest.get("specs", []):
        if entry.get("status") != "active":
            continue
        if len(result.cases) >= max_networks:
            break
        path = library_dir / entry["file"]
        try:
            current = NetworkSpec.from_json(path.read_text(encoding="utf-8"))
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        addition = _add_host(raw, len(result.cases) + 1)
        if addition is None:
            continue
        case = EnhancementCase(
            library=entry.get("id", path.stem),
            added_host=addition["host"],
            added_to=addition["switch"],
            added_port=addition["port"],
            added_ip=addition["ip"],
        )
        try:
            desired = NetworkSpec.model_validate(raw)
        except Exception as exc:
            case.error = f"desired spec did not validate: {exc}"
            result.cases.append(case)
            continue
        try:
            plan = EnhancementPlanner().plan(current, desired)
            case.planned = not plan.blocked
            case.changes = len(plan.changes)
            case.operations = len(plan.operations)
            case.conflicts = [c.message for c in plan.conflicts]
            if plan.blocked:
                case.error = plan.blocked_reason or "plan blocked by conflicts"
                result.cases.append(case)
                continue
            adapter = SimulatedAdapter(current)
            with tempfile.TemporaryDirectory(prefix="havyn-enhance-") as directory:
                enhancer = BrownfieldEnhancer(
                    adapter,
                    SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=300)),
                    backup=BackupManager(adapter, root=Path(directory)),
                )
                outcome = enhancer.apply(plan, approved=True)
            case.executed = outcome.executed
            case.preserved = outcome.preserved
            case.desired_satisfied = outcome.desired_satisfied
            case.success = outcome.success
            case.original_before = outcome.original_before
            case.original_after = outcome.original_after
            case.problems = outcome.problems
            if outcome.blocked_reason:
                case.error = outcome.blocked_reason
        except Exception as exc:
            case.error = f"{type(exc).__name__}: {exc}"
        result.cases.append(case)
        if sum(1 for c in result.cases if c.success) >= target and len(result.cases) >= target:
            # Keep going for the report, but do not scan the whole library.
            if len(result.cases) >= max_networks:
                break
    return result
