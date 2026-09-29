"""Scorecard (S2.4): measured metrics in, honest gaps stated, no fabrication."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

scorecard = importlib.import_module("scripts.scorecard")


def _results(tmp_path: Path) -> Path:
    root = tmp_path / "results"
    root.mkdir()
    (root / "T1.json").write_text(json.dumps({
        "case_id": "T1", "spec": "t1.json", "status": "pass",
        "criteria": {"pass": 2, "fail": 0, "unknown": 0, "total": 2},
        "elapsed_s": 10.0, "build_id": "b1", "detail": None,
        "finished_at": "then",
    }), encoding="utf-8")
    (root / "T2.json").write_text(json.dumps({
        "case_id": "T2", "spec": "t2.json", "status": "blocked",
        "criteria": {"pass": 0, "fail": 0, "unknown": 0, "total": 0},
        "elapsed_s": 4.0, "build_id": None, "detail": "preflight",
        "finished_at": "then",
    }), encoding="utf-8")
    (root / "summary.json").write_text(json.dumps(
        {"total": 2, "ran": 2}), encoding="utf-8")
    return root


def test_scorecard_aggregates_only_what_the_files_support(tmp_path):
    card = scorecard.build_scorecard(*scorecard.load_results(_results(tmp_path)), [])

    assert card["cases"] == 2
    assert card["status_counts"] == {"pass": 1, "blocked": 1}
    assert card["criteria"]["pass_pct"] == 100.0
    assert card["timing_s"]["total"] == 14.0
    assert card["refusal_rate"] == 0.5
    # unmeasured stays named with its reason, never a fabricated zero
    assert set(card["unmeasured"]) >= {"false_pass_rate", "diagnosis_accuracy"}
    assert all(card["unmeasured"].values())


def test_scorecard_carries_the_skip_record_and_renders_it(tmp_path):
    root = _results(tmp_path)
    skipped = [{"id": "T07", "reason": "awaiting live proof"}]
    card = scorecard.build_scorecard(*scorecard.load_results(root), skipped)
    md = scorecard.render_markdown(card)

    assert card["skipped"] == skipped
    assert "T07" in md and "awaiting live proof" in md
    assert "Not measured by this run" in md


def test_scorecard_cli_writes_both_artifacts(tmp_path):
    root = _results(tmp_path)
    out, md = tmp_path / "card.json", tmp_path / "card.md"
    assert scorecard.main([str(root), "--out", str(out), "--md", str(md)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["cases"] == 2
    assert md.read_text(encoding="utf-8").startswith("# Havyn nightly scorecard")


def test_empty_results_dir_is_an_error_not_an_empty_card(tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    assert scorecard.main([str(root)]) == 1
