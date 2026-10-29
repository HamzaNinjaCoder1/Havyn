#!/usr/bin/env python
"""ASA configuration read-back for S3.C2-C6.

The earlier probes could not read the ASA because `enable` asks for a password
the engine did not have. The IpcAPI exposes `Asa::enterCommand(command, mode)`,
which runs a command in the named mode and returns its output. This probe
pushes the C2-C6 configuration and reads every `show` through that door, so the
verdict rests on the ASA's own output instead of a push receipt.

    python probes/stage3_asa_readback.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "asa-readback.txt"
DEV = "s3p-asa-rb"

PUSH = [
    "hostname s3p-asa-rb",
    "interface GigabitEthernet1/1", " nameif inside", " security-level 100",
    " ip address 10.99.0.1 255.255.255.0", " no shutdown", " exit",
    "interface GigabitEthernet1/2", " nameif outside", " security-level 0",
    " ip address 10.99.1.1 255.255.255.0", " no shutdown", " exit",
    "interface GigabitEthernet1/3", " nameif dmz", " security-level 50",
    " ip address 10.99.2.1 255.255.255.0", " no shutdown", " exit",
    "object network INSIDE-NET", " subnet 10.99.0.0 255.255.255.0",
    " nat (inside,outside) dynamic interface", " exit",
    "object-group network WEB-SERVERS", " network-object host 10.99.2.10",
    " exit",
    "access-list OUTSIDE-IN extended permit icmp any object-group WEB-SERVERS",
    "access-list OUTSIDE-IN extended deny ip any any",
    "access-group OUTSIDE-IN in interface outside",
    "dhcpd address 10.99.0.100-10.99.0.150 inside",
    "dhcpd enable inside",
    "route outside 0.0.0.0 0.0.0.0 10.99.1.254 1",
]

SHOWS = [
    "show running-config",
    "show interface ip brief",
    "show nameif",
    "show route",
    "show nat",
    "show dhcpd",
    "show object-group",
    "show access-list",
]


def send(adapter: BridgeAdapter, code: str, wait: float = 30.0) -> str:
    expr = (
        "(function(){var d=ipc.network().getDevice(" + repr(DEV) + ");"
        "if(!d){return 'E:no-device';}try{" + code + "}catch(e){return 'E:'+e;}})()"
    )
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def wait_boot(adapter: BridgeAdapter, timeout: float = 150.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = send(
            adapter,
            "var cl=d.getConsoleLine();return String(d.isBooting())+'|'"
            "+String(cl.getOutput()).slice(-80);",
        )
        if last.startswith("false"):
            return last
        time.sleep(3.0)
    return f"TIMEOUT:{last}"


def run_show(adapter: BridgeAdapter, command: str) -> tuple[str, str]:
    text = send(
        adapter,
        "var r=d.enterCommand(" + json.dumps(command) + ",'enable');"
        "return JSON.stringify({status:String(r.first),out:String(r.second)});",
    )
    try:
        data = json.loads(text)
        return data.get("status", ""), data.get("out", "")
    except ValueError:
        return "UNPARSED", text


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    try:
        adapter.delete_device(DEV)
    except Exception:
        pass
    receipt = adapter.create_device(DEV, "firewall", model_hint="5506-X",
                                    position=(500, 220))
    print("create:", receipt.result.value, receipt.error)
    if receipt.result.value != "success":
        return 2
    try:
        print("boot:", wait_boot(adapter)[:120])
        push = adapter.configure_ios(DEV, PUSH)
        print("push:", push.result.value, push.error)
        time.sleep(10)
        lines = [
            f"# ASA read-back @ "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"# device: {DEV}",
            f"# push: {push.result.value}",
            "",
        ]
        for command in SHOWS:
            print("read", command, flush=True)
            status, out = run_show(adapter, command)
            lines.extend([f"## {command}", f"# status: {status}", out, ""])
        RAW.parent.mkdir(parents=True, exist_ok=True)
        RAW.write_text("\n".join(lines), encoding="utf-8")
        print("wrote", RAW)
    finally:
        try:
            print("delete:", adapter.delete_device(DEV).result.value)
        except Exception as exc:
            print("delete error:", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
