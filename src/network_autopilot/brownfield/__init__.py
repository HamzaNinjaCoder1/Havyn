"""Stage 4: brownfield network intelligence.

Import an existing Packet Tracer network, read and parse its configuration,
turn it into an as-is spec, confirm objectives, trace and diagnose faults,
back up before changing anything, repair minimally with rollback, enhance
safely, and score the diagnosis engine against injected faults.

The package is layered: `models` holds observations and the diagnosis schema,
`config_reader`/`parser` read and structure configuration, `importer` builds
the inventory, `intent` builds and round-trips the spec, `objectives` owns
confirmation, `tracer`/`faults`/`report` diagnose, `backup`/`repair` mutate
safely and `enhance`/`benchmark` extend and score.
"""

from __future__ import annotations

from .models import (
    BrownfieldInventory,
    ConfigExtract,
    ConfigStatus,
    Confidence,
    Diagnostic,
    FieldConfidence,
    FixAction,
    HostInventory,
    InterfaceInventory,
    LinkInventory,
    Objective,
    Platform,
)

__all__ = [
    "BrownfieldInventory",
    "ConfigExtract",
    "ConfigStatus",
    "Confidence",
    "Diagnostic",
    "FieldConfidence",
    "FixAction",
    "HostInventory",
    "InterfaceInventory",
    "LinkInventory",
    "Objective",
    "Platform",
]
