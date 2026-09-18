"""The network compiler: NetworkSpec + AddressPlan -> executable OperationDag.

Deterministic end to end: same spec, same plan, same compiler version -> the
same operation DAG (same ids, same order, same idempotency keys). The compiler
knows the adapter CONTRACT, never the transport: it emits create/link/
configure/verify/persist operations; the adapter decides how each one reaches
Packet Tracer.

Phase-2 scope is explicit. Features outside it are refused by name — never
half-implemented, never silently skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.ids import build_fingerprint
from ..core.network_spec import DeviceRole, NetworkSpec
from ..ipam.planner import AddressPlan
from .dag import OperationDag
from .operations import Check, MutationClassification, NetworkOperation, Risk, key_for
from .layout import build_layout
from .port_solver import PortPlan, solve_ports
from .preflight import PreflightResult, run_preflight

COMPILER_VERSION = "2.0.0"

_PORT_TEMPLATES: dict[str, list[str]] = {
    "router": ["GigabitEthernet0/0", "GigabitEthernet0/1", "GigabitEthernet0/2"],
    "switch": ["FastEthernet0/1", "FastEthernet0/2", "FastEthernet0/3", "FastEthernet0/4",
               "FastEthernet0/5", "FastEthernet0/6", "FastEthernet0/7", "FastEthernet0/8",
               "GigabitEthernet0/1", "GigabitEthernet0/2"],
    "l3_switch": ["FastEthernet0/1", "FastEthernet0/2", "GigabitEthernet0/1"],
    "pc": ["FastEthernet0"],
    "server": ["FastEthernet0"],
    "laptop": ["FastEthernet0"],
    "access_point": ["FastEthernet0"],
    "cloud": [],
    "other": ["FastEthernet0"],
}

_PHASE2_FEATURES = {
    "devices": "roles router/switch/pc/server/laptop with fixed models",
    "links": "auto-media ethernet links",
    "addressing": "static IPv4 interface addressing",
    "routing": "static routes (routing.mode static/connected)",
    "verification": "structural, addressing and ICMP flow verification",
    "persistence": "project save",
}


class CompileError(ValueError):
    """The spec asks for something the compiler does not (yet) implement."""


@dataclass
class CompileResult:
    dag: OperationDag
    unsupported_features: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fingerprint: dict[str, str] = field(default_factory=dict)
    port_plan: PortPlan | None = None
    preflight: PreflightResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dag": self.dag.to_dict(),
            "unsupported_features": list(self.unsupported_features),
            "notes": list(self.notes),
            "fingerprint": self.fingerprint,
        }


def _model_for(spec: NetworkSpec, device_id: str, role: DeviceRole) -> str:
    device = spec.device_by_id(device_id)
    if device and device.model_hint:
        return device.model_hint
    return {
        DeviceRole.ROUTER: "2911",
        DeviceRole.SWITCH: "2960-24TT",
        DeviceRole.L3_SWITCH: "3560-24PS",
        DeviceRole.PC: "PC",
        DeviceRole.SERVER: "Server-PT",
        DeviceRole.LAPTOP: "Laptop-PT",
    }.get(role, "Generic")


def _check_phase2_support(spec: NetworkSpec) -> None:
    unsupported: list[str] = []
    if spec.vlans:
        unsupported.append("vlans")
    if spec.policies:
        unsupported.append("security policies (ACL)")
    if spec.routing.ospf is not None or spec.routing.mode not in ("connected", "static"):
        unsupported.append(f"routing mode {spec.routing.mode!r}")
    if spec.routing.static_routes and spec.routing.mode == "connected":
        unsupported.append("static routes with routing.mode=connected (use 'static')")
    for assignment in spec.ip_assignments:
        if assignment.interface_hint and "." in assignment.interface_hint:
            unsupported.append(
                f"subinterface {assignment.interface_hint!r} (VLAN trunking)"
            )
        if assignment.dhcp is True:
            unsupported.append("DHCP client configuration")
    for subnet in spec.subnets:
        if subnet.dhcp:
            unsupported.append(f"DHCP service on subnet {subnet.id!r}")
    if unsupported:
        raise CompileError(
            "spec requires features outside the Phase-2 compiler scope: "
            + ", ".join(sorted(set(unsupported)))
            + f". Supported: {sorted(_PHASE2_FEATURES.values())}"
        )


def compile_spec(
    spec: NetworkSpec, plan: AddressPlan, packet_tracer_version: str = "unknown"
) -> CompileResult:
    """Compile a validated spec + addressing plan into an OperationDag."""
    _check_phase2_support(spec)

    dag = OperationDag()
    device_ops: dict[str, str] = {}
    port_plan = solve_ports(spec, plan)
    if port_plan.incompatible:
        raise CompileError("port preflight failed: " + "; ".join(port_plan.incompatible))
    layout = build_layout(spec)
    if not layout.valid:
        raise CompileError(
            "layout validation failed: "
            + "; ".join(issue.detail for issue in layout.issues)
        )

    # -- devices ------------------------------------------------------------
    assignment_ports: dict[tuple[str, int], str] = {}
    assignment_index: dict[str, int] = {}
    for assignment in plan.assignments:
        if assignment.interface_hint:
            continue
        candidates = sorted(
            resolution.interface
            for resolution in port_plan.resolutions.values()
            if resolution.device == assignment.device
        )
        index = assignment_index.get(assignment.device, 0)
        if index < len(candidates):
            assignment_ports[(assignment.device, index)] = candidates[index]
        assignment_index[assignment.device] = index + 1

    for device in spec.devices:
        model = _model_for(spec, device.id, device.role)
        x, y = layout.positions[device.id]
        op = NetworkOperation(
            operation_id=f"op-create-{device.id}",
            operation_type="create_device",
            inputs={
                "name": device.id,
                "role": device.role.value,
                "model": model,
                "position": [x, y],
            },
            preconditions=(Check("device_absent", device.id, "absent"),),
            expected_postconditions=(
                Check("device_present", device.id, "present"),
                Check("device_model", device.id, model),
            ),
            classification=MutationClassification.CREATE,
            risk=Risk.LOW,
            blast_radius=(device.id,),
            retry_policy=1,
            idempotency_key=key_for("create_device", device.id, model),
        )
        dag.add(op)
        device_ops[device.id] = op.operation_id

    # -- links --------------------------------------------------------------
    for link in spec.links:
        a_device, a_port, b_device, b_port = port_plan.link_endpoints[link.id]
        media = port_plan.resolutions[f"{a_device}:{a_port}"].media
        op = NetworkOperation(
            operation_id=f"op-link-{link.id}",
            operation_type="create_link",
            inputs={
                "link_id": link.id,
                "a_device": a_device,
                "a_port": a_port,
                "b_device": b_device,
                "b_port": b_port,
                "media": media,
            },
            dependencies=(device_ops[link.a.device], device_ops[link.b.device]),
            preconditions=(
                Check("device_present", link.a.device, "present"),
                Check("device_present", link.b.device, "present"),
                Check("port_free", f"{link.a.device}:{a_port}", "free"),
                Check("port_free", f"{link.b.device}:{b_port}", "free"),
            ),
            expected_postconditions=(
                Check("link_present", link.id, "present"),
            ),
            classification=MutationClassification.LINK,
            risk=Risk.LOW,
            blast_radius=(link.a.device, link.b.device),
            retry_policy=1,
            idempotency_key=key_for(
                "create_link", tuple(sorted([(link.a.device, a_port), (link.b.device, b_port)]))
            ),
        )
        dag.add(op)

    # -- configuration: routers ----------------------------------------------
    route_deps: list[str] = []
    for device in spec.devices:
        if device.role is not DeviceRole.ROUTER:
            continue
        commands: list[str] = [f"hostname {device.id}"]
        interface_commands: list[str] = []
        device_assignment_index = 0
        for assignment in plan.assignments:
            if assignment.device != device.id or assignment.ip is None:
                continue
            iface = assignment.interface_hint or assignment_ports.get(
                (device.id, device_assignment_index)
            )
            if not iface:
                raise CompileError(
                    f"no resolved interface for address assignment on {device.id}"
                )
            device_assignment_index += 1
            mask = plan.allocation_by_subnet(assignment.subnet_id)
            if mask is None:  # pragma: no cover - plan/spec consistency
                raise CompileError(f"assignment {assignment.device} has no allocation")
            link_dep = _link_op_for_port(dag, device.id, iface)
            interface_commands.extend(
                [
                    f"interface {iface}",
                    f"ip address {assignment.ip} {mask.netmask}",
                    "no shutdown",
                ]
            )
            if link_dep and link_dep not in route_deps:
                route_deps.append(link_dep)
        if interface_commands:
            commands.extend(interface_commands)
        static_routes: list[dict[str, str]] = []
        if spec.routing.mode == "static":
            for route in spec.routing.static_routes:
                if route.device != device.id:
                    continue
                static_routes.append(
                    {
                        "destination": route.destination,
                        "next_hop": route.next_hop or "",
                        "exit_interface": route.exit_interface or "",
                    }
                )
                commands.append(
                    f"ip route {route.destination} "
                    f"{route.next_hop or route.exit_interface or ''}".strip()
                )
        deps: list[str] = [device_ops[device.id]]
        # interfaces must exist before they are configured where a link op
        # targets the configured port
        deps.extend(d for d in route_deps if d.startswith("op-link-"))
        op = NetworkOperation(
            operation_id=f"op-config-{device.id}",
            operation_type="configure_ios",
            inputs={
                "device": device.id,
                "commands": commands,
                "static_routes": static_routes,
            },
            dependencies=tuple(dict.fromkeys(deps)),
            preconditions=(Check("device_present", device.id, "present"),),
            expected_postconditions=(
                Check("ios_accepted", device.id, "commands accepted"),
            ),
            classification=MutationClassification.CONFIGURE,
            risk=Risk.MEDIUM,
            blast_radius=(device.id,),
            retry_policy=1,
            idempotency_key=key_for("configure_ios", device.id, commands),
        )
        dag.add(op)

    # -- configuration: hosts (PCs, servers, laptops) --------------------------
    for device in spec.devices:
        if device.role not in (DeviceRole.PC, DeviceRole.SERVER, DeviceRole.LAPTOP):
            continue
        assignment = next((a for a in plan.assignments if a.device == device.id), None)
        if assignment is None or assignment.ip is None:
            continue
        link_dep = _link_op_for_port(
            dag, device.id, assignment.interface_hint or "FastEthernet0"
        )
        deps = [device_ops[device.id]] + ([link_dep] if link_dep else [])
        op = NetworkOperation(
            operation_id=f"op-host-{device.id}",
            operation_type="configure_host",
            inputs={
                "device": device.id,
                "dhcp": False,
                "ip": assignment.ip,
                "mask": plan.allocation_by_subnet(assignment.subnet_id).netmask,
                "gateway": assignment.gateway,
            },
            dependencies=tuple(dict.fromkeys(deps)),
            preconditions=(Check("device_present", device.id, "present"),),
            expected_postconditions=(
                Check("host_ip", device.id, assignment.ip),
            ),
            classification=MutationClassification.CONFIGURE,
            risk=Risk.MEDIUM,
            blast_radius=(device.id,),
            retry_policy=1,
            idempotency_key=key_for(
                "configure_host", device.id, assignment.ip, assignment.gateway
            ),
        )
        dag.add(op)

    # -- verification ---------------------------------------------------------
    config_ops = [
        node.operation.operation_id
        for node in dag.nodes.values()
        if node.operation.classification is MutationClassification.CONFIGURE
    ]
    flow_ops: list[str] = []
    for criterion in spec.acceptance_criteria:
        test = criterion.test
        if getattr(test, "kind", "") == "flow":
            op = NetworkOperation(
                operation_id=f"op-flow-{criterion.id}",
                operation_type="verify_flow",
                inputs={
                    "criterion_id": criterion.id,
                    "requirement_ids": list(criterion.requirement_ids),
                    "source": test.source,
                    "destination": test.destination,
                    "expect": test.expect,
                    "transport": test.transport,
                    "port": test.port,
                },
                dependencies=tuple(dict.fromkeys(config_ops)) or (),
                classification=MutationClassification.OBSERVE,
                risk=Risk.LOW,
                blast_radius=(),
                idempotency_key=key_for(
                    "verify_flow", criterion.id, test.source, test.destination
                ),
            )
            dag.add(op)
            flow_ops.append(op.operation_id)

    verify_state = NetworkOperation(
        operation_id="op-verify-state",
        operation_type="verify_state",
        inputs={"device_names": [d.id for d in spec.devices]},
        dependencies=tuple(dict.fromkeys(config_ops)) or (),
        classification=MutationClassification.OBSERVE,
        risk=Risk.LOW,
        blast_radius=(),
        idempotency_key=key_for("verify_state", spec.spec_hash()),
    )
    dag.add(verify_state)

    # -- persistence ------------------------------------------------------------
    filename = f"{spec.name.replace(' ', '_').lower()}.pkt"
    save_deps = flow_ops + [verify_state.operation_id]
    dag.add(
        NetworkOperation(
            operation_id="op-save",
            operation_type="save_project",
            inputs={"filename": filename},
            dependencies=tuple(dict.fromkeys(save_deps)),
            classification=MutationClassification.PERSIST,
            risk=Risk.MEDIUM,
            blast_radius=(),
            idempotency_key=key_for("save_project", filename, spec.spec_hash()),
        )
    )

    dag.validate()

    # -- port solving + preflight (§9 + §G) --------------------------------
    port_plan = solve_ports(spec, plan)
    preflight_result = run_preflight(spec, plan, port_plan)

    return CompileResult(
        dag=dag,
        unsupported_features=[],
        notes=[
            "Phase-2 compiler: devices, links, static IPv4, static routes, "
            "flow verification, save. VLANs/ACL/DHCP/OSPF are refused by name."
        ],
        fingerprint=build_fingerprint(
            spec.spec_hash(),
            {
                "compiler": COMPILER_VERSION,
                "packet_tracer": packet_tracer_version,
                "adapter": "adapter-agnostic",
            },
            layout_seed=spec.layout_seed,
        ),
        port_plan=port_plan,
        preflight=preflight_result,
    )


def net_build(
    spec: NetworkSpec,
    plan: AddressPlan,
    gate: "SafetyGate | None" = None,
    adapter: "PacketTracerAdapter | None" = None,
) -> tuple[CompileResult | None, str | None]:
    """Compile with preflight as a HARD execution gate (§G).

    Flow: request → IPAM → compile → port/media/module preflight →
    safety gate → execution. If preflight fails, execution is blocked
    and a structured failure is returned immediately.
    """
    from .compiler import compile_spec

    result = compile_spec(spec, plan)

    # Preflight is a HARD gate: if it fails, DO NOT execute
    if result.preflight is not None and not result.preflight.can_execute:
        error_parts = result.preflight.errors
        if result.port_plan:
            for key, res in result.port_plan.resolutions.items():
                if res.constraint.name != "COMPATIBLE":
                    error_parts.append(f"{key}: {res.constraint.value}")
        return None, (
            "preflight blocked execution: "
            + "; ".join(error_parts)
        )

    # If preflight has warnings, still proceed but record them
    if result.preflight is not None and result.preflight.warnings:
        # Warnings don't block, but they're evidence
        pass

    return result, None


def _link_op_for_port(dag: OperationDag, device: str, interface: str) -> str | None:
    """The link op that terminates on (device, interface), if any."""
    for node in dag.nodes.values():
        op = node.operation
        if op.operation_type != "create_link":
            continue
        inputs = op.inputs
        if (inputs.get("a_device"), inputs.get("a_port")) == (device, interface) or (
            inputs.get("b_device"), inputs.get("b_port")
        ) == (device, interface):
            return op.operation_id
    return None
