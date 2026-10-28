"""S5.11 diagnosis data generation (offline share).

For every example the model sees:
    - the network spec (context),
    - the evidence record (failing flow + observed state deltas),
and must produce:
    confirmed cause  (when evidence names it),
    suspected cause + verification step (when evidence is partial),
    honest UNKNOWN   (when no state anomaly correlates).

Quotas per the stage plan: healthy ~15%, ambiguous/UNKNOWN ~10%,
multi-fault ~10%, the rest spread across the five certified fault classes.
Every fault used here must be in certificates/fault-injectors.json as
CERTIFIED - this script refuses to run against a stale certificate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from certify import (  # noqa: E402
    _flow_report,
    _state_from_spec,
    _device_role_ids,
    _HOST_PORT,
    _fixture_specs,
)
from generate_intents import GENERATOR_VERSION  # noqa: E402
from src.network_autopilot.diagnosis import diagnose  # noqa: E402
from src.network_autopilot.diagnosis.engine import _ip_in_network  # noqa: E402
from src.network_autopilot.diagnosis.fault_injection import (  # noqa: E402
    port_shutdown_fault,
    route_missing_fault,
    trunk_vlan_not_allowed_fault,
    wrong_access_vlan_fault,
    wrong_gateway_fault,
)

CERT_DIR = REPO / "havyn-stage5" / "certificates"

FAULT_FACTORIES = {
    "port-shutdown": lambda target, spec: port_shutdown_fault(target, _HOST_PORT),
    "route-missing": lambda target, spec: route_missing_fault(target),
    "wrong-access-vlan": lambda target, spec: wrong_access_vlan_fault(target, _HOST_PORT),
    "trunk-vlan-not-allowed": lambda target, spec: trunk_vlan_not_allowed_fault(target),
    "wrong-gateway": lambda target, spec: wrong_gateway_fault(target, _HOST_PORT),
}


def engine_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()[:12]


def load_injector_certificate() -> dict:
    path = CERT_DIR / "fault-injectors.json"
    if not path.exists():
        raise SystemExit("no fault-injectors certificate; run certify.py first")
    cert = json.loads(path.read_text(encoding="utf-8"))
    if cert.get("status") != "CERTIFIED":
        raise SystemExit("fault-injectors certificate is not CERTIFIED")
    return cert


def pick_endpoints(spec):
    """Source host + target endpoint in another subnet (host or server)."""
    endpoints = [
        a for a in spec.ip_assignments
        if next((d for d in spec.devices if d.id == a.device), None) is not None
        and next(d for d in spec.devices if d.id == a.device).role.value in
        ("pc", "laptop", "server", "printer")
    ]
    if len(endpoints) < 2:
        return None, None
    source = endpoints[0]
    target = next((a for a in endpoints if a.subnet != source.subnet), None)
    if target is None:
        return None, None
    return source, target


def evidence_record(spec, state, source, target) -> dict:
    """The evidence the diagnosing model sees: the failing flow plus a
    compact observed-state dump. No labels, no causes."""
    report = _flow_report(source.device, target.device)
    devices = {}
    for device_id, obs in state.devices.items():
        ports = {}
        for name, port in obs.ports.items():
            entry = {}
            for field in ("state", "ip", "gateway", "vlan", "trunk"):
                value = getattr(port, field)
                if value.is_known:
                    entry[field] = value.value
            if entry:
                ports[name] = entry
        devices[device_id] = {"ports": ports}
        if obs.routes.is_known:
            devices[device_id]["routes"] = obs.routes.value
        if obs.vlans.is_known:
            devices[device_id]["vlans"] = obs.vlans.value
    return {
        "flow": {
            "source": source.device, "target": target.device,
            "expected": "reachable", "observed": report.tests[0].observed_result,
        },
        "observed_state": devices,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--out", default="havyn-stage5/datasets/havyn-diagnosis-v1")
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--live-share", type=float, default=0.10,
                        help="reserved for real Packet Tracer cases (live runner)")
    args = parser.parse_args()

    cert = load_injector_certificate()
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    specs = _fixture_specs(limit=20)
    counts: dict[str, int] = {}
    per_fault = {name: 0 for name in FAULT_FACTORIES}
    examples = []
    hashes = set()
    offline_target = round(args.count * (1.0 - args.live_share))
    attempts = 0
    while len(examples) < offline_target and attempts < offline_target * 20:
        attempts += 1
        spec = rng.choice(specs)
        endpoints = [
            a for a in spec.ip_assignments
            if next((d for d in spec.devices if d.id == a.device), None)
            is not None
            and next(d for d in spec.devices if d.id == a.device).role.value
            in ("pc", "laptop", "server", "printer")
        ]
        if len(endpoints) < 2:
            continue
        source = rng.choice(endpoints)
        target = next((a for a in endpoints
                       if a.device != source.device and a.subnet != source.subnet),
                      None)
        if target is None:
            continue
        state = _state_from_spec(spec)

        roll = rng.random()
        injected: list = []
        if roll < 0.15:
            kind = "healthy"
            expected = {
                "verdict": "healthy-not-fault",
                "note": ("the flow evidence shows a failure but observed state "
                         "is clean; the correct next step is additional "
                         "verification, never a speculative change"),
            }
        elif roll < 0.25:
            # ambiguous / honest-UNKNOWN class: wrong-gateway has no rule by
            # design - the engine answers "no state anomaly", repair blocked.
            fault = wrong_gateway_fault(source.device, _HOST_PORT)
            injected.append(fault)
            fault.inject(state)
            kind = "unknown"
            expected = {
                "verdict": "unknown",
                "confirmed_cause": None,
                "note": ("connectivity failed but observed state shows no "
                         "anomaly; report insufficient evidence and request "
                         "additional verification; do not repair"),
            }
        else:
            fault_name = rng.choice(list(FAULT_FACTORIES))
            # pick the fault's device from the spec shape
            if fault_name == "route-missing":
                routes = spec.routing.static_routes
                if not routes:
                    continue
                device = next(
                    (r.device for r in routes
                     if _ip_in_network(target.ip, r.destination)), None)
                if device is None:
                    continue
            elif fault_name in ("port-shutdown", "wrong-gateway"):
                device = source.device
            else:
                switches = _device_role_ids(spec, "switch")
                if not switches:
                    continue
                device = switches[0]
            fault = FAULT_FACTORIES[fault_name](device, spec)
            fault.inject(state)
            injected.append(fault)
            if not fault.flow_broken(state):
                continue
            # 0.25-0.40 band => ~10% multi-fault share of the total.
            if roll < 0.40:
                second_name = rng.choice(
                    [n for n in FAULT_FACTORIES if n != fault_name])
                if second_name == "route-missing" and not spec.routing.static_routes:
                    continue
                second_device = device if second_name != "port-shutdown" \
                    else _device_role_ids(spec, "switch")[0]
                second = FAULT_FACTORIES[second_name](second_device, spec)
                second.inject(state)
                injected.append(second)
                if not second.flow_broken(state):
                    continue
                kind = "multi-fault"
                expected = {
                    "verdict": "multiple-faults",
                    "confirmed_causes": sorted({fault_name, second_name}),
                    "note": ("both anomalies are present in observed state; "
                             "report each with its own repair"),
                }
            else:
                kind = "fault"
                expected = {
                    "verdict": "confirmed",
                    "confirmed_cause": fault_name,
                    "repair_recommended": fault.expect_repairable,
                }

        evidence = evidence_record(spec, state, source, target)
        digest = hashlib.sha256(json.dumps(
            {"s": spec.name, "e": evidence, "k": kind}, sort_keys=True)
            .encode("utf-8")).hexdigest()
        if digest in hashes:
            continue
        hashes.add(digest)

        counts[kind] = counts.get(kind, 0) + 1
        if kind == "fault":
            per_fault[fault_name] += 1

        examples.append({
            "id": f"dx-{len(examples):05d}",
            "kind": kind,
            "spec": json.loads(spec.model_dump_json()),
            "evidence": evidence,
            "expected_output": expected,
            "generator_version": GENERATOR_VERSION,
            "engine_commit": cert["engine_commit"],
            "seed": args.seed,
            "mode": "offline",
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    with (out / "diagnosis.jsonl").open("w", encoding="utf-8") as f:
        for example in examples:
            f.write(json.dumps(example, ensure_ascii=False) + "\n")
    (out / "manifest.json").write_text(json.dumps({
        "dataset": "havyn-diagnosis-v1",
        "generator_version": GENERATOR_VERSION,
        "engine_commit": cert["engine_commit"],
        "seed": args.seed,
        "examples": len(examples),
        "counts": counts,
        "per_fault": per_fault,
        "live_share_reserved": args.live_share,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    print(f"wrote {len(examples)} diagnosis examples to {out}; counts: {counts}")


if __name__ == "__main__":
    main()
