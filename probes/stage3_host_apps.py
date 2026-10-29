#!/usr/bin/env python
"""Host application probe (S3.A15, S3.D1, S3.H2).

`getCommandLine()` is documented in this repo as working on hosts as well as
routers.  This probe asks a real PC console three questions the Stage 3 map
still marks Unverified:

* does `ipconfig /all` show the DNS server the spec configured?   (S3.H2)
* does `ping <name>` resolve through DNS?                          (S3.D1)
* does the PC console accept an SSH login command?                 (S3.A15)

Nothing is inferred: the raw console tail is written to
``probes/raw/S3.host-apps.txt`` and only what is visible there is reported.

    python probes/stage3_host_apps.py
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.host-apps.txt"
DEV = "s3p-host-pc"


def expr(body: str) -> str:
    return (
        "(function(){var d=ipc.network().getDevice(" + repr(DEV) + ");"
        "if(!d){return 'E:no-device';}"
        "var cl=null,err='';"
        "try{cl=d.getCommandLine();}catch(e){err=String(e);}"
        "if(!cl){return 'E:no-console '+err;}"
        + body + "})()"
    )


def send(adapter: BridgeAdapter, body: str, wait: float = 15.0) -> str:
    answer = adapter.send_expression(expr(body), wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def output(adapter: BridgeAdapter) -> str:
    return send(adapter, "return String(cl.getOutput());")


def cli(adapter: BridgeAdapter, command: str, timeout: float = 25.0) -> str:
    """Type one PC command and return its tail, anchored on PT's echo."""
    before = output(adapter)
    send(adapter, f"cl.enterCommand({command!r}); return 'typed';")
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        time.sleep(1.5)
        text = output(adapter)
        last = text
        index = text.rfind(command)
        tail = text[index:] if index >= 0 else text[-600:]
        prompt = send(
            adapter,
            "return JSON.stringify(String(cl.getPrompt()||''));",
        ).strip('"')
        if index >= 0 and len(text) >= len(before) and (
            prompt.endswith((">", "#")) or tail.rstrip().endswith((">", "#"))
        ):
            return tail
    return last


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    try:
        adapter.delete_device(DEV)
    except Exception:
        pass
    receipt = adapter.create_device(DEV, "pc", position=(300, 300))
    print("create:", receipt.result.value, receipt.error)
    time.sleep(3)
    configured = adapter.configure_host(
        DEV, dhcp=False, ip="10.99.0.10", mask="255.255.255.0",
        gateway="10.99.0.1", dns="10.99.0.53",
    )
    print("configure:", configured.result.value, configured.error)
    time.sleep(4)
    lines = [
        f"# host application probe @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"# device: {DEV}",
        f"# configure: {configured.result.value}",
        "",
    ]
    commands = [
        "ipconfig /all",
        "help",
        "ping s3p-server.local",
        "ssh -l admin 10.99.0.1",
    ]
    for command in commands:
        print("run", command, flush=True)
        tail = cli(adapter, command)
        lines.extend([f"## {command}", tail, ""])
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text("\n".join(lines), encoding="utf-8")
    try:
        print("delete:", adapter.delete_device(DEV).result.value)
    except Exception as exc:
        print("delete error:", exc)
    print("wrote", RAW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
