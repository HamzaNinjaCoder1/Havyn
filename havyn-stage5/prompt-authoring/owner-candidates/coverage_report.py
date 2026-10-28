"""Stage 5 prompt-authoring: candidate-vs-certified coverage classification.

The repo's certified coverage matrix is ``scripts/generate_intents.py
coverage_cells()``. The owner candidates were written against cell *labels*,
and labels and reality can drift in both directions (a candidate labelled with a
certified cell whose features differ, or a feature shape the matrix does not
have at all — e.g. DMZ + multi-site, guest VLANs, ASA5505 shapes).

This script does not relabel anything. It derives each candidate's feature
signature from its own ``expected_intent`` and reports, per candidate, one of:

    exact               declared label is a certified cell id AND the derived
                        features equal that cell's features
    label-only          declared label is a certified cell id but the derived
                        features differ from it
    feature-match       derived features equal some certified cell's features,
                        but the declared label is not that cell's id
    extension-cell      no certified cell has these features (a shape the
                        matrix does not cover; often a capability-boundary or
                        refusal candidate)
    refusal             expect_refusal / no intent (classification n/a)

Deterministic and read-only: it never edits ``candidate_data.py``.

Usage:
    python coverage_report.py            # print summary, write json + md
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for candidate_path in (str(REPO), str(HERE)):
    if candidate_path not in sys.path:
        sys.path.insert(0, candidate_path)

from candidate_data import CANDIDATES  # noqa: E402

sys.path.insert(0, str(REPO / "havyn-stage5" / "scripts"))
from generate_intents import coverage_cells  # noqa: E402

REPORTS = HERE.parent / "reports"


def _addressing(intent: dict) -> str:
    groups = [g for site in intent["sites"]
              for area in site["areas"]
              for g in area["host_groups"]]
    static = sum(1 for g in groups if g.get("addressing") == "static")
    if groups and static == len(groups):
        return "static"
    if static == 0:
        return "dhcp"
    return "mixed"


def _firewall_label(intent: dict) -> str:
    fw = intent.get("firewall")
    if not fw or not fw.get("enabled", True):
        return "none"
    return "asa-dmz" if fw.get("dmz") else "asa"


def signature(intent: dict) -> dict:
    """The comparable shape of a candidate intent."""
    sites = intent["sites"]
    areas = max((len(s["areas"]) for s in sites), default=0)
    hosts = max(
        (sum(g.get("count", 0) for g in area["host_groups"])
         for s in sites for area in s["areas"]),
        default=0,
    )
    site_ids = {s["id"] for s in sites}
    first_site = sites[0]["id"] if sites else None
    vlans = intent.get("vlans", [])
    vlans_at_first = [v for v in vlans if v.get("site") in (first_site, None)]
    internet = intent.get("internet") or {}
    reqs = intent.get("requirements", [])
    return {
        "site_count": len(sites),
        "areas_per_site": areas,
        "hosts_per_area": hosts,
        "vlan_mode": "multi" if len(vlans_at_first) > 1 else "single",
        "routing": (intent.get("routing") or {}).get("mode", "connected"),
        "internet": internet.get("access", "none"),
        "firewall": _firewall_label(intent),
        "wireless": bool(intent.get("wireless")),
        "iot": bool(intent.get("iot")),
        "addressing": _addressing(intent),
        "servers": len(intent.get("servers", [])),
        "public_server": bool(internet.get("public_server")),
        "security": any(r.get("type") == "security" for r in reqs),
        "isolated_sites": site_ids,
    }


def certified_cells() -> dict[str, dict]:
    out = {}
    for cell in coverage_cells():
        out[cell.cell_id] = {
            "site_count": cell.site_count,
            "areas_per_site": cell.areas_per_site,
            "vlan_mode": cell.vlan_mode,
            "routing": cell.routing,
            "internet": cell.internet,
            "firewall": cell.firewall,
            "wireless": cell.wireless,
            "iot": cell.iot,
            "addressing": cell.addressing,
            "servers": cell.servers,
            "security": cell.security_requirements,
            "features": cell.features(),
            "language": cell.language,
            "prompt_length": cell.prompt_length,
        }
    return out


FEATURE_KEYS = ("vlan_mode", "routing", "internet", "firewall", "wireless",
                "iot", "addressing", "servers", "security")


def features_of(sig: dict) -> str | None:
    cell = {
        "routing": sig["routing"], "internet": sig["internet"],
        "firewall": sig["firewall"], "wireless": sig["wireless"],
        "iot": sig["iot"], "addressing": sig["addressing"],
        "servers": sig["servers"], "security": sig["security"],
    }
    return certified_features_string(cell)


def certified_features_string(cell: dict) -> str:
    tags = [cell["routing"], cell["internet"], cell["firewall"]]
    if cell["wireless"]:
        tags.append("wireless")
    if cell["iot"]:
        tags.append("iot")
    tags.append(cell["addressing"])
    if cell["servers"]:
        tags.append(f"servers{cell['servers']}")
    if cell["security"]:
        tags.append("security")
    return "+".join(tags)


def classify(record: dict, cells: dict[str, dict]) -> dict:
    if record.get("expect_refusal") or not record.get("expected_intent"):
        return {
            "id": record["id"],
            "declared_cell": record.get("coverage_cell"),
            "classification": "refusal",
            "detail": "no intent (expected refusal) - cell coverage n/a",
        }

    sig = signature(record["expected_intent"])
    features = features_of(sig)
    declared = record.get("coverage_cell")

    # 1. exact: label is a certified id and features agree with it.
    if declared in cells and certified_features_string(cells[declared]) == features:
        cell = cells[declared]
        return {
            "id": record["id"],
            "declared_cell": declared,
            "classification": "exact",
            "matched_cell": declared,
            "features": features,
            "detail": f"site/area match: {cell['site_count']}s/{cell['areas_per_site']}a",
        }

    # 2. label-only: certified label, features drifted.
    if declared in cells:
        matched = certified_features_string(cells[declared])
        return {
            "id": record["id"],
            "declared_cell": declared,
            "classification": "label-only",
            "features": features,
            "detail": f"cell features '{matched}' vs candidate '{features}'",
        }

    # 3. feature-match: some certified cell has these features (any label).
    same_features = [cid for cid, cell in cells.items()
                     if certified_features_string(cell) == features]
    if same_features:
        return {
            "id": record["id"],
            "declared_cell": declared,
            "classification": "feature-match",
            "features": features,
            "detail": "certified cells with these features: "
                      + ", ".join(sorted(same_features)),
        }

    # 4. extension-cell: a shape the matrix does not contain.
    return {
        "id": record["id"],
        "declared_cell": declared,
        "classification": "extension-cell",
        "features": features,
        "detail": "no certified cell carries these features",
    }


def main() -> None:
    cells = certified_cells()
    rows = [classify(record, cells) for record in CANDIDATES]
    counts = Counter(row["classification"] for row in rows)

    per_cell: dict[str, int] = {cid: 0 for cid in cells}
    for row in rows:
        if row.get("matched_cell"):
            per_cell[row["matched_cell"]] += 1

    result = {
        "certified_cells": len(cells),
        "candidates": len(rows),
        "classification_counts": dict(sorted(counts.items())),
        "per_certified_cell_exact_fill": dict(sorted(per_cell.items())),
        "rows": rows,
    }
    (HERE / "coverage-report.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    REPORTS.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Coverage classification (candidates vs certified matrix)",
        "",
        "Generated by `../owner-candidates/coverage_report.py`. Nothing here is",
        "relabelled: a candidate whose label drifted from its own intent is",
        "reported as such, and shapes the matrix does not contain are marked",
        "`extension-cell` rather than squeezed into a certified cell.",
        "",
        f"- certified cells: {len(cells)}",
        f"- candidates: {len(rows)}",
        "",
        "## Classification counts",
        "",
        "| classification | count |",
        "| --- | --- |",
    ]
    for name, count in sorted(counts.items()):
        lines.append(f"| {name} | {count} |")
    lines += ["", "## Per candidate", "",
              "| id | declared cell | classification | features | detail |",
              "| --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            "| {id} | {declared} | {cls} | {features} | {detail} |".format(
                id=row["id"], declared=row.get("declared_cell") or "-",
                cls=row["classification"], features=row.get("features") or "-",
                detail=row.get("detail", ""),
            )
        )
    lines += ["", "## Exact fill per certified cell", "",
              "| certified cell | exact candidates |", "| --- | --- |"]
    for cid, n in sorted(per_cell.items()):
        lines.append(f"| {cid} | {n} |")
    lines.append("")
    (REPORTS / "coverage_report.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"classification_counts": dict(sorted(counts.items())),
                      "certified_cells": len(cells)}, indent=2))


if __name__ == "__main__":
    main()
