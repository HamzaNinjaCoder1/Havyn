"""Catalog auto-probe (S1.12 / F9): PT's truth vs the device catalog.

For every model Packet Tracer actually exposes, this creates the device,
reads its real ports back, and deletes it again — then diffs what PT said
against `pt/catalog/devices.py`. A wrong catalog means wrong cabling, so a
mismatch is a finding, not noise.

    python scripts/pt_catalog_probe.py [--limit N] [--only 2911,3560-24PS]
                                       [--out probe.json] [--dry-run]

Live requirements: PT open with the MCP Control Center extension (either
channel). Nothing but `zz-probe-*` devices is ever created or deleted, and
every probe device is removed in a finally — a failed read still cleans up.

Exit codes: 0 catalog matches PT · 1 engine error · 2 mismatches found ·
4 Packet Tracer unreachable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from network_autopilot.core.observability import Observable

PROBE_PREFIX = "zz-probe-"

#: Candidate enumeration expressions, tried in order. The blueprint names
#: `allDeviceTypes`, but that surface was never confirmed in-repo (rule 6),
#: so the probe records WHICH candidate answered and the report carries the
#: evidence. First KNOWN answer wins.
ENUMERATION_CANDIDATES = (
    "allDeviceTypes",
    "getAllDeviceTypes",
    "getDeviceTypes",
    "deviceTypes",
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISMATCH = 2
EXIT_UNREACHABLE = 4


@dataclass
class ModelProbe:
    """What PT said about one model."""

    model: str
    actual_ports: list[str] = field(default_factory=list)
    observed_model: str | None = None
    type_evidence: str | None = None
    error: str | None = None
    cleaned: bool = False


@dataclass
class ProbeReport:
    """The whole run, JSON-serialisable for the runner (S2.2)."""

    enumeration_api: str | None = None
    enumeration_raw: str | None = None
    probed: list[ModelProbe] = field(default_factory=list)
    catalog_missing: list[str] = field(default_factory=list)
    mismatches: list[dict] = field(default_factory=list)
    logical_notes: list[dict] = field(default_factory=list)
    cleanup_failures: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {
            "enumeration_api": self.enumeration_api,
            "enumeration_raw": self.enumeration_raw,
            "models_probed": len(self.probed),
            "matches": len(self.probed) - len(self.mismatches)
            - len(self.catalog_missing),
            "catalog_missing": list(self.catalog_missing),
            "mismatches": list(self.mismatches),
            "probed": [
                {
                    "model": p.model,
                    "actual_ports": list(p.actual_ports),
                    "observed_model": p.observed_model,
                    "error": p.error,
                    "cleaned": p.cleaned,
                }
                for p in self.probed
            ],
            "logical_notes": list(self.logical_notes),
            "cleanup_failures": list(self.cleanup_failures),
            "errors": [
                {"model": p.model, "error": p.error}
                for p in self.probed
                if p.error
            ],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _enum_expression(candidate: str) -> str:
    return (
        "(function(){try{"
        f"var n=ipc.network();var f=n[{candidate!r}];"
        "if(typeof f==='function'){return JSON.stringify(f.call(n));}"
        "if(typeof f!=='undefined'){return JSON.stringify(f);}"
        "return 'NOPE';"
        "}catch(e){return 'THREW:'+String((e&&e.message)||e);}})()"
    )


def _type_expression() -> str:
    return (
        "(function(){try{var n=ipc.network();var out={};"
        "for(var k of ['getDeviceType','deviceType','getType','type']){"
        "try{var f=n[k];out[k]=typeof f==='function'?'function':String(f).slice(0,80);}"
        "catch(e){out[k]='threw';}}"
        "return JSON.stringify(out);"
        "}catch(e){return 'THREW:'+String((e&&e.message)||e);}})()"
    )


def enumerate_models(adapter) -> tuple[list[str], str, str]:
    """Ask PT for its model list. Returns (models, api_used, raw).

    Raises RuntimeError when no candidate answers.
    """
    for candidate in ENUMERATION_CANDIDATES:
        answer = adapter.send_expression(_enum_expression(candidate), wait=20.0)
        if not answer.is_known or not answer.value:
            continue
        raw = str(answer.value)
        if raw in ("NOPE",) or raw.startswith("THREW:"):
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        models = _model_names(parsed)
        if models:
            return models, candidate, raw
    raise RuntimeError(
        "no device-type enumeration answered; tried "
        + ", ".join(ENUMERATION_CANDIDATES)
    )


def _model_names(parsed) -> list[str]:
    if isinstance(parsed, list):
        out = []
        for item in parsed:
            if isinstance(item, str) and item:
                out.append(item)
            elif isinstance(item, dict):
                for key in ("name", "model", "type", "pt_type"):
                    if item.get(key):
                        out.append(str(item[key]))
                        break
        return out
    if isinstance(parsed, dict):
        return [str(k) for k in parsed if k]
    return []


def probe_model(adapter, model: str, index: int) -> ModelProbe:
    """Create → read ports → delete (always) for one model."""
    from network_autopilot.pt.catalog import get_valid_ports
    from network_autopilot.pt.catalog.aliases import pt_model_string

    probe = ModelProbe(model=model)
    name = f"{PROBE_PREFIX}{index}-{model}".replace(" ", "_")[:60]
    cleanup_attempted = False
    try:
        # The probe tests the same string the engine sends: PT places some
        # chassis only under its own spelling (S1.12).
        receipt = adapter.create_device(
            name,
            role="other",
            model_hint=pt_model_string(model),
            position=(100 + (index % 25) * 50, 100 + (index // 25) * 50),
        )
        if receipt.result.value not in ("success",):
            probe.error = f"create refused: {receipt.error}"
            return probe
        try:
            observation = adapter.inspect_device(name)
            probe.actual_ports = sorted(observation.ports.keys())
            if observation.model.is_known:
                probe.observed_model = str(observation.model.value)
            typed = adapter.send_expression(_type_expression(), wait=20.0)
            if typed.is_known and typed.value:
                probe.type_evidence = str(typed.value)[:500]
            _ = get_valid_ports  # catalog read happens in diff, not here
        finally:
            gone = adapter.delete_device(name)
            cleanup_attempted = True
            probe.cleaned = gone.result.value == "success"
            if not probe.cleaned:
                probe.error = (probe.error + "; " if probe.error else "") + (
                    f"cleanup failed: {gone.error}"
                )
    except Exception as exc:  # a dead channel must not kill the whole run
        probe.error = f"{type(exc).__name__}: {exc}"
        if not cleanup_attempted:
            try:
                gone = adapter.delete_device(name)
                probe.cleaned = gone.result.value == "success"
            except Exception:
                pass
    return probe


#: PT's virtual interfaces: reported by port enumeration but never cableable,
#: so never catalog valid ports — unless the catalog declares one on purpose
#: (the ASA 5505's Vlan SVIs, which its routed config addresses). Measured
#: live on PT 9.0.1 (S1.12): a fresh chassis enumerates its cableable ports
#: plus, depending on model, Vlan SVIs, Bluetooth, Wireless radios, PPP
#: Dialer/Virtual-Access, cellular stubs, a "Backbone" trunk and Meraki's
#: numbered "Internet N" WAN stubs.
LOGICAL_PORT_RE = (
    r"(?:Vlan|VLAN|Loopback|Null)\d*"
    r"|Bluetooth"
    r"|Wireless[\s\d/]*"
    r"|Dialer\d*"
    r"|Virtual-Access\d*"
    r"|Cellular.*"
    r"|Backbone"
    r"|3G/4G Cell\d*"
    r"|Internet \d+"
)


def _split_logical(ports: set[str]) -> tuple[set[str], set[str]]:
    """(physical, logical) split of PT-reported ports."""
    import re

    logical = {p for p in ports if re.fullmatch(LOGICAL_PORT_RE, p)}
    return set(ports) - logical, logical


def diff_against_catalog(
    probed: list[ModelProbe],
) -> tuple[list[str], list[dict], list[dict]]:
    """Split probe results into (missing, mismatches, logical_notes).

    A mismatch names the model, what the catalog claims, and what PT showed.
    Order-independent: ports are sets on both sides. Logical interfaces PT
    reports (Vlan1 and kin) are acknowledged in logical_notes — they are
    real but never cableable, so they must not fail the diff. A logical
    interface the catalog *declares* (the ASA 5505's Vlan SVIs) is part of
    the contract, not a stray: it stays on the physical side of the diff.
    """
    from network_autopilot.pt.catalog import get_valid_ports, resolve_model

    missing: list[str] = []
    mismatches: list[dict] = []
    logical_notes: list[dict] = []
    for probe in probed:
        if probe.error and not probe.actual_ports:
            continue  # nothing observed; the error is recorded, not diffed
        if resolve_model(probe.model) is None:
            missing.append(probe.model)
            continue
        catalog_ports = set(get_valid_ports(probe.model))
        actual = set(probe.actual_ports)
        physical, logical = _split_logical(actual)
        # A declared port is the catalog's claim, not a stray virtual
        # interface: keep it on the physical side (5505 Vlan1/Vlan2).
        logical -= catalog_ports
        physical = actual - logical
        if logical:
            logical_notes.append({
                "model": probe.model,
                "logical_only": sorted(logical),
            })
        if catalog_ports != physical:
            mismatches.append({
                "model": probe.model,
                "catalog_only": sorted(catalog_ports - physical),
                "actual_only": sorted(physical - catalog_ports),
            })
    return missing, mismatches, logical_notes


def catalog_models() -> list[str]:
    """Every model the catalog claims PT supports (S1.12's probe set when PT
    itself exposes no enumeration surface — measured live: `ipc.network()`
    has getDevice/getDeviceAt/getDeviceCount but no allDeviceTypes)."""
    from network_autopilot.pt.catalog import all_models

    names: list[str] = []
    for entry in all_models():
        name = entry if isinstance(entry, str) else str(
            getattr(entry, "model", getattr(entry, "name", entry))
        )
        if name and name not in names:
            names.append(name)
    return names


def run_probe(adapter, *, limit: int | None = None,
              only: list[str] | None = None,
              models: list[str] | None = None) -> ProbeReport:
    """Full probe run against a live adapter. See module docstring."""
    from network_autopilot.core.ids import utc_now_iso

    report = ProbeReport(started_at=utc_now_iso())
    if models is None:
        models, api_used, raw = enumerate_models(adapter)
        report.enumeration_api = api_used
        report.enumeration_raw = raw[:2000]
    else:
        report.enumeration_api = "catalog-fallback"
        report.enumeration_raw = (
            "PT exposes no device-type enumeration "
            "(ipc.network() keys measured live); probing the catalog's "
            f"{len(models)} supported models instead"
        )
    if only:
        wanted = set(only)
        models = [m for m in models if m in wanted]
    if limit is not None:
        models = models[:limit]
    for index, model in enumerate(models):
        probe = probe_model(adapter, model, index)
        report.probed.append(probe)
        if not probe.cleaned:
            report.cleanup_failures.append(model)
    (report.catalog_missing, report.mismatches,
     report.logical_notes) = diff_against_catalog(report.probed)
    report.finished_at = utc_now_iso()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pt_catalog_probe",
        description="Probe every PT model and diff its ports against the catalog.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", default=None,
                        help="comma-separated models to probe")
    parser.add_argument("--from-catalog", action="store_true",
                        help="probe the catalog's supported models instead of "
                        "asking PT to enumerate (PT has no such surface)")
    parser.add_argument("--out", default=None,
                        help="write the JSON report here (default prints summary only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="reachability check only; probe nothing")
    args = parser.parse_args(argv)

    from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
    from network_autopilot.pt.mailbox import FileMailbox

    adapter = BridgeAdapter(host_bridge=False, file_bridge=FileMailbox())
    probe = adapter.ping(timeout=5.0)
    if not probe.reachable:
        print("Packet Tracer is not reachable on any channel.", file=sys.stderr)
        return EXIT_UNREACHABLE
    if args.dry_run:
        print("PT reachable; dry run, nothing probed.")
        return EXIT_OK

    t0 = time.monotonic()
    try:
        report = run_probe(
            adapter,
            limit=args.limit,
            only=args.only.split(",") if args.only else None,
            models=catalog_models() if args.from_catalog else None,
        )
    except RuntimeError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    elapsed = time.monotonic() - t0

    data = report.to_dict()
    print(f"enumeration API: {data['enumeration_api']}")
    print(f"models probed: {data['models_probed']} in {elapsed:.0f}s")
    print(f"matches: {data['matches']}")
    if data["catalog_missing"]:
        print("missing from catalog: " + ", ".join(data["catalog_missing"]))
    for mismatch in data["mismatches"]:
        print(
            f"MISMATCH {mismatch['model']}: catalog-only "
            f"{mismatch['catalog_only']}; actual-only {mismatch['actual_only']}"
        )
    for note in data["logical_notes"]:
        print(f"LOGICAL {note['model']}: {note['logical_only']} "
              "(reported by PT, never cableable, not a mismatch)")
    if data["errors"]:
        for entry in data["errors"]:
            print(f"ERROR {entry['model']}: {entry['error']}")
    if data["cleanup_failures"]:
        print("CLEANUP FAILURES: " + ", ".join(data["cleanup_failures"]))
        return EXIT_ERROR
    if args.out:
        Path(args.out).write_text(
            json.dumps(data, indent=1), encoding="utf-8"
        )
        print(f"report written to {args.out}")
    if data["mismatches"] or data["catalog_missing"]:
        return EXIT_MISMATCH
    print("catalog matches PT on every probed model.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
