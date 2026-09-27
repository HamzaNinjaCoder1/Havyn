"""JS builders: an unescaped device name is arbitrary code execution.

These tests are the offline half of repo rule 1: every dynamic string must
travel through json.dumps, so a hostile name can never break out of its JS
string literal inside `new Function()`.
"""

from __future__ import annotations

import json

import pytest

from src.network_autopilot.adapter.packet_tracer import js_commands as js

HOSTILE_NAME = 'x"); reportResult("pwned'


def test_hostile_device_name_cannot_break_out():
    code = js.js_add_device(HOSTILE_NAME, 0, "2911", 10, 20)
    # The name appears exactly as a json.dumps literal...
    assert json.dumps(HOSTILE_NAME) in code
    # ...and never as raw interpolation (which would terminate the literal).
    assert f'lwAddDevice("{HOSTILE_NAME}"' not in code
    # The literal decodes back to the original name.
    assert json.loads(json.dumps(HOSTILE_NAME)) == HOSTILE_NAME


def test_hostile_name_newlines_stay_escaped():
    hostile = "device\nname"
    code = js.js_add_device(hostile, 0, "2911", 0, 0)
    # A raw newline inside the queued JS would split the command; json.dumps
    # keeps it as \n inside one line.
    assert "\n" not in code.replace("\\n", "")


def test_report_wrapper_has_exactly_one_call_and_pt_error_guard():
    code = js.wrap_with_report("(1+1)", "function reportResult(d){/*upstream*/}")
    assert code.count("reportResult((function") == 1
    assert "PT_ERROR" in code
    assert "function reportResult" in code  # upstream definition kept, not duplicated


def test_cable_code_validation():
    assert js.cable_code_for("auto") == 8107
    assert js.cable_code_for("fiber") == 8103
    with pytest.raises(ValueError):
        js.cable_code_for("twisted-pair")


def test_non_int_coordinates_rejected():
    with pytest.raises(ValueError):
        js.js_add_device("PC1", 8, "PC", "100", 100)  # type: ignore[arg-type]


def test_save_js_carries_its_own_readback():
    statements, final = js.js_save_project_statements("C:/temp", "net.pkt")
    joined = "".join(statements) + final
    assert "fileSaveAsNoPrompt" in joined
    assert "fileExists" in joined  # the in-band postcondition
    assert json.dumps("C:/temp") in joined


def test_enum_tables_match_the_extension_runtime():
    assert js.DEVICE_TYPE_CODES["router"] == 0
    assert js.DEVICE_TYPE_CODES["switch"] == 1
    assert js.DEVICE_TYPE_CODES["pc"] == 8
    assert js.DEVICE_TYPE_CODES["server"] == 9
    assert js.CABLE_CODES["straight"] == 8100
    assert js.CABLE_CODES["serial"] == 8106
