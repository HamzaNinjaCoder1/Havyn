from __future__ import annotations

from src.network_autopilot.compiler.layout import build_layout, validate_layout
from src.network_autopilot.compiler import CompileError, compile_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing


def _spec() -> NetworkSpec:
    return NetworkSpec.model_validate(
        {
            "name": "layout-quality",
            "devices": [
                {"id": "r1", "role": "router"},
                {"id": "r2", "role": "router"},
                {"id": "s1", "role": "switch"},
                {"id": "s2", "role": "switch"},
                {"id": "pc1", "role": "pc"},
                {"id": "pc2", "role": "pc"},
            ],
            "links": [
                {"id": "wan", "a": {"device": "r1"}, "b": {"device": "r2"}},
                {"id": "lan1", "a": {"device": "r1"}, "b": {"device": "s1"}},
                {"id": "lan2", "a": {"device": "r2"}, "b": {"device": "s2"}},
                {"id": "host1", "a": {"device": "s1"}, "b": {"device": "pc1"}},
                {"id": "host2", "a": {"device": "s2"}, "b": {"device": "pc2"}},
            ],
        }
    )


def test_layout_is_stable_and_hierarchical():
    spec = _spec()
    first = build_layout(spec)
    second = build_layout(spec)
    assert first.positions == second.positions
    assert first.valid
    assert first.positions["r1"][1] == first.positions["r2"][1] < first.positions["s1"][1]
    assert first.positions["s1"][1] < first.positions["pc1"][1]


def test_layout_validator_detects_overlap_and_missing_position():
    spec = _spec()
    issues = validate_layout(spec, {"r1": (100, 100), "r2": (100, 100)})
    kinds = {issue.kind for issue in issues}
    assert "overlap" in kinds
    assert "missing_position" in kinds


def test_explicit_port_collision_is_rejected_before_execution():
    raw = _spec().model_dump(mode="json")
    raw["links"][1]["a"]["interface_hint"] = "GigabitEthernet0/0"
    raw["links"][2]["a"]["interface_hint"] = "GigabitEthernet0/0"
    spec = NetworkSpec.model_validate(raw)
    try:
        compile_spec(spec, plan_addressing(spec))
    except CompileError as exc:
        assert "already_used" in str(exc)
    else:
        raise AssertionError("compiler accepted two links on the same explicit port")
