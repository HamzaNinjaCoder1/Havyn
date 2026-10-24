"""Stage 4 as-is intent and round-trip tests (S4.4)."""

from __future__ import annotations

from network_autopilot.brownfield.intent import AsIsIntentBuilder, RoundTripValidator
from network_autopilot.brownfield.models import Confidence
from network_autopilot.core.network_spec import NetworkSpec

from tests.autopilot import s4_helpers as h


def test_as_is_spec_represents_library_network() -> None:
    spec = h.load_spec("T02-two-vlan-office.json")
    _, imported = h.import_simulated(spec)
    intent = AsIsIntentBuilder().build(imported)
    devices = {device.id for device in intent.spec.devices}
    assert devices == {"r1", "s1", "pc1", "pc2", "pc3"}
    assert {vlan.vlan_id for vlan in intent.spec.vlans} >= {10, 20}
    assert {s.cidr for s in intent.spec.subnets} == {
        "10.0.10.0/24", "10.0.20.0/24"
    }
    trunk = next(link for link in intent.spec.links if link.trunk)
    assert set(trunk.allowed_vlans or []) == {10, 20}
    assert intent.confidence_for("device(r1).model") is Confidence.KNOWN


def test_round_trip_preserves_supported_semantics() -> None:
    for name in ("T01-small-office.json", "T02-two-vlan-office.json",
                 "T09-ospf-backbone.json"):
        spec = h.load_spec(name)
        _, imported = h.import_simulated(spec)
        intent = AsIsIntentBuilder().build(imported)
        report = RoundTripValidator().validate(intent, imported)
        assert report.status in ("exact", "equivalent"), (
            name, report.status, report.detail,
            [d.differences for d in report.devices],
        )
        assert not any(d.status == "not_equivalent" for d in report.devices)


def test_round_trip_marks_unsupported_semantics_without_claiming_equality() -> None:
    from network_autopilot.adapter.null_adapter import NullAdapter
    from network_autopilot.brownfield.importer import BrownfieldImporter

    adapter = NullAdapter()
    adapter.create_device("r1", "router")
    adapter.create_device("pc1", "pc")
    adapter.create_link("r1", "GigabitEthernet0/0", "pc1", "FastEthernet0")
    adapter.configure_ios(
        "r1",
        [
            "interface GigabitEthernet0/0",
            "ip address 10.0.10.1 255.255.255.0",
            "no shutdown",
            "exit",
            "access-list 10 permit 10.0.10.0 0.0.0.255",
        ],
    )
    adapter.configure_host(
        "pc1", dhcp=False, ip="10.0.10.10", mask="255.255.255.0",
        gateway="10.0.10.1",
    )
    imported = BrownfieldImporter(adapter).import_network()
    intent = AsIsIntentBuilder().build(imported)
    report = RoundTripValidator().validate(intent, imported)
    # The ACL has no representation in NetworkSpec; it must be named, and the
    # verdict must not be "exact" when anything was excluded.
    assert report.status in ("equivalent", "partial")
    assert any(
        "access-list" in item.lower()
        for item in intent.unsupported + report.unsupported
    )


def test_unknown_device_model_keeps_confidence_unknown() -> None:
    from network_autopilot.adapter.null_adapter import NullAdapter
    from network_autopilot.brownfield.importer import BrownfieldImporter
    from network_autopilot.core.observability import Observable

    class UnknownModelAdapter(NullAdapter):
        def inspect_device(self, name):
            observation = super().inspect_device(name)
            observation.model = Observable[str].unknown("model not reported")
            return observation

    adapter = UnknownModelAdapter()
    adapter.create_device("r1", "router")
    imported = BrownfieldImporter(adapter).import_network()
    intent = AsIsIntentBuilder().build(imported)
    assert intent.confidence_for("device(r1).model") is Confidence.UNKNOWN
    assert any("category" in item for item in imported.inventory.incomplete)
