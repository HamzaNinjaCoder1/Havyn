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
        "elapsed_s": 10.0, "build_id": "b1", "evidence_dir": "evidence/b1",
        "detail": None, "finished_at": "then",
    }), encoding="utf-8")
    (root / "T2.json").write_text(json.dumps({
        "case_id": "T2", "spec": "t2.json", "status": "blocked",
        "criteria": {"pass": 0, "fail": 0, "unknown": 0, "total": 0},
        "elapsed_s": 4.0, "build_id": None, "evidence_dir": None,
        "detail": "preflight", "finished_at": "then",
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


# -- evidence-derived metrics (S2.4) -------------------------------------


def _faults(*rows) -> dict:
    return {"faults": list(rows)}


def test_false_pass_and_diagnosis_rates_come_from_the_fault_lab(tmp_path):
    lab = _faults(
        {"label": "port-shutdown", "expect_named_cause": True,
         "diagnosis_ran": True, "diagnosis_matched": True,
         "verdict_after_injection": "fail", "false_pass": False},
        {"label": "route-missing", "expect_named_cause": True,
         "diagnosis_ran": True, "diagnosis_matched": False,
         "verdict_after_injection": "pass", "false_pass": True},
        {"label": "wrong-gateway", "expect_named_cause": False,
         "diagnosis_ran": True, "diagnosis_matched": None,
         "verdict_after_injection": "fail", "false_pass": False},
    )
    card = scorecard.build_scorecard(
        *scorecard.load_results(_results(tmp_path)), [], fault_lab=lab)

    assert card["false_pass"]["rate"] == 0.333
    assert card["false_pass"]["false_passes"] == 1
    assert card["false_pass"]["labels"] == ["route-missing"]
    # the wrong gateway is excluded from the denominator: unknown is the
    # designed answer there, and scoring it would reward guessing
    assert card["diagnosis"]["accuracy"] == 0.5
    assert card["diagnosis"]["missed"] == ["route-missing"]
    assert card["diagnosis"]["unknown_by_design"] == ["wrong-gateway"]
    assert "false_pass_rate" not in card["unmeasured"]

    md = scorecard.render_markdown(card)
    assert "False PASS detail" in md
    assert "route-missing" in md
    assert "Diagnosis detail" in md


def test_a_lab_without_a_post_injection_verdict_stays_unmeasured(tmp_path):
    """An injection with no recorded verdict cannot support a false-PASS rate."""
    lab = _faults(
        {"label": "port-shutdown", "expect_named_cause": True,
         "diagnosis_ran": False, "verdict_after_injection": ""},
    )
    card = scorecard.build_scorecard(
        *scorecard.load_results(_results(tmp_path)), [], fault_lab=lab)
    assert card["false_pass"] is None
    assert "false_pass_rate" in card["unmeasured"]


def test_same_network_consistency_needs_repeated_runs(tmp_path):
    cases, summary = scorecard.load_results(_results(tmp_path))
    history = [
        {"topology_id": "T1", "status": "pass", "case_id": "a"},
        {"topology_id": "T1", "status": "pass", "case_id": "b"},
        {"topology_id": "T2", "status": "pass", "case_id": "c"},
        {"topology_id": "T2", "status": "fail", "case_id": "d"},
        {"topology_id": "T3", "status": "pass", "case_id": "e"},
    ]
    card = scorecard.build_scorecard(cases, summary, [], history=history)

    assert card["consistency"]["rate"] == 0.5
    assert card["consistency"]["repeated_networks"] == 2
    assert card["consistency"]["groups"]["T1"]["consistent"] is True
    assert card["consistency"]["groups"]["T2"]["consistent"] is False
    assert "T3" not in card["consistency"]["groups"], "one run is not a sample"

    md = scorecard.render_markdown(card)
    assert "Consistency detail" in md
    assert "INCONSISTENT" in md


def test_one_run_per_topology_leaves_consistency_unmeasured(tmp_path):
    card = scorecard.build_scorecard(*scorecard.load_results(_results(tmp_path)),
                                     [], history=[])
    assert card["consistency"] is None
    assert "same_network_consistency" in card["unmeasured"]


def test_case_table_carries_identity_evidence_and_reasons(tmp_path):
    card = scorecard.build_scorecard(*scorecard.load_results(_results(tmp_path)),
                                     [])
    md = scorecard.render_markdown(card)
    assert "| topology | case | run | mode | outcome |" in md
    assert "REFUSED" in md or "BLOCKED (environment)" in md
    assert "evidence/b1" in md, "the evidence directory must be visible on the page"


def test_history_loader_skips_torn_lines(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "history.jsonl").write_text(
        '{"topology_id":"T1","status":"pass"}\n'
        'not json\n'
        '{"topology_id":"T1","status":"pass"}\n',
        encoding="utf-8",
    )
    assert len(scorecard.load_history(results)) == 2


def test_bad_fault_lab_is_an_error_not_a_silent_zero(tmp_path, capsys):
    results = _results(tmp_path)
    bad = tmp_path / "fault_lab.json"
    bad.write_text("{}", encoding="utf-8")
    assert scorecard.main([str(results), "--fault-lab", str(bad)]) == 1
    assert "fault lab" in capsys.readouterr().err
