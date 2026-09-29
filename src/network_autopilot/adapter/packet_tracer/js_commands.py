"""JS command builders for the local Packet Tracer extension runtime.

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
    "repeater": 5,
    "accesspoint": 7,
    "pc": 8,
    "server": 9,
    "printer": 10,
    "wireless_router": 11,
    "ip_phone": 12,
    "modem": 13,
    "laptop": 17,
    "tablet": 18,
    "pda": 19,
    "wireless_end_device": 20,
    "wired_end_device": 21,
    # PT's generic "end device" chassis is a wired end device (21).
    "end_device": 21,
    "tv": 22,
    "voip": 23,
    "analog_phone": 24,
    "firewall": 26,
    "iot": 27,
    "home_gateway": 28,
    "cell_tower": 29,
    "central_office": 31,
    "sniffer": 33,
    "mcu": 34,
    "sbc": 35,
    "thing": 36,
    "embedded_server": 38,
    "multilayer_switch": 16,
    "l3_switch": 16,
    "splitter": 6,
    "wlc": 7,
    "patch_panel": 1,
    "wall_mount": 1,
    "power_dist": 1,
    "meraki": 0,
    "network_controller": 9,
}

# Models whose PT device type differs from their category default (verified
# live, probe _probe_wiot12): a Meraki MX is a router chassis while a Meraki
# Server is a server, even though the catalog groups both as "meraki".
MODEL_TYPE_OVERRIDES: dict[str, int] = {
    "Meraki-Server": 9,
    # A Meraki MX is a router chassis even though the catalog groups the
    # Meraki family together, so it needs the router type rather than the
    # group's default.
    "Meraki-MX65W": 0,
    # A cable modem is PT type 14, the DSL modem 13, though both are "modem".
    "Cable-Modem-PT": 14,
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


# -- batched builders --------------------------------------------------------
#
# A round-trip to Packet Tracer costs far more than the work inside one call,
# so independent operations are sent together and PT runs them in a single
# runCode. These build ONE command that creates many devices (or many links)
# and reports per-item results, so a partial failure still names which item
# failed instead of collapsing the whole batch into "false".


def js_batch_add_devices(
    devices: list[tuple[str, int, str, int, int]],
) -> tuple[list[str], str]:
    """Create many devices in one command.

    ``devices`` is ``[(name, type_code, model, x, y)]``. Returns
    ``(statements, final_expression)``: the statements do the work, the
    expression returns a JSON array of per-device outcomes. Each device is
    wrapped so one failure does not stop the rest — a partial batch is normal
    (a model that does not exist in PT) and the read-back has to show it.
    """
    stmts: list[str] = ["var __mcpResults=[];"]
    for name, type_code, model, x, y in devices:
        stmts.append("try{")
        stmts.append(f"  __mcpResults.push({{name:{_q(name)},ok:!!{js_add_device(name, type_code, model, x, y)[:-1]}}});")
        stmts.append("}catch(e){")
        stmts.append(f"  __mcpResults.push({{name:{_q(name)},ok:false,error:''+e}});")
        stmts.append("}")
    final = "JSON.stringify(__mcpResults)"
    return stmts, final


def js_batch_add_links(
    links: list[tuple[str, str, str, str, str]],
) -> tuple[list[str], str]:
    """Create many links in one command. ``links`` is ``(a, ap, b, bp, cable)``."""
    stmts: list[str] = ["var __mcpLinkResults=[];"]
    for a, ap, b, bp, cable in links:
        stmts.append("try{")
        stmts.append(
            f"  __mcpLinkResults.push({{link:{_q(a + ':' + ap + '-' + b + ':' + bp)},"
            f"ok:!!{js_add_link(a, ap, b, bp, cable)[:-1]}}});"
        )
        stmts.append("}catch(e){")
        stmts.append(
            f"  __mcpLinkResults.push({{link:{_q(a + ':' + ap + '-' + b + ':' + bp)},"
            "ok:false,error:''+e});"
        )
        stmts.append("}")
    final = "JSON.stringify(__mcpLinkResults)"
    return stmts, final



# -- wireless association ----------------------------------------------------
#
# Packet Tracer creates the association itself: once a host with a wireless
# NIC and an access point share the workspace, a link of class `Antenna`
# appears (measured live in _probe_wifi_matrix / _probe_wifi_assoc*). The
# helpers below do the two things that DO have to be asked for - install the
# radio, and read the association back - so nothing has to guess whether the
# client joined.

def js_install_wireless_radio(
    device: str, module: str, slot: str, interface: str
) -> str:
    """Give a host a wireless NIC, replacing the wired one in that slot.

    `addModule` returns false while the wired NIC still occupies the slot, so
    the removal comes first (the same order the extension's
    swapLaptopToWireless uses, verified live).
    """
    return (
        "(function(){"
        "var d=ipc.network().getDevice(" + _q(device) + ");"
        "if(!d){return 'MCPWIRELESSRADIO:no-device';}"
        "var ports=[];try{ports=d.getPorts()||[];}catch(e){}"
        "for(var i=0;i<ports.length;i++){"
        "try{if(ports[i]===" + _q(interface) + "){return 'MCPWIRELESSRADIO:already';}}catch(e){}}"
        "try{d.setPower(false);}catch(e){}"
        "try{d.removeModule(" + _q(slot) + ");}catch(e){}"
        "var t=allModuleTypes[" + _q(module) + "];"
        "if(t===undefined){return 'MCPWIRELESSRADIO:unknown-module';}"
        "var r=d.addModule(" + _q(slot) + ",t," + _q(module) + ");"
        "try{d.setPower(true);}catch(e){}"
        "try{if(typeof d.skipBoot==='function'){d.skipBoot();}}catch(e){}"
        "return 'MCPWIRELESSRADIO:'+String(r);"
        "})();"
    )


def js_wireless_association(device: str, interface: str) -> str:
    """Read the client's association: the antenna peer, or `not-associated`."""
    return (
        "(function(){"
        "var d=ipc.network().getDevice(" + _q(device) + ");"
        "if(!d){return 'MCPWIFIASSOC:{\"associated\":false,\"reason\":\"no-device\"}';}"
        "var p=null;try{p=d.getPort(" + _q(interface) + ");}catch(e){}"
        "if(!p){return 'MCPWIFIASSOC:{\"associated\":false,\"reason\":\"no-interface\"}';}"
        "var out={associated:false,client:" + _q(device) + ",interface:" + _q(interface) + "};"
        "try{out.port_up=!!p.isPortUp();}catch(e){out.port_up=null;}"
        "try{out.is_wireless=!!p.isWirelessPort();}catch(e){out.is_wireless=null;}"
        "var l=null;try{l=p.getLink();}catch(e){}"
        "if(!l){out.reason='no-link';return 'MCPWIFIASSOC:'+JSON.stringify(out);}"
        "try{out.link_class=String(l.getClassName());}catch(e){out.link_class=null;}"
        "if(out.link_class!=='Antenna'){out.reason='link-not-antenna';"
        "return 'MCPWIFIASSOC:'+JSON.stringify(out);}"
        "try{out.connection_type=String(l.getConnectionType());}catch(e){}"
        "try{var n=l.getReceiverCount();out.receiver_count=Number(n);"
        "if(n>0){var r=l.getReceiverAt(0);var rp=r.getPort();"
        "out.peer_device=String(rp.getOwnerDevice().getName());"
        "out.peer_interface=String(rp.getName());}}catch(e){out.peer_error=String(e);}"
        "out.associated=true;"
        "return 'MCPWIFIASSOC:'+JSON.stringify(out);})();"
    )


def js_set_wireless_channel(device: str, port: str, channel: int) -> str:
    """Put a radio on a specific channel.

    This is what makes the association deterministic. A fresh client does not
    necessarily pick the access point the topology asked for: PT hands it
    whatever radio it finds first, including antenna objects left behind by
    devices that were deleted. Putting both radios on the same explicit
    channel and re-scanning lands the client on the intended AP (measured live
    in _probe_wifi_channel: with both on channel 11 the client joined the
    requested AP, while on the default channel it joined a stale one).
    """
    return (
        "(function(){"
        "var d=ipc.network().getDevice(" + _q(device) + ");"
        "if(!d){return 'MCPWIFI:no-device';}"
        "var p=null;try{p=d.getPort(" + _q(port) + ");}catch(e){}"
        "if(!p){return 'MCPWIFI:no-port';}"
        "try{p.setChannel(" + str(_int(channel, "channel")) + ");"
        "return 'MCPWIFI:channel='+String(p.getChannel());}"
        "catch(e){return 'MCPWIFI:ERR '+e;}})();"
    )


def js_power_cycle(device: str) -> str:
    """Power-cycle a device so its radio re-scans for an access point."""
    return (
        "(function(){"
        "var d=ipc.network().getDevice(" + _q(device) + ");"
        "if(!d){return 'MCPWIFI:no-device';}"
        "try{d.setPower(false);}catch(e){}"
        "try{d.setPower(true);}catch(e){}"
        "try{if(typeof d.skipBoot==='function'){d.skipBoot();}}catch(e){}"
        "return 'MCPWIFI:power-cycled';})();"
    )


def js_antenna_links() -> str:
    """Every association in the workspace, as `device:interface` entries."""
    return (
        "(function(){"
        "var net=ipc.network();var out=[];"
        "for(var i=0;i<net.getLinkCount();i++){"
        "var l=net.getLinkAt(i);"
        "try{if(l.getClassName()!=='Antenna'){continue;}"
        "var p=l.getPort();"
        "out.push(String(p.getOwnerDevice().getName())+':'+String(p.getName()));"
        "}catch(e){}}"
        "return 'MCPWIFIANT:'+JSON.stringify(out);})();"
    )


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


def js_set_port_ip(device: str, port: str, ip: str, mask: str) -> str:
    """Set one port's IPv4 address through the port API (#10).

    The Linksys-WRT300N answers ``configureIosDevice`` with ``true`` and
    changes nothing (live probe), but ``port.setIpSubnetMask`` moves the
    address on its Vlan1 SVI and the read-back shows it. Returns
    ``MCPNET:set`` on success and a named reason otherwise, so the
    interpreter can tell a refusal from a timeout.
    """
    return (
        "(function(){"
        "var d=ipc.network().getDevice(" + _q(device) + ");"
        "if(!d){return 'MCPNET:no-device';}"
        "var p=d.getPort(" + _q(port) + ");"
        "if(!p){return 'MCPNET:no-port';}"
        "p.setIpSubnetMask(" + _q(ip) + "," + _q(mask) + ");"
        "return 'MCPNET:set';})();"
    )


def js_delete_device_statements(device: str) -> tuple[list[str], str]:
    """Delete a device from PT, with read-back.

    Returns (statements, final_expression) for wrap_with_report: the
    statements do the work and leave `__out`; the expression returns it. The
    wrapper already supplies reportResult, so the body must NOT call it.

    Both views have to be cleared, and live PT shows why: devices are created
    through the logical workspace (`lwAddDevice`), so a logical-only
    `removeDevice` can answer "deleted" and leave the device in the model,
    where it keeps showing up in every later read (measured in
    _probe_delete_mech: logical remove alone left it, global deleteDevice
    followed by the logical remove took it out for good). The read-back is
    what decides, so the receipt can be CONFIRMED rather than just "sent".
    """
    name = _q(device)
    statements = [
        "var __out='UNKNOWN';",
        "try {",
        f"  var dev = ipc.network().getDevice({name});",
        "  if (!dev) { __out='ABSENT:device not found'; }",
        "  else {",
        "    var devName = dev.getName();",
        # 1. the model-level delete (what the GUI uses),
        "    try { deleteDevice(devName); } catch(e1) {}",
        # 2. and the logical workspace it was created in,
        "    try { ipc.appWindow().getActiveWorkspace().getLogicalWorkspace()",
        "      .removeDevice(devName); } catch(e2) {}",
        # 3. and again, because either alone can leave the other holding it,
        "    try { deleteDevice(devName); } catch(e3) {}",
        f"    var still = ipc.network().getDevice({name});",
        "    __out = still ? 'ERROR:device still present after delete'"
        "                 : 'OK:deleted';",
        "  }",
        "} catch(e) { __out='ERROR:' + e; }",
    ]
    return statements, "__out"


def js_batch_delete_devices(devices: list[str]) -> tuple[list[str], str]:
    """Delete many devices in ONE command, with the same per-device read-back.

    Teardown is the one place where a round-trip per device does not scale: a
    sixty-device build deleted one at a time overran the cleanup budget and
    leaked half the topology into the workspace, which then made every later
    PT run slow enough that existence checks started timing out.

    Each device is wrapped on its own so one stubborn name cannot abort the
    rest, and each is re-checked after the delete exactly like the single-device
    path - the read-back, not the call, is what decides.
    """
    stmts: list[str] = ["var __mcpDel=[];"]
    for device in devices:
        name = _q(device)
        stmts.append("try{")
        stmts.append(f"  var d=ipc.network().getDevice({name});")
        stmts.append("  if(!d){__mcpDel.push({name:" + name + ",ok:true,note:'absent'});}")
        stmts.append("  else{")
        stmts.append("    var n=d.getName();")
        stmts.append("    try{deleteDevice(n);}catch(e1){}")
        stmts.append("    try{ipc.appWindow().getActiveWorkspace()"
                     ".getLogicalWorkspace().removeDevice(n);}catch(e2){}")
        stmts.append("    try{deleteDevice(n);}catch(e3){}")
        stmts.append(f"    var still=ipc.network().getDevice({name});")
        stmts.append("    __mcpDel.push({name:" + name + ",ok:!still,"
                     "note:still?'still present':'deleted'});")
        stmts.append("  }")
        stmts.append("}catch(e){__mcpDel.push({name:" + name
                     + ",ok:false,note:''+e});}")
    return stmts, "JSON.stringify(__mcpDel)"


def js_delete_link_statements(
    device_a: str, port_a: str, device_b: str, port_b: str
) -> tuple[list[str], str]:
    """Delete the link on endpoint A, with read-back.

    Mirrors the live-verified upstream `pt_delete_link` exactly
    (dev.getPort(...).deleteLink()), so no PT API is guessed. Deleting one
    endpoint's cable removes the physical link; the read-back confirms the
    port no longer reports one. A missing link is ABSENT (idempotent).
    """
    a, pa = _q(device_a), _q(port_a)
    statements = [
        "var __out='UNKNOWN';",
        "try {",
        f"  var dev = ipc.network().getDevice({a});",
        "  if (!dev) { __out='ABSENT:device not found'; }",
        "  else {",
        f"    var port = dev.getPort({pa});",
        "    if (!port) { __out='ABSENT:interface not found'; }",
        "    else if (port.getLink() == null) { __out='ABSENT:no link on this interface'; }",
        "    else {",
        "      port.deleteLink();",
        "      __out = (port.getLink() == null) ? 'OK:link removed'",
        "                                      : 'ERROR:link still present';",
        "    }",
        "  }",
        "} catch(e) { __out='ERROR:' + e; }",
    ]
    return statements, "__out"


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


def js_install_module_statements(
    device: str, slot: str, module: str, expected_port: str
) -> tuple[list[str], str]:
    """Install an expansion module and confirm it by reading the port back.

    Returns (statements, final_expression) for the report wrapper. `addModule`
    is the extension helper that power-cycles the device (some modules refuse
    to install while powered on) and returns true/false.

    The read-back is real: after install, the port the module is supposed to
    expose must appear in `device.getPorts()`. If addModule answers true but
    the port is still not visible, the result is `OK:installed:port-pending`
    — executed but NOT confirmed, so the receipt stays unconfirmed and the
    link operation that depends on it surfaces the real failure instead of
    inheriting a false green.
    """
    statements = [
        "var __out='UNKNOWN';",
        "try {",
        f"  var dev = ipc.network().getDevice({_q(device)});",
        "  if (!dev) { __out='ERROR:device not found'; }",
        "  else {",
        f"    var ok = addModule({_q(device)}, {_q(slot)}, {_q(module)});",
        "    var found = false;",
        "    if (typeof dev.getPorts === 'function') {",
        "      var ports = dev.getPorts();",
        "      for (var i = 0; i < ports.length; i++) {",
        f"        if (ports[i] === {_q(expected_port)}) {{ found = true; break; }}",
        "      }",
        "    }",
        f"    __out = !ok ? 'ERROR:addModule returned false'",
        f"      : found ? 'OK:installed:{expected_port}'",
        "      : 'OK:installed:port-pending';",
        "  }",
        "} catch(e) { __out='ERROR:' + e; }",
    ]
    return statements, "__out"


def js_link_peer_check_expr(
    a_device: str, a_port: str, b_device: str, b_port: str
) -> str:
    """Expression that verifies the two ports are linked TO EACH OTHER.

    The old check answered 'yes' when each port had *some* link attached —
    R1:Fa0/1→R2 and R2:Fa0/2→R3 satisfied link_exists(R1,Fa0/1,R2,Fa0/2) with
    the wrong cable in place.

    The peer proof uses the verified Port surface (getPortCount/getPortAt/
    getName/getLink) plus the Link method verified live against PT 9.0.1:
    `link.getObjectUuid()` answers the SAME uuid from both endpoints of one
    physical link, so uuid equality is proof that A and B are that link's two
    endpoints. (Object identity is NOT usable: getLink() returns a fresh
    wrapper per call, and `getOtherPort()` is rejected by the cable IPC —
    both established by live diagnosis, not assumption.)
    """
    return (
        "(function(){"
        "function portOf(dn,pn){"
        "var d=ipc.network().getDevice(dn);"
        "if(!d){return null;}"
        "var n=d.getPortCount();"
        "for(var i=0;i<n;i++){"
        "var p=d.getPortAt(i);"
        "if(p && String(p.getName())===pn){return p;}"
        "}return null;}"
        f"var pa=portOf({_q(a_device)},{_q(a_port)});"
        f"var pb=portOf({_q(b_device)},{_q(b_port)});"
        "if(!pa||!pb){return 'unknown:port not found';}"
        "var la=(typeof pa.getLink==='function')?pa.getLink():null;"
        "var lb=(typeof pb.getLink==='function')?pb.getLink():null;"
        "if(!la||!lb){return 'no:an endpoint has no link';}"
        "if(typeof la.getObjectUuid!=='function'||typeof lb.getObjectUuid!=='function'){"
        "return 'unknown:link has no object uuid';}"
        "return (String(la.getObjectUuid())===String(lb.getObjectUuid())) "
        "? 'yes' : 'no:different links';"
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
    [{name, ip, mask, up, linked, linkClass, peers}]}]}`; `up` is
    `true`/`false`/`null`. `linkClass` is the class of the object the port is
    linked to - `Cable` for a wired link, `Antenna` for a wireless
    association - and `peers` names the devices on the other end. Those two
    are what make an association verifiable instead of assumed.
    """
    statements = [
        "var net=ipc.network();var n=net.getDeviceCount();var arr=[];",
        "for(var i=0;i<n;i++){",
        "var d=net.getDeviceAt(i);var pc=d.getPortCount();var ports=[];",
        "for(var j=0;j<pc;j++){",
        "var p=d.getPortAt(j);var ip='';var mask='';var up=null;var linked=false;",
        "var linkClass='';var peers=[];",
        "try{ip=p.getIpAddress()||'';}catch(pe){}",
        "try{mask=p.getSubnetMask()||'';}catch(pe){}",
        "try{up=(typeof p.isPortUp==='function')?p.isPortUp():null;}catch(pe){}",
        "try{var lk=p.getLink();linked=(lk!=null);",
        "if(lk){linkClass=String(lk.getClassName());",
        "if(linkClass==='Antenna'){try{var rc=lk.getReceiverCount();",
        "if(rc>0){var rp=lk.getReceiverAt(0).getPort();",
        "peers=[String(rp.getOwnerDevice().getName())];}}catch(e){}}",
        "else{var q1=null,q2=null;try{q1=lk.getPort1();}catch(e1){}",
        "try{q2=lk.getPort2();}catch(e2){}",
        "var self=p;var peer=(q1&&q1.getOwnerDevice&&q1.getOwnerDevice().getName()===d.getName())?q2:q1;",
        "if(peer&&peer.getOwnerDevice){peers=[String(peer.getOwnerDevice().getName())];}}}}catch(pe){}",
        "ports.push({name:p.getName(),ip:ip,mask:mask,up:up,linked:linked,",
        "linkClass:linkClass,peers:peers});",
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

    `terminal length 0` is set before the command so long outputs are not
    paginated with `--More--` (which would leave output stuck waiting for
    input and the collect poll spinning).
    """
    statements = [
        "var cl=ipc.network().getDevice(" + _q(device) + ").getCommandLine();",
        "if(!cl){return 'ERR:device without console';}",
        "var p=String(cl.getPrompt()||'');",
        "if(p.indexOf('[yes/no]')>=0){cl.enterCommand('no');}",
        "cl.enterCommand('');",
        "cl.enterCommand('enable');",
        "cl.enterCommand('terminal length 0');",
        "var base=String(cl.getOutput()).length;",
        "cl.enterCommand(" + _q(command.strip()) + ");",
    ]
    return statements, "'BASE:' + base"


def js_console_collect_expr(device: str, base: int) -> str:
    """Poll for the show-command tail.

    The command is considered finished when the console produced any new
    output past `base`. PT's console prompt after `enable` on a 2911 ends in
    `#`; on hosts it ends in `>`. Found live against PT 9.0.1: the strict
    `#` check made `read_routing_table` time out on a router whose console
    prompt was not exactly `<hostname>#` (e.g. the initial-config dialog),
    so output growth alone now completes the exchange instead of a prompt
    shape guess. The command body is echoed back by PT, so a genuinely quiet
    command still produces the tail. Returns 'DONE:<tail>' or 'WAIT';
    'ERR:...' when there is no console.
    """
    return (
        "(function(){"
        "var cl=ipc.network().getDevice(" + _q(device) + ").getCommandLine();"
        "if(!cl){return 'ERR:device without console';}"
        "var o=String(cl.getOutput());"
        "if(o.length>" + str(int(base)) + "){"
        "var tail=o.slice(" + str(int(base)) + ");"
        "return 'DONE:'+tail;"
        "}return 'WAIT';"
        "})()"
    )


def js_console_collect_serial_expr(device: str, command: str) -> str:
    """Collect a `show controllers` tail by anchoring on the command's echo.

    `js_console_collect_expr` only completes when the output GREW past a base
    measured before the command was queued. On a router that just finished a
    build that never happens: PT keeps logging `%LINK-3-UPDOWN` /
    `%LINEPROTO-5-UPDOWN`, the console buffer sits at its cap, and the queued
    command's output is dropped - the length stays level, so the exchange
    spins to the deadline with a correct answer sitting in the device
    (measured in _serial_e2e.py on the two busiest routers).

    So completion is anchored on PT's own echo of THIS command instead of on a
    length: the last occurrence of the command text is the fresh one, and
    everything from there on is its output. Nothing to do with buffer size, so
    a trimmed or saturated console is irrelevant. Kept out of the shared
    collect on purpose: the routing and VLAN read-backs rely on strict growth,
    where a trimmed buffer would hand back a partial table.
    """
    return (
        "(function(){"
        "var cl=ipc.network().getDevice(" + _q(device) + ").getCommandLine();"
        "if(!cl){return 'ERR:device without console';}"
        "var o=String(cl.getOutput());"
        "var i=o.lastIndexOf(" + _q(command.strip()) + ");"
        "if(i>=0){return 'DONE:'+o.slice(i);}"
        "return 'WAIT';"
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


def serial_iface_arg(interface: str) -> str:
    """`Serial2/0` -> `2/0`: the form PT's `show controllers` accepts.

    Measured live (_probe_serial4.py): `show controllers serial Serial2/0`
    answers `% Invalid input detected at '^' marker.`, while the abbreviated
    `show controllers serial 2/0` returns the controller block. Module ports
    lose only the `Serial` prefix, so `Serial0/0/0` -> `0/0/0`.
    """
    name = str(interface or "").strip()
    if name.lower().startswith("serial"):
        return name[len("Serial"):]
    return name


_CONTROLLERS_INTERFACE = re.compile(r"^Interface\s+(\S+)", re.MULTILINE)
_CONTROLLERS_ROLE = re.compile(
    r"^(DCE|DTE)\s+(\S+?)(?:,\s*clock rate\s+(\d+))?(?:\s+TX and RX clocks detected)?\s*$",
    re.MULTILINE,
)


def parse_show_controllers_serial(output: str) -> dict | None:
    """Parse a real `show controllers serial <n>` block, or None if unproven.

    The captured shapes (tests/autopilot/_probe_serial5.py) are

        Interface Serial2/0
        Hardware is PowerQUICC MPC860
        DCE V.35, clock rate 64000

    and

        Interface Serial2/0
        Hardware is PowerQUICC MPC860
        DTE V.35 TX and RX clocks detected

    Anything without that role line - invalid input, an unsupported platform,
    a truncated console - returns None so the caller reports UNKNOWN rather
    than guessing a role or a clock.
    """
    text = str(output or "")
    if "Invalid input" in text or "Unknown command" in text:
        return None
    role = _CONTROLLERS_ROLE.search(text)
    if role is None:
        return None
    interface_match = _CONTROLLERS_INTERFACE.search(text)
    return {
        "interface": interface_match.group(1) if interface_match else None,
        "role": role.group(1).lower(),
        "clock_rate": int(role.group(3)) if role.group(3) else None,
        "signaling": role.group(2),
    }


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


def device_type_code_for(role: str) -> int | None:
    """PT's device-type id for a role or catalog CATEGORY, or None.

    Returning None instead of a silent wired_end_device fallback matters: a
    typo used to turn into a wired end device that PT happily created, so the
    build went on with the wrong chassis.
    """
    return DEVICE_TYPE_CODES.get(role)


def device_type_code_for_model(model_hint: str | None, *, role: str = "router") -> int:
    """The type id for a model, decided by the model's catalog category.

    The model is the strongest signal PT accepts: the same role covers a
    router and a firewall, and an IoT thing has no role of its own. Falls
    back to the role only when the model is unknown, and fails closed when
    neither is known rather than creating a wired end device by accident.
    """
    if model_hint:
        from ...pt.catalog import category_of_model

        override = MODEL_TYPE_OVERRIDES.get(model_hint)
        if override is not None:
            return override
        category = category_of_model(model_hint)
        code = DEVICE_TYPE_CODES.get(category) if category else None
        if code is not None:
            return code
    fallback = DEVICE_TYPE_CODES.get(role)
    if fallback is None:
        raise ValueError(
            f"unknown device type: model={model_hint!r} role={role!r} - "
            "no catalog category or role maps to a Packet Tracer device type"
        )
    return fallback
