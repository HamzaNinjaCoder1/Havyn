"""Fault-injection harness (S2.5): controlled faults with labels.

Each of the first five faults knows three things: how to break observed
state offline (``inject``), how to put it back (``revert``), and how to
break the live lab (``live_commands`` pushed through the adapter). The
offline fault is only real if the flow actually reads as broken
(``flow_broken``) and the diagnosis engine names the right cause family
(``expected_cause``) — a fault nobody detects is a fault that proves
nothing.

Live mode (``live_commands`` + ``live_check``) needs Packet Tracer and is
exercised by the nightly runner, not by the offline suite: the offline
tests pin the shapes, the revert discipline, and the diagnosis mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.actual_state import ActualNetworkState
from ..core.observability import Observable

#: The first five faults the blueprint names. Labels are stable: the
#: scorecard (S2.4) and future training data join on them.
WRONG_ACCESS_VLAN = "wrong-access-vlan"
TRUNK_VLAN_NOT_ALLOWED = "trunk-vlan-not-allowed"
WRONG_GATEWAY = "wrong-gateway"
ROUTE_MISSING = "route-missing"
PORT_SHUTDOWN = "port-shutdown"

FIRST_FIVE = (
    WRONG_ACCESS_VLAN,
    TRUNK_VLAN_NOT_ALLOWED,
    WRONG_GATEWAY,
    ROUTE_MISSING,
    PORT_SHUTDOWN,
)


@dataclass
class FaultSpec:
    """One controlled fault, both modes."""

    label: str
    description: str
    #: Offline: mutate observed state, return an opaque revert token.
    inject: Callable[[ActualNetworkState], Any]
    #: Offline: restore the pre-inject state from the token.
    revert: Callable[[ActualNetworkState, Any], None]
    #: Offline: does the mutated state read as a broken flow?
    flow_broken: Callable[[ActualNetworkState], bool]
    #: Live: CLI lines that produce the same fault on the real device.
    live_commands: list[str] = field(default_factory=list)
    #: Substring the diagnosis engine must name for this fault. For faults
    #: the engine has no rule for, this is the honest-unknown cause.
    expected_cause: str = ""
    #: Whether the expected diagnosis carries a repair.
    expect_repairable: bool = True


def _known(value: Any) -> Observable:
    return Observable.known(value, source="fault-injection")


def port_shutdown_fault(device: str, port: str) -> FaultSpec:
    """Administratively shut a path interface."""

    def _inject(state: ActualNetworkState) -> Any:
        dev = state.device(device)
        if dev is None:
            raise KeyError(f"no such observed device: {device!r}")
        prior = dev.port(port).state
        dev.port(port).state = _known("down")
        return prior

    def _revert(state: ActualNetworkState, token: Any) -> None:
        state.device(device).port(port).state = token

    def _broken(state: ActualNetworkState) -> bool:
        dev = state.device(device)
        if dev is None:
            return False
        obs = dev.port(port).state
        return bool(obs.is_known and obs.value == "down")

    return FaultSpec(
        label=PORT_SHUTDOWN,
        description=f"{device}:{port} administratively shut",
        inject=_inject,
        revert=_revert,
        flow_broken=_broken,
        live_commands=[f"interface {port}", "shutdown", "exit"],
        expected_cause="interface(s) down on the path",
        expect_repairable=True,
    )


def route_missing_fault(device: str, pruned_table: list[dict] | None = None) -> FaultSpec:
    """Empty the router's observed route table: the spec route is gone.

    ``pruned_table`` lets a caller remove one entry instead of all; the
    default (empty table) is the strongest form of the fault.
    """

    def _inject(state: ActualNetworkState) -> Any:
        dev = state.device(device)
        if dev is None:
            raise KeyError(f"no such observed device: {device!r}")
        prior = dev.routes
        dev.routes = _known(list(pruned_table or []))
        return prior

    def _revert(state: ActualNetworkState, token: Any) -> None:
        state.device(device).routes = token

    def _broken(state: ActualNetworkState) -> bool:
        dev = state.device(device)
        if dev is None or not dev.routes.is_known:
            return False
        return not (dev.routes.value or [])

    return FaultSpec(
        label=ROUTE_MISSING,
        description=f"route table emptied on {device}",
        inject=_inject,
        revert=_revert,
        flow_broken=_broken,
        live_commands=[f"no ip route 0.0.0.0 0.0.0.0"],
        expected_cause="is missing on",
        expect_repairable=True,
    )


def wrong_access_vlan_fault(device: str, port: str, wrong_vlan: str = "999") -> FaultSpec:
    """Put the access port on a VLAN the spec never defined there."""

    def _inject(state: ActualNetworkState) -> Any:
        dev = state.device(device)
        if dev is None:
            raise KeyError(f"no such observed device: {device!r}")
        prior = dev.port(port).vlan
        dev.port(port).vlan = _known(wrong_vlan)
        return prior

    def _revert(state: ActualNetworkState, token: Any) -> None:
        state.device(device).port(port).vlan = token

    def _broken(state: ActualNetworkState) -> bool:
        dev = state.device(device)
        if dev is None:
            return False
        obs = dev.port(port).vlan
        return bool(obs.is_known and obs.value == wrong_vlan)

    return FaultSpec(
        label=WRONG_ACCESS_VLAN,
        description=f"{device}:{port} moved to VLAN {wrong_vlan}",
        inject=_inject,
        revert=_revert,
        flow_broken=_broken,
        live_commands=[
            f"interface {port}",
            f"switchport access vlan {wrong_vlan}",
            "exit",
        ],
        expected_cause="does not report the expected VLAN or trunk state",
        expect_repairable=True,
    )


def trunk_vlan_not_allowed_fault(device: str, missing_vlan: int = 20) -> FaultSpec:
    """Drop a VLAN from the switch's observed table: the trunk cannot carry it."""

    def _inject(state: ActualNetworkState) -> Any:
        dev = state.device(device)
        if dev is None:
            raise KeyError(f"no such observed device: {device!r}")
        prior = dev.vlans
        current = list(prior.value) if prior.is_known and prior.value else []
        kept = [v for v in current
                if not (isinstance(v, dict) and v.get("id") == missing_vlan)]
        dev.vlans = _known(kept)
        return prior

    def _revert(state: ActualNetworkState, token: Any) -> None:
        state.device(device).vlans = token

    def _broken(state: ActualNetworkState) -> bool:
        dev = state.device(device)
        if dev is None or not dev.vlans.is_known:
            return False
        return not any(isinstance(v, dict) and v.get("id") == missing_vlan
                       for v in (dev.vlans.value or []))

    return FaultSpec(
        label=TRUNK_VLAN_NOT_ALLOWED,
        description=f"VLAN {missing_vlan} missing from {device}'s table",
        inject=_inject,
        revert=_revert,
        flow_broken=_broken,
        live_commands=[
            "interface GigabitEthernet0/1",
            f"switchport trunk allowed vlan remove {missing_vlan}",
            "exit",
        ],
        expected_cause="does not report the expected VLAN or trunk state",
        expect_repairable=True,
    )


def wrong_gateway_fault(device: str, port: str, wrong_gateway: str = "192.0.2.1") -> FaultSpec:
    """Point the host at a gateway that does not exist.

    The diagnosis engine has no gateway rule on purpose: a wrong gateway is
    indistinguishable in observed state from several other L3 failures, so
    the correct diagnosis is the honest unknown with repair blocked — never
    a confident wrong fix (blueprint: wrong fixes 0).
    """

    def _inject(state: ActualNetworkState) -> Any:
        dev = state.device(device)
        if dev is None:
            raise KeyError(f"no such observed device: {device!r}")
        prior = dev.port(port).gateway
        dev.port(port).gateway = _known(wrong_gateway)
        return prior

    def _revert(state: ActualNetworkState, token: Any) -> None:
        state.device(device).port(port).gateway = token

    def _broken(state: ActualNetworkState) -> bool:
        dev = state.device(device)
        if dev is None:
            return False
        obs = dev.port(port).gateway
        return bool(obs.is_known and obs.value == wrong_gateway)

    return FaultSpec(
        label=WRONG_GATEWAY,
        description=f"{device}:{port} gateway set to {wrong_gateway}",
        inject=_inject,
        revert=_revert,
        flow_broken=_broken,
        live_commands=[f"ip default-gateway {wrong_gateway}"],
        expected_cause="connectivity failed but no state anomaly was observed",
        expect_repairable=False,
    )


def live_check(adapter: Any, source: str, destination: str) -> dict[str, Any]:
    """Live proof the fault broke the flow: BLOCKED/PARTIAL after inject.

    Returns a small JSON-serialisable record for the nightly artifacts. Only
    used with Packet Tracer reachable; the offline suite never calls it.
    """
    flow = adapter.run_flow_test(source, destination, transport="icmp")
    return {
        "source": source,
        "destination": destination,
        "outcome": flow.outcome.value,
        "detail": str(flow.detail)[:200],
        "broken": flow.outcome.value in ("blocked", "partial"),
    }
