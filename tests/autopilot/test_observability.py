"""Observable semantics: unknown is never a fact."""

from __future__ import annotations

import pytest

from src.network_autopilot.core.observability import (
    Observable,
    ObservationStatus,
    UnknownValueError,
)


def test_empty_read_is_unknown_not_fact():
    assert Observable.from_raw("").status is ObservationStatus.UNKNOWN
    assert Observable.from_raw(None).status is ObservationStatus.UNKNOWN
    assert Observable.from_raw("GigabitEthernet0/0").is_known


def test_five_statuses_stay_distinct():
    variants = [
        Observable.known("x"),
        Observable.unknown("could not answer"),
        Observable.not_observed(),
        Observable.unsupported("runtime cannot expose this"),
        Observable.stale("x"),
    ]
    assert len({v.status for v in variants}) == 5
    assert all(not v.is_known for v in variants[1:])


def test_require_fails_closed_on_every_non_known():
    for obs in (
        Observable.unknown("no answer"),
        Observable.not_observed(),
        Observable.unsupported("n/a"),
        Observable.stale("old"),
    ):
        with pytest.raises(UnknownValueError):
            obs.require()


def test_stale_keeps_value_but_is_not_authoritative():
    obs = Observable.stale("192.168.1.1")
    assert obs.value == "192.168.1.1"
    assert not obs.is_known


def test_serialization_roundtrip_preserves_status():
    for obs in (
        Observable.known("up", source="test", observed_at="2026-01-01T00:00:00+00:00"),
        Observable.unknown("timeout"),
        Observable.not_observed(),
        Observable.unsupported("n/a"),
    ):
        restored = Observable.from_dict(obs.to_dict())
        assert restored.status is obs.status
        assert restored.value == obs.value
        assert restored.detail == obs.detail
