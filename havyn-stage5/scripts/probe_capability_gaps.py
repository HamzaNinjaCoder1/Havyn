"""Stage 5: execute the capability-gap claims through the deterministic engine.

The register in ``../prompt-authoring/reports/capability_gap_register.md``
claims that certain shapes are refused or not representable. A grep proves the
code exists; this script proves the engine actually behaves that way for a
minimal triggering intent.

Evidence produced here is **engine-executed, offline**:
    schema -> intent lint -> expander (no Packet Tracer, no compile to PT).

It is still not a live Packet Tracer verification, and it does not execute the
full wire-input of a specific candidate. Each probe constructs the smallest
intent that should trigger the claim and records the stage + code observed.

Usage:
    python probe_capability_gaps.py            # write JSON + print summary
    python probe_capability_gaps.py --quiet
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.network_autopilot.ai.expander import (  # noqa: E402
    ExpansionRefusal,
    IntentExpander,
)
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402

OUT = REPO / "havyn-stage5" / "prompt-authoring" / "reports" / "capability-gap-probes.json"


def _group(gid: str, area: str, count: int, device: str = "pc", vlan: str | None = None) -> dict:
    group = {"id": gid, "area": area, "count": count, "device": device}
    if vlan is not None:
        group["vlan"] = vlan
    return group


def _vlan(vid: str, site: str | None = "s1", **extra) -> dict:
    data = {"id": vid, "name": vid, "site": site, "dhcp": True}
    if "cidr" not in extra:
        data["required_hosts"] = 20
    data.update(extra)
    return data


def _site(sid: str, group: dict | None = None) -> dict:
    groups = [group or _group("pcs", "a1", 4)]
    return {"id": sid, "areas": [{"id": "a1", "kind": "area", "host_groups": groups}]}


def _base(**overrides) -> dict:
    data = {
        "intent_version": "1",
        "name": "probe",
        "sites": [_site("s1")],
        "vlans": [_vlan("v1")],
        "routing": {"mode": "connected"},
        "internet": {"access": "wan", "nat": True, "public_server": False},
    }
    data.update(overrides)
    return data


def _run(intent_data: dict) -> dict:
    """Return the first blocking stage/code, or an expansion result."""
    try:
        intent = HavynIntent.model_validate(intent_data)
    except Exception as exc:  # noqa: BLE001
        return {"stage": "schema", "code": type(exc).__name__, "detail": str(exc)[:300]}

    lint = lint_intent(intent)
    if lint.blocking:
        finding = lint.blocking[0]
        return {
            "stage": "intent-lint",
            "code": finding.code,
            "detail": finding.message,
        }

    try:
        result = IntentExpander(intent).expand()
    except ExpansionRefusal as exc:
        return {"stage": "expander", "code": exc.code, "detail": exc.message}
    except Exception as exc:  # noqa: BLE001
        return {"stage": "error", "code": type(exc).__name__, "detail": str(exc)[:300]}

    return {"stage": "expander-success", "code": None, "detail": None, "result": result}


def _expect_code(name: str, gap: str, claim: str, intent_data: dict, code: str) -> dict:
    observed = _run(intent_data)
    return {
        "probe": name,
        "gap": gap,
        "claim": claim,
        "expected": {"code": code},
        "observed": {k: v for k, v in observed.items() if k != "result"},
        "matched": observed.get("code") == code,
    }


def _expect_observation(
    name: str, gap: str, claim: str, intent_data: dict, observations: dict
) -> dict:
    observed = _run(intent_data)
    if observed["stage"] != "expander-success":
        return {
            "probe": name,
            "gap": gap,
            "claim": claim,
            "expected": observations,
            "observed": {k: v for k, v in observed.items() if k != "result"},
            "matched": False,
        }
    result = observed["result"]
    spec = result.spec
    actual = {
        "acl_policy_count": sum(1 for p in spec.policies if p.kind == "acl"),
        "acl_policy_ids": [p.id for p in spec.policies if p.kind == "acl"],
        "blocked_criteria": sum(
            1 for c in spec.acceptance_criteria if getattr(c.test, "expect", None) == "blocked"
        ),
        "iot_device_count": sum(1 for d in spec.devices if d.role == "iot"),
        "iot_ip_assignments": sorted(
            {a.device for a in spec.ip_assignments if a.device.startswith("lights-")}
        ),
        "net_wan_subnets": [s.id for s in spec.subnets if s.id == "net-wan"],
        "net_wan_assignments": [
            (a.device, a.ip) for a in spec.ip_assignments if a.subnet == "net-wan"
        ],
        "isp_device_count": sum(1 for d in spec.devices if d.id == "isp"),
    }
    return {
        "probe": name,
        "gap": gap,
        "claim": claim,
        "expected": observations,
        "observed": actual,
        "matched": all(actual.get(k) == v for k, v in observations.items()),
    }


def probes() -> list[dict]:
    two_vlans = {
        "vlans": [_vlan("v1"), _vlan("v2")],
        "sites": [
            {
                "id": "s1",
                "areas": [
                    {
                        "id": "a1",
                        "kind": "area",
                        "host_groups": [
                            _group("pcs1", "a1", 4, vlan="v1"),
                            _group("pcs2", "a1", 4, vlan="v2"),
                        ],
                    }
                ],
            }
        ],
    }
    two_sites = {
        "sites": [
            _site("s1", _group("pcs1", "a1", 4, vlan="v1")),
            _site("s2", _group("pcs2", "a1", 4, vlan="v1")),
        ],
        "vlans": [_vlan("v1", site=None)],
    }

    out: list[dict] = []

    out.append(
        _expect_code(
            "guest-isolation",
            "CG-4",
            "guest_isolation is refused by name",
            _base(firewall={"enabled": True, "model": "asa5506", "guest_isolation": True}),
            "guest-isolation-unsupported",
        )
    )
    out.append(
        _expect_code(
            "guest-vlan-without-isolation",
            "CG-4",
            "an is_guest VLAN without guest_isolation is a blocking lint finding",
            _base(vlans=[_vlan("guest", is_guest=True)]),
            "guest-vlan-without-isolation",
        )
    )
    out.append(
        _expect_code(
            "dmz-multi-site",
            "CG-6",
            "a DMZ on a multi-site intent is refused by name",
            _base(
                **two_sites,
                firewall={"enabled": True, "model": "asa5506", "dmz": True},
            ),
            "dmz-multi-site-unsupported",
        )
    )
    out.append(
        _expect_code(
            "asa5505-no-dmz",
            "CG-6",
            "an ASA 5505 with a DMZ is refused by name",
            _base(firewall={"enabled": True, "model": "asa5505", "dmz": True}),
            "asa5505-no-dmz",
        )
    )
    out.append(
        _expect_code(
            "firewall-needs-single-vlan",
            "CG-6",
            "the ASA inside gateway requires exactly one VLAN",
            _base(**two_vlans, firewall={"enabled": True, "model": "asa5506"}),
            "firewall-needs-single-vlan",
        )
    )
    out.append(
        _expect_code(
            "asa5505-multi-site",
            "CG-6",
            "an ASA 5505 on a multi-site intent is a blocking lint finding",
            _base(**two_sites, firewall={"enabled": True, "model": "asa5505"}),
            "asa5505-multi-site",
        )
    )
    out.append(
        _expect_code(
            "vlan-outside-site-block",
            "CG-10",
            "a fixed VLAN CIDR outside the site block is refused by name",
            _base(vlans=[_vlan("v1", cidr="192.168.50.0/24")]),
            "vlan-outside-site-block",
        )
    )
    out.append(
        _expect_code(
            "server-dhcp-service",
            "CG-5",
            "a Server-PT DHCP service is a blocking lint finding",
            _base(servers=[{"id": "srv", "site": "s1", "vlan": "v1", "services": ["dhcp"]}]),
            "server-dhcp-service",
        )
    )
    out.append(
        _expect_code(
            "wireless-group-too-large",
            "CG-9",
            "a wireless host group above 8 clients is a blocking lint finding",
            _base(
                sites=[
                    {
                        "id": "s1",
                        "areas": [
                            {
                                "id": "a1",
                                "kind": "area",
                                "host_groups": [
                                    _group("laptops", "a1", 9, device="laptop", vlan="v1")
                                ],
                            }
                        ],
                    }
                ],
                wireless=[
                    {
                        "id": "wifi",
                        "site": "s1",
                        "area": "a1",
                        "ssid": "ProbeWifi",
                        "security": "wpa2",
                        "passphrase": "probe-pass-2026",
                        "client_group_ids": ["laptops"],
                    }
                ],
            ),
            "wireless-group-too-large",
        )
    )
    out.append(
        _expect_code(
            "iot-camera-kind",
            "CG-7",
            "the IoT kind enum cannot express a camera",
            _base(
                iot=[
                    {
                        "id": "cameras",
                        "site": "s1",
                        "area": "a1",
                        "kind": "camera",
                        "count": 2,
                    }
                ]
            ),
            "ValidationError",
        )
    )
    out.append(
        _expect_code(
            "bonded-access",
            "CG-7",
            "the internet access enum cannot express a bonded DSL+cable link",
            _base(internet={"access": "bonded", "nat": True, "public_server": False}),
            "ValidationError",
        )
    )
    out.append(
        _expect_observation(
            "cg1-no-inter-vlan-acl",
            "CG-1",
            "a two-VLAN site emits no inter-VLAN ACL and no blocked criterion",
            _base(**two_vlans),
            {"acl_policy_count": 0, "blocked_criteria": 0},
        )
    )
    out.append(
        _expect_observation(
            "cg2-iot-no-ip-placement",
            "CG-2",
            "IoT things are created but receive no IP assignment",
            _base(
                iot=[
                    {
                        "id": "lights",
                        "site": "s1",
                        "area": "a1",
                        "kind": "smart_led",
                        "count": 2,
                    }
                ]
            ),
            {"iot_device_count": 2, "iot_ip_assignments": []},
        )
    )
    out.append(
        _expect_observation(
            "cg3-one-internet-block",
            "CG-3",
            "a two-site intent builds exactly one internet block at sites[0]",
            _base(**two_sites),
            {
                "net_wan_subnets": ["net-wan"],
                "isp_device_count": 1,
                "net_wan_assignments": [("s1-r1", "203.0.113.1"), ("isp", "203.0.113.2")],
            },
        )
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    results = probes()
    report = {
        "source_state": "working tree at engine commit e8503cdfb32a83817a2fbc84ad4245057acf75a2 (ai/ was untracked there; see reproducibility note)",
        "evidence_class": "engine-executed offline (schema -> intent lint -> expander); no Packet Tracer, no compile to PT",
        "probes": results,
        "summary": {
            "probes": len(results),
            "matched": sum(1 for r in results if r["matched"]),
            "unmatched": [r["probe"] for r in results if not r["matched"]],
        },
    }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if not args.quiet:
        for result in results:
            status = "OK  " if result["matched"] else "MISS"
            observed = result["observed"]
            print(
                f"{status} {result['probe']:<32} {observed.get('stage')}:"
                f"{observed.get('code')}"
            )
    print(json.dumps(report["summary"], indent=2))
    if report["summary"]["unmatched"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
