#!/usr/bin/env python
"""Live ASA running-config read-back (S4.2). Creates a 5506-X, configures
zones/ACL/route, reads `show running-config` through the ASA enable-mode
console, parses it, and writes raw evidence."""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from network_autopilot.brownfield.parser import IosConfigParser

DEV = "s4a-asa"
PUSH = [
    "interface GigabitEthernet1/1", "nameif inside", "security-level 100",
    "ip address 10.230.0.1 255.255.255.0", "no shutdown", "exit",
    "interface GigabitEthernet1/2", "nameif outside", "security-level 0",
    "ip address 203.0.113.1 255.255.255.252", "no shutdown", "exit",
    "access-list OUTSIDE-IN extended permit icmp any any echo-reply",
    "access-list OUTSIDE-IN extended deny ip any any",
    "access-group OUTSIDE-IN in interface outside",
    "route outside 0.0.0.0 0.0.0.0 203.0.113.2 1",
]


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    try:
        adapter.delete_device(DEV)
    except Exception:
        pass
    try:
        receipt = adapter.create_device(DEV, "firewall", model_hint="5506-X",
                                        position=(500, 220))
        print("create:", receipt.result.value, receipt.error)
        if receipt.result.value != "success":
            return 2
        time.sleep(5)
        push = adapter.configure_ios(DEV, PUSH)
        print("push:", push.result.value, push.error)
        deadline = time.time() + 180
        config = None
        while time.time() < deadline:
            config = adapter.read_running_config(DEV)
            if config.is_known:
                break
            time.sleep(6)
        record = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "device": DEV,
            "push": push.result.value,
            "push_error": push.error,
            "read_known": bool(config and config.is_known),
            "read_detail": config.detail if config else "no answer",
            "raw": str(config.value) if config and config.is_known else "",
        }
        if config and config.is_known:
            parsed = IosConfigParser().parse(str(config.value), device=DEV)
            record["parser"] = {
                "platform": parsed.platform.value,
                "interfaces": {
                    n: {"nameif": i.nameif, "security_level": i.security_level,
                        "ip": i.ip, "mask": i.mask}
                    for n, i in parsed.interfaces.items() if i.ip
                },
                "acl_bindings": parsed.globals.acl_bindings,
                "static_routes": [r.to_dict() for r in parsed.static_routes],
                "unparsed": [l.text for l in parsed.unparsed],
            }
        out = ROOT / "probes" / "raw" / "stage4-live-asa.json"
        out.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(json.dumps({k: record[k] for k in ("read_known", "read_detail")}, indent=2))
        if not (config and config.is_known):
            return 2
        parser = record["parser"]
        in_zone = parser["interfaces"].get("GigabitEthernet1/1", {})
        out_zone = parser["interfaces"].get("GigabitEthernet1/2", {})
        ok = (
            parser["platform"] == "asa"
            and in_zone.get("nameif") == "inside"
            and in_zone.get("security_level") == 100
            and out_zone.get("nameif") == "outside"
            and out_zone.get("security_level") == 0
            and parser["acl_bindings"]
            and any(r["cidr"] == "0.0.0.0/0" for r in parser["static_routes"])
        )
        print("verified:", ok)
        return 0 if ok else 2
    finally:
        try:
            print("cleanup:", adapter.delete_device(DEV).result.value)
        except Exception as exc:
            print("cleanup error:", exc)


if __name__ == "__main__":
    raise SystemExit(main())
