#!/usr/bin/env python
"""Router NTP/Syslog against a Server-PT (S3.A16, S3.D8).

Measured: the Server-PT `NtpServer`/`SyslogServer` processes accept their
configuration and read back, the router's `logging host` delivers real entries
(host IP, timestamp, message), and PT does not implement router NTP
synchronisation (`show ntp status` is invalid and the clock stays at 1993).
The verdict is therefore "logs proven, time sync unobservable", not a blanket
PASS.

    python probes/stage3_ntp_syslog_lab.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.ntp-syslog.txt"
SRV, RTR = "s3p-ntp-srv", "s3p-ntp-rtr"


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
    for name in (SRV, RTR):
        try:
            adapter.delete_device(name)
        except Exception:
            pass
    created: list[str] = []
    lines = [
        f"# NTP/Syslog lab @ "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]
    try:
        for name, role, model, pos in (
            (SRV, "server", "Server-PT", (300, 250)),
            (RTR, "router", "2911", (600, 250)),
        ):
            receipt = adapter.create_device(name, role, model_hint=model, position=pos)
            print("create", name, receipt.result.value, receipt.error)
            if receipt.result.value != "success":
                return 2
            created.append(name)
        time.sleep(4)
        link = adapter.create_link(SRV, "FastEthernet0", RTR,
                                   "GigabitEthernet0/0", media="cross")
        print("link", link.result.value, link.error)
        time.sleep(2)
        adapter.configure_host(SRV, dhcp=False, ip="10.76.0.2",
                               mask="255.255.255.0", gateway="10.76.0.1")
        cfg = adapter.configure_ios(RTR, [
            "hostname s3p-ntp-rtr",
            "interface GigabitEthernet0/0",
            " ip address 10.76.0.1 255.255.255.0",
            " no shutdown",
            " exit",
            "ntp server 10.76.0.2",
            "logging host 10.76.0.2",
            "logging trap debugging",
            "service timestamps log datetime msec",
            "clock timezone UTC 0",
        ])
        print("router cfg", cfg.result.value, cfg.error)
        ntp = js(adapter, SRV,
                 "var p=d.getProcess('NtpServer');p.setEnabled(true);"
                 "p.setRefClockTime('Mon Jan 01 2024 12:00:00.000 UTC');"
                 "return JSON.stringify({enabled:p.isEnabled(),"
                 "ref:p.getRefClockTime()});")
        syslog = js(adapter, SRV,
                    "var p=d.getProcess('SyslogServer');p.setEnable(true);"
                    "p.clearAllSysLogEntries();return JSON.stringify({"
                    "enabled:p.isEnabled(),port:p.getPortNumber(),"
                    "entries:p.getAllEntries().length});")
        print("server ntp", ntp)
        print("server syslog", syslog)
        time.sleep(25)
        clock = adapter._console_show_full(RTR, "show clock", timeout=25)
        ntp_status = adapter._console_show_full(RTR, "show ntp status", timeout=25)
        # Generate a log the server can receive.
        adapter.configure_ios(RTR, ["interface GigabitEthernet0/1",
                                    " shutdown", " no shutdown"])
        time.sleep(8)
        entries = js(adapter, SRV,
                     "var p=d.getProcess('SyslogServer');var e=p.getAllEntries();"
                     "var out=[];for(var i=0;i<e.length;i++){out.push({"
                     "dt:String(e[i].dateTime),host:String(e[i].hostIpAddress),"
                     "priority:Number(e[i].priority),message:String(e[i].message)"
                     "});}return JSON.stringify({count:e.length,"
                     "entries:out.slice(-8)});")
        print("entries", entries)
        lines.extend([
            "## server ntp", json.dumps(ntp, indent=1),
            "## server syslog", json.dumps(syslog, indent=1),
            "## show clock",
            str(clock.value) if clock.is_known else str(clock.detail),
            "## show ntp status",
            str(ntp_status.value) if ntp_status.is_known else str(ntp_status.detail),
            "## syslog entries", json.dumps(entries, indent=1),
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
