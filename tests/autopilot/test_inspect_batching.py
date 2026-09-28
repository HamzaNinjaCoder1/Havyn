"""S1.6b — one topology snapshot feeds a burst of read-backs.

Measured in the archived reference run: 52 address read-backs cost 162 s
because every inspect_device/observe_state fetched the whole topology again
(fresh=True), even though no mutation happened between them. The snapshot
cache already exists and every mutation already calls _invalidate_snapshot(),
so reads inside a burst are served from one fetch; a mutation (or the TTL
bound for out-of-band PT changes) forces the next real read. Ground truth
after a mutation is pinned here — rule 8 must not be traded for speed.
"""

from __future__ import annotations

import time

from src.network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from src.network_autopilot.core.observability import Observable

TEST_TOKEN = "test-token-long-enough-0123456789abcdef"

_DEVICES_JSON = (
    '{"devices":[{"name":"r1","model":"2911","ports":['
    '{"name":"Gig0/0","ip":"10.0.0.1","mask":"255.255.255.0",'
    '"up":true,"linked":true}]}]}'
)


def _adapter_counting_topology_fetches():
    """Adapter whose topology reads are counted; mutations answer benignly."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    fetches: list[str] = []

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "JSON.stringify" in text and "devices" in text:
            fetches.append(text)
            return Observable.known(_DEVICES_JSON)
        return Observable.known('{"ok":true}')

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    return adapter, fetches


def test_a_burst_of_readbacks_pays_for_one_topology_read():
    """Failing-first: three read-backs with no mutation in between are ONE
    observation, not three full topology fetches."""
    adapter, fetches = _adapter_counting_topology_fetches()
    adapter.inspect_device("r1")
    adapter.inspect_device("r1")
    adapter.observe_state(["r1"])
    assert len(fetches) == 1, len(fetches)


def test_a_mutation_between_readbacks_forces_a_re_read():
    """Rule 8 pin: invalidate the snapshot (what every mutation does) and the
    next read must be a real fetch, never the pre-mutation cache."""
    adapter, fetches = _adapter_counting_topology_fetches()
    adapter.inspect_device("r1")
    assert len(fetches) == 1
    adapter._invalidate_snapshot()
    adapter.inspect_device("r1")
    assert len(fetches) == 2, len(fetches)


def test_an_expired_ttl_forces_a_re_read_even_without_a_mutation():
    """Bounded staleness for changes we did not make (user hands on PT, PT
    finishing its own work): past the TTL the cache does not answer."""
    adapter, fetches = _adapter_counting_topology_fetches()
    adapter.inspect_device("r1")
    assert len(fetches) == 1
    adapter._snapshot_at = time.time() - (adapter._snapshot_ttl + 1)
    adapter.inspect_device("r1")
    assert len(fetches) == 2, len(fetches)


def test_create_then_read_pays_one_post_mutation_fetch():
    """The measured execute-window pattern: create (existence reads +
    postcondition) followed by a read-back reuses the post-mutation snapshot
    for the whole burst — but the mutation itself forces exactly one real
    re-read, so the read-back cannot answer from pre-mutation data."""
    adapter = BridgeAdapter(port=59999, token=TEST_TOKEN, host_bridge=False)
    present = {"r1": False}
    fetches: list[str] = []

    def fake_send(expression, *, statements=None, wait=None):
        text = " ".join(statements or []) + " " + str(expression)
        if "JSON.stringify" in text and "devices" in text:
            fetches.append(text)
            if present["r1"]:
                return Observable.known(_DEVICES_JSON)
            return Observable.known('{"devices":[]}')
        if "lwAddDevice" in text:
            present["r1"] = True  # PT honoured the create
            return Observable.known("true")
        return Observable.known("true")

    adapter.send_expression = fake_send  # type: ignore[method-assign]
    adapter.inspect_device("r1")
    pre = len(fetches)
    receipt = adapter.create_device("r1", "router")
    adapter.inspect_device("r1")
    post = len(fetches)
    assert receipt.result.value == "success", (receipt.result, receipt.error)
    # One fetch inside create (post-invalidation existence read) plus none for
    # the follow-up inspect served from that snapshot.
    assert post - pre <= 1, post - pre
    assert post >= pre + 1  # the mutation did force a real re-read
