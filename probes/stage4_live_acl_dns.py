#!/usr/bin/env python
"""Live ACL + DNS fault proof (S4.7), rerun after the DnsClient fix."""
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

S, PC1, PC2 = "s4z-s1", "s4z-pc1", "s4z-pc2"
NAMES = [S, PC1, PC2]


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    try:
        for name in NAMES:
            adapter.delete_device(name)
        time.sleep(2)
        for name, role in ((S, "switch"), (PC1, "pc"), (PC2, "pc")):
            if adapter.create_device(name, role).result.value != "success":
                return 2
            time.sleep(2)
        for port, host in (("FastEthernet1/1", PC1), ("FastEthernet2/1", PC2)):
            if adapter.create_link(S, port, host, "FastEthernet0").result.value != "success":
                return 2
            time.sleep(2)
        adapter.configure_ios(
            S,
            [
                "vlan 10", "name USERS", "exit",
                "interface FastEthernet1/1", "switchport mode access",
                "switchport access vlan 10", "exit",
                "interface FastEthernet2/1", "switchport mode access",
                "switchport access vlan 10", "exit",
            ],
        )
        adapter.configure_host(PC1, dhcp=False, ip="10.246.0.10",
                               mask="255.255.255.0", dns="8.8.8.8")
        adapter.configure_host(PC2, dhcp=False, ip="10.246.0.11",
                               mask="255.255.255.0", dns="8.8.8.8")
        time.sleep(6)
        clean = BrownfieldImporter(adapter, options=ImportOptions(read_stp=True)).import_network(device_names=NAMES)
        expected = ExpectedNetwork.from_spec(AsIsIntentBuilder().build(clean).spec)
        objectives = ObjectiveRegistry()
        objective = objectives.add_user_requirement(f"{PC1} must communicate with {PC2}")

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

        acl_push = adapter.configure_ios(
            S,
            [
                "access-list 100 deny ip any any",
                "interface FastEthernet1/1",
                "ip access-group 100 in",
                "exit",
            ],
        )
        time.sleep(5)
        acl_seen = diagnose()
        acl_ok = "acl-block" in acl_seen
        adapter.configure_ios(
            S,
            [
                "interface FastEthernet1/1",
                "no ip access-group 100 in",
                "exit",
                "no access-list 100",
            ],
        )
        time.sleep(4)
        dns_push = adapter.configure_host(
            PC2, dhcp=False, ip="10.246.0.11", mask="255.255.255.0",
            dns="192.0.2.53",
        )
        time.sleep(5)
        dns_seen = diagnose()
        dns_ok = "dns-misconfiguration" in dns_seen
        record = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "acl_push": acl_push.result.value,
            "acl_predicted": sorted(acl_seen),
            "acl_correct": acl_ok,
            "dns_push": dns_push.result.value,
            "dns_push_error": dns_push.error,
            "dns_predicted": sorted(dns_seen),
            "dns_correct": dns_ok,
            "dns_diagnosis": (
                dns_seen["dns-misconfiguration"].to_dict()
                if dns_ok else None
            ),
        }
        (ROOT / "probes" / "raw" / "stage4-live-acl-dns.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        print(json.dumps(record, indent=2)[:2000])
        return 0 if acl_ok and dns_ok else 2
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
