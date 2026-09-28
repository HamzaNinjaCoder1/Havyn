#!/usr/bin/env python
"""Build a NetworkSpec against a live Packet Tracer and verify the result.

    python build_havyn.py <spec.json> [safety_mode]

This is the one-shot entry point: it reads the spec, opens the bridge, runs the
full pipeline (compile -> preflight -> gated execution -> verification) and
prints the per-criterion report.

Exit codes (S1.3) — the process status IS the verdict, so scripts and CI never
read a failed build as green:

    0  SUCCESS (every verification test passed) or PLAN OK (a dry run that
       completed as intended)
    1  unexpected build failure (engine error)
    2  NOT SUCCESS (the report's verdict) — also bad usage / a missing spec,
       matching argparse's own convention
    3  the build was refused before starting: lint, preflight, compile or
       addressing checks rejected the spec
    4  Packet Tracer unreachable on both channels
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from network_autopilot.core.build_errors import PreflightBlocked
from network_autopilot.core.network_spec import NetworkSpec
from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
from network_autopilot.evidence.store import EvidenceStore
from network_autopilot.orchestrator import Autopilot

DEFAULT_SPEC = "my-network.spec.json"
EVIDENCE_DIR = "autopilot_evidence"

EXIT_SUCCESS = 0
EXIT_BUILD_ERROR = 1
EXIT_NOT_SUCCESS = 2
#: Bad usage / missing spec shares 2 with NOT SUCCESS, as argparse itself does.
EXIT_USAGE = 2
EXIT_PREFLIGHT_BLOCKED = 3
EXIT_PT_UNREACHABLE = 4

_EXIT_EPILOG = """\
exit codes:
  0  SUCCESS (all verification tests passed) or PLAN OK (dry run completed)
  1  unexpected build failure (engine error)
  2  NOT SUCCESS (report verdict); also bad usage or a missing spec
  3  refused before start (lint, preflight, compile, addressing)
  4  Packet Tracer unreachable on either channel
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_havyn",
        description="Build a NetworkSpec in a live Packet Tracer and verify it.",
        epilog=_EXIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    parser.add_argument(
        "--clean-workspace", action="store_true",
        help="delete every existing workspace device first (the S0.4 cleanup; "
             "protected power-distribution devices are kept). Without it, a "
             "workspace that already holds a spec device name stops the build "
             "with exit code 3.",
    )
    args = parser.parse_args(argv)

    spec_path = Path(args.spec)
    if not spec_path.is_file():
        print(f"spec not found: {spec_path}", file=sys.stderr)
        return EXIT_USAGE

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
        return EXIT_PT_UNREACHABLE

    if args.clean_workspace:
        from scripts.pt_clean_workspace import clean

        report = clean(adapter)
        print(
            f"clean-workspace: {len(report.to_delete)} to delete, "
            f"{len(report.deleted)} deleted, {len(report.protected)} protected, "
            f"{len(report.failed)} failed"
        )
        if report.read_failed:
            print(
                "FAILED: workspace could not be read; nothing was deleted "
                "and the build will not start blind.",
                file=sys.stderr,
            )
            return EXIT_BUILD_ERROR
        if report.failed:
            print(
                "FAILED: could not delete "
                + ", ".join(sorted(report.failed))
                + "; refusing to build on a half-cleaned workspace.",
                file=sys.stderr,
            )
            return EXIT_BUILD_ERROR

    start = time.time()
    build, error = Autopilot(adapter, gate, evidence).build(spec)
    elapsed = time.time() - start

    if error:
        print("FAILED:", error, file=sys.stderr)
        # Preflight/lint refusal is the safety gate doing its job, not a
        # crash: it gets its own code so CI can tell them apart (S1.3).
        return EXIT_PREFLIGHT_BLOCKED if isinstance(error, PreflightBlocked) else EXIT_BUILD_ERROR

    print(f"\nTIME: {elapsed:.1f}s  ({elapsed / len(spec.devices):.2f}s/device)\n")
    print(build.human_text())
    # The verdict in the report becomes the process status. A dry run prints
    # PLAN OK - it completed exactly as asked - so it exits 0; anything else
    # that is not SUCCESS exits 2, never 0.
    if build.planned_only:
        return EXIT_SUCCESS
    return EXIT_SUCCESS if build.success else EXIT_NOT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
