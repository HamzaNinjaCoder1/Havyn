#!/usr/bin/env python
"""Live rollback proof (S4.10). A deliberately wrong repair is applied through
the real repair engine; the objective stays broken, the engine rolls back,
and the device is read back against the pre-repair backup."""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from network_autopilot.brownfield.backup import BackupManager
from network_autopilot.brownfield.faults import ExpectedNetwork, FaultDetector
from network_autopilot.brownfield.importer import BrownfieldImporter, ImportOptions
from network_autopilot.brownfield.intent import AsIsIntentBuilder
from network_autopilot.brownfield.models import FixAction
from network_autopilot.brownfield.objectives import ObjectiveRegistry
from network_autopilot.brownfield.repair import BrownfieldRepairEngine, RepairPlan
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode

S, PC1, PC2 = "s4r-sw1", "s4r-pc1", "s4r-pc2"
NAMES = [S, PC1, PC2]


def flow(adapter, timeout=90):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = adapter.run_flow_test(PC1, PC2)
        if last.outcome.value in ("reachable", "blocked", "partial"):
            return {"outcome": last.outcome.value, "detail": last.detail}
        time.sleep(6)
    return {"outcome": last.outcome.value if last else "unknown",
            "detail": last.detail if last else "no result"}


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    try:
        for n in NAMES:
            adapter.delete_device(n)
        time.sleep(2)
        for n, r in ((S, "switch"), (PC1, "pc"), (PC2, "pc")):
            receipt = adapter.create_device(n, r)
            print("create", n, receipt.result.value)
            if receipt.result.value != "success":
                return 2
            time.sleep(2)
        for a, ap, b, bp in ((S, "FastEthernet1/1", PC1, "FastEthernet0"),
                             (S, "FastEthernet2/1", PC2, "FastEthernet0")):
            receipt = adapter.create_link(a, ap, b, bp)
            print("link", ap, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            time.sleep(2)
        adapter.configure_ios(S, [
            "vlan 10", "name USERS", "exit",
            "interface FastEthernet1/1", "switchport mode access",
            "switchport access vlan 10", "exit",
            "interface FastEthernet2/1", "switchport mode access",
            "switchport access vlan 10", "exit",
        ])
        adapter.configure_host(PC1, dhcp=False, ip="10.241.0.10",
                               mask="255.255.255.0", gateway=None)
        adapter.configure_host(PC2, dhcp=False, ip="10.241.0.11",
                               mask="255.255.255.0", gateway=None)
        time.sleep(8)
        clean_flow = flow(adapter)
        if clean_flow["outcome"] != "reachable":
            raise RuntimeError(f"clean flow not reachable: {clean_flow}")
        clean = BrownfieldImporter(adapter, options=ImportOptions(read_stp=True)).import_network(device_names=NAMES)
        expected = ExpectedNetwork.from_spec(AsIsIntentBuilder().build(clean).spec)
        objectives = ObjectiveRegistry()
        objective = objectives.add_user_requirement(f"{PC1} must communicate with {PC2}")
        # Inject a real shutdown on pc2's switch port.
        fault = adapter.configure_ios(S, ["interface FastEthernet2/1", "shutdown", "exit"])
        time.sleep(6)
        fault_flow = flow(adapter)
        if fault_flow["outcome"] not in ("blocked", "partial"):
            raise RuntimeError(f"fault did not block: {fault_flow}")
        faulted = BrownfieldImporter(adapter, options=ImportOptions(read_stp=True)).import_network(device_names=NAMES)
        detector = FaultDetector(faulted.inventory, faulted.corpus, objectives, expected=expected)
        diagnostics = detector.diagnose(objective)
        diag = next(d for d in diagnostics if d.cause_class == "port-shutdown")
        wrong = FixAction(
            device=S,
            commands=("interface FastEthernet2/1", "switchport access vlan 20", "exit"),
            reason="deliberately wrong repair for live rollback proof",
            rollback_commands=("interface FastEthernet2/1", "switchport access vlan 10", "exit"),
        )
        plan = RepairPlan(
            repair_id="live-wrong-repair",
            diagnosis_ids=(diag.diagnosis_id,),
            objective_ids=(objective.id,),
            actions=(wrong,),
            affected_devices=(S,),
            expected_improvement="none (deliberately wrong)",
            verification=(objective.statement,),
        )
        gate = SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget(max_mutations=50))
        engine = BrownfieldRepairEngine(
            adapter, gate, objectives=objectives,
            backup=BackupManager(adapter, root=ROOT / "probes" / "stage4-backups"),
        )
        outcome = engine.execute(plan, objective=objective, approved=True)
        after_flow = flow(adapter)
        config = adapter.read_running_config(S)
        raw = str(config.value) if config.is_known else ""
        record = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "clean_flow": clean_flow,
            "fault_push": fault.result.value,
            "fault_flow": fault_flow,
            "diagnosis": diag.to_dict(),
            "repair": outcome.to_dict(),
            "flow_after_rollback": after_flow,
            "config_read_known": bool(config.is_known),
            "config_has_shutdown": "interface FastEthernet2/1\n shutdown" in raw
            or "shutdown" in raw.split("interface FastEthernet2/1")[-1].split("interface")[0],
            "config_has_vlan10": "switchport access vlan 10" in raw,
        }
        (ROOT / "probes" / "raw" / "stage4-live-rollback.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        print(json.dumps({
            "executed": outcome.executed,
            "rolled_back": outcome.rolled_back,
            "rollback_complete": outcome.rollback_complete,
            "incorrect_fix_left": outcome.incorrect_fix_left_in_place,
            "flow_after": after_flow,
            "config_has_shutdown": record["config_has_shutdown"],
            "config_has_vlan10": record["config_has_vlan10"],
        }, indent=2))
        ok = (
            outcome.executed and outcome.rolled_back
            and outcome.rollback_complete
            and not outcome.incorrect_fix_left_in_place
            and after_flow["outcome"] in ("blocked", "partial")
            and record["config_has_shutdown"] and record["config_has_vlan10"]
        )
        return 0 if ok else 2
    finally:
        try:
            print("cleanup:", adapter.delete_devices_bulk(NAMES))
        except Exception:
            for n in NAMES:
                try:
                    adapter.delete_device(n)
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
