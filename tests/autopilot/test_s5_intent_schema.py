"""S5.1/S5.2: intent schema contract, lint, and the deterministic expander.

Every valid example must survive the full offline chain:
intent -> schema validation -> lint (no blocking findings) -> expansion ->
spec lint -> compile. Every invalid example must fail at its designed gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.network_autopilot.ai.expander import (
    EXPANDER_VERSION,
    ExpansionRefusal,
    expand_intent,
)
from src.network_autopilot.ai.intent import INTENT_VERSION, HavynIntent, export_json_schema
from src.network_autopilot.ai.lint import lint_intent
from src.network_autopilot.compiler import compile_spec
from src.network_autopilot.compiler.spec_lint import lint_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.ipam import plan_addressing

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "data" / "intents" / "examples"
INVALID = REPO / "data" / "intents" / "invalid"

VALID_IDS = sorted(p.stem for p in EXAMPLES.glob("*.json"))
INVALID_IDS = sorted(p.stem for p in INVALID.glob("*.json"))

# Examples whose expansion is expected to refuse (rule 9, by name).
EXPECTED_REFUSALS = {"i06-guest-isolation-refusal"}
# Examples that are schema-valid but must produce blocking lint findings.
LINT_GATE = {
    "i07-vlan-too-small": "contradiction",
    "i08-wireless-client-without-ap": "missing_information",
    "i09-wired-device-on-wireless": "contradiction",
    "i10-firewall-without-internet": "missing_information",
}


def load_intent(path: Path) -> HavynIntent:
    return HavynIntent.from_json(path.read_text(encoding="utf-8"))


def full_chain(intent: HavynIntent) -> NetworkSpec:
    """The complete offline acceptance chain for one intent."""
    result = lint_intent(intent)
    assert not result.blocking, f"blocking lint findings: {result.to_dicts()}"
    expansion = expand_intent(intent)
    spec = expansion.spec
    problems = lint_spec(spec)
    assert not problems, f"spec lint problems: {problems}"
    plan = plan_addressing(spec)
    compile_spec(spec, plan)
    return spec


# --- S5.1: schema -----------------------------------------------------------


@pytest.mark.parametrize("name", VALID_IDS)
def test_valid_examples_pass_schema(name: str) -> None:
    intent = load_intent(EXAMPLES / f"{name}.json")
    assert intent.intent_version == INTENT_VERSION


@pytest.mark.parametrize("name", INVALID_IDS)
def test_invalid_examples_fail_their_gate(name: str) -> None:
    raw = (INVALID / f"{name}.json").read_text(encoding="utf-8")
    if name in EXPECTED_REFUSALS:
        intent = HavynIntent.from_json(raw)
        with pytest.raises(ExpansionRefusal):
            full_chain(intent)
        return
    if name in LINT_GATE:
        intent = HavynIntent.from_json(raw)
        findings = lint_intent(intent).blocking
        assert findings, f"{name} should produce blocking lint findings"
        assert findings[0].kind == LINT_GATE[name]
        return
    with pytest.raises(ValueError):
        HavynIntent.from_json(raw)


def test_json_schema_export_is_versioned_and_complete() -> None:
    schema = export_json_schema()
    assert schema["title"].startswith("Havyn Intent Schema v")
    props = schema["properties"]
    for field in (
        "sites", "vlans", "servers", "routing", "internet",
        "firewall", "wireless", "iot", "requirements", "constraints",
    ):
        assert field in props, f"blueprint-required field missing: {field}"
    assert schema["required"] == ["name", "sites"]


def test_missing_field_rejected() -> None:
    with pytest.raises(ValueError):
        HavynIntent.model_validate({"intent_version": "1", "sites": []})


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValueError):
        HavynIntent.model_validate({
            "intent_version": "1",
            "name": "x",
            "sites": [{"id": "s"}],
            "not_a_field": True,
        })


def test_ledger_rejects_earned_statuses() -> None:
    with pytest.raises(ValueError):
        HavynIntent.model_validate({
            "intent_version": "1",
            "name": "x",
            "sites": [{"id": "s"}],
            "requirements": [
                {"id": "REQ-001", "statement": "s", "status": "verified"}
            ],
        })


# --- S5.2: expander ---------------------------------------------------------


@pytest.mark.parametrize("name", VALID_IDS)
def test_valid_examples_survive_full_offline_chain(name: str) -> None:
    intent = load_intent(EXAMPLES / f"{name}.json")
    spec = full_chain(intent)
    assert spec.devices, "expansion produced an empty spec"


def test_expansion_is_deterministic() -> None:
    intent = load_intent(EXAMPLES / "e03-campus-three-floors.json")
    first = expand_intent(intent)
    second = expand_intent(intent)
    assert first.spec.spec_hash() == second.spec.spec_hash()
    assert first.summary() == second.summary()


def test_requirement_ledger_maps_into_the_spec() -> None:
    intent = load_intent(EXAMPLES / "e18-long-document.json")
    expansion = expand_intent(intent)
    assert set(expansion.requirement_map) == {"REQ-001", "REQ-002", "REQ-003"}
    spec = expansion.spec
    by_id = {r.id: r for r in spec.requirements}
    assert by_id["R-002"].statement.startswith("Wireless laptops")
    assert by_id["R-003"].kind == "isolation"  # intent "security" -> spec "isolation"
    # every criterion traces to a mapped requirement or the engine's own
    # generic-check requirement
    mapped = set(expansion.requirement_map.values()) | {"R-ENG-001"}
    for criterion in spec.acceptance_criteria:
        assert set(criterion.requirement_ids) <= mapped


def test_generic_checks_never_prove_a_user_isolation_requirement() -> None:
    """A reachable flow is not proof of isolation (rule 7)."""
    intent = HavynIntent.model_validate(
        {
            "intent_version": "1",
            "name": "isolation-probe",
            "sites": [
                {
                    "id": "s1",
                    "areas": [
                        {
                            "id": "a1",
                            "kind": "area",
                            "host_groups": [
                                {
                                    "id": "pcs",
                                    "area": "a1",
                                    "count": 4,
                                    "device": "pc",
                                    "vlan": "v1",
                                }
                            ],
                        }
                    ],
                }
            ],
            "vlans": [
                {
                    "id": "v1",
                    "name": "Users",
                    "site": "s1",
                    "required_hosts": 10,
                    "dhcp": True,
                }
            ],
            "requirements": [
                {
                    "id": "REQ-001",
                    "statement": "Users are isolated from everyone else",
                    "type": "isolation",
                    "priority": "critical",
                    "source": "prompt",
                }
            ],
        }
    )
    spec = expand_intent(intent).spec
    isolation = next(r for r in spec.requirements if r.id == "R-001")
    assert isolation.kind == "isolation"
    assert not any(
        isolation.id in c.requirement_ids for c in spec.acceptance_criteria
    )
    assert any(
        c.requirement_ids == ["R-ENG-001"] for c in spec.acceptance_criteria
    )


def test_engine_choices_are_recorded_as_design_decisions() -> None:
    intent = load_intent(EXAMPLES / "e03-campus-three-floors.json")
    expansion = expand_intent(intent)
    assert expansion.decisions, "expander recorded no design decisions"
    for decision in expansion.spec.design_decisions:
        assert decision.source == "engine"


def test_multi_vlan_site_uses_router_on_a_stick() -> None:
    intent = load_intent(EXAMPLES / "e02-two-vlan-office.json")
    spec = expand_intent(intent).spec
    roles = {d.id: d.role.value for d in spec.devices}
    assert roles["office-r1"] == "router"
    subs = [a for a in spec.ip_assignments
            if a.device == "office-r1" and a.interface_hint]
    assert len(subs) == 2, "each VLAN needs a router sub-interface"


def test_multi_site_multi_vlan_uses_fastethernet_subinterfaces() -> None:
    """Router-PT (serial WAN) has no GigabitEthernet ports; the router-on-a-
    stick sub-interfaces must ride FastEthernet0/0 or preflight rejects the
    port as absent."""
    intent = HavynIntent.model_validate({
        "intent_version": "1",
        "name": "two-site-two-vlan",
        "sites": [
            {"id": "site1", "areas": [{"id": "a1", "host_groups": [
                {"id": "g1", "area": "a1", "count": 6, "device": "pc",
                 "vlan": "v1"},
                {"id": "g2", "area": "a1", "count": 6, "device": "pc",
                 "vlan": "v2"},
            ]}]},
            {"id": "site2", "areas": [{"id": "a1", "host_groups": [
                {"id": "g3", "area": "a1", "count": 6, "device": "pc",
                 "vlan": "v3"},
            ]}]},
        ],
        "vlans": [
            {"id": "v1", "name": "users 1", "site": "site1",
             "required_hosts": 20},
            {"id": "v2", "name": "users 2", "site": "site1",
             "required_hosts": 20},
            {"id": "v3", "name": "users 3", "site": "site2",
             "required_hosts": 20},
        ],
    })
    spec = expand_intent(intent).spec
    for device in spec.devices:
        if device.id.endswith("-r1"):
            assert device.model_hint == "Router-PT"
    subs = [a.interface_hint for a in spec.ip_assignments
            if a.device.endswith("-r1") and a.interface_hint]
    assert subs, "multi-VLAN site routers need sub-interface hints"
    for hint in subs:
        assert hint.startswith("FastEthernet0/0."), (
            f"Router-PT has no GigabitEthernet: got {hint!r}"
        )
    full_chain(intent)


def test_firewall_shape_replaces_the_site_router() -> None:
    intent = load_intent(EXAMPLES / "e08-firewall-perimeter.json")
    spec = expand_intent(intent).spec
    roles = {d.id: d.role.value for d in spec.devices}
    assert "office-r1" not in roles
    assert roles["fw"] == "firewall"
    assert any(c.test.kind == "flow" and c.test.expect == "blocked"
               for c in spec.acceptance_criteria if c.test.kind == "flow")
    assert spec.policies, "the perimeter policy is missing"


def test_static_hosts_do_not_collide_with_gateways_or_servers() -> None:
    intent = load_intent(EXAMPLES / "e15-static-addressing.json")
    spec = expand_intent(intent).spec
    ips = [a.ip for a in spec.ip_assignments if a.ip]
    assert len(ips) == len(set(ips)), f"duplicate static ip: {ips}"


def test_expander_refuses_guest_isolation_by_name() -> None:
    intent = load_intent(INVALID / "i06-guest-isolation-refusal.json")
    with pytest.raises(ExpansionRefusal) as excinfo:
        expand_intent(intent)
    assert "guest-isolation" in excinfo.value.code


def test_lint_reports_contradictions_and_missing_information() -> None:
    too_small = load_intent(INVALID / "i07-vlan-too-small.json")
    findings = lint_intent(too_small)
    codes = {f.code for f in findings.contradictions}
    assert "vlan-too-small" in codes

    no_ap = load_intent(INVALID / "i08-wireless-client-without-ap.json")
    findings = lint_intent(no_ap)
    codes = {f.code for f in findings.missing_information}
    assert "wireless-client-without-ap" in codes

    fw = load_intent(INVALID / "i10-firewall-without-internet.json")
    findings = lint_intent(fw)
    codes = {f.code for f in findings.missing_information}
    assert "firewall-without-internet" in codes
