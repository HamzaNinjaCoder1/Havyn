"""Stage 5 prompt-authoring: audit VLAN pins on buildable candidates.

Rule (from FIX_PASS_PLAN.md Section A): in a site that declares two or more
non-management VLANs, every host group in that site must pin a VLAN, and every
server placed at that site must declare one. An unpinned host group silently
lands on the site's FIRST VLAN in expander v1 (``_instance_for(None, site)``),
so the prompt's "separate networks" claim is not realised even though the
intent schema accepts it.

This is a structural audit over the emitted candidate JSON, not an engine run.
It answers one question: is every declared multi-VLAN site fully pinned?

Usage:
    python audit_unpinned.py            # print summary, write unpinned-audit.json
    python audit_unpinned.py --quiet    # summary only

Exit code 1 when any violation is found.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAND_DIR = HERE / "candidates"


def _site_vlans(intent: dict, site_id: str) -> list[dict]:
    return [
        v
        for v in intent.get("vlans", [])
        if v.get("site") == site_id and not v.get("is_management")
    ]


def audit(records: list[dict]) -> dict:
    violations: list[dict] = []
    buildable = 0
    multi_vlan_sites = 0
    groups_checked = 0
    servers_checked = 0

    for record in records:
        intent = record.get("expected_intent")
        if record.get("expect_refusal") or not intent:
            continue
        buildable += 1

        for site in intent.get("sites", []):
            site_id = site.get("id")
            vlans = _site_vlans(intent, site_id)
            if len(vlans) < 2:
                continue
            multi_vlan_sites += 1

            for area in site.get("areas", []):
                for group in area.get("host_groups", []):
                    groups_checked += 1
                    if not group.get("vlan"):
                        violations.append(
                            {
                                "candidate": record["id"],
                                "site": site_id,
                                "area": area.get("id"),
                                "kind": "host_group",
                                "id": group.get("id"),
                                "device": group.get("device"),
                                "count": group.get("count"),
                                "reason": "unpinned host group in a multi-VLAN site",
                            }
                        )

        for server in intent.get("servers", []):
            servers_checked += 1
            site_id = server.get("site")
            if len(_site_vlans(intent, site_id)) >= 2 and not server.get("vlan"):
                violations.append(
                    {
                        "candidate": record["id"],
                        "site": site_id,
                        "kind": "server",
                        "id": server.get("id"),
                        "reason": "unpinned server in a multi-VLAN site",
                    }
                )

    return {
        "summary": {
            "candidates": len(records),
            "buildable_candidates": buildable,
            "multi_vlan_sites": multi_vlan_sites,
            "host_groups_checked": groups_checked,
            "servers_checked": servers_checked,
            "violations": len(violations),
        },
        "violations": violations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CAND_DIR.glob("oc-*.json"))
    ]
    if not records:
        raise SystemExit(f"no candidate JSON in {CAND_DIR}")

    report = audit(records)
    (HERE / "unpinned-audit.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if not args.quiet:
        for violation in report["violations"]:
            print(
                "UNPINNED {candidate} site={site} {kind}={id} ({reason})".format(
                    **violation
                )
            )
    print(json.dumps(report["summary"], indent=2))
    if report["violations"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
