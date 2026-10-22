"""Project-owned PacketTracerAdapter over the local HTTP bridge.

The adapter is the only place in `network_autopilot` that touches the
low-level transport package. It reuses the local
transport — the PTCommandBridge protocol, the per-machine token security and
the reportResult result-correlation — everything above depends on the
`PacketTracerAdapter` abstraction instead of these details.

Ways to reach a bridge:
- attach mode (`host_bridge=False`): pure HTTP client to a bridge some other
  process already hosts (POST /queue, GET /result?rid=...).
- host mode (`host_bridge=True`, default): starts a PTCommandBridge on `port`
  so the Packet Tracer extension (which polls the default port) finds it.

Honesty rules implemented here:
- /ping identity must match our token fingerprint before anything is queued
  (foreign-bridge detection, inherited from the upstream design).
- A timeout / no-result answer is UNKNOWN, never success.
- A `PT_ERROR: ...` result is FAILED with the error preserved as evidence.
- Read-back postconditions are separate sends; only their passage marks a
  receipt confirmed.
- IOS acceptance is not observable through this channel, so `configure_ios`
  receipts stay unconfirmed by construction - except where the effect IS
  readable: interface addresses, and on switches the VLAN/trunk state the
  script pushed (#5, read back with `show vlan brief`).
- One channel per request, never both (upstream rule): HTTP when the
  authenticated bridge sees PT polling, otherwise the file mailbox when the
  Script Engine heartbeat is fresh — a late HTTP pickup plus a file retry
  would execute the same mutation twice.
- Topology read-back (model/ports/IPs), device enumeration and real console
  pings are implemented over the verified upstream JS patterns; whatever they
  cannot answer stays UNKNOWN, never guessed.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from ...core.actual_state import ActualNetworkState, DeviceObservation
from ...core.ids import new_id, utc_now_iso
from ...core.observability import Observable, ObservationStatus
from ...core.receipts import CheckResult, OperationReceipt, OperationResult
from ...core.requirements import FlowOutcome, FlowResult, best_flow_of
from ...evidence.store import EvidenceStore
from ...pt.catalog.devices import category_of_model
from ..base import AdapterError, AdapterInfo, PacketTracerAdapter
from . import js_commands as js
from .pt_launcher import LaunchResult, ensure_packet_tracer, find_packet_tracer

@dataclass(frozen=True)
class _ConsoleStep:
    """One measured PT console state, as `js_console_show_expr` reports it."""

    #: how many times this command's text appears in the buffer; a read is
    #: only trusted once the count rises past what it was when we typed it,
    #: which is what makes a repeated command unambiguous without flush()
    #: (PT rejects flush outright: `Invalid arguments for IPC call "flush"`).
    echo_count: int
    #: `--More--` is the LAST thing in the buffer, i.e. PT is paging right now.
    pager_at_end: bool
    #: the device is back at a CLI prompt (`...#` / `...>`).
    at_prompt: bool
    #: the device is not paging, not in a `[yes/no]` dialog and not parked at
    #: `Press RETURN to get started`, so a command is worth typing. Measured:
    #: `getPrompt()` is "" while the console is idle, so readiness cannot be
    #: taken from it.
    settled: bool
    #: this call queued something on the device.
    typed: bool
    prompt: str
    #: everything the buffer holds from this command's own echo onwards.
    tail: str


try:
    from ...pt.http_bridge import (
        DEFAULT_PORT,
        PTCommandBridge,
        next_rid,
        report_result_js,
    )
    from ...pt.token import get_bridge_token, token_fingerprint
    from ...pt.mailbox import FileMailbox
    # Re-exported: callers (and tests) reach for the mailbox transport through
    # this module, so the name they already use keeps working.
    from ...pt.mailbox import FileMailbox as FileBridge
    from ...pt.ping import classify_ping
    from ...pt.catalog import infer_cable, category_of_model
except ImportError as exc:  # pragma: no cover - repo checkout without install
    raise ImportError(
        "network_autopilot.adapter.packet_tracer needs its own transport "
        "package (network_autopilot.pt): run `pip install -e .` or pytest "
        "from the repo root (pythonpath includes src/)"
    ) from exc


class BridgeUnreachable(AdapterError):
    """The bridge port could not be reached at all."""


# A cross-subnet ping is retried because PT (like real IOS) drops the first
# echoes while ARP resolves hop by hop. Three attempts was the observed
# blocked -> partial -> reachable sequence on a freshly built transit path;
# the settle between attempts gives the convergence room to happen.
_FLOW_MAX_ATTEMPTS = 3
_FLOW_CONVERGE_SETTLE_S = 1.0
# Per-attempt quiet-window schedule (S1.6a, re-measured 2026-10-16): a WARM
# path (ARP already resolved) answers inside the first second, and PT prints a
# 0%-loss stat block on a dead path within ~8 s. But the FIRST ping on a freshly
# built cross-fabric path is COLD: measured single-ping latencies were 14.3 s
# (pc16->srv1), 16.1 s (pc9->srvisp) and 24.3 s (pc1->pc7) — the first echo pays
# ARP resolution and, on a lossy path, per-echo timeouts. A first window shorter
# than that (the old 8 s) never caught a cold ping, forced a re-arm that threw
# the ARP convergence away, and cost MORE wall-clock (F-7's 775 s of doomed
# windows) than one window long enough to finish the job. So attempt 1 covers
# the measured worst cold ping (24.3 s + margin); attempts 2/3 are ARP-warm and
# cheap. Callers that pass an explicit timeout= keep it for every attempt — the
# schedule only replaces the default.
_FLOW_ATTEMPT_TIMEOUTS: tuple[float, ...] = (30.0, 15.0, 25.0)
# A large build configures dozens of interfaces right up to the flow test; the
# control plane needs this brief settle before the first echo or it loses the
# first packets to ARP/CDP convergence rather than to a real fault.
_FLOW_PRE_SETTLE_S = 1.5

# Transient-read settles (S1.7 follow-up, build-68648f705ccb): the mailbox
# heartbeat can be stale for a second or two while PT is busy, and a single
# unknown read must not fail an op whose answer is one read away. The budgets
# bound the WAIT, never the truth: after them the answer is still unknown.
# Radio visibility: PT's swap can be mid-flight on the first read-back.
_RADIO_SETTLE_S = 0.5
_RADIO_SETTLE_RETRIES = 4
# Destination resolve: one heartbeat hiccup used to poison a whole flow.
# Sized against mailbox.HEARTBEAT_FRESH_S (6 s): the wait must out-run one
# paused-heartbeat window before the flow is called unknown.
_RESOLVE_RETRY_S = 1.0
_RESOLVE_RETRIES = 6

# Bulk host read-back: the batch write and the one snapshot that verifies it
# are separate sends, so the snapshot needs the same bounded settle as every
# other read-back — one unknown read used to fail all 20 static hosts with
# 'address not observed' although PT had reported ok:true for each.
_BULK_READ_SETTLE_S = 0.5
_BULK_READ_RETRIES = 3

# IOS configuration is re-sent until the addresses show up on a read-back. A
# router added moments earlier is still booting and swallows the CLI input
# without reporting an error, so "the JS was delivered" is not evidence that
# anything was configured. Three sends with a settle covers the boot window
# without turning one device into a long stall.
_IOS_CONFIG_ATTEMPTS = 3
_IOS_SETTLE_SECONDS = 2.5

# A heavy config push types `ip route ...` while the route's exit interface is
# still coming up, so IOS discards the line (the push answers `true` anyway).
# The retry re-pushes just the dropped routes in a small call - by then the
# interfaces are up - and settles before each `show ip route` read-back: a read
# typed a moment after the push catches the table mid-install and falsely
# reports the route missing (measured on redge: the default route read back
# fine but the other five were read before PT applied them). Two rounds make
# the install reliable; the gate is not weakened because a route is only
# reported present when `show ip route` actually shows it.
_IOS_ROUTE_RETRY_ROUNDS = 2

# Existence is checked before and after every mutation, and an UNKNOWN answer
# makes the caller fail closed. PT's script engine drops the occasional poll
# while it is busy, so a single hiccup would reject a safe create. Two extra
# reads turn that into a real answer without ever guessing: if the channel is
# genuinely gone, all attempts come back UNKNOWN and the build still stops.
_EXISTS_READ_ATTEMPTS = 3
_EXISTS_READ_SETTLE_S = 0.4

# A refused cable is retried with escalating waits. The budget is sized for a
# LOADED PT, not an idle one: measured live, a 63-device build that followed
# twenty minutes of other live tests lost roughly a third of its addLink calls
# to publish lag, and the same build on a settled PT lost none. Nine seconds of
# patience was not enough there; ~30s is. Only a refusal pays any of this.
_LINK_RETRY_ATTEMPTS = 5
_LINK_RETRY_BASE_S = 1.5

_IOS_INTERFACE_RE = re.compile(r"^\s*interface\s+(\S+)", re.IGNORECASE)
_IOS_ADDRESS_RE = re.compile(r"^\s*ip\s+address\s+(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
#: Matches an IOS static route: `ip route <dst> <mask> <next-hop> [<metric>]`.
#: The optional metric is tolerated but not surfaced: the read-back returns the
#: route without it, so the postcondition would be a spurious contradiction if
#: we kept it on the expectation.
_IOS_ROUTE_RE = re.compile(
    r"^\s*ip\s+route\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)",
    re.IGNORECASE,
)
#: Matches an ASA static route: `route <nameif> <dst> <mask> <next-hop>`.
#: The nameif carries no routing-table read-back signal, but skipping the line
#: altogether would let a wrong-nameif install slip through; we record the
#: nameif and only treat the (dst, mask, next-hop) tuple as the postcondition.
_ASA_ROUTE_RE = re.compile(
    r"^\s*route\s+(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)",
    re.IGNORECASE,
)


def _expected_ios_routes(commands: list[str]) -> list[dict]:
    """The static routes a config script is asking for, parsed from the commands.

    Mirrors ``_expected_ios_addresses``: the expectation travels with the
    script so every caller gets the read-back check for free. Returns an empty
    list when the script defines no static route - the ordinary switch/host
    case, which has no ``show ip route`` to look at.

    The IOS and ASA forms coexist (``ip route ...`` and ``route <nameif> ...``).
    Both surface as ``{network, prefix, next_hop}`` tuples so the read-back
    check is a single compare against ``read_routing_table``'s output. The
    nameif is dropped: PT's route read-back omits it too, so keeping it on the
    expectation would always look like a contradiction.
    """
    import ipaddress

    expected: list[dict] = []
    for command in commands:
        m = _IOS_ROUTE_RE.match(command)
        if m:
            dst, mask, nhop = m.group(1), m.group(2), m.group(3)
        else:
            m2 = _ASA_ROUTE_RE.match(command)
            if m2:
                dst, mask, nhop = m2.group(2), m2.group(3), m2.group(4)
            else:
                continue
        try:
            net = ipaddress.ip_network(f"{dst}/{mask}", strict=False)
        except ValueError:
            continue
        # `str(net)` carries the prefix (`0.0.0.0/0`); the read-back parser
        # gives just the address. Normalise to the address so a single compare
        # against `obs.value` lines up.
        expected.append({
            "network": str(net.network_address),
            "prefix": net.prefixlen,
            "next_hop": nhop,
        })
    return expected


def _mask_from_prefix(prefix: int) -> str:
    """Translate a CIDR prefix to the dotted-quad mask ``ip route`` requires.

    ``configureIosDevice`` accepts either form on the wire but the read-back
    parser, the receipt, and the spec all use ``255.255.255.0`` style masks -
    so for the retry (Step 4) we want to round-trip identically to the original
    push rather than introduce a second shape for the engine to keep straight.
    """
    import ipaddress
    return str(ipaddress.IPv4Network((0, prefix)).netmask)


def _expected_ios_addresses(commands: list[str]) -> dict[str, str]:
    """The interface -> address pairs a config script is asking IOS for.

    Parsed from the commands themselves so every caller gets the read-back
    check for free, whether it compiled the script or hand-wrote it.
    """
    expected: dict[str, str] = {}
    iface: str | None = None
    for command in commands:
        entered = _IOS_INTERFACE_RE.match(command)
        if entered:
            iface = entered.group(1)
            continue
        addressed = _IOS_ADDRESS_RE.match(command)
        if addressed and iface:
            expected[iface] = addressed.group(1)
    return expected


def _command_statuses(
    commands: list[str], expected: dict[str, str], missing: dict[str, str]
) -> dict[str, str]:
    """A per-command verdict, derived from what read-back could actually show.

    PT's ``configureIosDevice`` answers ``true`` for a script it aborted on at
    the first invalid line, and it does not echo into the console buffer
    (measured live: a bogus line after a good config never appears in
    ``getCommandLine().getOutput()``), so the error text cannot be read back.
    What CAN be read back is the effect: an address we asked for and cannot
    find means that line - and everything behind it - never applied. Commands
    with no observable effect say so instead of claiming ``unknown``.
    """
    statuses: dict[str, str] = {}
    iface: str | None = None
    for command in commands:
        entered = _IOS_INTERFACE_RE.match(command)
        if entered:
            iface = entered.group(1)
            continue
        addressed = _IOS_ADDRESS_RE.match(command)
        if addressed and iface:
            ip = addressed.group(1)
            if expected.get(iface) == ip and iface in missing:
                statuses[command] = f"not applied: {iface} never took {ip}"
            elif iface in expected:
                statuses[command] = f"applied: {iface} reads back {ip}"
            else:
                statuses[command] = "not applied"
            continue
        statuses[command] = "no read-back signal"
    return statuses


#: Categories whose ports carry VLAN membership. A router has no
#: `show vlan brief` table, so asking one costs a round trip and learns
#: nothing (#20). The same gate is what keeps a firewall's
#: `switchport access vlan` lines out of the configure_ios read-back (#5):
#: the compiler emits those lines for an ASA 5505 (`_build_firewall_commands`)
#: but a firewall has no VLAN table to read them back from.
_VLAN_CATEGORIES = ("switch",)

_IOS_VLAN_ID_RE = re.compile(r"^\s*vlan\s+(\d{1,4})\s*$", re.IGNORECASE)
_IOS_VLAN_NAME_RE = re.compile(r"^\s*name\s+(\S+)\s*$", re.IGNORECASE)
_IOS_ACCESS_VLAN_RE = re.compile(
    r"^\s*switchport\s+access\s+vlan\s+(\d{1,4})\b", re.IGNORECASE
)
_IOS_TRUNK_MODE_RE = re.compile(r"^\s*switchport\s+mode\s+trunk\b", re.IGNORECASE)
_IOS_TRUNK_ALLOWED_RE = re.compile(
    r"^\s*switchport\s+trunk\s+allowed\s+vlan\s+([0-9,\-\s]+)\s*$", re.IGNORECASE
)
_IOS_EXIT_RE = re.compile(r"^\s*(?:exit|end)\s*$", re.IGNORECASE)


def _parse_vlan_list(text: str) -> list[int]:
    """`10,20` -> [10, 20]; `10-12` -> [10, 11, 12]."""
    ids: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            low, _, high = token.partition("-")
            try:
                ids.extend(range(int(low), int(high) + 1))
            except ValueError:
                continue
        else:
            try:
                ids.append(int(token))
            except ValueError:
                continue
    return ids


def _expected_ios_vlans(commands: list[str]) -> dict[str, dict] | None:
    """The VLAN state an IOS script is asking for, parsed from the commands.

    Same contract as `_expected_ios_addresses`: the expectation travels with
    the script, so every caller gets the read-back check for free. Returns
    None when the script defines no VLAN, assigns no access VLAN and sets no
    trunk - the ordinary router case, which has no `show vlan brief` at all.

    Observable through the existing read APIs (`read_vlans` ->
    `parse_show_vlan_brief`) and therefore checked:

    * ``vlan <id>`` - the id must be in the read-back table; a ``name <...>``
      line inside that block must match the Name column (names containing a
      space are skipped: the parser only takes the first token, so a mismatch
      there would be a parsing artefact, not a config failure);
    * ``switchport access vlan <id>`` - the interface must be listed as a
      member of that VLAN;
    * ``switchport mode trunk`` - the interface must read back as a trunk.

    Deliberately NOT checked, because this repo has no read API that can see
    it (inventing a PT call is worse than an unchecked line):

    * DHCP - ``ip dhcp pool ...`` / ``network ...`` / ``default-router ...``
      are pushed for routers and switches, but the only console reads that
      exist here are `show vlan brief`, `show ip route` and
      `show controllers serial`, plus the topology snapshot. None of them
      reports DHCP pools, so a pool that silently did not apply stays
      invisible to this receipt.
    * ``switchport trunk native vlan ...`` and
      ``switchport trunk encapsulation dot1q ...`` - `show vlan brief` carries
      neither field; the per-port membership check below is the honest limit
      of what that table can prove.
    """
    defined: dict[int, str | None] = {}
    access: dict[str, int] = {}
    trunk: dict[str, list[int] | None] = {}
    iface: str | None = None
    vlan_id: int | None = None
    for command in commands:
        entered = _IOS_INTERFACE_RE.match(command)
        if entered:
            iface, vlan_id = entered.group(1), None
            continue
        defined_vlan = _IOS_VLAN_ID_RE.match(command)
        if defined_vlan:
            iface, vlan_id = None, int(defined_vlan.group(1))
            defined.setdefault(vlan_id, None)
            continue
        if _IOS_EXIT_RE.match(command):
            iface, vlan_id = None, None
            continue
        named = _IOS_VLAN_NAME_RE.match(command)
        if named and vlan_id is not None:
            defined[vlan_id] = named.group(1)
            continue
        access_vlan = _IOS_ACCESS_VLAN_RE.match(command)
        if access_vlan and iface:
            access[iface] = int(access_vlan.group(1))
            continue
        if _IOS_TRUNK_MODE_RE.match(command) and iface:
            trunk.setdefault(iface, None)
            continue
        allowed = _IOS_TRUNK_ALLOWED_RE.match(command)
        if allowed and iface and iface in trunk:
            trunk[iface] = _parse_vlan_list(allowed.group(1))
    if not (defined or access or trunk):
        return None
    return {"defined": defined, "access": access, "trunk": trunk}


def _flow_is_retryable(result: "FlowResult") -> bool:
    """Should a lost/slow flow attempt be retried?

    BLOCKED and PARTIAL are retryable: PT (like real IOS) drops the first
    echoes across a multi-hop path while ARP resolves hop by hop. A poll that
    saw no new statistics is also retryable -- under load a blocked ping can
    still be in flight past its own deadline, and the next attempt often
    catches it. Arm and console failures are terminal: retrying changes
    nothing.
    """
    if result.outcome in (FlowOutcome.BLOCKED, FlowOutcome.PARTIAL):
        return True
    return (
        result.outcome is FlowOutcome.UNKNOWN
        and "no new ping statistics" in (result.detail or "")
    )


class BridgeAdapter(PacketTracerAdapter):
    name = "packet-tracer-bridge"
    version = "0.1.0"

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        token: str | None = None,
        *,
        host_bridge: bool = True,
        evidence: EvidenceStore | None = None,
        max_result_wait: float = 45.0,
        file_bridge: FileBridge | None = None,
        auto_launch: bool = False,
        launch_timeout: float = 120.0,
        observe_vlans: bool = True,
    ):
        self.port = port
        # Default (None) uses the real per-machine token via the upstream
        # token module. Tests pass an explicit token to avoid touching it.
        self.token = token or get_bridge_token()
        self._fingerprint = token_fingerprint(self.token)
        self._host_bridge = host_bridge
        self._evidence = evidence
        self._max_wait = max_result_wait
        self._hosted: PTCommandBridge | None = None
        # Fallback transport for when the extension webview is closed: the
        # Script Engine keeps running and drains the file mailbox. Tests can
        # inject a temp-directory FileBridge.
        self._file_bridge = file_bridge or FileBridge()
        # On-demand launch: when PT is closed the live channel is unusable
        # until someone opens the GUI. auto_launch lets this adapter open it
        # itself (bounded by launch_timeout) instead of returning UNKNOWN.
        self._auto_launch = auto_launch
        self._launch_timeout = launch_timeout
        # #20: one `show vlan brief` per switch. It is what makes the L2
        # criteria decidable, and a caller whose spec asks for no VLAN can
        # turn it off to save the round trip.
        self._observe_vlans = observe_vlans
        self.last_launch: LaunchResult | None = None
        # Cached channel selection (see _pick_channel). Cleared on a failed
        # send so a dead channel is re-probed instead of trusted.
        self._channel: str | None = None
        self._channel_cached_at: float | None = None
        # Channel-outage ride-out (see _resolve_channel): True once this
        # adapter has actually seen a live channel, i.e. PT was open and
        # answering during THIS session. An adapter that has never seen one
        # keeps failing fast — live-gated tests and CLI runs against a
        # closed PT depend on that immediate answer.
        self._ever_live_channel: bool = False
        # The shared deadline of the current outage (None while healthy,
        # past when a window was spent and PT never came back).
        self._outage_deadline: float | None = None
        # Negative cache for the HTTP half of the channel probe. Probing it
        # costs a real round trip, and on a machine with no bridge listening
        # that is a refused loopback connect measured at 2.03 s (plus 0.36 s
        # of getaddrinfo). A build sends ~440 commands and the positive cache
        # only lasts 4 s, so re-discovering the same absence dominated the
        # wall clock of the 20-minute reference build. Remembering the absence
        # is behaviour-preserving: the answer with HTTP down is the file
        # channel or "", exactly as before — only the cost changes.
        # See _HTTP_ABSENT_TTL_SECONDS.
        self._http_absent_until: float = 0.0
        # Short-lived topology snapshot, shared by the burst of existence
        # checks a build makes. Invalidated whenever a mutation runs, so a
        # read after a create/link/config always sees the change.
        self._snapshot_cache: Observable[list[dict]] | None = None
        self._snapshot_at: float = 0.0
        self._snapshot_ttl: float = 2.0

    def _invalidate_snapshot(self) -> None:
        """Drop the cached topology after a mutation.

        Called after anything that changes PT's state, so the next existence
        check re-reads instead of trusting a pre-mutation snapshot.
        """
        self._snapshot_cache = None

    # -- HTTP plumbing --------------------------------------------------

    def _http(self, method: str, path_query: str, body: str | None = None, timeout: float = 10.0) -> tuple[int, str]:
        url = f"http://127.0.0.1:{self.port}{path_query}"
        req = urllib.request.Request(
            url, data=body.encode("utf-8") if body is not None else None, method=method
        )
        if body is not None:
            req.add_header("Content-Type", "text/plain")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except http.client.HTTPException as exc:
            # A listener on the bridge port that does not speak HTTP answers
            # with a bare status line; urlopen raises BadStatusLine (an
            # HTTPException, not an OSError) straight out of getresponse().
            # Without this the exception escaped _probe_channels and killed
            # the whole command pipeline instead of just ruling HTTP out.
            raise BridgeUnreachable(f"port held by a non-HTTP service: {exc}") from exc
        except urllib.error.URLError as exc:
            raise BridgeUnreachable(str(getattr(exc, "reason", exc))) from exc
        except OSError as exc:
            raise BridgeUnreachable(str(exc)) from exc

    def _ensure_hosted(self) -> None:
        if not self._host_bridge or self._hosted is not None:
            return
        try:
            bridge = PTCommandBridge(port=self.port, token=self.token)
            bridge.start()
            self._hosted = bridge
        except OSError:
            # Port taken: either a foreign service or another bridge. ping()
            # will report which, via the identity check.
            self._hosted = False  # type: ignore[assignment]

    def stop(self) -> None:
        """Shut down a bridge we hosted (tests, clean shutdown)."""
        if isinstance(self._hosted, PTCommandBridge):
            self._hosted.stop()
        self._hosted = None

    # -- on-demand launch ---------------------------------------------------

    def launch(self, timeout: float | None = None) -> LaunchResult:
        """Open Packet Tracer and wait for the Script Engine heartbeat.

        Idempotent and safe to call speculatively: when the heartbeat is
        already fresh it returns ALIVE without spawning anything. This is the
        hook an agent uses to remove 'PT must already be open' from the loop.

        Never starts a GUI when the bridge is reachable. Never touches a PT
        API — it spawns the documented executable and reads the existing
        heartbeat, so no bridge endpoint or token is involved.
        """
        result = ensure_packet_tracer(
            alive=self._file_bridge.pt_alive,
            timeout=self._launch_timeout if timeout is None else timeout,
        )
        self.last_launch = result
        if result.usable:
            # The channel cache predates the launch; drop it so the next send
            # re-probes and actually picks up the now-live file channel. The
            # remembered HTTP absence goes too: a fresh launch is the one
            # moment a bridge is plausibly newly reachable.
            self._channel = None
            self._channel_cached_at = None
            self._http_absent_until = 0.0
        return result

    def _resolve_channel(self) -> str:
        """Pick a channel, launching PT on demand when none is alive.

        send() calls this instead of _pick_channel() directly so that an
        adapter built with auto_launch=True can satisfy its own precondition
        instead of answering every command with UNKNOWN while PT is closed.

        A send on an adapter that HAS seen a live channel also rides out a
        channel outage (see _CHANNEL_OUTAGE_BUDGET_S): auto-launch answers
        "PT is closed", this window answers "PT was open moments ago and its
        Script Engine has gone quiet" — the measured stall class.
        """
        channel = self._pick_channel()
        if not channel and self._auto_launch:
            # No live channel and launching is permitted: open PT, then
            # re-probe exactly once. A launch that does not produce a
            # heartbeat falls through to the same UNKNOWN the caller would
            # have got anyway.
            self.launch()
            channel = self._pick_channel()
        if channel:
            self._ever_live_channel = True
            self._outage_deadline = None
            return channel
        if self._ever_live_channel:
            return self._await_channel_recovery()
        return ""

    def _await_channel_recovery(self) -> str:
        """'file' | 'http' | '' — one shared, bounded wait per outage.

        PT's Script Engine stops answering for stretches of variable length
        while the mailbox heartbeat only ticks between executions; inside
        such a stretch a single dark probe is not evidence that PT is gone
        (build-81df879ad555: 8 preconditions were BLOCKED and 22 host
        configs failed on one 215 s stall, and the repairs minutes later
        proved PT had been open all along). The FIRST waiter opens the
        window; every later waiter in the same outage shares that deadline,
        so a PT that never returns costs the budget once — after it is
        spent, sends fail fast with the honest "no live channel" UNKNOWN
        until a probe succeeds again (the gate always re-probes first, so a
        late recovery is still picked up).
        """
        now = time.time()
        if self._outage_deadline is None:
            self._outage_deadline = now + self._CHANNEL_OUTAGE_BUDGET_S
        while True:
            remaining = self._outage_deadline - time.time()
            if remaining <= 0:
                return ""
            time.sleep(min(self._CHANNEL_RECOVERY_POLL_S, remaining))
            # Bypass the channel cache: this is the moment to re-probe.
            channel = self._probe_channels()
            if channel:
                self._channel = channel
                self._channel_cached_at = time.time()
                self._outage_deadline = None
                return channel

    # -- command pipeline -----------------------------------------------

    def _pick_channel(self) -> str:
        """'http' | 'file' | '' — one channel per request, never both.

        Mirrors the upstream routing: HTTP only when the authenticated bridge
        actually sees PT polling (ping() checks identity + /status); otherwise
        the file mailbox when the Script Engine heartbeat is fresh.

        Cached briefly: the probe costs two HTTP round trips and a build sends
        one command per operation. The cache is invalidated by a failed send
        and by the TTL, so a channel that dies mid-run is re-probed.
        """
        now = time.time()
        cached_at = self._channel_cached_at
        if (
            self._channel is not None
            and cached_at is not None
            and now - cached_at < self._CHANNEL_CACHE_TTL_SECONDS
        ):
            return self._channel
        self._channel_cached_at = now
        self._channel = self._probe_channels()
        return self._channel

    def _probe_channels(self) -> str:
        # HTTP is only usable when the authenticated bridge actually answers
        # — NOT when the file channel alone is alive. Using ping() here would
        # pick "http" whenever the Script Engine heartbeat is fresh and then
        # every send would hit a refused connection.
        #
        # The probe is not free: a refused loopback connect measures 2.03 s on
        # this machine (plus 0.36 s of getaddrinfo), and the positive channel
        # cache only lives 4 s — so re-proving the same absence before every
        # command was the largest fixed cost of a build (build-dcca6bd4ab7f:
        # 435 sends, every one on the file channel, none ever on HTTP). A
        # bridge proved absent stays absent for _HTTP_ABSENT_TTL_SECONDS. The
        # verdict is unchanged with or without the cache; only the cost moves.
        if time.time() >= self._http_absent_until:
            if self._http_ping().reachable:
                return "http"
            self._http_absent_until = time.time() + self._HTTP_ABSENT_TTL_SECONDS
        try:
            if self._file_bridge.pt_alive():
                return "file"
        except OSError:
            pass
        return ""

    def _http_ping(self, timeout: float = 5.0) -> AdapterInfo:
        """HTTP-bridge-only probe. Does NOT fall back to the file channel.

        Used by _probe_channels to decide whether the HTTP channel is an
        option at all. The public ping() does fall back, so it must not be
        used for channel routing.
        """
        try:
            status, body = self._http("GET", "/ping", timeout=timeout)
        except BridgeUnreachable as exc:
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known(f"http bridge not reachable: {exc}"),
            )
        if status != 200 or not body:
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known(f"/ping answered HTTP {status}"),
            )
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known("port held by an unidentified service"),
            )
        if data.get("service") != "pt-mcp-bridge":
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known("port held by a different service"),
            )
        if data.get("id") != self._fingerprint:
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known("a foreign bridge holds the port (token fingerprint mismatch)"),
            )
        return AdapterInfo(
            self.name, self.version, True,
            Observable.known("http bridge authenticated"),
        )

    def _invalidate_channel(self) -> None:
        """Drop the cached channel so the next send re-probes.

        Also forgets a remembered HTTP absence: this runs when a send failed,
        which is exactly the moment the other channel deserves another look
        (the bridge may have come up). It cannot thrash — `_send_http` only
        runs on a channel whose HTTP probe just succeeded, so the negative
        cache is already clear there.
        """
        self._channel = None
        self._channel_cached_at = None
        self._http_absent_until = 0.0

    def _send_http(self, expression: str, statements: list[str] | None, wait: float) -> Observable[str]:
        rid = next_rid()
        payload = report_result_js(port=self.port, token=self.token, rid=rid) + js.wrap_with_report(
            expression, "", statements
        )
        # report_result_js already defines reportResult; wrap_with_report with
        # an empty report_fn_js appends the reporting call. Pass "" so the
        # definition is not duplicated.
        try:
            status, _ = self._http(
                "POST", f"/queue?t={self.token}", body=payload, timeout=10.0
            )
        except BridgeUnreachable as exc:
            self._invalidate_channel()
            return Observable.unknown(f"bridge unreachable while queueing: {exc}")
        if status != 200:
            return Observable.unknown(f"/queue answered {status}")
        try:
            status, body = self._http(
                "GET",
                f"/result?t={self.token}&rid={rid}&wait={min(wait, 60.0):.1f}",
                timeout=wait + 10.0,
            )
        except BridgeUnreachable as exc:
            return Observable.unknown(f"bridge unreachable while waiting for result: {exc}")
        if status != 200 or not body:
            self._invalidate_channel()
            return Observable.unknown(f"no result for rid={rid} within {wait:.0f}s")
        if self._evidence is not None:
            # The payload embeds the bridge token inside the report URL; the
            # evidence store must never see the secret itself.
            sanitized = payload.replace(self.token, "***REDACTED***")
            self._evidence.record(
                "bridge_send",
                {"rid": rid, "js": sanitized, "result": body, "channel": "http"},
                component="adapter.packet_tracer",
            )
        return Observable.known(body, source=f"bridge:{rid}")

    def _send_file(self, expression: str, statements: list[str] | None, wait: float) -> Observable[str]:
        # The Script Engine injects a LOCAL reportResult and captures its
        # argument, so the body goes out without the HTTP report definition —
        # the same single-report wrapper, just answered by file instead of XHR.
        body = js.wrap_with_report(expression, "", statements)
        answer = self._file_bridge.send_and_wait(body, timeout=wait)
        if self._evidence is not None:
            self._evidence.record(
                "bridge_send",
                {"js": body, "result": answer, "channel": "file"},
                component="adapter.packet_tracer",
            )
        if answer is None:
            return Observable.unknown(f"file channel produced no result within {wait:.0f}s")
        return Observable.known(answer, source="file-bridge")

    def send_expression(
        self,
        expression: str,
        *,
        statements: list[str] | None = None,
        wait: float | None = None,
    ) -> Observable[str]:
        """Queue one correlated command and wait for its correlated result.

        The returned Observable is KNOWN with PT's reported string ('OK:...',
        'yes', 'PT_ERROR: ...', ...) or UNKNOWN when no answer arrived —
        a timeout is never mapped to success.
        """
        wait = wait or self._max_wait
        channel = self._resolve_channel()
        if channel == "http":
            return self._send_http(expression, statements, wait)
        if channel == "file":
            return self._send_file(expression, statements, wait)
        return Observable.unknown(
            "no live channel: HTTP bridge sees no PT poll and the file mailbox "
            "heartbeat is stale (is PT open with the extension installed?)"
        )

    # Channel selection is cached for a few seconds. Probing costs two HTTP
    # round trips and was re-issued before EVERY command, which dominated the
    # wall clock of a multi-op build (§35). A channel that goes bad mid-run is
    # still recovered: a failed send clears the cache and the next command
    # re-probes.
    _CHANNEL_CACHE_TTL_SECONDS: float = 4.0

    #: One stalled PT session must not decide the build. PT's Script Engine
    #: stops answering for stretches of variable length while the mailbox
    #: heartbeat only ticks between executions. Measured twice on the
    #: reference build's evidence timeline: ~10 s in build-e77b084cd8b8
    #: (absorbed by luck of timing — 1 op blocked) and 215 s in
    #: build-81df879ad555 (20:39:11 → 20:42:55 UTC — 8 preconditions
    #: BLOCKED, the 22-host batch never left the machine, 102/123). A send
    #: on an adapter that HAS seen a live channel therefore shares one
    #: bounded wait for the channel to return instead of concluding UNKNOWN
    #: on the first dark probe (see _await_channel_recovery). Bounded at
    #: 240 s: the measured worst case was 215 s, and a build that has truly
    #: lost PT pays the window once — every later send fails fast.
    _CHANNEL_OUTAGE_BUDGET_S: float = 240.0
    #: How often the recovery window re-probes. One heartbeat poll each.
    _CHANNEL_RECOVERY_POLL_S: float = 1.0

    #: How long a proven HTTP-bridge absence is remembered. The refused
    #: connect costs 2.03 s on this machine, so re-probing it every channel
    #: TTL was the single largest fixed cost in a build; 30 s bounds the
    #: discovery delay for a bridge that appears mid-run to one probe.
    _HTTP_ABSENT_TTL_SECONDS: float = 30.0

    # -- probing ----------------------------------------------------------

    def ping(self, timeout: float = 5.0) -> AdapterInfo:
        """Overall reachability: HTTP bridge OR Script Engine file channel.

        Falls back to the file channel when HTTP is not hosted — PT is open
        with the extension polling the mailbox, so it is perfectly reachable
        even though no HTTP bridge is listening.
        """
        http = self._http_ping(timeout)
        if http.reachable:
            # Authenticated; is PT actually polling? /status carries that.
            try:
                status, body = self._http("GET", f"/status?t={self.token}", timeout=timeout)
                connected = bool(json.loads(body).get("connected")) if status == 200 else False
            except (BridgeUnreachable, json.JSONDecodeError):
                connected = False
            detail = (
                "bridge authenticated; Packet Tracer extension is polling"
                if connected
                else "bridge authenticated; no Packet Tracer poll seen yet (is PT open with the extension?)"
            )
            return AdapterInfo(self.name, self.version, connected, Observable.known(detail))
        # HTTP unavailable: try the file channel before declaring unreachable.
        try:
            if self._file_bridge.pt_alive():
                return AdapterInfo(
                    self.name, self.version, True,
                    Observable.known(
                        "reachable via Script Engine file channel "
                        "(HTTP bridge not hosted)"
                    ),
                )
        except OSError:
            pass
        return AdapterInfo(
            self.name, self.version, False,
            Observable.known(http.detail.value or "bridge unreachable"),
        )

    # -- inspection ---------------------------------------------------------

    def device_exists(self, name: str) -> Observable[bool]:
        # Answered from the shared snapshot when one is warm: a build asks this
        # before and after every mutation, and each fresh answer is a full
        # topology read. Only fall back to the dedicated probe when the
        # snapshot cannot be read at all.
        for attempt in range(_EXISTS_READ_ATTEMPTS):
            snapshot = self._live_devices(fresh=False)
            if snapshot.is_known:
                found = any(str(d.get("name")) == name for d in (snapshot.value or []))
                return Observable.known(found, source=snapshot.source)
            # UNKNOWN here means the channel hiccuped, not that the device is
            # absent. Callers fail closed on UNKNOWN, so one dropped poll would
            # reject a create that was perfectly safe - retry before concluding
            # that existence really cannot be established.
            if attempt + 1 < _EXISTS_READ_ATTEMPTS:
                time.sleep(_EXISTS_READ_SETTLE_S)
        answer = self.send_expression(js.js_device_exists_expr(name), wait=10.0)
        if not answer.is_known:
            return Observable[bool].unknown(answer.detail or answer.status.value)
        value = str(answer.value)
        if value == "yes":
            return Observable.known(True, source=answer.source)
        if value == "no":
            return Observable.known(False, source=answer.source)
        if value.startswith("PT_ERROR"):
            return Observable[bool].unknown(f"PT reported: {value}")
        return Observable[bool].unknown(f"unexpected answer: {value!r}")

    def link_exists(
        self, a_device: str, a_port: str, b_device: str, b_port: str
    ) -> Observable[bool]:
        # Peer-aware (P1-3): it is not enough for each port to have *some* link
        # - they have to be connected to each other. The previous version
        # validated R1:Fa0/1->R2 against R2:Fa0/2->R3: two wrong cables
        # counted as one correct link.
        snapshot = self._live_devices(fresh=False)
        if snapshot.is_known:
            for d in (snapshot.value or []):
                if str(d.get("name")) != a_device:
                    continue
                for p in (d.get("ports") or []):
                    if str(p.get("name")) != a_port or not p.get("linked"):
                        continue
                    if b_device in [str(x) for x in (p.get("peers") or [])]:
                        return Observable.known(True, source=snapshot.source)
            return Observable.known(False, source=snapshot.source)

        answer = self.send_expression(
            js.js_link_peer_check_expr(a_device, a_port, b_device, b_port), wait=10.0
        )
        if not answer.is_known:
            return Observable[bool].unknown(answer.detail or answer.status.value)
        value = str(answer.value)
        if value == "yes":
            return Observable.known(True, source=answer.source)
        if value.startswith("yes:"):
            return Observable.known(True, source=answer.source)
        if value == "no" or value.startswith("no:"):
            # 'no:different links' / 'no:an endpoint has no link' — carry the
            # reason so diagnosis can tell a missing cable from a wrong peer.
            return Observable.known(False, source=answer.source)
        if value.startswith("unknown"):
            return Observable[bool].unknown(f"peer check could not answer: {value!r}")
        return Observable[bool].unknown(f"unexpected answer: {value!r}")

    def _live_devices(self, *, fresh: bool = True) -> Observable[list[dict]]:
        """One topology read-back: [{name, model, ports:[{name,ip,mask,up,linked}]}].

        Built on the upstream-verified live-devices JS. Anything that goes
        wrong (no channel, PT error, unparseable JSON) is UNKNOWN — never an
        empty list, which would read as "workspace is empty".

        A short-lived cache absorbs the burst of existence checks a build makes
        (idempotency, preconditions, postconditions). Those are asked in quick
        succession and a fresh read per question is what made a twenty-link
        topology pay for a full topology read per link. The window is small
        enough that a deliberate re-read after a mutation still sees the
        change, and callers that need ground truth pass ``fresh=True``.
        """
        if not fresh:
            cached = self._snapshot_cache
            if cached is not None and (
                self._snapshot_ttl <= 0
                or time.time() - self._snapshot_at < self._snapshot_ttl
            ):
                return cached

        statements, final = js.js_live_devices_statements()
        answer = self.send_expression(final, statements=statements, wait=10.0)
        if not answer.is_known:
            return Observable[list[dict]].unknown(answer.detail or answer.status.value)
        value = str(answer.value)
        if value.startswith("PT_ERROR"):
            return Observable[list[dict]].unknown(f"PT reported: {value}")
        try:
            data = json.loads(value)
            devices = data.get("devices")
            if not isinstance(devices, list):
                raise ValueError("devices is not a list")
            result_obs: Observable[list[dict]] = Observable.known(
                list(devices), source=answer.source
            )
            self._snapshot_cache = result_obs
            self._snapshot_at = time.time()
            return result_obs
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            return Observable[list[dict]].unknown(
                f"unparseable topology answer: {exc}: {value[:120]!r}"
            )

    def list_device_names(self) -> Observable[list[str]]:
        devices = self._live_devices()
        if not devices.is_known:
            return Observable[list[str]].unknown(devices.detail or devices.status.value)
        names = [str(d.get("name") or "") for d in devices.value]
        return Observable.known([n for n in names if n], source=devices.source)

    def _observation_from_live(self, match: dict) -> DeviceObservation:
        name_raw = match.get("name")
        name = str(name_raw) if name_raw else ""
        observation = DeviceObservation(name=name)
        source = f"live-readback:{name}"
        model_raw = match.get("model")
        observation.model = (
            Observable.known(str(model_raw), source=source)
            if model_raw and isinstance(model_raw, str)
            else Observable[str].unknown("model not reported by read-back")
        )
        for p in match.get("ports") or []:
            pname_raw = p.get("name")
            pname = str(pname_raw) if pname_raw else ""
            if not pname:
                continue
            port = observation.port(pname)
            up = p.get("up")
            if up is True:
                port.state = Observable.known("up", source=source)
            elif up is False:
                port.state = Observable.known("down", source=source)
            else:
                port.state = Observable[str].unknown("port up-state not reported")
            ip_raw = p.get("ip")
            mask_raw = p.get("mask")
            port.ip = (
                Observable.known(str(ip_raw), source=source)
                if ip_raw and isinstance(ip_raw, str) and ip_raw.strip()
                else Observable[str].unknown("no ip on this port")
            )
            port.mask = (
                Observable.known(str(mask_raw), source=source)
                if mask_raw and isinstance(mask_raw, str) and mask_raw.strip()
                else Observable[str].unknown("no mask on this port")
            )
            link_class = p.get("linkClass")
            port.link_class = (
                Observable.known(str(link_class), source=source)
                if link_class
                else Observable[str].unknown("link type not reported")
            )
            peers = [
                str(peer) for peer in (p.get("peers") or []) if str(peer).strip()
            ]
            port.peers = (
                Observable.known(peers, source=source)
                if peers
                else Observable[list[str]].unknown("no peer device reported")
            )
        self._fill_vlan_state(observation)
        return observation

    def _fill_vlan_state(self, observation: DeviceObservation) -> None:
        """#20: put real VLAN membership on the ports, or the L2 criteria can
        never pass.

        `vlan_defined` and `trunk_configured` read `port.vlan`/`port.trunk`, and
        the topology snapshot carries neither field, so both stayed
        not-observed and every L2 criterion reported FAIL against a switch that
        had been configured correctly. `show vlan brief` answers both from one
        command: a port listed under a single VLAN is an access port, a port
        listed under several is carrying a trunk.

        A read that fails leaves the fields untouched - not-observed, which the
        verifier reports as UNKNOWN rather than inventing a PASS.
        """
        if not self._observe_vlans or not observation.ports:
            return
        model = observation.model
        if not model.is_known:
            return
        if category_of_model(str(model.value)) not in _VLAN_CATEGORIES:
            # F-5/A-015: routers/firewalls have no `show vlan brief`, so the
            # switch-side membership pass cannot run — but the topology read
            # DOES list every ROAS subinterface (probe-verified live: rhq
            # GigabitEthernet0/0.10/.20/.30), which is the honest trunk
            # evidence for the parent port.
            self._fill_trunk_from_subinterfaces(observation)
            return
        table = self.read_vlans(observation.name)
        if not table.is_known:
            return
        membership: dict[str, list[str]] = {}
        for vlan in table.value or []:
            vlan_id = str(vlan.get("id"))
            for port_name in vlan.get("ports") or []:
                # `show vlan brief` pads a multi-entry Ports column, so a
                # parsed name can still carry leading spaces.
                membership.setdefault(str(port_name).strip(), []).append(vlan_id)
        for port_name, vlan_ids in membership.items():
            port = observation.ports.get(port_name)
            if port is None:
                continue
            if len(vlan_ids) == 1:
                port.vlan = Observable[str].known(
                    vlan_ids[0], source="show vlan brief"
                )
                port.trunk = Observable[bool].known(
                    False, source="show vlan brief"
                )
            else:
                port.vlan = Observable[str].known(
                    ",".join(vlan_ids), source="show vlan brief"
                )
                port.trunk = Observable[bool].known(
                    True, source="show vlan brief"
                )

    def _fill_trunk_from_subinterfaces(self, observation: DeviceObservation) -> None:
        """F-5: router-on-a-stick trunk state, read from the topology itself.

        A parent Ethernet port carrying dot1Q subinterfaces is trunking (the
        encapsulation is what made PT accept addresses on those children); a
        parent without any is a plain routed link. Evidence only — a read
        that cannot see subinterfaces leaves `port.trunk` not-observed, which
        the verifier reports as UNKNOWN rather than inventing a verdict.
        """
        parents_with_children = {
            name.split(".", 1)[0]
            for name in observation.ports
            if "." in name and name.split(".", 1)[0] in observation.ports
        }
        for name, port in observation.ports.items():
            if "." in name:
                continue
            if not name.startswith(
                ("Ethernet", "FastEthernet", "GigabitEthernet", "TenGigabitEthernet")
            ):
                continue
            port.trunk = Observable[bool].known(
                name in parents_with_children, source="topology subinterfaces"
            )

    def inspect_device(self, name: str) -> DeviceObservation:
        # S1.6b: one snapshot feeds the whole read-back burst. Freshness is
        # the mutation's job — every mutating call already invalidates the
        # snapshot — and the TTL bounds anything out-of-band (user hands on
        # PT) to `_snapshot_ttl` seconds.
        devices = self._live_devices(fresh=False)
        observation = DeviceObservation(name=name)
        if not devices.is_known:
            observation.notes.append(
                f"topology read-back unavailable: {devices.detail or devices.status.value}"
            )
            return observation
        match = next((d for d in devices.value if str(d.get("name") or "") == name), None)
        if match is None:
            observation.notes.append("not present in observed workspace")
            return observation
        return self._observation_from_live(match)

    def observe_state(
        self,
        names: list[str] | None = None,
        *,
        observed_at: str | None = None,
    ) -> ActualNetworkState:
        """One topology fetch feeds every requested device.

        Same contract as the base implementation (known-absent devices are
        recorded as absent metadata, never as empty observations); when the
        fetch fails it falls back to the base per-device behavior so the
        failure detail stays per-device instead of being lost.
        """
        if names is None:
            return super().observe_state(None, observed_at=observed_at)
        devices = self._live_devices(fresh=False)
        if not devices.is_known:
            return super().observe_state(names, observed_at=observed_at)
        state = ActualNetworkState(
            source=f"adapter:{self.name}", observed_at=observed_at or utc_now_iso()
        )
        for name in names:
            match = next(
                (d for d in devices.value if str(d.get("name") or "") == name), None
            )
            if match is None:
                state.metadata[f"absent:{name}"] = Observable[str].known(
                    "reported absent by adapter", source=devices.source
                )
                continue
            state.observe_device(self._observation_from_live(match))
        return state

    # -- mutations ----------------------------------------------------------

    def _precondition_receipt(
        self, operation: str, requested: dict, operation_id: str | None,
        pre: list[CheckResult], error: str,
    ) -> OperationReceipt:
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation=operation,
            requested=requested,
            result=OperationResult.REJECTED,
            preconditions=pre,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def create_device(
        self,
        name: str,
        role: str,
        model_hint: str | None = None,
        position: tuple[int, int] | None = None,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        requested = {"name": name, "role": role, "model_hint": model_hint, "position": position}
        exists = self.device_exists(name)
        pre = [
            CheckResult.of("device_absent", "False", exists, compare=lambda v, _: v is False)
        ]
        if not (exists.is_known and exists.value is False):
            return self._precondition_receipt(
                "create_device", requested, operation_id, pre,
                "existence could not be verified before mutation (fail-closed)",
            )

        # Endpoint default follows the upstream catalog's pt_type ("PC-PT"):
        # found live, plain "PC" makes lw.addDevice fail silently and the
        # addDevice fallback answer "Invalid arguments" — no device, no error.
        model_aliases = {"PC": "PC-PT"}
        default_model = {"router": "Router-PT", "switch": "Switch-PT"}.get(role, "PC-PT")
        model = model_aliases.get(model_hint, model_hint) if model_hint else default_model
        # S1.12: PT places some chassis only under its own spelling
        # ("4321" -> "ISR4321", "Smart-LED" -> "Smart LED"); the autopilot
        # key fails silently, so translate before sending.
        from ...pt.catalog.aliases import pt_model_string
        model = pt_model_string(model)
        x, y = position or (100, 100)
        try:
            type_code = js.device_type_code_for_model(model, role=role)
        except ValueError as exc:
            return self._precondition_receipt(
                "create_device", requested, operation_id, pre, str(exc)
            )
        answer = self.send_expression(
            js.js_add_device(name, type_code, model, x, y)
        )
        self._invalidate_snapshot()  # the topology just changed
        # PT publishes the device asynchronously; a link created immediately
        # after can find it missing. Wait for the read-back to actually show it
        # rather than sleeping a fixed fraction of a second - the same publish
        # lag that breaks cabling is much larger on a busy PT.
        result, executed, error = self._interpret(answer)
        if result is OperationResult.SUCCESS:
            self._settle_until_published([name], timeout=20.0)
        post: list[CheckResult] = []
        if result is OperationResult.SUCCESS:
            after = self.device_exists(name)
            post = [CheckResult.of("device_present", "True", after)]
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="create_device",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=post,
            evidence={"model": model},
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def create_devices_bulk(
        self, specs: list[dict], operation_id_prefix: str = "batch-create"
    ) -> list[OperationReceipt]:
        """Create many devices in ONE round-trip.

        A single device needs a few PT calls, and each call costs a round-trip
        through the script engine. When a topology wants twenty hosts, doing
        that serially is the dominant cost of a build. This sends them as one
        command and returns a receipt per device, so the DAG still sees one
        operation per device and the per-device evidence is preserved.

        Each device is still created independently inside PT (each wrapped in
        its own try/catch), so a model that PT rejects is reported as its own
        failed receipt instead of aborting the batch.
        """
        receipts: list[OperationReceipt] = []
        if not specs:
            return receipts

        prepared: list[tuple[dict, tuple[str, int, str, int, int]]] = []
        for spec in specs:
            name = spec["name"]
            role = spec.get("role", "pc")
            # The executor hands us raw DAG op inputs, where the planned model
            # lives under "model"; direct callers use "model_hint". Accepting
            # only one of the two silently built a Router-PT for an op whose
            # ports were solved against a 2911, and every addLink then came
            # back as a bare "false" on a port the device did not have.
            model_hint = spec.get("model_hint") or spec.get("model")
            model_aliases = {"PC": "PC-PT"}
            default_model = {"router": "Router-PT", "switch": "Switch-PT"}.get(role, "PC-PT")
            model = (
                model_aliases.get(model_hint, model_hint)
                if model_hint else default_model
            )
            x, y = spec.get("position") or (100, 100)
            try:
                type_code = js.device_type_code_for_model(model, role=role)
            except ValueError as exc:
                receipts.append(OperationReceipt(
                    operation_id=f"{operation_id_prefix}-{name}",
                    operation="create_device",
                    requested=spec,
                    result=OperationResult.REJECTED,
                    started_at=utc_now_iso(),
                    finished_at=utc_now_iso(),
                    error=str(exc),
                ))
                continue
            prepared.append((spec, (name, type_code, model, int(x), int(y))))

        if not prepared:
            return receipts

        # One command for every device whose model resolved to a PT type.
        batch = [item for _, item in prepared]
        stmts, final = js.js_batch_add_devices(batch)
        answer = self.send_expression(final, statements=stmts, wait=max(15.0, 2.0 * len(batch)))
        self._invalidate_snapshot()  # the topology just changed
        # PT publishes a device to ipc.network() asynchronously, and a router
        # registers its interfaces a beat after the device appears. A link
        # created in the very next command can find the device missing, or find
        # it without the ports yet, and PT answers 'false' with no error. This
        # settle is what lets the link batch (or any immediate read) see a
        # workspace that has caught up. Without it a build cables into a
        # half-registered topology and reports phantom link failures.
        #
        # Poll for the devices instead of sleeping a flat interval: the delay
        # grows with the batch AND with how busy PT already is, and a fixed
        # sleep is either wasteful on a quiet PT or too short on a loaded one.
        # Bounded, so a device PT genuinely refused cannot stall the build.
        # prepared entries are (spec, (name, type_code, model, x, y)); only the
        # names are needed to know when PT has caught up.
        self._settle_until_published(
            [entry[1][0] for entry in prepared], timeout=45.0
        )
        per_device: dict[str, dict] = {}
        if answer.is_known:
            try:
                for row in json.loads(str(answer.value)):
                    per_device[str(row.get("name"))] = row
            except (json.JSONDecodeError, ValueError):
                per_device = {}

        # One workspace read confirms the whole batch. Reading each device
        # separately is a round-trip per device, which is the cost the batch
        # exists to remove.
        snapshot = self._live_devices()
        present = set()
        if snapshot.is_known:
            present = {
                str(d.get("name")) for d in (snapshot.value or [])
            }

        # A freshly started PT can answer the batch with `ok:true` for every
        # device and still place none of them (observed live on a scheduled
        # night: all four T06 creates "succeeded" and the read-back was
        # empty). Re-send ONLY the missing devices once, then let the
        # read-back decide. This is safe: present names are never re-added,
        # and an unknown snapshot is not retried (it would duplicate work).
        retried: set[str] = set()
        if snapshot.is_known:
            missing = [
                (spec, item) for spec, item in prepared if item[0] not in present
            ]
            if missing:
                retried = {item[0] for _, item in missing}
                self._settle_until_published(
                    sorted(retried), timeout=10.0
                )
                retry_batch = [item for _, item in missing]
                stmts2, final2 = js.js_batch_add_devices(retry_batch)
                answer2 = self.send_expression(
                    final2,
                    statements=stmts2,
                    wait=max(15.0, 2.0 * len(retry_batch)),
                )
                self._invalidate_snapshot()
                self._settle_until_published(sorted(retried), timeout=45.0)
                if answer2.is_known:
                    try:
                        for row in json.loads(str(answer2.value)):
                            per_device[str(row.get("name"))] = row
                    except (json.JSONDecodeError, ValueError):
                        pass
                snapshot = self._live_devices()
                if snapshot.is_known:
                    present = {
                        str(d.get("name")) for d in (snapshot.value or [])
                    }

        for spec, (name, type_code, model, x, y) in prepared:
            row = per_device.get(name, {})
            ok = bool(row.get("ok"))
            # Read-back decides, not the return string: PT can answer truthy
            # for a device it did not actually place.
            is_present = name in present
            result = OperationResult.SUCCESS if (ok and is_present) else OperationResult.FAILED
            post = []
            if snapshot.is_known:
                post = [CheckResult.of(
                    "device_present", "True",
                    Observable.known(True, source=snapshot.source)
                    if is_present else
                    Observable[bool].unknown(f"{name} absent from the workspace read"),
                )]
            receipts.append(OperationReceipt(
                operation_id=f"{operation_id_prefix}-{name}",
                operation="create_device",
                requested=spec,
                executed={"batched": True, "model": model, "js_ok": ok,
                          "retry_batch": name in retried},
                result=result,
                postconditions=post,
                evidence={"model": model, "batch": True,
                          "js_error": row.get("error", "")},
                started_at=utc_now_iso(),
                finished_at=utc_now_iso(),
                error=None if result is OperationResult.SUCCESS else (
                    row.get("error") or "device not present after create"
                ),
            ))
        return receipts

    def rename_device(
        self, old_name: str, new_name: str, operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"old_name": old_name, "new_name": new_name}
        old_exists = self.device_exists(old_name)
        new_absent = self.device_exists(new_name)
        pre = [
            CheckResult.of("old_device_present", "True", old_exists),
            CheckResult.of("new_name_free", "False", new_absent, compare=lambda v, _: v is False),
        ]
        if not (
            old_exists.is_known and old_exists.value
            and new_absent.is_known and new_absent.value is False
        ):
            return self._precondition_receipt(
                "rename_device", requested, operation_id, pre,
                "precondition could not be verified (fail-closed)",
            )
        statements = [
            f"var dev=ipc.network().getDevice({js._q(old_name)});",
            "if(!dev){throw new Error('device vanished');}",
            f"dev.setName({js._q(new_name)});",
        ]
        answer = self.send_expression("'renamed'", statements=statements)
        result, executed, error = self._interpret(answer)
        post: list[CheckResult] = []
        if result is OperationResult.SUCCESS:
            post = [CheckResult.of("renamed_present", "True", self.device_exists(new_name))]
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="rename_device",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=post,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def position_device(
        self, name: str, x: int, y: int, operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"name": name, "x": x, "y": y}
        exists = self.device_exists(name)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._precondition_receipt(
                "position_device", requested, operation_id, pre, "device missing or unknown"
            )
        statements = [
            f"var d=ipc.network().getDevice({js._q(name)});",
            "if(!d){throw new Error('device vanished');}",
            # moveToLocation is the verified canvas-move call (upstream
            # pt_move_device uses exactly this).
            f"d.moveToLocation({int(x)}, {int(y)});",
        ]
        answer = self.send_expression("'positioned'", statements=statements)
        result, executed, error = self._interpret(answer)
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="position_device",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            evidence={"postcondition": "canvas position read-back not yet implemented"},
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def create_link(
        self,
        a_device: str,
        a_port: str | None,
        b_device: str,
        b_port: str | None,
        media: str = "auto",
        operation_id: str | None = None,
    ) -> OperationReceipt:
        requested = {
            "a_device": a_device, "a_port": a_port,
            "b_device": b_device, "b_port": b_port, "media": media,
        }
        a_exists = self._device_exists_settled(a_device)
        b_exists = self._device_exists_settled(b_device)
        pre = [
            CheckResult.of("a_device_present", "True", a_exists),
            CheckResult.of("b_device_present", "True", b_exists),
        ]
        if not (a_exists.is_known and a_exists.value and b_exists.is_known and b_exists.value):
            return self._precondition_receipt(
                "create_link", requested, operation_id, pre,
                "endpoint existence could not be verified (fail-closed)",
            )
        try:
            cable = js.cable_name_for(media)
        except ValueError as exc:
            return self._precondition_receipt(
                "create_link", requested, operation_id, pre, str(exc)
            )
        if cable == "auto":
            # PT rejects addLink('auto') for some pairs (found live: PC↔PC
            # answers false while 'cross' works), so 'auto' is resolved from
            # the upstream wiring rules using the OBSERVED models; endpoint
            # pairs have no rule in that table and take a cross-over cable.
            devices = self._live_devices()
            models: list[str] = []
            for dev in (a_device, b_device):
                match = (
                    next(
                        (d for d in devices.value if str(d.get("name") or "") == dev),
                        None,
                    )
                    if devices.is_known
                    else None
                )
                models.append(str(match.get("model") or "") if match else "")
            cat_a = category_of_model(models[0]) or "pc"
            cat_b = category_of_model(models[1]) or "pc"
            cable = infer_cable(cat_a, cat_b)
            endpoints = ("pc", "laptop", "server")
            if cat_a in endpoints and cat_b in endpoints:
                cable = "cross"
        answer = self.send_expression(
            js.js_add_link(a_device, a_port or "", b_device, b_port or "", cable)
        )
        self._invalidate_snapshot()  # cabling changed the topology
        result, executed, error = self._interpret(answer)

        # PT can refuse a cable it has not finished registering — usually an
        # endpoint created moments ago, whose ports are not published yet. It
        # answers a bare 'false' with no error, which is indistinguishable from
        # a genuinely bad pairing. So a refusal earns escalating waits and
        # retries: a real pairing still fails (after the waits), and a merely-
        # too-early one succeeds. The escalation matters because PT's publish
        # delay is not a fixed small number - it grows with the batch being
        # published and with how much work PT already has queued, so the budget
        # has to cover a loaded PT and not just an idle one. Only a refusal pays
        # any of this, so the common path costs nothing.
        attempts = 0
        while result is OperationResult.FAILED and attempts < _LINK_RETRY_ATTEMPTS:
            time.sleep(_LINK_RETRY_BASE_S * (attempts + 1))
            self._invalidate_snapshot()
            retry = self.send_expression(
                js.js_add_link(a_device, a_port or "", b_device, b_port or "", cable)
            )
            retry_result, retry_executed, retry_error = self._interpret(retry)
            attempts += 1
            if retry_result is OperationResult.SUCCESS:
                result, executed, error = retry_result, retry_executed, retry_error
                self._invalidate_snapshot()

        postconditions: list[CheckResult] = []
        if result is OperationResult.SUCCESS:
            # read-back: the link exists only if BOTH endpoints report one
            linked = self.link_exists(a_device, a_port or "", b_device, b_port or "")
            postconditions.append(CheckResult.of("link_present", "True", linked))
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="create_link",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=postconditions,
            evidence={
                **(executed or {}),
                **(
                    {"postcondition": "both endpoints report a link (read-back)"}
                    if result is OperationResult.SUCCESS
                    else {}
                ),
            },
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def _cable_for_media(
        self, media: str, a_device: str, b_device: str, models: dict[str, str]
    ) -> str:
        """Resolve the cable name for one link, given already-read models.

        Split out so a batch can read the workspace once and resolve every
        link from that single snapshot, instead of a full topology read per
        link.
        """
        try:
            cable = js.cable_name_for(media)
        except ValueError:
            raise
        if cable != "auto":
            return cable
        cat_a = category_of_model(models.get(a_device, "")) or "pc"
        cat_b = category_of_model(models.get(b_device, "")) or "pc"
        cable = infer_cable(cat_a, cat_b)
        if cat_a in ("pc", "laptop", "server") and cat_b in ("pc", "laptop", "server"):
            cable = "cross"
        return cable

    def create_links_bulk(
        self, links: list[dict], operation_id_prefix: str = "batch-link"
    ) -> list[OperationReceipt]:
        """Create many links in ONE round-trip.

        The per-link `auto` media path used to read the whole workspace to
        learn two device models. For a forty-link topology that is forty full
        topology reads before any cable is even placed. Here the workspace is
        read once, every cable is resolved from that snapshot, and the links
        go out together.
        """
        receipts: list[OperationReceipt] = []
        if not links:
            return receipts

        devices = self._live_devices()
        models = {
            str(d.get("name")): str(d.get("model") or "")
            for d in (devices.value or [])
        } if devices.is_known else {}

        prepared: list[tuple[dict, tuple[str, str, str, str, str]]] = []
        for spec in links:
            a = spec["a_device"]; ap = spec.get("a_port") or ""
            b = spec["b_device"]; bp = spec.get("b_port") or ""
            media = spec.get("media", "auto")
            try:
                cable = self._cable_for_media(media, a, b, models)
            except ValueError as exc:
                receipts.append(OperationReceipt(
                    operation_id=f"{operation_id_prefix}-{spec.get('link_id','')}",
                    operation="create_link", requested=spec,
                    result=OperationResult.REJECTED,
                    started_at=utc_now_iso(), finished_at=utc_now_iso(),
                    error=str(exc),
                ))
                continue
            prepared.append((spec, (a, ap, b, bp, cable)))

        if not prepared:
            return receipts

        batch = [item for _, item in prepared]
        stmts, final = js.js_batch_add_links(batch)
        answer = self.send_expression(
            final, statements=stmts, wait=max(20.0, 2.0 * len(batch))
        )
        self._invalidate_snapshot()  # cabling changed the topology
        per_link: dict[str, dict] = {}
        if answer.is_known:
            try:
                for row in json.loads(str(answer.value)):
                    per_link[str(row.get("link"))] = row
            except (json.JSONDecodeError, ValueError):
                per_link = {}

        # One workspace read confirms every link at once: a port with a
        # `Cable`/`Antenna` object and the expected peer is a present link.
        snapshot = self._live_devices()
        wired: set[tuple[str, str, str, str]] = set()
        if snapshot.is_known:
            for d in (snapshot.value or []):
                name = str(d.get("name"))
                for p in (d.get("ports") or []):
                    if not p.get("linked"):
                        continue
                    for peer in (p.get("peers") or []):
                        peer = str(peer)
                        # A link is symmetric; record both directions so the
                        # lookup does not depend on argument order.
                        wired.add((name, str(p.get("name")), peer, ""))
                        wired.add((peer, "", name, str(p.get("name"))))

        def _link_present(a: str, ap: str, b: str, bp: str) -> bool:
            if (a, ap, b, bp) in wired:
                return True
            # The peer's port name is unknown from this side, so match on the
            # (device, port) the local side sees plus the peer device.
            return any(
                w[0] == a and w[1] == ap and w[2] == b
                for w in wired
            ) or any(
                w[0] == b and w[1] == bp and w[2] == a
                for w in wired
            )

        for spec, (a, ap, b, bp, cable) in prepared:
            key = f"{a}:{ap}-{b}:{bp}"
            row = per_link.get(key, {})
            ok = bool(row.get("ok"))
            present = _link_present(a, ap, b, bp)
            result = (
                OperationResult.SUCCESS if (ok and present)
                else OperationResult.FAILED
            )
            # A batch is a latency optimisation, never a correctness shortcut.
            # If PT refused the cable inside the batch — usually a device it
            # had not finished publishing, or a port not yet registered — that
            # one link is redone the slow, reliable way. A build must not fail
            # over a race the batch introduced.
            if result is not OperationResult.SUCCESS:
                retry = self.create_link(
                    a, ap, b, bp, media=spec.get("media", "auto"),
                    operation_id=f"{operation_id_prefix}-{spec.get('link_id', key)}-retry",
                )
                if retry.result is OperationResult.SUCCESS:
                    result = OperationResult.SUCCESS
                    ok, present = True, True
            link_id = spec.get("link_id", key)
            post = []
            if snapshot.is_known:
                post = [CheckResult.of(
                    "link_present", "True",
                    Observable.known(True, source=snapshot.source)
                    if present else
                    Observable[bool].unknown(f"link {key} absent from the workspace read"),
                )]
            receipts.append(OperationReceipt(
                operation_id=f"{operation_id_prefix}-{link_id}",
                operation="create_link",
                requested=spec,
                executed={"batched": True, "cable": cable, "js_ok": ok},
                result=result,
                postconditions=post,
                evidence={"batch": True, "cable": cable,
                          "js_error": row.get("error", "")},
                started_at=utc_now_iso(),
                finished_at=utc_now_iso(),
                error=None if result is OperationResult.SUCCESS else (
                    row.get("error") or "link not present after create"
                ),
            ))
        return receipts

    def _device_exists_settled(self, name: str, attempts: int = 3) -> Observable[bool]:
        """device_exists with a settle for PT's asynchronous publication.

        A device created through the logical workspace can take a moment to
        show up in `ipc.network()`. A single immediate query turned that into
        "endpoint existence could not be verified (fail-closed)" on the link
        that follows its own create, so the question is asked again after a
        short settle before it is treated as unanswerable.
        """
        exists = self.device_exists(name)
        for _ in range(max(attempts, 1) - 1):
            if exists.is_known:
                break
            time.sleep(1.0)
            exists = self.device_exists(name)
        return exists

    def delete_device(
        self, name: str, operation_id: str | None = None
    ) -> OperationReceipt:
        """Remove a device from the logical workspace. Idempotent.

        An already-absent device is a CONFIRMED success: deletion is a
        desired-state operation, and "it is not there" satisfies it.
        """
        requested = {"name": name}
        # Settled read like the link paths: teardown runs right behind a full
        # build, when the heartbeat is most likely to hiccup, and one unknown
        # read used to fail-closed the delete as 'rejected' and leak the
        # device into the user's workspace (scale run: asa1-4).
        before = self._device_exists_settled(name)
        if before.is_known and not before.value:
            # Already absent: CONFIRMED success (absent is the desired state).
            absent = Observable.known(True, source=before.source)
            return OperationReceipt(
                operation_id=operation_id or new_id("op"),
                operation="delete_device",
                requested=requested,
                executed={"decision": "already absent"},
                result=OperationResult.SUCCESS,
                preconditions=[CheckResult.of("device_present", "False", before)],
                postconditions=[CheckResult.of("device_absent", "True", absent)],
                started_at=utc_now_iso(),
                finished_at=utc_now_iso(),
                evidence={"postcondition": "device already absent (idempotent)"},
            )
        if not before.is_known:
            return self._precondition_receipt(
                "delete_device", requested, operation_id,
                [CheckResult.of("device_present", "True", before)],
                "device existence could not be verified (fail-closed)",
            )
        _stmts, _final = js.js_delete_device_statements(name)
        result, executed, error = self._interpret_delete(
            self.send_expression(_final, statements=_stmts, wait=15.0)
        )
        self._invalidate_snapshot()  # the device list changed
        post: list[CheckResult] = []
        if result is OperationResult.SUCCESS:
            # PT keeps the model and the logical workspace in sync
            # asynchronously, so a device can still be visible right after a
            # delete that PT reported as done. Settle, re-check, and run the
            # delete again while it is still there: a delete that only works on
            # the second look is a delete that leaves the workspace dirty.
            after = self.device_exists(name)
            for _ in range(3):
                if not after.is_known or not after.value:
                    break
                time.sleep(1.0)
                self.send_expression(_final, statements=_stmts, wait=15.0)
                after = self.device_exists(name)
            if after.is_known:
                # "absent" is the boolean the check name promises, so a deleted
                # device reads as passed=True rather than str(False)=="True".
                absent = Observable.known(not after.value, source=after.source)
                if after.value:
                    result = OperationResult.FAILED
                    error = error or "device still present after removeDevice"
                post = [CheckResult.of("device_absent", "True", absent)]
            else:
                post = [CheckResult.of("device_absent", "True", after)]
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="delete_device",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=[CheckResult.of("device_present", "True", before)],
            postconditions=post,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def _settle_until_published(
        self, names: list[str], timeout: float = 45.0, interval: float = 1.0
    ) -> bool:
        """Wait until PT's read-back shows every named device WITH its ports.

        Waiting for the device alone is not enough and the difference is the
        whole bug: PT publishes a device to ipc.network() first and registers
        its interfaces a beat later, so a link issued in between finds a device
        that exists and a port that does not, and addLink answers a bare
        'false'. Measured live on a loaded PT, a 63-device build lost a third
        of its cables to exactly that window.

        Bounded on purpose: a model PT refused never appears, and the caller has
        to be told so rather than wait out the clock. Returns True when the
        workspace caught up, False on timeout.
        """
        wanted = {n for n in names if n}
        if not wanted:
            return True
        deadline = time.time() + timeout
        while True:
            self._invalidate_snapshot()
            observed = self._live_devices(fresh=True)
            if observed.is_known:
                published = {
                    str(d.get("name")): d.get("ports") or []
                    for d in (observed.value or [])
                }
                if all(
                    name in published and published[name]
                    for name in wanted
                ):
                    return True
            # Either the channel is struggling or PT has not caught up yet;
            # back off and look again rather than trusting a fixed interval.
            if time.time() >= deadline:
                return False
            time.sleep(interval)

    def delete_devices_bulk(
        self, names: list[str], operation_id_prefix: str = "batch-delete"
    ) -> dict[str, str]:
        """Remove many devices in ONE round-trip. Returns {name: outcome}.

        Teardown is the one place a per-device round-trip does not scale: at
        sixty-odd devices the serial version overran its budget and left half
        the topology in the workspace, which then made every later PT run slow
        enough that existence checks timed out and looked like build failures.

        Each name is still verified by read-back afterwards, and anything the
        batch reports as still present is retried through the single-device
        path - the read-back, not the call, is what decides.
        """
        outcomes: dict[str, str] = {}
        pending = [n for n in names if n]
        if not pending:
            return outcomes

        statements, final = js.js_batch_delete_devices(pending)
        answer = self.send_expression(final, statements=statements, wait=60.0)
        self._invalidate_snapshot()
        reported: dict[str, bool] = {}
        if answer.is_known and not str(answer.value).startswith("PT_ERROR"):
            try:
                for row in json.loads(str(answer.value)) or []:
                    reported[str(row.get("name"))] = bool(row.get("ok"))
            except (ValueError, AttributeError, TypeError):
                reported = {}
        for name in pending:
            outcomes[name] = "deleted" if reported.get(name) else "unknown"

        # PT keeps the model and the logical workspace in sync asynchronously,
        # so one settle-and-check catches the names the batch could not confirm.
        if any(v != "deleted" for v in outcomes.values()):
            time.sleep(1.5)
            self._invalidate_snapshot()
            for name, outcome in outcomes.items():
                if outcome == "deleted":
                    continue
                try:
                    receipt = self.delete_device(name, f"{operation_id_prefix}-{name}")
                    outcomes[name] = receipt.result.value
                except Exception as exc:  # teardown must never raise
                    outcomes[name] = f"error:{type(exc).__name__}"
        return outcomes

    def delete_link(
        self,
        a_device: str,
        a_port: str,
        b_device: str,
        b_port: str,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        """Remove the physical link on endpoint A. Idempotent (absent = OK).

        Deleting one endpoint's cable removes the link; the receipt is
        confirmed only when the port no longer reports a link.
        """
        requested = {
            "a_device": a_device, "a_port": a_port,
            "b_device": b_device, "b_port": b_port,
        }
        a_exists = self._device_exists_settled(a_device)
        b_exists = self._device_exists_settled(b_device)
        pre = [
            CheckResult.of("a_device_present", "True", a_exists),
            CheckResult.of("b_device_present", "True", b_exists),
        ]
        if not (a_exists.is_known and a_exists.value and b_exists.is_known and b_exists.value):
            return self._precondition_receipt(
                "delete_link", requested, operation_id, pre,
                "endpoint existence could not be verified (fail-closed)",
            )
        _stmts, _final = js.js_delete_link_statements(a_device, a_port, b_device, b_port)
        answer = self.send_expression(_final, statements=_stmts, wait=15.0)
        self._invalidate_snapshot()  # uncabling changed the topology
        result, executed, error = self._interpret_delete(answer)
        post: list[CheckResult] = []
        if result is OperationResult.SUCCESS:
            linked = self.link_exists(a_device, a_port, b_device, b_port)
            if linked.is_known:
                absent = Observable.known(not linked.value, source=linked.source)
                if linked.value:
                    result = OperationResult.FAILED
                    error = error or "link still present after deleteLink"
                post = [CheckResult.of("link_absent", "True", absent)]
            else:
                post = [CheckResult.of("link_absent", "True", linked)]
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="delete_link",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=post,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def install_module(
        self,
        device: str,
        slot: str,
        module: str,
        expected_port: str,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        """Install an expansion module and confirm it by reading the port back.

        Idempotent: if `expected_port` is already visible on the device the
        module is already installed and the receipt is a CONFIRMED no-op. The
        receipt is confirmed only when the port actually appears in
        `getPorts()` — `addModule` answering true alone is executed-but-
        unconfirmed, so a module that silently failed surfaces at the link
        that depends on it instead of as a false green here.
        """
        requested = {
            "device": device, "slot": slot, "module": module,
            "expected_port": expected_port,
        }
        exists = self.device_exists(device)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._precondition_receipt(
                "install_module", requested, operation_id, pre, "device missing or unknown"
            )
        # Idempotency: the port already exists -> the module is already in.
        already = self._port_visible(device, expected_port)
        if already.is_known and already.value:
            return OperationReceipt(
                operation_id=operation_id or new_id("op"),
                operation="install_module",
                requested=requested,
                executed={"decision": "module already installed"},
                result=OperationResult.SUCCESS,
                preconditions=pre,
                postconditions=[CheckResult.of("module_port_visible", expected_port, already)],
                evidence={"postcondition": "port visible before install (already installed)"},
                started_at=utc_now_iso(),
                finished_at=utc_now_iso(),
            )
        statements, final = js.js_install_module_statements(
            device, slot, module, expected_port
        )
        answer = self.send_expression(final, statements=statements, wait=20.0)
        self._invalidate_snapshot()  # a module changes the port list
        result, executed, error = self._interpret_delete(answer)
        # 'OK:installed:<port>' = confirmed; 'OK:installed:port-pending' =
        # executed but not observable yet (PT can need the boot to finish).
        value = str(answer.value or "")
        confirmed = value.startswith("OK:installed:") and "port-pending" not in value
        post: list[CheckResult] = []
        if result is OperationResult.SUCCESS:
            visible = self._port_visible(device, expected_port)
            if visible.is_known:
                post = [CheckResult.of("module_port_visible", expected_port, visible)]
                if not visible.value:
                    result = OperationResult.FAILED
                    error = error or f"port {expected_port!r} not visible after install"
            else:
                post = [CheckResult.of("module_port_visible", expected_port, visible)]
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="install_module",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=post,
            evidence={
                **(executed or {}),
                "read_back": "getPorts() after addModule",
                "confirmed": confirmed,
            },
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def _port_visible(self, device: str, port: str) -> Observable[bool]:
        """Whether `port` currently appears on the device's port list."""
        obs = self.inspect_device(device)
        return Observable.known(port in obs.ports, source=f"inspect_device:{device}")

    @staticmethod
    def _interpret_delete(answer) -> tuple[OperationResult, dict | None, str | None]:
        """Turn a delete's textual answer into (result, executed, error).

        'ABSENT:*' is a successful idempotent no-op; 'ERROR:*' is a failure;
        anything unparseable is UNKNOWN, never a silent success.
        """
        if not answer.is_known:
            return OperationResult.UNKNOWN, None, answer.detail or "no answer from PT"
        text = str(answer.value).strip()
        executed = {"answer": text}
        if text.startswith("ABSENT"):
            return OperationResult.SUCCESS, executed, None
        if text.startswith("OK"):
            return OperationResult.SUCCESS, executed, None
        if text.startswith("ERROR"):
            return OperationResult.FAILED, executed, text[len("ERROR:"):]
        return OperationResult.UNKNOWN, executed, f"uninterpretable delete answer: {text!r}"

    def ensure_wireless_radio(
        self,
        device: str,
        *,
        module: str = "PT-LAPTOP-NM-1W",
        slot: str = "0",
        interface: str = "Wireless0",
        operation_id: str | None = None,
    ) -> dict:
        """Install a wireless NIC on a host, replacing the wired one.

        Idempotent by read-back: if the interface is already there the module
        is left alone. PT refuses `addModule` while the wired NIC still owns
        the slot, so the module is removed first - the sequence the extension's
        swapLaptopToWireless uses, and the one verified live.
        """
        exists = self.device_exists(device)
        if not (exists.is_known and exists.value):
            return {
                "installed": False,
                "device": device,
                "error": "device missing or unknown",
            }
        # Idempotent by read-back: the interface is the fact, not the answer.
        if self._port_visible(device, interface).value:
            return {
                "installed": True,
                "device": device,
                "interface": interface,
                "module": module,
                "slot": slot,
                "answer": "already present",
            }
        answer = self.send_expression(
            js.js_install_wireless_radio(device, module, slot, interface)
        )
        text = str(answer.value) if answer.is_known else ""
        marker = "MCPWIRELESSRADIO:"
        payload = text[len(marker):] if text.startswith(marker) else text
        # The module install mutates PT; the read-back decides. A build that
        # trusts the return string alone reported failure for a radio that was
        # demonstrably there, because the file channel can answer a different
        # request under load.
        visible = self._port_visible(device, interface)
        for _ in range(_RADIO_SETTLE_RETRIES):
            if visible.is_known and visible.value:
                break
            # The swap can be mid-flight when the first read lands (PT's own
            # answer even has an `installed:port-pending` state for this) —
            # build-68648f705ccb failed op-wifi-radio-lap5 on exactly one
            # premature read while the port appeared a moment later.
            time.sleep(_RADIO_SETTLE_S)
            visible = self._port_visible(device, interface)
        installed = bool(visible.is_known and visible.value)
        return {
            "installed": installed,
            "device": device,
            "interface": interface if installed else None,
            "module": module,
            "slot": slot,
            "answer": payload,
        }

    def wireless_association(
        self, device: str, interface: str = "Wireless0"
    ):
        """What PT reports about this host's association, read from the port.

        The `Antenna` link is the association: its peer port names the access
        point. No link, or a link of another class, means not associated.
        """
        from ...wireless import WirelessAssociation

        answer = self.send_expression(js.js_wireless_association(device, interface))
        if not answer.is_known:
            return None
        text = str(answer.value)
        marker = "MCPWIFIASSOC:"
        if not text.startswith(marker):
            return None
        try:
            data = json.loads(text[len(marker):])
        except (json.JSONDecodeError, ValueError):
            return None
        if not data.get("associated"):
            return None
        return WirelessAssociation(
            client=device,
            access_point=str(data.get("peer_device") or ""),
            client_interface=interface,
            ap_interface=data.get("peer_interface"),
            link_class=str(data.get("link_class") or "Antenna"),
            port_up=bool(data.get("port_up")),
            evidence=data,
        )

    def list_wireless_associations(self) -> list[str]:
        """Every `device:interface` currently showing an antenna link."""
        answer = self.send_expression(js.js_antenna_links())
        if not answer.is_known:
            return []
        text = str(answer.value)
        marker = "MCPWIFIANT:"
        if not text.startswith(marker):
            return []
        try:
            data = json.loads(text[len(marker):])
        except (json.JSONDecodeError, ValueError):
            return []
        return [str(row) for row in data]

    def set_wireless_channel(
        self, device: str, port: str, channel: int
    ) -> dict[str, Any]:
        """Put a radio on an explicit channel (association determinism)."""
        exists = self.device_exists(device)
        if not (exists.is_known and exists.value):
            return {"ok": False, "device": device, "error": "device missing or unknown"}
        answer = self.send_expression(js.js_set_wireless_channel(device, port, channel))
        text = str(answer.value) if answer.is_known else ""
        marker = "MCPWIFI:"
        payload = text[len(marker):] if text.startswith(marker) else text
        return {
            "ok": payload.startswith("channel="),
            "device": device,
            "port": port,
            "channel": channel,
            "answer": payload,
        }

    def power_cycle(self, device: str) -> dict[str, Any]:
        """Power-cycle a device so its radio re-scans for an access point."""
        exists = self.device_exists(device)
        if not (exists.is_known and exists.value):
            return {"ok": False, "device": device, "error": "device missing or unknown"}
        answer = self.send_expression(js.js_power_cycle(device))
        text = str(answer.value) if answer.is_known else ""
        return {
            "ok": "power-cycled" in text,
            "device": device,
            "answer": text,
        }

    def configure_ios(
        self, device: str, commands: list[str], operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"device": device, "command_count": len(commands)}
        exists = self.device_exists(device)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._precondition_receipt(
                "configure_ios", requested, operation_id, pre, "device missing or unknown"
            )
        expected = _expected_ios_addresses(commands)
        answer: Observable[str] | None = None
        result, executed, error = OperationResult.NOT_EXECUTED, None, None
        missing: dict[str, str] = dict(expected)
        read_back = False
        attempts = 0
        for attempts in range(1, _IOS_CONFIG_ATTEMPTS + 1):
            answer = self.send_expression(
                js.js_configure_ios(device, "\n".join(commands))
            )
            result, executed, error = self._interpret(answer)
            if (
                result is OperationResult.UNKNOWN
                and attempts < _IOS_CONFIG_ATTEMPTS
            ):
                # A channel-stale answer is not IOS's verdict: the push is
                # idempotent and the address read-back below decides truth, so
                # give the channel a bounded chance to carry the script (the
                # 63-device scale build aborted op-config-*-r1 on exactly one
                # stale answer while the re-send a moment later landed).
                # A KNOWN PT failure keeps its verdict on the first answer.
                time.sleep(_IOS_SETTLE_SECONDS)
                continue
            if result is not OperationResult.SUCCESS or not expected:
                break
            # The JS was delivered, but a router that is still booting drops
            # the CLI input without saying so - that is how a 63-device build
            # ended with every op "succeeded" and not one address configured.
            # Re-send until the addresses actually show up on the read-back.
            self._invalidate_snapshot()  # interfaces now carry addresses
            time.sleep(_IOS_SETTLE_SECONDS)
            missing = self._missing_ios_addresses(device, expected)
            read_back = True
            if not missing:
                break
        return self._finish_ios_receipt(
            device,
            commands,
            operation_id=operation_id,
            pre=pre,
            # Captured before the address verdict below can flip `result`: the
            # VLAN read-back still runs (and still reports) on a push PT
            # accepted but whose addresses never landed.
            pushed_ok=result is OperationResult.SUCCESS,
            result=result,
            executed=executed,
            error=error,
            attempts=attempts,
            expected=expected,
            missing=missing,
            read_back=read_back,
        )

    def _finish_ios_receipt(
        self,
        device: str,
        commands: list[str],
        *,
        operation_id: str | None,
        pre: list[CheckResult],
        pushed_ok: bool,
        result: OperationResult,
        executed: dict | None,
        error: str | None,
        attempts: int,
        expected: dict[str, str],
        missing: dict[str, str],
        read_back: bool,
        snapshot: Observable[list[dict]] | None = None,
    ) -> OperationReceipt:
        """Everything `configure_ios` does AFTER the push: read-backs + receipt.

        Shared verbatim with `configure_ios_bulk` so a batch cannot carry
        weaker semantics than the serial path: the same address verdict, the
        same VLAN and route read-backs (rule 8), the same honest UNKNOWNs.

        `pushed_ok` is the JS push's own verdict (PT said `true`), kept apart
        from `result` because the address verdict below can turn a pushed op
        into FAILED - the read-backs still run for it, because the config may
        well be half-applied and that is exactly what they report. `snapshot`
        is the batch's shared topology read (None = a fresh read of its own,
        the serial behaviour); it only feeds `_verify_ios_vlans`, which would
        otherwise pay a second full read for a device the batch already read.
        """
        requested = {"device": device, "command_count": len(commands)}
        if expected:
            post = CheckResult(
                name="configured_addresses_present",
                expected=json.dumps(expected, sort_keys=True),
                actual=Observable.known(
                    {} if not missing else missing,
                    source="file-bridge",
                ),
                passed=not missing,
            )
        else:
            post = CheckResult(
                name="ios_acceptance",
                expected="commands accepted by IOS",
                actual=Observable.unknown(
                    "IOS result semantics are not observable through the bridge; "
                    "verify by read-back/behavior, not by this receipt"
                ),
                passed=None,
            )
        # Bug #5: an address we asked for and cannot read back is a FAILED
        # configuration, not a green op. PT reports `true` for a script it
        # aborted on, so the DAG used to mark these SUCCEEDED and every later
        # check (VLAN, trunk, routes) silently inherited the wrong config.
        # A PT-level failure or timeout keeps its own verdict; it only gets the
        # missing work named alongside it, because "we do not know" and "these
        # addresses are absent" are both true there. Nothing is blamed on the
        # script when no read-back ever ran - the channel failed, not IOS.
        if missing and read_back:
            absent = ", ".join(
                f"{iface}={ip}" for iface, ip in sorted(missing.items())
            )
            detail = f"not observed after {attempts} attempt(s): {absent}"
            if result is OperationResult.SUCCESS:
                result = OperationResult.FAILED
                error = f"configuration did not take effect - {detail}"
            else:
                error = f"{error or 'configuration failed'} - {detail}"
        # Bug #5, VLAN half: the same silence hides a `switchport access vlan`
        # or `vlan <id>` that PT accepted and never applied, and #20 made the
        # table readable. The expectation is parsed from the pushed block and
        # read back with `show vlan brief`; only a read that succeeded AND
        # contradicts the push fails the op (see _verify_ios_vlans).
        vlan_expected = _expected_ios_vlans(commands)
        vlan_missing: list[str] = []
        postconditions = [post]
        if vlan_expected is not None and pushed_ok:
            vlan_post, vlan_missing = self._verify_ios_vlans(
                device, vlan_expected, snapshot=snapshot
            )
            if vlan_post is not None:
                postconditions.append(vlan_post)
        if vlan_missing:
            detail = "vlan state not observed after the push: " + "; ".join(vlan_missing)
            if result is OperationResult.SUCCESS:
                result = OperationResult.FAILED
                error = f"configuration did not take effect - {detail}"
            else:
                error = f"{error or 'configuration failed'} - {detail}"
        # Silent-route-drop gate (Step 4 / S1.19), including its bounded
        # recovery - see `_confirm_ios_routes`. The recovery may only repair
        # the ROUTE half: if this receipt already failed (an address that
        # never landed), that failure is real and survives, because a route
        # that came back does not put the address on the interface.
        route_expected = _expected_ios_routes(commands)
        route_missing: list[dict] = []
        if route_expected and pushed_ok:
            route_post, route_missing = self._confirm_ios_routes(
                device, route_expected
            )
            postconditions.append(route_post)
        if route_missing:
            detail = "routes still missing after retry: " + "; ".join(
                f"{r['network']}/{r['prefix']} via {r['next_hop']}"
                for r in route_missing
            )
            if result is OperationResult.SUCCESS:
                result = OperationResult.FAILED
                error = f"configuration did not take effect - {detail}"
            else:
                error = f"{error or 'configuration failed'} - {detail}"
        statuses = (
            _command_statuses(commands, expected, missing)
            if read_back
            else {command: "no read-back performed" for command in commands}
        )
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="configure_ios",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=postconditions,
            evidence={
                "command_status": json.dumps(statuses, sort_keys=True),
                "attempts": str(attempts),
                **({"missing_addresses": json.dumps(missing, sort_keys=True)}
                   if missing else {}),
                **({"missing_vlans": json.dumps(vlan_missing, sort_keys=True)}
                   if vlan_missing else {}),
                **({"missing_routes": json.dumps(route_missing, sort_keys=True)}
                   if route_missing else {}),
            },
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def _confirm_ios_routes(
        self, device: str, route_expected: list[dict]
    ) -> tuple[CheckResult, list[dict]]:
        """`show ip route` read-back with the bounded silent-drop recovery.

        PT reports ``true`` for an ``ip route`` line that never installs (on a
        heavy push the routes are typed while their exit interfaces are still
        coming up, so IOS discards the line). Read the table back; if an
        expected entry is missing, re-push just the dropped routes in a
        smaller call, SETTLE, and re-read - bounded by
        ``_IOS_ROUTE_RETRY_ROUNDS``. A read typed a moment after the push
        catches the table mid-install and falsely reports the route missing
        (measured on redge), hence the settle.

        Returns ``(postcondition, routes-still-missing)``. This function never
        touches the receipt's result: the caller owns the verdict, so a
        recovered route cannot erase a failure found elsewhere (an address
        that never landed is still FAILED), and a retry read that came back
        UNKNOWN or EMPTY is not a recovery - UNKNOWN never becomes a PASS
        (rule 7/8) and an empty table is a read that contradicted the push.
        """
        table = self.read_routing_table(device)
        if not (table.is_known and table.value is not None):
            # The read itself is reported honestly: a ``show ip route`` that
            # could not run (UNKNOWN) means we cannot prove presence OR
            # absence, so the postcondition stays `passed=None` - same shape
            # as the address half when the topology snapshot is stale.
            return CheckResult(
                name="configured_routes_present",
                expected=json.dumps(route_expected, sort_keys=True),
                actual=Observable.unknown(table.detail or table.status.value),
                passed=None,
            ), []
        observed = {
            (r["network"], r["prefix"], r.get("next_hop")) for r in table.value
        }
        route_missing = [
            r for r in route_expected
            if (r["network"], r["prefix"], r["next_hop"]) not in observed
        ]
        post = CheckResult(
            name="configured_routes_present",
            expected=json.dumps(route_expected, sort_keys=True),
            actual=Observable.known(route_missing, source="file-bridge"),
            passed=not route_missing,
        )
        if not route_missing:
            return post, []
        pending = list(route_missing)
        for _round in range(_IOS_ROUTE_RETRY_ROUNDS):
            if not pending:
                break
            mask_for = {r["network"]: _mask_from_prefix(r["prefix"]) for r in pending}
            route_body = "\n".join(
                f"ip route {r['network']} {mask_for[r['network']]} {r['next_hop']}"
                for r in pending
            )
            route_stmts, route_final = js.js_configure_ios_statements(
                device, route_body
            ) if hasattr(js, "js_configure_ios_statements") else (
                None, js.js_configure_ios(device, route_body)
            )
            if route_stmts is not None:
                answer = self.send_expression(
                    route_final, statements=route_stmts, wait=8.0
                )
            else:
                answer = self.send_expression(route_final)
            if not (answer.is_known and str(answer.value).strip() == "true"):
                break  # the push itself failed; the missing routes stand
            # PT applies a pushed `ip route` asynchronously: settle before
            # the read-back or `show ip route` catches the table mid-install.
            time.sleep(_IOS_SETTLE_SECONDS)
            table = self.read_routing_table(device)
            if not (table.is_known and table.value is not None):
                # A dark (or empty) retry read proves nothing: the missing
                # entries stand. The old code read "could not read" as
                # "recovered", which is exactly how an UNKNOWN became a PASS.
                break
            observed = {
                (r["network"], r["prefix"], r.get("next_hop")) for r in table.value
            }
            pending = [
                r for r in route_expected
                if (r["network"], r["prefix"], r["next_hop"]) not in observed
            ]
            if not pending:
                break  # recovered
        if not pending:
            # Recovered. Replace the failing postcondition with a passing one
            # keyed to the retry path so verification sees a consistent PASS.
            return CheckResult(
                name="configured_routes_present",
                expected=json.dumps(route_expected, sort_keys=True),
                actual=Observable.known([], source="file-bridge-retry"),
                passed=True,
            ), []
        return post, list(pending)

    def configure_ios_bulk(
        self, specs: list[dict], operation_id_prefix: str = "batch-config"
    ) -> list[OperationReceipt]:
        """Configure many IOS devices in ONE push / ONE settle / ONE snapshot.

        Thirteen serial `configure_ios` calls cost thirteen pushes, thirteen
        2.5 s settles and thirteen FRESH topology reads (measured 2.7-66.4 s
        each on the idle 43-device canvas) - that was 237 s of the reference
        build's configure phase. The devices' configs are independent, so the
        transport carries them together; the SEMANTICS stay the serial ones:

        * an absent device leaves through `configure_ios`'s own precondition
          receipt and is never pushed (refuse by name, rule 9);
        * addresses are read back from ONE shared snapshot; a known snapshot
          without an address re-pushes THAT device only, bounded by
          `_IOS_CONFIG_ATTEMPTS` exactly like the serial loop, with the same
          settle before every read-back;
        * a batch (or a re-push) that never reached PT is UNKNOWN with the
          channel's detail (rule 7) - never an "address not observed" verdict
          that blames the device for our channel, and never a push the serial
          path would have bounded differently;
        * route and VLAN read-backs still run per device through the same
          `_finish_ios_receipt` the serial path finishes with (rule 8), so a
          batch cannot confirm what the serial path would only accept after
          reading it back.
        """
        receipts: list[OperationReceipt] = []
        if not specs:
            return receipts

        # Presence first: one (snapshot-cached) topology answer decides who may
        # be pushed. An absent or unreadable device exits exactly like the
        # serial path - its own precondition receipt, no push.
        pre_by: dict[str, list[CheckResult]] = {}
        pushable: list[dict] = []
        for spec in specs:
            device = spec["device"]
            exists = self.device_exists(device)
            pre_by[device] = [CheckResult.of("device_present", "True", exists)]
            if exists.is_known and exists.value:
                pushable.append(spec)
            else:
                receipts.append(self.configure_ios(
                    device,
                    spec["commands"],
                    operation_id=f"{operation_id_prefix}-{device}",
                ))
        if not pushable:
            return receipts

        expected_by = {
            spec["device"]: _expected_ios_addresses(spec["commands"])
            for spec in pushable
        }
        state: dict[str, dict] = {
            spec["device"]: {
                "answered": False,  # a KNOWN batch answer covered it
                "row": None,        # PT's own verdict for this device
                "dark": False,      # its last push got no answer at all
                "attempts": 0,
                "missing": dict(expected_by[spec["device"]]),
                "read_back": False,
            }
            for spec in pushable
        }
        channel_detail = ""
        snapshot_detail = "workspace snapshot not read"
        snapshot: Observable[list[dict]] = Observable[list[dict]].unknown(
            snapshot_detail
        )
        pending = list(pushable)
        push_round = 0

        while True:
            push_round += 1
            stmts: list[str] = ["var __mcpCfgResults=[];"]
            for spec in pending:
                device = spec["device"]
                call = js.js_configure_ios(device, "\n".join(spec["commands"]))
                stmts.append("try{")
                stmts.append(
                    f"  __mcpCfgResults.push({{device:{json.dumps(device)},"
                    f"ok:!!{call[:-1]}}});"
                )
                stmts.append("}catch(e){")
                stmts.append(
                    f"  __mcpCfgResults.push({{device:{json.dumps(device)},"
                    "ok:false,error:''+e});"
                )
                stmts.append("}")
            answer = self.send_expression(
                "JSON.stringify(__mcpCfgResults)", statements=stmts,
                wait=max(30.0, 4.0 * len(pending)),
            )
            for spec in pending:
                state[spec["device"]]["attempts"] += 1
            rows: dict[str, dict] = {}
            if answer.is_known:
                try:
                    loaded = json.loads(str(answer.value))
                    rows = {str(r.get("device")): r for r in loaded}
                except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
                    # An answer we cannot parse is not an answer: same bounded
                    # re-send as a dark channel, never a device verdict.
                    answer = Observable[str].unknown(
                        "unparseable configure batch answer: "
                        f"{str(answer.value)[:120]!r}"
                    )
            if not answer.is_known:
                # The channel class: bounded re-send like the serial path,
                # then UNKNOWN with the channel's reason (rule 7).
                channel_detail = answer.detail or answer.status.value
                if push_round < _IOS_CONFIG_ATTEMPTS:
                    time.sleep(_IOS_SETTLE_SECONDS)
                    continue
                for spec in pending:
                    state[spec["device"]]["dark"] = True
                break
            for spec in pending:
                st = state[spec["device"]]
                st["answered"] = True
                st["row"] = rows.get(spec["device"])
            need_readback = [
                spec for spec in pending
                if (rows.get(spec["device"]) or {}).get("ok")
                and expected_by[spec["device"]]
            ]
            if not need_readback:
                break  # nothing to verify: no settle, no read (serial parity)
            self._invalidate_snapshot()
            time.sleep(_IOS_SETTLE_SECONDS)
            snapshot = Observable[list[dict]].unknown(snapshot_detail)
            for read_attempt in range(1, _BULK_READ_RETRIES + 1):
                snapshot = self._live_devices()
                if snapshot.is_known:
                    break
                if read_attempt < _BULK_READ_RETRIES:
                    time.sleep(_BULK_READ_SETTLE_S)
            if not snapshot.is_known:
                snapshot_detail = snapshot.detail or snapshot.status.value
                break  # a dark read cannot be fixed by pushing again
            observed_by_name = {
                str(d.get("name")): d for d in (snapshot.value or [])
            }
            for spec in need_readback:
                st = state[spec["device"]]
                st["read_back"] = True
                ports = {
                    str(p.get("name")): p
                    for p in ((observed_by_name.get(spec["device"]) or {})
                              .get("ports") or [])
                }
                st["missing"] = {
                    iface: ip for iface, ip in expected_by[spec["device"]].items()
                    if str((ports.get(iface) or {}).get("ip") or "") != ip
                }
            still_missing = [
                spec for spec in need_readback
                if state[spec["device"]]["missing"]
            ]
            if not still_missing or push_round >= _IOS_CONFIG_ATTEMPTS:
                break
            pending = still_missing  # re-push only what did not take

        for spec in pushable:
            device = spec["device"]
            st = state[device]
            expected = expected_by[device]
            missing = st["missing"]
            read_back = st["read_back"]
            pushed_ok = bool(st["row"] and st["row"].get("ok"))
            attempts = st["attempts"]
            result = OperationResult.NOT_EXECUTED
            executed: dict | None = {"batched": True, "js_ok": pushed_ok}
            error: str | None = None
            if st["dark"] and not st["answered"]:
                # Never a verdict: the push never reached PT (rule 7).
                result = OperationResult.UNKNOWN
                error = f"the configure batch never reached PT: {channel_detail}"
                pushed_ok = False
                read_back = False
                missing = dict(expected)
                executed = None
            elif st["dark"]:
                # A re-push went dark after an earlier read: UNKNOWN with the
                # channel reason; the earlier read-back facts still travel and
                # the shared finisher names them exactly like the serial loop.
                result = OperationResult.UNKNOWN
                error = f"the configure batch never reached PT: {channel_detail}"
                executed = None
            elif st["row"] is None:
                # PT ran the batch and said nothing about this device.
                result = OperationResult.FAILED
                error = "PT answered the batch without a result for this device"
                pushed_ok = False
                read_back = False
                missing = dict(expected)
            elif not pushed_ok:
                result = OperationResult.FAILED
                error = st["row"].get("error") or "PT reported the configure failed"
                read_back = False
                missing = dict(expected)
            elif not expected:
                result = OperationResult.SUCCESS
                read_back = False
                missing = {}
            elif not read_back:
                # Push accepted but the snapshot never came back: UNKNOWN with
                # the channel reason - a FAILED verdict here would blame the
                # device for our read (rule 7/8).
                result = OperationResult.UNKNOWN
                error = (
                    "workspace snapshot unavailable after configure: "
                    f"{snapshot_detail}"
                )
            else:
                # Read back and known: the shared finisher runs the same
                # address verdict the serial path runs.
                result = OperationResult.SUCCESS
            receipts.append(self._finish_ios_receipt(
                device,
                spec["commands"],
                operation_id=f"{operation_id_prefix}-{device}",
                pre=pre_by[device],
                pushed_ok=pushed_ok,
                result=result,
                executed=executed,
                error=error,
                attempts=attempts,
                expected=expected,
                missing=missing,
                read_back=read_back,
                snapshot=snapshot if snapshot.is_known else None,
            ))
        return receipts

    def _missing_ios_addresses(
        self, device: str, expected: dict[str, str]
    ) -> dict[str, str]:
        """Which of the addresses we asked IOS for are not on the device."""
        obs = self.inspect_device(device)
        missing: dict[str, str] = {}
        for iface, ip in expected.items():
            port = obs.ports.get(iface)
            actual = port.ip.value if port is not None and port.ip.is_known else None
            if actual != ip:
                missing[iface] = ip
        return missing

    def configure_gateway_lan(
        self,
        device: str,
        ip: str,
        mask: str,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        """Set a Linksys-family gateway's LAN address through the port API (#10).

        Live probes on a Linksys-WRT300N: `configureIosDevice` answers `true`
        and changes nothing, while `port.setIpSubnetMask` on Vlan1 moves the
        address and the topology read-back shows it. Same receipt discipline
        as `configure_ios`: an address we pushed and cannot read back is a
        FAILED configuration, not a green op (#5/#17).
        """
        requested = {"device": device, "ip": ip, "mask": mask}
        exists = self.device_exists(device)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._precondition_receipt(
                "configure_gateway_lan",
                requested,
                operation_id,
                pre,
                "device missing or unknown",
            )
        answer = self.send_expression(js.js_set_port_ip(device, "Vlan1", ip, mask))
        self._invalidate_snapshot()  # the SVI now carries the new address
        result, executed, error = self._interpret(answer)
        read_back = False
        observed: dict[str, str] = {}
        missing: dict[str, str] = {"Vlan1": ip}
        if result is OperationResult.SUCCESS:
            obs = self.inspect_device(device)
            read_back = True
            port = obs.ports.get("Vlan1")
            actual_ip = port.ip.value if port is not None and port.ip.is_known else None
            actual_mask = (
                port.mask.value if port is not None and port.mask.is_known else None
            )
            observed = {
                "Vlan1": " ".join(x for x in (actual_ip, actual_mask) if x)
                or "<unobserved>"
            }
            # An unknown mask cannot contradict the push; a known wrong one can.
            mask_ok = actual_mask is None or actual_mask == mask
            if actual_ip == ip and mask_ok:
                missing = {}
        post = CheckResult(
            name="configured_addresses_present",
            expected=json.dumps({"Vlan1": ip}, sort_keys=True),
            actual=Observable.known(observed, source="file-bridge"),
            passed=(not missing) if read_back else None,
        )
        if missing and read_back and result is OperationResult.SUCCESS:
            result = OperationResult.FAILED
            error = (
                "configuration did not take effect - not observed: "
                + ", ".join(f"{iface}={want}" for iface, want in missing.items())
            )
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="configure_gateway_lan",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=[post],
            evidence={
                **(executed or {}),
                "postcondition": (
                    "gateway LAN address read back on Vlan1"
                    if read_back
                    else "no read-back performed"
                ),
            },
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def _verify_ios_vlans(
        self,
        device: str,
        expected: dict[str, dict],
        snapshot: Observable[list[dict]] | None = None,
    ) -> tuple[CheckResult | None, list[str]]:
        """Read back the VLAN state the pushed script asked for (#5).

        Mirrors `_expected_ios_addresses` on every point that matters:

        * a read we could not perform is `passed=None` - not a PASS, and never
          a failure for a configuration PT may well have applied;
        * only a table we DID read and that contradicts the push yields
          problems, and only those fail the receipt;
        * no round trip at all for a device that has no VLAN table
          (`_VLAN_CATEGORIES`), decided from the same topology read-back the
          address check already trusts.

        Returns ``(postcondition, contradictions)``; the contradictions are
        empty unless the read succeeded, so the caller can gate on them
        directly. `observe_vlans=False` (#20) skips everything, including the
        postcondition: a caller that asked for no VLAN observation gets the
        receipt it had before this check existed.

        `snapshot` is the batch caller's own shared topology read: the same
        read-back the address check trusts, already post-mutation, so
        `configure_ios_bulk` verifies VLANs without paying a second fresh
        read. None keeps the serial behaviour - a fresh read of its own.
        """
        if not self._observe_vlans:
            return None, []

        def unobserved(reason: str) -> tuple[CheckResult, list[str]]:
            return (
                CheckResult(
                    name="configured_vlans_present",
                    expected=json.dumps(expected, sort_keys=True),
                    actual=Observable[list].unknown(reason),
                    passed=None,
                ),
                [],
            )

        observed = self._live_devices()
        if not observed.is_known:
            return unobserved(
                f"topology read-back unavailable: {observed.detail or observed.status.value}"
            )
        match = next(
            (d for d in observed.value if str(d.get("name") or "") == device), None
        )
        if match is None:
            return unobserved("device absent from topology read-back")
        model = match.get("model")
        if not isinstance(model, str) or not model:
            return unobserved("model not reported by read-back")
        if category_of_model(model) not in _VLAN_CATEGORIES:
            # Router/firewall: there is no `show vlan brief` to read, so an
            # expectation here can be neither confirmed nor contradicted and
            # no postcondition is invented for it.
            return None, []
        table = self.read_vlans(device)
        if not table.is_known:
            return unobserved(
                f"vlan read-back unavailable: {table.detail or table.status.value}"
            )

        by_id: dict[int, dict] = {}
        membership: dict[str, list[int]] = {}
        for record in table.value or []:
            if not isinstance(record, dict):
                continue
            try:
                vid = int(record.get("id"))
            except (TypeError, ValueError):
                continue
            by_id[vid] = record
            for port_name in record.get("ports") or []:
                # `show vlan brief` abbreviates and pads port names; the pushed
                # config spells them out, so both sides go through the same
                # expansion before they are compared.
                port = js.expand_if_name(str(port_name).strip())
                membership.setdefault(port, []).append(vid)

        problems: list[str] = []
        for vid, name in sorted(expected["defined"].items()):
            record = by_id.get(vid)
            if record is None:
                problems.append(f"vlan {vid} not in read-back")
                continue
            if name and " " not in name and str(record.get("name") or "") != name:
                problems.append(
                    f"vlan {vid} read back as {record.get('name')!r}, pushed {name!r}"
                )
        for iface, vid in sorted(expected["access"].items()):
            port = js.expand_if_name(iface)
            record = by_id.get(vid)
            if record is None:
                problems.append(f"{port}: access vlan {vid} not in read-back")
                continue
            members = {
                js.expand_if_name(str(p).strip()) for p in record.get("ports") or []
            }
            if port not in members:
                problems.append(f"{port} is not a member of vlan {vid}")
        for iface, allowed in sorted(expected["trunk"].items()):
            port = js.expand_if_name(iface)
            member_of = sorted(set(membership.get(port, [])))
            if not member_of:
                # F-4, proven against PT's raw output: `show vlan brief` OMITS
                # trunk ports from every membership list (S1.1 capture: the
                # three trunked ports absent, unused ports sitting in VLAN 1).
                # Absence under a trunk push is the read-back evidence, not a
                # contradiction — demanding membership made this postcondition
                # unsatisfiable and failed every VLAN op that carried a trunk.
                # The allowed list itself is not observable here (S3.B1).
                continue
            if allowed:
                absent = [v for v in allowed if v not in member_of]
                if absent:
                    wanted = ",".join(str(v) for v in absent)
                    problems.append(
                        f"{port} does not carry vlan(s) {wanted} in read-back"
                    )
            elif len(member_of) < 2:
                # `_fill_vlan_state` derives trunk from a port listed under
                # more than one VLAN, so one (or zero) memberships is read-back
                # evidence against the trunk. Only a push with NO allowed list
                # lands here: the compiler names the allowed set whenever the
                # trunk carries something, and a named set is checked above
                # against membership instead of a bare count - a trunk allowed
                # exactly one VLAN is indistinguishable from an access port in
                # `show vlan brief`, so counting it would invent a failure.
                seen = ", ".join(str(v) for v in member_of) or "none"
                problems.append(f"{port} is not a trunk in read-back (vlan(s): {seen})")
        post = CheckResult(
            name="configured_vlans_present",
            expected=json.dumps(expected, sort_keys=True),
            actual=Observable.known(problems, source=f"show vlan brief:{device}"),
            passed=not problems,
        )
        return post, problems

    def configure_host(
        self,
        device: str,
        *,
        dhcp: bool,
        ip: str | None = None,
        mask: str | None = None,
        gateway: str | None = None,
        dns: str | None = None,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        requested = {
            "device": device, "dhcp": dhcp, "ip": ip, "mask": mask,
            "gateway": gateway, "dns": dns,
        }
        exists = self.device_exists(device)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._precondition_receipt(
                "configure_host", requested, operation_id, pre, "device missing or unknown"
            )
        answer = self.send_expression(
            js.js_configure_pc_ip(device, dhcp, ip or "", mask or "", gateway or "", dns or "")
        )
        self._invalidate_snapshot()  # the host now holds an address
        result, executed, error = self._interpret(answer)
        postconditions: list[CheckResult] = []
        if result is OperationResult.SUCCESS and not dhcp and ip:
            # read-back: the configured IP must be observable on the device.
            # The check compares str(actual) to `expected`, so the Observable
            # must carry the IP (not a bare True) or the receipt is permanently
            # unconfirmed even when the read-back succeeded.
            obs = self.inspect_device(device)
            port = next(
                (p for p in obs.ports.values() if p.ip.is_known and p.ip.value == ip),
                None,
            )
            if port is not None:
                check: Observable = Observable.known(
                    ip, source=f"inspect_device:{device}"
                )
            else:
                check = Observable[bool].unknown(
                    f"configured ip {ip!r} not observed on {device!r}"
                )
            postconditions.append(CheckResult.of("ip_observed", ip, check))
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="configure_host",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=postconditions,
            evidence={
                **(executed or {}),
                **(
                    {"postcondition": "ip observed on device read-back"}
                    if postconditions
                    else {"postcondition": "dhcp/no-ip: no read-back check"}
                ),
            },
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def configure_hosts_bulk(
        self, specs: list[dict], operation_id_prefix: str = "batch-host"
    ) -> list[OperationReceipt]:
        """Set many host addresses in ONE round-trip.

        Addressing twenty PCs one round-trip each is pure transport cost. The
        calls are independent — each configures a different device — so they
        travel together. The per-device read-back is preserved, because an
        address PT accepted but did not apply is exactly the failure worth
        catching.
        """
        receipts: list[OperationReceipt] = []
        if not specs:
            return receipts

        stmts: list[str] = ["var __mcpHostResults=[];"]
        order: list[str] = []
        for spec in specs:
            device = spec["device"]
            order.append(device)
            call = js.js_configure_pc_ip(
                device,
                bool(spec.get("dhcp")),
                spec.get("ip") or "", spec.get("mask") or "",
                spec.get("gateway") or "", spec.get("dns") or "",
            )
            stmts.append("try{")
            stmts.append(
                f"  __mcpHostResults.push({{device:{json.dumps(device)},"
                f"ok:!!{call[:-1]}}});"
            )
            stmts.append("}catch(e){")
            stmts.append(
                f"  __mcpHostResults.push({{device:{json.dumps(device)},"
                "ok:false,error:''+e});"
            )
            stmts.append("}")

        answer = self.send_expression(
            "JSON.stringify(__mcpHostResults)", statements=stmts,
            wait=max(20.0, 2.0 * len(specs)),
        )
        self._invalidate_snapshot()  # the hosts now hold addresses
        per_device: dict[str, dict] = {}
        if answer.is_known:
            try:
                for row in json.loads(str(answer.value)):
                    per_device[str(row.get("device"))] = row
            except (json.JSONDecodeError, ValueError):
                per_device = {}

        # One workspace read verifies every address at once. A per-host
        # inspect_device is a round-trip each, which is exactly what the batch
        # removed from the write path. The read gets a bounded settle like
        # every other read-back: an unknown snapshot must neither fail the
        # hosts (PT reported ok:true) nor become their verdict — it stays
        # UNKNOWN with the channel detail, per rule 7.
        snapshot: Observable[list[dict]] = Observable[list[dict]].unknown(
            "workspace snapshot not read"
        )
        for read_attempt in range(1, _BULK_READ_RETRIES + 1):
            snapshot = self._live_devices()
            if snapshot.is_known:
                break
            if read_attempt < _BULK_READ_RETRIES:
                time.sleep(_BULK_READ_SETTLE_S)
        observed: dict[str, set[str]] = {}
        if snapshot.is_known:
            for d in (snapshot.value or []):
                name = str(d.get("name"))
                observed[name] = {
                    str(p.get("ip") or "") for p in (d.get("ports") or [])
                }

        for spec in specs:
            device = spec["device"]
            row = per_device.get(device)
            reported = row is not None
            row = row or {}
            ok = bool(row.get("ok"))
            postconditions: list[CheckResult] = []
            ip = spec.get("ip")
            needs_readback = bool(ok and not spec.get("dhcp") and ip)
            seen = False
            if needs_readback:
                seen = ip in observed.get(device, set())
                postconditions.append(CheckResult.of(
                    "ip_observed", ip,
                    Observable.known(ip, source=f"inspect_device:{device}")
                    if seen else Observable[bool].unknown(
                        f"configured ip {ip!r} not observed on {device!r}"
                    ),
                ))
            error: str | None
            if not answer.is_known:
                # The batch never reached PT (or PT never answered the round
                # trip): nothing was pushed and nothing was observed. That is
                # UNKNOWN with the transport reason — never a FAILED verdict
                # that blames the device for our channel (rule 7/8:
                # build-81df879ad555's 22 receipts all claimed "address not
                # observed" for a batch that never left the machine).
                result = OperationResult.UNKNOWN
                error = (
                    "the host batch never reached PT: "
                    f"{answer.detail or answer.status.value}"
                )
            elif not reported:
                result = OperationResult.FAILED
                error = "PT answered the batch without a result for this device"
            elif not ok:
                result = OperationResult.FAILED
                error = row.get("error") or "PT reported the configure failed"
            elif needs_readback and not snapshot.is_known:
                result = OperationResult.UNKNOWN
                error = (
                    "workspace snapshot unavailable after configure: "
                    f"{snapshot.detail or snapshot.status.value}"
                )
            elif needs_readback and not seen:
                result = OperationResult.FAILED
                error = row.get("error") or "address not observed after configure"
            else:
                result = OperationResult.SUCCESS
                error = None
            receipts.append(OperationReceipt(
                operation_id=f"{operation_id_prefix}-{device}",
                operation="configure_host",
                requested=spec,
                executed={"batched": True, "js_ok": ok},
                result=result,
                postconditions=postconditions,
                evidence={"batch": True, "js_error": row.get("error", "")},
                started_at=utc_now_iso(),
                finished_at=utc_now_iso(),
                error=error,
            ))
        return receipts

    def save_project(
        self, filename: str, operation_id: str | None = None, directory: str = ""
    ) -> OperationReceipt:
        requested = {"filename": filename, "directory": directory}
        statements, final = js.js_save_project_statements(directory, filename)
        answer = self.send_expression(final, statements=statements, wait=25.0)
        result, executed, error = self._interpret(answer)
        # 'ERR:...' is PT reporting the save failed — map it honestly.
        if result is OperationResult.SUCCESS and str(answer.value or "").startswith("ERR:"):
            result, error = OperationResult.FAILED, str(answer.value)
        post: list[CheckResult] = []
        if result is OperationResult.SUCCESS and str(answer.value or "").startswith("OK:"):
            # The save JS itself verified file existence + size in-band; that
            # IS the read-back, so the receipt can legitimately confirm.
            _, _, size = str(answer.value)[3:].rpartition("|")
            post = [
                CheckResult(
                    name="file_exists_on_disk",
                    expected="saved file exists with a size",
                    actual=Observable.known(f"size={size}", source=answer.source),
                    passed=True,
                )
            ]
            executed = {"saved_path": str(answer.value)[3:].rpartition("|")[0], "size": size}
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="save_project",
            requested=requested,
            executed=executed,
            result=result,
            postconditions=post,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def open_project(self, path: str, operation_id: str | None = None) -> OperationReceipt:
        # Deliberately not wired in Phase 1 rather than guessed: the verified
        # open/read-back JS lands with the Phase-2 save/reopen proof work.
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="open_project",
            requested={"path": path},
            result=OperationResult.NOT_EXECUTED,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error="open_project is not wired in Phase 1 (no verified read-back yet)",
        )

    # -- verification -------------------------------------------------------

    def _resolve_destination_ip(self, destination: str) -> Observable[str]:
        """The console ping needs an IP; resolve it from observed state."""
        devices = self._live_devices()
        for _ in range(_RESOLVE_RETRIES):
            if devices.is_known:
                break
            # A heartbeat hiccup at resolve time made whole flows UNKNOWN in
            # build-68648f705ccb without ever looking at the topology; give
            # the channel a bounded chance to answer before giving up.
            time.sleep(_RESOLVE_RETRY_S)
            devices = self._live_devices()
        if not devices.is_known:
            return Observable[str].unknown(
                f"cannot resolve {destination!r} address: "
                f"{devices.detail or devices.status.value}"
            )
        match = next(
            (d for d in devices.value if str(d.get("name") or "") == destination), None
        )
        if match is None:
            return Observable[str].unknown(
                f"destination {destination!r} not present in the observed workspace"
            )
        ips = {
            str(p.get("ip"))
            for p in match.get("ports") or []
            if str(p.get("ip") or "").strip()
            and str(p.get("ip")) != "0.0.0.0"  # placeholder (e.g. Bluetooth NIC)
        }
        if not ips:
            return Observable[str].unknown(
                f"destination {destination!r} has no observable IP to ping"
            )
        if len(ips) > 1:
            return Observable[str].unknown(
                f"destination {destination!r} has several IPs ({sorted(ips)}); "
                "the flow test does not guess which one is meant"
            )
        return Observable.known(ips.pop(), source=devices.source)

    def run_flow_test(
        self,
        source: str,
        destination: str,
        *,
        transport: str = "icmp",
        port: int | None = None,
        timeout: float | None = None,
        destination_ip: str | None = None,
    ) -> FlowResult:
        """A REAL console ping: arm on the source, poll for the new stat block.

        Verdicts come from the upstream ping-stat classifier: 100% replies is
        REACHABLE, some loss is PARTIAL (never reported as healthy), no replies
        is BLOCKED. No console, no address, no channel, or a quiet console is
        UNKNOWN — a flow test that could not run is never PASS.

        `timeout` is the quiet window for EVERY attempt when given; when it is
        omitted the adaptive `_FLOW_ATTEMPT_TIMEOUTS` schedule applies
        (short first windows, the old 25 s patience kept for the last).

        Transport-honest (P1-5): the only data-plane probe verified against PT
        is ICMP echo. A criterion declaring tcp/udp cannot be proved by an echo
        reply, so it comes back UNKNOWN with the reason instead of inheriting
        an ICMP verdict — the caller decides, never the adapter.
        """
        transport_norm = (transport or "icmp").strip().lower()
        if transport_norm not in ("icmp", "", "ping"):
            return FlowResult(
                source=source,
                destination=destination,
                transport=transport_norm,
                port=port,
                outcome=FlowOutcome.UNKNOWN,
                detail=(
                    f"transport {transport_norm!r} has no verified PT probe: ICMP "
                    "echo is real, but proving a TCP/UDP criterion with echo "
                    "would be a false positive. Outcome stays UNKNOWN."
                ),
                evidence={"transport": transport_norm, "port": port, "icmp_only": True},
            )
        # A dual-homed device (a router with a WAN and a LAN address, a
        # firewall with two sides) has no single "the" address. Guessing would
        # probe a random interface, so the caller names the one it means. This
        # is a deliberate capability, not a fallback: without it every
        # WAN-to-LAN flow is UNKNOWN and silently undiagnosable.
        if destination_ip:
            target = Observable[str].known(
                destination_ip, source="caller-specified destination"
            )
        else:
            target = self._resolve_destination_ip(destination)
        if not target.is_known:
            return FlowResult(
                source=source,
                destination=destination,
                outcome=FlowOutcome.UNKNOWN,
                detail=target.detail or target.status.value,
            )
        # PT (like real IOS) drops the first pings across a multi-hop path
        # while ARP resolves hop by hop: a healthy transit network answers
        # blocked -> partial -> reachable on successive attempts. A single
        # shot would misread that convergence as a broken path, so the ping
        # is retried and the converged verdict is the one reported.
        attempts: list[FlowResult] = []
        time.sleep(_FLOW_PRE_SETTLE_S)
        # A PARTIAL on the last budgeted attempt is convergence in progress
        # (some echoes returned), not a verdict: give it exactly one
        # confirmation attempt. BLOCKED-only chains keep the three-attempt
        # budget, so a genuinely blocked flow does not pay extra wall time.
        max_attempts = _FLOW_MAX_ATTEMPTS
        attempt_no = 0
        while attempt_no < max_attempts:
            attempt_timeout = (
                timeout
                if timeout is not None
                else _FLOW_ATTEMPT_TIMEOUTS[
                    min(attempt_no, len(_FLOW_ATTEMPT_TIMEOUTS) - 1)
                ]
            )
            result = self._single_ping_flow(source, destination, target, attempt_timeout)
            attempts.append(result)
            attempt_no += 1
            if result.outcome is FlowOutcome.REACHABLE:
                break
            if not _flow_is_retryable(result):
                # Arm/console failures cannot improve with a retry, unlike a
                # BLOCKED/PARTIAL loss or a ping still in flight.
                break
            if (
                result.outcome is FlowOutcome.PARTIAL
                and attempt_no >= max_attempts
                and max_attempts == _FLOW_MAX_ATTEMPTS
            ):
                max_attempts += 1
            time.sleep(_FLOW_CONVERGE_SETTLE_S)
        best = best_flow_of(attempts)
        if len(attempts) > 1:
            # Report the converged verdict but keep every attempt as evidence.
            converged = best is not attempts[-1]
            detail = best.detail or ""
            if converged:
                detail += " (after ARP convergence)"
            best = FlowResult(
                source=best.source,
                destination=best.destination,
                outcome=best.outcome,
                transport=best.transport,
                port=best.port,
                detail=detail,
                evidence={**best.evidence, "attempts": [
                    {"attempt": i + 1, "outcome": a.outcome.value,
                     "detail": a.detail}
                    for i, a in enumerate(attempts)
                ]},
            )
        return best

    def _single_ping_flow(
        self, source: str, destination: str, target: Observable, timeout: float
    ) -> FlowResult:
        """One arm/poll console ping exchange; the caller retries on loss."""
        armed = self.send_expression(
            js.js_console_ping_arm_expr(source, str(target.value)), wait=10.0
        )
        if not armed.is_known:
            return FlowResult(
                source=source,
                destination=destination,
                outcome=FlowOutcome.UNKNOWN,
                detail=f"ping could not be armed: {armed.detail or armed.status.value}",
            )
        arm_value = str(armed.value)
        if arm_value.startswith("PT_ERROR"):
            return FlowResult(
                source=source,
                destination=destination,
                outcome=FlowOutcome.UNKNOWN,
                detail=f"PT reported while arming: {arm_value}",
            )
        if not arm_value.startswith("BASE:"):
            return FlowResult(
                source=source,
                destination=destination,
                outcome=FlowOutcome.UNKNOWN,
                detail=f"ping arm answered: {arm_value!r}",
            )
        base = int(arm_value[5:])
        poll = js.js_console_poll_expr(source, base)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.6)
            answer = self.send_expression(poll, wait=5.0)
            if not answer.is_known:
                continue  # transient channel hiccup: keep polling until deadline
            value = str(answer.value)
            if value.startswith("DONE:"):
                stat = value[5:]
                verdict = classify_ping(stat)
                outcome = {
                    "ok": FlowOutcome.REACHABLE,
                    "partial": FlowOutcome.PARTIAL,
                    "none": FlowOutcome.BLOCKED,
                }[verdict]
                return FlowResult(
                    source=source,
                    destination=destination,
                    outcome=outcome,
                    detail=f"ping {source}->{str(target.value)}: {verdict}",
                    evidence={"ping_stat": stat},
                )
            if value.startswith("ERR:"):
                return FlowResult(
                    source=source,
                    destination=destination,
                    outcome=FlowOutcome.UNKNOWN,
                    detail=f"console unavailable: {value}",
                )
            # 'WAIT' or anything else: keep polling
        return FlowResult(
            source=source,
            destination=destination,
            outcome=FlowOutcome.UNKNOWN,
            detail=(
                f"no new ping statistics appeared on {source!r} within {timeout:.0f}s"
            ),
        )

    # -- parallel data-plane flows --------------------------------------------

    #: Set HAVYN_BULK_FLOWS_DEBUG=1 to trace rounds on stderr.
    _BULK_DEBUG = os.environ.get("HAVYN_BULK_FLOWS_DEBUG", "") == "1"

    #: Sleep between the SHARED polls of an armed round. The whole round is
    #: polled in one Script Engine round trip, so this only paces those shared
    #: calls; it is deliberately well below one ping window.
    _BULK_POLL_INTERVAL_S = 0.5
    #: Per-attempt quiet window for the batched path, mirroring the serial
    #: `_FLOW_ATTEMPT_TIMEOUTS`. Sized from a live measurement on
    #: build-ef996a15797f's canvas (2026-10-16): a single clean ping on a freshly
    #: built cross-fabric path printed its `Packets: Sent` stat block after
    #: 14.3 s (pc16->srv1), 24.3 s (pc1->pc7) and 16.1 s (pc9->srvisp) — the
    #: first echo pays ARP resolution and, on a lossy cold path, per-echo
    #: timeouts. The old first window (9 s) was sized only to out-last a DEAD
    #: path's 0%-loss block (~8 s), so it timed out on every live cold ping,
    #: forced a re-arm (a fresh ping that discarded the first's ARP convergence)
    #: and left 5 flows in build-ef996a15797f with no stat block in 9+16+25 s.
    #: The first window now covers the measured worst cold ping (24.3 s) plus
    #: margin so a cold ping finishes inside ONE attempt; after it, the second
    #: ping is ARP-warm and fast (15 s is ample), and the last keeps 25 s
    #: patience for a slow console.
    _BULK_ATTEMPT_TIMEOUTS: tuple[float, ...] = (30.0, 15.0, 25.0)

    def run_flow_tests_bulk(
        self, specs: list[dict[str, object]]
    ) -> list[FlowResult] | None:
        """Run every flow concurrently: one armed ping per source device.

        Flows are grouped by ``source``: one console holds one in-flight ping
        (the stat-block counter is per console), so a source's own flows run as
        sequential rounds while DIFFERENT sources run at the same time. Arming
        and polling each cost ONE Script Engine round trip per round, so twelve
        flows cost roughly one ping window instead of twelve.

        Verdicts are identical to ``run_flow_test``: the same classifier, the
        same transport honesty (non-ICMP is UNKNOWN, never an echo verdict),
        the same bounded ARP-convergence retries with the converged result
        reported and every attempt kept as evidence.
        """
        if not specs:
            return []
        from collections import deque

        #: source -> queue of (slot, spec, target_ip, attempts). The attempts
        #: list travels WITH the flow: a re-armed flow must keep its history so
        #: the retry budget advances, otherwise a retryable flow would loop
        #: forever on a fresh counter.
        pending: dict[str, deque] = {}
        #: source -> in-flight record
        armed: dict[str, dict] = {}
        results: list[FlowResult | None] = [None] * len(specs)

        for slot, spec in enumerate(specs):
            transport = str(spec.get("transport") or "icmp").strip().lower()
            src, dest = str(spec["source"]), str(spec["destination"])
            port = spec.get("port")
            if transport not in ("icmp", "", "ping"):
                results[slot] = FlowResult(
                    source=src, destination=dest, transport=transport, port=port,
                    outcome=FlowOutcome.UNKNOWN,
                    detail=(
                        f"transport {transport!r} has no verified PT probe: ICMP "
                        "echo is real, but proving a TCP/UDP criterion with echo "
                        "would be a false positive. Outcome stays UNKNOWN."
                    ),
                    evidence={"transport": transport, "port": port, "icmp_only": True},
                )
                continue
            dest_ip = spec.get("destination_ip")
            if dest_ip:
                target: Observable = Observable[str].known(
                    str(dest_ip), source="caller-specified destination"
                )
            else:
                target = self._resolve_destination_ip(dest)
            if not target.is_known:
                results[slot] = FlowResult(
                    source=src, destination=dest, transport=transport, port=port,
                    outcome=FlowOutcome.UNKNOWN,
                    detail=target.detail or target.status.value,
                )
                continue
            pending.setdefault(src, deque()).append(
                (slot, spec, str(target.value), [])
            )

        while pending or armed:
            if self._BULK_DEBUG:
                print(
                    f"[bulk] round: armed={sorted(armed)} "
                    f"pending={ {k: len(v) for k, v in pending.items()} }",
                    file=sys.stderr, flush=True,
                )
            self._arm_pending(pending, armed, results)
            if not armed:
                break
            deadline = time.time() + max(
                self._BULK_ATTEMPT_TIMEOUTS[
                    min(len(r["attempts"]), len(self._BULK_ATTEMPT_TIMEOUTS) - 1)
                ]
                for r in armed.values()
            )
            if self._BULK_DEBUG:
                print(
                    f"[bulk]   deadline in {deadline - time.time():.0f}s, "
                    f"attempts={ {k: len(r['attempts']) for k, r in armed.items()} }",
                    file=sys.stderr, flush=True,
                )
            self._poll_armed(armed, results, deadline, pending)
            # A source whose flow resolved keeps its remaining queue; a flow
            # that resolved retryable and still has budget is re-queued itself.
            for src in [s for s in list(pending) if s not in armed and not pending[s]]:
                del pending[src]

        # Every spec must land a result; an empty queue with no verdict is a
        # logic gap, never a silent PASS.
        for slot, result in enumerate(results):
            if result is None:
                results[slot] = FlowResult(
                    source=str(specs[slot]["source"]),
                    destination=str(specs[slot]["destination"]),
                    transport=str(specs[slot].get("transport") or "icmp"),
                    port=specs[slot].get("port"),
                    outcome=FlowOutcome.UNKNOWN,
                    detail="no ping attempt produced an answer",
                )
        return results

    def _arm_pending(self, pending, armed, results) -> None:
        """Arm the next pending flow on every idle source in one round trip."""
        jobs: list[tuple[str, str]] = []
        for src in sorted(pending):
            if src in armed or not pending[src]:
                continue
            slot, spec, target, attempts = pending[src].popleft()
            jobs.append((src, target))
            armed[src] = {
                "slot": slot, "spec": spec, "target": target,
                "base": None, "attempts": attempts,
            }
        if not jobs:
            return
        answer = self.send_expression(js.js_console_ping_arm_multi_expr(jobs), wait=12.0)
        bases: dict[int, int] = {}
        errors: dict[int, str] = {}
        if answer.is_known:
            try:
                for entry in json.loads(str(answer.value)):
                    idx = int(entry["i"])
                    if "err" in entry:
                        errors[idx] = str(entry["err"])
                    else:
                        bases[idx] = int(entry["base"])
            except (ValueError, TypeError, KeyError):
                pass  # a malformed answer leaves bases unset: the poll retries
        # The JS keys every entry by the job's index WITHIN THIS BATCH, never
        # by the flow's slot in the whole spec list (js_commands: "for(var i=0;
        # i<jobs.length;i++)", docstring {"i": <job index>}). The two only match
        # while the batch is as large as the spec. Keying by slot left any flow
        # whose slot fell outside the batch with base=None, so _poll_armed
        # dropped it from `live` and it was NEVER polled - a guaranteed false
        # "no new ping statistics" for a path that actually answers (measured in
        # build-1d2dd0140a1e: round 2 armed 9, polled 6, and A-010/A-011/A-024
        # reported no stats while a live probe pinged them in 9.8-18.4 s).
        for job_index, (src, _target) in enumerate(jobs):
            record = armed[src]
            if job_index in errors:
                record["attempts"].append(
                    FlowResult(
                        source=src,
                        destination=str(record["spec"]["destination"]),
                        transport="icmp",
                        port=record["spec"].get("port"),
                        outcome=FlowOutcome.UNKNOWN,
                        detail=f"ping could not be armed: {errors[job_index]}",
                    )
                )
                self._settle_flow(src, record, results, pending, terminal=True)
                # An arm failure is terminal: leave the armed table so the
                # record is not re-polled, and take it out of the pending
                # queue for good.
                armed.pop(src, None)
                continue
            record["base"] = bases.get(job_index)

    def _poll_armed(self, armed, results, deadline: float, pending) -> None:
        """Poll every armed flow in shared round trips until the deadline."""
        while armed:
            time.sleep(self._BULK_POLL_INTERVAL_S)
            live = [
                (src, rec["base"])
                for src, rec in armed.items()
                if rec["base"] is not None
            ]
            timed_out = time.time() >= deadline
            if not live:
                if timed_out:
                    for src, rec in list(armed.items()):
                        rec["attempts"].append(self._quiet_flow_result(rec, deadline))
                        self._settle_flow(src, rec, results, pending)
                    armed.clear()
                    return
                continue
            answer = self.send_expression(js.js_console_poll_multi_expr(live), wait=8.0)
            resolved: list[str] = []
            if answer.is_known:
                try:
                    polled = json.loads(str(answer.value))
                except (ValueError, TypeError):
                    polled = []
                for entry in polled:
                    src = live[int(entry["i"])][0]
                    rec = armed.get(src)
                    if rec is None:
                        continue
                    value = str(entry.get("r", "WAIT"))
                    if value.startswith("DONE:"):
                        rec["attempts"].append(self._flow_result_from_stat(rec, value[5:]))
                        resolved.append(src)
                    elif value.startswith("ERR:"):
                        rec["attempts"].append(
                            FlowResult(
                                source=src,
                                destination=str(rec["spec"]["destination"]),
                                transport="icmp",
                                port=rec["spec"].get("port"),
                                outcome=FlowOutcome.UNKNOWN,
                                detail=f"console unavailable: {value}",
                            )
                        )
                        resolved.append(src)
            for src in resolved:
                self._settle_flow(src, armed[src], results, pending)
                del armed[src]
            if timed_out:
                for src, rec in list(armed.items()):
                    rec["attempts"].append(self._quiet_flow_result(rec, deadline))
                    self._settle_flow(src, rec, results, pending)
                armed.clear()
                return

    def _quiet_flow_result(self, record: dict, deadline: float) -> FlowResult:
        timeout = max(
            self._BULK_ATTEMPT_TIMEOUTS[
                min(len(record["attempts"]), len(self._BULK_ATTEMPT_TIMEOUTS) - 1)
            ],
            1.0,
        )
        return FlowResult(
            source=str(record["spec"]["source"]),
            destination=str(record["spec"]["destination"]),
            transport="icmp",
            port=record["spec"].get("port"),
            outcome=FlowOutcome.UNKNOWN,
            detail=(
                f"no new ping statistics appeared on "
                f"{record['spec']['source']!r} within {timeout:.0f}s"
            ),
        )

    def _settle_flow(
        self, src: str, record: dict, results, pending, *, terminal: bool = False
    ) -> None:
        """Finish a flow, or re-queue it when the outcome is still retryable.

        A BLOCKED/PARTIAL/quiet attempt on a freshly built path is ARP
        convergence, not a verdict: while the flow has attempts left it goes
        back to its source's queue and is re-armed next round. Arm/console
        failures are terminal (``terminal=True``) because a retry changes
        nothing about them.
        """
        last = record["attempts"][-1]
        budget_left = len(record["attempts"]) < _FLOW_MAX_ATTEMPTS
        if not terminal and budget_left and _flow_is_retryable(last):
            queue = pending[src] if pending is not None and src in pending else None
            if queue is not None:
                # The attempts list travels back into the queue so the next
                # arm inherits the history and the retry budget advances.
                queue.append(
                    (record["slot"], record["spec"], record["target"],
                     record["attempts"])
                )
                return
        self._finish_flow(record, results)

    def _flow_result_from_stat(self, record: dict, stat: str) -> FlowResult:
        verdict = classify_ping(stat)
        outcome = {
            "ok": FlowOutcome.REACHABLE,
            "partial": FlowOutcome.PARTIAL,
            "none": FlowOutcome.BLOCKED,
        }[verdict]
        return FlowResult(
            source=str(record["spec"]["source"]),
            destination=str(record["spec"]["destination"]),
            transport="icmp",
            port=record["spec"].get("port"),
            outcome=outcome,
            detail=f"ping {record['spec']['source']}->{record['target']}: {verdict}",
            evidence={"ping_stat": stat},
        )

    def _finish_flow(self, record: dict, results: list) -> None:
        """Fold the attempt list into the converged verdict at its slot."""
        attempts = record["attempts"]
        slot = int(record["slot"])
        best = best_flow_of(attempts)
        if len(attempts) > 1:
            detail = best.detail or ""
            if best is not attempts[-1]:
                detail += " (after ARP convergence)"
            best = FlowResult(
                source=best.source,
                destination=best.destination,
                outcome=best.outcome,
                transport=best.transport,
                port=best.port,
                detail=detail,
                evidence={**best.evidence, "attempts": [
                    {"attempt": i + 1, "outcome": a.outcome.value, "detail": a.detail}
                    for i, a in enumerate(attempts)
                ]},
            )
        results[slot] = best

    # -- control-plane read-back (console) -----------------------------------

    def _console_show(
        self,
        device: str,
        command: str,
        *,
        timeout: float = 12.0,
        collect_expr: Callable[[], str] | None = None,
    ) -> Observable[str]:
        """Run one `show ...` command on the device console and return the tail.

        Verified live: PT exposes the routing table / VLAN database ONLY
        through getCommandLine(), so this is an arm/poll exchange like the
        ping path. UNKNOWN on any failure — the raw console evidence is kept.
        `collect_expr` lets a caller supply a completion rule other than
        "output grew past the base" (the serial controller read-back needs to
        survive a trimmed buffer).
        """
        statements, final = js.js_console_command_arm_statements(device, command)
        armed = self.send_expression(final, statements=statements, wait=10.0)
        if not armed.is_known:
            return Observable[str].unknown(
                f"console arm failed: {armed.detail or armed.status.value}"
            )
        value = str(armed.value)
        if value.startswith(("PT_ERROR", "ERR:")):
            return Observable[str].unknown(f"console unavailable: {value}")
        if not value.startswith("BASE:"):
            return Observable[str].unknown(f"console arm answered: {value!r}")
        base = int(value[5:])
        collect = collect_expr() if collect_expr else js.js_console_collect_expr(device, base)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            answer = self.send_expression(collect, wait=5.0)
            if not answer.is_known:
                continue
            text = str(answer.value)
            if text.startswith("DONE:"):
                return Observable.known(text[5:], source=f"console:{device}")
            if text.startswith(("ERR:", "PT_ERROR")):
                return Observable[str].unknown(f"console error: {text}")
        return Observable[str].unknown(
            f"{command!r} produced no prompt on {device!r} within {timeout:.0f}s"
        )

    # -- console read-back: one honest, pager-safe `show` -------------------

    #: Live-measured PT limits this read is built around (see
    #: `.zwork/logs/probe_*.out` and `js_console_show_expr`'s docstring):
    #: the console buffer is capped, long output pages on `--More--`, a fresh
    #: router parks at the initial-config dialog, and a burst of
    #: `enterCommand` calls merges into one garbled input line.
    _CONSOLE_POLL_S = 0.5
    _CONSOLE_SETTLE_S = 12.0
    _CONSOLE_READ_S = 25.0
    #: A booting device answers with ROMMON/bootloader text instead of a table.
    #: Parsing that yields an empty table, which reads as "no routes" - the R3
    #: lie. Recognised instead and reported UNKNOWN with the text as evidence.
    _CONSOLE_BOOT_MARKERS = (
        "System Bootstrap",
        "Cisco IOS Software",
        "Evaluating BIOS Options",
        "Launching BootLoader",
        "initial configuration dialog",
        "Press RETURN to get started",
    )

    def _console_step(
        self, device: str, command: str, action: str = "state"
    ) -> Observable[_ConsoleStep]:
        """One round trip against a PT console. Never guesses a signature."""
        answer = self.send_expression(
            js.js_console_show_expr(device, command, action), wait=8.0
        )
        if not answer.is_known:
            return Observable[_ConsoleStep].unknown(
                answer.detail or answer.status.value
            )
        text = str(answer.value)
        if text.startswith("E:"):
            return Observable[_ConsoleStep].unknown(text[2:])
        if not text.startswith("S:"):
            return Observable[_ConsoleStep].unknown(f"unexpected console answer {text[:120]!r}")
        parts = text[2:].split("|", 6)
        if len(parts) < 7:
            return Observable[_ConsoleStep].unknown(f"short console answer {text[:120]!r}")
        try:
            echo_count, pager, at_prompt, settled, typed = (
                int(part) for part in parts[:5]
            )
        except ValueError:
            return Observable[_ConsoleStep].unknown(f"malformed console answer {text[:120]!r}")
        try:
            prompt = json.loads(parts[5])
        except (TypeError, ValueError):
            prompt = parts[5]
        return Observable.known(
            _ConsoleStep(
                echo_count=echo_count,
                pager_at_end=bool(pager),
                at_prompt=bool(at_prompt),
                settled=bool(settled),
                typed=bool(typed),
                prompt=str(prompt),
                tail=parts[6],
            ),
            source=f"console:{device}",
        )

    def _console_settle(self, device: str, command: str) -> Observable[_ConsoleStep]:
        """Get the console to a usable CLI prompt before typing anything.

        A freshly added PT router parks at `initial configuration dialog?
        [yes/no]:` and a console left mid-page sits on `--More--`; typing into
        either produces garbage (`Router#nable` / `Translating "nable"...`,
        measured live). One command at a time, and the prompt decides.
        """
        deadline = time.time() + self._CONSOLE_SETTLE_S
        last: _ConsoleStep | None = None
        while time.time() < deadline:
            step = self._console_step(device, command, "settle")
            if not step.is_known:
                return Observable[_ConsoleStep].unknown(
                    step.detail or "console unreachable"
                )
            last = step.value
            if step.value.settled:
                return step
            time.sleep(self._CONSOLE_POLL_S)
        detail = (
            f"console on {device!r} never became usable "
            f"(last prompt {last.prompt!r})" if last else "console unreachable"
        )
        return Observable[_ConsoleStep].unknown(detail)

    def _console_show_once(
        self, device: str, command: str, *, timeout: float
    ) -> Observable[str]:
        """Type ONE command and return its complete output, pager included."""
        settled = self._console_settle(device, command)
        if not settled.is_known:
            return Observable[str].unknown(settled.detail or "console unreachable")

        armed = self._console_step(device, command, "type")
        if not armed.is_known:
            return Observable[str].unknown(
                f"could not type {command!r} on {device!r}: "
                f"{armed.detail or armed.status.value}"
            )
        baseline = armed.value.echo_count

        deadline = time.time() + timeout
        last_tail = ""
        stuck_on_pager = False
        while time.time() < deadline:
            step = self._console_step(device, command, "state")
            if not step.is_known:
                time.sleep(self._CONSOLE_POLL_S)
                continue
            state = step.value
            if state.tail:
                last_tail = state.tail
            fresh = state.echo_count > baseline
            stuck_on_pager = fresh and state.pager_at_end
            if fresh and state.at_prompt and not state.pager_at_end:
                return Observable.known(state.tail, source=f"console:{device}")
            if stuck_on_pager:
                # The read itself landed on `--More--`. This call queues the
                # space; the next poll collects the page behind it (Q3).
                self._console_step(device, command, "state")
            time.sleep(self._CONSOLE_POLL_S)
        if stuck_on_pager:
            reason = (
                f"{command!r} on {device!r} is still paging -- the console never "
                f"cleared --More-- within {timeout:.0f}s; output so far "
                f"{last_tail[-80:]!r}"
            )
        else:
            reason = (
                f"{command!r} never returned to a prompt on {device!r} within "
                f"{timeout:.0f}s"
                + (f"; last output ended {last_tail[-80:]!r}" if last_tail else "")
            )
        return Observable[str].unknown(reason)

    def _console_show_full(
        self,
        device: str,
        command: str,
        *,
        timeout: float | None = None,
        attempts: int = 2,
    ) -> Observable[str]:
        """Read one `show ...` off a PT console, completely, and retry once.

        Distinct from `_console_show`, which keeps the strict-growth rule for
        the serial-controller read-back (that one needs a caller-supplied
        completion rule on a saturated console). This one owns the pager.

        The retry exists because a device that is still booting answers with
        ROMMON text (R3). One retry turns a transient boot answer into real
        data instead of a permanent UNKNOWN; if it still fails, the verdict is
        UNKNOWN with the reason - never an empty table pretending to be one.
        """
        limit = self._CONSOLE_READ_S if timeout is None else timeout
        detail = ""
        for _attempt in range(max(1, attempts)):
            result = self._console_show_once(device, command, timeout=limit)
            if result.is_known:
                return result
            detail = result.detail or result.status.value
        return Observable[str].unknown(f"{detail} (after {attempts} attempts)")

    @staticmethod
    def _looks_like_boot_text(output: str) -> str:
        """The boot marker in `output`, or '' when it is a normal capture."""
        for marker in BridgeAdapter._CONSOLE_BOOT_MARKERS:
            if marker.lower() in output.lower():
                return marker
        return ""

    def read_routing_table(self, device: str) -> Observable[list]:
        """Structured `show ip route` read-back; empty table is KNOWN [].

        UNKNOWN - never a fabricated empty table - whenever the route table
        could not be observed: no console, a device that never returned to its
        prompt, or output that is boot text rather than a routing table. An
        ASA is the measured case: `getCommandLine()` is null on it and
        `getConsoleLine()` rejects `show route` with `% Invalid input detected`
        (probe_asa_api3.out / probe_asa_final.out), so an ASA route table is
        UNOBSERVABLE through PT and says so.
        """
        tail = self._console_show_full(device, "show ip route")
        if not tail.is_known:
            return Observable[list].unknown(tail.detail or tail.status.value)
        raw = tail.value
        marker = self._looks_like_boot_text(raw)
        if marker and "Codes:" not in raw:
            return Observable[list].unknown(
                f"{device!r} returned boot text, not a route table ({marker!r}): "
                f"{raw[:160]!r}"
            )
        if "% Invalid input" in raw or "% Ambiguous" in raw:
            return Observable[list].unknown(
                f"{device!r} rejected 'show ip route': {raw[-160:]!r}"
            )
        try:
            routes = js.parse_show_ip_route(raw)
        except Exception as exc:  # unparseable output stays honest
            return Observable[list].unknown(f"unparseable route table: {exc}: {raw[:200]!r}")
        # Observable is frozen; the raw console output travels in `detail` so the
        # evidence chain keeps the verbatim PT output without mutating the object.
        return Observable(
            status=ObservationStatus.KNOWN,
            value=routes,
            detail=raw,
            source=tail.source,
            observed_at=None,
        )

    def read_serial_controllers(
        self,
        device: str,
        interface: str,
        *,
        timeout: float = 12.0,
        attempts: int = 1,
        settle: float = 5.0,
    ) -> Observable[dict]:
        """Structured `show controllers serial` read-back for a DCE/DTE proof.

        Verified live: PT names the clocking end `DCE V.35, clock rate 64000`
        and the other end `DTE V.35 TX and RX clocks detected`, on Router-PT
        and on a 2911's HWIC-2T port alike. UNKNOWN when the platform has no
        such command - never a guessed role.

        TWO MEASURED PT LIMITS, both honoured here rather than papered over:

        1. PT answers `show controllers serial` for only ONE serial interface
           per router - the first one cabled. A router with two serial links
           answers for the first-cabled port and stays silent forever on the
           second, in either cabling order (_probe_serial7.py). The answer it
           does give is always correct, so this returns UNKNOWN - not a
           guess - for the rest, and such a hop is proven by its traffic: a
           serial link with no clock drops its line protocol
           (_probe_serial5.py), so a passing ping across the hop is itself
           evidence that the clock is in place.
        2. The completion rule anchors on the echo of this command instead of
           on the buffer growing past a base. On a router that just finished a
           build PT is still logging interface up/down, the console buffer sits
           at its cap, and a base-relative wait never completes
           (_serial_e2e.py).

        `attempts` stays at 1 so a build does not spend a minute polling an
        interface PT will never answer; a caller that wants the second chance
        asks for it.
        """
        command = f"show controllers serial {js.serial_iface_arg(interface)}"
        unknown: Observable[dict] | None = None
        for attempt in range(max(1, attempts)):
            if attempt and settle > 0:
                time.sleep(settle * attempt)
            tail = self._console_show(
                device,
                command,
                timeout=timeout * (attempt + 1),
                collect_expr=lambda: js.js_console_collect_serial_expr(
                    device, command
                ),
            )
            if not tail.is_known:
                unknown = Observable[dict].unknown(tail.detail or tail.status.value)
                continue
            parsed = js.parse_show_controllers_serial(tail.value)
            if parsed is None:
                return Observable[dict].unknown(
                    f"no serial controller evidence for {interface}: {tail.value[:200]!r}"
                )
            return Observable(
                status=ObservationStatus.KNOWN,
                value=parsed,
                detail=tail.value,
                source=tail.source,
                observed_at=None,
            )
        return unknown or Observable[dict].unknown("console unavailable")

    def read_vlans(self, device: str) -> Observable[list]:
        """Structured `show vlan brief` read-back (switches)."""
        tail = self._console_show_full(device, "show vlan brief")
        if not tail.is_known:
            return Observable[list].unknown(tail.detail or tail.status.value)
        try:
            vlans = js.parse_show_vlan_brief(tail.value)
        except Exception as exc:
            return Observable[list].unknown(f"unparseable vlan table: {exc}: {tail.value[:200]!r}")
        # Observable is frozen; the raw console output travels in `detail`.
        return Observable(
            status=ObservationStatus.KNOWN,
            value=vlans,
            detail=tail.value,
            source=tail.source,
            observed_at=None,
        )

    def read_trunk_allowed(self, device: str) -> Observable[dict[str, list[int]]]:
        """Structured `show interfaces trunk` read-back: {port: allowed vlans}.

        `show vlan brief` cannot see a trunk's allowed-VLAN list (only access
        members), so a removed trunk VLAN was invisible to verification and
        diagnosis. This is the one command that answers it. UNKNOWN on any
        unreadable/rejected answer — never an empty map that would read as
        "no trunk ports".
        """
        tail = self._console_show_full(device, "show interfaces trunk")
        if not tail.is_known:
            return Observable[dict[str, list[int]]].unknown(
                tail.detail or tail.status.value
            )
        if "% Invalid input" in tail.value or "% Ambiguous" in tail.value:
            return Observable[dict[str, list[int]]].unknown(
                f"{device!r} rejected 'show interfaces trunk'"
            )
        try:
            table = js.parse_show_interfaces_trunk(tail.value)
        except Exception as exc:
            return Observable[dict[str, list[int]]].unknown(
                f"unparseable trunk table: {exc}: {tail.value[:200]!r}"
            )
        return Observable(
            status=ObservationStatus.KNOWN,
            value=table,
            detail=tail.value,
            source=tail.source,
            observed_at=None,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _interpret(
        answer: Observable[str],
    ) -> tuple[OperationResult, dict | None, str | None]:
        """Map PT's reported string to an OperationResult. Timeout → UNKNOWN.

        JS literal `false` (and an empty answer) are FAILED: found live —
        lwAddLink answers `false` when the cable could not be established,
        and mapping that to success would make the receipt lie.
        """
        if not answer.is_known:
            return OperationResult.UNKNOWN, None, answer.detail
        value = str(answer.value)
        if value.startswith("PT_ERROR"):
            return OperationResult.FAILED, {"pt_result": value}, value
        if value.strip() in ("false", ""):
            return (
                OperationResult.FAILED,
                {"pt_result": value or "<empty>"},
                f"PT reported {value.strip() or '<empty>'!r} for the mutation",
            )
        return OperationResult.SUCCESS, {"pt_result": value}, None
