"""Offline tests for the S5.4 bake-off scoring half (no GPU involved)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "havyn-stage5" / "scripts"
for p in (str(REPO), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from benchmark_harness import (  # noqa: E402
    compare_intents,
    extract_json,
    render_prompt,
    score_generation,
    summarise,
)
from network_autopilot.ai.intent import HavynIntent  # noqa: E402

EXAMPLES = REPO / "data" / "intents" / "examples"


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_extract_json_finds_balanced_object() -> None:
    text = 'Sure!\n```json\n{"a": {"b": "c { d"}, "e": 1}\n```\nDone.'
    assert extract_json(text) == '{"a": {"b": "c { d"}, "e": 1}'


def test_extract_json_handles_braces_inside_strings() -> None:
    text = '{"x": "}"}'
    assert extract_json(text) == text


def test_extract_json_none_when_no_object() -> None:
    assert extract_json("I cannot build that network.") is None


def test_render_prompt_contains_no_invented_information() -> None:
    intent = HavynIntent.model_validate(load("e01-small-office.json"))
    prompt = render_prompt(intent)
    assert intent.name in prompt
    for site in intent.sites:
        assert site.id in prompt
    for vlan in intent.vlans:
        assert vlan.id in prompt


def test_render_prompt_roman_urdu() -> None:
    intent = HavynIntent.model_validate(load("e01-small-office.json"))
    prompt = render_prompt(intent, language="roman-urdu")
    assert "Aik network banao" in prompt


def test_score_generation_valid_response() -> None:
    body = load("e01-small-office.json")
    score = score_generation(
        prompt_id="t1", category="en", response=f"```json\n{json.dumps(body)}\n```",
        expected_intent=body,
    )
    assert score.is_valid_json
    assert score.is_schema_valid
    assert score.lint_pass
    assert score.field_accuracy == 1.0


def test_score_generation_refusal_counts_as_refused() -> None:
    score = score_generation(
        prompt_id="t2", category="refusal",
        response="I cannot complete this request: the VLAN capacity is too "
                 "small for the hosts requested. Please clarify.",
        expect_refusal=True,
    )
    assert score.correct_refusal is True


def test_score_generation_intent_where_refusal_expected_is_wrong() -> None:
    body = load("i07-vlan-too-small.json") if (EXAMPLES / "i07-vlan-too-small.json").exists() else None
    if body is None:
        pytest.skip("invalid example missing")
    score = score_generation(
        prompt_id="t3", category="refusal", response=str(body),
        expect_refusal=True,
    )
    # A confident intent for an invalid request must not count as a refusal.
    assert score.correct_refusal is False


def test_compare_intents_counts_field_mismatches() -> None:
    expected = HavynIntent.model_validate(load("e01-small-office.json"))
    actual_body = load("e01-small-office.json")
    actual_body["sites"][0]["id"] = "renamed"
    actual = HavynIntent.model_validate(actual_body)
    result = compare_intents(expected, actual)
    assert result["field_accuracy"] < 1.0
    assert result["fields_matched"] < result["fields_total"]


def test_summarise_aggregates_percentages() -> None:
    body = load("e01-small-office.json")
    good = score_generation("a", "en", json.dumps(body), expected_intent=body)
    bad = score_generation("b", "en", "no json here")
    summary = summarise([good, bad])
    assert summary["examples"] == 2
    assert summary["valid_json_pct"] == 50.0
    assert summary["schema_valid_pct"] == 50.0


def test_summarise_empty() -> None:
    assert summarise([]) == {"examples": 0}
