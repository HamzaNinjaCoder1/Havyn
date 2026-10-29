#!/usr/bin/env python
"""Console probe for the remaining Stage-3 device families.

Asks each family's real console the questions the map still needs:

* Server-PT  - is there a service/CLI path?                     (S3.D0/D1)
* WLC-PT     - can the controller be configured/read at all?    (S3.E4)
* 7960       - is the phone a configurable/addressable endpoint? (S3.H1)
* tablet/smartphone/printer - `ipconfig /all` proof.             (S3.H1)
* PC         - SSH client command surface.                       (S3.A15)

The pager is drained (`--More--` + space) before the next command, because
PT otherwise eats the first character of that command (measured: `help`
arrived as `elp`). Output is verbatim in ``probes/raw/S3.consoles.txt``.

    python probes/stage3_console_probe.py
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.consoles.txt"

JOBS = [
    ("s3p-con-server", "server", "Server-PT", [
        "ipconfig /all", "show version", "show running-config", "help",
    ]),
    ("s3p-con-wlc", "wlc", "WLC-PT", [
        "show version", "show running-config", "show wlan summary",
        "show ap summary", "help",
    ]),
    ("s3p-con-phone", "ip_phone", "7960", [
        "show version", "show running-config", "show ephone", "help",
    ]),
    ("s3p-con-tablet", "tablet", "TabletPC-PT", ["ipconfig /all"]),
    ("s3p-con-smartphone", "smartphone", "SMARTPHONE-PT", ["ipconfig /all"]),
    ("s3p-con-printer", "printer", "Printer-PT", ["ipconfig /all"]),
    ("s3p-con-pc", "pc", "PC-PT", ["help"]),
]


def expr(device: str, body: str) -> str:
    return (
        "(function(){var d=ipc.network().getDevice(" + repr(device) + ");"
        "if(!d){return 'E:no-device';}"
        "var cl=null,err='';"
        "try{cl=d.getCommandLine();}catch(e){err=String(e);}"
        "if(!cl){return 'E:no-commandline '+err;}"
        + body + "})()"
    )


def send_dev(adapter: BridgeAdapter, device: str, body: str, wait: float = 20.0) -> str:
    answer = adapter.send_expression(expr(device, body), wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def output(adapter: BridgeAdapter, device: str) -> str:
    return send_dev(adapter, device, "return String(cl.getOutput());")


def cli(adapter: BridgeAdapter, device: str, command: str,
        timeout: float = 30.0) -> str:
    """Type one command, drain the pager, and return its echoed tail."""
    before = output(adapter, device)
    send_dev(adapter, device, f"cl.enterCommand({command!r}); return 'typed';")
    deadline = time.time() + timeout
    last = ""
    seen = 0
    while time.time() < deadline:
        time.sleep(1.2)
        text = output(adapter, device)
        last = text
        tail = text[-300:]
        if "--More--" in tail:
            send_dev(adapter, device, "cl.enterCommand(' '); return 'page';")
            seen += 1
            if seen > 20:
                break
            continue
        index = text.rfind(command)
        tail = text[index:] if index >= 0 else text[-800:]
        prompt = send_dev(
            adapter, device,
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
    lines = [
        f"# Stage 3 console probe @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    for name, role, model, commands in JOBS:
        try:
            adapter.delete_device(name)
        except Exception:
            pass
        receipt = adapter.create_device(name, role, model_hint=model)
        print("create", name, receipt.result.value, receipt.error, flush=True)
        if receipt.result.value != "success":
            lines.extend([f"## {name} ({model})", f"CREATE FAILED: {receipt.error}", ""])
            continue
        try:
            time.sleep(3.0)
            lines.extend([f"## {name} ({model})", ""])
            for command in commands:
                print("  run", command, flush=True)
                text = cli(adapter, name, command)
                lines.extend([f"### {command}", text, ""])
        finally:
            try:
                adapter.delete_device(name)
            except Exception:
                pass
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", RAW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
