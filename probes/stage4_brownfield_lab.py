#!/usr/bin/env python
"""Stage 4 live brownfield lab — import, diagnose, backup, repair, verify.

Creates a small two-VLAN network in the real Packet Tracer, imports it,
confirms an explicit objective, proves the clean flow, injects a real
wrong-access-VLAN fault, diagnoses it with the Stage 4 engine, repairs it
through the mandatory-backup + SafetyGate path, and proves the objective again.

Every artifact is written under ``probes/raw/`` and summarised in
``probes/live/stage4-brownfield.md``. Cleanup removes exactly the devices this
script created.

    python probes/stage4_brownfield_lab.py
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
from network_autopilot.brownfield.faults import ExpectedNetwork, FaultDetector
from network_autopilot.brownfield.importer import BrownfieldImporter, ImportOptions
from network_autopilot.brownfield.intent import AsIsIntentBuilder
from network_autopilot.brownfield.objectives import ObjectiveRegistry
from network_autopilot.brownfield.repair import BrownfieldRepairEngine
from network_autopilot.brownfield.report import DiagnosisReport
from network_autopilot.brownfield.tracer import PathTracer
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode

RAW = ROOT / "probes" / "raw"
LIVE = ROOT / "probes" / "live"
BACKUPS = ROOT / "probes" / "stage4-backups"

R = "s4l-r1"
S = "s4l-s1"
PC1 = "s4l-pc1"
PC2 = "s4l-pc2"
NAMES = [R, S, PC1, PC2]

ROUTER_CONFIG = [
    "interface GigabitEthernet0/0",
    "no shutdown",
    "exit",
    "interface GigabitEthernet0/0.10",
    "encapsulation dot1Q 10",
    "ip address 10.0.10.1 255.255.255.0",
    "exit",
    "interface GigabitEthernet0/0.20",
    "encapsulation dot1Q 20",
    "ip address 10.0.20.1 255.255.255.0",
    "exit",
]
SWITCH_CONFIG = [
    "vlan 10",
    "name USERS",
    "exit",
    "vlan 20",
    "name GUESTS",
    "exit",
    "interface FastEthernet0/1",
    "switchport mode trunk",
    "switchport trunk allowed vlan 10,20",
    "exit",
    "interface FastEthernet1/1",
    "switchport mode access",
    "switchport access vlan 10",
    "exit",
    "interface FastEthernet2/1",
    "switchport mode access",
    "switchport access vlan 20",
    "exit",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _wait_flow(adapter, source: str, destination: str, *, expect_reachable: bool,
               timeout: float = 150.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = adapter.run_flow_test(source, destination)
        last = result
        value = result.outcome.value
        if expect_reachable and value == "reachable":
            return {
                "outcome": value, "detail": result.detail,
                "expect": "reachable", "passed": True,
            }
        if not expect_reachable and value in ("blocked", "partial"):
            return {
                "outcome": value, "detail": result.detail,
                "expect": "blocked", "passed": True,
            }
        time.sleep(6)
    return {
        "outcome": last.outcome.value if last else "unknown",
        "detail": (last.detail if last else "no result"),
        "expect": "reachable" if expect_reachable else "blocked",
        "passed": False,
    }


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    probe = adapter.ping(timeout=5)
    if not probe.reachable:
        print("PT UNREACHABLE:", probe.detail.value or probe.detail.status.value)
        return 4
    RAW.mkdir(parents=True, exist_ok=True)
    LIVE.mkdir(parents=True, exist_ok=True)
    evidence: dict = {
        "started": _now(),
        "adapter": probe.detail.value or probe.detail.detail or probe.adapter_name,
    }
    try:
        for name in NAMES:
            adapter.delete_device(name)
        time.sleep(3)
        created = {}
        for name, role in ((R, "router"), (S, "switch"), (PC1, "pc"), (PC2, "pc")):
            receipt = adapter.create_device(
                name, role, model_hint="2911" if name == R else None
            )
            created[name] = receipt.result.value
            if receipt.result.value != "success":
                raise RuntimeError(f"create {name}: {receipt.error}")
            time.sleep(2)
        evidence["created"] = created
        links = [
            (R, "GigabitEthernet0/0", S, "FastEthernet0/1"),
            (S, "FastEthernet1/1", PC1, "FastEthernet0"),
            (S, "FastEthernet2/1", PC2, "FastEthernet0"),
        ]
        evidence["links"] = []
        for a, ap, b, bp in links:
            receipt = adapter.create_link(a, ap, b, bp)
            evidence["links"].append(
                {"a": f"{a}:{ap}", "b": f"{b}:{bp}",
                 "result": receipt.result.value, "error": receipt.error}
            )
            if receipt.result.value != "success":
                raise RuntimeError(f"link {a}:{ap}-{b}:{bp}: {receipt.error}")
            time.sleep(2)
        pushes = {
            R: adapter.configure_ios(R, ROUTER_CONFIG),
            S: adapter.configure_ios(S, SWITCH_CONFIG),
        }
        for host, ip, gateway in (
            (PC1, "10.0.10.10", "10.0.10.1"),
            (PC2, "10.0.20.10", "10.0.20.1"),
        ):
            pushes[host] = adapter.configure_host(
                host, dhcp=False, ip=ip, mask="255.255.255.0",
                gateway=gateway, dns="8.8.8.8",
            )
        evidence["pushes"] = {
            name: {"result": receipt.result.value, "error": receipt.error}
            for name, receipt in pushes.items()
        }
        for receipt in pushes.values():
            if receipt.result.value != "success":
                raise RuntimeError(f"configuration push failed: {receipt.error}")
        time.sleep(8)

        # -- S4.1/S4.2: import and read back --------------------------------
        imported = BrownfieldImporter(
            adapter,
            options=ImportOptions(read_positions=False, read_stp=True),
        ).import_network(device_names=NAMES)
        intent = AsIsIntentBuilder().build(imported)
        expected = ExpectedNetwork.from_spec(intent.spec)
        evidence["import"] = {
            "summary": imported.inventory.summary(),
            "incomplete": imported.inventory.incomplete,
            "links": [
                {"a": f"{l.a_device}:{l.a_port}", "b": f"{l.b_device}:{l.b_port}"}
                for l in imported.inventory.links
            ],
            "config_status": {
                name: {
                    "status": device.config.status.value,
                    "detail": device.config.detail,
                    "chars": len(device.config.raw),
                }
                for name, device in imported.inventory.devices.items()
                if device.config is not None
            },
            "hosts": {
                name: {
                    "ip": device.host.ip.value if device.host and device.host.ip.is_known else None,
                    "gateway": device.host.gateway.value if device.host and device.host.gateway.is_known else None,
                    "dns": device.host.dns.value if device.host and device.host.dns.is_known else None,
                    "dhcp": device.host.dhcp.value if device.host and device.host.dhcp.is_known else None,
                }
                for name, device in imported.inventory.devices.items()
                if device.host is not None
            },
            "as_is_confidence": intent.counts,
            "as_is_unsupported": intent.unsupported,
        }
        _write(
            RAW / "stage4-import.json",
            json.dumps(evidence["import"], indent=2, sort_keys=True),
        )
        for name, device in imported.inventory.devices.items():
            if device.config is not None and device.config.raw:
                _write(RAW / f"stage4-running-{name}.txt", device.config.raw)

        # -- S4.5: confirmed objective, clean trace and flow -----------------
        objectives = ObjectiveRegistry()
        objective = objectives.add_user_requirement(
            f"{PC1} must communicate with {PC2}"
        )
        before_flow = _wait_flow(adapter, PC1, PC2, expect_reachable=True)
        trace_before = PathTracer(
            imported.inventory, imported.corpus, objectives=objectives
        ).trace_objective(objective)
        evidence["baseline"] = {
            "flow": before_flow,
            "trace_status": trace_before.status,
            "trace": trace_before.to_dict(),
        }
        if not before_flow["passed"]:
            raise RuntimeError(f"clean flow did not pass: {before_flow}")

        # -- Inject a real wrong-access-VLAN fault --------------------------
        fault_push = adapter.configure_ios(
            S,
            [
                "interface FastEthernet1/1",
                "switchport access vlan 20",
                "exit",
            ],
        )
        evidence["fault"] = {
            "kind": "wrong-access-vlan",
            "device": S,
            "interface": "FastEthernet1/1",
            "pushed": fault_push.result.value,
            "error": fault_push.error,
        }
        if fault_push.result.value != "success":
            raise RuntimeError(f"fault push failed: {fault_push.error}")
        time.sleep(8)
        faulted = BrownfieldImporter(
            adapter,
            options=ImportOptions(read_positions=False, read_stp=True),
        ).import_network(device_names=NAMES)
        fault_flow = _wait_flow(adapter, PC1, PC2, expect_reachable=False)
        detector = FaultDetector(
            faulted.inventory, faulted.corpus, objectives, expected=expected
        )
        diagnostics = detector.diagnose(objective)
        primary = next(
            (d for d in diagnostics if d.cause_class == "wrong-access-vlan"),
            None,
        )
        report = DiagnosisReport.build(
            "stage4-live-two-vlan",
            diagnostics,
            objectives=[objective],
            observations={"fault_flow": fault_flow},
        )
        _write(RAW / "stage4-diagnosis.json", report.to_json())
        _write(RAW / "stage4-diagnosis.md", report.to_markdown())
        evidence["diagnosis"] = {
            "fault_flow": fault_flow,
            "diagnostics": [d.to_dict() for d in diagnostics],
            "summary": report.summary(),
            "primary": primary.to_dict() if primary is not None else None,
        }
        if primary is None:
            raise RuntimeError("the injected fault was not diagnosed")

        # -- S4.9/S4.10: backup, repair, verify -----------------------------
        gate = SafetyGate(
            SafetyMode.AUTONOMOUS,
            MutationBudget(max_mutations=50, max_repair_mutations=5),
        )
        backup = BackupManager(adapter, root=BACKUPS)
        engine = BrownfieldRepairEngine(
            adapter, gate, objectives=objectives, backup=backup
        )
        plan = engine.plan([primary], objective=objective)
        outcome = engine.execute(plan, objective=objective, approved=True)
        repair_record = {
            "plan": plan.to_dict(),
            "outcome": outcome.to_dict(),
            "flow_after": _wait_flow(adapter, PC1, PC2, expect_reachable=True),
        }
        evidence["repair"] = repair_record
        _write(
            RAW / "stage4-repair.json",
            json.dumps(repair_record, indent=2, sort_keys=True),
        )
        evidence["finished"] = _now()
        _write(
            RAW / "stage4-live.json",
            json.dumps(evidence, indent=2, sort_keys=True),
        )
        lines = [
            "# Stage 4 live brownfield lab",
            "",
            f"- started: {evidence['started']}",
            f"- finished: {evidence['finished']}",
            f"- devices: {', '.join(NAMES)}",
            f"- import: {evidence['import']['summary']}",
            f"- clean flow: {before_flow['outcome']} (expected reachable)",
            f"- injected fault: wrong access VLAN on {S}:FastEthernet1/1",
            f"- faulted flow: {fault_flow['outcome']} "
            f"(expected blocked, observed as {fault_flow['outcome']})",
            f"- diagnosed: {primary.cause_class} on "
            f"{primary.affected_device}:{primary.affected_interface} "
            f"({primary.confidence})",
            f"- repair: {'repaired' if outcome.repaired else 'not repaired'}",
            f"- backup available: "
            f"{outcome.backup_validation.available if outcome.backup_validation else False}",
            f"- flow after repair: {repair_record['flow_after']['outcome']}",
            "",
            "Evidence: `probes/raw/stage4-import.json`, "
            "`probes/raw/stage4-diagnosis.{json,md}`, "
            "`probes/raw/stage4-repair.json`, "
            "`probes/raw/stage4-live.json`.",
        ]
        _write(LIVE / "stage4-brownfield.md", "\n".join(lines) + "\n")
        print(json.dumps(
            {
                "clean_flow": before_flow,
                "fault_flow": fault_flow,
                "diagnosed": primary.cause_class,
                "repair_repaired": outcome.repaired,
                "repair_objective": outcome.objective_verdict,
                "backup": (
                    outcome.backup_validation.available
                    if outcome.backup_validation else False
                ),
                "flow_after": repair_record["flow_after"],
                "evidence": str(RAW),
            },
            indent=2,
        ))
        return 0 if outcome.repaired and repair_record["flow_after"]["passed"] else 2
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["finished"] = _now()
        _write(
            RAW / "stage4-live-error.json",
            json.dumps(evidence, indent=2, sort_keys=True),
        )
        print("LIVE LAB ERROR:", evidence["error"])
        return 1
    finally:
        try:
            outcomes = adapter.delete_devices_bulk(NAMES)
            print("cleanup:", outcomes)
        except Exception:
            for name in NAMES:
                try:
                    adapter.delete_device(name)
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
