"""Bridge plumbing for the autopilot's own live-layout tools.

The auto-layout tools need to read the live canvas (devices + links) and push
moves back. Both operations go through the adapter boundary the rest of the
engine already uses: ``send_expression`` to run JS in PT's script engine and
``position_device`` to move an icon. Nothing here talks to the transport
directly, so the layout tools stay channel-agnostic (HTTP or file mailbox).
"""

from __future__ import annotations

import json
from typing import Any


def snapshot_topology(adapter: Any) -> tuple[list[tuple[str, str, float, float]], list[tuple[str, str]]] | None:
    """Read the live canvas as ``(devices, links)``.

    ``devices`` is a list of ``(name, category, x, y)``; ``links`` a list of
    ``(a, b)`` endpoint pairs. Returns ``None`` when the channel is not usable
    or PT returns something unparseable, so a tool degrades to a message
    instead of raising mid-command.
    """
    probe = adapter.ping(timeout=5.0)
    if not probe.reachable:
        return None

    answer = adapter.send_expression(_SNAPSHOT_JS)
    if not answer.is_known:
        return None
    text = answer.value
    if not text or str(text).startswith("ERROR:"):
        return None
    try:
        data: dict = json.loads(str(text))
    except (json.JSONDecodeError, ValueError):
        return None

    devices = [
        (str(row[0]), _category_of_model(str(row[1])),
         float(row[2]), float(row[3]))
        for row in data.get("devices", [])
    ]
    links = [
        (str(pair[0]), str(pair[1]))
        for pair in data.get("links", [])
        if len(pair) >= 2
    ]
    return devices, links


def apply_moves(adapter: Any, moves: list[tuple[str, int, int]]) -> tuple[int, list[str]]:
    """Push a batch of ``(device, x, y)`` moves. Returns ``(applied, skipped)``.

    Each move goes through ``position_device``, which already checks the device
    exists and interprets PT's answer. Unknown devices are collected rather
    than aborting the batch, so one stale name cannot block a whole tidy-up.
    """
    applied = 0
    skipped: list[str] = []
    for name, x, y in moves:
        receipt = adapter.position_device(name, int(x), int(y))
        if receipt.executed and receipt.result.success:
            applied += 1
        else:
            skipped.append(name)
    return applied, skipped


def _category_of_model(model_name: str) -> str:
    """Category for a PT model string, "" when unknown.

    Looked up through the local catalog replacement so this helper never
    reaches into upstream code. An empty result is meaningful to callers: they
    fall back to a neutral footprint and host-tier placement.
    """
    from .catalog.devices import category_of_model

    return category_of_model(model_name)


# JS run inside PT's script engine. Uses only IPC calls already exercised
# elsewhere in this project (getDeviceCount/getDeviceAt/getXCoordinate and
# getLinkAt/getPort1/getPort2), so no unverified PT API is exercised here.
_SNAPSHOT_JS = (
    "try {"
    "  var net = ipc.network();"
    "  var devOut = [];"
    "  var dn = net.getDeviceCount();"
    "  for (var i = 0; i < dn; i++) {"
    "    var d = net.getDeviceAt(i);"
    "    var x = 0, y = 0;"
    "    try { x = d.getXCoordinate(); y = d.getYCoordinate(); } catch(e) {}"
    "    devOut.push([d.getName(), d.getModel(), x, y]);"
    "  }"
    "  var linkOut = [];"
    "  var ln = net.getLinkCount();"
    "  for (var k = 0; k < ln; k++) {"
    "    try {"
    "      var l = net.getLinkAt(k);"
    "      if (l.getClassName() !== 'Antenna') {"
    "        var p1 = l.getPort1(); var p2 = l.getPort2();"
    "        linkOut.push([p1.getOwnerDevice().getName(),"
    "                      p2.getOwnerDevice().getName()]);"
    "      }"
    "    } catch(le) {}"
    "  }"
    "  reportResult(JSON.stringify({devices: devOut, links: linkOut}));"
    "} catch(e) { reportResult('ERROR:' + e); }"
)
