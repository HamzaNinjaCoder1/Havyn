#!/usr/bin/env python
"""Build a NetworkSpec against a live Packet Tracer and verify the result.

    python build_havyn.py <spec.json> [safety_mode]

This is the one-shot entry point: it reads the spec, opens the bridge, runs the
full pipeline (compile -> preflight -> gated execution -> verification) and
prints the per-criterion report. The build is not "done" because the script
exited 0 - it is done when the printed report says so.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from network_autopilot.core.network_spec import NetworkSpec
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from network_autopilot.evidence.store import EvidenceStore
from network_autopilot.orchestrator import Autopilot

DEFAULT_SPEC = "my-network.spec.json"
EVIDENCE_DIR = "autopilot_evidence"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_havyn",
        description="Build a NetworkSpec in a live Packet Tracer and verify it.",
    )
    parser.add_argument(
        "spec", nargs="?", default=DEFAULT_SPEC,
        help=f"spec file to build (default: {DEFAULT_SPEC})",
    )
    parser.add_argument(
        "safety_mode", nargs="?", default="autonomous",
        choices=[mode.value for mode in SafetyMode],
        help="safety gate mode (default: autonomous)",
    )
    parser.add_argument(
        "--evidence-dir", default=EVIDENCE_DIR,
        help=f"where to write the evidence record (default: {EVIDENCE_DIR})",
    )
    parser.add_argument(
        "--budget", type=int, default=None,
        help="mutation budget; raise it for a topology larger than the default",
    )
    args = parser.parse_args(argv)

    spec_path = Path(args.spec)
    if not spec_path.is_file():
        print(f"spec not found: {spec_path}", file=sys.stderr)
        return 2

    spec = NetworkSpec.from_json(spec_path.read_text(encoding="utf-8"))
    print(f"spec: {spec.name}  devices={len(spec.devices)}  links={len(spec.links)}")

    budget = MutationBudget(max_mutations=args.budget) if args.budget else MutationBudget()
    gate = SafetyGate(SafetyMode(args.safety_mode), budget)
    evidence = EvidenceStore(root=Path(args.evidence_dir))
    adapter = BridgeAdapter(host_bridge=True, evidence=evidence)

    probe = adapter.ping(timeout=3.0)
    detail = probe.detail.value if probe.detail.is_known else str(probe.detail)
    print("PT reachable:", probe.reachable, "|", detail)
    if not probe.reachable:
        print(
            "Packet Tracer is not answering on either channel. Open it with the "
            "MCP Control Center extension loaded, then re-run.",
            file=sys.stderr,
        )
        return 3

    start = time.time()
    build, error = Autopilot(adapter, gate, evidence).build(spec)
    elapsed = time.time() - start

    if error:
        print("FAILED:", error, file=sys.stderr)
        return 1

    print(f"\nTIME: {elapsed:.1f}s  ({elapsed / len(spec.devices):.2f}s/device)\n")
    print(build.human_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
