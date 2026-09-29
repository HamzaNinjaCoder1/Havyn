"""Scale: the engine must compile and budget LARGE topologies, not just labs.

The fixed mutation budget (25) used to abort anything bigger than a 12-device
lab. `MutationBudget.for_operations` now scales with the DAG, so a big build is
authorized to actually finish. These tests say how big 'big' currently is and
catch a regression the moment the budget stops scaling.
"""

from __future__ import annotations

import pathlib
import re
import time

import pytest

from src.network_autopilot.compiler.compiler import compile_spec
from src.network_autopilot.core.network_spec import NetworkSpec
from src.network_autopilot.core.safety import MutationBudget
from src.network_autopilot.ipam import plan_addressing


def _large_spec(cores: int = 4, access: int = 16, pcs: int = 16) -> dict:
    """A campus core/access/edge: L3 core mesh, 24-port access, one PC per port.

    Sized against the real catalog — a 2911 has two onboard ports, so the core
    is 3560-24PS (24 ports) rather than a router, which is how a real campus of
    this size is built anyway.
    """
    devices = [{"id": f"c{i}", "role": "l3_switch"} for i in range(cores)]
    devices += [{"id": f"s{i}", "role": "switch"} for i in range(access)]
    devices += [{"id": f"pc{i}", "role": "pc"} for i in range(pcs)]

    links = []
    # Full-mesh core so every L3 switch can route to every other.
    for i in range(cores):
        for j in range(i + 1, cores):
            links.append({"id": f"lc{i}c{j}", "a": {"device": f"c{i}"},
                          "b": {"device": f"c{j}"}})
    # Access uplinks: switch i homed on core (i // per_core).
    per_core = max(access // cores, 1) if cores else 1
    for i in range(access):
        links.append({"id": f"ls{i}", "a": {"device": f"c{i // per_core}"},
                      "b": {"device": f"s{i}"}})
    # One PC per access switch.
    for i in range(pcs):
        links.append({"id": f"lp{i}", "a": {"device": f"s{i % access}"},
                      "b": {"device": f"pc{i}"}})

    # One /28 access subnet per switch: gateway on the core, PC on the edge.
    subnets, assignments = [], []
    for i in range(access):
        sid = f"net{i}"
        subnets.append({"id": sid, "cidr": f"10.{i}.0.0/28"})
        gw, host = f"10.{i}.0.1", f"10.{i}.0.2"
        assignments.append({"device": f"c{i // per_core}", "subnet": sid, "ip": gw})
        if i < pcs:
            assignments.append({"device": f"pc{i}", "subnet": sid, "ip": host})

    return {
        "spec_version": "1",
        "name": "scale-campus",
        "devices": devices,
        "links": links,
        "subnets": subnets,
        "ip_assignments": assignments,
    }


def test_large_topology_compiles_within_budget():
    """A 36-device campus: the DAG is complete and the budget covers every op."""
    spec = NetworkSpec.model_validate(_large_spec())
    plan = plan_addressing(spec)

    t0 = time.time()
    result = compile_spec(spec, plan)
    elapsed = time.time() - t0

    ops = [n.operation for n in result.dag.nodes.values()]
    mutators = [o for o in ops if o.classification.value == "configure"
                or o.operation_type.startswith(("create", "install"))]

    # Sanity: the topology actually materialized into operations.
    # 4 core L3 + 16 access + 16 edge = 36 devices; 6 core mesh + 16 uplink + 16 edge.
    assert len(spec.devices) == 36
    assert len(spec.links) == 38
    assert len(ops) > len(mutators) > 0
    assert elapsed < 60.0, f"compile took {elapsed:.1f}s — scale regression"

    # The scaling guarantee: no mutating op is refused for lack of budget.
    budget = MutationBudget.for_operations(len(ops))
    assert budget.max_mutations >= len(mutators), (
        f"budget {budget.max_mutations} < {len(mutators)} mutating ops: "
        "the engine would abort this build partway"
    )
    # And the configured default (25) would NOT have covered it — proving the
    # fixed budget is what used to break big labs.
    assert len(mutators) > MutationBudget().max_mutations


def test_budget_scaling_formula_is_monotonic():
    """More work must mean a budget that grows with it, up to the ceiling."""
    small = MutationBudget.for_operations(10)
    medium = MutationBudget.for_operations(100)
    large = MutationBudget.for_operations(1000)

    assert small.max_mutations < medium.max_mutations < large.max_mutations
    assert large.max_mutations <= MutationBudget.GLOBAL_CEILING


def test_budget_formula_matches_the_documented_numbers():
    """S1.13/F10: the README states the budget formula; the code must agree.

    The documented numbers are parsed out of the README (not copied here),
    so changing the formula without updating the docs — or vice versa —
    turns this red. Single truth, checked both ways.
    """
    readme = (pathlib.Path(__file__).parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    formula = re.search(r"min\((\d+)\s*\+\s*(\d+)\*ops,\s*(\d+)\)", readme)
    assert formula, "README must state the budget as min(<base> + <head>*ops, <ceiling>)"
    base, head, ceiling = (int(g) for g in formula.groups())
    default = re.search(r"unsized default is `(\d+)`", readme)
    assert default, "README must state the unsized default budget"
    assert MutationBudget().max_mutations == int(default.group(1))
    assert MutationBudget.GLOBAL_CEILING == ceiling
    for ops in (0, 1, 10, 100, 100_000):
        assert MutationBudget.for_operations(ops).max_mutations == min(
            base + head * ops, ceiling
        ), f"budget formula drifted for {ops} ops"


def test_large_compile_is_deterministic():
    """Two compiles of the same big spec must agree byte for byte."""
    spec = NetworkSpec.model_validate(_large_spec())
    plan = plan_addressing(spec)

    first = compile_spec(spec, plan).to_dict()
    second = compile_spec(spec, plan).to_dict()

    assert first == second


def test_compile_reports_op_count_for_budget_sizing():
    """The compiled DAG carries the op count the budget is sized from."""
    spec = NetworkSpec.model_validate(_large_spec())
    plan = plan_addressing(spec)

    result = compile_spec(spec, plan)
    ops = [n.operation for n in result.dag.nodes.values()]
    budget = MutationBudget.for_operations(len(ops))

    # Every mutating op must be consumable: this is the exact check the
    # executor performs, so a regression here is a real build abort.
    consumed = 0
    for op in ops:
        if op.classification.value in ("configure",) or op.operation_type.startswith(
            ("create", "install")
        ):
            assert budget.can_consume(1), (
                f"op {op.operation_id} refused: budget "
                f"{budget.max_mutations} exhausted at {consumed}"
            )
            budget.consume(1)
            consumed += 1
    assert consumed == len(
        [o for o in ops
         if o.classification.value == "configure"
         or o.operation_type.startswith(("create", "install"))]
    )


def test_budget_ceiling_is_enforced():
    """A runaway DAG is still capped — scaling is not a blank cheque."""
    budget = MutationBudget.for_operations(100_000)
    assert budget.max_mutations == MutationBudget.GLOBAL_CEILING
