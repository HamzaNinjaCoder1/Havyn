#!/usr/bin/env python
"""FTP and AAA server configuration surface (S3.D6, S3.D7).

Deliberately separate from the email probe: `EmailServer.updateAllAccounts`
blocks the Script Engine and closes PT (measured twice). FTP/AAA are queried
with short waits so a refusal/timeout cannot take the channel with it.

    python probes/stage3_server_ftp_aaa.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.server-ftp-aaa.txt"
SRV, RTR = "s3p-ftp-srv", "s3p-ftp-rtr"


def send(adapter: BridgeAdapter, device: str, code: str, wait: float = 12.0) -> str:
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


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    for name in (SRV, RTR):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# FTP/AAA probe @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (SRV, "server", "Server-PT", (350, 250)),
            (RTR, "router", "2911", (650, 250)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
        time.sleep(3)
        ftp = js(adapter, SRV,
                 "var f=d.getProcess('FtpServer');f.setEnabled(true);"
                 "var m=f.getFtpUserAccountManager();"
                 "m.addFtpUser('ftpuser','ftppass','rw');"
                 "var users=[];for(var i=0;i<m.getUsersCount();i++){"
                 "users.push({name:m.getUsernameAt(i),perm:m.getPermissionAt(i)});}"
                 "return JSON.stringify({enabled:f.isEnabled(),users:users});")
        print("ftp", ftp)
        aaa = js(adapter, SRV,
                 "var a=d.getProcess('AcsServer');"
                 "var u=a.addToUserMap('admin','radiuspass','');"
                 "var c=a.addToClientMap('10.89.0.1',0,'sharedsecret','');"
                 "a.enableACSServerService(true);"
                 "return JSON.stringify({userAdded:u,clientAdded:c,enabled:a.isEnabled(),"
                 "users:String(a.getUserRecordsAsString()),"
                 "clients:String(a.getClientRecordsAsString())});")
        print("aaa", aaa)
        router_processes = js(adapter, RTR,
                              "var out={};var names=['Aaa','AcsServer','RadiusClient',"
                              "'RadiusServer','TacacsClient','TacacsServer'];"
                              "for(var i=0;i<names.length;i++){try{var p=d.getProcess(names[i]);"
                              "out[names[i]]=p?Object.getOwnPropertyNames("
                              "Object.getPrototypeOf(p)):null;}catch(e){out[names[i]]='E:'+e;}}"
                              "return JSON.stringify(out);")
        print("router processes", router_processes)
        client_surface = js(adapter, SRV,
                            "var c=d.getProcess('FtpClient');"
                            "return JSON.stringify({present:!!c,methods:c?Object.getOwnPropertyNames("
                            "Object.getPrototypeOf(c)):[]});")
        print("ftp client surface", client_surface)
        lines.extend([
            "## ftp server", json.dumps(ftp, indent=1),
            "## aaa server", json.dumps(aaa, indent=1),
            "## router aaa processes", json.dumps(router_processes, indent=1),
            "## ftp client surface", json.dumps(client_surface, indent=1),
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
