#!/usr/bin/env python
"""AAA-backed SSH login probe (S3.D7), RADIUS and TACACS+.

Server-PT's `AcsServer` can hold users and clients; the router has
`RadiusClient`/`TacacsClient`. This lab points a router's vty login at the ACS
server for **both** protocols, generates the RSA key, and tries a real SSH
login with the ACS credential. The record shows the live result; a timeout is
the refusal evidence, not a claim of support.

IpcAPI: `EAcsServerType`: 0 = TACACS, 1 = RADIUS.

    python probes/stage3_aaa_lab.py
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.aaa-login.txt"

# Lab-only credentials: generated per run unless explicitly supplied.
LAB_USER = os.environ.get("HAVYN_LAB_USER", "labuser")
LAB_PASSWORD = os.environ.get("HAVYN_LAB_PASSWORD") or secrets.token_urlsafe(12)
AAA_KEY = os.environ.get("HAVYN_AAA_KEY") or secrets.token_urlsafe(12)

PROTOCOLS = [
    # (label, EAcsServerType, router server line, aaa group)
    ("radius", 1, f"radius-server host 10.90.0.2 auth-port 1812 "
     f"acct-port 1813 key {AAA_KEY}", "group radius"),
    ("tacacs", 0, f"tacacs-server host 10.90.0.2 key {AAA_KEY}",
     "group tacacs+"),
]


def send(adapter: BridgeAdapter, device: str, code: str, wait: float = 20.0) -> str:
    expr = (
        "(function(){var d=ipc.network().getDevice(" + repr(device) + ");"
        "if(!d){return 'E:no-device';}try{" + code + "}catch(e){return 'E:'+e;}})()"
    )
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def js(adapter: BridgeAdapter, device: str, code: str) -> object:
    text = send(adapter, device, code)
    try:
        return json.loads(text)
    except ValueError:
        return text


def run_protocol(adapter: BridgeAdapter, label: str, acs_type: int,
                 server_line: str, aaa_group: str) -> list[str]:
    srv = f"s3p-aaa-{label}-srv"
    rtr = f"s3p-aaa-{label}-rtr"
    pc = f"s3p-aaa-{label}-pc"
    created: list[str] = []
    lines = [f"## protocol: {label}", ""]
    try:
        for name, role, model, pos in (
            (srv, "server", "Server-PT", (300, 250)),
            (rtr, "router", "2911", (600, 250)),
            (pc, "pc", "PC-PT", (900, 250)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print(label, "create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return lines + [f"create failed: {receipt.error}", ""]
            created.append(name)
        time.sleep(4)
        for a_dev, a_port, b_dev, b_port, media in (
            (srv, "FastEthernet0", rtr, "GigabitEthernet0/0", "cross"),
            (rtr, "GigabitEthernet0/1", pc, "FastEthernet0", "cross"),
        ):
            receipt = adapter.create_link(a_dev, a_port, b_dev, b_port, media=media)
            print(label, "link", a_dev, b_dev, receipt.result.value, receipt.error)
        time.sleep(3)
        adapter.configure_host(srv, dhcp=False, ip="10.90.0.2",
                               mask="255.255.255.0", gateway="10.90.0.1")
        adapter.configure_host(pc, dhcp=False, ip="10.90.0.10",
                               mask="255.255.255.0", gateway="10.90.0.1")
        acs = js(adapter, srv,
                 "var a=d.getProcess('AcsServer');"
                 "var u=a.addToUserMap(" + json.dumps(LAB_USER) + "," +
                 json.dumps(LAB_PASSWORD) + ",'');"
                 "var c=a.addToClientMap('10.90.0.1'," + str(acs_type) + "," +
                 json.dumps(AAA_KEY) + ",'');"
                 "a.enableACSServerService(true);"
                 "return JSON.stringify({user:u,client:c,enabled:a.isEnabled(),"
                 "users:String(a.getUserRecordsAsString()),"
                 "clients:String(a.getClientRecordsAsString())});")
        print(label, "acs", acs)
        cfg = adapter.configure_ios(rtr, [
            "hostname s3p-aaa-rtr",
            "interface GigabitEthernet0/0",
            " ip address 10.90.0.1 255.255.255.0",
            " no shutdown",
            " exit",
            "interface GigabitEthernet0/1",
            " ip address 10.91.0.1 255.255.255.0",
            " no shutdown",
            " exit",
            "ip domain-name lab.local",
            "aaa new-model",
            server_line,
            f"aaa authentication login default {aaa_group}",
            "line vty 0 4",
            " login authentication default",
            " transport input ssh",
            " exit",
            "ip ssh version 2",
        ])
        print(label, "router cfg", cfg.result.value, cfg.error)
        time.sleep(4)
        key = adapter.generate_rsa_key(rtr, bits=1024)
        print(label, "key", key.result.value, key.error)
        ip_ssh = adapter._console_show_full(rtr, "show ip ssh", timeout=25)

        def pc_cli(command: str, wait: float = 6.0) -> str:
            send(adapter, pc,
                 "d.getCommandLine().enterCommand(" + json.dumps(command) + ");"
                 "return 'sent';")
            time.sleep(wait)
            return send(adapter, pc,
                        "return String(d.getCommandLine().getOutput()).slice(-900);")

        attempt = pc_cli(f"ssh -l {LAB_USER} 10.90.0.1", wait=8)
        password = pc_cli(LAB_PASSWORD, wait=8)
        records = js(adapter, srv,
                     "var a=d.getProcess('AcsServer');"
                     "return JSON.stringify({users:String(a.getUserRecordsAsString()),"
                     "clients:String(a.getClientRecordsAsString()),"
                     "enabled:a.isEnabled()});")
        lines.extend([
            "## acs", json.dumps(acs, indent=1),
            "## router cfg", cfg.result.value,
            "## rsa key", f"{key.result.value} {key.error or ''}",
            "## show ip ssh",
            str(ip_ssh.value) if ip_ssh.is_known else str(ip_ssh.detail),
            "## ssh attempt", attempt[-1200:],
            "## password", password[-1200:],
            "## acs records", json.dumps(records, indent=1),
            "",
        ])
        return lines
    finally:
        for name in created:
            try:
                adapter.delete_device(name)
            except Exception:
                pass


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    lines = [
        f"# AAA login lab @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    for label, acs_type, server_line, aaa_group in PROTOCOLS:
        lines.extend(run_protocol(adapter, label, acs_type, server_line, aaa_group))
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", RAW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
