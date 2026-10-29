#!/usr/bin/env python
"""Wireless controller / lightweight-AP probe (S3.E2-E5).

Three questions, each answered from live PT state:

1. Does a WLC-PT console accept IOS-style configuration and can its wireless
   state be read back?
2. Does PT actually place a lightweight AP (`LAP-PT`, `3702i`) so a WLC join
   could be attempted?
3. Does an AP/clients have any SSID or security configuration method?

    python probes/stage3_wireless_probe.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.wireless.txt"
WLC = "s3p-wlc"


def send(adapter: BridgeAdapter, expr: str, wait: float = 20.0) -> str:
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def wlc_expr(body: str) -> str:
    return (
        "(function(){var d=ipc.network().getDevice(" + repr(WLC) + ");"
        "if(!d){return 'E:no-device';}"
        "var cl=null,err='';"
        "try{cl=d.getCommandLine();}catch(e){err=String(e);}"
        "if(!cl){return 'E:no-commandline '+err;}" + body + "})()"
    )


def wlc_send(adapter: BridgeAdapter, body: str, wait: float = 20.0) -> str:
    return send(adapter, wlc_expr(body), wait=wait)


def wlc_output(adapter: BridgeAdapter) -> str:
    return wlc_send(adapter, "return String(cl.getOutput());")


def wlc_cli(adapter: BridgeAdapter, command: str, timeout: float = 30.0) -> str:
    before = wlc_output(adapter)
    wlc_send(adapter, f"cl.enterCommand({command!r}); return 'typed';")
    deadline = time.time() + timeout
    last = ""
    paged = 0
    while time.time() < deadline:
        time.sleep(1.5)
        text = wlc_output(adapter)
        last = text
        if "--More--" in text[-300:]:
            wlc_send(adapter, "cl.enterCommand(' '); return 'page';")
            paged += 1
            if paged > 20:
                break
            continue
        index = text.rfind(command)
        tail = text[index:] if index >= 0 else text[-800:]
        prompt = wlc_send(
            adapter, "return JSON.stringify(String(cl.getPrompt()||''));"
        ).strip('"')
        if index >= 0 and len(text) >= len(before) and (
            prompt.endswith((">", "#")) or tail.rstrip().endswith((">", "#"))
        ):
            return tail
    return last


def state(adapter: BridgeAdapter, device: str) -> dict:
    expr = (
        "(function(){var d=ipc.network().getDevice(" + repr(device) + ");"
        "if(!d){return 'E:no-device';}var o={model:String(d.getModel()),"
        "ports:d.getPorts()};"
        "try{o.commandLine=d.getCommandLine()?'yes':'null';}catch(e){o.commandLine='E:'+e;}"
        "return JSON.stringify(o);})()"
    )
    text = send(adapter, expr)
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    lines = [
        f"# Stage 3 wireless probe @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    created: list[str] = []
    try:
        receipt = adapter.create_device(WLC, "wlc", model_hint="WLC-PT",
                                        position=(400, 200))
        print("create WLC:", receipt.result.value, receipt.error)
        if receipt.result.value == "success":
            created.append(WLC)
            time.sleep(20.0)
            raw = wlc_output(adapter)
            lines.extend(["## WLC boot output", raw[-2000:], ""])
            # prime: initial dialog, RETURN, enable
            prompt = wlc_send(
                adapter, "return JSON.stringify(String(cl.getPrompt()||''));"
            )
            lines.extend(["## WLC prompt before prime", prompt, ""])
            wlc_send(adapter, "cl.enterCommand('no'); return 'no';")
            time.sleep(1.0)
            wlc_send(adapter, "cl.enterCommand(''); return 'enter';")
            time.sleep(1.0)
            wlc_send(adapter, "cl.enterCommand('enable'); return 'enable';")
            time.sleep(2.0)
            for command in ("show version", "show running-config",
                            "show wlan summary", "show ap summary"):
                print("  WLC", command, flush=True)
                lines.extend([f"### WLC: {command}",
                              wlc_cli(adapter, command), ""])
        for model in ("LAP-PT", "3702i", "LAP-3702I", "AIR-LAP1142N-A-K9"):
            name = "s3p-" + model.lower().replace("-", "")
            try:
                adapter.delete_device(name)
            except Exception:
                pass
            result = adapter.create_device(name, "access_point",
                                           model_hint=model,
                                           position=(650, 350))
            info = state(adapter, name) if result.result.value == "success" else {}
            print("create", model, result.result.value, result.error, info)
            lines.extend([f"## create {model}",
                          f"result: {result.result.value} error: {result.error}",
                          json.dumps(info), ""])
            if result.result.value == "success":
                created.append(name)
        # The configured device must also expose (or not) SSID methods.
        probe = (
            "(function(){var out={};"
            "var models=['" + WLC + "'];"
            "for(var i=0;i<models.length;i++){"
            "var d=ipc.network().getDevice(models[i]);if(!d){continue;}"
            "var names=['Ssid','SsidName','Wlan','WlanConfig','Wireless',"
            "'Security','Wpa','SetSsid'];"
            "out[models[i]]={};"
            "for(var j=0;j<names.length;j++){"
            "var n=names[j];"
            "out[models[i]][n]={get:typeof d['get'+n],set:typeof d['set'+n]};"
            "}}return JSON.stringify(out);})()"
        )
        lines.extend(["## SSID method surface", send(adapter, probe), ""])
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
