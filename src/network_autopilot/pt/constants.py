"""Constants for the Packet Tracer integration layer.

These describe fixed values of the PT environment (well-known port numbers,
device-type identifiers PT's IPC API expects, default addressing parameters).
They are facts about the PT platform, recorded here for the engine's own use.
"""

from __future__ import annotations

# DNS server handed to DHCP pools and host stacks when the spec does not say
# otherwise. A public resolver is a sane default for a lab topology.
DEFAULT_DNS = "8.8.8.8"

# Port the local HTTP bridge listens on. PT's webview polls this endpoint;
# the value must match the extension's configured target.
DEFAULT_BRIDGE_PORT = 54321
