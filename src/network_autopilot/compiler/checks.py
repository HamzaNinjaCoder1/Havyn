"""Executable preconditions and postconditions (P2-2).

A `Check` is not documentation — it is evaluated against real observed state:

- preconditions run BEFORE the mutation and are fail-closed: a check that
  cannot be answered TRUE blocks the operation (BLOCKED, never a speculative
  mutation);
- postconditions run AFTER the mutation and its read-back, and their outcome
  is what turns an executed receipt into a CONFIRMED one.

Everything is answered from the adapter (the only door to Packet Tracer) or
from the executor's observed state. There is no path here that infers a
verdict from the absence of information: UNKNOWN stays UNKNOWN.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..adapter.base import PacketTracerAdapter
from ..core.actual_state import ActualNetworkState
from .operations import Check

#: Pause between precondition re-reads, seconds (transient-heartbeat settle).
_PRECONDITION_SETTLE_S = 1.0
#: How many extra reads an UNKNOWN existence answer is worth. Sized against
#: mailbox.HEARTBEAT_FRESH_S (6 s): a paused Script Engine heartbeat older
#: than that reads as "no live channel" until the next tick, so the budget
#: has to out-wait one full pause — the scale build blocked three switch
#: configs on a stall that a 3-read window could not cover. Bounded either
#: way: after the budget the verdict is still UNKNOWN, honestly.
_PRECONDITION_UNKNOWN_RETRIES = 8


@dataclass(frozen=True)
class CheckOutcome:
    """The result of evaluating one Check.

    `passed` is tri-state: True/False when the evidence decided it, None when
    it could not be determined (UNKNOWN). Only True confirms.
    """

    check: Check
    passed: bool | None
    observed: str | None
    evidence: str

    @property
    def verdict(self) -> str:
        return "pass" if self.passed else ("fail" if self.passed is False else "unknown")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.check.kind,
            "target": self.check.target,
            "expected": self.check.expected,
            "observed": self.observed,
            "verdict": self.verdict,
            "evidence": self.evidence,
        }


class CheckEvaluator:
    """Answers Checks from real state, fail-closed.

    A transient unknown (the mailbox heartbeat can be momentarily stale while
    PT is busy — build-68648f705ccb blocked two router configs for exactly
    this) gets a bounded re-read before it is treated as unanswerable. The
    retries wait out a hiccup; they never invent knowledge: after the budget
    the verdict is still UNKNOWN, honestly.
    """

    def __init__(
        self,
        adapter: PacketTracerAdapter,
        state: ActualNetworkState,
        link_endpoints: dict[str, tuple[str, str, str, str]] | None = None,
    ):
        self.adapter = adapter
        self.state = state
        self.link_endpoints = link_endpoints or {}

    # -- public API ---------------------------------------------------------

    def evaluate(self, check: Check) -> CheckOutcome:
        method = getattr(self, f"_check_{check.kind}", None)
        if method is None:
            return CheckOutcome(
                check, None, None,
                f"check kind {check.kind!r} has no evaluator — treated as UNKNOWN",
            )
        return method(check)

    def all_hold(self, checks: tuple[Check, ...]) -> tuple[bool, list[CheckOutcome]]:
        """Fail-closed AND over checks. Unknown blocks as well as False."""
        outcomes = [self.evaluate(c) for c in checks]
        decided_fail = [o for o in outcomes if o.passed is False]
        unknown = [o for o in outcomes if o.passed is None]
        if not outcomes:
            return True, []
        # Nothing may proceed on a FALSE or an UNANSWERABLE precondition.
        return not decided_fail and not unknown, outcomes

    # -- individual checks --------------------------------------------------

    def _check_wireless_interface_visible(self, check: Check) -> CheckOutcome:
        """The client really has a radio (the association cannot exist without).

        Read live from the adapter, not from the executor's state snapshot:
        the radio is installed moments before this runs, so a snapshot would
        still show the old wired NIC.
        """
        obs = self.adapter.inspect_device(check.target)
        visible = check.expected in obs.ports
        return CheckOutcome(
            check, visible, check.expected if visible else None,
            f"port list of {check.target} after the radio install"
            + ("" if visible else f" (known ports: {sorted(obs.ports) or 'none'})"),
        )

    def _check_wireless_associated(self, check: Check) -> CheckOutcome:
        """PT shows an antenna link from the client to that access point.

        `check.target` is `client->access_point`; the proof is PT's own
        antenna link on the client's radio naming that AP as the receiver.
        Read live, because the association is established by PT a moment
        after the devices exist.
        """
        client, _, ap = check.target.partition("->")
        if not client or not ap:
            return CheckOutcome(
                check, None, None,
                f"wireless_associated target must be 'client->access_point', got {check.target!r}",
            )
        lister = getattr(self.adapter, "list_wireless_associations", None)
        if lister is None:
            return CheckOutcome(
                check, None, None,
                "adapter cannot read wireless associations",
            )
        entries = [str(e) for e in lister()]
        client_entries = [e for e in entries if e.startswith(f"{client}:")]
        if not client_entries:
            return CheckOutcome(
                check, False, None,
                f"PT shows no antenna link for {client} (associations: {entries or 'none'})",
            )
        association = self.adapter.wireless_association(client)
        observed_peer = association.access_point if association else None
        if observed_peer == ap:
            return CheckOutcome(
                check, True, f"{ap} via {association.client_interface}",
                f"PT antenna link on {client}:{association.client_interface} "
                f"names {ap} (link class {association.link_class})",
            )
        return CheckOutcome(
            check, False, None,
            f"{client} is associated to {observed_peer or 'nothing'}, not {ap} "
            f"(antenna entries: {client_entries})",
        )

    def _check_device_present(self, check: Check) -> CheckOutcome:
        exists = self.adapter.device_exists(check.target)
        if exists.is_known and not exists.value:
            # PT's logical workspace publishes a just-created device with a
            # delay: a create that already succeeded with a confirmed
            # read-back can still answer "no" for a moment. One settle and one
            # re-query separates that from a device that really is absent.
            time.sleep(_PRECONDITION_SETTLE_S)
            retry = self.adapter.device_exists(check.target)
            if retry.is_known and retry.value:
                return CheckOutcome(
                    check, True, "True",
                    f"device_exists({check.target}) read back after a settle",
                )
            exists = retry
        if not exists.is_known:
            # Transient channel unknown: re-read before declaring it
            # unanswerable (build-68648f705ccb: one stale heartbeat blocked
            # op-config-rhq/op-config-rbr forever — the devices were right
            # there). The budget bounds the wait; truth still decides.
            for _ in range(_PRECONDITION_UNKNOWN_RETRIES):
                time.sleep(_PRECONDITION_SETTLE_S)
                exists = self.adapter.device_exists(check.target)
                if exists.is_known:
                    break
        if not exists.is_known:
            return CheckOutcome(check, None, None, exists.detail or "existence unknown")
        return CheckOutcome(
            check, exists.value, str(exists.value),
            f"device_exists({check.target}) read back",
        )

    def _check_device_absent(self, check: Check) -> CheckOutcome:
        # `device_absent` gates a CREATE op. It is satisfied in BOTH cases
        # where existence is known: absent → the create runs, present → the
        # executor's idempotent-reuse path skips the create. Only an UNKNOWN
        # existence is unsafe (fail-closed). Blocking on "present" here would
        # make every rebuild of an existing topology BLOCKED instead of reused.
        exists = self.adapter.device_exists(check.target)
        if not exists.is_known:
            # Same transient-heartbeat settle as device_present: a create must
            # not be blocked by a heartbeat that is one read from recovering.
            for _ in range(_PRECONDITION_UNKNOWN_RETRIES):
                time.sleep(_PRECONDITION_SETTLE_S)
                exists = self.adapter.device_exists(check.target)
                if exists.is_known:
                    break
        if not exists.is_known:
            return CheckOutcome(check, None, None, exists.detail or "existence unknown")
        if exists.value:
            return CheckOutcome(
                check, True, "present (reuse)",
                f"{check.target} already exists — the executor reuses it",
            )
        return CheckOutcome(
            check, True, "absent",
            f"{check.target} is absent — the create proceeds",
        )

    def _check_device_model(self, check: Check) -> CheckOutcome:
        obs = self.adapter.inspect_device(check.target)
        if not obs.model.is_known:
            return CheckOutcome(
                check, None, None,
                obs.model.detail or "model not observable on this device",
            )
        match = obs.model.value == check.expected
        return CheckOutcome(
            check, match, obs.model.value,
            f"model read back from inspect_device({check.target})",
        )

    def _check_port_free(self, check: Check) -> CheckOutcome:
        # Like device_absent, this gates a CREATE (the link op). It is
        # satisfied when the port is free (the create proceeds) AND when the
        # port is already linked to the peer this op intends (the executor
        # reuses the link). Only an unreadable link state blocks.
        device, _, port = check.target.partition(":")
        intended = self._intended_peer(device, port)
        if intended is not None:
            other_dev, other_port = intended
            exists = self.adapter.link_exists(device, port, other_dev, other_port)
            if not exists.is_known:
                return CheckOutcome(
                    check, None, None, exists.detail or "link state unknown"
                )
            if exists.value:
                return CheckOutcome(
                    check, True, f"linked to intended peer {other_dev}:{other_port}",
                    "peer-aware reuse: the link this op would create already exists",
                )
            return CheckOutcome(
                check, True, "free of the intended peer",
                "port is free towards its intended peer — the create proceeds",
            )
        link = self._port_has_link(device, port)
        if link is None:
            return CheckOutcome(check, None, None, f"could not read link state of {check.target}")
        return CheckOutcome(
            check, not link, str(not link),
            f"link state of {check.target} read back",
        )

    def _intended_peer(self, device: str, port: str) -> tuple[str, str] | None:
        """The (device, port) this op's link is meant to connect to, if any."""
        for _link_id, (a_dev, a_port, b_dev, b_port) in self.link_endpoints.items():
            if (a_dev, a_port) == (device, port):
                return (b_dev, b_port)
            if (b_dev, b_port) == (device, port):
                return (a_dev, a_port)
        return None

    def _check_link_present(self, check: Check) -> CheckOutcome:
        endpoints = self.link_endpoints.get(check.target)
        if endpoints is None:
            return CheckOutcome(
                check, None, None,
                f"link {check.target} has no compiled endpoints to verify against",
            )
        a_dev, a_port, b_dev, b_port = endpoints
        exists = self.adapter.link_exists(a_dev, a_port, b_dev, b_port)
        if not exists.is_known:
            return CheckOutcome(check, None, None, exists.detail or exists.status.value)
        return CheckOutcome(
            check, exists.value,
            f"{a_dev}:{a_port} <-> {b_dev}:{b_port}",
            "peer-aware link_exists read-back",
        )

    def _check_module_port_visible(self, check: Check) -> CheckOutcome:
        # target is the device; expected is the port the module must expose.
        device, port = check.target, check.expected
        obs = self.adapter.inspect_device(device)
        visible = port in obs.ports
        return CheckOutcome(
            check, visible, port if visible else None,
            f"port list of {device} after install"
            + ("" if visible else f" (known ports: {sorted(obs.ports) or 'none'})"),
        )

    def _check_host_ip(self, check: Check) -> CheckOutcome:
        device = check.target
        expected_ip = check.expected
        obs = self.adapter.inspect_device(device)
        found = next(
            (p for p in obs.ports.values() if p.ip.is_known and p.ip.value == expected_ip),
            None,
        )
        if found is not None:
            return CheckOutcome(
                check, True, expected_ip,
                f"ip observed on {device}:{found.name}",
            )
        if not obs.ports:
            return CheckOutcome(check, None, None, f"{device} has no readable ports")
        return CheckOutcome(
            check, False, None,
            f"ip {expected_ip!r} not on any observed port of {device}",
        )

    def _check_ios_accepted(self, check: Check) -> CheckOutcome:
        # IOS acceptance is not directly observable from the outside; the
        # honest answer is UNKNOWN and the receipt must stay unconfirmed. The
        # semantic diff / verification layer judges the real effect.
        return CheckOutcome(
            check, None, None,
            "IOS acceptance is not observable from the device surface; "
            "judged by read-back verification instead",
        )

    # -- helpers ---------------------------------------------------------------

    def _port_has_link(self, device: str, port: str) -> bool | None:
        """True/False if the port's link state is known, else None."""
        if not device or not port:
            return None
        # Prefer the observed snapshot: it is the freshest aggregate read.
        dev = self.state.devices.get(device)
        if dev is not None and port in dev.ports:
            peer = dev.ports[port].peers
            if peer.is_known:
                return bool(peer.value)
        # Fall back to a targeted read against the other end of any link that
        # mentions this port — the peer-aware adapter call decides it.
        for _link_id, (a_dev, a_port, b_dev, b_port) in self.link_endpoints.items():
            if (a_dev, a_port) == (device, port) or (b_dev, b_port) == (device, port):
                other = (b_dev, b_port) if (a_dev, a_port) == (device, port) else (a_dev, a_port)
                exists = self.adapter.link_exists(device, port, other[0], other[1])
                if exists.is_known:
                    return bool(exists.value)
                return None
        return None
