#!/usr/bin/env python
"""End-device endpoints (S3.H1).

Printer-PT is a full IP host: it takes an address and passes ICMP both ways.
The 7960 is not: it has no usable CLI, its Vlan1 accepts an address through
the port API but never comes up (`isPortUp()` false even with the wall port
linked and powered), and a flow from it never produces a ping. The
smartphone/tablet half is proven in `S3.e3-clients.txt`.

    python probes/stage3_h1_lab.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.h1-end-devices.txt"
RTR, SW, PC, PRN, PH = ("s3p-h1-rtr", "s3p-h1-sw", "s3p-h1-pc",
                        "s3p-h1-prn", "s3p-h1-ph")


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
    for name in (RTR, SW, PC, PRN, PH):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# H1 end devices @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (RTR, "router", "2911", (180, 320)),
            (SW, "switch", "2960-24TT", (380, 320)),
            (PC, "pc", "PC-PT", (580, 200)),
            (PRN, "printer", "Printer-PT", (580, 320)),
            (PH, "ip_phone", "7960", (580, 440)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
        time.sleep(4)
        for a_dev, a_port, b_dev, b_port in (
            (RTR, "GigabitEthernet0/0", SW, "FastEthernet0/1"),
            (SW, "FastEthernet0/2", PC, "FastEthernet0"),
            (SW, "FastEthernet0/3", PRN, "FastEthernet0"),
            (SW, "FastEthernet0/4", PH, "Switch"),
        ):
            receipt = adapter.create_link(a_dev, a_port, b_dev, b_port,
                                          media="straight")
            print("link", a_dev, b_dev, receipt.result.value, receipt.error)
        time.sleep(3)
        adapter.configure_ios(RTR, [
            "hostname s3p-h1-rtr",
            "interface GigabitEthernet0/0",
            " ip address 192.168.79.1 255.255.255.0",
            " no shutdown",
            " exit",
        ])
        adapter.configure_host(PC, dhcp=False, ip="192.168.79.10",
                               mask="255.255.255.0", gateway="192.168.79.1")
        adapter.configure_host(PRN, dhcp=False, ip="192.168.79.20",
                               mask="255.255.255.0", gateway="192.168.79.1")
        phone_cfg = js(adapter, PH,
                       "var p=d.getPort('Vlan1');"
                       "p.setIpSubnetMask('192.168.79.30','255.255.255.0');"
                       "p.setDefaultGateway('192.168.79.1');p.setPower(true);"
                       "return JSON.stringify({ip:String(p.getIpAddress()),"
                       "up:!!p.isPortUp(),proto:!!p.isProtocolUp()});")
        time.sleep(8)
        phone_state = js(adapter, PH,
                         "var out={};var ps=d.getPorts();for(var i=0;i<ps.length;i++){"
                         "var p=d.getPort(ps[i]);out[ps[i]]={up:!!p.isPortUp(),"
                         "proto:!!p.isProtocolUp(),power:!!p.getPower(),"
                         "link:p.getLink()?'yes':'no'};}return JSON.stringify(out);")
        printer_ports = adapter.inspect_device(PRN)
        printer_flows = {
            "pc_to_printer": adapter.run_flow_test(
                PC, PRN, destination_ip="192.168.79.20", timeout=20
            ).outcome.value,
            "printer_to_pc": adapter.run_flow_test(
                PRN, PC, destination_ip="192.168.79.10", timeout=20
            ).outcome.value,
        }
        phone_flows = {
            "pc_to_phone": adapter.run_flow_test(
                PC, PH, destination_ip="192.168.79.30", timeout=20
            ).outcome.value,
            "phone_to_pc": adapter.run_flow_test(
                PH, PC, destination_ip="192.168.79.10", timeout=20
            ).outcome.value,
        }
        print("phone cfg", phone_cfg)
        print("phone state", phone_state)
        print("printer flows", printer_flows)
        print("phone flows", phone_flows)
        lines.extend([
            "## printer ports",
            json.dumps({k: (str(v.ip.value) if v.ip.is_known else None)
                        for k, v in printer_ports.ports.items()}, indent=1),
            "## printer flows", json.dumps(printer_flows, indent=1),
            "## phone config", json.dumps(phone_cfg, indent=1),
            "## phone ports", json.dumps(phone_state, indent=1),
            "## phone flows", json.dumps(phone_flows, indent=1),
            "## tablet/smartphone",
            "proven in probes/raw/S3.e3-clients.txt "
            "(antenna association, DHCP .2/.3, flows both ways)",
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
