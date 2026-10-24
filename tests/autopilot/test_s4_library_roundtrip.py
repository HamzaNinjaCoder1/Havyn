"""Round-trip verification across all 16 topology-library specs (S4.4)."""

from __future__ import annotations

import json

from network_autopilot.brownfield.importer import ImportOptions
from network_autopilot.brownfield.intent import AsIsIntentBuilder, RoundTripValidator

from tests.autopilot import s4_helpers as h


def test_all_library_specs_survive_the_round_trip() -> None:
    manifest = json.loads(
        (h.LIBRARY / "manifest.json").read_text(encoding="utf-8")
    )
    failures = []
    for entry in manifest["specs"]:
        spec = h.load_spec(entry["file"])
        from network_autopilot.brownfield.simulated import SimulatedAdapter

        adapter = SimulatedAdapter(spec)
        from network_autopilot.brownfield.importer import BrownfieldImporter

        imported = BrownfieldImporter(
            adapter, options=ImportOptions(read_stp=True)
        ).import_network()
        intent = AsIsIntentBuilder().build(imported)
        report = RoundTripValidator().validate(intent, imported)
        if report.status not in ("exact", "equivalent"):
            failures.append(
                (
                    entry["id"],
                    report.status,
                    [d.device for d in report.devices if d.status == "not_equivalent"],
                )
            )
    assert not failures, failures
