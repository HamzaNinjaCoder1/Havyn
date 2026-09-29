"""S2.1: the 16-spec topology library.

Every spec parses; every spec lints and compiles clean offline (pending
ones too — pending only tells the runner to skip live execution, with the
reason recorded in the manifest). A spec that cannot clear lint+compile
fails here, not at 2am in the nightly runner.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler.spec_lint import format_issues, lint_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing

LIB = pathlib.Path(__file__).parents[2] / "library"
_MANIFEST = json.loads((LIB / "manifest.json").read_text(encoding="utf-8"))
_ENTRIES = _MANIFEST["specs"]


def _load(entry) -> dict:
    return json.loads((LIB / entry["file"]).read_text(encoding="utf-8"))


def test_manifest_covers_exactly_the_library():
    files = sorted(p.name for p in LIB.glob("T*.json"))
    assert len(files) == 16, files
    assert sorted(s["file"] for s in _ENTRIES) == files
    assert len({s["id"] for s in _ENTRIES}) == 16
    for entry in _ENTRIES:
        assert entry["status"] in ("active", "pending"), entry["id"]
        if entry["status"] == "pending":
            assert entry.get("pending_reason"), entry["id"]


@pytest.mark.parametrize("entry", _ENTRIES, ids=[e["id"] for e in _ENTRIES])
def test_spec_lints_and_compiles(entry):
    spec = NetworkSpec.model_validate(_load(entry))
    problems = lint_spec(spec)
    assert problems == [], format_issues(problems)
    plan = plan_addressing(spec)
    compile_spec(spec, plan)  # strict: must not raise
