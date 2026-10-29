"""S5.6/S5.8: coverage-driven intent generation through the full engine chain.

Every generated intent passes: schema validation -> intent lint (blocking
findings discard) -> expansion -> spec lint -> IPAM -> compile. Failures are
recorded with their exact reason and category, never silently dropped.

Deterministic: fixed seed, fixed coverage enumeration, same output bytes for
the same EXPANDER_VERSION + engine commit. Only certified generator versions
may be used for large runs (S5.7): pass --generator-version and the script
records it into every example's metadata.

Usage:
    python generate_intents.py --out dataset/intents --limit 500 --seed 20261028
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src.network_autopilot.ai.expander import EXPANDER_VERSION, expand_intent  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402
from src.network_autopilot.compiler import compile_spec  # noqa: E402
from src.network_autopilot.compiler.spec_lint import lint_spec  # noqa: E402
from src.network_autopilot.ipam import plan_addressing  # noqa: E402

GENERATOR_VERSION = "0.1.0"


def engine_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return None


# --- coverage matrix (S5.6) ---------------------------------------------------

@dataclass
class CoverageCell:
    cell_id: str
    site_count: int
    areas_per_site: int
    hosts_per_area: int
    vlan_mode: str  # "single" | "multi"
    routing: str  # "connected" | "static" | "ospf"
    internet: str  # "none" | "wan" | "dsl" | "cable"
    firewall: str  # "none" | "asa" | "asa-dmz"
    wireless: bool
    iot: bool
    addressing: str  # "dhcp" | "static" | "mixed"
    servers: int
    language: str  # "en" | "roman-urdu" | "mixed"
    prompt_length: str  # "short" | "medium" | "long" | "document"
    security_requirements: bool

    def features(self) -> str:
        tags = [self.routing, self.internet, self.firewall]
        if self.wireless:
            tags.append("wireless")
        if self.iot:
            tags.append("iot")
        tags.append(self.addressing)
        if self.servers:
            tags.append(f"servers{self.servers}")
        if self.security_requirements:
            tags.append("security")
        return "+".join(tags)


def coverage_cells() -> list[CoverageCell]:
    """Enumerate the tracked coverage cells. Size tiers keep easy cells from
    dominating: large topologies get fewer quota slots per cell."""
    cells = []
    sizes = [
        # (site_count, areas, hosts, weight)
        (1, 1, 4, 1.0),      # tiny
        (1, 2, 10, 1.0),     # small
        (1, 3, 15, 0.8),     # medium
        (2, 1, 8, 0.6),      # two sites
        (2, 2, 12, 0.4),     # two sites, larger
        (3, 1, 6, 0.3),      # three sites
        (1, 4, 40, 0.2),     # large
    ]
    for sites, areas, hosts, weight in sizes:
        size_class = "small" if hosts <= 10 else ("medium" if hosts <= 20 else "large")
        cell_id = f"{size_class}-s{sites}-a{areas}"
        cells.append(CoverageCell(
            cell_id=f"{cell_id}-single-static-none-none",
            site_count=sites, areas_per_site=areas, hosts_per_area=hosts,
            vlan_mode="single", routing="static", internet="none", firewall="none",
            wireless=False, iot=False, addressing="static", servers=0,
            language="en", prompt_length="short", security_requirements=False,
        ))
        # The ASA perimeter (T07) replaces the internet-facing site's router
        # and is the inside gateway, which the expander only verifies on a
        # single-VLAN site - so the firewall cells are single-VLAN by design.
        cells.append(CoverageCell(
            cell_id=f"{cell_id}-single-dhcp-wan-asa",
            site_count=sites, areas_per_site=areas, hosts_per_area=hosts,
            vlan_mode="single", routing="static", internet="wan", firewall="asa",
            wireless=True, iot=False, addressing="dhcp", servers=1,
            language="en", prompt_length="medium", security_requirements=True,
        ))
        cells.append(CoverageCell(
            cell_id=f"{cell_id}-multi-dhcp-{('dsl' if sites == 1 else 'wan')}-none",
            site_count=sites, areas_per_site=areas, hosts_per_area=hosts,
            vlan_mode="multi", routing="static",
            internet="dsl" if sites == 1 else "wan", firewall="none",
            wireless=False, iot=False, addressing="dhcp", servers=0,
            language="roman-urdu", prompt_length="conversational",
            security_requirements=False,
        ))
        cells.append(CoverageCell(
            cell_id=f"{cell_id}-multi-dhcp-cable-none-iot",
            site_count=sites, areas_per_site=areas, hosts_per_area=hosts,
            vlan_mode="multi", routing="static", internet="cable", firewall="none",
            wireless=True, iot=True, addressing="dhcp", servers=0,
            language="mixed", prompt_length="medium", security_requirements=False,
        ))
        if sites == 1:
            cells.append(CoverageCell(
                cell_id=f"{cell_id}-single-dhcp-wan-asa-dmz",
                site_count=sites, areas_per_site=areas, hosts_per_area=hosts,
                vlan_mode="single", routing="static", internet="wan",
                firewall="asa-dmz", wireless=False, iot=False,
                addressing="dhcp", servers=1, language="en",
                prompt_length="document", security_requirements=True,
            ))
            cells.append(CoverageCell(
                cell_id=f"{cell_id}-multi-static-ospf-wan-none",
                site_count=sites, areas_per_site=areas, hosts_per_area=hosts,
                vlan_mode="multi", routing="ospf", internet="wan", firewall="none",
                wireless=False, iot=False, addressing="static", servers=0,
                language="en", prompt_length="long", security_requirements=False,
            ))
    return cells


# --- intent synthesis ----------------------------------------------------------

def build_intent(cell: CoverageCell, variant: int, rng: random.Random) -> HavynIntent:
    sites = []
    vlans = []
    for site_index in range(cell.site_count):
        site_id = f"site{site_index + 1}"
        areas = []
        for area_index in range(cell.areas_per_site):
            area_id = f"area{area_index + 1}"
            groups = [{
                "id": f"{site_id}-g{area_index}",
                "area": area_id,
                "count": max(1, cell.hosts_per_area + rng.randint(-2, 2)),
                "device": "pc",
            }]
            if cell.addressing == "static":
                groups[-1]["addressing"] = "static"
            elif cell.addressing == "mixed" and area_index == 0:
                groups[-1]["addressing"] = "static"
            if cell.wireless and area_index == 0:
                groups.append({
                    "id": f"{site_id}-wifi",
                    "area": area_id,
                    "count": rng.randint(2, 6),
                    "device": "laptop",
                })
            areas.append({"id": area_id, "host_groups": groups})
        sites.append({"id": site_id, "areas": areas})
        if cell.vlan_mode == "multi":
            for area_index in range(cell.areas_per_site):
                vlans.append({
                    "id": f"{site_id}-v{area_index + 1}",
                    "name": f"{site_id} vlan {area_index + 1}",
                    "site": site_id,
                    "required_hosts": cell.hosts_per_area + 10,
                    "dhcp": cell.addressing != "static",
                })
        else:
            vlans.append({
                "id": f"{site_id}-users",
                "name": f"{site_id} users",
                "site": site_id,
                "required_hosts": cell.hosts_per_area * cell.areas_per_site + 10,
                "dhcp": cell.addressing != "static",
            })
    intent: dict = {
        "intent_version": "1",
        "name": f"gen-{cell.cell_id}-v{variant}",
        "sites": sites,
        "vlans": vlans,
        "routing": {"mode": cell.routing},
    }
    if cell.internet != "none":
        intent["internet"] = {
            "access": cell.internet,
            "nat": True,
            "public_server": True,
        }
    if cell.firewall != "none" and cell.site_count == 1:
        firewall = {"enabled": True, "model": "asa5506"}
        if cell.firewall == "asa-dmz":
            firewall["dmz"] = True
        intent["firewall"] = firewall
    if cell.wireless:
        intent["wireless"] = [{
            "id": f"cell{site_index + 1}",
            "site": f"site{site_index + 1}",
            "ssid": f"Net{site_index + 1}",
            "security": "open" if variant % 3 == 0 else "wpa2",
            "client_group_ids": [f"site{site_index + 1}-wifi"],
            **({"passphrase": f"pass{variant:04d}key"} if variant % 3 else {}),
        } for site_index in range(cell.site_count)]
    if cell.iot:
        intent["iot"] = [{
            "id": f"led{site_index + 1}",
            "site": f"site{site_index + 1}",
            "area": "area1",
            "count": rng.randint(1, 4),
        } for site_index in range(cell.site_count)]
    if cell.servers:
        server_vlans = [v for v in vlans if v["site"] == "site1"]
        intent["servers"] = [{
            "id": "srv1",
            "site": "site1",
            "vlan": server_vlans[0]["id"] if server_vlans else None,
            "services": ["dns", "http"],
        }]
    requirements = [{
        "id": "REQ-001",
        "statement": "Every host group reaches every other host group",
        "type": "connectivity",
    }]
    if cell.internet != "none":
        requirements.append({
            "id": "REQ-002",
            "statement": "The internet test server is reachable from the LAN",
            "type": "connectivity",
        })
    if cell.security_requirements:
        requirements.append({
            "id": "REQ-003",
            "statement": "The perimeter blocks outside-initiated access",
            "type": "security",
            "priority": "critical",
        })
    if cell.iot:
        requirements.append({
            "id": "REQ-004",
            "statement": "IoT devices are cabled into the network",
            "type": "operational",
        })
    intent["requirements"] = requirements
    if cell.language == "roman-urdu":
        intent["language"] = "roman-urdu"
        intent["source_excerpt"] = (
            "Ek network banayen jisme sab PC connected hon aur internet chalay."
        )
    elif cell.language == "mixed":
        intent["language"] = "mixed"
        intent["source_excerpt"] = (
            "Office setup chahiye with VLANs, sab devices connected, "
            "internet bhi available ho."
        )
    return HavynIntent.model_validate(intent)


def verify_intent(intent: HavynIntent) -> tuple[str, str | None, dict | None]:
    """The full offline chain. Returns (status, failure_stage, summary)."""
    findings = lint_intent(intent)
    blocking = findings.blocking
    if blocking:
        return ("lint-reject", blocking[0].code, None)
    try:
        expansion = expand_intent(intent)
        spec = expansion.spec
        problems = lint_spec(spec)
        if problems:
            return ("spec-lint-fail", problems[0][:200], None)
        plan = plan_addressing(spec)
        compile_spec(spec, plan)
    except Exception as exc:
        return ("expansion-fail", f"{type(exc).__name__}: {exc}"[:300], None)
    return ("valid", None, {
        "summary": expansion.summary(),
        "spec": spec.to_json(indent=None),
    })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="havyn-stage5/datasets/havyn-intents-v1")
    parser.add_argument("--limit", type=int, default=None,
                        help="max variants per coverage pass (for smoke runs)")
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--generator-version", default=GENERATOR_VERSION)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    cells = coverage_cells()
    variants_per_cell = args.limit or 60

    valid_path = out_dir / "valid.jsonl"
    failed_path = out_dir / "failed.jsonl"
    coverage_path = out_dir / "coverage-report.json"
    manifest_path = out_dir / "manifest.json"

    counts: dict[str, int] = {}
    failure_categories: dict[str, int] = {}
    hashes: set[str] = set()
    total = duplicates = 0

    with valid_path.open("w", encoding="utf-8") as valid_file, \
            failed_path.open("w", encoding="utf-8") as failed_file:
        for cell in cells:
            cell_quota = max(1, round(variants_per_cell * (1.0 if cell.hosts_per_area <= 20 else 0.4)))
            for variant in range(cell_quota):
                total += 1
                intent = build_intent(cell, variant, rng)
                canonical = intent.to_json(indent=None)
                digest = hashlib.sha256(canonical.encode()).hexdigest()
                if digest in hashes:
                    duplicates += 1
                    continue
                hashes.add(digest)
                status, stage, payload = verify_intent(intent)
                record = {
                    "id": f"gen-{digest[:12]}",
                    "coverage_cell": cell.cell_id,
                    "features": cell.features(),
                    "language": cell.language,
                    "prompt_length": cell.prompt_length,
                    "generator_version": args.generator_version,
                    "expander_version": EXPANDER_VERSION,
                    "seed": args.seed,
                    "intent": json.loads(canonical),
                    "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                if status == "valid":
                    counts[cell.cell_id] = counts.get(cell.cell_id, 0) + 1
                    record["validation"] = "valid"
                    record["spec_hash"] = payload["summary"]["spec_hash"]
                    record["summary"] = payload["summary"]
                    valid_file.write(json.dumps(record, sort_keys=True) + "\n")
                else:
                    failure_categories[f"{status}:{stage}"] = \
                        failure_categories.get(f"{status}:{stage}", 0) + 1
                    record["validation"] = status
                    record["failure_stage"] = stage
                    failed_file.write(json.dumps(record, sort_keys=True) + "\n")

    coverage_report = {
        "cells": {cell_id: count for cell_id, count in sorted(counts.items())},
        "total_generated": total,
        "unique_valid": sum(counts.values()),
        "duplicates_skipped": duplicates,
        "failure_categories": failure_categories,
        "coverage_dimensions": {
            "site_count": sorted({c.site_count for c in cells}),
            "vlan_mode": ["single", "multi"],
            "routing": ["static", "ospf"],
            "internet": ["none", "wan", "dsl", "cable"],
            "firewall": ["none", "asa", "asa-dmz"],
            "addressing": ["dhcp", "static", "mixed"],
            "language": ["en", "roman-urdu", "mixed"],
            "security_requirements": [True, False],
        },
    }
    coverage_path.write_text(json.dumps(coverage_report, indent=2), encoding="utf-8")
    manifest = {
        "dataset": "havyn-intents-v1",
        "generator_version": args.generator_version,
        "expander_version": EXPANDER_VERSION,
        "engine_commit": engine_commit(),
        "seed": args.seed,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": coverage_report,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in
                      ("dataset", "expander_version", "engine_commit", "seed")}, indent=2))
    print(json.dumps(coverage_report["cells"], indent=2))
    print("unique valid:", coverage_report["unique_valid"])
    if failure_categories:
        print("failures:", json.dumps(failure_categories, indent=2))


if __name__ == "__main__":
    main()
