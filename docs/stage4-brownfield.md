# Stage 4 — Brownfield Network Intelligence

Stage 4 lets Havyn work on a network it did not build: import it, read and
parse its configuration, turn it into an as-is `NetworkSpec`, confirm what the
network must do, trace and diagnose faults, back up before touching anything,
repair minimally with rollback, enhance safely, and score the diagnosis engine
against injected faults.

## Pipeline

```
existing network
  -> importer (topology + host config + running config)
  -> parser (typed structures + unparsed statements + line traceability)
  -> as-is intent (NetworkSpec + KNOWN/PARTIAL/UNKNOWN confidence)
  -> confirmed objectives (explicit; inferred stay unconfirmed)
  -> hop-by-hop trace + layer-ordered diagnosis
  -> standardized diagnosis report
  -> verified backup
  -> minimal repair (SafetyGate + approval)
  -> objective + regression re-verification
  -> keep or roll back
```

Enhancement uses the same safety path with a structured diff: conflicts (IP
overlap, VLAN collision, naming, capacity), a preview of additions /
modifications / removals, approval, only the changed operations, and full
re-verification of the original criteria.

## Usage

```bash
# Import a live network and build the as-is spec/round-trip report
python probes/stage4_brownfield_lab.py

# Live enhancement proof
python probes/stage4_live_enhance.py

# Live ASA / STP / ARP / NAT / rollback / multi-fault evidence
python probes/stage4_live_asa.py
python probes/stage4_live_stp.py
python probes/stage4_live_arp.py
python probes/stage4_live_nat.py
python probes/stage4_live_rollback.py
python probes/stage4_live_faults.py

# Deterministic fault-injection + enhancement benchmarks
python scripts/stage4_benchmark.py

# Final scorecard
python scripts/stage4_scorecard.py
```

In Python:

```python
from network_autopilot.brownfield.importer import BrownfieldImporter
from network_autopilot.brownfield.intent import AsIsIntentBuilder, RoundTripValidator
from network_autopilot.brownfield.objectives import ObjectiveRegistry
from network_autopilot.brownfield.faults import FaultDetector, ExpectedNetwork

imported = BrownfieldImporter(adapter).import_network()
intent = AsIsIntentBuilder().build(imported)
round_trip = RoundTripValidator().validate(intent, imported)

objectives = ObjectiveRegistry()
objective = objectives.add_user_requirement("pc1 must communicate with pc2")
detector = FaultDetector(
    imported.inventory, imported.corpus, objectives,
    expected=ExpectedNetwork.from_spec(intent.spec),
)
diagnostics = detector.diagnose(objective)
```

## Evidence categories

- **LIVE VERIFIED** — demonstrated against actual Packet Tracer.
- **OFFLINE VERIFIED** — automated tests or deterministic simulation.
- **IMPLEMENTED — NOT LIVE VERIFIED** — code exists, live proof missing.
- **UNSUPPORTED** — platform limitation with evidence.
- **FAILED** — tested and did not pass.

See `docs/reports/stage4-scorecard.md` and the raw artifacts under
`probes/raw/stage4-*` and `probes/live/stage4-*`.

## Known limitations

- The 96-scenario benchmark is deterministic simulation built by the real
  compiler, not live Packet Tracer injection. Live fault evidence is separate
  and covers a subset of classes.
- The current compiler does not emit L3-switch routed-port addresses/static
  routes. The round trip records those as unsupported semantics instead of
  claiming equivalence.
- A `NetworkSpec` link stores one trunk allowed-VLAN list; asymmetric per-side
  lists are documented as unsupported.
- Linksys has no running-config read-back and reports UNSUPPORTED.
- ASA operational tables remain constrained by its CLI; the ASA running
  configuration read is live verified.
- ARP and NAT are observed live; NAT is not yet a path-decision fault rule and
  ARP is evidence rather than a fault class.
- STP blocking is a fault only when no alternate forwarding path exists.
- The Stage 1.19 performance gate remains unresolved; Stage 3 is not declared
  released.
