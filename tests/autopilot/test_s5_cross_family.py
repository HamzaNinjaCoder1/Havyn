"""Cross-family review runner: package, precheck, baseline and verdict parsing."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "havyn-stage5" / "scripts"
for p in (str(REPO), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import cross_family_review as cfr  # noqa: E402


def test_package_sha_and_offline_precheck_pass() -> None:
    report = cfr.precheck(run_engine=True, write=False)
    assert report["input_package"]["sha256"] == (
        "f7a0a1f4edb1a3a16b990878e2333cc00a5b6a0aa141a6891d8ab418c02bc492")
    assert report["input_package"]["records"] == 56
    assert report["engine"]["verified"] == 56
    assert report["engine"]["failures"] == []
    assert report["issues"] == []
    assert report["passed"] is True


def test_language_guess_distinguishes_the_three_labels() -> None:
    assert cfr.language_guess("Design a network with four PCs and a VLAN.") == "en"
    assert cfr.language_guess("Har area ko apni alag VLAN par rakhein.") == "roman-urdu"
    assert cfr.language_guess(
        "Har area apni alag VLAN pe ho, so that traffic separate rahe."
    ) == "mixed"


def test_baseline_is_the_resolved_pool() -> None:
    base = cfr.baseline_map()
    assert len(base) == 56
    counts = {v: sum(1 for e in base.values() if e["verdict"] == v)
              for v in ("ACCEPT", "REVIEW", "REJECT")}
    assert counts == {"ACCEPT": 55, "REVIEW": 1, "REJECT": 0}
    assert base["oc-028"]["source"].startswith("owner-decision")


def _valid_review(rid: str = "oc-001") -> dict:
    return {
        "id": rid,
        "recovered_intent_summary": "one LAN",
        "missing_facts": [],
        "lost_requirements": [],
        "invented_facts": [],
        "contradictions": [],
        "ambiguity_notes": [],
        "human_likeness": 0.9,
        "fidelity": 1.0,
        "verdict": "ACCEPT",
        "reason": "faithful",
    }


def test_normalize_review_accepts_a_complete_verdict() -> None:
    verdict, error = cfr.normalize_review(_valid_review(), "oc-001")
    assert error is None and verdict["verdict"] == "ACCEPT"


def test_normalize_review_rejects_missing_keys_and_bad_verdicts() -> None:
    bad = _valid_review()
    bad.pop("reason")
    verdict, error = cfr.normalize_review(bad, "oc-001")
    assert verdict is None and "missing keys" in error

    bad = _valid_review()
    bad["verdict"] = "PASS"
    verdict, error = cfr.normalize_review(bad, "oc-001")
    assert verdict is None and "invalid verdict" in error

    verdict, error = cfr.normalize_review(_valid_review(), "oc-002")
    assert verdict is None and "id mismatch" in error


def test_model_family_comes_from_the_registry() -> None:
    assert cfr.model_family(
        "mistralai/Mistral-Small-24B-Instruct-2501") == "Mistral"
    assert cfr.model_family("Qwen/Qwen3-8B") == "Qwen3"
