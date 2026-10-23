#!/usr/bin/env python
"""ASA show-command probe (S3.C2-C6), using the console line directly.

    python probes/stage3_asa_shows.py

The generic console reader cannot anchor on an ASA prompt, so this probe
waits for boot, pushes through `configureIosDevice`, enters enable, and reads
each `show` command's tail after a fixed settle. The file is the verbatim PT
output; no interpretation is applied here.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "asa-shows.txt"
DEV = "s3p-asa-show"

PUSH = [
    "interface GigabitEthernet1/1", "nameif inside", "security-level 100",
    "ip address 10.99.0.1 255.255.255.0", "no shutdown", "exit",
    "interface GigabitEthernet1/2", "nameif outside", "security-level 0",
    "ip address 10.99.1.1 255.255.255.0", "no shutdown", "exit",
    "interface GigabitEthernet1/3", "nameif dmz", "security-level 50",
    "ip address 10.99.2.1 255.255.255.0", "no shutdown", "exit",
    "object network INSIDE-NET", "subnet 10.99.0.0 255.255.255.0",
    "nat (inside,outside) dynamic interface", "exit",
    "object-group network WEB-SERVERS", "network-object host 10.99.2.10", "exit",
    "access-list OUTSIDE-IN extended permit tcp any object-group WEB-SERVERS eq 80",
    "access-group OUTSIDE-IN in interface outside",
    "dhcpd address 10.99.0.100-10.99.0.150 inside", "dhcpd enable inside",
    "route outside 0.0.0.0 0.0.0.0 10.99.1.254 1",
    "hostname s3p-asa-show",
]

SHOWS = [
    "show nameif",
    "show interface ip brief",
    "show route",
    "show nat",
    "show dhcpd",
    "show access-list",
    "show object-group",
    "show running-config",
]


def expr(body: str) -> str:
    return (
        "(function(){var d=ipc.network().getDevice(" + repr(DEV) + ");"
        "if(!d){return 'E:no-device';}"
        "var cl=null;try{cl=d.getConsoleLine();}catch(e){return 'E:'+e;}"
        "if(!cl){return 'E:no-console';}" + body + "})()"
    )


def send(adapter: BridgeAdapter, body: str, wait: float = 15.0) -> str:
    answer = adapter.send_expression(expr(body), wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def out(adapter: BridgeAdapter) -> str:
    return send(adapter, "return String(cl.getOutput());")


def type_cmd(adapter: BridgeAdapter, command: str, settle: float = 6.0) -> str:
    send(adapter, f"cl.enterCommand({command!r}); return 'typed';")
    time.sleep(settle)
    raw = out(adapter)
    index = raw.rfind(command)
    return raw[index:] if index >= 0 else raw[-2000:]


def wait_boot(adapter: BridgeAdapter, timeout: float = 150.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        text = out(adapter)
        last = text
        if text.rstrip().endswith((">", "#")) and "Boot in" not in text[-500:]:
            return text.rstrip()[-120:]
        time.sleep(3.0)
    return f"TIMEOUT tail={last[-200:]!r}"


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    try:
        adapter.delete_device(DEV)
    except Exception:
        pass
    receipt = adapter.create_device(
        DEV, "firewall", model_hint="5506-X", position=(520, 220)
    )
    print("create:", receipt.result.value, receipt.error)
    print("boot:", wait_boot(adapter))
    time.sleep(2)
    push = adapter.configure_ios(DEV, PUSH)
    print("push:", push.result.value, push.error)
    time.sleep(10)
    print("enable:", type_cmd(adapter, "enable", settle=4.0)[-200:])
    print("pager:", type_cmd(adapter, "terminal pager 0", settle=3.0)[-200:])
    lines = [
        f"# ASA show probe @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"# device: {DEV}",
        f"# push: {push.result.value}",
        "",
    ]
    for command in SHOWS:
        print("read", command, flush=True)
        tail = type_cmd(adapter, command, settle=8.0)
        lines.extend([f"## {command}", tail, ""])
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text("\n".join(lines), encoding="utf-8")
    print("delete:", adapter.delete_device(DEV).result.value)
    print("wrote", RAW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
