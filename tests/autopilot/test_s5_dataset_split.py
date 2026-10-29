"""S5.14: family-aware split must never leave train empty when >= 2 families."""

from __future__ import annotations

import random
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "havyn-stage5" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assemble_dataset import split_families  # noqa: E402


def test_single_family_stays_in_train() -> None:
    train, val, held = split_families(["only"], random.Random(1))
    assert train == {"only"}
    assert val == set()
    assert held == set()


def test_two_families_keep_one_in_train() -> None:
    train, val, held = split_families(["a", "b"], random.Random(1))
    assert len(train) == 1
    assert len(val) == 1
    assert held == set()
    assert train | val | held == {"a", "b"}


def test_families_are_disjoint_and_complete() -> None:
    families = [f"f{i}" for i in range(20)]
    train, val, held = split_families(families, random.Random(7))
    assert train and val and held
    assert not (train & val or train & held or val & held)
    assert train | val | held == set(families)
    assert len(train) >= 1


# --- regression coverage for the prompt-task key collapse and row semantics ---

import json  # noqa: E402

from assemble_dataset import prompt_gate  # noqa: E402
from generate_prompts import intent_key  # noqa: E402
from validate_training_split import _leak_key, validation_targets  # noqa: E402


def test_intent_key_uses_id_when_hash_absent() -> None:
    # valid.jsonl rows have no `hash`; reading it alone gave None for every
    # row and paired every prompt with one intent.
    assert intent_key({"id": "gen-abc"}) == "gen-abc"
    assert intent_key({"hash": "h1", "id": "gen-abc"}) == "h1"
    assert intent_key({}) is None


def test_template_prompts_are_gated_by_construction() -> None:
    assert prompt_gate({"teacher_generated": False}, {}, False) == "template_construction"
    assert prompt_gate({"teacher_generated": False}, {}, True) == "template_construction"


def test_teacher_prompts_require_independent_model_accept() -> None:
    # A dry-run ACCEPT is not evidence: model_verified must be true.
    assert prompt_gate({"teacher_generated": True}, {"queue": "ACCEPT"}, False) is None
    assert prompt_gate({"teacher_generated": True}, {"queue": "REVIEW"}, True) is None
    assert (
        prompt_gate({"teacher_generated": True}, {"queue": "ACCEPT"}, True)
        == "independent_model_roundtrip_accept"
    )


def test_impossible_enhancement_has_no_after_target() -> None:
    assert validation_targets(
        "enhancement", {"kind": "impossible", "after_intent": {}}
    ) == ["before_intent"]
    assert validation_targets(
        "enhancement", {"kind": "mutation", "after_intent": {"name": "x"}}
    ) == ["before_intent", "after_intent"]


def test_clarification_validates_the_completed_intent() -> None:
    # prompt_intent is deliberately incomplete for a missing-info record.
    assert validation_targets(
        "clarification", {"kind": "missing:host-group-count"}
    ) == ["complete_intent"]
    assert validation_targets(
        "clarification", {"kind": "complete"}
    ) == ["prompt_intent", "complete_intent"]


def test_leak_scope_shares_prompt_but_not_enhancement_payload() -> None:
    payload = {"prompt": "same base request", "expected_output": {"patch": ["a"]}}
    assert _leak_key("prompt", payload) == "same base request"
    assert json.loads(_leak_key("enhancement", payload)) == payload
