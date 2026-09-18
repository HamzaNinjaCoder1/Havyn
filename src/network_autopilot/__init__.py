"""AI Network Autopilot — closed-loop network engineering for Cisco Packet Tracer.

Principle: "configured" does not mean "verified". The authoritative truth is
observed Packet Tracer state and verification evidence, never LLM prose.

Provenance: everything in this package is engineered independently for this
product; the low-level Packet Tracer transport it optionally rides on lives in
the local Packet Tracer transport package and is reachable only through
`network_autopilot.adapter` — see PROVENANCE.md and THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
