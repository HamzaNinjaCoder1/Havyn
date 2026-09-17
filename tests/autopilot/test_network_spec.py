"""NetworkSpec: typed, strict, hash-stable, reference-integrity checked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.network_autopilot.core.ids import build_fingerprint, idempotency_key
from src.network_autopilot.core.network_spec import NetworkSpec

EXAMPLE = Path("examples") / "three_floor_office.spec.json"


def _load_example() -> NetworkSpec:
    return NetworkSpec.from_json(EXAMPLE.read_text(encoding="utf-8"))


def test_golden_example_spec_validates():
    spec = _load_example()
    assert spec.name == "three-floor-office"
    assert len(spec.devices) == 11
    assert len(spec.links) == 10
    assert len(spec.acceptance_criteria) == 8
    # Every requirement is traceable to at least one criterion
    linked = {rid for c in spec.acceptance_criteria for rid in c.requirement_ids}
    assert linked == {r.id for r in spec.requirements}


def test_hash_stable_across_roundtrip():
    spec = _load_example()
    reparsed = NetworkSpec.from_json(spec.to_json())
    assert spec.spec_hash() == reparsed.spec_hash()


def test_free_form_llm_output_is_rejected():
    with pytest.raises(ValueError):
        NetworkSpec.from_json(json.dumps({"name": "x", "whatever": {"deep": [1, 2, 3]}}))
    with pytest.raises(ValueError):
        NetworkSpec.from_json("not json at all")


def test_broken_device_reference_rejected():
    raw = {
        "name": "bad",
        "devices": [{"id": "r1", "role": "router"}],
        "links": [
            {
                "id": "lnk-1",
                "a": {"device": "r1"},
                "b": {"device": "missing-switch"},
            }
        ],
    }
    with pytest.raises(ValueError, match="unknown device"):
        NetworkSpec.model_validate(raw)


def test_criterion_without_requirement_rejected():
    raw = {
        "name": "bad",
        "devices": [{"id": "pc1", "role": "pc"}],
        "requirements": [{"id": "R-1", "statement": "exists"}],
        "acceptance_criteria": [
            {
                "id": "A-1",
                "requirement_ids": ["R-404"],
                "test": {"kind": "structural", "check": "device_exists", "target": "pc1"},
            }
        ],
    }
    with pytest.raises(ValueError, match="unknown requirement"):
        NetworkSpec.model_validate(raw)


def test_duplicate_device_ids_rejected():
    raw = {
        "name": "bad",
        "devices": [
            {"id": "pc1", "role": "pc"},
            {"id": "pc1", "role": "server"},
        ],
    }
    with pytest.raises(ValueError, match="duplicate device"):
        NetworkSpec.model_validate(raw)


def test_subnet_must_state_or_request_a_size():
    base = {"id": "net-1", "purpose": "users"}
    with pytest.raises(ValueError, match="exactly one"):
        NetworkSpec.model_validate(
            {
                "name": "bad",
                "devices": [{"id": "pc1", "role": "pc"}],
                "subnets": [dict(base, cidr="10.0.0.0/24", required_hosts=50)],
            }
        )


def test_idempotency_key_is_order_insensitive():
    assert idempotency_key({"a": 1, "b": 2}) == idempotency_key({"b": 2, "a": 1})
    assert idempotency_key({"a": 1}) != idempotency_key({"a": 2})


def test_build_fingerprint_deterministic():
    components = {"compiler": "0.1.0", "adapter": "0.1.0", "packet_tracer": "9.0"}
    first = build_fingerprint("abc123", components, layout_seed=42)
    second = build_fingerprint("abc123", dict(reversed(list(components.items()))), layout_seed=42)
    assert first == second
    assert build_fingerprint("different", components, layout_seed=42) != first
