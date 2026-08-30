"""
Orquestador principal.

Traduce un TopologyRequest a un TopologyPlan completo
con dispositivos, enlaces, IPs, DHCP y rutas, todo validado.
"""

from __future__ import annotations

from ..models.requests import TopologyRequest
from ..models.plans import TopologyPlan, DevicePlan, LinkPlan, ValidationCheck
from ..models.vlans import VLANConfig, AccessPortConfig, TrunkConfig
from ..models.errors import ValidationResult
from .ip_planner import IPPlanner
from .validator import validate_plan
from ...infrastructure.catalog.devices import (
    resolve_model, get_ports_by_speed,
)
from ...infrastructure.catalog.cables import infer_cable
from ...shared.enums import PortSpeed, DeviceRole, TopologyTemplate
from ...shared.constants import (
    DEFAULT_ROUTER, DEFAULT_SWITCH,
    LAYOUT_X_START, LAYOUT_Y_ROUTER, LAYOUT_Y_SWITCH, LAYOUT_Y_PC,
    LAYOUT_X_SPACING, LAYOUT_PC_X_SPACING, LAYOUT_CLOUD_X_OFFSET,
)


def plan_from_request(request: TopologyRequest) -> tuple[TopologyPlan, ValidationResult]:
    """
    Pipeline completo. Retorna (plan, validation_result).
    """
    plan = TopologyPlan()

    # Respetar las restricciones de la plantilla (ej router_on_a_stick y star son
    # 1 router por definición). Clampamos para que el default routers=2 no rompa la forma.
    from ...infrastructure.catalog.templates import get_template
    try:
        spec = get_template(request.template)
        request.routers = max(spec.min_routers, min(request.routers, spec.max_routers))
    except Exception:
        pass

    pcs_list = _normalize_pcs(request)
    laptops_list = _normalize_laptops(request)

    _create_devices(plan, request, pcs_list, laptops_list)
    _create_links(plan, request, pcs_list, laptops_list)
    _create_vlans(plan, request)

    ip_planner = IPPlanner(
        lan_base=request.base_network,
        link_base=request.inter_router_network,
    )
    ip_planner.plan_addressing(
        plan,
        routing=request.routing,
        dhcp=request.dhcp,
        floating_routes=request.floating_routes,
        ospf_process_id=request.ospf_process_id,
        eigrp_as=request.eigrp_as,
        dual_stack=request.dual_stack,
        ipv6_base=request.ipv6_base,
    )

    _create_validations(plan)

    result = validate_plan(plan)
    return plan, result


def _normalize_pcs(req: TopologyRequest) -> list[int]:
    if isinstance(req.pcs_per_lan, int):
        return [req.pcs_per_lan] * req.routers
    pcs = list(req.pcs_per_lan)
    while len(pcs) < req.routers:
        pcs.append(pcs[-1] if pcs else 3)
    return pcs


def _normalize_laptops(req: TopologyRequest) -> list[int]:
    if isinstance(req.laptops_per_lan, int):
        return [req.laptops_per_lan] * req.routers
    laptops = list(req.laptops_per_lan)
    while len(laptops) < req.routers:
        laptops.append(laptops[-1] if laptops else 0)
    return laptops


def _wifi_lan_indices(req: TopologyRequest, laptops_list: list[int]) -> list[int]:
    """LANs que necesitan AP propio: las que tienen laptops inalambricas.

    Antes se creaba UN solo AP para toda la topologia, cableado al switch de la
    primera LAN. Como en la vista logica el alcance RF es global, las laptops de
    TODAS las LANs se asociaban a ese AP y terminaban con IP del pool DHCP de la
    LAN 1 -- verificado contra PT 9.0.1: LT9, planificada en la LAN 5, recibia
    192.168.0.5/24. Un AP por LAN deja a cada laptop en la subred que le toca.
    """
    if not req.wireless_laptops or req.access_points:
        return []
    return [i for i in range(req.routers) if laptops_list[i] > 0]


def _layout_metrics(req: TopologyRequest, pcs_list: list[int],
                    laptops_list: list[int]) -> tuple[int, int]:
    """(x inicial, ancho de columna) para que ninguna LAN se salga ni se pise.

    El ancho fijo de 250 px alcanzaba para 3 hosts por LAN. Con 4 el cluster
    (4 x 80 = 320) desbordaba su columna y se metia en la LAN vecina, y como el
    cluster se centra, el primer host caia en x=-60: fuera del canvas.
    """
    per_lan = [max(p, l) for p, l in zip(pcs_list, laptops_list)]
    widest = max(per_lan) if per_lan else 0
    column_width = max(LAYOUT_X_SPACING, widest * LAYOUT_PC_X_SPACING)
    # El cluster se centra en su columna, asi que se extiende media anchura hacia
    # la izquierda: el origen tiene que dejar ese margen. Los servidores comparten
    # la columna de la LAN 1, asi que tambien cuentan.
    left_reach = max(widest, req.servers) * LAYOUT_PC_X_SPACING // 2
    return max(LAYOUT_X_START, left_reach), column_width


def _create_devices(plan: TopologyPlan, req: TopologyRequest, pcs_list: list[int], laptops_list: list[int]):
    router_model = req.router_model or DEFAULT_ROUTER
    switch_model = req.switch_model or DEFAULT_SWITCH
    x_start, column_width = _layout_metrics(req, pcs_list, laptops_list)

    def _column_x(lan_index: int) -> int:
        """Centro de la columna de la LAN `lan_index`."""
        return x_start + lan_index * column_width

    def _row_x(centre: int, count: int, index: int) -> int:
        """x del host `index` de una fila de `count`, centrada en `centre`."""
        return centre - (count * LAYOUT_PC_X_SPACING // 2) + index * LAYOUT_PC_X_SPACING

    # Fila propia para los AP: colgados del switch pero sin pisarlo ni pisar los
    # switches secundarios, que se desplazan de a 120 px sobre la misma fila.
    ap_y = LAYOUT_Y_SWITCH + 70

    # Routers
    for i in range(req.routers):
        role = DeviceRole.CORE_ROUTER if req.routers == 1 else (
            DeviceRole.EDGE_ROUTER if (i == 0 or i == req.routers - 1) else DeviceRole.CORE_ROUTER
        )
        plan.devices.append(DevicePlan(
            name=f"R{i + 1}", model=router_model, category="router",
            role=role,
            x=_column_x(i), y=LAYOUT_Y_ROUTER,
        ))

    # Switches + PCs + Laptops
    switch_idx = 0
    pc_idx = 0
    laptop_idx = 0
    for i in range(req.routers):
        for s in range(req.switches_per_router):
            switch_idx += 1
            plan.devices.append(DevicePlan(
                name=f"SW{switch_idx}", model=switch_model, category="switch",
                role=DeviceRole.ACCESS_SWITCH,
                x=_column_x(i) + s * 120, y=LAYOUT_Y_SWITCH,
            ))
            if s == 0:
                n_pcs = pcs_list[i]
                for p in range(n_pcs):
                    pc_idx += 1
                    plan.devices.append(DevicePlan(
                        name=f"PC{pc_idx}", model="PC-PT", category="pc",
                        role=DeviceRole.END_HOST,
                        x=_row_x(_column_x(i), n_pcs, p),
                        y=LAYOUT_Y_PC,
                    ))
                n_laptops = laptops_list[i]
                for l in range(n_laptops):
                    laptop_idx += 1
                    plan.devices.append(DevicePlan(
                        name=f"LT{laptop_idx}", model="Laptop-PT", category="laptop",
                        role=DeviceRole.END_HOST,
                        wireless=req.wireless_laptops,
                        x=_row_x(_column_x(i), n_laptops, l),
                        y=LAYOUT_Y_PC + 80,
                    ))

    # WiFi: un AP por LAN que tenga laptops inalambricas. Cada uno se cablea al
    # switch de SU LAN en _create_links, que es lo que pone a esas laptops en la
    # subred correcta.
    for k, lan in enumerate(_wifi_lan_indices(req, laptops_list)):
        plan.devices.append(DevicePlan(
            name=f"WAP{k + 1}", model="AccessPoint-PT", category="accesspoint",
            role=DeviceRole.END_HOST, x=_column_x(lan), y=ap_y,
        ))

    # Access Points pedidos explicitamente — uno por switch primario de cada router
    if req.access_points > 0:
        for i in range(min(req.access_points, req.routers)):
            plan.devices.append(DevicePlan(
                name=f"AP{i + 1}", model="AccessPoint-PT", category="accesspoint",
                role=DeviceRole.END_HOST,
                x=_column_x(i), y=ap_y,
            ))

    # Servers — en la columna del switch al que se cablean (el primero) y en su
    # propia fila. Antes se los mandaba al extremo derecho del diagrama mientras
    # el cable seguia yendo a SW1, dibujando diagonales de punta a punta.
    for i in range(req.servers):
        plan.devices.append(DevicePlan(
            name=f"SRV{i + 1}", model="Server-PT", category="server",
            role=DeviceRole.SERVER_HOST,
            x=_row_x(_column_x(0), req.servers, i),
            y=LAYOUT_Y_PC + 240,
        ))

    # Cloud / WAN
    if req.has_wan:
        plan.devices.append(DevicePlan(
            name="WAN", model="Cloud-PT", category="cloud",
            role=DeviceRole.WAN_CLOUD,
            x=x_start + req.routers * column_width + LAYOUT_CLOUD_X_OFFSET,
            y=LAYOUT_Y_ROUTER,
        ))


def _create_links(plan: TopologyPlan, req: TopologyRequest, pcs_list: list[int], laptops_list: list[int]):
    router_model_obj = resolve_model(req.router_model or DEFAULT_ROUTER)
    switch_model_obj = resolve_model(req.switch_model or DEFAULT_SWITCH)
    if not router_model_obj or not switch_model_obj:
        plan.errors.append("Modelo de router o switch no válido")
        return

    routers = plan.devices_by_category("router")
    switches = plan.devices_by_category("switch")
    pcs = plan.devices_by_category("pc")
    laptops = plan.devices_by_category("laptop")
    aps = plan.devices_by_category("accesspoint")
    servers = plan.devices_by_category("server")
    cloud = next((d for d in plan.devices if d.category == "cloud"), None)

    used: dict[str, list[str]] = {d.name: [] for d in plan.devices}

    def _next_port(name: str, model: str, speed: str) -> str | None:
        m = resolve_model(model)
        if not m:
            return None
        for p in get_ports_by_speed(m, speed):
            if p.full_name not in used[name]:
                used[name].append(p.full_name)
                return p.full_name
        return None

    def _gig(name: str, model: str) -> str | None:
        return _next_port(name, model, PortSpeed.GIGABIT_ETHERNET)

    def _fast(name: str, model: str) -> str | None:
        return _next_port(name, model, PortSpeed.FAST_ETHERNET)

    # Router ↔ Router — la FORMA depende del template (antes siempre era cadena, lo
    # que hacía que three_router_triangle quedara sin el cierre R3↔R1 = sin redundancia).
    def _link_routers(ra, rb):
        p1, p2 = _gig(ra.name, ra.model), _gig(rb.name, rb.model)
        if p1 and p2:
            plan.links.append(LinkPlan(
                device_a=ra.name, port_a=p1,
                device_b=rb.name, port_b=p2,
                cable=infer_cable("router", "router"),
            ))

    if req.template == TopologyTemplate.HUB_SPOKE and len(routers) >= 2:
        # Estrella de routers: R1 (hub) ↔ cada spoke.
        hub = routers[0]
        for spoke in routers[1:]:
            _link_routers(hub, spoke)
    else:
        # Cadena: R[i] ↔ R[i+1].
        for i in range(len(routers) - 1):
            _link_routers(routers[i], routers[i + 1])
        # Triángulo/anillo: cierra el último ↔ el primero para dar el camino redundante.
        if req.template == TopologyTemplate.THREE_ROUTER_TRIANGLE and len(routers) >= 3:
            _link_routers(routers[-1], routers[0])

    # Router ↔ Switch
    spr = req.switches_per_router
    for i, router in enumerate(routers):
        for sw in switches[i * spr:(i + 1) * spr]:
            rp, sp = _gig(router.name, router.model), _gig(sw.name, sw.model)
            if rp and sp:
                plan.links.append(LinkPlan(
                    device_a=router.name, port_a=rp,
                    device_b=sw.name, port_b=sp,
                    cable=infer_cable("router", "switch"),
                ))

    # Switch ↔ PCs
    pc_idx = 0
    for i in range(req.routers):
        primary_sw = switches[i * spr] if i * spr < len(switches) else None
        if not primary_sw:
            continue
        for _ in range(pcs_list[i]):
            if pc_idx >= len(pcs):
                break
            pc = pcs[pc_idx]
            sp, pp = _fast(primary_sw.name, primary_sw.model), _fast(pc.name, pc.model)
            if sp and pp:
                plan.links.append(LinkPlan(
                    device_a=primary_sw.name, port_a=sp,
                    device_b=pc.name, port_b=pp,
                    cable=infer_cable("switch", "pc"),
                ))
            pc_idx += 1

    # Switch ↔ Laptops
    laptop_idx = 0
    for i in range(req.routers):
        primary_sw = switches[i * spr] if i * spr < len(switches) else None
        if not primary_sw:
            continue
        for _ in range(laptops_list[i]):
            if laptop_idx >= len(laptops):
                break
            lt = laptops[laptop_idx]
            laptop_idx += 1
            if lt.wireless:
                # WiFi: la asociación es por RF (auto a un AP por SSID default), no por cable.
                continue
            sp, lp = _fast(primary_sw.name, primary_sw.model), _fast(lt.name, lt.model)
            if sp and lp:
                plan.links.append(LinkPlan(
                    device_a=primary_sw.name, port_a=sp,
                    device_b=lt.name, port_b=lp,
                    cable=infer_cable("switch", "pc"),
                ))

    # Switch ↔ Access Points — cada AP al switch de SU LAN.
    #
    # Con el AP unico esto no importaba: iba al primer switch y listo. Ahora los
    # AP automaticos existen solo para las LANs que tienen laptops WiFi, asi que
    # el indice del AP no coincide con el de la LAN y hay que mapearlo.
    wifi_lans = _wifi_lan_indices(req, laptops_list)
    if wifi_lans:
        ap_to_lan = list(enumerate(wifi_lans))
    else:
        ap_to_lan = [(k, k) for k in range(min(req.access_points, req.routers))]

    for ap_idx, lan in ap_to_lan:
        if ap_idx >= len(aps):
            break
        primary_sw = switches[lan * spr] if lan * spr < len(switches) else None
        if not primary_sw:
            continue
        ap = aps[ap_idx]
        sp, ap_port = _fast(primary_sw.name, primary_sw.model), _fast(ap.name, ap.model)
        if sp and ap_port:
            plan.links.append(LinkPlan(
                device_a=primary_sw.name, port_a=sp,
                device_b=ap.name, port_b=ap_port,
                cable=infer_cable("switch", "pc"),
            ))

    # Switch ↔ Servers
    if servers and switches:
        sw = switches[0]
        for srv in servers:
            sp, srp = _fast(sw.name, sw.model), _fast(srv.name, srv.model)
            if sp and srp:
                plan.links.append(LinkPlan(
                    device_a=sw.name, port_a=sp,
                    device_b=srv.name, port_b=srp,
                    cable=infer_cable("switch", "server"),
                ))

    # Router ↔ Cloud
    if cloud and routers:
        last = routers[-1]
        rp, cp = _gig(last.name, last.model), _fast(cloud.name, cloud.model)
        if rp and cp:
            plan.links.append(LinkPlan(
                device_a=last.name, port_a=rp,
                device_b=cloud.name, port_b=cp,
                cable=infer_cable("router", "cloud"),
            ))


def _switch_port_for(plan: TopologyPlan, switch_name: str, peer_name: str) -> str | None:
    """Puerto del switch en el link switch↔peer (o None si no hay link)."""
    for link in plan.links:
        if link.device_a == switch_name and link.device_b == peer_name:
            return link.port_a
        if link.device_b == switch_name and link.device_a == peer_name:
            return link.port_b
    return None


def _create_vlans(plan: TopologyPlan, req: TopologyRequest):
    """ROUTER_ON_A_STICK: convierte la LAN única en N VLANs con inter-VLAN routing.

    La topología física (1 router ↔ 1 switch por trunk, switch ↔ PCs por access) ya la
    creó _create_links; aquí añadimos la capa lógica: declara VLANs, marca el uplink del
    switch como trunk, asigna cada PC a una VLAN access y setea DevicePlan.vlan. El IP
    planner hace el subnetting por-VLAN y crea las subinterfaces .1q del router.
    """
    if req.template != TopologyTemplate.ROUTER_ON_A_STICK:
        return
    routers = plan.devices_by_category("router")
    switches = plan.devices_by_category("switch")
    pcs = plan.devices_by_category("pc")
    if not routers or not switches or not pcs:
        return
    router, switch = routers[0], switches[0]

    vlan_count = req.vlans if req.vlans and req.vlans > 0 else 2
    vlan_count = max(1, min(vlan_count, len(pcs)))  # no más VLANs que PCs
    vlan_ids = [10 * (i + 1) for i in range(vlan_count)]
    plan.vlans = [VLANConfig(vlan_id=v, name=f"VLAN{v}") for v in vlan_ids]

    for idx, pc in enumerate(pcs):
        vid = vlan_ids[idx % vlan_count]
        pc.vlan = vid
        sw_port = _switch_port_for(plan, switch.name, pc.name)
        if sw_port:
            plan.access_ports.append(
                AccessPortConfig(switch=switch.name, port=sw_port, vlan_id=vid)
            )

    trunk_port = _switch_port_for(plan, switch.name, router.name)
    if trunk_port:
        plan.trunks.append(
            TrunkConfig(switch=switch.name, port=trunk_port, allowed_vlans=vlan_ids)
        )


def _create_validations(plan: TopologyPlan):
    pcs = plan.devices_by_category("pc")
    if len(pcs) >= 2:
        plan.validations.append(ValidationCheck(
            check_type="ping", from_device=pcs[0].name,
            to_target=pcs[-1].name, expected="Reply",
        ))
