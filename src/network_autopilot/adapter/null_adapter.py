"""Deterministic in-memory adapter — structural, honest about its limits.

It knows devices, ports, links and recorded configuration strings, which makes
the adapter contract testable offline and gives DRY_RUN something real to
plan against. It deliberately has NO data plane: `run_flow_test` returns
UNKNOWN, because a fabricated "reachable" would be exactly the class of lie
this product exists to eliminate.
"""

from __future__ import annotations

import json
import threading

from ..core.actual_state import DeviceObservation, PortObservation
from ..core.ids import new_id, utc_now_iso
from ..core.network_spec import default_model_for_role
from ..core.observability import Observable
from ..core.receipts import CheckResult, OperationReceipt, OperationResult
from ..core.requirements import FlowOutcome, FlowResult
from ..pt.catalog import resolve_model, resolve_module, ports_for_slot
from .base import AdapterInfo, PacketTracerAdapter


def _catalog_ports(model_name: str) -> list[str]:
    """The model's ports as the catalog declares them - the same source of truth
    the real solver and deploy use. The null adapter used to carry its own list
    (router = 3 GE), which no longer matches a real 2911 (2 onboard + modules),
    so an offline test did not reflect the topology the backend would build."""
    model = resolve_model(model_name)
    if model is None:
        return ["FastEthernet0"]
    return [p.full_name for p in model.ports]


class NullAdapter(PacketTracerAdapter):
    name = "null"
    version = "0.1.0"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # name -> {role, model, position, ports: {port: {config, connected_to}}}
        self._devices: dict[str, dict] = {}
        self._links: list[dict] = []
        self._project: str | None = None

    # -- helpers ------------------------------------------------------------

    def _receipt(
        self,
        operation: str,
        requested: dict,
        operation_id: str | None,
        result: OperationResult,
        *,
        preconditions: list[CheckResult] | None = None,
        postconditions: list[CheckResult] | None = None,
        executed: dict | None = None,
        error: str | None = None,
    ) -> OperationReceipt:
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation=operation,
            requested=requested,
            executed=executed,
            result=result,
            preconditions=preconditions or [],
            postconditions=postconditions or [],
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def _ports_for(self, role: str, model_hint: str | None = None) -> list[str]:
        return _catalog_ports(model_hint or default_model_for_role(role))

    # -- probing ------------------------------------------------------------

    def ping(self, timeout: float = 5.0) -> AdapterInfo:
        return AdapterInfo(
            adapter_name=self.name,
            adapter_version=self.version,
            reachable=True,
            detail=Observable.known(
                "in-memory structural adapter; no Packet Tracer involved; "
                "no data plane (flow tests return UNKNOWN)"
            ),
        )

    # -- inspection ---------------------------------------------------------

    def device_exists(self, name: str) -> Observable[bool]:
        with self._lock:
            return Observable.known(name in self._devices, source="null-adapter")

    def list_device_names(self) -> Observable[list[str]]:
        with self._lock:
            return Observable.known(sorted(self._devices), source="null-adapter")

    def link_exists(
        self, a_device: str, a_port: str, b_device: str, b_port: str
    ) -> Observable[bool]:
        # Peer-aware (P1-3): both ports have to be connected TO EACH OTHER,
        # not merely each have some cable.
        with self._lock:
            a = self._devices.get(a_device, {}).get("ports", {}).get(a_port)
            b = self._devices.get(b_device, {}).get("ports", {}).get(b_port)
            linked = (
                a is not None
                and b is not None
                and a["connected_to"] == (b_device, b_port)
                and b["connected_to"] == (a_device, a_port)
            )
            return Observable.known(linked, source="null-adapter")

    def inspect_device(self, name: str) -> DeviceObservation:
        with self._lock:
            info = self._devices.get(name)
            if info is None:
                obs = DeviceObservation(name=name)
                obs.notes.append("not present in null adapter workspace")
                return obs
            observation = DeviceObservation(
                name=name,
                model=Observable.known(info["model"], source="null-adapter"),
                role=Observable.known(info["role"], source="null-adapter"),
            )
            self._reflect_ios_config(info)
            for port_name, port in sorted(info["ports"].items()):
                observation.ports[port_name] = PortObservation(
                    name=port_name,
                    state=Observable.known(
                        "up" if port["connected_to"] else "down", source="null-adapter"
                    ),
                    ip=Observable.from_raw(port["config"].get("ip"), source="null-adapter"),
                    mask=Observable.from_raw(port["config"].get("mask"), source="null-adapter"),
                    gateway=Observable.from_raw(
                        port["config"].get("gateway"), source="null-adapter"
                    ),
                    vlan=Observable.from_raw(port["config"].get("vlan"), source="null-adapter"),
                )
            return observation

    @staticmethod
    def _reflect_ios_config(info: dict) -> None:
        """Project recorded IOS commands into port config observations.

        The structural adapter stores `configure_ios` command lists; a real
        device would have applied them. Deterministically parsing the recorded
        `interface X` / `ip address A M` blocks keeps observations consistent
        with what was configured — without pretending to be IOS.
        """
        commands = info.get("ios_commands")
        if not commands:
            return
        current: str | None = None
        for command in commands:
            stripped = command.strip()
            lowered = stripped.lower()
            if lowered.startswith("interface "):
                current = stripped.split(None, 1)[1]
            elif lowered.startswith("no ip address") and current:
                # F5 reconcile: removing an address is a real config change the
                # double must reflect, or a stale address would survive reuse and
                # the read-back would "confirm" a state that is not there.
                if current in info["ports"]:
                    info["ports"][current]["config"].pop("ip", None)
                    info["ports"][current]["config"].pop("mask", None)
            elif lowered.startswith("ip address ") and current:
                parts = stripped.split()
                if len(parts) >= 4 and current in info["ports"]:
                    info["ports"][current]["config"]["ip"] = parts[2]
                    info["ports"][current]["config"]["mask"] = parts[3]

    # -- mutations ----------------------------------------------------------

    def create_device(
        self,
        name: str,
        role: str,
        model_hint: str | None = None,
        position: tuple[int, int] | None = None,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        requested = {"name": name, "role": role, "model_hint": model_hint, "position": position}
        exists = self.device_exists(name)
        pre = [CheckResult.of("device_absent", "False", exists, compare=lambda v, _: v is False)]
        if exists.is_known and exists.value:
            return self._receipt(
                "create_device", requested, operation_id, OperationResult.REJECTED,
                preconditions=pre, error="device already exists",
            )
        if not exists.is_known:
            return self._receipt(
                "create_device", requested, operation_id, OperationResult.UNKNOWN,
                preconditions=pre, error="existence could not be determined (fail-closed)",
            )

        model = model_hint or default_model_for_role(role)
        ports = {p: {"config": {}, "connected_to": None} for p in self._ports_for(role, model)}
        with self._lock:
            self._devices[name] = {
                "role": role, "model": model, "position": position or (0, 0), "ports": ports,
            }
        post = [
            CheckResult.of("device_present", "True", self.device_exists(name))
        ]
        return self._receipt(
            "create_device", requested, operation_id, OperationResult.SUCCESS,
            preconditions=pre, postconditions=post,
            executed={"model": model, "position": list(position or (0, 0))},
        )

    def rename_device(
        self, old_name: str, new_name: str, operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"old_name": old_name, "new_name": new_name}
        old_exists = self.device_exists(old_name)
        new_absent = self.device_exists(new_name)
        pre = [
            CheckResult.of("old_device_present", "True", old_exists),
            CheckResult.of(
                "new_name_free", "False", new_absent, compare=lambda v, _: v is False
            ),
        ]
        if not (old_exists.is_known and old_exists.value and new_absent.is_known and not new_absent.value):
            return self._receipt(
                "rename_device", requested, operation_id, OperationResult.REJECTED,
                preconditions=pre, error="precondition failed or not determinable",
            )
        with self._lock:
            self._devices[new_name] = self._devices.pop(old_name)
        post = [CheckResult.of("renamed_present", "True", self.device_exists(new_name))]
        return self._receipt(
            "rename_device", requested, operation_id, OperationResult.SUCCESS,
            preconditions=pre, postconditions=post,
        )

    def position_device(
        self, name: str, x: int, y: int, operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"name": name, "x": x, "y": y}
        exists = self.device_exists(name)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._receipt(
                "position_device", requested, operation_id, OperationResult.REJECTED,
                preconditions=pre, error="device missing",
            )
        with self._lock:
            self._devices[name]["position"] = (int(x), int(y))
        post = [
            CheckResult.of(
                "position_read_back",
                f"{int(x)},{int(y)}",
                Observable.known(
                    f"{self._devices[name]['position'][0]},{self._devices[name]['position'][1]}",
                    source="null-adapter",
                ),
            )
        ]
        return self._receipt(
            "position_device", requested, operation_id, OperationResult.SUCCESS,
            preconditions=pre, postconditions=post,
        )

    def create_link(
        self,
        a_device: str,
        a_port: str | None,
        b_device: str,
        b_port: str | None,
        media: str = "auto",
        operation_id: str | None = None,
    ) -> OperationReceipt:
        requested = {
            "a_device": a_device, "a_port": a_port,
            "b_device": b_device, "b_port": b_port, "media": media,
        }
        with self._lock:
            pre_checks: list[CheckResult] = []
            chosen: dict[str, str | None] = {"a": None, "b": None}
            ok = True
            for label, dev, port in (("a", a_device, a_port), ("b", b_device, b_port)):
                exists = dev in self._devices
                pre_checks.append(
                    CheckResult.of(
                        f"{label}_device_present", "True",
                        Observable.known(exists, source="null-adapter"),
                    )
                )
                if not exists:
                    ok = False
                    continue
                ports = self._devices[dev]["ports"]
                picked = port or next(
                    (p for p, v in ports.items() if v["connected_to"] is None), None
                )
                chosen[label] = picked
                free = (
                    picked is not None
                    and picked in ports
                    and ports[picked]["connected_to"] is None
                )
                pre_checks.append(
                    CheckResult.of(
                        f"{label}_port_free", "True",
                        Observable.known(free, source="null-adapter"),
                    )
                )
                if not free:
                    ok = False
            final_a, final_b = chosen["a"], chosen["b"]
            if not ok or final_a is None or final_b is None:
                return self._receipt(
                    "create_link", requested, operation_id, OperationResult.REJECTED,
                    preconditions=pre_checks, error="devices/ports unavailable",
                )
            self._devices[a_device]["ports"][final_a]["connected_to"] = (b_device, final_b)
            self._devices[b_device]["ports"][final_b]["connected_to"] = (a_device, final_a)
            self._links.append({"a": (a_device, final_a), "b": (b_device, final_b), "media": media})
        return self._receipt(
            "create_link", requested, operation_id, OperationResult.SUCCESS,
            preconditions=pre_checks,
            executed={"ports_reserved": [f"{a_device}:{final_a}", f"{b_device}:{final_b}"]},
        )

    def delete_device(
        self, name: str, operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"name": name}
        with self._lock:
            present = name in self._devices
            if not present:
                # Idempotent: already absent is a CONFIRMED success.
                obs = Observable.known(False, source="null-adapter")
                return self._receipt(
                    "delete_device", requested, operation_id, OperationResult.SUCCESS,
                    preconditions=[CheckResult.of("device_present", "False", obs)],
                    postconditions=[CheckResult.of("device_absent", "True", obs)],
                    executed={"decision": "already absent"},
                )
            # detach every link touching the device first
            self._links = [
                lnk for lnk in self._links
                if lnk["a"][0] != name and lnk["b"][0] != name
            ]
            del self._devices[name]
            gone = Observable.known(name not in self._devices, source="null-adapter")
        return self._receipt(
            "delete_device", requested, operation_id, OperationResult.SUCCESS,
            postconditions=[CheckResult.of("device_absent", "True", gone)],
        )

    def delete_link(
        self,
        a_device: str,
        a_port: str,
        b_device: str,
        b_port: str,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        requested = {
            "a_device": a_device, "a_port": a_port,
            "b_device": b_device, "b_port": b_port,
        }
        with self._lock:
            before = any(
                lnk["a"] == (a_device, a_port) or lnk["b"] == (b_device, b_port)
                for lnk in self._links
            )
            self._links = [
                lnk for lnk in self._links
                if not (lnk["a"] == (a_device, a_port) or lnk["b"] == (b_device, b_port))
            ]
            for dev, port in ((a_device, a_port), (b_device, b_port)):
                ports = self._devices.get(dev, {}).get("ports", {})
                if port in ports and ports[port]["connected_to"] is not None:
                    ports[port]["connected_to"] = None
            after = Observable.known(not before, source="null-adapter")
        return self._receipt(
            "delete_link", requested, operation_id, OperationResult.SUCCESS,
            postconditions=[CheckResult.of("link_absent", "True", after)],
            executed={"had_link": before},
        )

    def install_module(
        self,
        device: str,
        slot: str,
        module: str,
        expected_port: str,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        requested = {
            "device": device, "slot": slot, "module": module,
            "expected_port": expected_port,
        }
        exists = self.device_exists(device)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._receipt(
                "install_module", requested, operation_id, OperationResult.REJECTED,
                preconditions=pre, error="device missing",
            )
        spec = resolve_module(module)
        if spec is None:
            return self._receipt(
                "install_module", requested, operation_id, OperationResult.FAILED,
                preconditions=pre, error=f"unknown module {module!r}",
            )
        with self._lock:
            info = self._devices[device]
            ports = info["ports"]
            if expected_port in ports:
                # Idempotent: the module is already installed and its port exists.
                # That is a CONFIRMED success, not a second install.
                already = Observable.known(expected_port, source="null-adapter")
                return self._receipt(
                    "install_module", requested, operation_id, OperationResult.SUCCESS,
                    preconditions=pre,
                    postconditions=[CheckResult.of("module_port_visible", expected_port, already)],
                    executed={"decision": "module already installed"},
                )
            for name in ports_for_slot(spec, slot):
                ports.setdefault(name, {"config": {}, "connected_to": None})
            visible = Observable.known(
                expected_port if expected_port in ports else None,
                source="null-adapter",
            )
            installed = expected_port in ports
        return self._receipt(
            "install_module", requested, operation_id,
            OperationResult.SUCCESS if installed else OperationResult.FAILED,
            preconditions=pre,
            postconditions=[CheckResult.of("module_port_visible", expected_port, visible)],
            executed={"ports_added": list(ports_for_slot(spec, slot))},
            error=None if installed else f"port {expected_port!r} not visible after install",
        )

    def configure_ios(
        self, device: str, commands: list[str], operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"device": device, "command_count": len(commands)}
        exists = self.device_exists(device)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._receipt(
                "configure_ios", requested, operation_id, OperationResult.REJECTED,
                preconditions=pre, error="device missing",
            )
        with self._lock:
            self._devices[device].setdefault("ios_commands", []).extend(commands)
        # Honest structural postcondition: the commands were recorded, but IOS
        # acceptance is not observable structurally — so the check stays
        # unknown and the receipt remains unconfirmed.
        post = [
            CheckResult(
                name="ios_acceptance",
                expected="commands accepted by IOS",
                actual=Observable.unknown(
                    "structural adapter records commands but cannot observe IOS acceptance"
                ),
                passed=None,
            )
        ]
        return self._receipt(
            "configure_ios", requested, operation_id, OperationResult.SUCCESS,
            preconditions=pre, postconditions=post,
            executed={"recorded": len(commands)},
            error=None,
        )

    def configure_gateway_lan(
        self,
        device: str,
        ip: str,
        mask: str,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        requested = {"device": device, "ip": ip, "mask": mask}
        exists = self.device_exists(device)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._receipt(
                "configure_gateway_lan", requested, operation_id,
                OperationResult.REJECTED,
                preconditions=pre, error="device missing",
            )
        with self._lock:
            config = self._devices[device].setdefault("config", {})
            config["ip"] = ip
            config["mask"] = mask
        # Honest structural postcondition: the address is recorded in the
        # double, but a real PT gateway's read-back is not observable here —
        # so the check stays unknown and the receipt stays unconfirmed.
        post = [
            CheckResult(
                name="configured_addresses_present",
                expected=json.dumps({"Vlan1": ip}, sort_keys=True),
                actual=Observable.unknown(
                    "structural adapter records the LAN address but cannot "
                    "read a gateway's Vlan1 back"
                ),
                passed=None,
            )
        ]
        return self._receipt(
            "configure_gateway_lan", requested, operation_id,
            OperationResult.SUCCESS,
            preconditions=pre, postconditions=post,
            executed={"recorded": ip},
            error=None,
        )

    def configure_host(
        self,
        device: str,
        *,
        dhcp: bool,
        ip: str | None = None,
        mask: str | None = None,
        gateway: str | None = None,
        dns: str | None = None,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        requested = {
            "device": device, "dhcp": dhcp, "ip": ip, "mask": mask,
            "gateway": gateway, "dns": dns,
        }
        exists = self.device_exists(device)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._receipt(
                "configure_host", requested, operation_id, OperationResult.REJECTED,
                preconditions=pre, error="device missing",
            )
        with self._lock:
            port = next(iter(self._devices[device]["ports"]))
            config = self._devices[device]["ports"][port]["config"]
            config["dhcp"] = dhcp
            if ip: config["ip"] = ip
            if mask: config["mask"] = mask
            if gateway: config["gateway"] = gateway
            if dns: config["dns"] = dns
        post = [
            CheckResult.of("dhcp_flag", str(dhcp), Observable.known(dhcp, source="null-adapter")),
        ]
        if ip:
            post.append(
                CheckResult.of(
                    "ip_read_back", ip, Observable.known(ip, source="null-adapter")
                )
            )
        return self._receipt(
            "configure_host", requested, operation_id, OperationResult.SUCCESS,
            preconditions=pre, postconditions=post, executed={"port": port},
        )

    def save_project(self, filename: str, operation_id: str | None = None) -> OperationReceipt:
        requested = {"filename": filename}
        with self._lock:
            self._project = filename
            persisted = self._project == filename
        post = [
            CheckResult.of(
                "project_path_recorded", filename,
                Observable.known(self._project, source="null-adapter"),
            )
        ]
        return self._receipt(
            "save_project", requested, operation_id,
            OperationResult.SUCCESS if persisted else OperationResult.FAILED,
            postconditions=post,
        )

    def open_project(self, path: str, operation_id: str | None = None) -> OperationReceipt:
        requested = {"path": path}
        with self._lock:
            self._project = path
        post = [
            CheckResult.of(
                "project_path_recorded", path,
                Observable.known(self._project, source="null-adapter"),
            )
        ]
        return self._receipt(
            "open_project", requested, operation_id, OperationResult.SUCCESS,
            postconditions=post,
        )

    # -- verification -------------------------------------------------------

    def run_flow_test(
        self,
        source: str,
        destination: str,
        *,
        transport: str = "icmp",
        port: int | None = None,
        destination_ip: str | None = None,
    ) -> FlowResult:
        return FlowResult(
            source=source,
            destination=destination,
            transport=transport,
            port=port,
            outcome=FlowOutcome.UNKNOWN,
            detail="null adapter has no data plane; flow outcomes require the real runtime",
        )
