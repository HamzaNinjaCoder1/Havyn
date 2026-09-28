"""Structured build errors (S1.3, F3).

``Autopilot.build()`` returns ``(report, error)`` where ``error`` is a string,
so every existing caller, log line and test keeps working. A caller that must
pick an *exit code*, however, cannot sniff prose: the typed subclass below
lets ``build_havyn.py`` map "preflight stopped the build" to its own exit code
while the value stays a plain string everywhere else.

``isinstance(err, str)`` holds for every error class here - the type is extra
information, never a replacement for the message.
"""

from __future__ import annotations


class BuildError(str):
    """A build error that is still a plain string but carries why it happened."""

    kind: str = "build"


class PreflightBlocked(BuildError):
    """Preflight (or the pre-build lint) refused to let execution start.

    Distinct from a compile *refusal* or an addressing error: those are
    errors in the request, this one is the safety gate doing its job - which
    is why ``build_havyn.py`` gives it its own exit code.
    """

    kind = "preflight"
