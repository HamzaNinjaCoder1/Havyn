"""Compatibility import for the pre-1.0 adapter path.

New code must import from :mod:`network_autopilot.adapter.packet_tracer`.
"""

import time

from ..packet_tracer.bridge_adapter import BridgeAdapter, BridgeUnreachable, FileBridge

__all__ = ["BridgeAdapter", "BridgeUnreachable", "FileBridge"]
