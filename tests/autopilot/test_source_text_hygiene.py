"""Source text hygiene: no double-encoded (mojibake) strings ship.

The build-report header, the DAG notes and the acceptance summary lines are
printed to the user. A UTF-8 character decoded as cp1252 and re-saved as
UTF-8 shows up as several garbage characters instead of the real one - which
is exactly what 26 lines in `compiler/executor.py` and `orchestrator.py`
contained before this test existed (``â€"`` for an em-dash, ``â†’`` for an
arrow, ``Â§`` for a section sign). The corruption is in the FILE BYTES, not
in how a tool renders them: reading the file and printing
``unicode_escape`` proves it.

Detection is the round trip itself, so it cannot flag legitimate prose: a
candidate run is a cp1252-rendered UTF-8 lead byte (bytes C2-F4 render as
``Â``..``ô``) followed by cp1252-rendered continuation artifacts (the Latin-1
``\\u00a0-\\u00bf`` range plus the cp1252 ``0x80-0x9F`` punctuation maps ``€ " —
™ ‹ › Š ž ...``). It is a violation only when re-encoding the run as cp1252
and decoding that as UTF-8 SUCCEEDS - i.e. the bytes really were UTF-8 once,
which is precisely the signature of the bug. A Spanish ``ñ`` followed by a
space never matches; a corrupted em-dash always does.

Red on the un-fixed sources (the corruption is in the files themselves);
mutation: re-insert one triplet and the scan finds it again.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).parents[2]
#: Both shipped Python surfaces (the engine and the runner/build entry
#: points) plus the user-facing Markdown docs. ISSUES.md, QA_REPORT.md and
#: the HTML trackers are gitignored, never scanned here.
_SCAN_ROOTS = (_ROOT / "src" / "network_autopilot", _ROOT / "scripts")
_SCAN_DOCS = tuple(
    p for p in (_ROOT / "README.md", _ROOT / "CHANGELOG.md", _ROOT / "AGENTS.md")
    if p.is_file()
)

#: cp1252 renderings of UTF-8 lead bytes (C2-F4 -> Â .. ô).
_LEAD_RANGE = "\u00c2-\u00f4"

#: cp1252 renderings of UTF-8 continuation bytes: 0xA0-0xBF stay Latin-1;
#: 0x80-0x9F map to these characters instead.
_CONT_RANGE = (
    "\u00a0-\u00bf"                        # Â§ Ã© ... (0xA0-0xBF)
    "\u2013\u2014\u2018\u2019\u201c\u201d"  # – — ' ' " "
    "\u2020\u2021\u2022\u2026\u2030"        # † ‡ • … ‰
    "\u2039\u203a\u20ac\u2122"              # ‹ › € ™
    "\u0160\u0161\u017d\u017e"              # Š š Ž ž
)

_CANDIDATE = re.compile(f"([{_LEAD_RANGE}][{_CONT_RANGE}]+)")


def _is_double_encoded(run: str) -> bool:
    """True only when the run decodes as the UTF-8 bytes it once was."""
    try:
        run.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False
    return True


def _violations() -> list[str]:
    """Every `file:line` whose text carries a confirmed double-encoding."""
    out: list[str] = []
    for root in _SCAN_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8-sig")
            for lineno, line in enumerate(text.splitlines(), 1):
                hits = [m for m in _CANDIDATE.findall(line) if _is_double_encoded(m)]
                if hits:
                    rel = path.relative_to(_ROOT)
                    out.append(f"{rel}:{lineno}: {sorted(set(hits))} in {line.strip()!r}")
    for path in _SCAN_DOCS:
        text = path.read_text(encoding="utf-8-sig")
        for lineno, line in enumerate(text.splitlines(), 1):
            hits = [m for m in _CANDIDATE.findall(line) if _is_double_encoded(m)]
            if hits:
                rel = path.relative_to(_ROOT)
                out.append(f"{rel}:{lineno}: {sorted(set(hits))} in {line.strip()!r}")
    return out


def test_source_text_has_no_double_encoded_strings():
    found = _violations()
    assert found == [], (
        "double-encoded UTF-8 (mojibake) in shipped source - every one of "
        "these renders as garbage in a report, a DAG note or a summary "
        "line:\n" + "\n".join(found)
    )
