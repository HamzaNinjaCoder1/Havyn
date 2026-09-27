"""Back-to-back serial links: who clocks, and how that is proven.

A serial link between two routers is a lab wire, not a service handoff: the
cable itself carries no timing, so exactly ONE end must be told to generate
the clock. Live Packet Tracer proves both halves of that rule
(measured live against PT):

  * a link with a clock on one end passes traffic both ways, and PT names that
    end `DCE V.35, clock rate 64000` while the other answers
    `DTE V.35 TX and RX clocks detected`;
  * removing that clock drops the far end -
    `%LINEPROTO-5-UPDOWN: Line protocol on Interface Serial2/0, changed state
    to down` - and the ping is BLOCKED.

So an unclocked build is not "good enough": it is one command away from a dead
link, and PT will not tell the user which end is wrong. This module owns both
halves of the answer:

  * `serial_dce_sides()` / `serial_clock_expectations()` - the compiler's
    decision of which end clocks, with the reason in the code rather than in
    tribal knowledge;
  * `read_serial_clock_evidence()` - the read-back that compares PT's own
    answer against that decision. A disagreement is a FAIL and a silent
    console is UNKNOWN; UNKNOWN is never upgraded to a pass.

A serial link to a cloud, a modem or a cell tower is deliberately NOT clocked
here: that is a service handoff where the far side provides the timing, and a
router-generated clock on a WAN handoff is wrong in real deployments too.
"""

from __future__ import annotations

from typing import Any, Protocol

from .core.actual_state import Observable

#: The clock rate a lab DCE end is configured with. 64000 is the PT/IOS
#: default taught for back-to-back labs and is what the live probes used.
DCE_CLOCK_RATE = 64000

_ROUTER_ROLES = frozenset({"router", "l3_switch"})


class _PortPlanLike(Protocol):
    link_endpoints: dict[str, tuple[str, str, str, str]]
    resolutions: dict[str, Any]


class _SpecLike(Protocol):
    devices: list[Any]
    links: list[Any]


def _is_router(device: Any) -> bool:
    if device is None:
        return False
    role = getattr(device, "role", None)
    value = getattr(role, "value", role)
    return str(value) in _ROUTER_ROLES


def _media_of(port_plan: _PortPlanLike, device: str, interface: str) -> str:
    resolution = port_plan.resolutions.get(f"{device}:{interface}")
    return str(getattr(resolution, "media", "") or "")


def _back_to_back_serial_links(
    spec: _SpecLike, port_plan: _PortPlanLike
) -> list[tuple[str, str, str, str]]:
    """The solved endpoints of every router↔router serial link.

    A link qualifies when both endpoints are routers and the resolved media on
    the two sides is serial; a serial link to a cloud or modem never appears
    here, so it is never clocked.
    """
    devices = {device.id: device for device in spec.devices}
    out: list[tuple[str, str, str, str]] = []
    for link in spec.links:
        endpoints = port_plan.link_endpoints.get(link.id)
        if not endpoints:
            continue
        a_device, a_iface, b_device, b_iface = endpoints
        if not (
            _is_router(devices.get(a_device)) and _is_router(devices.get(b_device))
        ):
            continue
        a_serial = _media_of(port_plan, a_device, a_iface) == "serial"
        b_serial = _media_of(port_plan, b_device, b_iface) == "serial"
        if a_serial and b_serial:
            out.append((a_device, a_iface, b_device, b_iface))
    return out


def serial_dce_sides(spec: _SpecLike, port_plan: _PortPlanLike) -> dict[tuple[str, str], int]:
    """Which (device, interface) pairs must carry the DCE clock.

    One entry per back-to-back serial link, always on the link's `a` endpoint.
    The choice is deterministic on purpose: PT's `show controllers` output is
    what *verifies* the roles, and a build that flipped ends between runs would
    be impossible to compare.
    """
    return {
        (a_device, a_iface): DCE_CLOCK_RATE
        for a_device, a_iface, _b_device, _b_iface in _back_to_back_serial_links(
            spec, port_plan
        )
    }


def serial_clock_expectations(
    spec: _SpecLike, port_plan: _PortPlanLike
) -> dict[str, list[dict]]:
    """Per-device expectations for the serial clock read-back.

    Every endpoint of a back-to-back serial link gets an expectation: the DCE
    end with the clock rate we pushed, the DTE end with `None` so a stray clock
    on the far end is caught.
    """
    out: dict[str, list[dict]] = {}
    for a_device, a_iface, b_device, b_iface in _back_to_back_serial_links(
        spec, port_plan
    ):
        for device_id, interface, role, rate in (
            (a_device, a_iface, "dce", DCE_CLOCK_RATE),
            (b_device, b_iface, "dte", None),
        ):
            out.setdefault(device_id, []).append(
                {
                    "device": device_id,
                    "interface": interface,
                    "expected_role": role,
                    "expected_clock_rate": rate,
                }
            )
    return out


def read_serial_clock_evidence(adapter: Any, entries: list[dict]) -> list[dict]:
    """Compare PT's serial controller read-back against the compiled plan.

    Each entry is `{device, interface, expected_role, expected_clock_rate}`.
    Returns one record per entry with `status` in {pass, fail, unknown} and the
    verbatim PT output as `raw`; an adapter that cannot answer produces
    `unknown`, never a pass.
    """
    reader = getattr(adapter, "read_serial_controllers", None)
    evidence: list[dict] = []
    for entry in entries:
        device = str(entry["device"])
        interface = str(entry["interface"])
        expected_role = str(entry.get("expected_role") or "")
        expected_rate = entry.get("expected_clock_rate")
        base = {
            "device": device,
            "interface": interface,
            "expected_role": expected_role,
            "expected_clock_rate": expected_rate,
            "observed_role": None,
            "observed_clock_rate": None,
        }
        if reader is None:
            evidence.append({
                **base,
                "status": "unknown",
                "detail": "adapter cannot read serial controllers",
                "raw": None,
            })
            continue
        observed: Observable = reader(device, interface)
        if not observed.is_known:
            evidence.append({
                **base,
                "status": "unknown",
                "detail": str(observed.detail or observed.status.value),
                "raw": None,
            })
            continue
        parsed = observed.value or {}
        role = str(parsed.get("role") or "").lower() or None
        rate = parsed.get("clock_rate")
        matches = (role == expected_role) and (rate == expected_rate)
        detail = (
            f"PT reports {role or 'no role'} on {interface}"
            + (f" with clock rate {rate}" if rate else " with no clock")
            + f"; expected {expected_role}"
            + (
                f" with clock rate {expected_rate}"
                if expected_rate
                else " with no clock"
            )
        )
        evidence.append({
            **base,
            "status": "pass" if matches else "fail",
            "detail": detail,
            "raw": observed.detail,
        })
    return evidence
