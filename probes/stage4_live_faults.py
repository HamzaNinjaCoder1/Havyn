#!/usr/bin/env python
"""Live multi-class fault injection benchmark (S4.5/S4.7).

Builds a real router-on-a-stick lab, injects each supported fault class,
diagnoses it with the real Stage 4 engine, restores the network, and finally
injects two faults at once. Results are raw evidence, not assertions from a
mock.
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
from network_autopilot.brownfield.faults import ExpectedNetwork, FaultDetector
from network_autopilot.brownfield.importer import BrownfieldImporter, ImportOptions
from network_autopilot.brownfield.intent import AsIsIntentBuilder
from network_autopilot.brownfield.objectives import ObjectiveRegistry

R, S, PC1, PC2 = "s4f-r1", "s4f-sw1", "s4f-pc1", "s4f-pc2"
NAMES = [R, S, PC1, PC2]
R_CONFIG = [
    "interface GigabitEthernet0/0", "no shutdown", "exit",
    "interface GigabitEthernet0/0.10", "encapsulation dot1Q 10",
    "ip address 10.244.0.1 255.255.255.0", "exit",
    "ip route 192.0.2.0 255.255.255.0 10.244.0.254",
]
S_CONFIG = [
    "vlan 10", "name USERS", "exit",
    "interface FastEthernet0/1", "switchport mode trunk",
    "switchport trunk allowed vlan 10", "exit",
    "interface FastEthernet1/1", "switchport mode access",
    "switchport access vlan 10", "exit",
    "interface FastEthernet2/1", "switchport mode access",
    "switchport access vlan 10", "exit",
]
PC1_PORT, PC2_PORT = "FastEthernet1/1", "FastEthernet2/1"


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    results = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scenarios": [],
    }
    try:
        for name in NAMES:
            adapter.delete_device(name)
        time.sleep(2)
        for name, role, model in (
            (R, "router", "2911"), (S, "switch", None),
            (PC1, "pc", None), (PC2, "pc", None),
        ):
            receipt = adapter.create_device(name, role, model_hint=model)
            if receipt.result.value != "success":
                return 2
            time.sleep(2)
        for a_dev, a_port, b_dev, b_port in (
            (R, "GigabitEthernet0/0", S, "FastEthernet0/1"),
            (S, PC1_PORT, PC1, "FastEthernet0"),
            (S, PC2_PORT, PC2, "FastEthernet0"),
        ):
            receipt = adapter.create_link(a_dev, a_port, b_dev, b_port)
            if receipt.result.value != "success":
                return 2
            time.sleep(2)
        adapter.configure_ios(R, R_CONFIG)
        adapter.configure_ios(S, S_CONFIG)
        adapter.configure_host(
            PC1, dhcp=False, ip="10.244.0.10", mask="255.255.255.0",
            gateway="10.244.0.1", dns="8.8.8.8",
        )
        adapter.configure_host(
            PC2, dhcp=False, ip="10.244.0.11", mask="255.255.255.0",
            gateway="10.244.0.1", dns="8.8.8.8",
        )
        time.sleep(8)
        clean = BrownfieldImporter(
            adapter, options=ImportOptions(read_stp=True)
        ).import_network(device_names=NAMES)
        expected = ExpectedNetwork.from_spec(AsIsIntentBuilder().build(clean).spec)
        objectives = ObjectiveRegistry()
        objective = objectives.add_user_requirement(
            f"{PC1} must communicate with {PC2}"
        )

        def diagnose():
            imported = BrownfieldImporter(
                adapter, options=ImportOptions(read_stp=True)
            ).import_network(device_names=NAMES)
            detector = FaultDetector(
                imported.inventory, imported.corpus, objectives, expected=expected
            )
            found = {}
            for diagnostic in detector.diagnose(objective) + detector.detect_all():
                found.setdefault(diagnostic.cause_class, diagnostic)
            return found

        def restore(commands_by_device):
            for device, commands in commands_by_device.items():
                if commands:
                    adapter.configure_ios(device, commands)
            time.sleep(4)

        def scenario(label, inject, restore_cmds, expected_class):
            inject()
            time.sleep(5)
            seen = diagnose()
            correct = expected_class in seen
            results["scenarios"].append(
                {
                    "label": label,
                    "expected": expected_class,
                    "predicted": sorted(seen),
                    "correct": correct,
                    "diagnosis": (
                        seen[expected_class].to_dict()
                        if expected_class in seen else None
                    ),
                }
            )
            print(label, "->", "OK" if correct else "MISS", sorted(seen))
            restore(restore_cmds)
            time.sleep(4)

        scenario(
            "port-shutdown",
            lambda: adapter.configure_ios(
                S, [f"interface {PC2_PORT}", "shutdown", "exit"]
            ),
            {S: [f"interface {PC2_PORT}", "no shutdown", "exit"]},
            "port-shutdown",
        )
        scenario(
            "wrong-access-vlan",
            lambda: adapter.configure_ios(
                S, [f"interface {PC2_PORT}", "switchport access vlan 20", "exit"]
            ),
            {S: [f"interface {PC2_PORT}", "switchport access vlan 10", "exit"]},
            "wrong-access-vlan",
        )
        scenario(
            "wrong-ip",
            lambda: adapter.configure_host(
                PC2, dhcp=False, ip="10.244.0.99", mask="255.255.255.0",
                gateway="10.244.0.1", dns="8.8.8.8",
            ),
            {},
            "wrong-ip",
        )
        adapter.configure_host(
            PC2, dhcp=False, ip="10.244.0.11", mask="255.255.255.0",
            gateway="10.244.0.1", dns="8.8.8.8",
        )
        time.sleep(3)
        scenario(
            "wrong-gateway",
            lambda: adapter.configure_host(
                PC2, dhcp=False, ip="10.244.0.11", mask="255.255.255.0",
                gateway="10.244.0.254", dns="8.8.8.8",
            ),
            {},
            "wrong-gateway",
        )
        adapter.configure_host(
            PC2, dhcp=False, ip="10.244.0.11", mask="255.255.255.0",
            gateway="10.244.0.1", dns="8.8.8.8",
        )
        time.sleep(3)
        scenario(
            "missing-route",
            lambda: adapter.configure_ios(
                R, ["no ip route 192.0.2.0 255.255.255.0 10.244.0.254"]
            ),
            {R: ["ip route 192.0.2.0 255.255.255.0 10.244.0.254"]},
            "missing-route",
        )
        scenario(
            "acl-block",
            lambda: adapter.configure_ios(
                S,
                [
                    "access-list 100 deny ip any any",
                    f"interface {PC1_PORT}",
                    "ip access-group 100 in",
                    "exit",
                ],
            ),
            {
                S: [
                    f"interface {PC1_PORT}",
                    "no ip access-group 100 in",
                    "exit",
                    "no access-list 100",
                ]
            },
            "acl-block",
        )
        scenario(
            "dns-misconfiguration",
            lambda: adapter.configure_host(
                PC2, dhcp=False, ip="10.244.0.11", mask="255.255.255.0",
                gateway="10.244.0.1", dns="192.0.2.53",
            ),
            {},
            "dns-misconfiguration",
        )
        adapter.configure_host(
            PC2, dhcp=False, ip="10.244.0.11", mask="255.255.255.0",
            gateway="10.244.0.1", dns="8.8.8.8",
        )
        time.sleep(3)

        adapter.configure_ios(
            S,
            [
                f"interface {PC2_PORT}", "shutdown", "exit",
                f"interface {PC1_PORT}", "switchport access vlan 20", "exit",
            ],
        )
        time.sleep(6)
        seen = diagnose()
        both = "port-shutdown" in seen and "wrong-access-vlan" in seen
        results["multi_fault"] = {"detected": sorted(seen), "both_detected": both}
        print("multi ->", both, sorted(seen))
        adapter.configure_ios(
            S,
            [
                f"interface {PC2_PORT}", "no shutdown", "exit",
                f"interface {PC1_PORT}", "switchport access vlan 10", "exit",
            ],
        )
        correct = sum(1 for item in results["scenarios"] if item["correct"])
        results["summary"] = {
            "scenarios": len(results["scenarios"]),
            "correct": correct,
            "accuracy": round(correct / len(results["scenarios"]), 4),
            "multi_fault_both_detected": both,
        }
        (ROOT / "probes" / "raw" / "stage4-live-faults.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8"
        )
        print(json.dumps(results["summary"], indent=2))
        return 0 if correct == len(results["scenarios"]) and both else 2
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
