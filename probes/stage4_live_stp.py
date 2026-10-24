#!/usr/bin/env python
"""Live STP observation (S4.6/S4.7). Builds a two-switch loop, reads
`show spanning-tree`, then traces a host-to-host path and verifies that
intentional STP blocking is reported as INFO, not as a connectivity fault."""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from network_autopilot.brownfield.importer import BrownfieldImporter, ImportOptions
from network_autopilot.brownfield.tracer import PathTracer

S1, S2, PC1, PC2 = "s4s-sw1", "s4s-sw2", "s4s-pc1", "s4s-pc2"
NAMES = [S1, S2, PC1, PC2]


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    try:
        for n in NAMES:
            adapter.delete_device(n)
        time.sleep(2)
        for n, r in ((S1, "switch"), (S2, "switch"), (PC1, "pc"), (PC2, "pc")):
            receipt = adapter.create_device(n, r)
            print("create", n, receipt.result.value)
            if receipt.result.value != "success":
                return 2
            time.sleep(2)
        links = [
            (S1, "FastEthernet0/1", S2, "FastEthernet0/1"),
            (S1, "FastEthernet1/1", S2, "FastEthernet1/1"),
            (S1, "FastEthernet2/1", PC1, "FastEthernet0"),
            (S2, "FastEthernet2/1", PC2, "FastEthernet0"),
        ]
        for a, ap, b, bp in links:
            receipt = adapter.create_link(a, ap, b, bp)
            print("link", ap, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            time.sleep(2)
        adapter.configure_ios(S1, ["spanning-tree vlan 1 root primary"])
        adapter.configure_host(PC1, dhcp=False, ip="10.240.0.10",
                               mask="255.255.255.0")
        adapter.configure_host(PC2, dhcp=False, ip="10.240.0.11",
                               mask="255.255.255.0")
        deadline = time.time() + 150
        rows = []
        while time.time() < deadline:
            rows = []
            for switch in (S1, S2):
                answer = adapter.read_spanning_tree(switch)
                if answer.is_known:
                    rows.extend(answer.value or [])
            if any(str(r.get("state")) in ("BLK", "ALN") for r in rows):
                break
            time.sleep(8)
        record = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stp_rows": rows,
            "blocked": [r for r in rows if str(r.get("state")) in ("BLK", "ALN")],
        }
        print(json.dumps(record, indent=2)[:2000])
        imported = BrownfieldImporter(
            adapter, options=ImportOptions(read_positions=False, read_stp=True)
        ).import_network(device_names=NAMES)
        trace = PathTracer(imported.inventory, imported.corpus).trace(PC1, PC2)
        record["trace_status"] = trace.status
        record["trace_stp_checks"] = [
            c.to_dict() for hop in trace.hops for c in hop.checks if c.name == "stp"
        ]
        record["trace_first_failure"] = (
            trace.first_failing_check.to_dict()
            if trace.first_failing_check is not None else None
        )
        out = ROOT / "probes" / "raw" / "stage4-live-stp.json"
        out.write_text(json.dumps(record, indent=2), encoding="utf-8")
        intentional = (
            bool(record["blocked"])
            and trace.status == "config_consistent"
            and all(c["status"] == "info" for c in record["trace_stp_checks"])
        )
        print("intentional-blocking-distinguished:", intentional)
        return 0 if intentional else 2
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
