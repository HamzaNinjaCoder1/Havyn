"""JS builders for the Stage-3 capabilities proven live in ``probes/``.

Kept out of ``js_commands.py`` so the Stage-3 surface is reviewable on its own
and the long-standing builders stay untouched. Every interpolated value goes
through ``_q`` (json.dumps): PT runs the result through ``new Function()``, so
an unescaped device/page string would be code execution inside the user's PT
(AGENTS rule 1).
"""

from __future__ import annotations

from .js_commands import _q


def js_configure_server_services(device: str, services: list[dict]) -> str:
    """Configure the proven Server-PT services in one round trip.

    Returns a JSON array with one ``{service, ok, ...}`` entry per intent. A
    per-service exception is caught and reported as ``ok:false`` so one bad
    service cannot hide the others or open PT's modal.
    """
    parts = [
        "var d=ipc.network().getDevice(" + _q(device) + ");",
        "if(!d){return 'PT_ERROR: no device';}",
        "var out=[];",
    ]
    for svc in services:
        kind = str(svc.get("service") or "")
        enabled = "true" if bool(svc.get("enabled", True)) else "false"
        if kind == "dns":
            records = "".join(
                "p.addIpAddress(" + _q(str(rec.get("host", ""))) + ","
                + _q(str(rec.get("address", ""))) + ");"
                "p.addARecordToNameServerDb(" + _q(str(rec.get("host", ""))) + ","
                + _q(str(rec.get("address", ""))) + ");"
                for rec in (svc.get("records") or [])
            )
            parts.append(
                "try{var p=d.getProcess('DnsServer');"
                "if(!p){out.push({service:'dns',ok:false,error:'no DnsServer'});}"
                "else{p.setEnable(" + enabled + ");" + records +
                "out.push({service:'dns',ok:true,enabled:p.isEnabled(),"
                "entries:p.getEntryCount()});}}"
                "catch(e){out.push({service:'dns',ok:false,error:String(e)});}"
            )
        elif kind in ("http", "https"):
            pages = "".join(
                "p.setPageContents(" + _q(str(url)) + "," + _q(str(body)) + ");"
                for url, body in (svc.get("pages") or {}).items()
            )
            https = "p.setHttpsEnable(true);" if kind == "https" else ""
            parts.append(
                "try{var p=d.getProcess('HttpServer');"
                "if(!p){out.push({service:'" + kind + "',ok:false,error:'no HttpServer'});}"
                "else{p.setEnable(" + enabled + ");" + https + pages +
                "out.push({service:'" + kind + "',ok:true,enabled:p.isEnabled()});}}"
                "catch(e){out.push({service:'" + kind + "',ok:false,error:String(e)});}"
            )
        elif kind == "dhcp":
            pool = svc.get("pool") or {}
            port = str(pool.get("port") or "FastEthernet0")
            try:
                max_users = int(pool.get("max_users") or 50)
            except (TypeError, ValueError):
                max_users = 50
            add_pool = (
                "p.addNewPool(" + _q(str(pool.get("name") or "POOL1")) + ","
                + _q(str(pool.get("gateway") or "0.0.0.0")) + ","
                + _q(str(pool.get("dns") or "0.0.0.0")) + ","
                + _q(str(pool.get("start") or "")) + ","
                + _q(str(pool.get("mask") or "255.255.255.0")) + ","
                + str(max_users) + ","
                + _q(str(pool.get("tftp") or "0.0.0.0")) + ","
                + _q(str(pool.get("wlc") or "0.0.0.0")) + ");"
            )
            parts.append(
                "try{var m=d.getProcess('DhcpServerMain');"
                "var p=m?m.getDhcpServerProcessByPortName(" + _q(port) + "):null;"
                "if(!p){out.push({service:'dhcp',ok:false,error:'no dhcp port process'});}"
                "else{p.setEnable(" + enabled + ");" + add_pool +
                "out.push({service:'dhcp',ok:true,enabled:p.isEnable(),"
                "pools:p.getPoolCount()});}}"
                "catch(e){out.push({service:'dhcp',ok:false,error:String(e)});}"
            )
        elif kind == "ntp":
            ref = svc.get("ref_clock_time")
            ref_js = ("p.setRefClockTime(" + _q(str(ref)) + ");") if ref else ""
            parts.append(
                "try{var p=d.getProcess('NtpServer');"
                "if(!p){out.push({service:'ntp',ok:false,error:'no NtpServer'});}"
                "else{p.setEnabled(" + enabled + ");" + ref_js +
                "out.push({service:'ntp',ok:true,enabled:p.isEnabled(),"
                "ref:p.getRefClockTime()});}}"
                "catch(e){out.push({service:'ntp',ok:false,error:String(e)});}"
            )
        elif kind == "syslog":
            parts.append(
                "try{var p=d.getProcess('SyslogServer');"
                "if(!p){out.push({service:'syslog',ok:false,error:'no SyslogServer'});}"
                "else{p.setEnable(" + enabled + ");"
                "out.push({service:'syslog',ok:true,enabled:p.isEnabled(),"
                "port:p.getPortNumber()});}}"
                "catch(e){out.push({service:'syslog',ok:false,error:String(e)});}"
            )
        elif kind == "ftp":
            parts.append(
                "try{var p=d.getProcess('FtpServer');"
                "if(!p){out.push({service:'ftp',ok:false,error:'no FtpServer'});}"
                "else{p.setEnabled(" + enabled + ");"
                "out.push({service:'ftp',ok:true,enabled:p.isEnabled()});}}"
                "catch(e){out.push({service:'ftp',ok:false,error:String(e)});}"
            )
        elif kind == "aaa":
            parts.append(
                "try{var p=d.getProcess('AcsServer');"
                "if(!p){out.push({service:'aaa',ok:false,error:'no AcsServer'});}"
                "else{p.enableACSServerService(" + enabled + ");"
                "out.push({service:'aaa',ok:true,enabled:p.isEnabled()});}}"
                "catch(e){out.push({service:'aaa',ok:false,error:String(e)});}"
            )
        else:
            parts.append(
                "out.push({service:" + _q(kind) +
                ",ok:false,error:'service is refused by the compiler'});"
            )
    parts.append("return JSON.stringify(out);")
    return "(function(){" + "".join(parts) + "})()"


def js_configure_cloud_mapping(device: str, mappings: list[dict]) -> str:
    """Map two cloud ports (DSL/cable) or two Frame Relay sub-links (serial)."""
    parts = [
        "var d=ipc.network().getDevice(" + _q(device) + ");",
        "if(!d){return 'PT_ERROR: no device';}",
        "var out=[];",
    ]
    for mapping in mappings:
        ctype = str(mapping.get("connection_type") or "dsl")
        port_a = str(mapping.get("cloud_port") or "")
        port_b = str(mapping.get("peer_cloud_port") or "")
        if ctype in ("dsl", "cable"):
            parts.append(
                "try{var dsl=d.setDslConnection(" + _q(port_a) + ","
                + ("true" if ctype == "dsl" else "false") + ");"
                "var c=d.addPortConnection(" + _q(port_a) + "," + _q(port_b) + ");"
                "out.push({type:" + _q(ctype) + ",ok:!!c,dsl:!!dsl,"
                "connections:d.getPortConnectionCount()});}"
                "catch(e){out.push({type:" + _q(ctype) +
                ",ok:false,error:String(e)});}"
            )
        else:
            sub_a = str(mapping.get("sub_link") or "s1")
            sub_b = str(mapping.get("peer_sub_link") or "s2")
            try:
                dlci_a = int(mapping.get("dlci") or 100)
                dlci_b = int(mapping.get("peer_dlci") or 200)
            except (TypeError, ValueError):
                dlci_a, dlci_b = 100, 200
            parts.append(
                "try{var p1=d.getPort(" + _q(port_a) + "),p2=d.getPort(" + _q(port_b) + ");"
                "if(!p1||!p2){out.push({type:'serial',ok:false,error:'missing port'});}"
                "else{p1.setLmiType(1);p2.setLmiType(1);"
                "p1.addSubLink(" + _q(sub_a) + "," + str(dlci_a) + ");"
                "p2.addSubLink(" + _q(sub_b) + "," + str(dlci_b) + ");"
                "var c=d.addSubLinkConnection(" + _q(port_a) + "," + _q(sub_a) + ","
                + _q(port_b) + "," + _q(sub_b) + ");"
                "out.push({type:'serial',ok:!!c,"
                "connections:d.getSubLinkConnectionCount()});}}"
                "catch(e){out.push({type:'serial',ok:false,error:String(e)});}"
            )
    parts.append("return JSON.stringify(out);")
    return "(function(){" + "".join(parts) + "})()"


def js_configure_wireless_ssid(device: str, spec: dict) -> str:
    """Configure the AP-side SSID/security/channel and read it back.

    ``channel`` is the real 2.4 GHz channel (1-11); PT's StandardChannel enum
    is zero-based, so channel 11 is enum 10. Channels above 11 are rejected by
    the compiler, not guessed here.
    """
    security = str(spec.get("security") or "wpa2")
    auth = 6 if security == "open" else 4
    enc = 0 if security == "open" else 4
    key = str(spec.get("passphrase") or "")
    key_js = (
        "p.getWpaProcess().setKey(" + _q(key) + ");"
        if security == "wpa2" and key else ""
    )
    channel = spec.get("channel")
    channel_js = ""
    if channel:
        channel_js = "p.setStandardChannel(" + str(int(channel) - 1) + ");"
    return (
        "(function(){var d=ipc.network().getDevice(" + _q(device) + ");"
        "if(!d){return 'PT_ERROR: no device';}"
        "try{var p=d.getProcess('WirelessServer');"
        "if(!p){return 'PT_ERROR: no WirelessServer';}"
        "p.setPort('Port 1');p.setSsid(" + _q(str(spec.get("ssid") or "")) + ");"
        "p.setAuthenType(" + str(auth) + ");p.setEncryptType(" + str(enc) + ");"
        + key_js + channel_js +
        "p.setSsidBrdCastEnabled(true);p.resetAllAssociations();"
        "return JSON.stringify({ssid:String(p.getSsid()),"
        "auth:String(p.getAuthenType()),enc:String(p.getEncryptType()),"
        "channel:String(p.getStandardChannel())});}"
        "catch(e){return 'PT_ERROR: '+e;}})()"
    )


def js_rsa_key_start(device: str) -> str:
    """Prime the console, enter global config, type the key command once."""
    return (
        "(function(){var d=ipc.network().getDevice(" + _q(device) + ");"
        "if(!d){return 'PT_ERROR: no device';}"
        "var cl=d.getCommandLine();if(!cl){return 'PT_ERROR: no console';}"
        "var p=String(cl.getPrompt()||'');"
        "if(p.indexOf('[yes/no]')>=0){cl.enterCommand('no');}"
        "cl.enterCommand('');cl.enterCommand('enable');"
        "cl.enterCommand('configure terminal');"
        "cl.enterCommand('crypto key generate rsa');"
        "return 'typed';})()"
    )


def js_rsa_key_bits(device: str, bits: int = 1024) -> str:
    """Answer the modulus prompt and leave global config."""
    return (
        "(function(){var d=ipc.network().getDevice(" + _q(device) + ");"
        "if(!d){return 'PT_ERROR: no device';}"
        "var cl=d.getCommandLine();if(!cl){return 'PT_ERROR: no console';}"
        "cl.enterCommand(" + _q(str(int(bits))) + ");"
        "cl.enterCommand('end');"
        "return String(cl.getOutput()).slice(-600);})()"
    )
