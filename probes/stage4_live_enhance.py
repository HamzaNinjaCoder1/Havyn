#!/usr/bin/env python
"""Stage 4 live enhancement proof (S4.11).

Builds a two-VLAN network, imports it as-is, asks for a third user PC on the
existing VLAN 10, lets the real planner/enhancer apply only the changed
operations behind a verified backup, and re-proves the original pc1 -> pc2
flow plus the new pc1 -> pc3 path.

    python probes/stage4_live_enhance.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from network_autopilot.brownfield.backup import BackupManager
from network_autopilot.brownfield.enhance import BrownfieldEnhancer, EnhancementPlanner
from network_autopilot.brownfield.importer import BrownfieldImporter, ImportOptions
from network_autopilot.brownfield.intent import AsIsIntentBuilder
from network_autopilot.core.network_spec import (
    AcceptanceCriterionSpec,
    FlowTestSpec,
    NetworkSpec,
)
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode

RAW = ROOT / "probes" / "raw"
LIVE = ROOT / "probes" / "live"
BACKUPS = ROOT / "probes" / "stage4-backups"

R = "s4e-r1"
S = "s4e-s1"
PC1 = "s4e-pc1"
PC2 = "s4e-pc2"
PC3 = "s4e-pc3"
NAMES = [R, S, PC1, PC2, PC3]

ROUTER_CONFIG = [
    "interface GigabitEthernet0/0", "no shutdown", "exit",
    "interface GigabitEthernet0/0.10", "encapsulation dot1Q 10",
    "ip address 10.0.10.1 255.255.255.0", "exit",
    "interface GigabitEthernet0/0.20", "encapsulation dot1Q 20",
    "ip address 10.0.20.1 255.255.255.0", "exit",
]
SWITCH_CONFIG = [
    "vlan 10", "name USERS", "exit", "vlan 20", "name GUESTS", "exit",
    "interface FastEthernet0/1", "switchport mode trunk",
    "switchport trunk allowed vlan 10,20", "exit",
    "interface FastEthernet1/1", "switchport mode access",
    "switchport access vlan 10", "exit",
    "interface FastEthernet2/1", "switchport mode access",
    "switchport access vlan 20", "exit",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _flow(adapter, source: str, destination: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = adapter.run_flow_test(source, destination)
        last = result
        if result.outcome.value == "reachable":
            return {"outcome": "reachable", "detail": result.detail, "passed": True}
        time.sleep(6)
    return {
        "outcome": last.outcome.value if last else "unknown",
        "detail": last.detail if last else "no result",
        "passed": False,
    }


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    evidence: dict = {"started": _now()}
    try:
        for name in NAMES:
            adapter.delete_device(name)
        time.sleep(3)
        for name, role in ((R, "router"), (S, "switch"), (PC1, "pc"), (PC2, "pc")):
            receipt = adapter.create_device(
                name, role, model_hint="2911" if name == R else None
            )
            if receipt.result.value != "success":
                raise RuntimeError(f"create {name}: {receipt.error}")
            time.sleep(2)
        for a, ap, b, bp in (
            (R, "GigabitEthernet0/0", S, "FastEthernet0/1"),
            (S, "FastEthernet1/1", PC1, "FastEthernet0"),
            (S, "FastEthernet2/1", PC2, "FastEthernet0"),
        ):
            receipt = adapter.create_link(a, ap, b, bp)
            if receipt.result.value != "success":
                raise RuntimeError(f"link {a}:{ap}: {receipt.error}")
            time.sleep(2)
        adapter.configure_ios(R, ROUTER_CONFIG)
        adapter.configure_ios(S, SWITCH_CONFIG)
        adapter.configure_host(
            PC1, dhcp=False, ip="10.0.10.10", mask="255.255.255.0",
            gateway="10.0.10.1", dns="8.8.8.8",
        )
        adapter.configure_host(
            PC2, dhcp=False, ip="10.0.20.10", mask="255.255.255.0",
            gateway="10.0.20.1", dns="8.8.8.8",
        )
        time.sleep(8)
        before = _flow(adapter, PC1, PC2)
        if not before["passed"]:
            raise RuntimeError(f"clean flow did not pass: {before}")

        imported = BrownfieldImporter(
            adapter,
            options=ImportOptions(read_positions=False, read_stp=True),
        ).import_network(device_names=[R, S, PC1, PC2])
        intent = AsIsIntentBuilder(
            acceptance=[
                AcceptanceCriterionSpec(
                    id="A-1",
                    title=f"{PC1} reaches {PC2}",
                    test=FlowTestSpec(
                        source=PC1, destination=PC2, expect="reachable"
                    ),
                )
            ]
        ).build(imported)
        current = NetworkSpec.model_validate(intent.spec.model_dump(mode="json"))
        raw = current.model_dump(mode="json")
        vlan10_subnet = next(
            s for s in raw["subnets"] if s.get("vlan_id") == 10
        )
        raw["name"] = "stage4-live-enhanced"
        raw["devices"].append({"id": PC3, "role": "pc"})
        raw["links"].append(
            {
                "id": f"l-{S}-{PC3}".lower(),
                "a": {
                    "device": S,
                    "interface_hint": "FastEthernet3/1",
                    "vlan": 10,
                },
                "b": {"device": PC3, "interface_hint": "FastEthernet0"},
            }
        )
        raw["ip_assignments"].append(
            {"device": PC3, "subnet": vlan10_subnet["id"], "ip": "10.0.10.11"}
        )
        desired = NetworkSpec.model_validate(raw)
        plan = EnhancementPlanner().plan(current, desired)
        evidence["plan"] = plan.preview()
        if plan.blocked:
            raise RuntimeError(plan.blocked_reason or "plan blocked")
        enhancer = BrownfieldEnhancer(
            adapter,
            SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=100)),
            backup=BackupManager(adapter, root=BACKUPS),
        )
        outcome = enhancer.apply(plan, approved=True)
        after_original = _flow(adapter, PC1, PC2)
        after_new = _flow(adapter, PC1, PC3)
        record = {
            "plan": plan.preview(),
            "outcome": outcome.to_dict(),
            "flow_original_after": after_original,
            "flow_new_after": after_new,
        }
        _write(RAW / "stage4-enhance.json", json.dumps(record, indent=2, sort_keys=True))
        evidence["finished"] = _now()
        _write(RAW / "stage4-live-enhance.json", json.dumps(evidence, indent=2, sort_keys=True))
        _write(
            LIVE / "stage4-enhance.md",
            "\n".join(
                [
                    "# Stage 4 live enhancement",
                    "",
                    f"- started: {evidence['started']}",
                    f"- finished: {evidence['finished']}",
                    f"- added: {PC3} on {S}:FastEthernet3/1 (VLAN 10, 10.0.10.11)",
                    f"- changed operations: {len(plan.operations)}",
                    f"- executed: {outcome.executed}",
                    f"- original criterion preserved: {outcome.preserved}",
                    f"- flow {PC1}->{PC2}: {after_original['outcome']}",
                    f"- flow {PC1}->{PC3}: {after_new['outcome']}",
                    f"- backup available: "
                    f"{outcome.backup_validation.available if outcome.backup_validation else False}",
                    "",
                    "Evidence: `probes/raw/stage4-enhance.json`, "
                    "`probes/raw/stage4-live-enhance.json`.",
                ]
            ) + "\n",
        )
        print(json.dumps(
            {
                "planned": len(plan.operations),
                "executed": outcome.executed,
                "preserved": outcome.preserved,
                "success": outcome.success,
                "flow_original_after": after_original,
                "flow_new_after": after_new,
            },
            indent=2,
        ))
        return 0 if outcome.executed and after_original["passed"] else 2
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["finished"] = _now()
        _write(RAW / "stage4-live-enhance-error.json", json.dumps(evidence, indent=2, sort_keys=True))
        print("LIVE ENHANCE ERROR:", evidence["error"])
        return 1
    finally:
        try:
            print("cleanup:", adapter.delete_devices_bulk(NAMES))
        except Exception:
            for name in NAMES:
                try:
                    adapter.delete_device(name)
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
