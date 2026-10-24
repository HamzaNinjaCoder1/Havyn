"""PacketTracerAdapter — the only door between the Autopilot engine and Packet Tracer.

Everything above this layer (spec handling, planning, verification, diagnosis,
repair, evidence) depends on this abstraction, never on raw Packet Tracer IPC
details. Concrete adapters: the deterministic offline `NullAdapter` and the
project-owned Packet Tracer adapter in `adapter/packet_tracer/`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from ..core.actual_state import ActualNetworkState, DeviceObservation
from ..core.ids import utc_now_iso
from ..core.observability import Observable
from ..core.receipts import OperationReceipt, OperationResult
from ..core.requirements import FlowResult


@dataclass(frozen=True)
class AdapterInfo:
    """Result of a reachability/identity probe."""

    adapter_name: str
    adapter_version: str
    reachable: bool
    detail: Observable[str] = field(default_factory=Observable.not_observed)


class AdapterError(RuntimeError):
    """Raised for adapter-level failures that are not operation outcomes."""


class PacketTracerAdapter(ABC):
    """The contract every Packet Tracer integration must satisfy.

    Mutating operations follow PRECONDITION → MUTATION → READ-BACK →
    POSTCONDITION and return an `OperationReceipt`. A receipt is *confirmed*
    only when its read-back postconditions passed — a bare SUCCESS is
    explicitly not a confirmation.
    """

    name: ClassVar[str] = "abstract"
    version: ClassVar[str] = "0"

    # -- probing ------------------------------------------------------------

    @abstractmethod
    def ping(self, timeout: float = 5.0) -> AdapterInfo:
        """Probe reachability and identity. Never mutates."""

    # -- inspection ---------------------------------------------------------

    @abstractmethod
    def device_exists(self, name: str) -> Observable[bool]:
        """Known True/False, or UNKNOWN when the runtime cannot answer."""

    @abstractmethod
    def list_device_names(self) -> Observable[list[str]]:
        """Names of all devices in the workspace, or an honest UNKNOWN."""

    @abstractmethod
    def link_exists(
        self, a_device: str, a_port: str, b_device: str, b_port: str
    ) -> Observable[bool]:
        """Whether both named ports currently have a link attached.

        Sufficient for idempotent link creation (a port has at most one
        link); peer correctness is verified separately. Adapters that cannot
        observe links report UNKNOWN — the executor then attempts creation
        and records the outcome honestly.
        """

    @abstractmethod
    def inspect_device(self, name: str) -> DeviceObservation:
        """Observed state of one device (fields may be explicitly unknown)."""

    # -- mutations ----------------------------------------------------------

    @abstractmethod
    def create_device(
        self,
        name: str,
        role: str,
        model_hint: str | None = None,
        position: tuple[int, int] | None = None,
        operation_id: str | None = None,
    ) -> OperationReceipt: ...

    @abstractmethod
    def rename_device(
        self, old_name: str, new_name: str, operation_id: str | None = None
    ) -> OperationReceipt: ...

    @abstractmethod
    def position_device(
        self, name: str, x: int, y: int, operation_id: str | None = None
    ) -> OperationReceipt: ...

    @abstractmethod
    def create_link(
        self,
        a_device: str,
        a_port: str | None,
        b_device: str,
        b_port: str | None,
        media: str = "auto",
        operation_id: str | None = None,
    ) -> OperationReceipt: ...

    @abstractmethod
    def delete_device(
        self, name: str, operation_id: str | None = None
    ) -> OperationReceipt:
        """Remove a device from the workspace.

        Idempotent: a device that is already absent is a SUCCESS+confirmed
        receipt, not an error. Used by rollback, test cleanup and the delete
        controller — never deletes a device this run did not own.
        """

    @abstractmethod
    def delete_link(
        self,
        a_device: str,
        a_port: str,
        b_device: str,
        b_port: str,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        """Remove the physical link on endpoint A. Idempotent (absent = OK)."""

    @abstractmethod
    def install_module(
        self,
        device: str,
        slot: str,
        module: str,
        expected_port: str,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        """Install an expansion module into a device slot.

        Idempotent: a module already providing `expected_port` is a CONFIRMED
        success without a second install. The receipt is confirmed only when
        the port the module is supposed to expose is actually visible on the
        device — never on `addModule` answering true alone.
        """

    @abstractmethod
    def configure_ios(
        self, device: str, commands: list[str], operation_id: str | None = None
    ) -> OperationReceipt:
        """Push an IOS CLI block. Acceptance by IOS may be unobservable — the
        receipt must say so instead of implying the config took effect."""

    @abstractmethod
    def configure_gateway_lan(
        self, device: str, ip: str, mask: str, operation_id: str | None = None
    ) -> OperationReceipt:
        """Set a Linksys-family gateway's LAN address on its Vlan1 SVI (#10).

        PT's Linksys-WRT300N answers `configureIosDevice` with `true` and
        applies nothing (live probe); the port API (`setIpSubnetMask`) is the
        only push that moves the address. The receipt is confirmed only when
        the read-back shows the address — a push that does not land is FAILED.
        """

    @abstractmethod
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
    ) -> OperationReceipt: ...

    def configure_host_dns(self, device: str, dns: str) -> OperationReceipt:
        """Set a host's DNS server; adapters with no DNS API report UNKNOWN."""
        return OperationReceipt(
            operation_id="dns-unsupported",
            operation="configure_host_dns",
            requested={"device": device, "dns": dns},
            result=OperationResult.UNKNOWN,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            error="this adapter has no host DNS API",
        )

    @abstractmethod
    def save_project(self, filename: str, operation_id: str | None = None) -> OperationReceipt: ...

    @abstractmethod
    def open_project(self, path: str, operation_id: str | None = None) -> OperationReceipt: ...

    # -- verification -------------------------------------------------------

    @abstractmethod
    def run_flow_test(
        self,
        source: str,
        destination: str,
        *,
        transport: str = "icmp",
        port: int | None = None,
        destination_ip: str | None = None,
    ) -> FlowResult:
        """One traffic flow, actually executed and observed. NEVER fabricated.

        `transport`/`port` are the criterion's declared protocol (tcp/udp/icmp
        and the target port). An adapter that can only drive ICMP must return
        UNKNOWN for any other transport — proving a TCP:443 criterion with an
        echo reply is exactly the false positive this contract exists to
        prevent (P1-5).
        """

    def run_flow_tests_bulk(
        self, specs: list[dict[str, object]]
    ) -> list[FlowResult] | None:
        """Run many flows concurrently; one ``FlowResult`` per spec, in order.

        Returns ``None`` when the adapter cannot batch (the caller then runs
        each spec through ``run_flow_test`` serially). ``None`` is the
        base-class answer, so an adapter that has no data plane to parallelise
        keeps the exact serial semantics and verdicts it already had.

        Flows that share a source device share one console, so a caller may
        batch them; the adapter is free to serialise per source internally —
        only the transport is shared, never the verdicts.
        """
        return None

    def read_routing_table(self, device: str) -> Observable[list]:
        """Structured route table for one device, or an honest UNKNOWN.

        Concrete default (S1.11/F7): an adapter with no console reads reports
        NOT_OBSERVED, so the route-missing rule stays silent instead of
        guessing. The live Packet Tracer adapter overrides this with a real
        `show ip route` read-back.
        """
        return Observable[list].not_observed()

    # -- brownfield read surface (Stage 4) ---------------------------------

    def read_running_config(self, device: str) -> Observable[str]:
        """The device's own running configuration, verbatim.

        UNSUPPORTED by default: an adapter that cannot read a device's
        configuration must say so, never return an empty string that a parser
        would read as "no configuration". The live adapter implements this for
        IOS routers/switches and the ASA; a Linksys exposes no CLI read-back
        and reports UNSUPPORTED with the measured reason.
        """
        return Observable[str].unsupported(
            "this adapter has no running-configuration reader"
        )

    def read_host_ip_config(self, device: str) -> Observable[dict]:
        """One host's observed IP configuration (ip/mask/gateway/dns/dhcp).

        UNSUPPORTED by default. The live adapter reads it from the host's own
        console (`ipconfig /all`), so values carry the host as their source
        instead of being inferred from the spec.
        """
        return Observable[dict].unsupported(
            "this adapter has no host IP-configuration reader"
        )

    def read_device_positions(self) -> Observable[dict[str, tuple[int, int]]]:
        """Canvas positions keyed by device name, or an honest UNKNOWN."""
        return Observable[dict].unsupported(
            "this adapter cannot read device canvas positions"
        )

    def read_spanning_tree(self, device: str) -> Observable[list]:
        """Structured `show spanning-tree` state (blocking ports per VLAN).

        Default UNSUPPORTED: a device with no STP console has no STP evidence,
        and "no evidence" must not be mistaken for "nothing is blocking".
        """
        return Observable[list].unsupported(
            "this adapter has no spanning-tree reader"
        )

    def read_vlans(self, device: str) -> Observable[list]:
        """Structured `show vlan brief` table, or an honest gap."""
        return Observable[list].unsupported(
            "this adapter has no VLAN table reader"
        )

    def read_trunk_allowed(self, device: str) -> Observable[dict]:
        """`show interfaces trunk` -> {port: allowed VLAN ids}, or a gap."""
        return Observable[dict].unsupported(
            "this adapter has no trunk table reader"
        )

    def read_arp_table(self, device: str) -> Observable[list]:
        """Observed ARP cache as [{'ip','mac','interface','type'}], or a gap."""
        return Observable[list].unsupported(
            "this adapter has no ARP table reader"
        )

    def read_nat_translations(self, device: str) -> Observable[list]:
        """Observed NAT translation table, or an honest gap."""
        return Observable[list].unsupported(
            "this adapter has no NAT translation reader"
        )

    # -- convenience --------------------------------------------------------

    def observe_state(
        self,
        names: list[str] | None = None,
        *,
        observed_at: str | None = None,
    ) -> ActualNetworkState:
        """Aggregate per-device inspection into an ActualNetworkState.

        A device the adapter reports as KNOWN-absent is NOT added to the
        state: absence is a fact about the workspace, and silently adding an
        empty observation would make a missing device look "present with
        unknown fields" — exactly the collapse this product forbids.
        """
        state = ActualNetworkState(
            source=f"adapter:{self.name}", observed_at=observed_at or utc_now_iso()
        )
        if names is None:
            listed = self.list_device_names()
            if listed.is_known:
                names = list(listed.value or [])
            else:
                names = []
                state.metadata["device_list"] = Observable[str].unknown(
                    listed.detail or listed.status.value
                )
        for name in names:
            exists = self.device_exists(name)
            if exists.is_known and not exists.value:
                state.metadata[f"absent:{name}"] = Observable[str].known(
                    "reported absent by adapter", source=exists.source
                )
                continue
            state.observe_device(self.inspect_device(name))
        return state
