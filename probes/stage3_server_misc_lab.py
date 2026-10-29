#!/usr/bin/env python
"""Server-PT email configuration refusal probe (S3.D5).

WARNING: `EmailServer.addUser` / `updateAllAccounts` blocks the Script Engine
and closes Packet Tracer. This script is retained to reproduce that refusal;
do not run it on a PT session you need. The captured timeout is in
`raw/S3.server-misc.txt`. FTP and AAA have their own probes
(`stage3_ftp_lab.py`, `stage3_aaa_lab.py`).

    python probes/stage3_server_misc_lab.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.server-misc.txt"
SRV = "s3p-misc-srv"


def send(adapter: BridgeAdapter, device: str, code: str, wait: float = 25.0) -> str:
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
    try:
        adapter.delete_device(SRV)
    except Exception:
        pass
    receipt = adapter.create_device(SRV, "server", model_hint="Server-PT",
                                    position=(400, 250))
    print("create", receipt.result.value, receipt.error)
    if receipt.result.value != "success":
        return 2
    created = [SRV]
    lines = [
        f"# Server misc services @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        time.sleep(3)
        email = js(adapter, SRV,
                   "var e=d.getProcess('EmailServer');"
                   "var a=e.addUser('alice','pass');var b=e.addUser('bob','pass');"
                   "e.updateAllAccounts('lab.local');"
                   "var s=d.getProcess('SmtpServer');s.setEnable(true);"
                   "s.setServerDomainName('lab.local');"
                   "var p=d.getProcess('Pop3Server');p.setEnable(true);"
                   "return JSON.stringify({alice:a,bob:b,"
                   "accounts:String(e.getAllEmailAcctAsStrings()),"
                   "smtpEnabled:s.isEnabled(),domain:String(s.getServerDomainName()),"
                   "pop3Enabled:p.isEnabled()});")
        ftp = js(adapter, SRV,
                 "var f=d.getProcess('FtpServer');f.setEnabled(true);"
                 "var m=f.getFtpUserAccountManager();"
                 "m.addFtpUser('ftpuser','ftppass','rw');"
                 "var users=[];for(var i=0;i<m.getUsersCount();i++){"
                 "users.push({name:m.getUsernameAt(i),perm:m.getPermissionAt(i)});}"
                 "return JSON.stringify({enabled:f.isEnabled(),users:users});")
        aaa = js(adapter, SRV,
                 "var a=d.getProcess('AcsServer');"
                 "var u=a.addToUserMap('admin','radiuspass','');"
                 "var c=a.addToClientMap('10.89.0.1',0,'sharedsecret','');"
                 "a.enableACSServerService(true);"
                 "return JSON.stringify({userAdded:u,clientAdded:c,enabled:a.isEnabled(),"
                 "users:String(a.getUserRecordsAsString()),"
                 "clients:String(a.getClientRecordsAsString())});")
        iot = js(adapter, SRV,
                 "var s=d.getProcess('IoeServer');"
                 "return JSON.stringify({present:!!s,"
                 "methods:s?Object.getOwnPropertyNames(Object.getPrototypeOf(s)):[]});")
        print("email", email)
        print("ftp", ftp)
        print("aaa", aaa)
        print("iot", iot)
        lines.extend([
            "## email", json.dumps(email, indent=1),
            "## ftp", json.dumps(ftp, indent=1),
            "## aaa (ACS server)", json.dumps(aaa, indent=1),
            "## iot server", json.dumps(iot, indent=1),
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
