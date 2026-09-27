"""Packet Tracer console output parsing.

The engine needs to turn a PT ping statistics line into a connectivity
verdict. PT emits two distinct formats depending on who answered:

  Host (PC/Server)::

      Pinging 192.168.1.2 with 32 bytes of data:
      Reply from 192.168.1.2: bytes=32 time=1ms TTL=128
      ...
      Ping statistics for 192.168.1.2:
          Packets: Sent = 4, Received = 4, Lost = 0 (0% loss)

  IOS (router/switch)::

      Success rate is 100 percent (5/5)

Only the statistics line is meaningful for a verdict: a link that drops 3 of
4 packets is not "connected" the way a clean 4/4 is, but it is also not
"down". That distinction is why the result has three states instead of a
boolean.
"""

from __future__ import annotations

import re

# Verdicts. REACHABLE means every probe was answered; PARTIAL means some but
# not all (a degrading link, worth reporting separately from a healthy one);
# NONE means nothing came back. A fourth value would break the engine's
# verdict mapping, so callers rely on exactly these three.
PING_OK = "ok"
PING_PARTIAL = "partial"
PING_NONE = "none"

# "Packets: Sent = 4, Received = 4, Lost = 0 (0% loss)"
_HOST_SENT = re.compile(r"Sent\s*=\s*(\d+)")
_HOST_RECEIVED = re.compile(r"Received\s*=\s*(\d+)")
_HOST_LOST = re.compile(r"Lost\s*=\s*(\d+)")

# "Success rate is 100 percent (4/5)"
_IOS_RATE = re.compile(r"Success rate is\s*(\d+)\s*percent")

# Bare "(4/5)" ratio, used by some PT surfaces without the surrounding prose.
_RATIO = re.compile(r"\(\s*(\d+)\s*/\s*(\d+)\s*\)")


def classify_ping(stat_line: str) -> str:
    """Classify a PT ping statistics line as "ok" | "partial" | "none".

    Empty or unparseable input is "none" rather than an exception: the caller
    treats an unreadable result as no evidence of connectivity, and raising
    here would let a malformed PT reply take down an entire verification flow.
    """
    if not stat_line:
        return PING_NONE

    # Host form: we have explicit sent/received counts.
    received = _HOST_RECEIVED.search(stat_line)
    if received:
        got = int(received.group(1))
        sent_match = _HOST_SENT.search(stat_line)
        if sent_match:
            sent = int(sent_match.group(1))
        else:
            lost = _HOST_LOST.search(stat_line)
            sent = got + (int(lost.group(1)) if lost else 0)
        return _verdict_from_counts(got, sent)

    # IOS form: a percentage plus a ratio.
    rate = _IOS_RATE.search(stat_line)
    if rate:
        pct = int(rate.group(1))
        if pct <= 0:
            return PING_NONE
        return PING_OK if pct >= 100 else PING_PARTIAL

    ratio = _RATIO.search(stat_line)
    if ratio:
        got, sent = int(ratio.group(1)), int(ratio.group(2))
        return _verdict_from_counts(got, sent)

    return PING_NONE


def _verdict_from_counts(got: int, sent: int) -> str:
    if got <= 0:
        return PING_NONE
    if sent <= 0:
        # No probes were sent; PT reports this as 0/0. Treat nothing-answered
        # as "none" rather than silently upgrading to ok.
        return PING_NONE if got <= 0 else PING_PARTIAL
    if got >= sent:
        return PING_OK
    return PING_PARTIAL


def interpret_ping(stat_line: str) -> bool:
    """Boolean view of :func:`classify_ping` — at least one reply arrived.

    Kept for callers that only care "did anything answer". Prefer
    :func:`classify_ping` when the difference between full and partial success
    matters.
    """
    return classify_ping(stat_line) != PING_NONE
