"""Every offline-detectable spec problem in one pass (#35).

The complaint this fixes is real and expensive: each check used to fire on its
own, so a spec with a bad address, a policy aimed at the wrong box and a port
that does not exist cost three separate build attempts to find out. The checks
below are all pure - no Packet Tracer, no I/O - so nothing stops them from
running together and reporting every problem in the one error.

`compile_spec` still enforces each of these on its own; it is the last line of
defence for a caller that skips the lint, not the first to notice.
"""

from __future__ import annotations

from typing import Any

from ..core.network_spec import NetworkSpec
from . import firewall_policy

#: Roles that can serve a DHCP pool for a subnet they are attached to.
# Roles that hold an address AND can serve DHCP behind it. `wireless_router`
# counts because a Linksys at its factory 192.168.0.1/24 really does serve
# its pool (measured); a custom LAN IP is caught precisely by the compiler
# (#10), which raises a better message than this coarse check could.
_DHCP_SERVER_ROLES = ("router", "firewall", "wireless_router")


def _ip(text: str) -> Any:
    import ipaddress

    try:
        return ipaddress.ip_address(str(text).strip())
    except ValueError:
        return None


def transport_issues(spec: NetworkSpec) -> list[str]:
    """Flow criteria that no probe in Packet Tracer can decide (#25)."""
    issues: list[str] = []
    for criterion in spec.acceptance_criteria:
        test = criterion.test
        if getattr(test, "kind", "") != "flow":
            continue
        transport = str(getattr(test, "transport", "icmp")).lower()
        if transport in ("tcp", "udp"):
            issues.append(
                f"criterion {criterion.id!r} asks for a {transport.upper()} probe "
                f"(port {test.port}); Packet Tracer exposes no verified way to "
                "observe one, so it could never be decided. Keep the ICMP "
                "reachability you can check and record the port-level "
                "requirement as a design decision."
            )
    return issues


def policy_issues(spec: NetworkSpec) -> list[str]:
    """Policies aimed at a box that cannot enforce them."""
    issues: list[str] = []
    for policy in spec.policies:
        if policy.enforcement != "available":
            continue
        for target in policy.apply_to:
            model = None
            try:
                from .compiler import _resolve_policy_model

                device = spec.device_by_id(target.strip())
                if device is not None:
                    model = _resolve_policy_model(spec, device)
            except Exception as exc:  # a bad model hint is its own problem
                issues.append(
                    f"policy {policy.id!r} target {target!r}: {exc}"
                )
                continue
            if model is None or getattr(model, "category", None) not in ("firewall", "router"):
                issues.append(
                    f"policy {policy.id!r} targets {target!r}, which is not an "
                    "ASA firewall or an IOS router. This build verifies "
                    "access-list enforcement only on those two (ASA models "
                    "5505, 5506-X and IOS routers); on any other device the "
                    "policy would compile and then do nothing."
                )
    return issues


def addressing_issues(spec: NetworkSpec) -> list[str]:
    """Address-shaped problems: bad literals, addresses outside their subnet."""
    issues: list[str] = []
    subnets = {s.id: s for s in spec.subnets}
    for assignment in spec.ip_assignments:
        subnet = subnets.get(assignment.subnet)
        label = f"assignment {assignment.device!r}/{assignment.subnet!r}"
        if subnet is None:
            issues.append(f"{label}: no such subnet")
            continue
        if assignment.ip:
            address = _ip(assignment.ip)
            if address is None:
                issues.append(
                    f"{label}: {assignment.ip!r} is not a valid IPv4 address"
                )
                continue
            if subnet.cidr:
                import ipaddress

                try:
                    network = ipaddress.ip_network(subnet.cidr, strict=False)
                except ValueError as exc:
                    issues.append(f"subnet {subnet.id!r}: {exc}")
                    continue
                if address not in network:
                    issues.append(
                        f"{label}: {assignment.ip} is not inside "
                        f"{subnet.cidr} (subnets are stated as networks; the "
                        "usable range excludes the network and broadcast "
                        "addresses)"
                    )
        if subnet.gateway:
            gateway = _ip(subnet.gateway)
            if gateway is None:
                issues.append(
                    f"subnet {subnet.id!r}: gateway {subnet.gateway!r} is not "
                    "a valid IPv4 address"
                )
    for subnet in spec.subnets:
        if subnet.cidr:
            import ipaddress

            try:
                network = ipaddress.ip_network(subnet.cidr, strict=True)
            except ValueError as exc:
                issues.append(f"subnet {subnet.id!r}: {exc}")
    return issues


def vlan_issues(spec: NetworkSpec) -> list[str]:
    """VLAN references the spec never defines."""
    issues: list[str] = []
    defined = {v.vlan_id for v in spec.vlans}
    for subnet in spec.subnets:
        if subnet.vlan_id is not None and subnet.vlan_id not in defined:
            issues.append(
                f"subnet {subnet.id!r} is placed in VLAN {subnet.vlan_id}, "
                "which the spec never defines"
            )
    return issues


def dhcp_issues(spec: NetworkSpec) -> list[str]:
    """A DHCP subnet with nothing that can hand out a lease."""
    issues: list[str] = []
    subnets = {s.id: s for s in spec.subnets}
    # A Server-PT can serve the subnet too (S3.D4): it needs an address on it
    # and a `service: dhcp` intent. Only routers/firewalls were counted before,
    # which rejected a valid server-backed DHCP lab.
    service_servers = {
        service.device for service in spec.services
        if service.service == "dhcp" and service.enabled
    }
    for subnet in spec.subnets:
        if not subnet.dhcp:
            continue
        servers = set()
        for assignment in spec.ip_assignments:
            if assignment.subnet != subnet.id or assignment.ip is None:
                continue
            device = spec.device_by_id(assignment.device)
            role = str(getattr(getattr(device, "role", None), "value", ""))
            if role in _DHCP_SERVER_ROLES or assignment.device in service_servers:
                servers.add(assignment.device)
        if not servers:
            issues.append(
                f"subnet {subnet.id!r} is marked dhcp but no router, firewall "
                "or Server-PT with a dhcp service holds an address on it, so "
                "there is no pool to serve it: its clients would never get a "
                "lease"
            )
    return issues


def plan_issues(spec: NetworkSpec) -> list[str]:
    """Addressing-plan and port/media problems, computed offline.

    Both stages are pure, so they run here instead of aborting the build on
    their first complaint. A stage that raises is reported as one issue: a
    malformed address must not hide the five other problems in the file.
    """
    issues: list[str] = []
    try:
        from ..ipam.planner import plan_addressing
        from ..ipam.validator import has_errors, validate_plan

        plan = plan_addressing(spec)
    except Exception as exc:
        return [f"addressing plan could not be built: {exc}"]
    try:
        for issue in validate_plan(plan, spec):
            if has_errors([issue]):
                issues.append(f"addressing: {issue.resource}: {issue.problem}")
    except Exception as exc:
        issues.append(f"addressing validation failed: {exc}")
    try:
        from .port_solver import solve_ports

        port_plan = solve_ports(spec, plan)
    except Exception as exc:
        issues.append(f"ports could not be resolved: {exc}")
        return issues
    try:
        from .preflight import run_preflight

        preflight = run_preflight(spec, plan, port_plan)
        issues.extend(f"preflight: {line}" for line in preflight.errors)
    except Exception as exc:
        issues.append(f"preflight failed: {exc}")
    return issues


def lint_spec(spec: NetworkSpec) -> list[str]:
    """Every problem this spec can be shown without touching Packet Tracer."""
    issues: list[str] = []
    for check in (
        addressing_issues,
        vlan_issues,
        dhcp_issues,
        policy_issues,
        transport_issues,
        plan_issues,
    ):
        try:
            issues.extend(check(spec))
        except Exception as exc:  # a broken check must not hide the others
            issues.append(f"{check.__name__} could not run: {exc}")
    seen: set[str] = set()
    unique: list[str] = []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            unique.append(issue)
    return unique


def split_lint_problems(problems: list[str]) -> tuple[list[str], list[str]]:
    """Split lint problems into (hard, preflight_deferred).

    S1.14/F11: problems the embedded preflight run reported (prefixed
    "preflight: ") are NOT build-killers at lint time. They resurface with
    full context (workspace, reservations) in the compile-time preflight,
    where they scope to the implicated ops instead of zeroing the build.
    Everything else — bad addressing, policy violations, a solver that
    raised, a preflight run that itself crashed ("preflight failed: ...") —
    stays hard and refuses the spec as before.
    """
    hard: list[str] = []
    deferred: list[str] = []
    for problem in problems:
        if problem.startswith("preflight: "):
            deferred.append(problem)
        else:
            hard.append(problem)
    return hard, deferred


def format_issues(issues: list[str]) -> str:
    """The one error string a caller gets instead of one per round."""
    if not issues:
        return ""
    return (
        f"spec is not buildable ({len(issues)} problem(s)):\n"
        + "\n".join(f"  - {issue}" for issue in issues)
    )


# Re-exported so callers do not have to know where the policy check lives.
assert_policies_target_firewalls = firewall_policy.assert_policies_target_firewalls
