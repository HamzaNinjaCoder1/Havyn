"""Every Stage 3 library spec passes offline lint and compile.

This is the S2.1-style sweep for the Stage 3 labs: a spec that cannot build
offline must never be presented as a live lab candidate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler.spec_lint import lint_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing

LIB = Path(__file__).resolve().parents[2] / "library" / "stage3"


def _specs() -> list[Path]:
    return sorted(LIB.glob("S3-*.json"))


@pytest.mark.parametrize("path", _specs(), ids=lambda p: p.stem)
def test_stage3_spec_lints_and_compiles(path: Path):
    spec = NetworkSpec.from_json(path.read_text(encoding="utf-8"))
    problems = lint_spec(spec)
    assert problems == [], problems
    result = compile_spec(spec, plan_addressing(spec))
    assert result.dag.nodes


def test_manifest_lists_every_stage3_spec():
    manifest = json.loads((LIB / "manifest.json").read_text(encoding="utf-8"))
    listed = {entry["file"] for entry in manifest["specs"]}
    present = {path.name for path in _specs()}
    assert listed == present
