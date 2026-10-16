"""S1.19 Stage 1: route read-back must tell the truth (R1 / R2 / R3).

`.zwork/audit_routes.py` reported 9 of 18 spec static routes MISSING while a raw
console capture of the very same device showed `S*  0.0.0.0/0 [1/0] via
10.0.0.5` sitting in the table. The audit lied, so no route-based diagnosis
could be trusted. Three separate defects, all measured live against PT 9.x:

**R1 - the collector stops at the pager.** PT's console buffer is capped
(measured 7 931 chars on a 2911) and a long `show ip route` pages. The capture
in `autopilot_evidence/build-b0d96e34cc97/evidence.jsonl` line 414 ends at
``S       10.20.0.0/16 [1/0] via 10.0.0.10\\n --More-- `` - every remaining
static route AND the `S*` default live on page 2, so the parsed table was a
truncated lie. Probed live: a single space (`enterCommand(' ')`) advances PT's
pager by exactly one page (buffer 6 572 -> 7 852).

**R2 - a console-less device is UNOBSERVABLE, not MISSING.** An ASA answers
`ERR:device without console`. Reporting that as "route missing" is a rule-7
violation: the property could not be observed, so the verdict is UNKNOWN with
the reason.

**R3 - the read can return a stale / mid-boot buffer.** A freshly created PT
router parks at `initial configuration dialog? [yes/no]: ` and a booting one
shows `System Bootstrap, Version 12.1(3r)T2`. The old arm batched four
`enterCommand` calls into one round trip and PT merged them into ONE input
line - measured verbatim as `Router#nable` followed by
`Translating "nable"...domain server (255.255.255.255)`. The read then handed
back whatever was in the buffer, which parsed as "no routes at all".

Every test below fails against the unfixed reader.
"""

from __future__ import annotations

import json

import pytest

from src.network_autopilot.adapter.packet_tracer import js_commands as js
from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.observability import Observable

# --------------------------------------------------------------------------
# Real PT captures. Verbatim from evidence.jsonl (build-b0d96e34cc97) and from
# the live probes in .zwork/logs/probe_pager3.out - never hand-written.
# --------------------------------------------------------------------------

#: line 414 of the build evidence: page 1 of a paged table. `S*` is absent.
PAGED_ROUTE_TABLE_PAGE1 = """show ip route
Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP

Gateway of last resort is 10.0.0.5 to network 0.0.0.0

     10.0.0.0/8 is variably subnetted, 11 subnets, 4 masks
C       10.0.0.4/30 is directly connected, GigabitEthernet0/1
L       10.0.0.6/32 is directly connected, GigabitEthernet0/1
C       10.0.0.8/30 is directly connected, GigabitEthernet0/2
L       10.0.0.9/32 is directly connected, GigabitEthernet0/2
C       10.10.10.0/24 is directly connected, GigabitEthernet0/0.10
L       10.10.10.1/32 is directly connected, GigabitEthernet0/0.10
C       10.10.20.0/24 is directly connected, GigabitEthernet0/0.20
L       10.10.20.1/32 is directly connected, GigabitEthernet0/0.20
C       10.10.30.0/24 is directly connected, GigabitEthernet0/0.30
L       10.10.30.1/32 is directly connected, GigabitEthernet0/0.30
S       10.20.0.0/16 [1/0] via 10.0.0.10
 --More-- """

#: line 138 of the build evidence: the same device, short enough to fit a page.
UNPAGED_ROUTE_TABLE = """show ip route
Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP

Gateway of last resort is 10.0.0.5 to network 0.0.0.0

     10.0.0.0/8 is variably subnetted, 8 subnets, 3 masks
C       10.0.0.4/30 is directly connected, GigabitEthernet0/1
L       10.0.0.6/32 is directly connected, GigabitEthernet0/1
C       10.10.10.0/24 is directly connected, GigabitEthernet0/0.10
L       10.10.10.1/32 is directly connected, GigabitEthernet0/0.10
S*   0.0.0.0/0 [1/0] via 10.0.0.5

rhq#"""

#: verbatim console tail from .zwork/logs/probe_pager2.out on a booting 2911.
BOOT_TEXT = """System Bootstrap, Version 12.1(3r)T2 (release) (fc1) on Thu Jan  1 00:02:11 UTC 2026
Copyright (c) 1986-2003 by cisco Systems, Inc.

"""

#: verbatim prompt from .zwork/logs/probe_pager3.out on a freshly added 2911.
INITIAL_CONFIG_PROMPT = (
    "initial configuration dialog? [yes/no]: "
    "Would you like to enter the initial configuration dialog? [yes/no]: "
)


class FakeConsole:
    """A PT `CommandLine` double speaking the real step protocol.

    It reimplements, in Python, the decision `js_console_show_expr` makes in JS,
    so the tests exercise the ORCHESTRATION (when to settle, when to type, when
    to drain the pager, when to accept, when to give up) against the PT
    behaviours measured in `.zwork/logs/probe_pager3.out` and
    `probe_drain_asa.out`:

    * the buffer is capped, so `o.length` cannot be a completion signal;
    * output past one screen PAGES on `--More--`, and only a typed space
      advances it (measured 6 572 -> 7 852 for one page);
    * `--More--` stays in the scrollback forever, so only the TAIL says whether
      PT is paging right now;
    * `getPrompt()` is the settle signal: a fresh PT 2911 reports
      `initial configuration dialog? [yes/no]: `, a usable one `Router#`;
    * a booting device answers with ROMMON text instead of a table.

    The expression-content tests below pin that the shipped JS agrees with this
    model, so the two cannot drift apart silently.
    """

    def __init__(
        self,
        pages: list[str],
        *,
        page_chars: int = 0,
        prompt: str = "rbr#",
        has_console: bool = True,
        max_buffer: int = 8000,
        boot_first: bool = False,
        dismiss_works: bool = True,
        needs_return_first: bool = False,
    ) -> None:
        self.pages = list(pages)
        self.page_chars = page_chars
        self.prompt = prompt
        self.has_console = has_console
        self.max_buffer = max_buffer
        self.boot_first = boot_first
        self.dismiss_works = dismiss_works
        self.needs_return_first = needs_return_first
        self.buffer = ""
        self.sent: list[str] = []
        self.page_index = 0
        self.typed_command = False
        # PT's buffer already holds whatever the device last printed.
        if needs_return_first:
            self.prompt = ""
            self._raw("...\nrbr con0 is now available\n\nPress RETURN to get started.\n")
        elif prompt and not _is_cli(prompt):
            self._raw(prompt)

    # -- PT's behaviour ---------------------------------------------------
    def _raw(self, text: str) -> None:
        self.buffer += text
        if len(self.buffer) > self.max_buffer:
            self.buffer = self.buffer[-self.max_buffer :]

    def _echo(self, text: str) -> None:
        """Typing renders as `<prompt><text>` then a newline, like PT does."""
        self._raw(f"{self.prompt}{text}\n")
        self.sent.append(text)

    def _pump(self) -> None:
        if self.page_index >= len(self.pages):
            self._raw(self.prompt)
            return
        chunk = self.pages[self.page_index]
        if self.page_chars:
            chunk = chunk[: self.page_chars]
        self._raw(chunk)
        self.page_index += 1
        more = self.page_index < len(self.pages)
        if more:
            self._raw("--More-- ")
            self.prompt = "--More--"
        else:
            self.prompt = "rbr#"
            self._raw(self.prompt)

    def step(self, command: str, action: str) -> str:
        """Return exactly the payload `js_console_show_expr` would return."""
        out = self.buffer
        buf_last = ""
        for line in reversed(out.split("\n")):
            stripped = line.replace(" ", "").replace("\r", "")
            if stripped:
                buf_last = stripped
                break
        pager_at_end = bool(out.rstrip().endswith("--More--"))
        dialog = "[yes/no]" in buf_last or "[yes/no]" in self.prompt
        # `buf_last` has spaces stripped above, so the marker is matched in the
        # same spaceless form the JS uses (`/PressRETURNtogetstarted/i`) - a
        # spaced phrase in a spaceless string is always False, which is exactly
        # the defect this pair of models exists to catch.
        needs_return = "pressreturntogetstarted" in buf_last.lower()
        settled = not (pager_at_end or dialog or needs_return)
        echo_count = out.count(command) if command else 0
        index = out.rfind(command) if command else -1
        tail = out[index:] if index >= 0 else ""
        last = ""
        for line in reversed(tail.replace("\r", "").split("\n")):
            stripped = line.replace(" ", "")
            if stripped:
                last = stripped
                break
        at_prompt = bool(last) and last.endswith(("#", ">"))

        act = False
        send = ""
        if action == "type" and not self.typed_command:
            self.typed_command = True
            self.page_index = 0
            self._echo(command)
            if self.boot_first:
                self._raw(BOOT_TEXT)
                self.prompt = "rbr#"
                self._raw("rbr#")
            else:
                self._pump()
            act, send = True, command
        elif pager_at_end:
            self._echo(" ")
            if self.dismiss_works:
                self._pump()
            act, send = True, " "
        elif dialog:
            self._echo("no")
            self.prompt = "rbr>"
            self._raw(self.prompt)
            act, send = True, "no"
        elif needs_return:
            # An empty command: PT just prints the prompt again.
            self.sent.append("")
            self._raw("rbr#")
            self.prompt = "rbr#"
            act, send = True, ""
        return (
            f"S:{echo_count}|{int(pager_at_end)}|{int(at_prompt)}|{int(settled)}"
            f"|{int(act)}|{json.dumps(self.prompt)}|{tail}"
        )


def _is_cli(prompt: str) -> bool:
    text = str(prompt or "").strip()
    return bool(text) and "[" not in text and text.endswith(("#", ">"))


def _adapter_with_console(monkeypatch, console: FakeConsole) -> BridgeAdapter:
    """A BridgeAdapter whose send_expression drives the FakeConsole."""
    adapter = BridgeAdapter.__new__(BridgeAdapter)
    adapter._CONSOLE_POLL_S = 0.0
    adapter._CONSOLE_SETTLE_S = 0.5
    adapter._CONSOLE_READ_S = 0.5

    def _fake_send(expression, statements=None, wait=None, **_kw):
        if not console.has_console:
            return Observable[str].known("E:no console ERR:device without console")
        command = _command_in(expression)
        action = _action_in(expression)
        return Observable[str].known(console.step(command, action))

    monkeypatch.setattr(adapter, "send_expression", _fake_send)
    return adapter


def _command_in(expression: str) -> str:
    """Pull the command out of the shipped JS expression (pin: it is quoted)."""
    marker = "var echoes=o.split("
    if marker not in expression:
        return ""
    rest = expression[expression.index(marker) + len(marker):]
    return json.loads(rest[: rest.index(")")])


def _action_in(expression: str) -> str:
    """Pull the action out of the shipped JS expression."""
    marker = "==='type'"
    if marker not in expression:
        return "state"
    head = expression[: expression.index(marker)]
    quoted = head[head.rindex("if(") + 3:]
    return json.loads(quoted)


# ==========================================================================
# R1 - the pager must be drained, not truncated
# ==========================================================================


def test_paged_route_table_recovers_the_default_route_from_the_next_page(monkeypatch):
    """RED before the fix: only page 1 was read, so `S* 0.0.0.0/0` vanished.

    The audit then reported `rhq 0.0.0.0/0 via 10.0.0.5 MISSING` on a router
    that demonstrably had it.
    """
    console = FakeConsole(
        pages=[PAGED_ROUTE_TABLE_PAGE1, "\nS*   0.0.0.0/0 [1/0] via 10.0.0.5\n\nrbr#"],
        page_chars=1302,
    )
    adapter = _adapter_with_console(monkeypatch, console)

    table = adapter.read_routing_table("rbr")

    assert table.is_known, table.detail
    defaults = [r for r in table.value if r["kind"] == "default"]
    assert defaults, (
        "the default route lives on page 2; a reader that stops at --More-- "
        f"reports it missing. parsed instead: {table.value}"
    )
    assert defaults[0]["next_hop"] == "10.0.0.5"
    assert any(r["kind"] == "static" for r in table.value)


def test_unpaged_route_table_is_still_read_completely(monkeypatch):
    """The short table (page 1 only) must not regress."""
    console = FakeConsole(pages=[UNPAGED_ROUTE_TABLE + "\n"])
    adapter = _adapter_with_console(monkeypatch, console)

    table = adapter.read_routing_table("rhq")

    assert table.is_known, table.detail
    assert any(r["kind"] == "default" and r["next_hop"] == "10.0.0.5" for r in table.value)
    assert len([r for r in table.value if r["kind"] == "local"]) == 2


def test_reader_types_a_space_to_advance_pt_pager(monkeypatch):
    """Probed live (probe_pager3 Q3): a bare space advances PT one page."""
    console = FakeConsole(
        pages=[PAGED_ROUTE_TABLE_PAGE1, "\nS*   0.0.0.0/0 [1/0] via 10.0.0.5\n\nrbr#"],
        page_chars=1302,
    )
    adapter = _adapter_with_console(monkeypatch, console)

    adapter.read_routing_table("rbr")

    assert " " in console.sent, (
        f"PT only advances its pager on input; nothing was typed: {console.sent}"
    )


def test_step_expression_never_gates_completion_on_buffer_growth():
    """A capped PT buffer stops growing, so `o.length > base` never fires.

    The old `js_console_collect_expr` gated on exactly that and returned
    UNKNOWN for a paged table (measured live: base 5 213, buffer stayed 5 213).
    """
    expr = js.js_console_show_expr("rbr", "show ip route")
    assert "o.length>" not in expr and "o.length<" not in expr
    assert "lastIndexOf" in expr, "completion must anchor on THIS command's echo"


def test_pager_is_judged_from_the_buffer_tail_not_the_whole_buffer():
    """Regression pin for my own first wrong guess.

    `--More--` stays in the scrollback forever, so a whole-buffer test reports
    a pager that no longer exists and the reader loops on it. Only the tail
    says whether PT is paging RIGHT NOW.
    """
    expr = js.js_console_show_expr("rbr", "show ip route")
    assert "--More--" in expr
    assert "pager_at_end" in expr or "PAGER" in expr
    # the pager test must be anchored to the END of the buffer, not indexOf.
    assert "o.indexOf('--More--')" not in expr, (
        "indexOf finds the pager in the scrollback and re-pages forever"
    )


def test_read_routing_table_is_unknown_when_the_pager_never_clears(monkeypatch):
    """A console wedged on `--More--` must be UNKNOWN with the reason.

    Reporting an empty table here is exactly the R2/R3 lie: "I could not see
    it" must never render as "it is not there".
    """
    wedged = FakeConsole(
        pages=[
            PAGED_ROUTE_TABLE_PAGE1,
            "\nS*   0.0.0.0/0 [1/0] via 10.0.0.5\n\nrbr#",
        ],
        page_chars=1302,
        dismiss_works=False,
    )
    adapter = _adapter_with_console(monkeypatch, wedged)

    table = adapter.read_routing_table("rbr")

    assert not table.is_known
    assert "more" in (table.detail or "").lower(), table.detail


# ==========================================================================
# R2 - a console-less device is UNKNOWN, never MISSING
# ==========================================================================


def test_device_without_console_reads_as_unknown_with_the_reason(monkeypatch):
    console = FakeConsole(pages=[], has_console=False)
    adapter = _adapter_with_console(monkeypatch, console)

    table = adapter.read_routing_table("fwhq")

    assert not table.is_known
    assert "console" in (table.detail or "").lower(), table.detail


@pytest.mark.parametrize("device", ["fwhq", "fwdc", "fwbr"])
def test_audit_never_reports_a_route_missing_on_an_unobservable_table(device):
    """Rule 7 as executable code: UNKNOWN in, UNKNOWN out - never MISSING."""
    from scripts.audit_routes import audit_route

    verdict = audit_route(
        device=device,
        destination="0.0.0.0/0",
        next_hop="10.0.0.9",
        table=Observable[list].unknown("console unavailable: ERR:device without console"),
    )

    assert verdict.status == "unknown", verdict
    assert "console" in verdict.reason.lower(), verdict.reason
    assert "MISSING" not in verdict.status.upper()


def test_audit_reports_a_route_present_when_the_table_proves_it():
    from scripts.audit_routes import audit_route

    table = Observable.known(
        [
            {"kind": "default", "network": "0.0.0.0", "prefix": 0,
             "next_hop": "10.0.0.5", "interface": None},
        ],
        source="console:rhq",
    )
    verdict = audit_route(
        device="rhq", destination="0.0.0.0/0", next_hop="10.0.0.5", table=table
    )
    assert verdict.status == "pass", verdict


def test_audit_reports_missing_only_when_a_read_table_proves_absence():
    from scripts.audit_routes import audit_route

    table = Observable.known(
        [{"kind": "connected", "network": "10.0.0.4", "prefix": 30,
          "next_hop": None, "interface": "GigabitEthernet0/1"}],
        source="console:rhq",
    )
    verdict = audit_route(
        device="rhq", destination="0.0.0.0/0", next_hop="10.0.0.5", table=table
    )
    assert verdict.status == "missing", verdict


# ==========================================================================
# R3 - a stale / mid-boot buffer must never parse as a route table
# ==========================================================================


def test_boot_text_is_not_parsed_as_a_route_table(monkeypatch):
    """RED before the fix: boot text parsed to an empty table, reported KNOWN."""
    console = FakeConsole(pages=[BOOT_TEXT + "\nrbr#"])
    adapter = _adapter_with_console(monkeypatch, console)

    table = adapter.read_routing_table("rbr")

    assert not table.is_known, (
        "boot text is not a route table; an empty KNOWN table is the R3 lie "
        f"(value={table.value!r})"
    )
    assert "bootstrap" in (table.detail or "").lower() or "boot" in (
        table.detail or ""
    ).lower(), table.detail


def test_boot_text_read_retries_and_succeeds_once_the_device_is_ready(monkeypatch):
    """The retry is the point: a booting device must not be a permanent UNKNOWN."""
    console = FakeConsole(pages=[BOOT_TEXT + "\nrbr#"])

    adapter = _adapter_with_console(monkeypatch, console)
    calls = {"n": 0}
    real_read = adapter.read_routing_table

    def _read(device):
        calls["n"] += 1
        if calls["n"] == 1:
            return Observable[list].unknown("still booting")
        console.pages = [UNPAGED_ROUTE_TABLE + "\n"]
        console.page_index = 0
        console._pump()
        return real_read(device)

    monkeypatch.setattr(adapter, "read_routing_table", _read)

    first = adapter.read_routing_table("rbr")
    assert not first.is_known
    second = adapter.read_routing_table("rbr")
    assert second.is_known
    assert any(r["kind"] == "default" for r in second.value)


def test_initial_configuration_dialog_is_answered_before_the_command(monkeypatch):
    """Measured live: a fresh PT router parks at `[yes/no]` and PT merges a
    batched burst into one input line (`Router#nable`). The reader must settle
    the dialog first and send exactly one command."""
    console = FakeConsole(pages=[UNPAGED_ROUTE_TABLE + "\n"], prompt=INITIAL_CONFIG_PROMPT)
    adapter = _adapter_with_console(monkeypatch, console)

    table = adapter.read_routing_table("rbr")

    assert table.is_known, table.detail
    assert "no" in console.sent, f"the [yes/no] dialog was never answered: {console.sent}"
    assert console.sent.count("show ip route") == 1, console.sent


def test_rebooted_device_is_woken_from_press_return_before_typing(monkeypatch):
    """Measured live on rbr and redge after a full build: both had rebooted and
    their consoles sat at

        rbr con0 is now available

        Press RETURN to get started.

    `getPrompt()` is "" there, so a prompt-based gate would call it ready and
    type into a device that is not listening. The reader must press RETURN first.
    """
    console = FakeConsole(
        pages=[UNPAGED_ROUTE_TABLE + "\n"], needs_return_first=True
    )
    adapter = _adapter_with_console(monkeypatch, console)

    table = adapter.read_routing_table("rbr")

    assert table.is_known, table.detail
    assert "" in console.sent, f"RETURN was never pressed: {console.sent}"
    assert any(r["kind"] == "default" for r in table.value)


def test_press_return_text_in_scrollback_must_not_wedge_the_reader(monkeypatch):
    """Regression pin for the live failure this file exists to prevent.

    The first live run of the fixed reader reported
    `console on 'rbr' never became usable (last prompt 'rbr>')` for a perfectly
    healthy lab, because the readiness check asked whether the buffer CONTAINED
    `Press RETURN to get started`. That text never leaves the scrollback, so a
    rebooted device stays "not ready" forever. Readiness must be a question
    about the buffer's LAST line - exactly the trap `--More--` already set.
    """
    console = FakeConsole(
        pages=[UNPAGED_ROUTE_TABLE + "\n"], needs_return_first=True
    )
    # the marker is still sitting in the buffer after the device woke up
    assert "Press RETURN to get started" in console.buffer

    adapter = _adapter_with_console(monkeypatch, console)
    table = adapter.read_routing_table("rbr")

    assert table.is_known, table.detail
    assert "Press RETURN to get started" in console.buffer, (
        "the test is only meaningful while the stale marker is still there"
    )


def test_settled_flag_never_depends_on_getprompt(monkeypatch):
    """`getPrompt()` is "" while a console is idle and `rhq>` only while something
    is typed, so readiness has to come from the buffer, not from the prompt."""
    console = FakeConsole(pages=[UNPAGED_ROUTE_TABLE + "\n"], prompt="")
    adapter = _adapter_with_console(monkeypatch, console)

    table = adapter.read_routing_table("rhq")

    assert table.is_known, table.detail


def test_step_expression_reports_settled_separately_from_at_prompt():
    """The two are different questions and the wire format has to keep them apart.

    A device that is usable can have async `%LINEPROTO-5-UPDOWN` spam at the end
    of its buffer and no trailing prompt at all; requiring a trailing prompt to
    declare it settled made every post-build router read UNKNOWN (measured:
    `scripts/audit_routes.py` reported 0 pass / 18 unknown against a healthy lab).
    """
    expr = js.js_console_show_expr("rhq", "show ip route")
    assert "settled" in expr
    assert "needs_return" in expr
    # Matched in its spaceless form: the JS strips spaces out of the buffer's
    # last line before testing, so the spaced phrase would never match there.
    assert "PressRETURNtogetstarted" in expr
    assert "Press RETURN to get started" not in expr.split("needs_return")[1], (
        "the marker is tested against a space-stripped line, so the regex has "
        "to be spaceless too"
    )
    assert "at_prompt" in expr, "completion still needs the device back at a prompt"


def test_step_expression_sends_exactly_one_command_per_round_trip():
    """PT merges a burst of `enterCommand` calls into one input line.

    Measured verbatim: `Router#nable` then `Translating "nable"...`. The step
    expression must therefore queue a single command, never a batch.
    """
    expr = js.js_console_show_expr("rbr", "show ip route")
    assert expr.count("enterCommand") == 1, expr
