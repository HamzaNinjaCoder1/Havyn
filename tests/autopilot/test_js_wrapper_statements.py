"""Live-found regression: a trailing `;` inside String(...) broke PT with
`SyntaxError: Parse error` (js_add_device/js_add_link return statements)."""

from __future__ import annotations

from src.network_autopilot.adapter.upstream import js_commands as js


def test_statement_expression_is_normalized_in_wrapper():
    body = js.wrap_with_report(js.js_add_device("r1", 0, "2911", 10, 20), "")
    # no `;)` sequence: the statement semicolon must be stripped before String()
    assert ";)" not in body.replace(")();", "")
    assert 'String(lwAddDevice("r1", 0, "2911", 10, 20))' in body


def test_wrapper_keeps_plain_expressions_untouched():
    body = js.wrap_with_report("'A'", "")
    assert "return String('A');" in body


def test_all_statement_builders_survive_the_wrapper():
    for expr in (
        js.js_add_device("r1", 0, "2911", 1, 2),
        js.js_add_link("r1", "Gi0/0", "s1", "Fa0/1", "straight"),
    ):
        body = js.wrap_with_report(expr, "")
        # paren balance sanity: every '(' has its ')'
        assert body.count("(") == body.count(")")


def test_add_link_uses_cable_name_not_numeric_code():
    # found live: lwAddLink(..., 8107) answers false for PC ports; the
    # upstream-verified call is the GLOBAL addLink with a cable NAME.
    expr = js.js_add_link("a", "Fa0", "b", "Fa0", "cross")
    assert expr.startswith("addLink(")
    assert '"cross"' in expr
    assert "8107" not in expr and "lwAddLink" not in expr


def test_cable_name_for_maps_aliases_and_rejects_unknown():
    assert js.cable_name_for("auto") == "auto"
    assert js.cable_name_for("crossover") == "cross"
    try:
        js.cable_name_for("unobtanium")
    except ValueError as exc:
        assert "unobtanium" in str(exc)
    else:
        raise AssertionError("unknown media must raise ValueError")
