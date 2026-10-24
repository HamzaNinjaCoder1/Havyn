#!/usr/bin/env python
"""Live NAT observation (S4.6). Builds a PAT lab, proves PC->server, then
reads `show ip nat translations` and records the real translation table."""
from __future__ import annotations
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

R, PC, SRV = "s4n-r1", "s4n-pc1", "s4n-srv"
NAMES = [R, PC, SRV]
CONFIG = [
    "interface GigabitEthernet0/0", "ip address 10.243.0.1 255.255.255.0",
    "ip nat inside", "no shutdown", "exit",
    "interface GigabitEthernet0/1", "ip address 203.0.113.1 255.255.255.252",
    "ip nat outside", "no shutdown", "exit",
    "access-list 1 permit 10.243.0.0 0.0.0.255",
    "ip nat inside source list 1 interface GigabitEthernet0/1 overload",
]

def main() -> int:
    a = BridgeAdapter(host_bridge=False)
    if not a.ping(timeout=5).reachable:
        print("PT UNREACHABLE"); return 4
    try:
        for n in NAMES: a.delete_device(n)
        time.sleep(2)
        for n, r, model in ((R, "router", "2911"), (PC, "pc", None), (SRV, "server", None)):
            if a.create_device(n, r, model_hint=model).result.value != "success": return 2
            time.sleep(2)
        for aa, ap, b, bp in ((R, "GigabitEthernet0/0", PC, "FastEthernet0"),
                              (R, "GigabitEthernet0/1", SRV, "FastEthernet0")):
            if a.create_link(aa, ap, b, bp).result.value != "success": return 2
            time.sleep(2)
        push = a.configure_ios(R, CONFIG)
        print("push", push.result.value, push.error)
        a.configure_host(PC, dhcp=False, ip="10.243.0.10", mask="255.255.255.0", gateway="10.243.0.1")
        a.configure_host(SRV, dhcp=False, ip="203.0.113.2", mask="255.255.255.252", gateway="203.0.113.1")
        time.sleep(8)
        flow = None
        for _ in range(20):
            flow = a.run_flow_test(PC, SRV)
            if flow.outcome.value == "reachable": break
            time.sleep(5)
        answer = a.read_nat_translations(R)
        record = {
            "flow": flow.outcome.value if flow else "unknown",
            "nat_known": bool(answer.is_known),
            "nat_detail": answer.detail if not answer.is_known else None,
            "translations": answer.value if answer.is_known else [],
        }
        (ROOT / "probes" / "raw" / "stage4-live-nat.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        print(json.dumps(record, indent=2)[:3000])
        ok = flow is not None and flow.outcome.value == "reachable" and bool(answer.is_known)
        return 0 if ok else 2
    finally:
        try: print("cleanup:", a.delete_devices_bulk(NAMES))
        except Exception:
            for n in NAMES:
                try: a.delete_device(n)
                except Exception: pass

if __name__ == "__main__":
    raise SystemExit(main())
