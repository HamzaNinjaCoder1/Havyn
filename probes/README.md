# Stage 3 probe and live-lab evidence

Everything here is reproducible against a running Packet Tracer on the Script
Engine file channel. Nothing in this directory is hand-written evidence: the
`raw/` files are verbatim console captures, and `live/` is written by
`stage3_live_lab.py` after the flows run.

| Path | What it is |
| --- | --- |
| `stage3_probe.py` | Router, switch and ASA feature probes; writes `raw/` and `stage3-probe-results.json`. |
| `stage3_live_lab.py` | One live lab: creates the compiler's DAG, runs the flow criteria, writes `live/<spec>.{md,json}`, deletes only the names it created. |
| `stage3_ipv6_probe.py` | Two-router IPv6 + OSPFv3 probe, including a real IPv6 ping. |
| `stage3_gre_probe.py` | Two-router GRE tunnel probe with `tunnel source <interface>` and a real ping. |
| `stage3_asa_read.py`, `stage3_asa_shows.py` | ASA boot wait, push and console/show probes. |
| `raw/` | Verbatim PT output for every probe. |
| `live/` | Live-lab records and the switch probe report. |
| `failing-first.txt` | The new tests failing on the Stage 2 baseline and passing on this tree. |
| `a2-multi-area-refusal.md` | The OSPF multi-area refusal evidence. |
| `router.md`, `asa.md`, `wireless.md`, `cloud.md`, `end-devices.md`, `server-services.md`, `iot.md` | Per-track probe summaries and verdicts. |

## Recommended order

```bash
python probes/stage3_probe.py
python probes/stage3_live_lab.py library/stage3/S3-A1-ospf-single.json
python probes/stage3_live_lab.py library/stage3/S3-A2-ospf-multi-area.json --dump-config
python probes/stage3_live_lab.py library/stage3/S3-A3-eigrp.json
python probes/stage3_live_lab.py library/stage3/S3-A4-ripv2.json
python probes/stage3_live_lab.py library/stage3/S3-A5-default-redistribute.json
python probes/stage3_live_lab.py library/stage3/S3-A7-dhcp-relay.json
python probes/stage3_live_lab.py library/stage3/S3-A8-hsrp.json --failover-device r1 --failover-interface all
python probes/stage3_live_lab.py library/stage3/S3-A12-bgp.json
python probes/stage3_live_lab.py library/T04-serial-wan.json
python probes/stage3_live_lab.py library/T10-nat-internet.json
python probes/stage3_ipv6_probe.py
python probes/stage3_gre_probe.py
```

The labs delete their own devices in a `finally` block. `--keep` is for
debugging and prints the names it left behind so they can be removed.

## Honest limits

* The ASA has no `getCommandLine()`; `getConsoleLine()` works but `enable`
  asks for a password the engine does not have, so C2–C6 have push evidence
  but no post-push read-back.
* Server-PT services and IoT registration have no Script Engine API; see
  `server-services.md` and `iot.md`.
* The A5/A6/A7/A9 labs show a PASSing flow while the adapter still reports a
  static route missing on read-back. The flow is the stronger proof; the
  read-back defect is recorded, not hidden.
