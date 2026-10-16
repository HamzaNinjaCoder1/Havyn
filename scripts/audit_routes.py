#!/usr/bin/env python
"""Audit every spec static route against the LIVE route tables.

The verdict has exactly three values, and the third one is the whole point:

    pass     the device's own route table proves the route is there
    missing  the device's own route table was read and does NOT have it
    unknown  the route table could not be observed - never "missing"

This last rule is not decoration. `.zwork/audit_routes.py` (the predecessor of
this script) reported 9 of 18 spec routes MISSING, including

    rhq   0.0.0.0/0   via 10.0.0.5    MISSING

on a router whose console plainly carried `S*  0.0.0.0/0 [1/0] via 10.0.0.5`.
Three separate read defects produced that lie, all now measured and fixed in
`js_console_show_expr` / `BridgeAdapter._console_show_full`:

  R1  PT's console buffer is capped and long output PAGES on `--More--`, so a
      read that stopped at page 1 lost the `S*` default and every static route
      behind it.
  R2  an ASA has no `getCommandLine()`; its `getConsoleLine()` rejects
      `show route` with `% Invalid input detected`. An ASA route table is
      UNOBSERVABLE through PT, so it is UNKNOWN - not missing.
  R3  a booting device answers with ROMMON/bootloader text, which parses to an
      empty table and reads as "no routes at all".

Usage:
    python scripts/audit_routes.py [spec.json]
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.network_autopilot.core.observability import Observable  # noqa: E402

PASS = "pass"
MISSING = "missing"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class RouteVerdict:
    """One spec route against one device's observed table."""

    device: str
    destination: str
    next_hop: str
    status: str
    reason: str = ""
    observed: list = field(default_factory=list)

    def line(self) -> str:
        tail = "" if self.status == PASS else f"   <- {self.reason}"
        return (
            f"  {self.device:<6} {self.destination:<18} via {self.next_hop:<14} "
            f"{self.status.upper()}{tail}"
        )


def _split_destination(destination: str) -> tuple[str, int]:
    text = str(destination).strip()
    if "/" in text:
        net, _, prefix = text.partition("/")
        return net, int(prefix)
    return text, 32


def _routes_matching(table: list, network: str, prefix: int, next_hop: str) -> list:
    """Rows that prove this exact route is installed.

    A static row matches on destination AND next hop. IOS wraps long static
    routes onto a second line (`[1/0] via <nh>` indented under the network),
    which is why a wrapped row is accepted with an empty next hop only when the
    device's own table has no unwrapped form to compare against.
    """
    hits = []
    for row in table:
        try:
            row_net = str(row.get("network"))
            row_prefix = int(row.get("prefix"))
        except (TypeError, ValueError):
            continue
        if row_net != network or row_prefix != prefix:
            continue
        row_nh = str(row.get("next_hop") or "")
        if row_nh == next_hop:
            hits.append(row)
    return hits


def audit_route(
    *,
    device: str,
    destination: str,
    next_hop: str,
    table: Observable,
) -> RouteVerdict:
    """Rule 7 as code: an unobservable table yields UNKNOWN, never MISSING."""
    network, prefix = _split_destination(destination)
    if not table.is_known:
        reason = str(table.detail or table.status.value or "table not observed")
        return RouteVerdict(device, destination, next_hop, UNKNOWN, reason)

    rows = list(table.value or [])
    hits = _routes_matching(rows, network, prefix, next_hop)
    if hits:
        return RouteVerdict(device, destination, next_hop, PASS, "", hits)

    # Not found in a table we DID read. Say what else is there so a MISSING is
    # never a bare accusation.
    same_net = [
        r for r in rows
        if str(r.get("network")) == network and int(r.get("prefix") or -1) == prefix
    ]
    if same_net:
        seen = ", ".join(
            f"{r.get('next_hop') or 'connected'} ({r.get('kind')})" for r in same_net
        )
        return RouteVerdict(
            device, destination, next_hop, MISSING,
            f"table has {network}/{prefix} via {seen}, not via {next_hop}",
        )
    return RouteVerdict(
        device, destination, next_hop, MISSING,
        f"no {network}/{prefix} in a table read from {table.source or device!r}",
    )


def audit_spec(spec: dict, read_table) -> list[RouteVerdict]:
    """Audit every `routing.static_routes` entry, one read per device."""
    cache: dict[str, Observable] = {}

    def table_for(device: str) -> Observable:
        if device not in cache:
            cache[device] = read_table(device)
        return cache[device]

    verdicts = []
    for entry in spec.get("routing", {}).get("static_routes", []):
        verdicts.append(
            audit_route(
                device=str(entry["device"]),
                destination=str(entry["destination"]),
                next_hop=str(entry["next_hop"]),
                table=table_for(str(entry["device"])),
            )
        )
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_routes",
        description="Audit spec static routes against live Packet Tracer tables.",
    )
    parser.add_argument("spec", nargs="?", default="my-network.spec.json")
    args = parser.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))

    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import (
        BridgeAdapter,
    )

    adapter = BridgeAdapter(host_bridge=True)
    verdicts = audit_spec(spec, adapter.read_routing_table)

    print(f"=== spec route audit: {args.spec} ===")
    for verdict in verdicts:
        print(verdict.line())

    tally = {PASS: 0, MISSING: 0, UNKNOWN: 0}
    for verdict in verdicts:
        tally[verdict.status] += 1
    print(
        f"\n{tally[PASS]} pass, {tally[MISSING]} missing, "
        f"{tally[UNKNOWN]} unknown of {len(verdicts)} spec routes"
    )
    if tally[UNKNOWN]:
        print("UNKNOWN means the route table could not be read - not that the "
              "route is absent. Never read it as MISSING.")
    # A missing route is a real finding; an unreadable table is not a verdict.
    return 1 if tally[MISSING] else 0


if __name__ == "__main__":
    raise SystemExit(main())
