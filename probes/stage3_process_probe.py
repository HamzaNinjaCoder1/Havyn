#!/usr/bin/env python
"""Enumerate the real PT process objects behind the Stage-3 capabilities.

The PT IpcAPI reference lists the valid `Device.getProcess()` names
(`DnsServer`, `DhcpServer`, `HttpServer`, `NtpServer`, `SyslogServer`,
`WirelessServer`, `WirelessClient`, `WLCServer`, `IoeServer`, ...). The earlier
Stage-3 probes passed family words (`DNS`, `DHCP`, `Wireless`) and concluded
"no API". This probe asks for the documented names and dumps each returned
object's methods, so the map can be corrected from evidence rather than from
the earlier guess.

    python probes/stage3_process_probe.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.processes.txt"

PROCESSES = {
    "server": [
        "DnsServer", "DhcpServer", "HttpServer", "HttpsServer", "EmailServer",
        "SmtpServer", "Pop3Server", "FtpServer", "TftpServer", "SyslogServer",
        "NtpServer", "SnmpAgent", "Aaa", "AcsServer", "IoeServer", "HostIp",
        "WirelessServer",
    ],
    "access_point": [
        "WirelessServer", "WirelessCommon", "AccessPointSwitcher", "WPA", "WEP",
        "WLCServer", "CapwapAC", "LWAccessPointSwitcher",
    ],
    "laptop": [
        "WirelessClient", "WirelessCommon", "WPA", "WEP", "HostIp", "DnsClient",
    ],
    "wlc": [
        "WLCServer", "WLC2504Server", "WLC2504HttpsServer", "CapwapAC",
        "WirelessServerManager", "WirelessServer",
    ],
    "router": [
        "DnsServer", "DhcpServer", "HttpServer", "NtpServer", "NtpClient",
        "SyslogServer", "SyslogClient", "SshServer", "SnmpAgent", "Aaa",
        "Gre", "TunnelInterface", "Ipsec", "Ike", "EasyVpnClient", "HostIp",
    ],
    "iot": ["IoeServer", "Ioe", "IoeUserManager", "IoEComponent"],
    "pc": [
        "HostIp", "DnsClient", "HttpClient", "EmailClient", "FtpClient",
        "SshClient", "NtpClient",
    ],
}

FAMILIES = [
    ("s3p-prc-server", "server", "Server-PT"),
    ("s3p-prc-ap", "access_point", "AccessPoint-PT"),
    ("s3p-prc-laptop", "laptop", "Laptop-PT"),
    ("s3p-prc-wlc", "wlc", "WLC-PT"),
    ("s3p-prc-router", "router", "2911"),
    ("s3p-prc-pc", "pc", "PC-PT"),
]


def send(adapter: BridgeAdapter, expr: str, wait: float = 25.0) -> str:
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def probe_expr(device: str, names: list[str]) -> str:
    names_js = json.dumps(names)
    return (
        "(function(){var d=ipc.network().getDevice(" + repr(device) + ");"
        "if(!d){return 'E:no-device';}var names=" + names_js + ";var out={};"
        "for(var i=0;i<names.length;i++){var n=names[i];"
        "try{var p=d.getProcess(n);"
        "if(!p){out[n]={present:false};continue;}"
        "var keys=Object.getOwnPropertyNames(Object.getPrototypeOf(p));"
        "out[n]={present:true,keys:keys};"
        "}catch(e){out[n]={present:false,error:String(e)};}}"
        "return JSON.stringify(out);})()"
    )


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    lines = [
        f"# Stage 3 process probe @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    created: list[str] = []
    try:
        for name, role, model in FAMILIES:
            try:
                adapter.delete_device(name)
            except Exception:
                pass
            receipt = adapter.create_device(name, role, model_hint=model)
            print("create", name, receipt.result.value, receipt.error, flush=True)
            if receipt.result.value != "success":
                continue
            created.append(name)
            if role == "laptop":
                radio = adapter.ensure_wireless_radio(
                    name, module="PT-LAPTOP-NM-1W", slot="0",
                    interface="Wireless0",
                )
                print("  radio", radio)
            time.sleep(3.0)
            text = send(adapter, probe_expr(name, PROCESSES[role]))
            lines.extend([f"## {name} ({model})", text, ""])
    finally:
        for name in created:
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
