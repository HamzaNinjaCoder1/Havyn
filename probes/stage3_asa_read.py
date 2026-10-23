#!/usr/bin/env python
"""ASA read-back probe (S3.C2-C6).

The ASA has no `getCommandLine()`; `getConsoleLine()` is its console.  The
generic console reader cannot anchor on a prompt there, so this probe uses the
ASA-specific door, drains `--More--`, and prints the verbatim output of each
`show` command.

    python probes/stage3_asa_read.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw"
DEV = "s3p-asa-read"

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
    "dhcpd address 10.99.0.100-10.99.0.150 inside",
    "dhcpd enable inside",
    "route outside 0.0.0.0 0.0.0.0 10.99.1.254 1",
    "hostname s3p-asa-read",
]


def _expr(device: str, body: str) -> str:
    return (
        "(function(){var d=ipc.network().getDevice(" + repr(device) + ");"
        "if(!d){return 'E:no-device';}"
        "var cl=null,err='';"
        "try{cl=d.getConsoleLine();}catch(e){err=String(e);}"
        "if(!cl){return 'E:no-console '+err;}"
        + body + "})()"
    )


def send(adapter: BridgeAdapter, expr: str, wait: float = 15.0) -> str:
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def output(adapter: BridgeAdapter) -> str:
    return send(adapter, _expr(DEV, "return String(cl.getOutput());"))


def cli(adapter: BridgeAdapter, command: str, timeout: float = 30.0) -> str:
    """Type one ASA command and drain its output, including the pager."""
    before = len(output(adapter))
    send(adapter, _expr(DEV, f"cl.enterCommand({command!r}); return 'typed';"))
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        time.sleep(1.5)
        text = output(adapter)
        last = text
        tail = text[-400:]
        if "--More--" in tail:
            send(adapter, _expr(DEV, "cl.enterCommand(' '); return 'page';"))
            continue
        prompt = send(adapter, _expr(DEV, "return JSON.stringify(String(cl.getPrompt()||''));"))
        if len(text) > before and (prompt.strip('"').endswith(('>', '#')) or text.rstrip().endswith(('>', '#'))):
            break
    return last


def wait_boot(adapter: BridgeAdapter, timeout: float = 120.0) -> str:
    """An ASA created from scratch boots ROMMON -> ASA OS for ~40-60 s.

    Pushing into that window is silently dropped, so wait until the console
    buffer ends at a usable prompt. A timeout is reported, never assumed.
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        text = output(adapter)
        last = text
        tail = text.rstrip()
        if tail.endswith((">", "#")) and "Boot in" not in tail[-400:]:
            return tail[-200:]
        time.sleep(2.0)
    return f"TIMEOUT: console never reached a prompt; tail={last[-200:]!r}"


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    info = adapter.ping(timeout=5)
    if not info.reachable:
        print("PT UNREACHABLE:", info.detail)
        return 4
    try:
        adapter.delete_device(DEV)
    except Exception:
        pass
    created = adapter.create_device(
        DEV, "firewall", model_hint="5506-X", position=(500, 200)
    )
    print("create:", created.result.value, created.error)
    booted = wait_boot(adapter)
    print("boot:", booted)
    time.sleep(3)
    receipt = adapter.configure_ios(DEV, PUSH)
    print("push:", receipt.result.value, receipt.error)
    time.sleep(10)
    # ASA consoles can sit in user mode after a configureIosDevice push.
    cli(adapter, "enable", timeout=8)
    commands = [
        "show interface ip brief",
        "show nameif",
        "show route",
        "show nat",
        "show dhcpd",
        "show access-list",
        "show running-config",
    ]
    lines = [f"# ASA read-back @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"# device: {DEV}",
             f"# push: {receipt.result.value}", ""]
    for command in commands:
        print("read", command, flush=True)
        text = cli(adapter, command)
        lines.extend([f"## {command}", text, ""])
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "asa-read.txt").write_text("\n".join(lines), encoding="utf-8")
    try:
        print("delete:", adapter.delete_device(DEV).result.value)
    except Exception as exc:
        print("delete error:", exc)
    print("wrote", RAW / "asa-read.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
