"""BridgeAdapter receipt semantics, offline: the transport is faked, the
receipt logic is real. A timeout stays UNKNOWN, PT_ERROR is FAILED, and only
a passing read-back postcondition confirms."""

from __future__ import annotations

import json

from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.observability import Observable
from src.network_autopilot.core.receipts import OperationReceipt, OperationResult

TEST_TOKEN = "test-token-long-enough-0123456789abcdef"


def _adapter_with_answers(*answers: str, exists: str | None = "no") -> BridgeAdapter:
    """Fake transport that answers by *intent*, not by call position.

    ``exists`` is the answer to "is this device in the workspace?" ("yes",
    "no", or ``None`` for no answer at all); ``answers`` are consumed in order
    by the mutations themselves. Classifying by intent matters because
    ``device_exists`` is answered from the shared topology snapshot, so how
    many reads a call makes is an implementation detail of the read cache -
    these tests are about receipt semantics, not about call counts.
    """
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    queue = list(answers)
    present = {"value": exists == "yes"}

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "JSON.stringify" in text and "devices" in text:
            if exists is None:
                return Observable.unknown("no answer arrived")
            if present["value"]:
                return Observable.known(
                    '{"devices":[{"name":"R1","model":"2911","ports":[]}]}'
                )
            return Observable.known('{"devices":[]}')
        answer = queue.pop(0) if queue else None
        if answer is None:
            return Observable.unknown("no answer arrived")
        # A mutation that PT accepted puts the device in the workspace, so the
        # read-back that follows has to see it - that is what "confirmed" means.
        if answer.startswith("OK") or answer == "yes":
            present["value"] = True
        return Observable.known(answer)

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter


def test_create_device_confirmed_when_readback_passes():
    # answers: precondition 'no' -> add 'OK' -> read-back 'yes'
    receipt = _adapter_with_answers("OK", "yes", exists="no").create_device("R1", "router")
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed
    assert receipt.postconditions[0].passed is True


def test_timeout_is_unknown_never_success():
    # precondition answers, then the mutation itself gets no answer
    receipt = _adapter_with_answers(exists="no").create_device("R1", "router")
    assert receipt.result is OperationResult.UNKNOWN
    assert not receipt.confirmed


def test_pt_error_is_failed_with_evidence():
    receipt = _adapter_with_answers("PT_ERROR: bad model", exists="no").create_device("R1", "router")
    assert receipt.result is OperationResult.FAILED
    assert "bad model" in (receipt.error or "")
    assert receipt.executed and "PT_ERROR" in receipt.executed["pt_result"]


def test_precondition_unknown_fails_closed():
    # nothing answers: existence cannot be verified, so no mutation happens
    receipt = _adapter_with_answers(exists=None).create_device("R1", "router")
    assert receipt.result is OperationResult.REJECTED
    assert receipt.error and "fail-closed" in receipt.error


def test_duplicate_precondition_blocks():
    receipt = _adapter_with_answers(exists="yes").create_device("R1", "router")
    assert receipt.result is OperationResult.REJECTED


def test_save_project_confirms_from_inband_readback():
    receipt = _adapter_with_answers("OK:C:/temp/net.pkt|1234").save_project("net.pkt")
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed
    assert receipt.executed and receipt.executed["size"] == "1234"


def test_save_project_failure_reports_err():
    receipt = _adapter_with_answers("ERR:not created C:/x").save_project("net.pkt")
    assert receipt.result is OperationResult.FAILED


def test_configure_ios_is_never_confirmed():
    # answers: precondition 'yes' -> configureIosDevice 'OK'
    receipt = _adapter_with_answers("OK", exists="yes").configure_ios("R1", ["hostname R1"])
    assert receipt.executed_ok
    assert not receipt.confirmed  # IOS acceptance is not observable over the bridge
    assert receipt.postconditions[0].passed is None


def test_device_exists_maps_answers_honestly():
    adapter = _adapter_with_answers()
    adapter.send_expression = lambda expression, *, statements=None, wait=None: Observable.known("yes")  # type: ignore[method-assign]
    assert adapter.device_exists("R1").value is True
    adapter.send_expression = lambda expression, *, statements=None, wait=None: Observable.known("PT_ERROR: x")  # type: ignore[method-assign]
    assert not adapter.device_exists("R1").is_known


def test_foreign_bridge_is_reported_by_ping_identity():
    # ping() with a fake _http that returns a foreign fingerprint
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    import json as _json

    def fake_http(method, path_query, body=None, timeout=10.0):
        if path_query == "/ping":
            return 200, _json.dumps({"service": "pt-mcp-bridge", "proto": 1, "id": "deadbeef"})
        raise AssertionError("unexpected call")

    adapter._http = fake_http  # type: ignore[method-assign]
    # Isolate the HTTP verdict: the ambient machine may have a live Script
    # Engine (PT open with the extension), and ping() legitimately falls
    # back to that file mailbox. A fresh heartbeat would satisfy the
    # fallback and mask the foreign-bridge rejection under test here.
    adapter._file_bridge.pt_alive = lambda: False  # type: ignore[method-assign]
    info = adapter.ping()
    assert not info.reachable
    assert "foreign bridge" in (info.detail.value or "")


def test_bulk_create_honours_the_model_the_dag_planned():
    """Regression: the executor feeds create_devices_bulk the raw DAG op
    inputs, where the planned model is under "model" - not "model_hint".
    Reading only "model_hint" silently built a Router-PT for an op whose ports
    the solver had resolved against a 2911, and every addLink then came back
    as a bare "false" on GigabitEthernet0/0, a port that Router-PT does not
    have. Found live; the symptom looked like a cabling problem, not a
    dropped keyword."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    sent: list[str] = []

    def capture(expression, *, statements=None, wait=None):
        sent.append(" ".join(statements or []) + " " + str(expression))
        return Observable.known('[{"name":"R1","ok":true}]')

    adapter.send_expression = capture  # type: ignore[method-assign]
    adapter.create_devices_bulk(
        [{"name": "R1", "role": "router", "model": "2911", "position": [10, 20]}]
    )
    joined = " ".join(sent)
    assert '"2911"' in joined, joined
    assert "Router-PT" not in joined, joined


def test_bulk_create_still_accepts_model_hint_for_direct_callers():
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    sent: list[str] = []

    def capture(expression, *, statements=None, wait=None):
        sent.append(" ".join(statements or []) + " " + str(expression))
        return Observable.known('[{"name":"R1","ok":true}]')

    adapter.send_expression = capture  # type: ignore[method-assign]
    adapter.create_devices_bulk(
        [{"name": "R1", "role": "router", "model_hint": "2911", "position": [10, 20]}]
    )
    assert '"2911"' in " ".join(sent)


def _ios_adapter(ports_json: str, *, ok: str = "OK") -> BridgeAdapter:
    """Adapter whose fake PT applies the config and then reports `ports_json`."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    calls: list[str] = []

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        calls.append(text)
        if "devices" in text and "JSON.stringify" in text:
            return Observable.known(ports_json)
        return Observable.known(ok)

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter


_IOS_CMDS = [
    "hostname R1",
    "interface GigabitEthernet0/0",
    "ip address 10.0.0.1 255.255.255.0",
    "no shutdown",
    "exit",
]


def test_expected_ios_addresses_are_parsed_from_the_script():
    """The read-back expectation is derived from the commands themselves, so
    every caller gets it without having to restate the addresses."""
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import (
        _expected_ios_addresses,
    )

    assert _expected_ios_addresses(_IOS_CMDS) == {
        "GigabitEthernet0/0": "10.0.0.1"
    }
    assert _expected_ios_addresses(["hostname R1"]) == {}


def test_configure_ios_confirms_when_the_address_lands():
    adapter = _ios_adapter(
        '{"devices":[{"name":"R1","model":"2911","ports":'
        '[{"name":"GigabitEthernet0/0","ip":"10.0.0.1"}]}]}'
    )
    receipt = adapter.configure_ios("R1", _IOS_CMDS)
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed
    assert receipt.postconditions[0].name == "configured_addresses_present"
    assert receipt.postconditions[0].passed is True


def _gateway_adapter(ports_json: str) -> BridgeAdapter:
    """Fake PT whose Linksys applies the port-API push, then reports ports_json."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "devices" in text and "JSON.stringify" in text:
            return Observable.known(ports_json)
        return Observable.known("MCPNET:set")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter


def test_configure_gateway_lan_confirms_when_the_address_lands():
    """#10: the Linksys LAN address rides the port API (Vlan1), and the
    receipt is confirmed only when the read-back shows that address."""
    adapter = _gateway_adapter(
        '{"devices":[{"name":"wr","model":"Linksys-WRT300N","ports":'
        '[{"name":"Vlan1","ip":"192.168.0.1","mask":"255.255.255.0"}]}]}'
    )
    receipt = adapter.configure_gateway_lan(
        "wr", "192.168.0.1", "255.255.255.0"
    )
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed
    assert receipt.postconditions[0].name == "configured_addresses_present"
    assert receipt.postconditions[0].passed is True


def test_configure_gateway_lan_fails_when_the_address_never_lands():
    """#10/#5: PT said the push ran, but Vlan1 still reads the old address —
    the receipt must be FAILED, not a green op claiming the LAN is set."""
    adapter = _gateway_adapter(
        '{"devices":[{"name":"wr","model":"Linksys-WRT300N","ports":'
        '[{"name":"Vlan1","ip":"192.168.0.100","mask":"255.255.255.0"}]}]}'
    )
    receipt = adapter.configure_gateway_lan(
        "wr", "192.168.0.1", "255.255.255.0"
    )
    assert receipt.result is OperationResult.FAILED
    assert receipt.error and "did not take effect" in receipt.error
    assert receipt.postconditions[0].passed is False
    assert not receipt.confirmed


def test_configure_ios_resends_until_the_router_accepts_it():
    """A router that is still booting swallows the CLI input without reporting
    an error, so a single send is not evidence. The first read-back here shows
    no address; only the retry makes it stick."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    sends = {"configure": 0}
    reads = {"n": 0}

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "devices" in text and "JSON.stringify" in text:
            reads["n"] += 1
            # first read-back: nothing applied yet
            if reads["n"] <= 2:
                return Observable.known(
                    '{"devices":[{"name":"R1","model":"2911","ports":'
                    '[{"name":"GigabitEthernet0/0","ip":"0.0.0.0"}]}]}'
                )
            return Observable.known(
                '{"devices":[{"name":"R1","model":"2911","ports":'
                '[{"name":"GigabitEthernet0/0","ip":"10.0.0.1"}]}]}'
            )
        sends["configure"] += 1
        return Observable.known("OK")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    receipt = adapter.configure_ios("R1", _IOS_CMDS)
    assert sends["configure"] > 1, "must re-send when the address did not land"
    assert receipt.confirmed
    assert receipt.postconditions[0].passed is True


def test_configure_ios_does_not_claim_success_when_addresses_never_appear():
    """The whole point: a green receipt must mean the addresses are really
    there. Before this, a 63-device build reported every op succeeded while not
    one interface held an address."""
    adapter = _ios_adapter(
        '{"devices":[{"name":"R1","model":"2911","ports":'
        '[{"name":"GigabitEthernet0/0","ip":"0.0.0.0"}]}]}'
    )
    receipt = adapter.configure_ios("R1", _IOS_CMDS)
    assert receipt.postconditions[0].passed is False
    assert "GigabitEthernet0/0" in receipt.evidence["missing_addresses"]


def test_configure_ios_fails_the_op_when_the_addresses_never_land():
    """Bug #5: PT's configureIosDevice answers `true` even when it aborted on
    the first invalid line, so the receipt stayed SUCCESS while the config was
    wrong - and the DAG marked the op SUCCEEDED. An address we asked for and
    cannot read back is a FAILED operation with the missing work named."""
    adapter = _ios_adapter(
        '{"devices":[{"name":"R1","model":"2911","ports":'
        '[{"name":"GigabitEthernet0/0","ip":"0.0.0.0"}]}]}'
    )
    receipt = adapter.configure_ios("R1", _IOS_CMDS)
    assert receipt.result is OperationResult.FAILED
    assert receipt.executed_ok is False
    assert receipt.error
    assert "GigabitEthernet0/0" in receipt.error
    # honest per-command status replaces the old all-"unknown" claim
    import json as _json

    status = _json.loads(receipt.evidence["command_status"])
    assert all(v != "unknown" for v in status.values())
    assert status["ip address 10.0.0.1 255.255.255.0"].startswith("not applied")


def test_configure_ios_stays_success_when_every_address_reads_back():
    """The fix must not overreach: a config whose addresses are all observed is
    still SUCCESS and still confirmed."""
    adapter = _ios_adapter(
        '{"devices":[{"name":"R1","model":"2911","ports":'
        '[{"name":"GigabitEthernet0/0","ip":"10.0.0.1"}]}]}'
    )
    receipt = adapter.configure_ios("R1", _IOS_CMDS)
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed


def test_configure_ios_without_addressable_commands_is_unconfirmed_not_failed():
    """Nothing to read back means we cannot claim failure either: the receipt
    stays an executed, unconfirmed success for verification to judge."""
    adapter = _ios_adapter('{"devices":[{"name":"R1","model":"2911","ports":[]}]}')
    receipt = adapter.configure_ios("R1", ["hostname R1"])
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed is False
    assert receipt.postconditions[0].passed is None


#: Static routes scripted into the device. The address + route pair is the
#: minimum of what an S1.19 script asks for, so the read-back check has to
#: catch a silent drop on either side.
_ROUTE_CMDS = [
    "interface GigabitEthernet0/0",
    "ip address 10.0.0.1 255.255.255.252",
    "no shutdown",
    "exit",
    "ip route 0.0.0.0 0.0.0.0 10.0.0.2",
    "ip route 10.10.0.0 255.255.0.0 10.0.0.6",
]


def _route_adapter(
    *,
    ports_json: str = '{"devices":[{"name":"R1","model":"2911","ports":[]}]}',
    route_json: str = "",
    ok: str = "OK",
) -> BridgeAdapter:
    """Fake PT that applies the config, returns ports and (optional) routes.

    ``route_json`` is the verbatim tail PT would print for ``show ip route``;
    empty string means "no read-back performed" (UNKNOWN). The fixture detects
    route reads by sniffing the JS string for ``show ip route`` so an
    address-only test above never accidentally returns a route table. The
    arm/collect dance that ``_console_show`` runs is faked: the arm call returns
    ``BASE:0`` and the subsequent collect returns ``DONE:<route_json>``.
    """
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "devices" in text and "JSON.stringify" in text:
            return Observable.known(ports_json)
        # ``_console_show`` runs an arm/collect dance:
        #   arm  = ``enterCommand('show ip route')`` then ``'BASE:' + base``
        #   collect = IIFE returning ``DONE:<tail>`` / ``WAIT`` / ``ERR:``
        # The configure_ios push has no enterCommand, so it falls through.
        is_arm = bool(statements) and any("enterCommand" in s for s in statements)
        if not route_json:
            if is_arm:
                return Observable.unknown("console unavailable")
            return Observable.known(ok)
        if is_arm:
            return Observable.known("BASE:0")
        # collect pass (only reached when ``show ip route`` already armed and
        # produced output past BASE; the configure_ios push never gets here).
        return Observable.known("DONE:" + route_json)

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter


def test_expected_ios_routes_parses_ios_and_asa_forms():
    """Step 4 / S1.19: the read-back expectation covers both ``ip route`` and
    ASA's ``route <nameif>`` form. The nameif is dropped on purpose: PT's
    `show ip route` does not carry it, so keeping it on would always look like a
    contradiction. Subnet masks are normalised to the canonical prefix so a
    /24 written as ``255.255.255.0`` matches the read-back's ``10.0.0.0/24``.
    """
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import (
        _expected_ios_routes,
    )

    assert _expected_ios_routes([
        "ip route 0.0.0.0 0.0.0.0 10.0.0.5",
        "ip route 10.10.0.0 255.255.0.0 10.0.0.6",
        "ip route 10.20.0.0 255.255.0.0 10.0.0.10 200",  # metric is tolerated
    ]) == [
        {"network": "0.0.0.0", "prefix": 0, "next_hop": "10.0.0.5"},
        {"network": "10.10.0.0", "prefix": 16, "next_hop": "10.0.0.6"},
        {"network": "10.20.0.0", "prefix": 16, "next_hop": "10.0.0.10"},
    ]

    # ASA form: ``route <nameif> <dst> <mask> <next-hop>``.
    asa = _expected_ios_routes([
        "route outside 0.0.0.0 0.0.0.0 10.0.0.17",
        "route inside 10.30.50.0 255.255.255.0 10.0.0.18",
    ])
    assert asa == [
        {"network": "0.0.0.0", "prefix": 0, "next_hop": "10.0.0.17"},
        {"network": "10.30.50.0", "prefix": 24, "next_hop": "10.0.0.18"},
    ]
    # Scripts that ask for no static route return nothing.
    assert _expected_ios_routes(["hostname R1", "exit"]) == []


def test_configure_ios_fails_when_static_routes_silently_drop():
    """The S1.19 silent-drop bug: ``ip route 0.0.0.0 0.0.0.0 10.0.0.5`` reports
    SUCCESS on the wire but the route is not in ``show ip route``. A receipt
    that calls itself CONFIRMED in that case is the failure mode Step 4 is
    about. The route read-back has to flag the missing entry as a FAILED op,
    not a green one."""
    adapter = _route_adapter(
        ports_json='{"devices":[{"name":"R1","model":"2911","ports":'
                   '[{"name":"GigabitEthernet0/0","ip":"10.0.0.1"}]}]}',
        route_json=(
            "Codes: C - connected, S - static\n"
            "S    10.10.0.0/16 [1/0] via 10.0.0.6\n"
            # NB: the default route (S* 0.0.0.0/0) is MISSING.
        ),
    )
    receipt = adapter.configure_ios("R1", _ROUTE_CMDS)
    assert receipt.result is OperationResult.FAILED
    assert receipt.executed_ok is False
    assert receipt.error and "0.0.0.0/0 via 10.0.0.2" in receipt.error
    # The named postcondition is appended to the address one.
    names = [p.name for p in receipt.postconditions]
    assert "configured_routes_present" in names
    route_post = next(p for p in receipt.postconditions if p.name == "configured_routes_present")
    assert route_post.passed is False


def test_configure_ios_succeeds_when_every_static_route_reads_back():
    """The fix must not overreach: a config whose routes are all observed is
    still SUCCESS, still confirmed."""
    adapter = _route_adapter(
        ports_json='{"devices":[{"name":"R1","model":"2911","ports":'
                   '[{"name":"GigabitEthernet0/0","ip":"10.0.0.1"}]}]}',
        route_json=(
            "Codes: C - connected, S - static\n"
            "S*   0.0.0.0/0 [1/0] via 10.0.0.2\n"
            "S    10.10.0.0/16 [1/0] via 10.0.0.6\n"
        ),
    )
    receipt = adapter.configure_ios("R1", _ROUTE_CMDS)
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed
    route_post = next(p for p in receipt.postconditions if p.name == "configured_routes_present")
    assert route_post.passed is True


def test_configure_ios_route_check_skips_when_show_ip_route_unavailable():
    """No read-back signal is not a contradiction. A device whose console is
    still settling after a heavy push can answer ``configureIosDevice`` and
    not yet answer ``show ip route``: that case must stay SUCCESS/UNCONFIRMED,
    not flip to FAILED on missing evidence."""
    adapter = _route_adapter(
        ports_json='{"devices":[{"name":"R1","model":"2911","ports":'
                   '[{"name":"GigabitEthernet0/0","ip":"10.0.0.1"}]}]}',
        route_json="",  # no read-back performed
    )
    receipt = adapter.configure_ios("R1", _ROUTE_CMDS)
    assert receipt.result is OperationResult.SUCCESS
    route_post = next(p for p in receipt.postconditions if p.name == "configured_routes_present")
    assert route_post.passed is None


def test_device_exists_retries_a_dropped_poll_before_saying_unknown():
    """Callers fail closed on UNKNOWN, and PT's script engine drops the odd
    poll while it is busy. Without a retry that turns into a spurious
    "existence could not be verified" rejection on a create that was safe."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    reads = {"n": 0}

    def flaky_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "devices" in text and "JSON.stringify" in text:
            reads["n"] += 1
            if reads["n"] == 1:
                return Observable.unknown("no answer arrived")
            return Observable.known('{"devices":[]}')
        return Observable.known("no")

    adapter.send_expression = flaky_send  # type: ignore[method-assign]
    result = adapter.device_exists("R1")
    assert result.is_known
    assert result.value is False
    assert reads["n"] > 1, "must re-read rather than report UNKNOWN on one hiccup"


def test_device_exists_still_unknown_when_the_channel_is_really_gone():
    adapter = _adapter_with_answers(exists=None)
    assert not adapter.device_exists("R1").is_known


def _absent_receipt(name: str) -> OperationReceipt:
    """A delete_device receipt for a device that is already gone."""
    return OperationReceipt(
        operation_id=f"op-{name}",
        operation="delete_device",
        requested={"name": name},
        executed={"decision": "already absent"},
        result=OperationResult.SUCCESS,
    )


def test_batch_delete_clears_every_name_in_one_round_trip():
    """Teardown at sixty-odd devices used to overrun its budget one device at a
    time and leak the rest, which then made later PT runs time out."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    sent: list[str] = []

    def fake_send(expression, *, statements=None, wait=None):
        sent.append(" ".join(statements or []) + " " + str(expression))
        return Observable.known(
            '[{"name":"A","ok":true,"note":"deleted"},'
            '{"name":"B","ok":true,"note":"absent"}]'
        )

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    outcomes = adapter.delete_devices_bulk(["A", "B"])
    assert outcomes == {"A": "deleted", "B": "deleted"}
    assert len(sent) == 1, "both deletes must travel in one command"


def test_batch_delete_reports_a_name_it_could_not_confirm():
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    deleted = {"A"}

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "JSON.stringify" in text and "devices" in text:
            return Observable.known('{"devices":[]}')
        if "__mcpDel" in text:
            return Observable.known(
                '[{"name":"A","ok":true,"note":"deleted"},'
                '{"name":"B","ok":false,"note":"still present"}]'
            )
        return Observable.known("OK:deleted")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    adapter.delete_device = lambda name, operation_id=None: _absent_receipt(name)
    outcomes = adapter.delete_devices_bulk(["A", "B"])
    assert outcomes["A"] == "deleted"
    assert outcomes["B"] == "success", "B must be retried and confirmed absent"


def test_bulk_create_waits_for_pt_to_publish_the_devices():
    """PT publishes a device asynchronously, and a link created straight after
    lands on a half-registered topology and comes back as a bare 'false'. The
    settle polls the read-back instead of sleeping a fixed interval, because the
    publish lag grows with the batch and with how busy PT already is."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    reads = {"n": 0}

    def fake_send(expression, *, statements=None, wait=None):
        nonlocal reads
        text = " ".join(statements or []) + " " + str(expression)
        if "devices" in text and "JSON.stringify" in text:
            reads["n"] += 1
            if reads["n"] < 3:
                # not published yet
                return Observable.known('{"devices":[]}')
            return Observable.known(
                '{"devices":[{"name":"R1","model":"2911","ports":[]}]}'
            )
        return Observable.known('[{"name":"R1","ok":true}]')

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    adapter._settle_until_published(["R1"], timeout=10.0, interval=0.01)
    assert reads["n"] >= 3, "must re-read until PT shows the device"


def test_settle_waits_for_the_interfaces_not_just_the_device():
    """The window that actually loses cables: PT publishes the device first and
    registers its interfaces a beat later. A link issued in between finds a
    device that exists and a port that does not, and addLink answers 'false'."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    reads = {"n": 0}

    def fake_send(expression, *, statements=None, wait=None):
        nonlocal reads
        text = " ".join(statements or []) + " " + str(expression)
        if "devices" in text and "JSON.stringify" in text:
            reads["n"] += 1
            if reads["n"] < 3:
                # device present, interfaces not yet registered
                return Observable.known(
                    '{"devices":[{"name":"R1","model":"2911","ports":[]}]}'
                )
            return Observable.known(
                '{"devices":[{"name":"R1","model":"2911",'
                '"ports":[{"name":"GigabitEthernet0/0"}]}]}'
            )
        return Observable.known("OK")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    assert adapter._settle_until_published(["R1"], timeout=10.0, interval=0.01)
    assert reads["n"] >= 3, "must not settle on a portless device"


def test_settle_gives_up_bounded_when_pt_never_publishes():
    """A model PT refused never appears, so the wait has to end and let the
    caller report the real failure instead of stalling the build."""
    adapter = _adapter_with_answers(exists="no")
    assert adapter._settle_until_published(["Ghost"], timeout=0.05,
                                           interval=0.01) is False


# ---------------------------------------------------------------------------
# Bug #27 - a first ping lost to ARP is warm-up, not a failure
# ---------------------------------------------------------------------------

from src.network_autopilot.adapter.packet_tracer import bridge_adapter as _ba
from src.network_autopilot.core.requirements import FlowOutcome, FlowResult


def _attempt(outcome, detail=""):
    return FlowResult(
        source="pc1", destination="r1", outcome=outcome, detail=detail
    )


def test_a_ping_lost_to_arp_is_retried_until_it_converges(monkeypatch):
    """#27: PT drops the first echoes across a multi-hop path while ARP
    resolves, so a single shot reads a healthy network as broken. The lost
    attempt is warm-up and the converged verdict is what gets reported."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    monkeypatch.setattr(_ba, "_FLOW_PRE_SETTLE_S", 0.0)
    monkeypatch.setattr(_ba, "_FLOW_CONVERGE_SETTLE_S", 0.0)
    answers = [
        _attempt(FlowOutcome.UNKNOWN, "no new ping statistics appeared within 25s"),
        _attempt(FlowOutcome.PARTIAL, "Success rate is 60 percent"),
        _attempt(FlowOutcome.REACHABLE, "Success rate is 100 percent"),
    ]
    calls = []

    def fake(source, destination, target, timeout):
        calls.append(timeout)
        return answers.pop(0)

    monkeypatch.setattr(adapter, "_single_ping_flow", fake)
    result = adapter.run_flow_test("pc1", "r1", destination_ip="10.0.0.1")
    assert result.outcome is FlowOutcome.REACHABLE
    assert len(calls) == 3, calls
    assert len(result.evidence["attempts"]) == 3


def test_a_terminal_arm_failure_is_not_retried(monkeypatch):
    """Bounded means bounded: an arm/console failure cannot improve, so it
    costs exactly one attempt instead of burning the whole window."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    monkeypatch.setattr(_ba, "_FLOW_PRE_SETTLE_S", 0.0)
    calls = []

    def fake(source, destination, target, timeout):
        calls.append(timeout)
        return _attempt(FlowOutcome.UNKNOWN, "ping could not be armed: PT_ERROR")

    monkeypatch.setattr(adapter, "_single_ping_flow", fake)
    result = adapter.run_flow_test("pc1", "r1", destination_ip="10.0.0.1")
    assert result.outcome is FlowOutcome.UNKNOWN
    assert len(calls) == 1, calls


# ---------------------------------------------------------------------------
# Bug #20 - VLAN membership and trunk state must be observed
# ---------------------------------------------------------------------------

# A real `show vlan brief` table. Fa0/3 sits in both VLANs, so it is carrying a
# trunk; Fa0/1 is a plain access port in VLAN 1.
SHOW_VLAN_BRIEF = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Fa0/1, Fa0/2
10   users                            active    Fa0/3
20   voice                            active    Fa0/3
1002 fddi-default                     act/unsup
"""

_SWITCH_MATCH = {
    "name": "s1",
    "model": "2960-24TT",
    "ports": [
        {"name": "FastEthernet0/1", "up": True, "ip": "", "mask": "",
         "linkClass": "Cable", "peers": ["pc1"]},
        {"name": "FastEthernet0/3", "up": True, "ip": "", "mask": "",
         "linkClass": "Cable", "peers": ["r2"]},
    ],
}


def _adapter_with_vlan_table(monkeypatch, table, **kwargs):
    from src.network_autopilot.adapter.packet_tracer.js_commands import (
        parse_show_vlan_brief,
    )

    adapter = BridgeAdapter(
        port=59999, token=TEST_TOKEN, host_bridge=False, **kwargs
    )
    asked: list[str] = []

    def fake_read_vlans(device):
        asked.append(device)
        if table is None:
            return Observable[list].unknown("console unavailable")
        return Observable[list].known(parse_show_vlan_brief(table))

    monkeypatch.setattr(adapter, "read_vlans", fake_read_vlans)
    return adapter, asked


def test_a_switch_port_reports_its_real_vlan_and_trunk_state(monkeypatch):
    """#20: `vlan_defined`/`trunk_configured` read port.vlan/port.trunk, and the
    topology snapshot carries neither, so A-012..A-015 reported `vlan not
    present` against a switch that was configured correctly."""
    adapter, asked = _adapter_with_vlan_table(monkeypatch, SHOW_VLAN_BRIEF)
    observation = adapter._observation_from_live(_SWITCH_MATCH)
    assert asked == ["s1"]

    access = observation.ports["FastEthernet0/1"]
    assert access.vlan.is_known and access.vlan.value == "1"
    assert access.trunk.is_known and access.trunk.value is False

    trunk = observation.ports["FastEthernet0/3"]
    assert trunk.trunk.is_known and trunk.trunk.value is True
    assert trunk.vlan.value.split(",") == ["10", "20"]


def test_a_device_without_vlans_is_never_asked_for_them(monkeypatch):
    """A router has no `show vlan brief` table: the round trip would cost time
    and return nothing."""
    adapter, asked = _adapter_with_vlan_table(monkeypatch, SHOW_VLAN_BRIEF)
    adapter._observation_from_live(
        {"name": "r1", "model": "2911",
         "ports": [{"name": "GigabitEthernet0/0", "up": True, "ip": "",
                    "mask": "", "linkClass": "Cable", "peers": ["r2"]}]}
    )
    assert asked == []


def test_a_failed_vlan_read_leaves_the_ports_unknown_rather_than_passing(monkeypatch):
    """No table is not a VLAN that does not exist: the fields stay
    not-observed so the criterion reports UNKNOWN, never PASS."""
    adapter, asked = _adapter_with_vlan_table(monkeypatch, None)
    observation = adapter._observation_from_live(_SWITCH_MATCH)
    assert asked == ["s1"]
    port = observation.ports["FastEthernet0/1"]
    assert not port.vlan.is_known
    assert not port.trunk.is_known


def test_the_vlan_read_can_be_switched_off_for_specs_that_ask_for_no_vlan(monkeypatch):
    adapter, asked = _adapter_with_vlan_table(
        monkeypatch, SHOW_VLAN_BRIEF, observe_vlans=False
    )
    observation = adapter._observation_from_live(_SWITCH_MATCH)
    assert asked == []
    assert not observation.ports["FastEthernet0/1"].vlan.is_known


def test_a_port_holder_that_is_not_http_rules_out_the_http_channel():
    """A non-HTTP service on the bridge port answers with a bare status line,
    so urlopen raises http.client.BadStatusLine - an HTTPException, not an
    OSError. Without converting it, _probe_channels crashed and the whole
    command pipeline died with it instead of just ruling HTTP out (found
    live while probing the 44 issues; PT's port was held by something else)."""
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def serve() -> None:
        for _ in range(4):
            try:
                conn, _addr = listener.accept()
                conn.recv(4096)
                conn.sendall(b"NOT-HTTP\r\n")
                conn.close()
            except OSError:
                return

    threading.Thread(target=serve, daemon=True).start()
    try:
        adapter = BridgeAdapter(port=port, token=TEST_TOKEN, host_bridge=False)
        info = adapter._http_ping(timeout=2.0)
        assert not info.reachable
        assert "non-HTTP" in str(info.detail.value)
        # channel probing must survive it and never pick http
        assert adapter._probe_channels() != "http"
    finally:
        listener.close()


# ---------------------------------------------------------------------------
# Bug #5 (VLAN half) - a pushed VLAN/trunk expectation must be read back
# ---------------------------------------------------------------------------

# What `_build_vlan_switch_commands` renders for a switch: hostname, the VLAN
# database it carries, then one block per port. Indentation is the compiler's.
_SWITCH_CMDS = [
    "hostname s1",
    "vlan 10",
    " name users",
    " exit",
    "interface FastEthernet0/1",
    " switchport mode access",
    " switchport access vlan 10",
    " exit",
]

# Fa0/1 sits in the default VLAN, not in vlan 10: the access assignment the
# script pushed never applied, which PT never reports on its own.
SWITCH_TABLE_VLAN10_EMPTY = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Fa0/1, Fa0/2
10   users                            active    Fa0/3
1002 fddi-default                     act/unsup
"""

# The same switch with the pushed state actually in place.
SWITCH_TABLE_VLAN10_APPLIED = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Fa0/2
10   users                            active    Fa0/1
1002 fddi-default                     act/unsup
"""


def _switch_adapter(monkeypatch, table, *, name="s1", model="2960-24TT", **kwargs):
    """Fake PT for a switch receipt: the topology names the device, the config
    push is accepted, and `show vlan brief` answers `table` (`None` = the
    console never answers).

    Combines the two transport fakes this file already uses: intent-based
    answering (`_adapter_with_answers`) and injecting the VLAN table as the one
    fact under test (`_adapter_with_vlan_table`).
    """
    from src.network_autopilot.adapter.packet_tracer.js_commands import (
        parse_show_vlan_brief,
    )

    adapter = BridgeAdapter(
        port=59999, token=TEST_TOKEN, host_bridge=False, **kwargs
    )
    asked: list[str] = []

    def fake_read_vlans(device):
        asked.append(device)
        if table is None:
            return Observable[list].unknown("console unavailable")
        return Observable[list].known(parse_show_vlan_brief(table))

    monkeypatch.setattr(adapter, "read_vlans", fake_read_vlans)

    match = {
        "name": name,
        "model": model,
        "ports": [
            {"name": "FastEthernet0/1", "up": True, "ip": "", "mask": "",
             "linkClass": "Cable", "peers": ["pc1"]},
        ],
    }

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "JSON.stringify" in text and "devices" in text:
            return Observable.known(json.dumps({"devices": [match]}))
        return Observable.known("OK")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter, asked


def _vlan_check(receipt: OperationReceipt):
    return next(
        (c for c in receipt.postconditions if c.name == "configured_vlans_present"),
        None,
    )


def test_an_access_vlan_that_never_applied_fails_the_receipt(monkeypatch):
    """#5 (VLAN half): PT never echoes a `configureIosDevice` abort, so a
    `switchport access vlan 10` that silently did not apply left the receipt
    green and the DAG marked the op SUCCEEDED. The expectation is parsed from
    the pushed block and read back with `show vlan brief`; a table that
    contradicts it is a FAILED operation."""
    adapter, asked = _switch_adapter(monkeypatch, SWITCH_TABLE_VLAN10_EMPTY)
    receipt = adapter.configure_ios("s1", _SWITCH_CMDS)
    assert receipt.result is OperationResult.FAILED
    check = _vlan_check(receipt)
    assert check is not None and check.passed is False
    assert "FastEthernet0/1" in json.dumps(check.actual.value)
    assert receipt.error and "vlan" in receipt.error
    assert asked == ["s1"], "the switch must be read back"


def test_an_access_vlan_that_reads_back_confirms_the_receipt(monkeypatch):
    """The check must not overreach: the same push against a table that shows
    the membership is a plain success."""
    adapter, asked = _switch_adapter(monkeypatch, SWITCH_TABLE_VLAN10_APPLIED)
    receipt = adapter.configure_ios("s1", _SWITCH_CMDS)
    assert asked == ["s1"]
    assert receipt.result is OperationResult.SUCCESS
    check = _vlan_check(receipt)
    assert check is not None and check.passed is True
    assert receipt.error is None


def test_an_asa_switchport_block_is_not_asked_for_vlan_state(monkeypatch):
    """A 5505 renders `switchport access vlan N` on its Ethernet ports
    (compiler `_build_firewall_commands`), but a firewall has no
    `show vlan brief`: the expectation must be dropped before any round trip
    instead of being judged against a table that cannot exist."""
    asa_cmds = [
        "hostname asa1",
        "interface Ethernet0/1",
        "switchport access vlan 2",
        "exit",
        "interface Vlan2",
        "nameif inside",
        "security-level 100",
        "no shutdown",
        "exit",
    ]
    adapter, asked = _switch_adapter(
        monkeypatch, SWITCH_TABLE_VLAN10_APPLIED, name="asa1", model="5505"
    )
    receipt = adapter.configure_ios("asa1", asa_cmds)
    assert asked == [], "a firewall has no VLAN table to read"
    assert receipt.result is OperationResult.SUCCESS
    assert _vlan_check(receipt) is None
    assert len(receipt.postconditions) == 1


def test_an_unreadable_vlan_read_does_not_fabricate_a_failure(monkeypatch):
    """No table is not a VLAN that does not exist: when `show vlan brief`
    cannot be read the receipt keeps today's semantics - executed, never a
    fabricated FAILED, and unconfirmed rather than confirmed."""
    adapter, asked = _switch_adapter(monkeypatch, None)
    receipt = adapter.configure_ios("s1", _SWITCH_CMDS)
    assert asked == ["s1"]
    assert receipt.result is OperationResult.SUCCESS
    check = _vlan_check(receipt)
    assert check is not None and check.passed is None


def test_a_vlan_definition_that_never_appears_fails_the_receipt(monkeypatch):
    """`vlan 20` pushed but absent from the read-back table: the definition
    itself was not applied, even though every port membership looks fine."""
    table = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Fa0/1, Fa0/2
10   users                            active    Fa0/1
1002 fddi-default                     act/unsup
"""
    adapter, asked = _switch_adapter(monkeypatch, table)
    receipt = adapter.configure_ios(
        "s1", _SWITCH_CMDS + ["vlan 20", " name voice", " exit"]
    )
    assert receipt.result is OperationResult.FAILED
    check = _vlan_check(receipt)
    assert check is not None and check.passed is False
    assert "vlan 20" in json.dumps(check.actual.value)


def test_a_trunk_that_reads_back_as_a_single_access_vlan_fails(monkeypatch):
    """`switchport mode trunk` with an allowed set of 10,20: a table that
    lists the port under one VLAN only contradicts the push."""
    table = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Fa0/1
10   users                            active    Fa0/2, Fa0/3
1002 fddi-default                     act/unsup
"""
    adapter, asked = _switch_adapter(monkeypatch, table)
    receipt = adapter.configure_ios(
        "s1",
        [
            "hostname s1",
            "vlan 10", " name users", " exit",
            "vlan 20", " name voice", " exit",
            "interface FastEthernet0/2",
            " switchport mode trunk",
            " switchport trunk allowed vlan 10,20",
            " exit",
        ],
    )
    assert receipt.result is OperationResult.FAILED
    check = _vlan_check(receipt)
    assert check is not None and check.passed is False
    assert "FastEthernet0/2" in json.dumps(check.actual.value)


def test_a_trunk_that_carries_its_allowed_vlans_confirms(monkeypatch):
    table = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Fa0/1
10   users                            active    Fa0/2, Fa0/3
20   voice                           active    Fa0/2, Fa0/3
1002 fddi-default                     act/unsup
"""
    adapter, asked = _switch_adapter(monkeypatch, table)
    receipt = adapter.configure_ios(
        "s1",
        [
            "hostname s1",
            "vlan 10", " name users", " exit",
            "vlan 20", " name voice", " exit",
            "interface FastEthernet0/2",
            " switchport mode trunk",
            " switchport trunk allowed vlan 10,20",
            " exit",
        ],
    )
    assert receipt.result is OperationResult.SUCCESS
    check = _vlan_check(receipt)
    assert check is not None and check.passed is True


def test_vlan_verification_is_skipped_when_observation_is_switched_off(monkeypatch):
    """A caller that asked for no VLAN observation (#20) pays no round trip and
    gets the receipt it got before this check existed."""
    adapter, asked = _switch_adapter(
        monkeypatch, SWITCH_TABLE_VLAN10_EMPTY, observe_vlans=False
    )
    receipt = adapter.configure_ios("s1", _SWITCH_CMDS)
    assert asked == []
    assert receipt.result is OperationResult.SUCCESS
    assert _vlan_check(receipt) is None


def test_expected_ios_vlans_are_parsed_from_the_pushed_block():
    from src.network_autopilot.adapter.packet_tracer.bridge_adapter import (
        _expected_ios_vlans,
    )

    expected = _expected_ios_vlans(_SWITCH_CMDS)
    assert expected is not None
    assert expected["defined"] == {10: "users"}
    assert expected["access"] == {"FastEthernet0/1": 10}
    assert expected["trunk"] == {}
    assert _expected_ios_vlans(["hostname R1"]) is None
    assert _expected_ios_vlans(_IOS_CMDS) is None

