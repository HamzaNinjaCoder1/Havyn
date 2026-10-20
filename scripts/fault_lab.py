"""Live fault-injection lab (S2.5): inject -> break -> diagnose -> revert.

    python scripts/fault_lab.py [--spec library/labs/fault-injection-lab.json]
        [--results-dir fault_lab_results] [--adapter bridge|null]
        [--only wrong-access-vlan]

Writing a fault label is not fault injection. For every one of the five
labelled faults this driver proves, in order:

1. the lab is known-good BEFORE anything is touched (the control probe must
   be reachable — a fault "proven" on an already-broken lab proves nothing);
2. the fault was really pushed (the inject receipt is recorded, executed or
   not, with the adapter's own words);
3. the intended behaviour actually broke (the same probe, now not reachable);
4. Havyn's diagnosis, run over the state observed *in the same pass* as the
   failing report, names the expected cause family;
5. the revert was really pushed;
6. the lab is known-good again afterwards (the probe reaches again);
7. no permanent contamination is left — the final probe of the whole run
   must still be reachable.

Every step is recorded, including the ones that fail: a fault that does not
break its flow is reported as ``not_broken``, never as a pass. Running with
``--adapter null`` is the honest-control case — a fake adapter cannot break
anything, so the harness must say so instead of inventing a break.

Exit codes: 0 every fault verified · 2 a fault failed its own criteria ·
1 the lab could not be built · 4 Packet Tracer unreachable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FAULTS_FAILED = 2
EXIT_UNREACHABLE = 4

DEFAULT_LAB = _REPO_ROOT / "library" / "labs" / "fault-injection-lab.json"

#: The lab's own control flow. Every fault below is required to break this
#: one criterion, so the before/after comparison is the same measurement
#: three times per fault.
PROBE_SOURCE = "pc1"
PROBE_DESTINATION = "srv1"

#: A probe outcome that means "traffic gets through".
_REACHABLE = ("reachable",)
#: Outcomes that mean "the fault really moved traffic off the path".
_BROKEN = ("blocked", "partial")


@dataclass(frozen=True)
class FaultAction:
    """One labelled fault bound to a concrete lab: inject and take it back.

    ``apply``/``revert`` return the adapter's own words about what happened
    (the receipt's error, or the command list that was pushed). They never
    raise for a device-level refusal — the caller records the outcome and
    judges it, which is what keeps a refused injection from reading as an
    injected one.
    """

    label: str
    description: str
    expected_cause: str
    apply: Callable[[Any], str]
    revert: Callable[[Any], str]
    #: True when the diagnosis engine is expected to name a cause at all.
    #: A wrong gateway is deliberately indistinguishable from other L3
    #: faults, so the honest expectation there is the unknown.
    expect_named_cause: bool = True


def _push_ios(adapter: Any, device: str, commands: list[str], op_id: str) -> str:
    """Push CLI lines, returning the adapter's verdict as text."""
    receipt = adapter.configure_ios(device, commands, operation_id=op_id)
    result = getattr(receipt.result, "value", str(receipt.result))
    if result != "success":
        return f"{result}: {receipt.error}"
    return "success"


def build_faults() -> tuple[FaultAction, ...]:
    """The five blueprint faults, bound to the fault-injection lab.

    Every target below is read off the lab's *rendered* configuration (the
    compiler's own output), not guessed: r1's WAN port, r1's static route to
    the server subnet, s1's access port toward pc1, s1's trunk port, and
    pc1's default gateway.
    """
    return (
        FaultAction(
            label="port-shutdown",
            description="r1 GigabitEthernet0/1 administratively shut",
            expected_cause="interface(s) down on the path",
            apply=lambda a: _push_ios(
                a, "r1", ["interface GigabitEthernet0/1", "shutdown", "exit"],
                "fault-port-shutdown"),
            revert=lambda a: _push_ios(
                a, "r1", ["interface GigabitEthernet0/1", "no shutdown", "exit"],
                "fault-port-shutdown-revert"),
        ),
        FaultAction(
            label="route-missing",
            description="r1's static route to 10.0.30.0/24 removed",
            expected_cause="is missing on",
            apply=lambda a: _push_ios(
                a, "r1",
                ["no ip route 10.0.30.0 255.255.255.0 10.0.0.2"],
                "fault-route-missing"),
            revert=lambda a: _push_ios(
                a, "r1",
                ["ip route 10.0.30.0 255.255.255.0 10.0.0.2"],
                "fault-route-missing-revert"),
        ),
        FaultAction(
            label="wrong-access-vlan",
            description="s1 FastEthernet0/1 (the access port toward pc1) moved to VLAN 999",
            expected_cause="does not report the expected VLAN or trunk state",
            apply=lambda a: _push_ios(
                a, "s1",
                ["interface FastEthernet0/1", "switchport access vlan 999", "exit"],
                "fault-wrong-access-vlan"),
            revert=lambda a: _push_ios(
                a, "s1",
                ["interface FastEthernet0/1", "switchport access vlan 10", "exit"],
                "fault-wrong-access-vlan-revert"),
        ),
        FaultAction(
            label="trunk-vlan-not-allowed",
            description="s1's trunk no longer carries VLAN 10",
            expected_cause="does not report the expected VLAN or trunk state",
            apply=lambda a: _push_ios(
                a, "s1",
                ["interface FastEthernet0/24",
                 "switchport trunk allowed vlan remove 10", "exit"],
                "fault-trunk-vlan-not-allowed"),
            revert=lambda a: _push_ios(
                a, "s1",
                ["interface FastEthernet0/24",
                 "switchport trunk allowed vlan 10", "exit"],
                "fault-trunk-vlan-not-allowed-revert"),
        ),
        FaultAction(
            label="wrong-gateway",
            description="pc1's default gateway pointed at 10.0.10.254",
            expected_cause="connectivity failed but no state anomaly was observed",
            expect_named_cause=False,
            apply=lambda a: _host_gateway(a, "10.0.10.254", "fault-wrong-gateway"),
            revert=lambda a: _host_gateway(a, "10.0.10.1", "fault-wrong-gateway-revert"),
        ),
    )


def _host_gateway(adapter: Any, gateway: str, op_id: str) -> str:
    """Repoint pc1's default gateway through the host configuration path."""
    receipt = adapter.configure_host(
        "pc1", dhcp=False, ip="10.0.10.10", mask="255.255.255.0",
        gateway=gateway, operation_id=op_id,
    )
    result = getattr(receipt.result, "value", str(receipt.result))
    if result != "success":
        return f"{result}: {receipt.error}"
    return f"success (gateway {gateway})"


@dataclass
class FaultOutcome:
    """One fault's whole story, JSON-serialisable."""

    label: str
    description: str
    control_before: str = ""
    injected: str = ""
    observed_broken: str = ""
    caused: list[str] = field(default_factory=list)
    reverted: str = ""
    control_after: str = ""
    verified: bool = False
    failure: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "description": self.description,
            "control_before": self.control_before,
            "injected": self.injected,
            "observed_broken": self.observed_broken,
            "caused": list(self.caused),
            "reverted": self.reverted,
            "control_after": self.control_after,
            "verified": self.verified,
            "failure": self.failure,
        }


def _probe(adapter: Any, source: str, destination: str) -> str:
    """One ICMP probe's outcome, or the honest reason it produced none."""
    try:
        flow = adapter.run_flow_test(source, destination, transport="icmp")
    except Exception as exc:  # a dead channel is not an outcome
        return f"probe-error: {type(exc).__name__}: {exc}"
    return flow.outcome.value


def run_fault(
    adapter: Any,
    autopilot: Any,
    spec: Any,
    action: FaultAction,
) -> FaultOutcome:
    """The whole inject -> break -> diagnose -> revert -> restore loop."""
    outcome = FaultOutcome(label=action.label, description=action.description)

    outcome.control_before = _probe(adapter, PROBE_SOURCE, PROBE_DESTINATION)
    if outcome.control_before not in _REACHABLE:
        outcome.failure = (
            "lab is not known-good before the fault: control probe was "
            f"{outcome.control_before!r}"
        )
        return outcome

    outcome.injected = action.apply(adapter)
    if not outcome.injected.startswith("success"):
        # The adapter refused or failed the injection. That is a real result
        # and it stops the loop here: judging the revert of a fault that was
        # never applied would report a restoration nobody performed.
        outcome.failure = f"injection did not take effect: {outcome.injected}"
        return outcome

    outcome.observed_broken = _probe(adapter, PROBE_SOURCE, PROBE_DESTINATION)
    if outcome.observed_broken not in _BROKEN:
        outcome.failure = (
            "the fault did not break the intended behaviour: probe still "
            f"reports {outcome.observed_broken!r}"
        )
        # Still revert: leaving a fault in place would contaminate the lab
        # for every later fault in this run.
        outcome.reverted = action.revert(adapter)
        outcome.control_after = _probe(adapter, PROBE_SOURCE, PROBE_DESTINATION)
        return outcome

    # Diagnosis runs over the state observed in the SAME pass as the failing
    # report, so the cause it names explains the verdict we just recorded.
    try:
        inspected = autopilot.inspect_diagnosable(spec)
    except Exception as exc:
        outcome.failure = f"diagnosis could not run: {type(exc).__name__}: {exc}"
        outcome.reverted = action.revert(adapter)
        outcome.control_after = _probe(adapter, PROBE_SOURCE, PROBE_DESTINATION)
        return outcome
    _, _, _, diagnoses, error = inspected
    if error is not None:
        outcome.failure = f"inspection failed: {error}"
    else:
        outcome.caused = [str(d.root_cause) for d in diagnoses]
        if action.expect_named_cause:
            named = any(action.expected_cause in cause for cause in outcome.caused)
            if not named:
                outcome.failure = (
                    f"diagnosis did not name {action.expected_cause!r}: "
                    f"got {outcome.caused or 'no diagnosis'}"
                )

    outcome.reverted = action.revert(adapter)
    outcome.control_after = _probe(adapter, PROBE_SOURCE, PROBE_DESTINATION)
    if outcome.control_after not in _REACHABLE and outcome.failure is None:
        outcome.failure = (
            f"the lab did not come back after the revert: {outcome.control_after!r}"
        )
    outcome.verified = outcome.failure is None
    return outcome


def _make_adapter(kind: str) -> Any:
    if kind == "null":
        from src.network_autopilot.adapter import NullAdapter

        return NullAdapter()
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import (
        BridgeAdapter,
    )
    from src.network_autopilot.pt.mailbox import FileMailbox

    return BridgeAdapter(host_bridge=False, file_bridge=FileMailbox())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fault-lab", description="Inject, break, diagnose, revert, restore.")
    parser.add_argument("--spec", default=str(DEFAULT_LAB))
    parser.add_argument("--results-dir", default="fault_lab_results")
    parser.add_argument("--adapter", default="bridge", choices=("bridge", "null"))
    parser.add_argument("--only", default=None,
                        help="run a single fault label")
    args = parser.parse_args(argv)

    from network_autopilot.core.network_spec import NetworkSpec
    from network_autopilot.core.safety import MutationBudget, SafetyGate, SafetyMode
    from network_autopilot.evidence.store import EvidenceStore
    from network_autopilot.orchestrator import Autopilot

    spec = NetworkSpec.from_json(Path(args.spec).read_text(encoding="utf-8"))
    adapter = _make_adapter(args.adapter)
    if args.adapter == "bridge" and not adapter.ping(timeout=5.0).reachable:
        print("Packet Tracer is not reachable.", file=sys.stderr)
        return EXIT_UNREACHABLE

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    evidence = EvidenceStore(root=results_dir / "evidence")
    autopilot = Autopilot(
        adapter,
        SafetyGate(SafetyMode.AUTONOMOUS, MutationBudget()),
        evidence,
    )

    started = time.monotonic()
    report, error = autopilot.build(spec)
    if error is not None or report is None:
        print(json.dumps({"lab": "not built", "error": str(error)}, indent=1))
        return EXIT_ERROR
    if not report.success:
        # A lab that is not known-good cannot host a fault experiment. This
        # is the honest stop: no fault is injected into a broken lab.
        print(json.dumps(
            {"lab": "not known-good", "build_id": report.build_id,
             "summary": report.verification.summary}, indent=1))
        return EXIT_ERROR

    faults = build_faults()
    if args.only:
        faults = tuple(f for f in faults if f.label == args.only)
        if not faults:
            print(f"no such fault: {args.only}", file=sys.stderr)
            return EXIT_ERROR

    outcomes = [run_fault(adapter, autopilot, spec, action) for action in faults]

    # Contamination check: the lab must still be known-good at the very end,
    # after every inject/revert cycle. Otherwise a fault was left behind.
    final_control = _probe(adapter, PROBE_SOURCE, PROBE_DESTINATION)
    payload = {
        "spec": str(args.spec),
        "build_id": report.build_id,
        "adapter": args.adapter,
        "elapsed_s": round(time.monotonic() - started, 1),
        "final_control": final_control,
        "contaminated": final_control not in _REACHABLE,
        "faults": [o.to_dict() for o in outcomes],
    }
    (results_dir / "fault_lab.json").write_text(
        json.dumps(payload, indent=1), encoding="utf-8"
    )
    print(json.dumps(payload, indent=1))

    if payload["contaminated"]:
        return EXIT_FAULTS_FAILED
    return EXIT_OK if all(o.verified for o in outcomes) else EXIT_FAULTS_FAILED


if __name__ == "__main__":
    raise SystemExit(main())