#!/usr/bin/env python
"""App-level DNS and HTTP proof (S3.D1, S3.D2, S3.D3, S3.H2).

The Server-PT exposes `DnsServer` and `HttpServer` processes; the PC exposes
`DnsClient` and `HttpClient`. This lab configures both sides, reads the
configuration back, then proves the service at the application layer:

* DNS  - the PC resolves `server.local` to the server address, and a console
         `ping server.local` uses the resolver.
* HTTP - the PC fetches the configured page and the body comes back.
* H2   - the PC's configured DNS server reads back from `DnsClient` and from
         `ipconfig /all`.

Measured details that shaped the calls:
* `DnsServer.addIpAddress(name, ip)` fills the host table that resolution
  uses; `addARecordToNameServerDb` writes the zone database instead.
* `HttpServer.setPageContents("index.html", body)` serves at that URL;
  `getPage("/")` throws `File not exist`, so the lab asks for the page it set.
* `HttpClient.getLastPageContent()` is populated asynchronously.

    python probes/stage3_dns_http_lab.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter

RAW = Path(__file__).resolve().parent / "raw" / "S3.dns-http.txt"
SRV, PC = "s3p-dns-srv", "s3p-dns-pc"
PAGE = "<html><body>HAVYN-APP-PROOF</body></html>"


def send(adapter: BridgeAdapter, device: str, code: str, wait: float = 25.0) -> str:
    expr = (
        "(function(){var d=ipc.network().getDevice(" + repr(device) + ");"
        "if(!d){return 'E:no-device';}try{" + code + "}catch(e){return 'E:'+e;}})()"
    )
    answer = adapter.send_expression(expr, wait=wait)
    return str(answer.value) if answer.is_known else f"UNKNOWN:{answer.detail}"


def js(adapter: BridgeAdapter, device: str, code: str) -> object:
    """Run statements that end in `return JSON.stringify(...)`."""
    text = send(adapter, device, code)
    try:
        return json.loads(text)
    except ValueError:
        return text


def pc_cli(adapter: BridgeAdapter, command: str, timeout: float = 30.0) -> str:
    before = send(adapter, PC, "var cl=d.getCommandLine();return String(cl.getOutput()).length;")
    send(adapter, PC, "d.getCommandLine().enterCommand(" + json.dumps(command) + ");return 'typed';")
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        time.sleep(1.5)
        last = send(adapter, PC, "var cl=d.getCommandLine();return String(cl.getOutput());")
        if "--More--" in last[-300:]:
            send(adapter, PC, "d.getCommandLine().enterCommand(' ');return 'page';")
            continue
        tail = last[-500:]
        if tail.rstrip().endswith((">", "#")):
            break
    return last


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
        f"# DNS/HTTP lab @ "
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
        srv_cfg = adapter.configure_host(
            SRV, dhcp=False, ip="10.88.0.2", mask="255.255.255.0",
            gateway="10.88.0.1", dns="10.88.0.2",
        )
        pc_cfg = adapter.configure_host(
            PC, dhcp=False, ip="10.88.0.10", mask="255.255.255.0",
            gateway="10.88.0.2", dns="10.88.0.2",
        )
        print("hosts", srv_cfg.result.value, pc_cfg.result.value)
        # -- server services -------------------------------------------------
        # Both tables are needed: the host table answers a local lookup and the
        # zone DB answers the resolver's network query (measured: with only one
        # of them, `ping server.local` says "could not find host").
        dns_on = js(adapter, SRV,
                    "var p=d.getProcess('DnsServer');p.setEnable(true);"
                    "var host=p.addIpAddress('server.local','10.88.0.2');"
                    "var zone=p.addARecordToNameServerDb('server.local','10.88.0.2');"
                    "return JSON.stringify({enabled:p.isEnabled(),"
                    "hostAdded:host,zoneAdded:zone,"
                    "exists:p.isDomainNameExisted('server.local'),"
                    "resolved:String(p.getIpAddOfDomain('server.local')),"
                    "entries:p.getEntryCount(),zoneSize:p.getSizeOfNameServerDb()});")
        http_on = js(adapter, SRV,
                     "var p=d.getProcess('HttpServer');p.setEnable(true);"
                     "p.setPageContents('index.html'," + json.dumps(PAGE) + ");"
                     "return JSON.stringify({enabled:p.isEnabled(),"
                     "page:p.getPage('index.html'),port:p.getPortNumber()});")
        print("dns", dns_on)
        print("http", http_on)
        time.sleep(3)
        # -- PC app-level proof ---------------------------------------------
        # `getIpOfHost` reads the client's local table; the resolver proof is
        # the console (nslookup/ping) below, which asks the server over the wire.
        dns_read = js(adapter, PC,
                      "var p=d.getProcess('DnsClient');"
                      "p.setServerIp('10.88.0.2');p.setEnabled(true);"
                      "return JSON.stringify({server:String(p.getServerIp()),"
                      "enabled:p.isEnabled()});")
        go = js(adapter, PC,
                "var p=d.getProcess('HttpClient');"
                "return JSON.stringify({ok:p.go('http://10.88.0.2/index.html')});")
        time.sleep(5)
        http_read = js(adapter, PC,
                       "var p=d.getProcess('HttpClient');"
                       "return JSON.stringify({body:p.getLastPageContent()});")
        print("dns_read", dns_read)
        print("http_read", http_read)
        nslookup = pc_cli(adapter, "nslookup server.local")
        ping = pc_cli(adapter, "ping server.local")
        ipconfig = pc_cli(adapter, "ipconfig /all")
        lines.extend([
            "## server dns", json.dumps(dns_on, indent=1),
            "## server http", json.dumps(http_on, indent=1),
            "## pc dns", json.dumps(dns_read, indent=1),
            "## pc http go", json.dumps(go, indent=1),
            "## pc http body", json.dumps(http_read, indent=1),
            "## pc console: nslookup server.local", nslookup[-2000:],
            "## pc console: ping server.local", ping[-2000:],
            "## pc console: ipconfig /all", ipconfig[-2000:],
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
