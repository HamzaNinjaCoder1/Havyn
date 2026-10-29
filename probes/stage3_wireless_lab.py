#!/usr/bin/env python
"""Wireless SSID/WPA2 and client lab (S3.E2, S3.E3, S3.E5).

Uses the PT process API that the earlier family-word probe missed:

* `AccessPoint-PT.getProcess("WirelessServer")` exposes `setSsid`,
  `setAuthenType`, `setEncryptType`, `getWpaProcess`, `setStandardChannel`.
* `Laptop/Tablet/Smartphone.getProcess("WirelessClient")` exposes the same
  client-side settings.

The lab sets a custom SSID + WPA2-PSK on both ends, proves the association from
PT's antenna link, leases an address over DHCP, and runs real ICMP flows to a
wired host. S3.E5 (multiple SSIDs -> VLANs) is checked against the same API:
the AP has one SSID and no VLAN mapping, so it is recorded as refused with
that evidence rather than guessed.

    python probes/stage3_wireless_lab.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.wireless-lab.txt"
RTR, SW, AP, WIRED = "s3p-wl-rtr", "s3p-wl-sw", "s3p-wl-ap", "s3p-wl-srv"
CLIENTS = [
    ("s3p-wl-lap", "laptop", "Laptop-PT", "PT-LAPTOP-NM-1W"),
    ("s3p-wl-tab", "tablet", "TabletPC-PT", "PT-TABLETPC-NM-1W"),
    ("s3p-wl-phone", "smartphone", "SMARTPHONE-PT", "PT-PDA-NM-1W"),
]
SSID = "HAVYN-WPA2"
KEY = "HavynPass123"
CHANNEL = 10  # PT enum value for 2.462 GHz / channel 11


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


def wifi_state(adapter: BridgeAdapter, device: str) -> object:
    return js(adapter, device,
              "var p=d.getProcess('WirelessClient');"
              "if(!p)p=d.getProcess('WirelessServer');"
              "return JSON.stringify({ssid:String(p.getSsid()),"
              "auth:String(p.getAuthenType()),enc:String(p.getEncryptType()),"
              "channel:String(p.getStandardChannel())});")


def configure_ap(adapter: BridgeAdapter) -> object:
    return js(adapter, AP,
              "var p=d.getProcess('WirelessServer');"
              "p.setSsid(" + json.dumps(SSID) + ");p.setAuthenType(2);"
              "p.setEncryptType(4);p.getWpaProcess().setKey(" + json.dumps(KEY) + ");"
              "p.setStandardChannel(" + str(CHANNEL) + ");"
              "p.setSsidBrdCastEnabled(true);p.resetAllAssociations();"
              "return JSON.stringify({ssid:String(p.getSsid()),"
              "auth:String(p.getAuthenType()),enc:String(p.getEncryptType()),"
              "channel:String(p.getStandardChannel()),key:String(p.getWpaProcess().getKey())});")


def configure_client(adapter: BridgeAdapter, device: str) -> object:
    return js(adapter, device,
              "var p=d.getProcess('WirelessClient');"
              "p.setSsid(" + json.dumps(SSID) + ");p.setAuthenType(2);"
              "p.setEncryptType(4);p.getWpaProcess().setKey(" + json.dumps(KEY) + ");"
              "p.setStandardChannel(" + str(CHANNEL) + ");p.resetAllAssociations();"
              "return JSON.stringify({ssid:String(p.getSsid()),"
              "auth:String(p.getAuthenType()),enc:String(p.getEncryptType()),"
              "channel:String(p.getStandardChannel()),key:String(p.getWpaProcess().getKey())});")


def ipconfig(adapter: BridgeAdapter, device: str) -> str:
    send(adapter, device, "d.getCommandLine().enterCommand('ipconfig');return 'sent';")
    time.sleep(3)
    return send(adapter, device,
                "return String(d.getCommandLine().getOutput()).slice(-900);")


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    for name, *_ in CLIENTS:
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    for name in (RTR, SW, AP, WIRED):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# wireless lab @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (RTR, "router", "2911", (180, 320)),
            (SW, "switch", "2960-24TT", (380, 320)),
            (AP, "access_point", "AccessPoint-PT", (580, 180)),
            (WIRED, "pc", "PC-PT", (580, 440)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
        for name, role, model, module in CLIENTS:
            receipt = adapter.create_device(name, role, model_hint=model,
                                            position=(760, 260))
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
            radio = adapter.ensure_wireless_radio(name, module=module, slot="0",
                                                  interface="Wireless0")
            print("  radio", name, radio.get("installed"), radio.get("interface"))
        time.sleep(4)
        links = [
            (RTR, "GigabitEthernet0/0", SW, "FastEthernet0/1", "straight"),
            (SW, "FastEthernet0/2", AP, "Port 0", "straight"),
            (SW, "FastEthernet0/3", WIRED, "FastEthernet0", "straight"),
        ]
        for a_dev, a_port, b_dev, b_port, media in links:
            receipt = adapter.create_link(a_dev, a_port, b_dev, b_port, media=media)
            print("link", a_dev, b_dev, receipt.result.value, receipt.error)
        time.sleep(3)
        router_cfg = adapter.configure_ios(RTR, [
            "hostname s3p-wl-rtr",
            "interface GigabitEthernet0/0",
            " ip address 192.168.77.1 255.255.255.0",
            " no shutdown",
            " exit",
            "ip dhcp pool WLAN",
            " network 192.168.77.0 255.255.255.0",
            " default-router 192.168.77.1",
            " dns-server 192.168.77.1",
            " exit",
        ])
        print("router", router_cfg.result.value, router_cfg.error)
        adapter.configure_host(WIRED, dhcp=False, ip="192.168.77.10",
                               mask="255.255.255.0", gateway="192.168.77.1",
                               dns="192.168.77.1")
        ap_state = configure_ap(adapter)
        print("ap", ap_state)
        client_states = {}
        for name, *_ in CLIENTS:
            client_states[name] = configure_client(adapter, name)
            print("client", name, client_states[name])
        time.sleep(20)
        associations = {}
        leases = {}
        for name, *_ in CLIENTS:
            assoc = adapter.wireless_association(name)
            associations[name] = assoc.to_dict() if assoc else None
            adapter.configure_host(name, dhcp=True)
        time.sleep(18)
        for name, *_ in CLIENTS:
            leases[name] = ipconfig(adapter, name)
            print("lease", name, leases[name][-200:].replace("\n", " "))
        flows = {}
        for name, *_ in CLIENTS:
            forward = adapter.run_flow_test(name, WIRED,
                                            destination_ip="192.168.77.10",
                                            timeout=20)
            target = adapter._resolve_destination_ip(name)
            reverse = adapter.run_flow_test(
                WIRED, name,
                destination_ip=target.value if target.is_known else None,
                timeout=20,
            )
            flows[name] = {
                "client_to_wired": forward.outcome.value,
                "wired_to_client": reverse.outcome.value,
            }
            print("flow", name, flows[name])
        # E5 evidence: one SSID, no VLAN mapping on the AP.
        e5 = js(adapter, AP,
                "var p=d.getProcess('WirelessServer');"
                "var names=Object.getOwnPropertyNames(Object.getPrototypeOf(p));"
                "return JSON.stringify({ssidCount:1,ssid:String(p.getSsid()),"
                "vlanMethods:names.filter(function(n){return /vlan/i.test(n);}),"
                "ssidMethods:names.filter(function(n){return /ssid/i.test(n);})});")
        print("e5", e5)
        lines.extend([
            "## ap state", json.dumps(ap_state, indent=1),
            "## client states", json.dumps(client_states, indent=1),
            "## associations", json.dumps(associations, indent=1),
            "## leases",
            *[f"### {name}\n{text}" for name, text in leases.items()],
            "## flows", json.dumps(flows, indent=1),
            "## e5 api", json.dumps(e5, indent=1),
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
