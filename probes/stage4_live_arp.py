#!/usr/bin/env python
"""Live ARP observation (S4.6). Pings once, then reads the host's own ARP
cache through `arp -a` and verifies the peer entry."""
from __future__ import annotations
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

S, PC1, PC2 = "s4p-sw1", "s4p-pc1", "s4p-pc2"
NAMES = [S, PC1, PC2]

def main() -> int:
    a = BridgeAdapter(host_bridge=False)
    if not a.ping(timeout=5).reachable:
        print("PT UNREACHABLE"); return 4
    try:
        for n in NAMES: a.delete_device(n)
        time.sleep(2)
        for n, r in ((S, "switch"), (PC1, "pc"), (PC2, "pc")):
            if a.create_device(n, r).result.value != "success": return 2
            time.sleep(2)
        for b, bp in ((PC1, "FastEthernet0"), (PC2, "FastEthernet0")):
            port = "FastEthernet1/1" if b == PC1 else "FastEthernet2/1"
            if a.create_link(S, port, b, bp).result.value != "success": return 2
            time.sleep(2)
        a.configure_host(PC1, dhcp=False, ip="10.242.0.10", mask="255.255.255.0")
        a.configure_host(PC2, dhcp=False, ip="10.242.0.11", mask="255.255.255.0")
        time.sleep(8)
        for _ in range(10):
            flow = a.run_flow_test(PC1, PC2)
            if flow.outcome.value == "reachable": break
            time.sleep(5)
        answer = a.read_arp_table(PC1)
        record = {
            "arp_known": bool(answer.is_known),
            "arp_detail": answer.detail if not answer.is_known else None,
            "entries": answer.value if answer.is_known else [],
            "flow": flow.outcome.value,
        }
        (ROOT / "probes" / "raw" / "stage4-live-arp.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        print(json.dumps(record, indent=2))
        ok = bool(answer.is_known and any(e.get("ip") == "10.242.0.11" for e in answer.value or []))
        return 0 if ok else 2
    finally:
        try: print("cleanup:", a.delete_devices_bulk(NAMES))
        except Exception:
            for n in NAMES:
                try: a.delete_device(n)
                except Exception: pass

if __name__ == "__main__":
    raise SystemExit(main())
