"""Capability Map generator (S2.6): what Havyn can do, with the proof.

    python scripts/capability_map.py [--out-docs docs/capability-map.md]
        [--out-json docs/capability-map.json]

Every feature carries a status with a mechanical rule behind it:

* ``Verified`` — a live test in this repo proves it against Packet Tracer.
  The registry names that test; the generator asserts the file exists.
* ``Refused`` — the engine refuses to emit this on purpose (rule 9), and an
  offline test pins the refusal. The registry names the reason and the test.
* ``Unverified`` — anything else. No live lab yet, no claim. The entry says
  so and points at what would verify it.

A status with no evidence pointer is rejected: the map may only ever claim
what the repo can show.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]

STATUSES = ("Verified", "Unverified", "Refused")


@dataclass(frozen=True)
class Feature:
    """One capability row."""

    id: str
    name: str
    families: tuple[str, ...]
    status: str
    evidence: str
    refusal_reason: str = ""

    def evidence_path(self) -> Path:
        """The repo file behind the evidence pointer (before :: test or note)."""
        return REPO_ROOT / self.evidence.split("::")[0].split(" (")[0].strip()

    def validate(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"feature {self.id!r}: unknown status {self.status!r}")
        if not self.evidence:
            raise ValueError(f"feature {self.id!r}: no evidence pointer")
        if self.status == "Refused" and not self.refusal_reason:
            raise ValueError(f"feature {self.id!r}: refusal needs its reason")
        if self.status in ("Verified", "Refused"):
            if not self.evidence_path().is_file():
                raise ValueError(
                    f"feature {self.id!r}: evidence {self.evidence!r} is not a file"
                )


def REGISTRY() -> list[Feature]:
    """The map itself. Edit here; the generator only renders."""
    return [
        Feature(
            id="router-acl-enforce",
            name="IOS router numbered ACLs with address matching",
            families=("router",),
            status="Verified",
            evidence="tests/autopilot/test_live_policy.py::"
            "test_router_acl_renders_a_cidr_operand_and_enforces_it",
        ),
        Feature(
            id="asa-wildcard-permit",
            name="ASA wildcard permit on the outside nameif passes the protocol",
            families=("firewall:5506-X", "firewall:5505"),
            status="Verified",
            evidence="tests/autopilot/test_live_policy.py::"
            "test_asa_address_scoped_acl_cannot_carry_traffic_but_wildcard_can",
        ),
        Feature(
            id="asa-5505-svi-readback",
            name="ASA 5505 Vlan SVIs read back pushed addresses",
            families=("firewall:5505",),
            status="Verified",
            evidence="tests/autopilot/test_live_policy.py::"
            "test_asa_5505_vlan_addresses_read_back_after_the_push",
        ),
        Feature(
            id="wireless-association",
            name="Wireless clients associate automatically (Antenna link)",
            families=("laptop", "accesspoint"),
            status="Verified",
            evidence="tests/autopilot/test_live_connectivity.py "
            "(wireless area: laptop associates, then reaches across the AP)",
        ),
        Feature(
            id="flow-reachable-blocked",
            name="ICMP flows verify reachable and blocked end to end",
            families=("router", "switch", "pc", "firewall"),
            status="Verified",
            evidence="tests/autopilot/test_live_connectivity.py",
        ),
        Feature(
            id="asa-address-scoped",
            name="Address-scoped ASA rules (per-host/per-subnet permits)",
            families=("firewall:5506-X", "firewall:5505"),
            status="Refused",
            evidence="tests/autopilot/test_asa_policy.py::"
            "test_host_scoped_source_is_refused_not_silently_emitted",
            refusal_reason="PT accepts the lines and matches nothing; "
            "emitting them would be a green build that enforces nothing. "
            "Enforce at the router (router-acl-enforce) instead.",
        ),
        Feature(
            id="acl-icmp-port",
            name="dest_port on an icmp ACL rule",
            families=("firewall", "router"),
            status="Refused",
            evidence="tests/autopilot/test_asa_policy.py::"
            "test_dest_port_on_icmp_is_refused_before_pt_aborts_the_block",
            refusal_reason="PT rejects the line and configureIosDevice aborts "
            "the rest of the block, which would cost the interface addresses.",
        ),
        Feature(
            id="asa-5505-third-zone",
            name="Third security zone on the ASA 5505",
            families=("firewall:5505",),
            status="Refused",
            evidence="tests/autopilot/test_asa_policy.py::"
            "test_5505_refuses_more_than_two_zones",
            refusal_reason="PT's 5505 only exposes Vlan1/Vlan2; a third zone "
            "cannot be configured. Use a 5506-X.",
        ),
        Feature(
            id="policy-on-non-enforcer",
            name="ACL policy on a device that enforces nothing (hosts, switches)",
            families=("pc", "server", "switch"),
            status="Refused",
            evidence="tests/autopilot/test_asa_policy.py::"
            "test_policy_on_a_device_with_no_enforcement_path_fails_the_build",
            refusal_reason="The policy would compile and then do nothing. "
            "Only ASA firewalls and IOS routers enforce.",
        ),
        Feature(
            id="hsrp-failover",
            name="HSRP gateway failover",
            families=("router", "l3_switch"),
            status="Unverified",
            evidence="library/T15-redundant-core.json (pending: no compiler "
            "render path; physical topology builds, redundancy unverified)",
        ),
        Feature(
            id="etherchannel",
            name="EtherChannel bundling",
            families=("switch", "l3_switch"),
            status="Unverified",
            evidence="library/T15-redundant-core.json (pending: no compiler "
            "render path; physical topology builds, redundancy unverified)",
        ),
        Feature(
            id="ospf-backbone",
            name="OSPF single-area routing",
            families=("router",),
            status="Unverified",
            evidence="library/T09-ospf-backbone.json (offline lint+compile "
            "pass; no live lab yet)",
        ),
        Feature(
            id="nat-pat",
            name="NAT/PAT internet access",
            families=("router",),
            status="Unverified",
            evidence="library/T10-nat-internet.json (offline lint+compile "
            "pass; no live lab yet)",
        ),
        Feature(
            id="asa-dmz",
            name="ASA three-zone DMZ policy",
            families=("firewall:5506-X",),
            status="Unverified",
            evidence="library/T08-asa-dmz.json (pending S1.16 live proof)",
        ),
    ]


def build_map(features: list[Feature]) -> dict:
    """Validate every row, then render the JSON form."""
    from network_autopilot.core.ids import utc_now_iso

    for feature in features:
        feature.validate()
    if len({f.id for f in features}) != len(features):
        raise ValueError("duplicate feature id")
    return {
        "generated_at": utc_now_iso(),
        "features": [
            {
                "id": f.id,
                "name": f.name,
                "families": list(f.families),
                "status": f.status,
                "evidence": f.evidence,
                "refusal_reason": f.refusal_reason,
            }
            for f in features
        ],
    }


def render_markdown(data: dict) -> str:
    lines = [
        "# Havyn Capability Map",
        "",
        f"_generated {data['generated_at']}_ — statuses change only with the "
        "evidence named in each row.",
        "",
    ]
    for status in STATUSES:
        rows = [f for f in data["features"] if f["status"] == status]
        lines.append(f"## {status} ({len(rows)})")
        lines.append("")
        for row in rows:
            lines.append(
                f"- **{row['id']}** — {row['name']} "
                f"({', '.join(row['families'])}). Evidence: `{row['evidence']}`."
            )
            if row["refusal_reason"]:
                lines.append(f"  Refusal: {row['refusal_reason']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capability-map")
    parser.add_argument("--out-docs", default=str(REPO_ROOT / "docs" / "capability-map.md"))
    parser.add_argument("--out-json", default=str(REPO_ROOT / "docs" / "capability-map.json"))
    args = parser.parse_args(argv)

    try:
        data = build_map(REGISTRY())
    except ValueError as exc:
        print(f"capability-map: {exc}")
        return 1
    Path(args.out_json).write_text(json.dumps(data, indent=1), encoding="utf-8")
    Path(args.out_docs).write_text(render_markdown(data), encoding="utf-8")
    counts = {s: sum(1 for f in data["features"] if f["status"] == s) for s in STATUSES}
    print(f"{len(data['features'])} features {counts} -> {args.out_docs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
