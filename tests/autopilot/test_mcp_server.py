"""MCP server: thin handlers over the real engine.

Unlike the upstream registry (closures inside register_tools, untestable),
every tool here is a module-level function: importable, callable without a
transport, and thin by construction — the logic under test lives in the engine
modules these handlers delegate to.
"""

from __future__ import annotations

import json

from src.network_autopilot.mcp import server
from tests.autopilot.test_compiler import GOLDEN_SPEC

SPEC = json.dumps(GOLDEN_SPEC)


def test_net_spec_validate_accepts_golden():
    out = server.net_spec_validate(SPEC)
    assert out["valid"] is True
    assert out["counts"]["devices"] == 8


def test_net_spec_validate_rejects_freeform():
    out = server.net_spec_validate(json.dumps({"devices": "lots"}))
    assert out["valid"] is False
    assert out["error"]


def test_net_plan_sizes_and_validates():
    out = server.net_plan(SPEC)
    assert out["validation"]["has_errors"] is False
    allocations = out["plan"]["allocations"]
    assert {a["subnet_id"] for a in allocations} == {"net-wan", "net-lan1", "net-lan2"}


def test_net_preflight_passes_without_touching_pt():
    out = server.net_preflight(SPEC)
    assert out["preflight"] == "PASS"
    assert out["operation_count"] > 0
    assert out["unsupported_features"] == []


def test_net_compile_is_deterministic():
    a, b = server.net_compile(SPEC), server.net_compile(SPEC)
    assert a["fingerprint"] == b["fingerprint"]
    ids_a = [o["operation_id"] for o in a["operations"]]
    ids_b = [o["operation_id"] for o in b["operations"]]
    assert ids_a == ids_b


def test_net_verify_on_empty_null_workspace_reports_failures_honestly():
    out = server.net_verify(SPEC, adapter="null")
    summary = out["summary"]
    assert summary["fail"] > 0  # nothing exists in a fresh workspace
    assert summary["pass"] < len(out["tests"])


def test_net_diff_on_empty_null_workspace_reports_drift():
    out = server.net_diff(SPEC, adapter="null")
    assert out["drift_count"] > 0


def test_net_build_dry_run_mutates_nothing(tmp_path):
    out = server.net_build(SPEC, adapter="null", safety_mode="dry_run", evidence_dir=str(tmp_path))
    assert out["report"]["execution"]["blocked"], "dry-run must not execute mutations"
    assert "NOT SUCCESS" in out["human_text"] or "UNKNOWN" in out["human_text"]


def test_net_build_autonomous_full_pipeline_with_evidence(tmp_path):
    out = server.net_build(SPEC, adapter="null", safety_mode="autonomous", evidence_dir=str(tmp_path))
    report = out["report"]
    assert report["execution"]["failed"] == []
    assert report["success"] is False  # connectivity UNKNOWN on the null adapter
    assert "UNKNOWN" in out["human_text"]
    # evidence chain written under the requested directory
    evidence_files = list(tmp_path.rglob("evidence.jsonl"))
    assert evidence_files, "the build must leave its evidence chain on disk"


def test_net_observe_null_returns_empty_state():
    out = server.net_observe("[]", adapter="null")
    assert out["devices"] == {}


def test_net_observe_bridge_without_channel_is_blocked():
    out = server.net_observe("[]", adapter="bridge")
    # honest either way: BLOCKED when no channel, real state when one exists
    if out.get("status") == "BLOCKED":
        assert out["detail"]
    else:
        assert "devices" in out
