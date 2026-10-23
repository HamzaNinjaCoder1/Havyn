#!/usr/bin/env python
"""IPv6 / OSPFv3 live probe (S3.A13, S3.A14).

Creates two routers on one link, configures IPv6 addresses and an OSPFv3
process, then reads the IPv6 route table, OSPFv3 neighbors and runs a real
IPv6 ping between the routers. Records the verbatim console output.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "ipv6-ospfv3.txt"
R1, R2 = "s3p-ipv6-r1", "s3p-ipv6-r2"

R1_CFG = [
    "hostname s3p-ipv6-r1",
    "ipv6 unicast-routing",
    "interface GigabitEthernet0/0", " ip address 10.0.12.1 255.255.255.252",
    " ipv6 address 2001:db8:12::1/64", " ipv6 enable", " no shutdown", " exit",
    "ipv6 router ospf 1", " router-id 1.1.1.1", " exit",
    "interface GigabitEthernet0/0", " ipv6 ospf 1 area 0", " exit",
]
R2_CFG = [
    "hostname s3p-ipv6-r2",
    "ipv6 unicast-routing",
    "interface GigabitEthernet0/0", " ip address 10.0.12.2 255.255.255.252",
    " ipv6 address 2001:db8:12::2/64", " ipv6 enable", " no shutdown", " exit",
    "ipv6 router ospf 1", " router-id 2.2.2.2", " exit",
    "interface GigabitEthernet0/0", " ipv6 ospf 1 area 0", " exit",
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
    for name in (R1, R2):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created = []
    for name, position in ((R1, (200, 200)), (R2, (500, 200))):
        receipt = adapter.create_device(name, "router", model_hint="2911",
                                        position=position)
        print("create", name, receipt.result.value, receipt.error)
        created.append(name)
    time.sleep(3)
    link = adapter.create_link(R1, "GigabitEthernet0/0", R2, "GigabitEthernet0/0",
                               media="crossover")
    print("link", link.result.value, link.error)
    time.sleep(3)
    for name, commands in ((R1, R1_CFG), (R2, R2_CFG)):
        receipt = adapter.configure_ios(name, commands)
        print("config", name, receipt.result.value, receipt.error)
    time.sleep(25)
    lines = [
        f"# IPv6 / OSPFv3 probe @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    for name in (R1, R2):
        for command in ("show ipv6 interface brief", "show ipv6 route",
                        "show ipv6 ospf neighbor"):
            text = show(adapter, name, command)
            print(f"{name} {command}: {text[-200:]!r}", flush=True)
            lines.extend([f"## {name}: {command}", text, ""])
    ping = show(adapter, R1, "ping 2001:db8:12::2")
    lines.extend(["## r1: ping 2001:db8:12::2", ping, ""])
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text("\n".join(lines), encoding="utf-8")
    print("ping tail:", ping[-200:])
    for name in created:
        try:
            print("delete", name, adapter.delete_device(name).result.value)
        except Exception as exc:
            print("delete error", name, exc)
    print("wrote", RAW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
