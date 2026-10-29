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
    """One capability row.

    ``topology`` links a row to a library spec so a live runner pass can
    promote an otherwise-unverified capability. The promotion happens in
    ``build_map`` from the result files, never by editing the row.
    """

    id: str
    name: str
    families: tuple[str, ...]
    status: str
    evidence: str
    refusal_reason: str = ""
    topology: str = ""

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
    rows = [
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
            "test_asa_address_scoped_acl_is_enforced",
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
            status="Verified",
            evidence="tests/autopilot/test_live_policy.py::"
            "test_asa_address_scoped_acl_is_enforced",
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
            topology="T15",
        ),
        Feature(
            id="etherchannel",
            name="EtherChannel bundling",
            families=("switch", "l3_switch"),
            status="Unverified",
            evidence="library/T15-redundant-core.json (pending: no compiler "
            "render path; physical topology builds, redundancy unverified)",
            topology="T15",
        ),
        Feature(
            id="ospf-backbone",
            name="OSPF single-area routing",
            families=("router",),
            status="Unverified",
            evidence="library/T09-ospf-backbone.json (offline lint+compile "
            "pass; no live lab yet)",
            topology="T09",
        ),
        Feature(
            id="nat-pat",
            name="NAT/PAT internet access",
            families=("router",),
            status="Unverified",
            evidence="library/T10-nat-internet.json (offline lint+compile "
            "pass; no live lab yet)",
            topology="T10",
        ),
        Feature(
            id="asa-dmz",
            name="ASA three-zone DMZ policy",
            families=("firewall:5506-X",),
            status="Unverified",
            evidence="library/T08-asa-dmz.json (pending S1.16 live proof)",
            topology="T08",
        ),
        Feature(
            id="small-office-dhcp",
            name="Single-router office with DHCP",
            families=("router", "switch", "pc"),
            status="Unverified",
            evidence="library/T01-small-office.json (builds offline; live runner "
            "pass promotes this row)",
            topology="T01",
        ),
        Feature(
            id="router-on-a-stick",
            name="Router-on-a-stick inter-VLAN routing",
            families=("router", "switch", "pc"),
            status="Unverified",
            evidence="library/T02-two-vlan-office.json (builds offline; live "
            "runner pass promotes this row)",
            topology="T02",
        ),
        Feature(
            id="campus-l3-core",
            name="Three-floor campus with an L3 core",
            families=("l3_switch", "switch", "pc"),
            status="Unverified",
            evidence="library/T03-campus-l3-core.json (builds offline; no live "
            "runner pass yet)",
            topology="T03",
        ),
        Feature(
            id="serial-wan",
            name="HQ + branch over a serial WAN",
            families=("router",),
            status="Unverified",
            evidence="library/T04-serial-wan.json (builds offline; no live "
            "runner pass yet)",
            topology="T04",
        ),
        Feature(
            id="dsl-internet",
            name="DSL access through a cloud/modem pair",
            families=("router", "cloud", "modem"),
            status="Unverified",
            evidence="library/T05-dsl-internet.json (builds offline; no live "
            "runner pass yet)",
            topology="T05",
        ),
        Feature(
            id="cable-modem-home",
            name="Home cable-modem network",
            families=("modem", "router", "pc"),
            status="Unverified",
            evidence="library/T06-home-cable.json (builds offline; live runner "
            "pass promotes this row)",
            topology="T06",
        ),
        Feature(
            id="asa-perimeter",
            name="ASA inside/outside perimeter",
            families=("firewall:5506-X", "firewall:5505"),
            status="Unverified",
            evidence="library/T07-asa-perimeter.json (pending S1.16 live proof)",
            topology="T07",
        ),
        Feature(
            id="wireless-office",
            name="Wireless office with AP-attached laptops and phones",
            families=("accesspoint", "laptop", "smartphone"),
            status="Unverified",
            evidence="library/T11-wireless-office.json (builds offline; no live "
            "runner pass yet)",
            topology="T11",
        ),
        Feature(
            id="linksys-home",
            name="Linksys home gateway",
            families=("home_router", "pc"),
            status="Unverified",
            evidence="library/T12-linksys-home.json (builds offline; no live "
            "runner pass yet)",
            topology="T12",
        ),
        Feature(
            id="server-farm",
            name="DNS + HTTP + DHCP server farm",
            families=("server", "switch", "pc"),
            status="Unverified",
            evidence="library/T13-server-farm.json (builds offline; no live "
            "runner pass yet)",
            topology="T13",
        ),
        Feature(
            id="iot-smart-home",
            name="IoT smart-home devices",
            families=("iot", "home_router"),
            status="Unverified",
            evidence="library/T14-iot-home.json (builds offline; no live runner "
            "pass yet)",
            topology="T14",
        ),
        Feature(
            id="enterprise-campus",
            name="Enterprise campus reference build",
            families=("router", "switch", "firewall", "server", "pc"),
            status="Unverified",
            evidence="library/T16-enterprise-campus.json (builds offline; no "
            "live runner pass in this library)",
            topology="T16",
        ),
    ]
    return rows + _stage3_rows()


def _stage3_rows() -> list[Feature]:
    """The 53 Stage 3 tracks, kept in their own reviewable registry."""
    try:
        from stage3_registry import stage3_features
    except ImportError:  # imported as scripts.capability_map
        from .stage3_registry import stage3_features
    return [Feature(**row) for row in stage3_features()]


def load_live_results(results_dir: Path) -> dict[str, dict]:
    """The latest live execution per topology, from results or history.

    The runner writes one result per case and an append-only history. History
    is preferred when present because it survives re-runs; either way only
    ``mode == "live"`` entries count — an offline NullAdapter pass is not
    evidence that Packet Tracer can do the thing.
    """
    entries: list[dict] = []
    history = results_dir / "history.jsonl"
    if history.is_file():
        for line in history.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    else:
        for path in sorted(results_dir.glob("*.json")):
            if path.name == "summary.json":
                continue
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(entry, dict) and entry.get("status"):
                entries.append(entry)

    latest: dict[str, dict] = {}
    for entry in entries:
        if entry.get("mode") != "live" or not entry.get("status"):
            continue
        topology = entry.get("topology_id")
        if not topology and entry.get("spec"):
            topology = Path(str(entry["spec"])).stem
        if not topology:
            continue
        stamp = (str(entry.get("finished_at") or ""), str(entry.get("run_id") or ""))
        prior = latest.get(str(topology))
        prior_stamp = (
            (str(prior.get("finished_at") or ""), str(prior.get("run_id") or ""))
            if prior else ("", "")
        )
        if prior is None or stamp >= prior_stamp:
            latest[str(topology)] = entry
    return latest


def _run_is_complete_pass(entry: dict) -> bool:
    """A live PASS only counts when every criterion passed, none unknown."""
    if entry.get("status") != "pass" or entry.get("refused"):
        return False
    criteria = entry.get("criteria") or {}
    total = int(criteria.get("total", 0))
    return (
        total > 0
        and int(criteria.get("pass", 0)) == total
        and int(criteria.get("fail", 0)) == 0
        and int(criteria.get("unknown", 0)) == 0
    )


def build_map(features: list[Feature], runs: dict[str, dict] | None = None) -> dict:
    """Validate every row, apply live-run evidence, then render the JSON form.

    Promotion is one-way and evidence-gated: an ``Unverified`` row whose
    topology has a complete live PASS becomes ``Verified`` and carries that
    run id. A failed live run is recorded beside the row but never downgrades
    or upgrades its status by itself — one failed attempt does not prove the
    capability absent, and it certainly does not prove it present.
    """
    from network_autopilot.core.ids import utc_now_iso

    for feature in features:
        feature.validate()
    if len({f.id for f in features}) != len(features):
        raise ValueError("duplicate feature id")

    rows = []
    for f in features:
        run = (runs or {}).get(f.topology) if f.topology else None
        row = {
            "id": f.id,
            "name": f.name,
            "families": list(f.families),
            "status": f.status,
            "evidence": f.evidence,
            "refusal_reason": f.refusal_reason,
            "topology": f.topology,
            "derived_from_run": False,
            "latest_run": None,
        }
        if run is not None:
            row["latest_run"] = {
                "topology_id": run.get("topology_id") or f.topology,
                "case_id": run.get("case_id"),
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "elapsed_s": run.get("elapsed_s"),
                "criteria": dict(run.get("criteria") or {}),
                "evidence_dir": run.get("evidence_dir"),
                "finished_at": run.get("finished_at"),
            }
            if f.status == "Unverified" and _run_is_complete_pass(run):
                row["status"] = "Verified"
                row["derived_from_run"] = True
                row["evidence"] = (
                    f"{f.evidence}; live runner pass {run.get('run_id')} "
                    f"({run.get('evidence_dir') or 'results dir'})"
                )
        rows.append(row)

    return {
        "generated_at": utc_now_iso(),
        "features": rows,
        "runs": {
            topology: {
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "elapsed_s": run.get("elapsed_s"),
                "evidence_dir": run.get("evidence_dir"),
                "finished_at": run.get("finished_at"),
            }
            for topology, run in sorted((runs or {}).items())
        },
    }


def render_markdown(data: dict) -> str:
    lines = [
        "# Havyn Capability Map",
        "",
        f"_generated {data['generated_at']}_ — statuses change only with the "
        "evidence named in each row.",
        "",
        "## Status meanings",
        "",
        "- **Verified** — live Packet Tracer evidence in this repository "
        "proves the capability (a live test or a verbatim probe capture). "
        "Implemented code alone is not Verified.",
        "- **Refused** — Packet Tracer lacks or blocks the required capability "
        "and Havyn intentionally refuses it at compile-time/runtime instead of "
        "emitting a config that would half-work or report a false PASS. A "
        "manual GUI path may exist; that is not automation proof.",
        "- **Unverified** — not proven; no claim is made.",
        "",
        "Offline tests prove internal consistency, not PT behaviour; only the "
        "live evidence named in a row proves that. Refused is an "
        "evidence-backed capability gap, not a failed test run.",
        "",
    ]
    if data.get("runs"):
        lines.append("## Latest live runner evidence")
        lines.append("")
        lines.append("| topology | status | run | elapsed (s) | evidence |")
        lines.append("| --- | --- | --- | --- | --- |")
        for topology, run in data["runs"].items():
            lines.append(
                f"| {topology} | {run.get('status')} | {run.get('run_id') or '—'} "
                f"| {run.get('elapsed_s') if run.get('elapsed_s') is not None else '—'} "
                f"| {run.get('evidence_dir') or '—'} |"
            )
        lines.append("")
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
            if row.get("latest_run"):
                run = row["latest_run"]
                lines.append(
                    f"  Latest live run `{run.get('run_id') or '—'}`: "
                    f"{run.get('status')} ({run.get('elapsed_s')} s, "
                    f"`{run.get('evidence_dir') or 'no evidence dir'}`)."
                )
            if row.get("derived_from_run"):
                lines.append(
                    "  Status derived from that complete live runner pass."
                )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capability-map")
    parser.add_argument("--out-docs", default=str(REPO_ROOT / "docs" / "capability-map.md"))
    parser.add_argument("--out-json", default=str(REPO_ROOT / "docs" / "capability-map.json"))
    parser.add_argument(
        "--results-dir", default=None,
        help="runner results dir; live passes promote Unverified rows",
    )
    args = parser.parse_args(argv)

    runs = None
    if args.results_dir:
        try:
            runs = load_live_results(Path(args.results_dir))
        except OSError as exc:
            print(f"capability-map: cannot read results: {exc}")
            return 1
    try:
        data = build_map(REGISTRY(), runs)
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
