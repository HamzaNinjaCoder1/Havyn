"""Stage 4 importer and configuration-reader tests (S4.1/S4.2)."""

from __future__ import annotations

from pathlib import Path

from network_autopilot.adapter.null_adapter import NullAdapter
from network_autopilot.brownfield.config_reader import RunningConfigReader, safe_component
from network_autopilot.brownfield.importer import BrownfieldImporter, ImportOptions
from network_autopilot.brownfield.models import ConfigStatus, Platform
from network_autopilot.core.observability import Observable

from tests.autopilot import s4_helpers as h


def test_importer_reads_both_cable_endpoints_and_states() -> None:
    spec = h.load_spec("T02-two-vlan-office.json")
    adapter, imported = h.import_simulated(spec)
    inventory = imported.inventory
    assert set(inventory.devices) == {"r1", "s1", "pc1", "pc2", "pc3"}
    endpoints = {
        frozenset({(l.a_device, l.a_port), (l.b_device, l.b_port)})
        for l in inventory.links
    }
    assert len(endpoints) == 4
    switch = inventory.device("s1")
    assert switch is not None
    access = next(
        i for i in switch.interfaces.values()
        if i.linked and i.vlan.is_known and i.vlan.value == 10
    )
    assert access is not None
    trunk = next(
        i for i in switch.interfaces.values()
        if i.linked and i.trunk.is_known and i.trunk.value
    )
    assert trunk.allowed_vlans.is_known
    assert set(trunk.allowed_vlans.value or []) == {10, 20}


def test_importer_reads_host_gateway_dns_and_disposition() -> None:
    spec = h.load_spec("T02-two-vlan-office.json")
    _, imported = h.import_simulated(spec)
    pc1 = imported.inventory.device("pc1")
    assert pc1.host is not None
    assert pc1.host.ip.is_known
    assert pc1.host.gateway.value == "10.0.10.1"
    assert pc1.host.dhcp.is_known and pc1.host.dhcp.value is True


def test_importer_never_invents_missing_configuration() -> None:
    adapter = NullAdapter()
    adapter.create_device("pc1", "pc")
    adapter.configure_host(
        "pc1", dhcp=False, ip="10.0.10.10", mask="255.255.255.0"
    )
    imported = BrownfieldImporter(adapter).import_network()
    host = imported.inventory.device("pc1").host
    assert host is not None
    assert host.ip.value == "10.0.10.10"
    # No gateway was configured or read: it stays UNKNOWN, not a guessed LLQ.
    assert not host.gateway.is_known
    assert not host.dns.is_known


def test_importer_marks_linksys_config_unsupported() -> None:
    adapter = NullAdapter()
    adapter.create_device("wr", "wireless_router")
    imported = BrownfieldImporter(adapter).import_network()
    wr = imported.inventory.device("wr")
    assert wr is not None
    assert wr.config is not None
    assert wr.config.status is ConfigStatus.UNSUPPORTED
    assert wr.config.detail


def test_config_reader_retries_then_reports_unknown() -> None:
    class Flaky:
        name = "flaky"

        def __init__(self):
            self.calls = 0

        def read_running_config(self, device):
            self.calls += 1
            return Observable[str].unknown("console busy")

    adapter = Flaky()
    reader = RunningConfigReader(adapter, attempts=3, retry_delay_s=0)
    extract = reader.read("r1", platform=Platform.IOS)
    assert adapter.calls == 3
    assert extract.status is ConfigStatus.UNKNOWN
    assert "console busy" in (extract.detail or "")


def test_config_reader_preserves_raw_and_hashes(tmp_path: Path) -> None:
    spec = h.load_spec("T01-small-office.json")
    adapter = h.SimulatedAdapter(spec)
    reader = RunningConfigReader(adapter, artifacts_root=tmp_path, attempts=1)
    extract = reader.read("r1", platform=Platform.IOS)
    assert extract.status is ConfigStatus.KNOWN
    assert extract.raw_path and Path(extract.raw_path).exists()
    assert Path(extract.raw_path).read_text(encoding="utf-8") == extract.raw
    assert extract.sha256


def test_path_components_are_safe() -> None:
    assert safe_component("../../etc/passwd") == "etc_passwd"
    assert safe_component("..") == "device"
    assert safe_component("r1") == "r1"
