"""S2.7: the engine-independence blocker.

The README promises the engine is self-contained — standard library plus
`mcp` and `pydantic`, nothing else. This test scans every module under
src/network_autopilot and fails on any other absolute import (scripts/,
tests/, GUI or web frameworks, new third-party wheels). It is the merge
blocker: a packaging violation can never go green, locally or in CI.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ENGINE_ROOT = pathlib.Path(__file__).parents[2] / "src" / "network_autopilot"

#: Third-party wheels the engine may use (README's list). Everything else
#: absolute must be the standard library or the package itself.
ALLOWED_THIRD_PARTY = frozenset({"mcp", "pydantic"})
ALLOWED_SELF = frozenset({"network_autopilot", "src"})


def _forbidden_imports() -> list[str]:
    """Every absolute import the engine is not allowed, as file:line names."""
    stdlib = sys.stdlib_module_names | {"__future__"}
    out: list[str] = []
    for path in sorted(ENGINE_ROOT.rglob("*.py")):
        tree = ast.parse(
            path.read_text(encoding="utf-8-sig"), filename=str(path)
        )
        for node in ast.walk(tree):
            tops: list[str] = []
            if isinstance(node, ast.Import):
                tops = [(a.name or "").split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # relative: inside the package by construction
                tops = [(node.module or "").split(".")[0]]
            for top in tops:
                if not top:
                    continue
                if (
                    top in stdlib
                    or top in ALLOWED_THIRD_PARTY
                    or top in ALLOWED_SELF
                ):
                    continue
                rel = path.relative_to(ENGINE_ROOT.parents[1])
                out.append(f"{rel}:{node.lineno}: forbids import of {top!r}")
    return sorted(out)


def test_engine_imports_nothing_but_stdlib_mcp_and_pydantic():
    violations = _forbidden_imports()
    assert violations == [], (
        "engine independence violated — the engine must stay self-contained:\n"
        + "\n".join(violations)
    )
