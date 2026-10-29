#!/usr/bin/env python
"""Server-PT DHCP pool and lease (S3.D4).

Measured: the service is reached through `getProcess("DhcpServerMain")`, whose
per-port `DhcpServerProcess` owns the pools. The server ships a default
`serverPool` covering 10.77.0.0/15; it must be removed first or it hands out
.1 before the named pool is consulted.

    python probes/stage3_server_dhcp_lab.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.server-dhcp.txt"
SRV, PC = "s3p-dhcp-srv", "s3p-dhcp-pc"


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
    for name in (SRV, PC):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# Server DHCP lab @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (SRV, "server", "Server-PT", (300, 250)),
            (PC, "pc", "PC-PT", (600, 250)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
        time.sleep(4)
        link = adapter.create_link(PC, "FastEthernet0", SRV, "FastEthernet0",
                                   media="cross")
        print("link", link.result.value, link.error)
        time.sleep(2)
        adapter.configure_host(SRV, dhcp=False, ip="10.77.0.2",
                               mask="255.255.255.0", gateway="10.77.0.2",
                               dns="10.77.0.2")
        before = js(adapter, SRV,
                    "var m=d.getProcess('DhcpServerMain');"
                    "var p=m.getDhcpServerProcessByPortName('FastEthernet0');"
                    "var out=[];for(var i=0;i<p.getPoolCount();i++){var q=p.getPoolAt(i);"
                    "out.push(q.getDhcpPoolName());}return JSON.stringify(out);")
        setup = js(adapter, SRV,
                   "var m=d.getProcess('DhcpServerMain');"
                   "var p=m.getDhcpServerProcessByPortName('FastEthernet0');"
                   "var names=[];for(var i=0;i<p.getPoolCount();i++){"
                   "names.push(p.getPoolAt(i).getDhcpPoolName());}"
                   "for(var j=0;j<names.length;j++){p.removePool(names[j]);}"
                   "p.addNewPool('POOL1','10.77.0.2','10.77.0.2','10.77.0.100',"
                   "'255.255.255.0',20,'0.0.0.0','0.0.0.0');p.setEnable(true);"
                   "var q=p.getPool('POOL1');return JSON.stringify({"
                   "enabled:p.isEnable(),pools:p.getPoolCount(),"
                   "name:q?q.getDhcpPoolName():null,start:q?String(q.getStartIp()):null,"
                   "end:q?String(q.getEndIp()):null,gw:q?String(q.getDefaultRouter()):null,"
                   "dns:q?String(q.getDnsServerIp()):null});")
        print("pools before", before)
        print("setup", setup)
        adapter.configure_host(PC, dhcp=True)
        time.sleep(15)
        send(adapter, PC, "d.getCommandLine().enterCommand('ipconfig /all');return 'sent';")
        time.sleep(4)
        ipconfig = send(adapter, PC,
                        "return String(d.getCommandLine().getOutput()).slice(-1500);")
        lease = js(adapter, PC,
                   "var p=d.getPort('FastEthernet0');"
                   "return JSON.stringify({ip:String(p.getIpAddress()),"
                   "mask:String(p.getSubnetMask()),dhcp:!!p.isDhcpClientOn()});")
        print("lease", lease)
        lines.extend([
            "## pools before", json.dumps(before, indent=1),
            "## setup", json.dumps(setup, indent=1),
            "## client lease", json.dumps(lease, indent=1),
            "## ipconfig /all", ipconfig,
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
