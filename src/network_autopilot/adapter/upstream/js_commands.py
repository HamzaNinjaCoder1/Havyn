"""JS command builders for the upstream bridge/extension runtime.

Security rule (inherited from this repo's non-negotiables): NEVER build
JavaScript with raw f-string interpolation of text fields. Packet Tracer runs
these commands through `new Function()`, so an unescaped device name is
arbitrary code execution. Every dynamic string goes through `json.dumps`
here; only validated ints are interpolated raw.

The JS function names (`lwAddDevice`, `lwAddLink`, `configureIosDevice`,
`configurePcIp`, `addModule`) and the enum values are the extension runtime's
verified API surface — see EXTENSION/script-engine/main.js and the upstream
generators. No PTBuilder-derived code is used here.
"""

from __future__ import annotations

import json

# PT IpcAPI enum values (verified against the extension runtime and the
# upstream device-type/connect-type constants).
DEVICE_TYPE_CODES: dict[str, int] = {
    "router": 0,
    "switch": 1,
    "cloud": 2,
    "bridge": 3,
    "hub": 4,
    "accesspoint": 7,
    "pc": 8,
    "server": 9,
    "wireless_router": 11,
    "multilayer_switch": 16,
    "laptop": 17,
    "wired_end_device": 21,
    "firewall": 26,
}

CABLE_CODES: dict[str, int] = {
    "straight": 8100,
    "cross": 8101,
    "crossover": 8101,
    "roll": 8102,
    "fiber": 8103,
    "phone": 8104,
    "cable": 8105,
    "serial": 8106,
    "auto": 8107,
    "console": 8108,
    "wireless": 8109,
    "coaxial": 8110,
    "octal": 8111,
    "cellular": 8112,
    "usb": 8113,
    "custom_io": 8114,
}


def _q(value: str) -> str:
    """Quote a string as a JS literal the way the whole product requires."""
    return json.dumps(value)


def _int(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an int, got {value!r}")
    return value


def js_add_device(name: str, device_type_code: int, model: str, x: int, y: int) -> str:
    return (
        f"lwAddDevice({_q(name)}, {_int(device_type_code, 'device_type_code')}, "
        f"{_q(model)}, {_int(x, 'x')}, {_int(y, 'y')});"
    )


def js_add_link(
    device_a: str, port_a: str, device_b: str, port_b: str, cable_code: int
) -> str:
    return (
        f"lwAddLink({_q(device_a)}, {_q(port_a)}, {_q(device_b)}, {_q(port_b)}, "
        f"{_int(cable_code, 'cable_code')});"
    )


def js_add_module(device: str, slot: str, module: str) -> str:
    return f"addModule({_q(device)}, {_q(slot)}, {_q(module)});"


def js_configure_ios(device: str, cli_block: str) -> str:
    return f"configureIosDevice({_q(device)}, {_q(cli_block)});"


def js_configure_pc_ip(
    device: str,
    dhcp: bool,
    ip: str = "",
    mask: str = "",
    gateway: str = "",
    dns: str = "",
) -> str:
    return (
        f"configurePcIp({_q(device)}, {'true' if dhcp else 'false'}, "
        f"{_q(ip)}, {_q(mask)}, {_q(gateway)}, {_q(dns)});"
    )


def js_device_exists_expr(name: str) -> str:
    """Expression evaluating to 'yes'/'no' inside the report wrapper."""
    return f"(ipc.network().getDevice({_q(name)}) ? 'yes' : 'no')"


def js_device_count_expr() -> str:
    return "ipc.network().getDeviceCount()"


def js_save_project_statements(directory: str, filename: str) -> tuple[list[str], str]:
    """Statements + final expression for saving the workspace.

    Read-back is built in: the expression itself verifies the file exists and
    reports its size, so 'OK:' results carry their own evidence. Pattern
    verified against the upstream pt_save_project implementation.
    """
    statements = [
        "var aw=ipc.appWindow();",
        f"var dir={_q(directory.strip())};",
        "if(!dir){dir=String(aw.getDefaultFileSaveLocation());}",
        "dir=String(dir).replace(/\\\\/g,'/').replace(/\\/+$/,'');",
        f"var full=dir+'/'+{_q(filename)};",
        "aw.fileSaveAsNoPrompt(full,false);",
        "var fm=ipc.systemFileManager();",
    ]
    final = "(fm.fileExists(full) ? ('OK:'+full+'|'+fm.getFileSize(full)) : ('ERR:not created '+full))"
    return statements, final


def js_both_ports_linked_expr(
    a_device: str, a_port: str, b_device: str, b_port: str
) -> str:
    """'yes' when BOTH named ports currently have a link attached.

    Uses only the verified Port surface (getPortCount/getPortAt/getName/
    getLink) behind guards, mirroring the upstream port-reader pattern.
    """
    return (
        "(function(){"
        "function linked(dn,pn){"
        "var d=ipc.network().getDevice(" + _q(dn) + ");"
        "if(!d){return false;}"
        "var n=d.getPortCount();"
        "for(var i=0;i<n;i++){"
        "var p=d.getPortAt(i);"
        "if(!p){continue;}"
        "if(String(p.getName())===" + _q(pn) + "){"
        "return (typeof p.getLink==='function') ? !!p.getLink() : false;}"
        "}return false;}"
        "return (linked(" + _q(a_device) + "," + _q(a_port) + ") && "
        "linked(" + _q(b_device) + "," + _q(b_port) + ")) ? 'yes' : 'no';"
        "})()"
    )


# -- topology read-back (deep inspection) -------------------------------------
#
# PROVENANCE: adapted from the upstream tool_registry `_LIVE_DEVICES_JS`
# (MIT, © 2026 Mateo / @Mats2208) — the pattern is verified live against PT:
# JSON.stringify works in the PT Script Engine and per-port reads use only the
# verified Port surface behind try/catch guards. One deliberate difference:
# `up` is three-valued (true/false/null) so a port whose up-state cannot be
# read is reported as unknown, not as "down".

def js_live_devices_statements() -> tuple[list[str], str]:
    """Statements + final expression reading the active topology as JSON.

    The final expression stringifies `{devices: [{name, model, ports:
    [{name, ip, mask, up, linked}]}]}`; `up` is `true`/`false`/`null`.
    """
    statements = [
        "var net=ipc.network();var n=net.getDeviceCount();var arr=[];",
        "for(var i=0;i<n;i++){",
        "var d=net.getDeviceAt(i);var pc=d.getPortCount();var ports=[];",
        "for(var j=0;j<pc;j++){",
        "var p=d.getPortAt(j);var ip='';var mask='';var up=null;var linked=false;",
        "try{ip=p.getIpAddress()||'';}catch(pe){}",
        "try{mask=p.getSubnetMask()||'';}catch(pe){}",
        "try{up=(typeof p.isPortUp==='function')?p.isPortUp():null;}catch(pe){}",
        "try{linked=(p.getLink()!=null);}catch(pe){}",
        "ports.push({name:p.getName(),ip:ip,mask:mask,up:up,linked:linked});",
        "}",
        "arr.push({name:d.getName(),model:d.getModel(),ports:ports});",
        "}",
    ]
    final = "JSON.stringify({devices:arr})"
    return statements, final


# -- real console ping (data-plane flows) --------------------------------------
#
# PROVENANCE: adapted from the upstream module-level `console_ping_arm_js` /
# `console_ping_poll_js` (MIT, © 2026 Mateo / @Mats2208). Those builders call
# reportResult() themselves; these are reshaped into IIFEs so they fit this
# package's single-report wrapper protocol. The verified knowledge is kept:
# `getCommandLine()` works on routers AND hosts (getCommandPrompt() exists on
# hosts only), the console must be primed out of the yes/no setup dialog, and
# statistic-block COUNTING (not output length) tells a new ping from history.

_CONSOLE_PRIME_JS = (
    "var p=String(cl.getPrompt()||'');"
    "if(p.indexOf('[yes/no]')>=0){cl.enterCommand('no');}"
    "cl.enterCommand('');"
)

_PING_STAT_MARKERS = "/Packets: Sent|Success rate/g"

# raw string: the \d and \n escapes belong to the JS regex, not Python.
_PING_STAT_BLOCK = (
    r"/Packets: Sent = \d+, Received = (\d+), Lost = (\d+)[^\n]*"
    r"|Success rate is (\d+) percent \((\d+)\/(\d+)\)/g"
)


def js_console_ping_arm_expr(device: str, target: str) -> str:
    """Prime the console, count prior stat blocks, fire `ping <target>`.

    Returns 'BASE:<n>' (how many stat blocks were already there), or
    'ERR:...' when the device has no console.
    """
    return (
        "(function(){"
        "var cl=ipc.network().getDevice(" + _q(device) + ").getCommandLine();"
        "if(!cl){return 'ERR:device without console';}"
        + _CONSOLE_PRIME_JS +
        "var o=String(cl.getOutput());"
        "var m=o.match(" + _PING_STAT_MARKERS + ");"
        "var base=m?m.length:0;"
        "cl.enterCommand(" + _q("ping " + target.strip()) + ");"
        "return 'BASE:'+base;"
        "})()"
    )


def js_console_poll_expr(device: str, base: int) -> str:
    """Poll the console until a NEW stat block appears.

    Returns 'DONE:<stat line>' once the ping finished, 'WAIT' while it is
    still running, or 'ERR:...' when the device has no console.
    """
    return (
        "(function(){"
        "var cl=ipc.network().getDevice(" + _q(device) + ").getCommandLine();"
        "if(!cl){return 'ERR:device without console';}"
        "var o=String(cl.getOutput());"
        "var m=o.match(" + _PING_STAT_MARKERS + ");"
        "var cur=m?m.length:0;"
        "if(cur>" + str(int(base)) + "){"
        "var stat=o.match(" + _PING_STAT_BLOCK + ");"
        "return 'DONE:'+(stat?stat[stat.length-1]:'no stats');"
        "}return 'WAIT';"
        "})()"
    )


def wrap_with_report(
    expression: str, report_fn_js: str, statements: list[str] | None = None
) -> str:
    """Build one queued command: define reportResult, run statements, report.

    `report_fn_js` is the upstream bridge's reportResult definition (it routes
    the answer back to /result?rid=...). The try/catch matters: an uncaught
    error inside runCode opens a modal that freezes the webview and kills the
    polling loop.
    """
    body = "".join(statements or [])
    return (
        report_fn_js
        + "reportResult((function(){try{"
        + body
        + "return String("
        + expression
        + ");}catch(e){return 'PT_ERROR: ' + e;}})());"
    )


def cable_code_for(media: str) -> int:
    code = CABLE_CODES.get(media.lower())
    if code is None:
        raise ValueError(f"unknown cable/media type: {media!r}")
    return code


def device_type_code_for(role: str) -> int:
    code = DEVICE_TYPE_CODES.get(role)
    if code is None:
        code = DEVICE_TYPE_CODES["wired_end_device"]
    return code
