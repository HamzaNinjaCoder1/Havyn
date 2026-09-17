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
import re

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
    device_a: str, port_a: str, device_b: str, port_b: str, cable: str
) -> str:
    """Global addLink with a cable NAME — the upstream-verified signature
    (tool_registry `addLink("d1","p1","d2","p2","straight")` + explicit
    getLink() read-back). Found live: lwAddLink(..., 8107) answers `false`
    for PC FastEthernet ports, so it is not used here."""
    return (
        f"addLink({_q(device_a)}, {_q(port_a)}, {_q(device_b)}, {_q(port_b)}, "
        f"{_q(cable)});"
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

    The inner helper takes the device/port NAMES as its own JS parameters;
    the Python values are interpolated only at the two call sites (found
    live: interpolating inside the helper referenced undefined names).
    """
    return (
        "(function(){"
        "function linked(dn,pn){"
        "var d=ipc.network().getDevice(dn);"
        "if(!d){return false;}"
        "var n=d.getPortCount();"
        "for(var i=0;i<n;i++){"
        "var p=d.getPortAt(i);"
        "if(!p){continue;}"
        "if(String(p.getName())===pn){"
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


# -- console show-command read-back (routing / VLAN control plane) -------------
#
# Verified live on PT 9.0.1: only getCommandLine() exposes the routing table
# and VLAN database, so read-back is an arm/poll console exchange. The primed
# console (initial-config dialog + `enable`) and the prompt shape
# (`<hostname>#`) were observed on a 2911 and a Switch-PT.

def js_console_command_arm_statements(device: str, command: str) -> tuple[list[str], str]:
    """Prime the console, enter `enable`, run `command`; final = 'BASE:<len>'.

    Statements/final split (not an IIFE) because `enterCommand` must run as a
    statement — found live, appending statements after a `return` inside
    String(...) is a parse error.
    """
    statements = [
        "var cl=ipc.network().getDevice(" + _q(device) + ").getCommandLine();",
        "if(!cl){return 'ERR:device without console';}",
        "var p=String(cl.getPrompt()||'');",
        "if(p.indexOf('[yes/no]')>=0){cl.enterCommand('no');}",
        "cl.enterCommand('');",
        "var base=String(cl.getOutput()).length;",
        "cl.enterCommand('enable');",
        "cl.enterCommand(" + _q(command.strip()) + ");",
    ]
    return statements, "'BASE:' + base"


def js_console_collect_expr(device: str, base: int) -> str:
    """Poll for the show-command tail.

    The command is considered finished when the last non-empty console line
    ends with `#` (the privileged prompt, verified live). Returns
    'DONE:<tail>' or 'WAIT'; 'ERR:...' when there is no console.
    """
    return (
        "(function(){"
        "var cl=ipc.network().getDevice(" + _q(device) + ").getCommandLine();"
        "if(!cl){return 'ERR:device without console';}"
        "var o=String(cl.getOutput());"
        "if(o.length>" + str(int(base)) + "){"
        "var tail=o.slice(" + str(int(base)) + ");"
        "var lines=tail.split('\\n').filter(function(l){return l.trim().length>0;});"
        "var last=lines.length?lines[lines.length-1].trim():'';"
        "if(last.charAt(last.length-1)==='#'){return 'DONE:'+tail;}"
        "}return 'WAIT';"
        "})()"
    )


# -- parsers for console output -------------------------------------------------
#
# Deterministic regex parsing of REAL captured output (see the golden fixtures
# in tests/autopilot/test_control_plane_readback.py). Anything unparseable is
# reported as UNKNOWN by the caller — never guessed.

_IF_ABBREV = {
    "Fa": "FastEthernet",
    "Gi": "GigabitEthernet",
    "Te": "TenGigabitEthernet",
    "Eth": "Ethernet",
    "Se": "Serial",
    "Vlan": "VLAN",
}


def expand_if_name(name: str) -> str:
    """Fa0/1 -> FastEthernet0/1 (PT abbreviates port names in console output)."""
    for abbrev, full in _IF_ABBREV.items():
        if name.startswith(abbrev) and not name.startswith(full):
            return full + name[len(abbrev):]
    return name


def parse_show_ip_route(output: str) -> list[dict]:
    """Parse a real `show ip route` table into structured route records."""
    routes: list[dict] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("Codes:") or line.startswith("Gateway of"):
            continue
        # static/default: 'S    10.9.0.0/24 [1/0] via 192.168.77.11, GigabitEthernet0/0'
        m = re.match(r"^S\*?\s+(\d+\.\d+\.\d+\.\d+)/(\d+)\s+\[[^\]]+\]\s+via\s+(\d+\.\d+\.\d+\.\d+)(?:,\s*(\S+))?$", line)
        if m:
            routes.append(
                {
                    "kind": "default" if line.startswith("S*") else "static",
                    "network": m.group(1),
                    "prefix": int(m.group(2)),
                    "next_hop": m.group(3),
                    "interface": expand_if_name(m.group(4) or "") or None,
                }
            )
            continue
        # connected/local: 'C    10.0.1.0/24 is directly connected, GigabitEthernet0/0'
        m = re.match(r"^([CL])\s+(\d+\.\d+\.\d+\.\d+)/(\d+)\s+is directly connected,\s*(\S+)$", line)
        if m:
            routes.append(
                {
                    "kind": "connected" if m.group(1) == "C" else "local",
                    "network": m.group(2),
                    "prefix": int(m.group(3)),
                    "next_hop": None,
                    "interface": expand_if_name(m.group(4)),
                }
            )
    return routes


def parse_show_vlan_brief(output: str) -> list[dict]:
    """Parse a real `show vlan brief` table into structured VLAN records.

    Multi-line port lists (continuation rows) are folded into their VLAN.
    """
    vlans: list[dict] = []
    current: dict | None = None
    row = re.compile(r"^(\d{1,4})\s+(\S+)\s+(\S+)\s*(.*)$")
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("VLAN Name") or line.strip().startswith("----"):
            continue
        m = row.match(line.strip())
        if m and int(m.group(1)) <= 4094:
            current = {
                "id": int(m.group(1)),
                "name": m.group(2),
                "status": m.group(3),
                "ports": [expand_if_name(p) for p in m.group(4).split(",") if p.strip()],
            }
            vlans.append(current)
        elif current is not None and line.startswith(" " * 20):
            # continuation of the port list for the previous VLAN row
            current["ports"].extend(
                expand_if_name(p) for p in line.strip().split(",") if p.strip()
            )
    return vlans


def wrap_with_report(
    expression: str, report_fn_js: str, statements: list[str] | None = None
) -> str:
    """Build one queued command: define reportResult, run statements, report.

    `report_fn_js` is the upstream bridge's reportResult definition (it routes
    the answer back to /result?rid=...). The try/catch matters: an uncaught
    error inside runCode opens a modal that freezes the webview and kills the
    polling loop.

    The expression goes inside String(...), so it must be an EXPRESSION: some
    js_* builders return statements with a trailing `;` (found live — PT
    answered `SyntaxError: Parse error`), so a trailing semicolon is stripped
    here at the single choke point every channel shares.
    """
    expression = expression.strip().rstrip(";").strip()
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


# Cable NAMES accepted by the global addLink (upstream CABLE_TYPES keys).
CABLE_NAMES: dict[str, str] = {
    "straight": "straight",
    "cross": "cross",
    "crossover": "cross",
    "roll": "roll",
    "fiber": "fiber",
    "phone": "phone",
    "cable": "cable",
    "coaxial": "coaxial",
    "auto": "auto",
    "console": "console",
    "serial": "serial",
    "wireless": "wireless",
}


def cable_name_for(media: str) -> str:
    name = CABLE_NAMES.get(media.lower())
    if name is None:
        raise ValueError(f"unknown cable/media type: {media!r}")
    return name


def device_type_code_for(role: str) -> int:
    code = DEVICE_TYPE_CODES.get(role)
    if code is None:
        code = DEVICE_TYPE_CODES["wired_end_device"]
    return code
