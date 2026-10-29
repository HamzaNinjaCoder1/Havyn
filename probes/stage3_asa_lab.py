#!/usr/bin/env python
"""ASA DMZ, routes and DHCP live lab (S3.C2, S3.C4, S3.C5).

Also records the per-command status that decides S3.C3 (NAT) and S3.C6
(object groups): PT's ASA 9.6 image rejects every NAT form and every
object-group line with status 2, while the DMZ interfaces, static route and
DHCP server lines are accepted and read back.

Topology::

    in-pc / dhcp-pc -- switch -- ASA Gi1/1 (inside, 100)
                                   Gi1/2 (outside, 0) -- out-rt -- inet-srv
                                   Gi1/3 (dmz, 50)     -- dmz-srv

    python probes/stage3_asa_lab.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.asa-lab.txt"
ASA = "s3p-asa-lab"
SW = "s3p-asa-sw"
IN_PC, DHCP_PC = "s3p-asa-inpc", "s3p-asa-dhcp"
OUT_RT, INET = "s3p-asa-outrt", "s3p-asa-inet"
DMZ = "s3p-asa-dmz"


def send(adapter: BridgeAdapter, device: str, code: str, wait: float = 30.0) -> str:
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


def asa_show(adapter: BridgeAdapter, command: str) -> str:
    payload = js(adapter, ASA,
                 "var r=d.enterCommand(" + json.dumps(command) + ",'enable');"
                 "return JSON.stringify({status:String(r.first),"
                 "out:String(r.second)});")
    if isinstance(payload, dict):
        return f"# status {payload.get('status')}\n{payload.get('out')}"
    return str(payload)


def asa_command(adapter: BridgeAdapter, command: str, mode: str = "global") -> dict:
    payload = js(adapter, ASA,
                 "var r=d.enterCommand(" + json.dumps(command) + "," +
                 json.dumps(mode) + ");return JSON.stringify({status:String(r.first),"
                 "out:String(r.second)});")
    return payload if isinstance(payload, dict) else {"raw": payload}


def wait_boot(adapter: BridgeAdapter, timeout: float = 150.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = send(adapter, ASA, "return String(d.isBooting());")
        if last == "false":
            return last
        time.sleep(3.0)
    return f"TIMEOUT:{last}"


def main() -> int:
    adapter = BridgeAdapter(host_bridge=False)
    if not adapter.ping(timeout=5).reachable:
        print("PT UNREACHABLE")
        return 4
    names = [ASA, SW, IN_PC, DHCP_PC, OUT_RT, INET, DMZ]
    for name in names:
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# ASA lab @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (ASA, "firewall", "5506-X", (420, 250)),
            (SW, "switch", "2960-24TT", (220, 250)),
            (IN_PC, "pc", "PC-PT", (60, 180)),
            (DHCP_PC, "pc", "PC-PT", (60, 330)),
            (OUT_RT, "router", "2911", (640, 250)),
            (INET, "server", "Server-PT", (820, 250)),
            (DMZ, "server", "Server-PT", (420, 470)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
        print("boot:", wait_boot(adapter)[:80])
        for a_dev, a_port, b_dev, b_port, media in (
            (ASA, "GigabitEthernet1/1", SW, "FastEthernet0/1", "straight"),
            (SW, "FastEthernet0/2", IN_PC, "FastEthernet0", "straight"),
            (SW, "FastEthernet0/3", DHCP_PC, "FastEthernet0", "straight"),
            (ASA, "GigabitEthernet1/2", OUT_RT, "GigabitEthernet0/0", "cross"),
            (OUT_RT, "GigabitEthernet0/1", INET, "FastEthernet0", "cross"),
            (ASA, "GigabitEthernet1/3", DMZ, "FastEthernet0", "cross"),
        ):
            receipt = adapter.create_link(a_dev, a_port, b_dev, b_port, media=media)
            print("link", a_dev, b_dev, receipt.result.value, receipt.error)
        time.sleep(4)
        push = adapter.configure_ios(ASA, [
            "hostname s3p-asa-lab",
            "interface GigabitEthernet1/1", " nameif inside", " security-level 100",
            " ip address 10.99.0.1 255.255.255.0", " no shutdown", " exit",
            "interface GigabitEthernet1/2", " nameif outside", " security-level 0",
            " ip address 10.99.1.1 255.255.255.0", " no shutdown", " exit",
            "interface GigabitEthernet1/3", " nameif dmz", " security-level 50",
            " ip address 10.99.2.1 255.255.255.0", " no shutdown", " exit",
            "access-list OUTSIDE-IN extended permit icmp any host 10.99.2.10",
            # The return half of an inside-initiated flow; a ping STARTED on
            # the outside is still an echo request and stays denied below.
            "access-list OUTSIDE-IN extended permit icmp host 10.99.10.10 "
            "host 10.99.0.10 echo-reply",
            "access-list OUTSIDE-IN extended deny ip any any",
            "access-group OUTSIDE-IN in interface outside",
            "route outside 10.99.10.0 255.255.255.0 10.99.1.2 1",
            "route outside 0.0.0.0 0.0.0.0 10.99.1.2 1",
            "dhcpd address 10.99.0.100-10.99.0.150 inside",
            "dhcpd enable inside",
        ])
        print("asa push", push.result.value, push.error)
        out_cfg = adapter.configure_ios(OUT_RT, [
            "hostname s3p-asa-outrt",
            "interface GigabitEthernet0/0",
            " ip address 10.99.1.2 255.255.255.0",
            " no shutdown",
            " exit",
            "interface GigabitEthernet0/1",
            " ip address 10.99.10.1 255.255.255.0",
            " no shutdown",
            " exit",
            "ip route 10.99.0.0 255.255.255.0 10.99.1.1",
            "ip route 10.99.2.0 255.255.255.0 10.99.1.1",
        ])
        print("out router", out_cfg.result.value, out_cfg.error)
        adapter.configure_host(IN_PC, dhcp=False, ip="10.99.0.10",
                               mask="255.255.255.0", gateway="10.99.0.1")
        adapter.configure_host(INET, dhcp=False, ip="10.99.10.10",
                               mask="255.255.255.0", gateway="10.99.10.1")
        adapter.configure_host(DMZ, dhcp=False, ip="10.99.2.10",
                               mask="255.255.255.0", gateway="10.99.2.1")
        adapter.configure_host(DHCP_PC, dhcp=True)
        time.sleep(18)
        dhcp_ip = js(adapter, DHCP_PC,
                     "var p=d.getPort('FastEthernet0');"
                     "return JSON.stringify({ip:String(p.getIpAddress()),"
                     "mask:String(p.getSubnetMask()),"
                     "dhcp:!!p.isDhcpClientOn()});")
        flows = {
            "dmz_from_outside": adapter.run_flow_test(
                OUT_RT, DMZ, destination_ip="10.99.2.10", timeout=25
            ).outcome.value,
            "outside_to_inside_blocked": adapter.run_flow_test(
                OUT_RT, IN_PC, destination_ip="10.99.0.10", timeout=25
            ).outcome.value,
            "inside_to_remote_route": adapter.run_flow_test(
                IN_PC, INET, destination_ip="10.99.10.10", timeout=25
            ).outcome.value,
        }
        # C3/C6 refusal statuses, recorded on the same device.
        nat_statuses = {
            "object-nat": asa_command(
                adapter, "nat (inside,outside) dynamic interface"),
            "twice-nat": asa_command(
                adapter, "nat (inside,outside) source dynamic any interface"),
            "classic-global": asa_command(
                adapter, "global (outside) 1 interface"),
        }
        object_group = asa_command(adapter, "object-group network WEB-SERVERS")
        show_run = asa_show(adapter, "show running-config")
        show_dhcp = asa_show(adapter, "show dhcpd")
        print("dhcp pc", dhcp_ip)
        print("flows", flows)
        lines.extend([
            "## asa push", push.result.value,
            "## dhcp lease", json.dumps(dhcp_ip, indent=1),
            "## flows", json.dumps(flows, indent=1),
            "## C3 nat statuses", json.dumps(nat_statuses, indent=1),
            "## C6 object-group status", json.dumps(object_group, indent=1),
            "## show running-config", show_run,
            "## show dhcpd", show_dhcp,
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
