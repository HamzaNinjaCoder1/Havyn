#!/usr/bin/env python
"""Dump the live Script-Engine API surface for the Stage-3 device families.

Creates one namespaced device per family, lists the instance prototype
methods, and tries the candidate calls that the Unverified tracks need
(wireless SSID/security, cloud port mapping, server services, host apps).
The output is verbatim JSON under ``probes/raw/S3.api-explore.txt``.

    python probes/stage3_api_explore.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.api-explore.txt"

FAMILIES = [
    ("S3.api-pc", "pc", "PC-PT"),
    ("S3.api-server", "server", "Server-PT"),
    ("S3.api-ap", "access_point", "AccessPoint-PT"),
    ("S3.api-wlc", "wlc", "WLC-PT"),
    ("S3.api-cloud", "cloud", "Cloud-PT"),
    ("S3.api-modem", "modem", "Cable-Modem-PT"),
    ("S3.api-printer", "printer", "Printer-PT"),
    ("S3.api-phone", "ip_phone", "7960"),
    ("S3.api-tablet", "tablet", "TabletPC-PT"),
    ("S3.api-smartphone", "smartphone", "SMARTPHONE-PT"),
]


def send(adapter: BridgeAdapter, expr: str, wait: float = 20.0) -> str:
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def dump_expr(device: str) -> str:
    """Everything observable about one device, defensively."""
    return (
        "(function(){"
        "var out={};"
        "function safe(k,fn){try{out[k]=fn();}catch(e){out[k]='E:'+String(e);}}"
        "var ip=ipc.network();"
        "safe('ipc_keys',function(){return Object.getOwnPropertyNames(ipc);});"
        "safe('network_keys',function(){return Object.getOwnPropertyNames(ip);});"
        "safe('network_proto',function(){return Object.getOwnPropertyNames("
        "Object.getPrototypeOf(ip));});"
        "var d=ip.getDevice(" + repr(device) + ");"
        "if(!d){out.error='E:no-device';return JSON.stringify(out);}"
        "safe('model',function(){return String(d.getModel());});"
        "safe('name',function(){return String(d.getName());});"
        "safe('instance_keys',function(){return Object.getOwnPropertyNames(d);});"
        "safe('proto_keys',function(){return Object.getOwnPropertyNames("
        "Object.getPrototypeOf(d));});"
        "safe('ports',function(){return d.getPorts();});"
        "safe('port_objects',function(){"
        "var ps=d.getPorts()||[];var o={};"
        "for(var i=0;i<ps.length;i++){"
        "var p=ps[i];var po=null;"
        "try{po=d.getPort(p);}catch(e){o[String(p)]='E:'+String(e);continue;}"
        "o[String(p)]=po?Object.getOwnPropertyNames(Object.getPrototypeOf(po))"
        ":'null';"
        "}return o;});"
        "safe('commandLine',function(){return d.getCommandLine()?'yes':'null';});"
        "safe('consoleLine',function(){return d.getConsoleLine()?'yes':'null';});"
        "safe('processes',function(){"
        "var names=['DNS','DHCP','HTTP','HTTPS','EMAIL','FTP','AAA','NTP',"
        "'SYSLOG','IoT','IoE','Wireless','WLAN','WLC','SNMP','TFTP'];"
        "var r={};for(var i=0;i<names.length;i++){"
        "try{r[names[i]]=String(d.getProcess(names[i]));}"
        "catch(e){r[names[i]]='E:'+String(e);}}return r;});"
        "safe('extra_calls',function(){"
        "var names=['getServices','getService','getConfig','setConfig',"
        "'getWirelessConfig','setWirelessConfig','getSsid','setSsid',"
        "'getSecurity','setSecurity','getChannel','setChannel',"
        "'addPortConnection','setDslConnection','getPortConnections',"
        "'getLinkCount','getDeviceInfo'];var r={};"
        "for(var i=0;i<names.length;i++){"
        "r[names[i]]=typeof d[names[i]];}return r;});"
        "return JSON.stringify(out);"
        "})()"
    )


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    lines = [
        f"# Stage 3 API exploration @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    created: list[str] = []
    try:
        for name, role, model in FAMILIES:
            receipt = adapter.create_device(name, role, model_hint=model)
            print("create", name, receipt.result.value, receipt.error, flush=True)
            if receipt.result.value != "success":
                lines.extend([f"## {name} ({model})", f"CREATE FAILED: {receipt.error}", ""])
                continue
            created.append(name)
            time.sleep(2.0)
            text = send(adapter, dump_expr(name))
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
