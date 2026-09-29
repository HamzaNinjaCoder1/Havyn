"""Nightly schedule (S2.3), offline.

The Task Scheduler import itself needs your Windows/admin action, so what is
pinned here is everything the repo owns: the queue builder covers every
active library spec, reports every pending spec as skipped-with-reason
(never silently dropped), and its output loads straight into the runner;
the task XML stays well-formed so `schtasks /create` has something valid
to import.
"""

from __future__ import annotations

import importlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

make_queue = importlib.import_module("scripts.nightly.make_queue")

REPO_ROOT = Path(__file__).parents[2]
LIBRARY = REPO_ROOT / "library"


def test_queue_covers_every_active_spec_and_names_every_skip():
    queue = make_queue.build_queue(LIBRARY)
    manifest = json.loads((LIBRARY / "manifest.json").read_text(encoding="utf-8"))

    active = [e["id"] for e in manifest["specs"] if e["status"] == "active"]
    pending = [e["id"] for e in manifest["specs"] if e["status"] == "pending"]
    assert [c["id"] for c in queue["cases"]] == active
    assert [s["id"] for s in queue["skipped"]] == pending
    for skip in queue["skipped"]:
        assert skip["reason"], f"pending {skip['id']} needs its reason"
    for case in queue["cases"]:
        assert Path(case["spec"]).is_file(), case


def test_queue_output_loads_into_the_runner(tmp_path):
    out = tmp_path / "nightly-queue.json"
    assert make_queue.main(["--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))

    havyn_runner = importlib.import_module("scripts.havyn_runner")
    cases = havyn_runner.load_queue(out)
    assert [c["id"] for c in cases] == [c["id"] for c in data["cases"]]
    assert data["skipped"], "the skip record must survive the file round-trip"


def test_task_xml_is_well_formed_and_points_at_the_runner():
    xml_path = REPO_ROOT / "scripts" / "nightly" / "havyn-nightly.xml"
    raw = xml_path.read_text(encoding="utf-8")
    root = ET.fromstring(raw)
    text = ET.tostring(root, encoding="unicode")
    # the task name is the schtasks /tn argument from the header comment
    assert 'schtasks /create /tn "HavynNightly"' in raw
    assert "havyn-nightly.ps1" in text
    assert "CalendarTrigger" in text and "DaysInterval" in text
    assert "%REPOROOT%" in text, "absolute repo path is the importer's job"
