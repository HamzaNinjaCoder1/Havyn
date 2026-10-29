#!/usr/bin/env python
"""Run one Stage 3 live lab against Packet Tracer.

    python probes/stage3_live_lab.py library/stage3/S3-A1-ospf-single.json

The lab executes the compiler's own DAG (create -> module -> cable ->
configure -> host), runs every flow criterion against the live network, writes
a JSON + markdown record under ``probes/live/``, and deletes exactly the
devices it created.  A flow that is not PASS is reported as FAIL/UNKNOWN -
never coerced to a green result.

``--failover-device`` shuts the named router's interface down after the first
round and runs the flows again, which is the HSRP gate (S3.A8).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter
from network_autopilot.compiler import compile_spec
from network_autopilot.core.network_spec import NetworkSpec
from network_autopilot.ipam import plan_addressing
from network_autopilot.wireless import (
    WirelessAssociationError,
    associate_wireless_client,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "probes" / "live"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ops(result, kind: str) -> list:
    return [
        node.operation for node in result.dag.nodes.values()
        if node.operation.operation_type == kind
    ]


def _execute(adapter: BridgeAdapter, result, tag: str) -> tuple[list[str], list[str]]:
    created: list[str] = []
    problems: list[str] = []
    for op in _ops(result, "create_device"):
        name = f"{tag}-{op.inputs['name']}"
        position = op.inputs.get("position") or (160, 160)
        receipt = adapter.create_device(
            name, op.inputs.get("role", "router"),
            model_hint=op.inputs.get("model"),
            position=(int(position[0]), int(position[1])),
        )
        if receipt.result.value != "success":
            problems.append(f"create {name}: {receipt.error}")
        created.append(name)
    for op in _ops(result, "install_module"):
        receipt = adapter.install_module(
            f"{tag}-{op.inputs['device']}",
            op.inputs["slot"], op.inputs["module"], op.inputs["expected_port"],
        )
        if receipt.result.value != "success":
            problems.append(f"module {op.inputs['device']}: {receipt.error}")
    for op in _ops(result, "create_link"):
        media = op.inputs.get("media", "auto")
        if media == "wireless":
            continue
        receipt = adapter.create_link(
            f"{tag}-{op.inputs['a_device']}", op.inputs["a_port"],
            f"{tag}-{op.inputs['b_device']}", op.inputs["b_port"], media=media,
        )
        if receipt.result.value != "success":
            problems.append(f"link {op.inputs['link_id']}: {receipt.error}")
    for op in _ops(result, "configure_ios"):
        receipt = adapter.configure_ios(
            f"{tag}-{op.inputs['device']}", op.inputs.get("commands", [])
        )
        if receipt.result.value != "success":
            problems.append(f"ios {op.inputs['device']}: {receipt.error}")
        time.sleep(1.0)
    for op in _ops(result, "configure_host"):
        receipt = adapter.configure_host(
            f"{tag}-{op.inputs['device']}",
            dhcp=bool(op.inputs.get("dhcp")),
            ip=op.inputs.get("ip"),
            mask=op.inputs.get("mask"),
            gateway=op.inputs.get("gateway"),
            dns=op.inputs.get("dns"),
        )
        if receipt.result.value != "success":
            problems.append(f"host {op.inputs['device']}: {receipt.error}")
    for op in _ops(result, "install_wireless_radio"):
        answer = adapter.ensure_wireless_radio(
            f"{tag}-{op.inputs['device']}",
            module=op.inputs.get("module", "PT-LAPTOP-NM-1W"),
            slot=op.inputs.get("slot", "0"),
            interface=op.inputs.get("interface", "Wireless0"),
        )
        if not answer.get("installed"):
            problems.append(
                f"wireless radio {op.inputs['device']}: {answer.get('error') or answer}"
            )
    for op in _ops(result, "associate_wireless"):
        try:
            associate_wireless_client(
                adapter,
                f"{tag}-{op.inputs['client']}",
                f"{tag}-{op.inputs['access_point']}",
                interface=op.inputs.get("client_interface", "Wireless0"),
                channel=op.inputs.get("channel"),
            )
        except WirelessAssociationError as exc:
            problems.append(f"wireless {op.inputs['client']}: {exc}")
    for op in _ops(result, "configure_server"):
        receipt = adapter.configure_server_services(
            f"{tag}-{op.inputs['device']}", op.inputs.get("services", [])
        )
        if receipt.result.value != "success":
            problems.append(f"server {op.inputs['device']}: {receipt.error}")
    for op in _ops(result, "configure_cloud"):
        receipt = adapter.configure_cloud_mapping(
            f"{tag}-{op.inputs['device']}", op.inputs.get("mappings", [])
        )
        if receipt.result.value != "success":
            problems.append(f"cloud {op.inputs['device']}: {receipt.error}")
    for op in _ops(result, "generate_rsa_key"):
        receipt = adapter.generate_rsa_key(
            f"{tag}-{op.inputs['device']}", bits=int(op.inputs.get("bits", 1024))
        )
        if receipt.result.value != "success":
            problems.append(f"ssh key {op.inputs['device']}: {receipt.error}")
    return created, problems


def _flows(adapter: BridgeAdapter, spec: NetworkSpec, planning, tag: str) -> list[dict]:
    results: list[dict] = []
    for criterion in spec.acceptance_criteria:
        test = criterion.test
        if getattr(test, "kind", "") == "wireless":
            client = f"{tag}-{test.client}"
            assoc = adapter.wireless_association(
                client, interface=test.interface
            )
            actual = "associated" if (
                assoc is not None
                and assoc.access_point == f"{tag}-{test.access_point}"
            ) else "not-associated"
            results.append({
                "criterion": criterion.id,
                "kind": "wireless",
                "source": test.client,
                "destination": test.access_point,
                "expect": test.expect,
                "observed": actual,
                "pass": actual == test.expect,
                "detail": str(assoc.to_dict()) if assoc else "no antenna link",
            })
            continue
        if getattr(test, "kind", "") != "flow":
            continue
        source = f"{tag}-{test.source}"
        destination = f"{tag}-{test.destination}"
        kwargs = {}
        if test.destination_ip:
            kwargs["destination_ip"] = test.destination_ip
        flow = adapter.run_flow_test(
            source, destination, transport=test.transport,
            port=test.port, **kwargs,
        )
        actual = flow.outcome.value
        results.append({
            "criterion": criterion.id,
            "source": test.source,
            "destination": test.destination,
            "expect": test.expect,
            "observed": actual,
            "pass": actual == test.expect,
            "detail": str(flow.detail)[:500] if flow.detail else "",
        })
    return results


def run(spec_path: Path, *, failover_device: str = "", failover_interface: str = "",
        settle: float = 15.0, dump_config: bool = False, keep: bool = False) -> int:
    spec = NetworkSpec.from_json(spec_path.read_text(encoding="utf-8"))
    planning = plan_addressing(spec)
    result = compile_spec(spec, planning)
    adapter = BridgeAdapter(host_bridge=False)
    info = adapter.ping(timeout=5)
    if not info.reachable:
        print("PT UNREACHABLE:", info.detail)
        return 4
    tag = f"q{uuid.uuid4().hex[:6]}"
    record = {
        "lab": spec.name,
        "spec": str(spec_path),
        "tag": tag,
        "started_at": _now(),
        "criteria": [],
        "problems": [],
    }
    created: list[str] = []
    code = 2
    try:
        created, problems = _execute(adapter, result, tag)
        record["problems"] = problems
        print(f"devices={len(created)} problems={len(problems)}", flush=True)
        # DHCP leases and routing adjacencies need time to converge; a static
        # lab is ready in seconds. Never shorten this to hide a failure.
        time.sleep(settle + 8.0 if any(a.dhcp for a in planning.assignments) else settle)
        record["criteria"] = _flows(adapter, spec, planning, tag)
        if dump_config:
            configs = {}
            for device in spec.devices:
                role = str(getattr(device.role, "value", device.role))
                if role not in ("router", "l3_switch", "switch"):
                    continue
                name = f"{tag}-{device.id}"
                running = adapter._console_show_full(name, "show running-config")
                entry: dict[str, str] = {
                    "running": str(running.value) if running.is_known
                    else f"UNKNOWN: {running.detail}",
                }
                if role in ("router", "l3_switch"):
                    brief = adapter._console_show_full(name, "show ip interface brief")
                    entry["interfaces"] = (
                        str(brief.value) if brief.is_known
                        else f"UNKNOWN: {brief.detail}"
                    )
                    standby = adapter._console_show_full(name, "show standby brief")
                    entry["standby"] = (
                        str(standby.value) if standby.is_known
                        else f"UNKNOWN: {standby.detail}"
                    )
                configs[device.id] = entry
            record["configs"] = configs
        if failover_device:
            active = f"{tag}-{failover_device}"
            if failover_interface == "all":
                interfaces = ["GigabitEthernet0/0", "GigabitEthernet0/1"]
            else:
                interfaces = [failover_interface or "GigabitEthernet0/1"]
            print(f"failover: shutting {active} {interfaces}", flush=True)
            commands: list[str] = []
            for iface in interfaces:
                commands.extend([f"interface {iface}", " shutdown", " exit"])
            adapter.configure_ios(active, commands)
            time.sleep(12.0)
            record["after_failover"] = _flows(adapter, spec, planning, tag)
    finally:
        delete = {}
        if created and not keep:
            try:
                delete = adapter.delete_devices_bulk(created)
            except Exception as exc:
                record["problems"].append(f"cleanup: {type(exc).__name__}: {exc}")
        elif keep:
            record["kept"] = created
        record["cleanup"] = delete
        record["finished_at"] = _now()
        OUT.mkdir(parents=True, exist_ok=True)
        stem = spec_path.stem
        (OUT / f"{stem}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        lines = [
            f"# {spec.name} — live lab",
            "",
            f"- spec: `{spec_path}`",
            f"- tag: `{tag}`",
            f"- started: {record['started_at']}",
            f"- finished: {record['finished_at']}",
            f"- problems: {len(record['problems'])}",
            "",
            "| criterion | source | destination | expected | observed | verdict |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in record["criteria"]:
            lines.append(
                f"| {row['criterion']} | {row['source']} | {row['destination']} | "
                f"{row['expect']} | {row['observed']} | "
                f"{'PASS' if row['pass'] else 'FAIL'} |"
            )
        if record.get("after_failover"):
            lines.extend(["", "## After failover", "",
                          "| criterion | expected | observed | verdict |",
                          "| --- | --- | --- | --- |"])
            for row in record["after_failover"]:
                lines.append(
                    f"| {row['criterion']} | {row['expect']} | {row['observed']} | "
                    f"{'PASS' if row['pass'] else 'FAIL'} |"
                )
        if record["problems"]:
            lines.extend(["", "## Problems", ""])
            lines.extend(f"- {p}" for p in record["problems"])
        (OUT / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        passed = all(row["pass"] for row in record["criteria"])
        if record.get("after_failover"):
            passed = passed and all(row["pass"] for row in record["after_failover"])
        print("RESULT:", "PASS" if passed else "FAIL", f"({OUT / (stem + '.md')})")
        code = 0 if passed else 2
    return code


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    parser.add_argument("--failover-device", default="")
    parser.add_argument("--failover-interface", default="")
    parser.add_argument("--settle", type=float, default=15.0)
    parser.add_argument("--dump-config", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv[1:])
    return run(
        Path(args.spec), failover_device=args.failover_device,
        failover_interface=args.failover_interface, settle=args.settle,
        dump_config=args.dump_config, keep=args.keep,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
