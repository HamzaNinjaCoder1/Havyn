#!/usr/bin/env python
"""IoT registration/control surface (S3.G1, S3.G2).

The earlier probe asked for a family word and found nothing. The IpcAPI lists
`Ioe`, `IoeClient`, `IoeServer`, `IoEComponent`, `CustomIO`; this probe asks
for those names on a real Smart-LED and a HomeRouter-PT-AC and records what
exists. If no registration call is exposed, the track stays refused with this
evidence.

    python probes/stage3_iot_probe.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.iot-api.txt"
LED, GW = "s3p-iot-led", "s3p-iot-gw"
PROCESSES = ["Ioe", "IoeClient", "IoeServer", "IoEComponent", "CustomIO",
             "WirelessRouter", "AccessPointSwitcher", "WirelessServer"]


def send(adapter: BridgeAdapter, device: str, code: str, wait: float = 15.0) -> str:
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


def processes(adapter: BridgeAdapter, device: str) -> object:
    names = json.dumps(PROCESSES)
    return js(adapter, device,
              "var out={};var names=" + names + ";"
              "for(var i=0;i<names.length;i++){try{var p=d.getProcess(names[i]);"
              "out[names[i]]=p?Object.getOwnPropertyNames(Object.getPrototypeOf(p))"
              ":null;}catch(e){out[names[i]]='E:'+e;}}"
              "return JSON.stringify(out);")


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    for name in (LED, GW):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# IoT probe @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (LED, "thing", "Smart-LED", (300, 250)),
            (GW, "home_gateway", "HomeRouter-PT-AC", (600, 250)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                continue
            created.append(name)
        time.sleep(3)
        led_ports = js(adapter, LED, "return JSON.stringify(d.getPorts());")
        gw_ports = js(adapter, GW, "return JSON.stringify(d.getPorts());")
        print("ports", led_ports, gw_ports)
        if isinstance(led_ports, list) and led_ports and isinstance(gw_ports, list) and gw_ports:
            receipt = adapter.create_link(LED, led_ports[0], GW, gw_ports[0],
                                          media="straight")
            print("link", receipt.result.value, receipt.error)
        time.sleep(3)
        led = processes(adapter, LED)
        gw = processes(adapter, GW)
        print("led", led)
        print("gw", gw)
        lines.extend([
            "## ports", f"led={led_ports} gateway={gw_ports}",
            "## smart-led processes", json.dumps(led, indent=1),
            "## home-gateway processes", json.dumps(gw, indent=1),
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
