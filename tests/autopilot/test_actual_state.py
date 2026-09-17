"""ActualNetworkState: explicit unknowns, merge discipline, honest summaries."""

from __future__ import annotations

from src.network_autopilot.core.actual_state import (
    ActualNetworkState,
    DeviceObservation,
    PortObservation,
)
from src.network_autopilot.core.observability import Observable


def _state_with(name: str, model: Observable) -> ActualNetworkState:
    state = ActualNetworkState(source="test", observed_at="2026-01-01T00:00:00+00:00")
    state.observe_device(DeviceObservation(name=name, model=model))
    return state


def test_missing_device_is_not_the_same_as_unobserved_device():
    state = _state_with("r1", Observable.unknown("never answered"))
    assert state.device("ghost") is None            # never seen at all
    assert state.device("r1") is not None           # seen, but properties unknown
    assert not state.device("r1").model.is_known    # type: ignore[union-attr]


def test_merge_unknown_never_destroys_known():
    base = _state_with(
        "r1", Observable.known("2911", observed_at="2026-01-01T00:00:00+00:00")
    )
    worse = _state_with("r1", Observable.unknown("device busy"))
    base.merge(worse)
    assert base.devices["r1"].model.is_known
    assert base.devices["r1"].model.value == "2911"


def test_merge_newer_known_wins():
    base = _state_with(
        "r1", Observable.known("2911", observed_at="2026-01-01T00:00:00+00:00")
    )
    newer = _state_with(
        "r1", Observable.known("1841", observed_at="2026-01-02T00:00:00+00:00")
    )
    base.merge(newer)
    assert base.devices["r1"].model.value == "1841"


def test_port_merge_respects_same_rule():
    state = ActualNetworkState(source="test", observed_at="t0")
    dev = DeviceObservation(name="pc1")
    port = dev.port("FastEthernet0")
    port.ip = Observable.known("192.168.10.5", observed_at="2026-01-01T00:00:00+00:00")
    state.observe_device(dev)

    other = ActualNetworkState(source="test", observed_at="t1")
    other_dev = DeviceObservation(name="pc1")
    other_dev.port("FastEthernet0").ip = Observable.unknown("device busy")
    other.observe_device(other_dev)

    state.merge(other)
    assert state.devices["pc1"].ports["FastEthernet0"].ip.value == "192.168.10.5"


def test_unknown_summary_makes_gaps_visible():
    state = ActualNetworkState(source="test", observed_at="t0")
    dev = DeviceObservation(name="r1")
    dev.port("GigabitEthernet0/0")
    state.observe_device(dev)
    summary = "\n".join(state.unknown_summary())
    assert "device(r1).model" in summary
    assert "port(GigabitEthernet0/0).ip" in summary
    assert "project_path" in summary


def test_serialization_roundtrip():
    state = ActualNetworkState(source="test", observed_at="t0")
    dev = DeviceObservation(name="srv1", ref_id=Observable.known("srv-1"))
    port = dev.port("FastEthernet0")
    port.ip = Observable.known("192.168.100.10")
    port.mask = Observable.known("255.255.255.0")
    state.observe_device(dev)
    restored = ActualNetworkState.from_dict(state.to_dict())
    assert restored.devices["srv1"].ref_id.value == "srv-1"
    assert restored.devices["srv1"].ports["FastEthernet0"].ip.value == "192.168.100.10"


def test_port_helper_creates_placeholder():
    dev = DeviceObservation(name="sw1")
    port = dev.port("FastEthernet0/1")
    assert isinstance(port, PortObservation)
    assert port.ip.status.value == "not_observed"
