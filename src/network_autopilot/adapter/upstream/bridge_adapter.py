"""PacketTracerAdapter over the upstream HTTP bridge.

PROVENANCE: this module is the ONLY place in `network_autopilot` allowed to
import `packet_tracer_mcp` (see PROVENANCE.md). It reuses the upstream
transport — the PTCommandBridge protocol, the per-machine token security and
the reportResult result-correlation — everything above depends on the
`PacketTracerAdapter` abstraction instead of these details.

Ways to reach a bridge:
- attach mode (`host_bridge=False`): pure HTTP client to a bridge some other
  process already hosts (POST /queue, GET /result?rid=...).
- host mode (`host_bridge=True`, default): starts a PTCommandBridge on `port`
  so the Packet Tracer extension (which polls the default port) finds it.

Honesty rules implemented here:
- /ping identity must match our token fingerprint before anything is queued
  (foreign-bridge detection, inherited from the upstream design).
- A timeout / no-result answer is UNKNOWN, never success.
- A `PT_ERROR: ...` result is FAILED with the error preserved as evidence.
- Read-back postconditions are separate sends; only their passage marks a
  receipt confirmed.
- IOS acceptance is not observable through this channel, so `configure_ios`
  receipts stay unconfirmed by construction.
- One channel per request, never both (upstream rule): HTTP when the
  authenticated bridge sees PT polling, otherwise the file mailbox when the
  Script Engine heartbeat is fresh — a late HTTP pickup plus a file retry
  would execute the same mutation twice.
- Topology read-back (model/ports/IPs), device enumeration and real console
  pings are implemented over the verified upstream JS patterns; whatever they
  cannot answer stays UNKNOWN, never guessed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ...core.actual_state import ActualNetworkState, DeviceObservation
from ...core.ids import new_id, utc_now_iso
from ...core.observability import Observable
from ...core.receipts import CheckResult, OperationReceipt, OperationResult
from ...core.requirements import FlowOutcome, FlowResult
from ...evidence.store import EvidenceStore
from ..base import AdapterError, AdapterInfo, PacketTracerAdapter
from . import js_commands as js

try:  # PROVENANCE: the single sanctioned import of the upstream package.
    from packet_tracer_mcp.infrastructure.execution.live_bridge import (
        DEFAULT_PORT,
        PTCommandBridge,
        next_rid,
        report_result_js,
    )
    from packet_tracer_mcp.infrastructure.execution.bridge_token import (
        get_bridge_token,
        token_fingerprint,
    )
    from packet_tracer_mcp.infrastructure.execution.file_bridge import FileBridge
    from packet_tracer_mcp.shared.utils import classify_ping
    from packet_tracer_mcp.infrastructure.catalog.cables import infer_cable
    from packet_tracer_mcp.infrastructure.catalog.devices import category_of_model
except ImportError as exc:  # pragma: no cover - repo checkout without install
    raise ImportError(
        "network_autopilot.adapter.upstream needs the packet_tracer_mcp package: "
        "run `pip install -e .` or pytest from the repo root (pythonpath includes src/)"
    ) from exc


class BridgeUnreachable(AdapterError):
    """The bridge port could not be reached at all."""


class BridgeAdapter(PacketTracerAdapter):
    name = "upstream-bridge"
    version = "0.1.0"

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        token: str | None = None,
        *,
        host_bridge: bool = True,
        evidence: EvidenceStore | None = None,
        max_result_wait: float = 45.0,
        file_bridge: FileBridge | None = None,
    ):
        self.port = port
        # Default (None) uses the real per-machine token via the upstream
        # token module. Tests pass an explicit token to avoid touching it.
        self.token = token or get_bridge_token()
        self._fingerprint = token_fingerprint(self.token)
        self._host_bridge = host_bridge
        self._evidence = evidence
        self._max_wait = max_result_wait
        self._hosted: PTCommandBridge | None = None
        # Fallback transport for when the extension webview is closed: the
        # Script Engine keeps running and drains the file mailbox. Tests can
        # inject a temp-directory FileBridge.
        self._file_bridge = file_bridge or FileBridge()

    # -- HTTP plumbing --------------------------------------------------

    def _http(self, method: str, path_query: str, body: str | None = None, timeout: float = 10.0) -> tuple[int, str]:
        url = f"http://127.0.0.1:{self.port}{path_query}"
        req = urllib.request.Request(
            url, data=body.encode("utf-8") if body is not None else None, method=method
        )
        if body is not None:
            req.add_header("Content-Type", "text/plain")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise BridgeUnreachable(str(getattr(exc, "reason", exc))) from exc
        except OSError as exc:
            raise BridgeUnreachable(str(exc)) from exc

    def _ensure_hosted(self) -> None:
        if not self._host_bridge or self._hosted is not None:
            return
        try:
            bridge = PTCommandBridge(port=self.port, token=self.token)
            bridge.start()
            self._hosted = bridge
        except OSError:
            # Port taken: either a foreign service or another bridge. ping()
            # will report which, via the identity check.
            self._hosted = False  # type: ignore[assignment]

    def stop(self) -> None:
        """Shut down a bridge we hosted (tests, clean shutdown)."""
        if isinstance(self._hosted, PTCommandBridge):
            self._hosted.stop()
        self._hosted = None

    # -- command pipeline -----------------------------------------------

    def _pick_channel(self) -> str:
        """'http' | 'file' | '' — one channel per request, never both.

        Mirrors the upstream routing: HTTP only when the authenticated bridge
        actually sees PT polling (ping() checks identity + /status); otherwise
        the file mailbox when the Script Engine heartbeat is fresh.
        """
        if self.ping().reachable:
            return "http"
        try:
            if self._file_bridge.pt_alive():
                return "file"
        except OSError:
            pass
        return ""

    def _send_http(self, expression: str, statements: list[str] | None, wait: float) -> Observable[str]:
        rid = next_rid()
        payload = report_result_js(port=self.port, token=self.token, rid=rid) + js.wrap_with_report(
            expression, "", statements
        )
        # report_result_js already defines reportResult; wrap_with_report with
        # an empty report_fn_js appends the reporting call. Pass "" so the
        # definition is not duplicated.
        try:
            status, _ = self._http(
                "POST", f"/queue?t={self.token}", body=payload, timeout=10.0
            )
        except BridgeUnreachable as exc:
            return Observable.unknown(f"bridge unreachable while queueing: {exc}")
        if status != 200:
            return Observable.unknown(f"/queue answered {status}")
        try:
            status, body = self._http(
                "GET",
                f"/result?t={self.token}&rid={rid}&wait={min(wait, 60.0):.1f}",
                timeout=wait + 10.0,
            )
        except BridgeUnreachable as exc:
            return Observable.unknown(f"bridge unreachable while waiting for result: {exc}")
        if status != 200 or not body:
            return Observable.unknown(f"no result for rid={rid} within {wait:.0f}s")
        if self._evidence is not None:
            # The payload embeds the bridge token inside the report URL; the
            # evidence store must never see the secret itself.
            sanitized = payload.replace(self.token, "***REDACTED***")
            self._evidence.record(
                "bridge_send",
                {"rid": rid, "js": sanitized, "result": body, "channel": "http"},
                component="adapter.upstream",
            )
        return Observable.known(body, source=f"bridge:{rid}")

    def _send_file(self, expression: str, statements: list[str] | None, wait: float) -> Observable[str]:
        # The Script Engine injects a LOCAL reportResult and captures its
        # argument, so the body goes out without the HTTP report definition —
        # the same single-report wrapper, just answered by file instead of XHR.
        body = js.wrap_with_report(expression, "", statements)
        answer = self._file_bridge.send_and_wait(body, timeout=wait)
        if self._evidence is not None:
            self._evidence.record(
                "bridge_send",
                {"js": body, "result": answer, "channel": "file"},
                component="adapter.upstream",
            )
        if answer is None:
            return Observable.unknown(f"file channel produced no result within {wait:.0f}s")
        return Observable.known(answer, source="file-bridge")

    def send_expression(
        self,
        expression: str,
        *,
        statements: list[str] | None = None,
        wait: float | None = None,
    ) -> Observable[str]:
        """Queue one correlated command and wait for its correlated result.

        The returned Observable is KNOWN with PT's reported string ('OK:...',
        'yes', 'PT_ERROR: ...', ...) or UNKNOWN when no answer arrived —
        a timeout is never mapped to success.
        """
        wait = wait or self._max_wait
        channel = self._pick_channel()
        if channel == "http":
            return self._send_http(expression, statements, wait)
        if channel == "file":
            return self._send_file(expression, statements, wait)
        return Observable.unknown(
            "no live channel: HTTP bridge sees no PT poll and the file mailbox "
            "heartbeat is stale (is PT open with the extension installed?)"
        )

    # -- probing ----------------------------------------------------------

    def ping(self, timeout: float = 5.0) -> AdapterInfo:
        try:
            status, body = self._http("GET", "/ping", timeout=timeout)
        except BridgeUnreachable as exc:
            if self._host_bridge:
                self._ensure_hosted()
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known(f"bridge not reachable: {exc}"),
            )
        if status != 200:
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known(f"/ping answered HTTP {status}"),
            )
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known("port held by an unidentified service"),
            )
        if data.get("service") != "pt-mcp-bridge":
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known("port held by a different service"),
            )
        if data.get("id") != self._fingerprint:
            return AdapterInfo(
                self.name, self.version, False,
                Observable.known("a foreign bridge holds the port (token fingerprint mismatch)"),
            )
        # Authenticated; is PT actually polling? /status carries that.
        try:
            status, body = self._http("GET", f"/status?t={self.token}", timeout=timeout)
            connected = bool(json.loads(body).get("connected")) if status == 200 else False
        except (BridgeUnreachable, json.JSONDecodeError):
            connected = False
        detail = (
            "bridge authenticated; Packet Tracer extension is polling"
            if connected
            else "bridge authenticated; no Packet Tracer poll seen yet (is PT open with the extension?)"
        )
        return AdapterInfo(self.name, self.version, connected, Observable.known(detail))

    # -- inspection ---------------------------------------------------------

    def device_exists(self, name: str) -> Observable[bool]:
        answer = self.send_expression(js.js_device_exists_expr(name), wait=10.0)
        if not answer.is_known:
            return Observable[bool].unknown(answer.detail or answer.status.value)
        value = str(answer.value)
        if value == "yes":
            return Observable.known(True, source=answer.source)
        if value == "no":
            return Observable.known(False, source=answer.source)
        if value.startswith("PT_ERROR"):
            return Observable[bool].unknown(f"PT reported: {value}")
        return Observable[bool].unknown(f"unexpected answer: {value!r}")

    def link_exists(
        self, a_device: str, a_port: str, b_device: str, b_port: str
    ) -> Observable[bool]:
        answer = self.send_expression(
            js.js_both_ports_linked_expr(a_device, a_port, b_device, b_port), wait=10.0
        )
        if not answer.is_known:
            return Observable[bool].unknown(answer.detail or answer.status.value)
        value = str(answer.value)
        if value == "yes":
            return Observable.known(True, source=answer.source)
        if value == "no":
            return Observable.known(False, source=answer.source)
        return Observable[bool].unknown(f"unexpected answer: {value!r}")

    def _live_devices(self) -> Observable[list[dict]]:
        """One topology read-back: [{name, model, ports:[{name,ip,mask,up,linked}]}].

        Built on the upstream-verified live-devices JS. Anything that goes
        wrong (no channel, PT error, unparseable JSON) is UNKNOWN — never an
        empty list, which would read as "workspace is empty".
        """
        statements, final = js.js_live_devices_statements()
        answer = self.send_expression(final, statements=statements, wait=10.0)
        if not answer.is_known:
            return Observable[list[dict]].unknown(answer.detail or answer.status.value)
        value = str(answer.value)
        if value.startswith("PT_ERROR"):
            return Observable[list[dict]].unknown(f"PT reported: {value}")
        try:
            data = json.loads(value)
            devices = data.get("devices")
            if not isinstance(devices, list):
                raise ValueError("devices is not a list")
            return Observable.known(list(devices), source=answer.source)
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            return Observable[list[dict]].unknown(
                f"unparseable topology answer: {exc}: {value[:120]!r}"
            )

    def list_device_names(self) -> Observable[list[str]]:
        devices = self._live_devices()
        if not devices.is_known:
            return Observable[list[str]].unknown(devices.detail or devices.status.value)
        names = [str(d.get("name") or "") for d in devices.value]
        return Observable.known([n for n in names if n], source=devices.source)

    def _observation_from_live(self, match: dict) -> DeviceObservation:
        name = str(match.get("name") or "")
        observation = DeviceObservation(name=name)
        source = f"live-readback:{name}"
        model = str(match.get("model") or "")
        observation.model = (
            Observable.known(model, source=source)
            if model
            else Observable[str].unknown("model not reported by read-back")
        )
        for p in match.get("ports") or []:
            pname = str(p.get("name") or "")
            if not pname:
                continue
            port = observation.port(pname)
            up = p.get("up")
            if up is True:
                port.state = Observable.known("up", source=source)
            elif up is False:
                port.state = Observable.known("down", source=source)
            else:
                port.state = Observable[str].unknown("port up-state not reported")
            ip = str(p.get("ip") or "")
            mask = str(p.get("mask") or "")
            port.ip = (
                Observable.known(ip, source=source)
                if ip
                else Observable[str].unknown("no ip on this port")
            )
            port.mask = (
                Observable.known(mask, source=source)
                if mask
                else Observable[str].unknown("no mask on this port")
            )
        return observation

    def inspect_device(self, name: str) -> DeviceObservation:
        devices = self._live_devices()
        observation = DeviceObservation(name=name)
        if not devices.is_known:
            observation.notes.append(
                f"topology read-back unavailable: {devices.detail or devices.status.value}"
            )
            return observation
        match = next((d for d in devices.value if str(d.get("name") or "") == name), None)
        if match is None:
            observation.notes.append("not present in observed workspace")
            return observation
        return self._observation_from_live(match)

    def observe_state(
        self,
        names: list[str] | None = None,
        *,
        observed_at: str | None = None,
    ) -> ActualNetworkState:
        """One topology fetch feeds every requested device.

        Same contract as the base implementation (known-absent devices are
        recorded as absent metadata, never as empty observations); when the
        fetch fails it falls back to the base per-device behavior so the
        failure detail stays per-device instead of being lost.
        """
        if names is None:
            return super().observe_state(None, observed_at=observed_at)
        devices = self._live_devices()
        if not devices.is_known:
            return super().observe_state(names, observed_at=observed_at)
        state = ActualNetworkState(
            source=f"adapter:{self.name}", observed_at=observed_at or utc_now_iso()
        )
        for name in names:
            match = next(
                (d for d in devices.value if str(d.get("name") or "") == name), None
            )
            if match is None:
                state.metadata[f"absent:{name}"] = Observable[str].known(
                    "reported absent by adapter", source=devices.source
                )
                continue
            state.observe_device(self._observation_from_live(match))
        return state

    # -- mutations ----------------------------------------------------------

    def _precondition_receipt(
        self, operation: str, requested: dict, operation_id: str | None,
        pre: list[CheckResult], error: str,
    ) -> OperationReceipt:
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation=operation,
            requested=requested,
            result=OperationResult.REJECTED,
            preconditions=pre,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

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
        pre = [
            CheckResult.of("device_absent", "False", exists, compare=lambda v, _: v is False)
        ]
        if not (exists.is_known and exists.value is False):
            return self._precondition_receipt(
                "create_device", requested, operation_id, pre,
                "existence could not be verified before mutation (fail-closed)",
            )

        # Endpoint default follows the upstream catalog's pt_type ("PC-PT"):
        # found live, plain "PC" makes lw.addDevice fail silently and the
        # addDevice fallback answer "Invalid arguments" — no device, no error.
        model_aliases = {"PC": "PC-PT"}
        default_model = {"router": "Router-PT", "switch": "Switch-PT"}.get(role, "PC-PT")
        model = model_aliases.get(model_hint, model_hint) if model_hint else default_model
        x, y = position or (100, 100)
        answer = self.send_expression(
            js.js_add_device(name, js.device_type_code_for(role), model, x, y)
        )
        result, executed, error = self._interpret(answer)
        post: list[CheckResult] = []
        if result is OperationResult.SUCCESS:
            after = self.device_exists(name)
            post = [CheckResult.of("device_present", "True", after)]
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="create_device",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=post,
            evidence={"model": model},
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def rename_device(
        self, old_name: str, new_name: str, operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"old_name": old_name, "new_name": new_name}
        old_exists = self.device_exists(old_name)
        new_absent = self.device_exists(new_name)
        pre = [
            CheckResult.of("old_device_present", "True", old_exists),
            CheckResult.of("new_name_free", "False", new_absent, compare=lambda v, _: v is False),
        ]
        if not (
            old_exists.is_known and old_exists.value
            and new_absent.is_known and new_absent.value is False
        ):
            return self._precondition_receipt(
                "rename_device", requested, operation_id, pre,
                "precondition could not be verified (fail-closed)",
            )
        statements = [
            f"var dev=ipc.network().getDevice({js._q(old_name)});",
            "if(!dev){throw new Error('device vanished');}",
            f"dev.setName({js._q(new_name)});",
        ]
        answer = self.send_expression("'renamed'", statements=statements)
        result, executed, error = self._interpret(answer)
        post: list[CheckResult] = []
        if result is OperationResult.SUCCESS:
            post = [CheckResult.of("renamed_present", "True", self.device_exists(new_name))]
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="rename_device",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=post,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def position_device(
        self, name: str, x: int, y: int, operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"name": name, "x": x, "y": y}
        exists = self.device_exists(name)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._precondition_receipt(
                "position_device", requested, operation_id, pre, "device missing or unknown"
            )
        statements = [
            f"var d=ipc.network().getDevice({js._q(name)});",
            "if(!d){throw new Error('device vanished');}",
            # moveToLocation is the verified canvas-move call (upstream
            # pt_move_device uses exactly this).
            f"d.moveToLocation({int(x)}, {int(y)});",
        ]
        answer = self.send_expression("'positioned'", statements=statements)
        result, executed, error = self._interpret(answer)
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="position_device",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            evidence={"postcondition": "canvas position read-back not yet implemented"},
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
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
        a_exists = self.device_exists(a_device)
        b_exists = self.device_exists(b_device)
        pre = [
            CheckResult.of("a_device_present", "True", a_exists),
            CheckResult.of("b_device_present", "True", b_exists),
        ]
        if not (a_exists.is_known and a_exists.value and b_exists.is_known and b_exists.value):
            return self._precondition_receipt(
                "create_link", requested, operation_id, pre,
                "endpoint existence could not be verified (fail-closed)",
            )
        try:
            cable = js.cable_name_for(media)
        except ValueError as exc:
            return self._precondition_receipt(
                "create_link", requested, operation_id, pre, str(exc)
            )
        if cable == "auto":
            # PT rejects addLink('auto') for some pairs (found live: PC↔PC
            # answers false while 'cross' works), so 'auto' is resolved from
            # the upstream wiring rules using the OBSERVED models; endpoint
            # pairs have no rule in that table and take a cross-over cable.
            devices = self._live_devices()
            models: list[str] = []
            for dev in (a_device, b_device):
                match = (
                    next(
                        (d for d in devices.value if str(d.get("name") or "") == dev),
                        None,
                    )
                    if devices.is_known
                    else None
                )
                models.append(str(match.get("model") or "") if match else "")
            cat_a = category_of_model(models[0]) or "pc"
            cat_b = category_of_model(models[1]) or "pc"
            cable = infer_cable(cat_a, cat_b)
            endpoints = ("pc", "laptop", "server")
            if cat_a in endpoints and cat_b in endpoints:
                cable = "cross"
        answer = self.send_expression(
            js.js_add_link(a_device, a_port or "", b_device, b_port or "", cable)
        )
        result, executed, error = self._interpret(answer)
        postconditions: list[CheckResult] = []
        if result is OperationResult.SUCCESS:
            # read-back: the link exists only if BOTH endpoints report one
            linked = self.link_exists(a_device, a_port or "", b_device, b_port or "")
            postconditions.append(CheckResult.of("link_present", "True", linked))
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="create_link",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=postconditions,
            evidence={
                **(executed or {}),
                **(
                    {"postcondition": "both endpoints report a link (read-back)"}
                    if result is OperationResult.SUCCESS
                    else {}
                ),
            },
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def configure_ios(
        self, device: str, commands: list[str], operation_id: str | None = None
    ) -> OperationReceipt:
        requested = {"device": device, "command_count": len(commands)}
        exists = self.device_exists(device)
        pre = [CheckResult.of("device_present", "True", exists)]
        if not (exists.is_known and exists.value):
            return self._precondition_receipt(
                "configure_ios", requested, operation_id, pre, "device missing or unknown"
            )
        answer = self.send_expression(
            js.js_configure_ios(device, "\n".join(commands))
        )
        result, executed, error = self._interpret(answer)
        # IOS acceptance is not observable over this channel: per-command
        # status is UNKNOWN and the receipt can never confirm. Saying so beats
        # a green check that means "JS delivered".
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="configure_ios",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=[
                CheckResult(
                    name="ios_acceptance",
                    expected="commands accepted by IOS",
                    actual=Observable.unknown(
                        "IOS result semantics are not observable through the bridge; "
                        "verify by read-back/behavior, not by this receipt"
                    ),
                    passed=None,
                )
            ],
            evidence={"command_status": json.dumps({c: "unknown" for c in commands})},
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
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
            return self._precondition_receipt(
                "configure_host", requested, operation_id, pre, "device missing or unknown"
            )
        answer = self.send_expression(
            js.js_configure_pc_ip(device, dhcp, ip or "", mask or "", gateway or "", dns or "")
        )
        result, executed, error = self._interpret(answer)
        postconditions: list[CheckResult] = []
        if result is OperationResult.SUCCESS and not dhcp and ip:
            # read-back: the configured IP must be observable on the device
            obs = self.inspect_device(device)
            port = next(
                (p for p in obs.ports.values() if p.ip.is_known and p.ip.value == ip),
                None,
            )
            check = Observable.known(True) if port else (
                Observable[bool].unknown(
                    f"configured ip {ip!r} not observed on {device!r}"
                )
            )
            postconditions.append(CheckResult.of("ip_observed", ip, check))
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="configure_host",
            requested=requested,
            executed=executed,
            result=result,
            preconditions=pre,
            postconditions=postconditions,
            evidence={
                **(executed or {}),
                **(
                    {"postcondition": "ip observed on device read-back"}
                    if postconditions
                    else {"postcondition": "dhcp/no-ip: no read-back check"}
                ),
            },
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def save_project(
        self, filename: str, operation_id: str | None = None, directory: str = ""
    ) -> OperationReceipt:
        requested = {"filename": filename, "directory": directory}
        statements, final = js.js_save_project_statements(directory, filename)
        answer = self.send_expression(final, statements=statements, wait=25.0)
        result, executed, error = self._interpret(answer)
        # 'ERR:...' is PT reporting the save failed — map it honestly.
        if result is OperationResult.SUCCESS and str(answer.value or "").startswith("ERR:"):
            result, error = OperationResult.FAILED, str(answer.value)
        post: list[CheckResult] = []
        if result is OperationResult.SUCCESS and str(answer.value or "").startswith("OK:"):
            # The save JS itself verified file existence + size in-band; that
            # IS the read-back, so the receipt can legitimately confirm.
            _, _, size = str(answer.value)[3:].rpartition("|")
            post = [
                CheckResult(
                    name="file_exists_on_disk",
                    expected="saved file exists with a size",
                    actual=Observable.known(f"size={size}", source=answer.source),
                    passed=True,
                )
            ]
            executed = {"saved_path": str(answer.value)[3:].rpartition("|")[0], "size": size}
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="save_project",
            requested=requested,
            executed=executed,
            result=result,
            postconditions=post,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error=error,
        )

    def open_project(self, path: str, operation_id: str | None = None) -> OperationReceipt:
        # Deliberately not wired in Phase 1 rather than guessed: the verified
        # open/read-back JS lands with the Phase-2 save/reopen proof work.
        return OperationReceipt(
            operation_id=operation_id or new_id("op"),
            operation="open_project",
            requested={"path": path},
            result=OperationResult.NOT_EXECUTED,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error="open_project is not wired in Phase 1 (no verified read-back yet)",
        )

    # -- verification -------------------------------------------------------

    def _resolve_destination_ip(self, destination: str) -> Observable[str]:
        """The console ping needs an IP; resolve it from observed state."""
        devices = self._live_devices()
        if not devices.is_known:
            return Observable[str].unknown(
                f"cannot resolve {destination!r} address: "
                f"{devices.detail or devices.status.value}"
            )
        match = next(
            (d for d in devices.value if str(d.get("name") or "") == destination), None
        )
        if match is None:
            return Observable[str].unknown(
                f"destination {destination!r} not present in the observed workspace"
            )
        ips = {
            str(p.get("ip"))
            for p in match.get("ports") or []
            if str(p.get("ip") or "").strip()
            and str(p.get("ip")) != "0.0.0.0"  # placeholder (e.g. Bluetooth NIC)
        }
        if not ips:
            return Observable[str].unknown(
                f"destination {destination!r} has no observable IP to ping"
            )
        if len(ips) > 1:
            return Observable[str].unknown(
                f"destination {destination!r} has several IPs ({sorted(ips)}); "
                "the flow test does not guess which one is meant"
            )
        return Observable.known(ips.pop(), source=devices.source)

    def run_flow_test(
        self, source: str, destination: str, *, timeout: float = 25.0
    ) -> FlowResult:
        """A REAL console ping: arm on the source, poll for the new stat block.

        Verdicts come from the upstream ping-stat classifier: 100% replies is
        REACHABLE, some loss is PARTIAL (never reported as healthy), no replies
        is BLOCKED. No console, no address, no channel, or a quiet console is
        UNKNOWN — a flow test that could not run is never PASS.
        """
        target = self._resolve_destination_ip(destination)
        if not target.is_known:
            return FlowResult(
                source=source,
                destination=destination,
                outcome=FlowOutcome.UNKNOWN,
                detail=target.detail or target.status.value,
            )
        armed = self.send_expression(
            js.js_console_ping_arm_expr(source, str(target.value)), wait=10.0
        )
        if not armed.is_known:
            return FlowResult(
                source=source,
                destination=destination,
                outcome=FlowOutcome.UNKNOWN,
                detail=f"ping could not be armed: {armed.detail or armed.status.value}",
            )
        arm_value = str(armed.value)
        if arm_value.startswith("PT_ERROR"):
            return FlowResult(
                source=source,
                destination=destination,
                outcome=FlowOutcome.UNKNOWN,
                detail=f"PT reported while arming: {arm_value}",
            )
        if not arm_value.startswith("BASE:"):
            return FlowResult(
                source=source,
                destination=destination,
                outcome=FlowOutcome.UNKNOWN,
                detail=f"ping arm answered: {arm_value!r}",
            )
        base = int(arm_value[5:])
        poll = js.js_console_poll_expr(source, base)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.6)
            answer = self.send_expression(poll, wait=5.0)
            if not answer.is_known:
                continue  # transient channel hiccup: keep polling until deadline
            value = str(answer.value)
            if value.startswith("DONE:"):
                stat = value[5:]
                verdict = classify_ping(stat)
                outcome = {
                    "ok": FlowOutcome.REACHABLE,
                    "partial": FlowOutcome.PARTIAL,
                    "none": FlowOutcome.BLOCKED,
                }[verdict]
                return FlowResult(
                    source=source,
                    destination=destination,
                    outcome=outcome,
                    detail=f"ping {source}->{str(target.value)}: {verdict}",
                    evidence={"ping_stat": stat},
                )
            if value.startswith("ERR:"):
                return FlowResult(
                    source=source,
                    destination=destination,
                    outcome=FlowOutcome.UNKNOWN,
                    detail=f"console unavailable: {value}",
                )
            # 'WAIT' or anything else: keep polling
        return FlowResult(
            source=source,
            destination=destination,
            outcome=FlowOutcome.UNKNOWN,
            detail=(
                f"no new ping statistics appeared on {source!r} within {timeout:.0f}s"
            ),
        )

    # -- control-plane read-back (console) -----------------------------------

    def _console_show(self, device: str, command: str, *, timeout: float = 12.0) -> Observable[str]:
        """Run one `show ...` command on the device console and return the tail.

        Verified live: PT exposes the routing table / VLAN database ONLY
        through getCommandLine(), so this is an arm/poll exchange like the
        ping path. UNKNOWN on any failure — the raw console evidence is kept.
        """
        statements, final = js.js_console_command_arm_statements(device, command)
        armed = self.send_expression(final, statements=statements, wait=10.0)
        if not armed.is_known:
            return Observable[str].unknown(
                f"console arm failed: {armed.detail or armed.status.value}"
            )
        value = str(armed.value)
        if value.startswith(("PT_ERROR", "ERR:")):
            return Observable[str].unknown(f"console unavailable: {value}")
        if not value.startswith("BASE:"):
            return Observable[str].unknown(f"console arm answered: {value!r}")
        base = int(value[5:])
        collect = js.js_console_collect_expr(device, base)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            answer = self.send_expression(collect, wait=5.0)
            if not answer.is_known:
                continue
            text = str(answer.value)
            if text.startswith("DONE:"):
                return Observable.known(text[5:], source=f"console:{device}")
            if text.startswith(("ERR:", "PT_ERROR")):
                return Observable[str].unknown(f"console error: {text}")
        return Observable[str].unknown(
            f"{command!r} produced no prompt on {device!r} within {timeout:.0f}s"
        )

    def read_routing_table(self, device: str) -> Observable[list]:
        """Structured `show ip route` read-back; empty table is KNOWN []."""
        tail = self._console_show(device, "show ip route")
        if not tail.is_known:
            return Observable[list].unknown(tail.detail or tail.status.value)
        try:
            routes = js.parse_show_ip_route(tail.value)
        except Exception as exc:  # unparseable output stays honest
            return Observable[list].unknown(f"unparseable route table: {exc}: {tail.value[:200]!r}")
        known = Observable.known(routes, source=tail.source)
        known.metadata = {"console": tail.value}
        return known

    def read_vlans(self, device: str) -> Observable[list]:
        """Structured `show vlan brief` read-back (switches)."""
        tail = self._console_show(device, "show vlan brief")
        if not tail.is_known:
            return Observable[list].unknown(tail.detail or tail.status.value)
        try:
            vlans = js.parse_show_vlan_brief(tail.value)
        except Exception as exc:
            return Observable[list].unknown(f"unparseable vlan table: {exc}: {tail.value[:200]!r}")
        known = Observable.known(vlans, source=tail.source)
        known.metadata = {"console": tail.value}
        return known

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _interpret(
        answer: Observable[str],
    ) -> tuple[OperationResult, dict | None, str | None]:
        """Map PT's reported string to an OperationResult. Timeout → UNKNOWN.

        JS literal `false` (and an empty answer) are FAILED: found live —
        lwAddLink answers `false` when the cable could not be established,
        and mapping that to success would make the receipt lie.
        """
        if not answer.is_known:
            return OperationResult.UNKNOWN, None, answer.detail
        value = str(answer.value)
        if value.startswith("PT_ERROR"):
            return OperationResult.FAILED, {"pt_result": value}, value
        if value.strip() in ("false", ""):
            return (
                OperationResult.FAILED,
                {"pt_result": value or "<empty>"},
                f"PT reported {value.strip() or '<empty>'!r} for the mutation",
            )
        return OperationResult.SUCCESS, {"pt_result": value}, None
