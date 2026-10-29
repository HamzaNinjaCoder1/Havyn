#!/usr/bin/env python
"""Smartphone/tablet wireless clients (S3.E3).

PT associates a wireless host on its own on the AP's default SSID (the same
mechanism S3.E1 already proves for a laptop). This lab extends it to the two
client models the map still lists as Unverified:

* install the model's wireless NIC,
* prove the antenna link to the AP,
* lease an address from the router pool over DHCP,
* run a real ICMP flow in both directions to a wired host.

The custom-SSID/WPA2 path is S3.E2 and is refused separately: the AP side
accepts and reads back the SSID/WPA2, but every documented client-profile call
(`addProfile`/`setCurrentProfile`/`setCurrentProfileStringIPs`) is rejected by
this PT build.

    python probes/stage3_e3_lab.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.e3-clients.txt"
RTR, SW, AP, WIRED = "s3p-e3b-rtr", "s3p-e3b-sw", "s3p-e3b-ap", "s3p-e3b-pc"
CLIENTS = [
    ("s3p-e3b-tab", "tablet", "TabletPC-PT", "PT-TABLETPC-NM-1W"),
    ("s3p-e3b-phone", "smartphone", "SMARTPHONE-PT", "PT-PDA-NM-1W"),
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


def radio_state(adapter: BridgeAdapter, device: str) -> object:
    return js(adapter, device,
              "var p=d.getPort('Wireless0');"
              "return JSON.stringify({ip:String(p.getIpAddress()),"
              "mask:String(p.getSubnetMask()),"
              "dhcp:!!p.isDhcpClientOn(),up:!!p.isPortUp(),"
              "link:p.getLink()?'yes':'no'});")


def ipconfig(adapter: BridgeAdapter, device: str) -> str:
    send(adapter, device, "d.getCommandLine().enterCommand('ipconfig /all');return 'sent';")
    time.sleep(4)
    text = send(adapter, device, "return String(d.getCommandLine().getOutput());")
    # drop a trailing pager request so the next command is not eaten
    if "--More--" in text[-300:]:
        send(adapter, device, "d.getCommandLine().enterCommand(' ');return 'page';")
    return text


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    for name in [RTR, SW, AP, WIRED] + [c[0] for c in CLIENTS]:
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# E3 wireless clients @ "
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
            print("  radio", radio)
        time.sleep(4)
        for a_dev, a_port, b_dev, b_port, media in (
            (RTR, "GigabitEthernet0/0", SW, "FastEthernet0/1", "straight"),
            (SW, "FastEthernet0/2", AP, "Port 0", "straight"),
            (SW, "FastEthernet0/3", WIRED, "FastEthernet0", "straight"),
        ):
            receipt = adapter.create_link(a_dev, a_port, b_dev, b_port, media=media)
            print("link", a_dev, b_dev, receipt.result.value, receipt.error)
        time.sleep(3)
        router_cfg = adapter.configure_ios(RTR, [
            "hostname s3p-e3b-rtr",
            "interface GigabitEthernet0/0",
            " ip address 192.168.78.1 255.255.255.0",
            " no shutdown",
            " exit",
            "ip dhcp pool WLAN",
            " network 192.168.78.0 255.255.255.0",
            " default-router 192.168.78.1",
            " dns-server 192.168.78.1",
            " exit",
        ])
        print("router", router_cfg.result.value, router_cfg.error)
        adapter.configure_host(WIRED, dhcp=False, ip="192.168.78.10",
                               mask="255.255.255.0", gateway="192.168.78.1")
        time.sleep(20)
        associations = {}
        for name, *_ in CLIENTS:
            assoc = adapter.wireless_association(name)
            associations[name] = assoc.to_dict() if assoc else None
            adapter.configure_host(name, dhcp=True)
        time.sleep(15)
        states = {name: radio_state(adapter, name) for name, *_ in CLIENTS}
        configs = {name: ipconfig(adapter, name) for name, *_ in CLIENTS}
        flows = {}
        for name, *_ in CLIENTS:
            target = adapter._resolve_destination_ip(name)
            flows[name] = {
                "client_to_wired": adapter.run_flow_test(
                    name, WIRED, destination_ip="192.168.78.10", timeout=20
                ).outcome.value,
                "wired_to_client": adapter.run_flow_test(
                    WIRED, name,
                    destination_ip=target.value if target.is_known else None,
                    timeout=20,
                ).outcome.value,
            }
            print("state", name, states[name], "flows", flows[name])
        lines.extend([
            "## associations", json.dumps(associations, indent=1),
            "## radio state", json.dumps(states, indent=1),
            "## ipconfig",
            *[f"### {name}\n{text}" for name, text in configs.items()],
            "## flows", json.dumps(flows, indent=1),
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
