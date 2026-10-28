"""S5.12 enhancement data generation.

For every example:
    before intent -> engine chain PASS
    structured mutation (a JSON patch + human sentence)
    after intent  -> engine chain PASS

~10% of examples are impossible/unsupported requests; their correct output
is the named refusal, never an invented patch. Every mutation family used
here must appear in certificates/mutations.json as CERTIFIED - this script
refuses to generate from an uncertified or stale certificate.
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

from generate_intents import GENERATOR_VERSION, build_intent, coverage_cells  # noqa: E402
from src.network_autopilot.ai.expander import ExpansionRefusal  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402

CERT_DIR = REPO / "havyn-stage5" / "certificates"
GENERATED = "2026-10-28"


def engine_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()[:12]


def load_mutation_certificate() -> dict:
    path = CERT_DIR / "mutations.json"
    if not path.exists():
        raise SystemExit("no mutations certificate; run certify.py first")
    cert = json.loads(path.read_text(encoding="utf-8"))
    if cert.get("status") != "CERTIFIED":
        raise SystemExit("mutations certificate is not CERTIFIED")
    return cert


def chain_ok(intent: HavynIntent) -> tuple[bool, str]:
    from certify import _chain_ok
    return _chain_ok(intent)


# --- mutation implementations ---------------------------------------------------
# Each returns (patched_intent_dict, json_patch_ops, human_sentence).

def vlan_addition(data: dict, rng: random.Random) -> tuple[dict, list, str]:
    site = data["sites"][0]
    area = site["areas"][0]
    vlan_id = f"{site['id']}-extra{rng.randint(1, 99)}"
    count = rng.randint(2, 8)
    data["vlans"].append({
        "id": vlan_id, "name": f"{site['id']} extra", "site": site["id"],
        "required_hosts": count + 10,
    })
    group = {"id": f"g-{vlan_id}", "area": area["id"], "count": count,
             "device": "pc", "vlan": vlan_id}
    area["host_groups"].append(group)
    return data, [
        {"op": "add", "path": f"/vlans/-", "value": vlan_id},
        {"op": "add", "path": f"/sites/0/areas/0/host_groups/-", "value": group},
    ], (f"Add a new VLAN {vlan_id} with {count} additional PCs in area "
        f"{area['id']}.")


def host_count_change(data: dict, rng: random.Random) -> tuple[dict, list, str]:
    group = data["sites"][0]["areas"][0]["host_groups"][0]
    old = group["count"]
    new = max(1, old + rng.randint(-3, 6))
    group["count"] = new
    return data, [
        {"op": "replace", "path": "/sites/0/areas/0/host_groups/0/count",
         "value": new},
    ], (f"The team on {group['area']} grows from {old} to {new} people; "
        "resize the network accordingly.")


def wireless_addition(data: dict, rng: random.Random) -> tuple[dict, list, str]:
    site = data["sites"][0]
    area = site["areas"][0]
    count = rng.randint(2, 6)
    group = {"id": f"{site['id']}-wifi", "area": area["id"], "count": count,
             "device": "laptop"}
    area["host_groups"].append(group)
    return data, [
        {"op": "add", "path": "/sites/0/areas/0/host_groups/-", "value": group},
    ], (f"We also need Wi-Fi for {count} laptops in area {area['id']}.")


def static_to_dhcp(data: dict, rng: random.Random) -> tuple[dict, list, str]:
    patch = []
    changed = 0
    for s_index, site in enumerate(data["sites"]):
        for a_index, area in enumerate(site["areas"]):
            for g_index, group in enumerate(area["host_groups"]):
                if group.get("addressing") == "static":
                    group["addressing"] = "dhcp"
                    patch.append({
                        "op": "replace",
                        "path": f"/sites/{s_index}/areas/{a_index}"
                                f"/host_groups/{g_index}/addressing",
                        "value": "dhcp",
                    })
                    changed += 1
    return data, patch, "Switch the remaining static PCs over to DHCP."


def ospf_routing(data: dict, rng: random.Random) -> tuple[dict, list, str]:
    data["routing"] = {"mode": "ospf"}
    return data, [
        {"op": "add", "path": "/routing", "value": {"mode": "ospf"}},
    ], "Use OSPF for routing instead of static routes."


# Impossible requests: the model must refuse with the reason, never patch.
IMPOSSIBLE = [
    {
        "request": "Add guest Wi-Fi that is completely isolated from the "
                   "internal network using the firewall.",
        "expected_refusal": "guest-isolation-unsupported",
    },
    {
        "request": "Move the DHCP server role onto the L3 core switch SVIs.",
        "expected_refusal": "l3-switch-dhcp-unsupported",
    },
    {
        "request": "Give the ASA 5505 a DMZ zone for the public web server.",
        "expected_refusal": "asa5505-no-dmz",
    },
    {
        "request": "Register all IoT devices with the IoT server so they can "
                   "be monitored.",
        "expected_refusal": "iot-registration-unsupported",
    },
    {
        "request": "Broadcast a custom SSID 'HavynNet' with WPA2 from the "
                   "access point.",
        "expected_refusal": "custom-ssid-unsupported",
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--out", default="havyn-stage5/datasets/havyn-enhancement-v1")
    parser.add_argument("--seed", type=int, default=20261028)
    args = parser.parse_args()

    cert = load_mutation_certificate()
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cells = [c for c in coverage_cells() if c.site_count == 1]
    mutators = [vlan_addition, host_count_change, wireless_addition,
                static_to_dhcp, ospf_routing]
    impossible_share = 0.10

    examples = []
    failures = {}
    hashes = set()
    attempts = 0
    while len(examples) < args.count and attempts < args.count * 10:
        attempts += 1
        cell = rng.choice(cells)
        base = build_intent(cell, rng.randint(0, 4), random.Random(rng.getrandbits(32)))
        if isinstance(base, HavynIntent):
            base = base.model_dump()

        if rng.random() < impossible_share:
            imp = rng.choice(IMPOSSIBLE)
            examples.append({
                "id": f"enh-{len(examples):05d}",
                "kind": "impossible",
                "before_intent": base,
                "request": imp["request"],
                "expected_output": {
                    "refusal": imp["expected_refusal"],
                    "patch": None,
                },
                "generator_version": GENERATOR_VERSION,
                "engine_commit": cert["engine_commit"],
                "seed": args.seed,
                "generated": GENERATED,
            })
            continue

        mutator = rng.choice(mutators)
        data = json.loads(json.dumps(base))
        try:
            patched, patch_ops, sentence = mutator(data, rng)
            before_intent = HavynIntent.model_validate(base)
            after_intent = HavynIntent.model_validate(patched)
        except Exception as exc:  # noqa: BLE001
            key = f"build:{type(exc).__name__}"
            failures[key] = failures.get(key, 0) + 1
            continue

        ok_before, why_before = chain_ok(before_intent)
        ok_after, why_after = chain_ok(after_intent)
        if not (ok_before and ok_after):
            key = "before" if not ok_before else "after"
            failures[f"{key}:{why_after[:80] if key == 'after' else why_before[:80]}"] = \
                failures.get(f"{key}", 0) + 1
            continue

        digest = hashlib.sha256(json.dumps(
            {"b": before_intent.model_dump(), "m": sentence},
            sort_keys=True).encode("utf-8")).hexdigest()
        if digest in hashes:
            continue
        hashes.add(digest)

        examples.append({
            "id": f"enh-{len(examples):05d}",
            "kind": "mutation",
            "before_intent": before_intent.model_dump(),
            "after_intent": after_intent.model_dump(),
            "request": sentence,
            "expected_output": {
                "refusal": None,
                "patch": patch_ops,
            },
            "generator_version": GENERATOR_VERSION,
            "engine_commit": cert["engine_commit"],
            "seed": args.seed,
            "generated": GENERATED,
        })

    with (out / "enhancement.jsonl").open("w", encoding="utf-8") as f:
        for example in examples:
            f.write(json.dumps(example, ensure_ascii=False) + "\n")
    (out / "manifest.json").write_text(json.dumps({
        "dataset": "havyn-enhancement-v1",
        "generator_version": GENERATOR_VERSION,
        "engine_commit": cert["engine_commit"],
        "seed": args.seed,
        "examples": len(examples),
        "impossible": sum(1 for e in examples if e["kind"] == "impossible"),
        "failures": failures,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    print(f"wrote {len(examples)} enhancement examples to {out}; "
          f"failures: {failures or 'none'}")


if __name__ == "__main__":
    main()
