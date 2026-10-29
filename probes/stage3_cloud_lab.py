#!/usr/bin/env python
"""Cloud mapping, serial Frame Relay and ISP template (S3.F2, S3.F3, S3.F4).

Two live labs in one run:

* F2/F4 - one Cloud-PT with two independent Frame Relay circuits
  (`addSubLink` + `addSubLinkConnection` per serial port). Each customer pair
  must pass traffic; the two pairs must NOT reach each other. This is the
  explicit cloud-mapping driver and the serial-WAN-through-cloud proof.
* F3 - the reusable ISP template: customer router -> cable modem -> Cloud
  (coax<->Ethernet mapping) -> ISP router -> internet server, with real flows
  in both directions.

    python probes/stage3_cloud_lab.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.cloud-lab.txt"


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


def serial_lab(adapter: BridgeAdapter, lines: list[str]) -> dict:
    cloud = "s3p-f2-cl"
    routers = [("s3p-f2-r1", "10.10.0.1", "100", "10.10.0.2"),
               ("s3p-f2-r2", "10.10.0.2", "200", "10.10.0.1"),
               ("s3p-f2-r3", "10.20.0.1", "300", "10.20.0.2"),
               ("s3p-f2-r4", "10.20.0.2", "400", "10.20.0.1")]
    names = [cloud] + [r[0] for r in routers]
    for name in names:
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    try:
        receipt = adapter.create_device(cloud, "cloud", model_hint="Cloud-PT",
                                        position=(450, 250))
        if receipt.result.value != "success":
            return {"error": receipt.error}
        created.append(cloud)
        for index, (name, _ip, _dlci, _peer) in enumerate(routers):
            receipt = adapter.create_device(
                name, "router", model_hint="Router-PT",
                position=(120 + index * 180, 470),
            )
            if receipt.result.value != "success":
                return {"error": f"{name}: {receipt.error}"}
            created.append(name)
        time.sleep(4)
        for index, (name, _ip, _dlci, _peer) in enumerate(routers):
            cloud_port = f"Serial{index}"
            receipt = adapter.create_link(name, "Serial2/0", cloud, cloud_port,
                                          media="serial")
            if receipt.result.value != "success":
                return {"error": f"link {name}: {receipt.error}"}
        time.sleep(3)
        mapping = js(adapter, cloud,
                     "var p0=d.getPort('Serial0'),p1=d.getPort('Serial1');"
                     "var p2=d.getPort('Serial2'),p3=d.getPort('Serial3');"
                     "p0.setLmiType(1);p1.setLmiType(1);p2.setLmiType(1);p3.setLmiType(1);"
                     "p0.addSubLink('r1',100);p1.addSubLink('r2',200);"
                     "p2.addSubLink('r3',300);p3.addSubLink('r4',400);"
                     "var a=d.addSubLinkConnection('Serial0','r1','Serial1','r2');"
                     "var b=d.addSubLinkConnection('Serial2','r3','Serial3','r4');"
                     "var v=d.getPortStrConnections();var s=[];"
                     "for(var i=0;i<v.length;i++)s.push(String(v[i]));"
                     "return JSON.stringify({pairA:a,pairB:b,"
                     "subLinkConnections:d.getSubLinkConnectionCount(),list:s});")
        for name, ip, dlci, peer in routers:
            cfg = adapter.configure_ios(name, [
                f"hostname {name}",
                "interface Serial2/0",
                " encapsulation frame-relay",
                f" ip address {ip} 255.255.255.252",
                " no shutdown",
                f" frame-relay map ip {peer} {dlci} broadcast",
                " exit",
            ])
            print("router", name, cfg.result.value, cfg.error)
        time.sleep(12)
        flows = {
            "customer_a": adapter.run_flow_test(
                "s3p-f2-r1", "s3p-f2-r2", destination_ip="10.10.0.2", timeout=25
            ).outcome.value,
            "customer_b": adapter.run_flow_test(
                "s3p-f2-r3", "s3p-f2-r4", destination_ip="10.20.0.2", timeout=25
            ).outcome.value,
            "a_to_b_isolation": adapter.run_flow_test(
                "s3p-f2-r1", "s3p-f2-r4", destination_ip="10.20.0.2", timeout=25
            ).outcome.value,
        }
        map_a = adapter._console_show_full("s3p-f2-r1", "show frame-relay map")
        lines.extend([
            "## F2/F4 serial mapping", json.dumps(mapping, indent=1),
            "## F2/F4 flows", json.dumps(flows, indent=1),
            "## r1 frame-relay map",
            str(map_a.value) if map_a.is_known else str(map_a.detail),
        ])
        return {"mapping": mapping, "flows": flows}
    finally:
        for name in created:
            try:
                adapter.delete_device(name)
            except Exception:
                pass


def isp_template_lab(adapter: BridgeAdapter, lines: list[str]) -> dict:
    cloud, modem, cust, isp, srv, pc = (
        "s3p-f3-cl", "s3p-f3-mod", "s3p-f3-cust", "s3p-f3-isp",
        "s3p-f3-srv", "s3p-f3-pc",
    )
    names = [cloud, modem, cust, isp, srv, pc]
    for name in names:
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    try:
        for name, role, model, pos in (
            (cloud, "cloud", "Cloud-PT", (450, 250)),
            (modem, "modem", "Cable-Modem-PT", (300, 450)),
            (cust, "router", "2911", (100, 450)),
            (isp, "router", "2911", (600, 450)),
            (srv, "server", "Server-PT", (800, 450)),
            (pc, "pc", "PC-PT", (100, 650)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model,
                                            position=pos)
            if receipt.result.value != "success":
                return {"error": f"{name}: {receipt.error}"}
            created.append(name)
        time.sleep(4)
        for a_dev, a_port, b_dev, b_port, media in (
            (modem, "Port 0", cloud, "Coaxial7", "coaxial"),
            (cust, "GigabitEthernet0/0", modem, "Port 1", "straight"),
            (isp, "GigabitEthernet0/0", cloud, "Ethernet6", "straight"),
            (isp, "GigabitEthernet0/1", srv, "FastEthernet0", "cross"),
            (cust, "GigabitEthernet0/1", pc, "FastEthernet0", "cross"),
        ):
            receipt = adapter.create_link(a_dev, a_port, b_dev, b_port,
                                          media=media)
            if receipt.result.value != "success":
                return {"error": f"link {a_dev}-{b_dev}: {receipt.error}"}
        time.sleep(3)
        mapping = js(adapter, cloud,
                     "var dsl=d.setDslConnection('Coaxial7',false);"
                     "var conn=d.addPortConnection('Coaxial7','Ethernet6');"
                     "var v=d.getPortStrConnections();var s=[];"
                     "for(var i=0;i<v.length;i++)s.push(String(v[i]));"
                     "return JSON.stringify({dsl:dsl,connection:conn,list:s});")
        cust_cfg = adapter.configure_ios(cust, [
            "hostname cust",
            "interface GigabitEthernet0/0",
            " ip address 10.100.0.2 255.255.255.252",
            " no shutdown",
            " exit",
            "interface GigabitEthernet0/1",
            " ip address 192.168.100.1 255.255.255.0",
            " no shutdown",
            " exit",
            "ip route 0.0.0.0 0.0.0.0 10.100.0.1",
        ])
        isp_cfg = adapter.configure_ios(isp, [
            "hostname isp",
            "interface GigabitEthernet0/0",
            " ip address 10.100.0.1 255.255.255.252",
            " no shutdown",
            " exit",
            "interface GigabitEthernet0/1",
            " ip address 203.0.113.1 255.255.255.0",
            " no shutdown",
            " exit",
            "ip route 192.168.100.0 255.255.255.0 10.100.0.2",
        ])
        adapter.configure_host(pc, dhcp=False, ip="192.168.100.10",
                               mask="255.255.255.0", gateway="192.168.100.1")
        adapter.configure_host(srv, dhcp=False, ip="203.0.113.10",
                               mask="255.255.255.0", gateway="203.0.113.1")
        time.sleep(12)
        flows = {
            "home_to_internet": adapter.run_flow_test(
                pc, srv, destination_ip="203.0.113.10", timeout=25
            ).outcome.value,
            "internet_to_home": adapter.run_flow_test(
                srv, pc, destination_ip="192.168.100.10", timeout=25
            ).outcome.value,
        }
        lines.extend([
            "## F3 cloud mapping", json.dumps(mapping, indent=1),
            "## F3 config", f"customer={cust_cfg.result.value} isp={isp_cfg.result.value}",
            "## F3 flows", json.dumps(flows, indent=1),
        ])
        return {"mapping": mapping, "flows": flows}
    finally:
        for name in created:
            try:
                adapter.delete_device(name)
            except Exception:
                pass


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    lines = [
        f"# cloud lab @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    serial = serial_lab(adapter, lines)
    print("serial:", json.dumps(serial))
    isp = isp_template_lab(adapter, lines)
    print("isp:", json.dumps(isp))
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", RAW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
