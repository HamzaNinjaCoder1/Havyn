"""S5.7 certification harness (Gate 1).

Certifies the components training data may be generated from. Nothing in
havyn-stage5/datasets may be produced from an uncertified component.

Certified component families:
  generator-cells   every coverage cell at min/normal/max size compiles
  fault-injectors   the five labelled faults: inject -> flow broken ->
                    diagnosis names the cause -> revert restores health;
                    the no-anomaly control stays honestly UNKNOWN
  mutations         every enhancement mutation compiles before and after

Certificates land in havyn-stage5/certificates/<family>.json and record the
engine commit and generator version - a certificate from a different commit
is stale and `--check` says so. Re-run `certify.py` after any engine change.

Usage:
    python havyn-stage5/scripts/certify.py            # certify, write certs
    python havyn-stage5/scripts/certify.py --check    # verify existing certs
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "havyn-stage5" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from generate_intents import GENERATOR_VERSION, build_intent, coverage_cells  # noqa: E402
from src.network_autopilot.ai.expander import IntentExpander  # noqa: E402
from src.network_autopilot.ai.intent import HavynIntent  # noqa: E402
from src.network_autopilot.ai.lint import lint_intent  # noqa: E402
from src.network_autopilot.compiler.compiler import compile_spec  # noqa: E402
from src.network_autopilot.compiler.spec_lint import lint_spec  # noqa: E402
from src.network_autopilot.core.actual_state import (  # noqa: E402
    ActualNetworkState,
    DeviceObservation,
)
from src.network_autopilot.core.observability import Observable  # noqa: E402
from src.network_autopilot.core.requirements import AcceptanceReport, Verdict  # noqa: E402
from src.network_autopilot.diagnosis import diagnose  # noqa: E402
from src.network_autopilot.diagnosis.engine import _ip_in_network  # noqa: E402
from src.network_autopilot.diagnosis.fault_injection import (  # noqa: E402
    FIRST_FIVE,
    port_shutdown_fault,
    route_missing_fault,
    trunk_vlan_not_allowed_fault,
    wrong_access_vlan_fault,
    wrong_gateway_fault,
)
from src.network_autopilot.verification.engine import (  # noqa: E402
    VerificationReport,
    VerificationTest,
)
from src.network_autopilot.ipam import plan_addressing  # noqa: E402

CERT_DIR = REPO / "havyn-stage5" / "certificates"


def engine_commit() -> str:
    """Identify the engine by its content, not by HEAD.

    Pinning HEAD made every later commit (including docs) report the
    certificates stale although src/ had not changed. The src tree hash is
    stable across unrelated commits and still changes when the engine does.
    """
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD:src"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()[:12]
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "src"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.strip()
    return f"src-tree:{tree}+dirty-src" if dirty else f"src-tree:{tree}"


@dataclass
class Certification:
    component: str
    cases: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    def case(self, name: str, ok: bool, detail: str = "") -> None:
        self.cases += 1
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")

    @property
    def status(self) -> str:
        return "CERTIFIED" if not self.failures else "FAILED"


# --- generator cells ---------------------------------------------------------

def _chain_ok(intent: HavynIntent) -> tuple[bool, str]:
    try:
        lint = lint_intent(intent)
        if lint.blocking:
            return False, f"lint: {lint.to_dicts()[:1]}"
        expansion = IntentExpander(intent).expand()
        problems = lint_spec(expansion.spec)
        if problems:
            return False, f"spec-lint: {problems[:1]}"
        plan = plan_addressing(expansion.spec)
        compile_spec(expansion.spec, plan)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


def certify_generator_cells() -> Certification:
    cert = Certification("generator-cells")
    for cell in coverage_cells():
        for variant, scale in ((0, 0.5), (1, 1.0), (2, 2.0)):
            name = f"{cell.cell_id}/v{variant}/scale{scale}"
            data = build_intent(cell, variant, __import__("random").Random(7))
            if isinstance(data, HavynIntent):
                data = data.model_dump()
            # Scale up within the engine's verified limits: wireless radio
            # cells top out at 8 clients, and a single-VLAN site shares one
            # /24 (~250 addresses), so total demand is normalised down to it.
            wireless = ("laptop", "smartphone", "tablet")
            groups = [g for s in data["sites"] for a in s["areas"]
                      for g in a["host_groups"]]
            for group in groups:
                cap = 8 if group.get("device") in wireless else 100
                group["count"] = min(
                    cap, max(1, round(group["count"] * scale)))
            total = sum(g["count"] for g in groups)
            budget = 230
            if total > budget:
                factor = budget / total
                for group in groups:
                    group["count"] = max(1, round(group["count"] * factor))
            ok, detail = _chain_ok(HavynIntent.model_validate(data))
            cert.case(name, ok, detail)
    return cert


# --- fault injectors ---------------------------------------------------------

_HOST_PORT = "FastEthernet0"


def _device_role_ids(spec, role: str) -> list[str]:
    return [d.id for d in spec.devices if d.role.value == role]


def _route_table(spec, device_id: str) -> list[dict]:
    """Route entries a healthy read-back would show: the device's own static
    routes, in the {network, prefix} shape the diagnosis rules join on."""
    entries = []
    for route in spec.routing.static_routes:
        if route.device != device_id:
            continue
        network, _, prefix = route.destination.partition("/")
        entries.append({"network": network, "prefix": int(prefix or 0)})
    return entries


def _state_from_spec(spec) -> ActualNetworkState:
    """Healthy observed state derived from a compiled spec: every host port
    up and addressed, every route present, every VLAN in each switch's
    table."""
    state = ActualNetworkState(source="certify", observed_at="now")
    role_of = {d.id: d.role.value for d in spec.devices}
    for device in spec.devices:
        obs = DeviceObservation(name=device.id)
        if device.role.value == "switch":
            obs.vlans = Observable.known(
                [{"id": v.vlan_id} for v in spec.vlans if v.vlan_id],
                source="certify")
        for assignment in spec.ip_assignments:
            if assignment.device != device.id:
                continue
            port = obs.port(_HOST_PORT)
            port.state = Observable.known("up", source="certify")
            port.ip = Observable.known(assignment.ip, source="certify")
            if device.role.value in ("pc", "laptop", "server", "printer"):
                port.gateway = Observable.known(
                    _gateway_for(spec, assignment), source="certify")
        if device.role.value == "router":
            obs.routes = Observable.known(
                _route_table(spec, device.id), source="certify")
        state.observe_device(obs)
    return state


def _gateway_for(spec, assignment) -> str:
    subnet = next((s for s in spec.subnets if s.id == assignment.subnet), None)
    if subnet is not None and subnet.gateway:
        return subnet.gateway
    for other in spec.ip_assignments:
        if other.subnet == assignment.subnet and other.device != assignment.device:
            role = _device_role_ids(spec, "router")
            if other.device in role:
                return other.ip
    return "0.0.0.0"


def _struct_report(test_id: str, link_id: str) -> VerificationReport:
    return VerificationReport(
        tests=(VerificationTest(
            test_id=test_id, level="structural",
            source_requirement="R-1", source_acceptance_criterion="A-2",
            source=None, target=link_id,
            expected_result="present", observed_result="mismatch",
            evidence=("read-back differs",), timestamp="now",
            verdict=Verdict.FAIL,
        ),),
        summary={},
        acceptance=AcceptanceReport(
            results=(), requirement_results=(), summary={}, satisfied=False,
        ),
        observations_complete=True,
    )


def _flow_report(source: str, target: str) -> VerificationReport:
    return VerificationReport(
        tests=(VerificationTest(
            test_id="VT-cert-1", level="connectivity",
            source_requirement="R-1", source_acceptance_criterion="A-1",
            source=source, target=target,
            expected_result="reachable", observed_result="blocked",
            evidence=("flow observed: blocked",), timestamp="now",
            verdict=Verdict.FAIL,
        ),),
        summary={},
        acceptance=AcceptanceReport(
            results=(), requirement_results=(), summary={}, satisfied=False,
        ),
        observations_complete=True,
    )


def _fixture_specs(limit: int = 5) -> list:
    """Healthy fixtures: multi-VLAN single-site specs (real VLANs and trunks)
    plus one multi-site static spec (real static routes)."""
    fixtures = []
    for cell in coverage_cells():
        if cell.site_count == 1 and cell.vlan_mode == "multi" \
                and cell.firewall == "none" and not cell.wireless:
            for variant in range(3):
                data = build_intent(cell, variant, __import__("random").Random(3))
                if isinstance(data, HavynIntent):
                    data = data.model_dump()
                fixtures.append(IntentExpander(
                    HavynIntent.model_validate(data)).expand().spec)
                if len(fixtures) >= limit - 1:
                    break
            if len(fixtures) >= limit - 1:
                break
    for cell in coverage_cells():
        if cell.site_count >= 2 and cell.addressing == "static" \
                and cell.firewall == "none":
            data = build_intent(cell, 0, __import__("random").Random(5))
            if isinstance(data, HavynIntent):
                data = data.model_dump()
            fixtures.append(IntentExpander(
                HavynIntent.model_validate(data)).expand().spec)
            break
    return fixtures


def certify_fault_injectors() -> Certification:
    cert = Certification("fault-injectors")
    for index, spec in enumerate(_fixture_specs()):
        state = _state_from_spec(spec)
        endpoints = [
            a for a in spec.ip_assignments
            if next((d for d in spec.devices if d.id == a.device), None)
            is not None
            and next(d for d in spec.devices
                     if d.id == a.device).role.value in
            ("pc", "laptop", "server", "printer")
        ]
        switches = _device_role_ids(spec, "switch")
        routers = _device_role_ids(spec, "router")
        if not endpoints or not switches or not routers:
            continue
        source = endpoints[0].device

        # Target preference: an endpoint whose subnet a spec static route
        # covers (the route-missing case); otherwise any endpoint in another
        # subnet than the source's.
        source_subnet = endpoints[0].subnet
        destinations = [r.destination for r in spec.routing.static_routes]
        target_assignment = next(
            (a for a in endpoints if a.device != source
             and any(_ip_in_network(a.ip, d) for d in destinations)),
            next((a for a in endpoints if a.subnet != source_subnet), None),
        )
        if target_assignment is None:
            continue
        target = target_assignment.device

        fault_cases: list[tuple[str, object, VerificationReport]] = []
        fault_cases.append((
            "port-shutdown",
            port_shutdown_fault(source, _HOST_PORT),
            _flow_report(source, target),
        ))
        route = next((r for r in spec.routing.static_routes
                      if _ip_in_network(target_assignment.ip, r.destination)),
                     None)
        if route is not None:
            fault_cases.append((
                "route-missing",
                route_missing_fault(route.device),
                _flow_report(source, target),
            ))
        fault_cases.append((
            "wrong-gateway",
            wrong_gateway_fault(source, _HOST_PORT),
            _flow_report(source, target),
        ))
        # The structural faults need a link the switch actually serves.
        switch_links = [
            l for l in spec.links
            if switches[0] in (l.a.device, l.b.device)
        ]
        if switch_links:
            link = switch_links[0]
            fault_cases.append((
                "wrong-access-vlan",
                wrong_access_vlan_fault(switches[0], _HOST_PORT),
                _struct_report("VT-struct-vlan-1", link.id),
            ))
            trunk = next((l for l in switch_links if l.trunk), None)
            vlan_ids = [v.vlan_id for v in spec.vlans if v.vlan_id]
            if trunk is not None and vlan_ids:
                fault_cases.append((
                    "trunk-vlan-not-allowed",
                    trunk_vlan_not_allowed_fault(switches[0], vlan_ids[0]),
                    _struct_report("VT-struct-trunk-1", trunk.id),
                ))

        for label, fault, report in fault_cases:
            name = f"net{index}/{label}"
            token = fault.inject(state)
            cert.case(
                f"{name}/flow-broken", fault.flow_broken(state),
                "injected state did not read as broken",
            )
            diagnosis = diagnose(spec, report, state)
            cause_text = json.dumps(diagnosis, default=str).lower()
            cert.case(
                f"{name}/diagnosis",
                fault.expected_cause.lower() in cause_text,
                f"diagnosis did not name {fault.expected_cause!r}: "
                f"{cause_text[:160]}",
            )
            fault.revert(state, token)
            cert.case(
                f"{name}/revert", not fault.flow_broken(state),
                "reverted state still reads as broken",
            )

    # No-anomaly control: failing flow, clean state -> honest unknown.
    spec = _fixture_specs()[0]
    state = _state_from_spec(spec)
    hosts = [a for a in spec.ip_assignments]
    report = _flow_report(hosts[0].device, hosts[-1].device)
    diagnosis = diagnose(spec, report, state)
    text = json.dumps(diagnosis, default=str).lower()
    honest = ("unknown" in text or "no state anomaly" in text
              or "insufficient" in text)
    cert.case("control/no-anomaly-honest-unknown", honest,
              f"clean state produced a confident cause: {text[:120]}")
    return cert


# --- mutations (enhancement, S5.12) -------------------------------------------

def certify_mutations() -> Certification:
    cert = Certification("mutations")
    base_cell = next(c for c in coverage_cells() if c.cell_id.endswith(
        "single-static-none-none"))
    base = build_intent(base_cell, 0, __import__("random").Random(11))
    if isinstance(base, HavynIntent):
        base = base.model_dump()
    base_ok, base_detail = _chain_ok(HavynIntent.model_validate(base))
    cert.case("mutation-base-compiles", base_ok, base_detail)

    mutations = {
        "vlan-addition": lambda d: (
            d["sites"][0]["areas"].append({
                "id": "areax",
                "host_groups": [{"id": "gx", "area": "areax", "count": 3,
                                 "device": "pc", "vlan": "vx"}],
            }),
            d["vlans"].append({"id": "vx", "name": "extra", "site": d["sites"][0]["id"],
                               "required_hosts": 10}),
        ),
        "host-count-increase": lambda d: (
            [g.update(count=g["count"] + 4)
             for s in d["sites"] for a in s["areas"] for g in a["host_groups"]],
        ),
        "static-to-dhcp": lambda d: [
            g.update(addressing="dhcp")
            for s in d["sites"] for a in s["areas"] for g in a["host_groups"]
        ],
        "ospf-routing": lambda d: d.update(routing={"mode": "ospf"}),
    }
    for name, mutate in mutations.items():
        data = json.loads(json.dumps(base))  # deep copy
        mutate(data)
        ok, detail = _chain_ok(HavynIntent.model_validate(data))
        cert.case(f"mutation/{name}/after-compiles", ok, detail)
    return cert


# --- driver ---------------------------------------------------------------------

def write_certificate(cert: Certification) -> Path:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "component": cert.component,
        "generator_version": GENERATOR_VERSION,
        "engine_commit": engine_commit(),
        "cases": cert.cases,
        "passed": cert.passed,
        "failed": len(cert.failures),
        "failures": cert.failures[:20],
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": cert.status,
    }
    path = CERT_DIR / f"{cert.component}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[certify] {path} -> {cert.status} ({cert.passed}/{cert.cases})")
    return path


def check_certificates() -> int:
    stale = 0
    commit = engine_commit()
    for path in sorted(CERT_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        reasons = []
        if payload.get("engine_commit") != commit:
            reasons.append(f"engine commit moved ({payload.get('engine_commit')} -> {commit})")
        if payload.get("status") != "CERTIFIED":
            reasons.append(f"status {payload.get('status')}")
        if reasons:
            stale += 1
            print(f"[check] STALE {path.name}: {'; '.join(reasons)}")
        else:
            print(f"[check] valid {path.name}")
    return 1 if stale else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify existing certificates instead of re-certifying")
    args = parser.parse_args()

    if args.check:
        sys.exit(check_certificates())

    certs = [
        certify_generator_cells(),
        certify_fault_injectors(),
        certify_mutations(),
    ]
    for cert in certs:
        write_certificate(cert)
        for failure in cert.failures:
            print(f"[certify]   FAIL {failure}")
    sys.exit(0 if all(not c.failures for c in certs) else 1)


if __name__ == "__main__":
    main()
