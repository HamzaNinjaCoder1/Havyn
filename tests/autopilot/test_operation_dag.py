"""Operation DAG: deterministic order, cycle detection, failure propagation."""

from __future__ import annotations

import pytest

from src.network_autopilot.compiler.dag import DagError, OperationDag, OpStatus
from src.network_autopilot.compiler.operations import NetworkOperation


def _op(op_id: str, deps: tuple[str, ...] = ()) -> NetworkOperation:
    return NetworkOperation(
        operation_id=op_id, operation_type="noop", dependencies=deps
    )


def test_topological_order_respects_dependencies():
    dag = OperationDag()
    dag.add(_op("c", ("a", "b")))
    dag.add(_op("b", ("a",)))
    dag.add(_op("a"))
    assert list(dag.topo_order()) == ["a", "b", "c"]


def test_order_is_deterministic_for_the_same_graph_definition():
    ops = {"a": _op("a"), "b": _op("b", ("a",)), "c": _op("c", ("a",)), "d": _op("d", ("b", "c"))}

    first = OperationDag()
    for op_id in ["a", "b", "c", "d"]:
        first.add(ops[op_id])
    second = OperationDag()
    for op_id in ["a", "b", "c", "d"]:
        second.add(ops[op_id])
    assert list(first.topo_order()) == list(second.topo_order())

    # A different insertion order changes the documented tiebreak (insertion
    # index) but must still yield a VALID topological order of the same graph.
    third = OperationDag()
    for op_id in ["d", "c", "b", "a"]:
        third.add(ops[op_id])
    order = list(third.topo_order())
    assert set(order) == {"a", "b", "c", "d"}
    assert order.index("a") < order.index("b") and order.index("a") < order.index("c")
    assert order.index("b") < order.index("d") and order.index("c") < order.index("d")


def test_cycle_detection():
    dag = OperationDag()
    dag.add(_op("a", ("b",)))
    dag.add(_op("b", ("a",)))
    with pytest.raises(DagError, match="cycle"):
        dag.validate()


def test_missing_dependency_rejected():
    dag = OperationDag()
    dag.add(_op("a", ("ghost",)))
    with pytest.raises(DagError, match="unknown operation"):
        dag.validate()


def test_duplicate_operation_id_rejected():
    dag = OperationDag()
    dag.add(_op("a"))
    with pytest.raises(DagError, match="duplicate"):
        dag.add(_op("a"))


def test_failure_blocks_transitive_dependents():
    dag = OperationDag()
    for op_id, deps in [("a", ()), ("b", ("a",)), ("c", ("b",)), ("d", ())]:
        dag.add(_op(op_id, deps))
    blocked = dag.mark_failed("a", "boom")
    assert dag.nodes["a"].status is OpStatus.FAILED
    assert set(blocked) == {"b", "c"}
    assert dag.nodes["b"].status is OpStatus.BLOCKED
    assert dag.nodes["c"].status is OpStatus.BLOCKED
    assert dag.nodes["d"].status is OpStatus.PENDING  # independent: untouched
    assert "blocked by failed dependency" in dag.nodes["c"].note
