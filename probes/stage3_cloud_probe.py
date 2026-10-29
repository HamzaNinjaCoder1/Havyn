#!/usr/bin/env python
"""Cloud-PT mapping probe (S3.F2, S3.F4).

The cloud's prototype exposes `addPortConnection`, `setDslConnection`,
`addSubLinkConnection` and the serial sub-link API. This probe records their
arity/source (PT functions are not always native, so the source can name the
arguments), then exercises a real two-customer mapping and reads it back.

    python probes/stage3_cloud_probe.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.cloud.txt"
CLOUD = "s3p-cloud"


def send(adapter: BridgeAdapter, expr: str, wait: float = 25.0) -> str:
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def cloud_expr(body: str) -> str:
    return (
        "(function(){var d=ipc.network().getDevice(" + repr(CLOUD) + ");"
        "if(!d){return 'E:no-device';}" + body + "})()"
    )


def cloud_send(adapter: BridgeAdapter, body: str, wait: float = 25.0) -> str:
    return send(adapter, cloud_expr(body), wait=wait)


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    try:
        adapter.delete_device(CLOUD)
    except Exception:
        pass
    receipt = adapter.create_device(CLOUD, "cloud", model_hint="Cloud-PT",
                                    position=(450, 250))
    print("create:", receipt.result.value, receipt.error)
    if receipt.result.value != "success":
        return 2
    time.sleep(4)
    lines = [
        f"# Stage 3 cloud probe @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"# device: {CLOUD}",
        "",
    ]
    signature = (
        "(function(){var d=ipc.network().getDevice(" + repr(CLOUD) + ");"
        "var out={};var names=['addPortConnection','setDslConnection',"
        "'addSubLinkConnection','removePortConnection',"
        "'removeAllPortConnection','getPortStrConnections',"
        "'getPortConnectionCount','isDslConnection'];"
        "for(var i=0;i<names.length;i++){var n=names[i];var f=d[n];"
        "out[n]={typeof:typeof f,length:(typeof f==='function'?f.length:null),"
        "src:(typeof f==='function'?String(f).slice(0,600):null)};}"
        "return JSON.stringify(out);})()"
    )
    lines.extend(["## signatures", cloud_send(adapter, signature), ""])
    before = cloud_send(
        adapter,
        "var o={};var ps=ipc.network().getDevice(" + repr(CLOUD) + ").getPorts();"
        "for(var i=0;i<ps.length;i++){"
        "try{o[ps[i]]={conns:String(d.getPortStrConnections(ps[i])),"
        "count:String(d.getPortConnectionCount(ps[i])),"
        "dsl:String(d.isDslConnection(ps[i]))};"
        "}catch(e){o[ps[i]]='E:'+e;}}return JSON.stringify(o);",
    )
    lines.extend(["## before", before, ""])
    attempts = [
        ("addPortConnection('Serial0','Serial1')",
         "d.addPortConnection('Serial0','Serial1');return 'ok';"),
        ("addPortConnection('Serial0','Serial0')",
         "d.addPortConnection('Serial0','Serial0');return 'ok';"),
        ("setDslConnection('Modem4','Modem5')",
         "d.setDslConnection('Modem4','Modem5');return 'ok';"),
    ]
    for label, body in attempts:
        print("try", label, flush=True)
        answer = cloud_send(adapter, "try{" + body + "}catch(e){return 'E:'+e;}")
        after = cloud_send(
            adapter,
            "var o={};var ps=ipc.network().getDevice(" + repr(CLOUD) + ").getPorts();"
            "for(var i=0;i<ps.length;i++){"
            "try{o[ps[i]]={conns:String(d.getPortStrConnections(ps[i])),"
            "count:String(d.getPortConnectionCount(ps[i])),"
            "dsl:String(d.isDslConnection(ps[i]))};"
            "}catch(e){o[ps[i]]='E:'+e;}}return JSON.stringify(o);",
        )
        lines.extend([f"## try {label}", f"answer: {answer}", f"after: {after}", ""])
        cloud_send(adapter, "try{d.removeAllPortConnection();}catch(e){};return 'cleared';")
        time.sleep(1.0)
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text("\n".join(lines), encoding="utf-8")
    try:
        adapter.delete_device(CLOUD)
    except Exception:
        pass
    print("wrote", RAW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
