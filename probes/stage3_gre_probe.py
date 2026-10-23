#!/usr/bin/env python
"""GRE tunnel live probe (S3.A10).

The first probe showed `tunnel source <ip>` is dropped. This probe uses the
interface form (`tunnel source GigabitEthernet0/0`) on two directly connected
routers and runs a real ping across 172.16.0.0/30.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "gre-tunnel.txt"
R1, R2 = "s3p-gre-r1", "s3p-gre-r2"

R1 = [
    "hostname s3p-gre-r1",
    "interface GigabitEthernet0/0", " ip address 10.0.12.1 255.255.255.252",
    " no shutdown", " exit",
    "interface Tunnel0", " ip address 172.16.0.1 255.255.255.252",
    " tunnel source GigabitEthernet0/0", " tunnel destination 10.0.12.2",
    " tunnel mode gre ip", " exit",
]
R2 = [
    "hostname s3p-gre-r2",
    "interface GigabitEthernet0/0", " ip address 10.0.12.2 255.255.255.252",
    " no shutdown", " exit",
    "interface Tunnel0", " ip address 172.16.0.2 255.255.255.252",
    " tunnel source GigabitEthernet0/0", " tunnel destination 10.0.12.1",
    " tunnel mode gre ip", " exit",
]


def show(adapter: BridgeAdapter, device: str, command: str) -> str:
    adapter._console_show_full(device, "enable", timeout=10)
    result = adapter._console_show_full(device, command, timeout=30)
    return str(result.value) if result.is_known else f"UNKNOWN: {result.detail}"


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    for name in ("s3p-gre-r1", "s3p-gre-r2"):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created = []
    for name, pos in (("s3p-gre-r1", (200, 350)), ("s3p-gre-r2", (500, 350))):
        receipt = adapter.create_device(name, "router", model_hint="2911",
                                        position=pos)
        print("create", name, receipt.result.value, receipt.error)
        created.append(name)
    time.sleep(3)
    link = adapter.create_link("s3p-gre-r1", "GigabitEthernet0/0",
                               "s3p-gre-r2", "GigabitEthernet0/0",
                               media="crossover")
    print("link", link.result.value, link.error)
    time.sleep(3)
    for name, commands in (("s3p-gre-r1", R1), ("s3p-gre-r2", R2)):
        receipt = adapter.configure_ios(name, commands)
        print("config", name, receipt.result.value, receipt.error)
    time.sleep(12)
    lines = [f"# GRE probe @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}", ""]
    for name in ("s3p-gre-r1", "s3p-gre-r2"):
        for command in ("show running-config | include Tunnel0|tunnel",
                        "show ip interface brief"):
            text = show(adapter, name, command)
            lines.extend([f"## {name}: {command}", text, ""])
    ping = show(adapter, "s3p-gre-r1", "ping 172.16.0.2")
    lines.extend(["## r1: ping 172.16.0.2", ping, ""])
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text("\n".join(lines), encoding="utf-8")
    print("ping tail:", ping[-250:])
    for name in created:
        try:
            print("delete", name, adapter.delete_device(name).result.value)
        except Exception as exc:
            print("delete error", name, exc)
    print("wrote", RAW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
