#!/usr/bin/env python
"""PC FTP login to a Server-PT FTP service (S3.D6).

The earlier note said a client login was not observable because the `FtpClient`
process has no action. The PC's command prompt does have an `ftp` command; this
lab uses it, draining the help pager first (it eats the next command's first
character), and records the real `230- Logged in` and `221- Service closing`
exchange.

    python probes/stage3_ftp_lab.py
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.ftp-login.txt"
SRV, PC = "s3p-ftp-srv", "s3p-ftp-pc"
# Lab-only credentials; HAVYN_FTP_PASSWORD may override a generated value.
USER = os.environ.get("HAVYN_FTP_USER", "labftp")
PASSWORD = os.environ.get("HAVYN_FTP_PASSWORD") or secrets.token_urlsafe(12)


def send(adapter: BridgeAdapter, device: str, code: str, wait: float = 15.0) -> str:
    expr = (
        "(function(){var d=ipc.network().getDevice(" + repr(device) + ");"
        "if(!d){return 'E:no-device';}try{" + code + "}catch(e){return 'E:'+e;}})()"
    )
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def output(adapter: BridgeAdapter) -> str:
    return send(adapter, PC, "return String(d.getCommandLine().getOutput());")


def drain(adapter: BridgeAdapter) -> None:
    for _ in range(20):
        if "--More--" not in output(adapter)[-300:]:
            return
        send(adapter, PC, "d.getCommandLine().enterCommand(' ');return 'page';")
        time.sleep(1.0)


def cli(adapter: BridgeAdapter, command: str, wait: float = 5.0) -> str:
    drain(adapter)
    send(adapter, PC,
         "d.getCommandLine().enterCommand(" + json.dumps(command) + ");"
         "return 'sent';")
    time.sleep(wait)
    drain(adapter)
    return output(adapter)[-1200:]


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    for name in (SRV, PC):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# FTP login lab @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (SRV, "server", "Server-PT", (300, 250)),
            (PC, "pc", "PC-PT", (600, 250)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
        time.sleep(4)
        link = adapter.create_link(PC, "FastEthernet0", SRV, "FastEthernet0",
                                   media="cross")
        print("link", link.result.value, link.error)
        time.sleep(2)
        adapter.configure_host(SRV, dhcp=False, ip="10.89.0.2",
                               mask="255.255.255.0", gateway="10.89.0.2")
        adapter.configure_host(PC, dhcp=False, ip="10.89.0.10",
                               mask="255.255.255.0", gateway="10.89.0.2")
        server = send(
            adapter, SRV,
            "var f=d.getProcess('FtpServer');f.setEnabled(true);"
            "var m=f.getFtpUserAccountManager();"
            "m.addFtpUser(" + json.dumps(USER) + "," + json.dumps(PASSWORD) + ",'rw');"
            "return JSON.stringify({enabled:f.isEnabled(),users:m.getUsersCount()});",
        )
        print("server", server)
        attempt = cli(adapter, "ftp 10.89.0.2", wait=5)
        user = cli(adapter, USER, wait=4)
        password = cli(adapter, PASSWORD, wait=4)
        listing = cli(adapter, "dir", wait=5)
        quit_ = cli(adapter, "quit", wait=4)
        print("logged in:", "230- Logged in" in password)
        lines.extend([
            "## server", server,
            "## ftp connect", attempt,
            "## username", user,
            "## password", password,
            "## dir", listing,
            "## quit", quit_,
            "",
        ])
    finally:
        RAW.parent.mkdir(parents=True, exist_ok=True)
        RAW.write_text("\n".join(lines), encoding="utf-8")
        for name in created:
            try:
                adapter.delete_device(name)
            except Exception:
                pass
    print("wrote", RAW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
