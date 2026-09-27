# Testing

The project ships a pytest suite covering the engine, the transports and the
verification layer.

```bash
pip install -e ".[test]"
python -m pytest -q
```

Run it from the repository root — a few tests read files by path relative to
the root.

## What's covered

- **Spec model and lint** — schema validation, honest roles, DHCP-server rules.
- **IPAM** — deterministic addressing plan and validator.
- **Port/media/module solver** — port existence, media compatibility, module
  requirements, port reservation.
- **Preflight** — the hard gate that blocks execution before any PT mutation.
- **Compiler and DAG** — every emitted operation, dependency ordering, refusals.
- **Executor and safety** — safety modes, mutation budgets, idempotent reuse,
  dependent blocking, receipts with read-back postconditions.
- **Verification** — per-criterion verdicts, UNKNOWN-never-PASS, functional-layer
  reporting, acceptance evaluation, semantic diff, diagnosis, bounded repair.
- **Transports** — HTTP bridge endpoints, the file mailbox protocol, token
  generation and validation, ping classification.
- **Live Packet Tracer** — `tests/autopilot/test_live_*.py`.

## Live tests

Live tests need Packet Tracer running with the extension loaded. They **skip
themselves** when neither channel answers, so the offline suite stays green on a
machine without Packet Tracer:

| File | What it proves |
| --- | --- |
| `test_live_contract.py` | the transport contract against a live canvas |
| `test_live_connectivity.py` | wireless association, IoT, cloud/DSL, ASA traversal and policy |
| `test_live_policy.py` | router ACL enforcement, ASA address scoping, ASA Vlan read-back |
| `test_scale_live.py` | a larger build end to end |

!!! warning "A green suite does not prove Packet Tracer integration"
    Live tests skip when PT is absent. An offline pass means the engine is
    internally consistent — it is **not** evidence that the live canvas behaves
    as expected. Run the live suite before trusting a build.

!!! note "The server boot is not covered by unit tests"
    Tests exercise the engine directly. To verify the MCP server actually boots
    (and that dependencies resolve), run `python -m network_autopilot` — it
    should start without errors.
