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


def test_nightly_repo_root_is_two_levels_above_the_script():
    """The wrapper lives in scripts/nightly.

    `Join-Path $PSScriptRoot ".."` is `scripts/`, so the runner path became
    `scripts/scripts/havyn_runner.py` and every scheduled night died before
    the first build. The repo root is two levels up.
    """
    ps1 = (REPO_ROOT / "scripts" / "nightly" / "havyn-nightly.ps1").read_text(
        encoding="utf-8"
    )
    assert '(Join-Path $PSScriptRoot "..\\..")' in ps1, ps1
    assert '(Join-Path $PSScriptRoot "..")' not in ps1


def test_nightly_prefers_the_repository_virtualenv():
    """A scheduled task has no activated venv.

    Falling back to the bare `python` found on the task's PATH silently runs a
    system interpreter that may not have the package. Prefer the repo venv
    when it exists, and keep HAVYN_PYTHON as an explicit override.
    """
    ps1 = (REPO_ROOT / "scripts" / "nightly" / "havyn-nightly.ps1").read_text(
        encoding="utf-8"
    )
    assert "$env:HAVYN_PYTHON" in ps1
    assert 'Join-Path $RepoRoot ".venv\\Scripts\\python.exe"' in ps1
    assert "& $Python scripts/havyn_runner.py" in ps1


def test_nightly_probes_the_channel_not_only_the_heartbeat():
    """A fresh heartbeat can coexist with a mailbox that cannot execute.

    Night 5: the wrapper started PT, saw a heartbeat, ran the runner, and got
    exit 4. The wrapper now waits for a real ping before the first case.
    """
    ps1 = (REPO_ROOT / "scripts" / "nightly" / "havyn-nightly.ps1").read_text(
        encoding="utf-8"
    )
    assert "function Test-Channel" in ps1
    assert "ping(timeout=5.0)" in ps1
    assert "while (-not (Test-Channel))" in ps1


def test_nightly_generates_and_uploads_the_reports():
    """The morning needs a scorecard and capability map, not raw case JSON.

    Both are written into the results dir so the existing verified uploader
    (which copies *.json, *.md and history) carries them to the destination.
    """
    ps1 = (REPO_ROOT / "scripts" / "nightly" / "havyn-nightly.ps1").read_text(
        encoding="utf-8"
    )
    assert "scripts\\scorecard.py" in ps1
    assert "scripts\\capability_map.py" in ps1
    assert "scripts\\upload_results.py" in ps1
    assert 'Join-Path $Results "scorecard.json"' in ps1
    assert 'Join-Path $Results "capability-map.json"' in ps1


def test_nightly_finds_the_real_pt_binary_and_names_failed_cases():
    """PT lives in `Cisco Packet Tracer X\\bin\\PacketTracer.exe`.

    The glob used to omit `bin`, so a scheduled night with PT closed could
    never start it. A failure notification must also name the case and the
    runner's reason, not just the exit code.
    """
    ps1 = (REPO_ROOT / "scripts" / "nightly" / "havyn-nightly.ps1").read_text(
        encoding="utf-8"
    )
    assert "Cisco Packet Tracer*\\bin\\PacketTracer.exe" in ps1
    assert "Get-FailureDetail" in ps1
    assert "$result.case_id" in ps1 and "$result.detail" in ps1


def test_nightly_heartbeat_path_matches_the_engine_mailbox():
    """The wrapper waits on the Script Engine heartbeat before it runs.

    It has to look where the engine actually writes it — ``<dir>/<subdir>/
    alive.txt`` — in both runtime dirs. A guessed subdir (``mailbox`` instead
    of the engine's ``bridge``) makes ``Test-Heartbeat`` return false forever,
    so the nightly throws "no Script Engine heartbeat within 5 minutes" with
    Packet Tracer wide awake. This ties the .ps1 literal to the engine's own
    constant, so renaming the subdir on either side fails here.
    """
    from network_autopilot.pt import mailbox

    subdir = mailbox._MAILBOX_SUBDIR
    ps1 = (REPO_ROOT / "scripts" / "nightly" / "havyn-nightly.ps1").read_text(
        encoding="utf-8"
    )
    gate = (REPO_ROOT / "scripts" / "s119_gate.ps1").read_text(encoding="utf-8")

    # Windows PowerShell 5.1's Join-Path accepts exactly Path + ChildPath.
    # The four-argument form is PS 6+ only and made the gate die at parse time
    # before its first build ("A positional parameter cannot be found").
    # Nest the joins so the task runs on the Windows the laptop actually has.
    for script in (ps1, gate):
        assert (
            f'(Join-Path (Join-Path (Join-Path $env:LOCALAPPDATA "Havyn") '
            f'"{subdir}") "alive.txt")'
        ) in script, script
        assert (
            f'(Join-Path (Join-Path (Join-Path $env:LOCALAPPDATA '
            f'"packet-tracer-mcp") "{subdir}") "alive.txt")'
        ) in script, script
        assert f'"Havyn" "{subdir}" "alive.txt"' not in script, (
            "four-argument Join-Path breaks Windows PowerShell 5.1"
        )
    assert '"mailbox" "alive.txt"' not in ps1, (
        "the nightly still probes a stale mailbox/ heartbeat path"
    )

