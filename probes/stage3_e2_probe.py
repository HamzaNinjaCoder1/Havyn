#!/usr/bin/env python
"""Custom SSID / WPA2 association probe (S3.E2).

The AP side works: `WirelessServer` accepts a custom SSID, WPA2-PSK
(authenType 4), AES (encryptType 4), the key and the channel, and reads every
one back. The client side is the blocker: every documented profile call
(`addProfile`, `setCurrentProfile`, `setCurrentProfileStringIPs`) is rejected
by this PT build, and no antenna link forms. The verdict is therefore a
refusal with evidence, not an unverified guess.

    python probes/stage3_e2_probe.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.e2-custom-ssid.txt"
AP, LAP = "s3p-e2-ap", "s3p-e2-lap"
SSID, KEY = "HAVYN-WPA2", "HavynPass123"


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
    for name in (AP, LAP):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# E2 custom SSID/WPA2 probe @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (AP, "access_point", "AccessPoint-PT", (300, 250)),
            (LAP, "laptop", "Laptop-PT", (600, 250)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
        time.sleep(3)
        print("radio", adapter.ensure_wireless_radio(
            LAP, module="PT-LAPTOP-NM-1W", slot="0", interface="Wireless0"))
        time.sleep(2)
        ap_state = js(adapter, AP,
                      "var p=d.getProcess('WirelessServer');p.setPort('Port 1');"
                      "p.setSsid(" + json.dumps(SSID) + ");p.setAuthenType(4);"
                      "p.setEncryptType(4);p.setNetworkType(7);"
                      "p.getWpaProcess().setKey(" + json.dumps(KEY) + ");"
                      "p.setStandardChannel(10);p.setSsidBrdCastEnabled(true);"
                      "p.resetAllAssociations();return JSON.stringify({"
                      "ssid:String(p.getSsid()),auth:String(p.getAuthenType()),"
                      "enc:String(p.getEncryptType()),"
                      "channel:String(p.getStandardChannel()),"
                      "key:String(p.getWpaProcess().getKey())});")
        attempts = {}
        for label, code in (
            ("addProfile",
             "var p=d.getProcess('WirelessClient');p.setPort('Wireless0');"
             "return JSON.stringify({ok:p.addProfile('P1'," + json.dumps(SSID) +
             ",7,'',4,4," + json.dumps(KEY) + ",'','',true,false,"
             "'0.0.0.0','0.0.0.0','0.0.0.0','0.0.0.0')});"),
            ("setCurrentProfile",
             "var p=d.getProcess('WirelessClient');p.setPort('Wireless0');"
             "var mac=d.getPort('Wireless0').getMacAddress();"
             "return JSON.stringify({ok:p.setCurrentProfile('P1'," + json.dumps(SSID) +
             ",7,mac,4,4," + json.dumps(KEY) + ",'','',true,false,"
             "'0.0.0.0','0.0.0.0','0.0.0.0','0.0.0.0')});"),
            ("setCurrentProfileStringIPs",
             "var p=d.getProcess('WirelessClient');p.setPort('Wireless0');"
             "return JSON.stringify({ok:p.setCurrentProfileStringIPs('P1'," +
             json.dumps(SSID) + ",7,'',4,4," + json.dumps(KEY) +
             ",'','',true,false,'0.0.0.0','0.0.0.0','0.0.0.0','0.0.0.0')});"),
        ):
            attempts[label] = js(adapter, LAP, code)
            print(label, attempts[label])
        # The simple setters update the process state but not the profile.
        setter_state = js(adapter, LAP,
                          "var p=d.getProcess('WirelessClient');p.setSsid(" +
                          json.dumps(SSID) + ");p.setAuthenType(4);"
                          "p.setEncryptType(4);p.getWpaProcess().setKey(" +
                          json.dumps(KEY) + ");p.setStandardChannel(10);"
                          "p.resetAllAssociations();"
                          "var q=p.getCurrentProfile();return JSON.stringify({"
                          "ssid:String(p.getSsid()),auth:String(p.getAuthenType()),"
                          "enc:String(p.getEncryptType()),"
                          "profileSsid:q?String(q.ssid):null});")
        time.sleep(20)
        assoc = adapter.wireless_association(LAP)
        print("association", assoc)
        lines.extend([
            "## AP state (custom SSID/WPA2 accepted)", json.dumps(ap_state, indent=1),
            "## client profile attempts", json.dumps(attempts, indent=1),
            "## client setters", json.dumps(setter_state, indent=1),
            "## association after 20s", json.dumps(assoc.to_dict() if assoc else None, indent=1),
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
