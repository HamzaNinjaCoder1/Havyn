#!/usr/bin/env python
"""SSH login from a PC to a router (S3.A15).

The earlier A15 probe could only read the router-side configuration back; the
PC SSH client was not exercised. This lab completes it:

1. router: domain name, local user, `login local`, `transport input ssh`,
   `ip ssh version 2`, then `crypto key generate rsa` in global config with the
   modulus prompt answered (PT rejects the command in exec mode);
2. `show ip ssh` must report enabled version 2 and `show crypto key` the key;
3. PC: `ssh -l <lab user> <router>` must reach `Password:`, accept the password,
   and land on the router prompt; `show users`/`show ssh` on the router must
   show the session.

    python probes/stage3_ssh_lab.py
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.ssh-login.txt"
RTR, PC = "s3p-ssh6-rtr", "s3p-ssh6-pc"
# Lab-only credentials: never commit a real one. A run generates a random
# password unless HAVYN_LAB_PASSWORD is set.
USER = os.environ.get("HAVYN_LAB_USER", "labuser")
PASSWORD = os.environ.get("HAVYN_LAB_PASSWORD") or secrets.token_urlsafe(12)


def send(adapter: BridgeAdapter, device: str, code: str, wait: float = 20.0) -> str:
    expr = (
        "(function(){var d=ipc.network().getDevice(" + repr(device) + ");"
        "if(!d){return 'E:no-device';}try{" + code + "}catch(e){return 'E:'+e;}})()"
    )
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def console(adapter: BridgeAdapter, device: str) -> str:
    return send(adapter, device,
                "return String(d.getCommandLine().getOutput());")


def cli(adapter: BridgeAdapter, device: str, command: str,
        wait: float = 4.0) -> str:
    send(adapter, device,
         "d.getCommandLine().enterCommand(" + json.dumps(command) + ");"
         "return 'sent';")
    time.sleep(wait)
    return console(adapter, device)


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    for name in (RTR, PC):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# SSH login lab @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (RTR, "router", "2911", (300, 250)),
            (PC, "pc", "PC-PT", (600, 250)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
        time.sleep(4)
        link = adapter.create_link(PC, "FastEthernet0", RTR, "GigabitEthernet0/0",
                                   media="cross")
        print("link", link.result.value, link.error)
        time.sleep(2)
        cfg = adapter.configure_ios(RTR, [
            "hostname s3p-ssh6-rtr",
            "interface GigabitEthernet0/0",
            " ip address 10.66.0.1 255.255.255.0",
            " no shutdown",
            " exit",
            "ip domain-name lab.local",
            f"username {USER} privilege 15 secret {PASSWORD}",
            "line vty 0 4",
            " login local",
            " transport input ssh",
            " exit",
            "ip ssh version 2",
        ])
        print("router cfg", cfg.result.value, cfg.error)
        adapter.configure_host(PC, dhcp=False, ip="10.66.0.10",
                               mask="255.255.255.0", gateway="10.66.0.1")
        time.sleep(3)
        # RSA key: global config, then answer the modulus prompt.
        send(adapter, RTR,
             "var cl=d.getCommandLine();var p=String(cl.getPrompt()||'');"
             "if(p.indexOf('[yes/no]')>=0){cl.enterCommand('no');}"
             "cl.enterCommand('');cl.enterCommand('enable');"
             "cl.enterCommand('configure terminal');return 'ready';")
        time.sleep(2)
        key = cli(adapter, RTR, "crypto key generate rsa", wait=3)
        prompt = send(adapter, RTR,
                      "return JSON.stringify(String(d.getCommandLine().getPrompt()));")
        bits = cli(adapter, RTR, "1024", wait=10)
        send(adapter, RTR, "d.getCommandLine().enterCommand('end');return 'end';")
        time.sleep(2)
        ip_ssh = adapter._console_show_full(RTR, "show ip ssh", timeout=25)
        crypto = adapter._console_show_full(RTR, "show crypto key mypubkey rsa",
                                            timeout=25)
        print("ip ssh:", str(ip_ssh.value)[-200:] if ip_ssh.is_known else ip_ssh.detail)
        # SSH from the PC, including the password exchange.
        ping = cli(adapter, PC, "ping 10.66.0.1", wait=8)
        attempt = cli(adapter, PC, f"ssh -l {USER} 10.66.0.1", wait=8)
        pw = cli(adapter, PC, PASSWORD, wait=10)
        users = adapter._console_show_full(RTR, "show users", timeout=25)
        ssh = adapter._console_show_full(RTR, "show ssh", timeout=25)
        lines.extend([
            "## router config", cfg.result.value,
            "## key prompt", prompt,
            "## key output", bits[-1200:],
            "## show ip ssh",
            str(ip_ssh.value) if ip_ssh.is_known else str(ip_ssh.detail),
            "## show crypto key mypubkey rsa",
            str(crypto.value) if crypto.is_known else str(crypto.detail),
            "## pc ping", ping[-800:],
            "## pc ssh", attempt[-800:],
            "## pc password", pw[-1200:],
            "## router show users",
            str(users.value) if users.is_known else str(users.detail),
            "## router show ssh",
            str(ssh.value) if ssh.is_known else str(ssh.detail),
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
