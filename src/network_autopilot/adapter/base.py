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
from ..core.receipts import OperationReceipt
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
