"""Adapter contract tests against the deterministic NullAdapter."""

from __future__ import annotations

import pytest

from src.network_autopilot.adapter import NullAdapter, PacketTracerAdapter
from src.network_autopilot.core.receipts import OperationResult


def test_null_adapter_satisfies_protocol():
    assert isinstance(NullAdapter(), PacketTracerAdapter)


def test_create_device_receipt_is_confirmed_by_readback():
    adapter = NullAdapter()
    receipt = adapter.create_device("R1", "router", model_hint="2911")
    assert receipt.result is OperationResult.SUCCESS
    assert receipt.confirmed
    assert receipt.postconditions[0].passed is True
    assert adapter.device_exists("R1").value is True


def test_duplicate_create_is_rejected_before_execution():
    adapter = NullAdapter()
    adapter.create_device("R1", "router")
    receipt = adapter.create_device("R1", "router")
    assert receipt.result is OperationResult.REJECTED
    assert receipt.error and "already exists" in receipt.error


def test_ports_are_reserved_no_silent_competition():
    adapter = NullAdapter()
    adapter.create_device("PC1", "pc")
    adapter.create_device("PC2", "pc")
    first = adapter.create_link("PC1", "FastEthernet0", "PC2", "FastEthernet0")
    assert first.result is OperationResult.SUCCESS
    second = adapter.create_link("PC1", "FastEthernet0", "PC2", "FastEthernet0")
    assert second.result is OperationResult.REJECTED
    names = [c.name for c in second.preconditions]
    assert "a_port_free" in names and "b_port_free" in names


def test_configure_ios_is_executed_but_never_confirmed():
    adapter = NullAdapter()
    adapter.create_device("R1", "router")
    receipt = adapter.configure_ios("R1", ["hostname R1", "no ip domain-lookup"])
    # executed: yes. confirmed: NO — structural recording is not IOS acceptance.
    assert receipt.executed_ok
    assert not receipt.confirmed
    assert receipt.postconditions[0].passed is None


def test_flow_test_is_never_fabricated():
    adapter = NullAdapter()
    adapter.create_device("PC1", "pc")
    adapter.create_device("PC2", "pc")
    flow = adapter.run_flow_test("PC1", "PC2")
    assert flow.outcome.value == "unknown"
    assert flow.detail and "no data plane" in flow.detail


def test_observe_state_reports_structural_truth():
    adapter = NullAdapter()
    adapter.create_device("srv1", "server")
    adapter.configure_host("srv1", dhcp=False, ip="10.0.0.10", mask="255.0.0.0")
    state = adapter.observe_state(["srv1"])
    dev = state.devices["srv1"]
    assert dev.model.value == "Server-PT"
    port = dev.ports["FastEthernet0"]
    assert port.ip.value == "10.0.0.10"
    assert port.state.value == "down"  # nothing linked yet


def test_configure_host_readback():
    adapter = NullAdapter()
    adapter.create_device("PC1", "pc")
    receipt = adapter.configure_host("PC1", dhcp=False, ip="192.168.10.5", gateway="192.168.10.1")
    assert receipt.confirmed


def test_rename_and_position():
    adapter = NullAdapter()
    adapter.create_device("R1", "router")
    renamed = adapter.rename_device("R1", "CORE-R1")
    assert renamed.confirmed
    moved = adapter.position_device("CORE-R1", 250, 300)
    assert moved.confirmed
    assert moved.postconditions[0].actual.value == "250,300"


def test_ping_declares_its_nature():
    info = NullAdapter().ping()
    assert info.reachable
    assert "no Packet Tracer involved" in (info.detail.value or "")
