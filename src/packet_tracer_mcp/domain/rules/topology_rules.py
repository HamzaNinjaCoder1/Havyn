"""Reglas sobre la FORMA del grafo y la coherencia del routing.

Las demás reglas miran dispositivos, puertos e IPs de a uno. Estas dos miran el
plan entero, que es donde se escondía el peor fallo silencioso del pipeline:

`hub_spoke` pide que el hub se enlace con cada spoke, pero un 2911 tiene tres
puertos Gigabit. Con seis routers el hub se queda sin puertos en el cuarto y
`_link_routers` simplemente no creaba el enlace — sin avisar. El resultado era un
plan con dos routers sueltos, sin interfaces, sin enlaces y con un OSPF de
`router-id 0.0.0.0` y cero redes... que el validador aprobaba con `valid=true`.

Una topología partida en islas se ve idéntica a una sana en todos los chequeos
por-dispositivo: cada dispositivo existe, cada puerto es válido, ninguna IP
choca. Lo único que la delata es recorrer el grafo.
"""

from __future__ import annotations

from ..models.plans import TopologyPlan
from ..models.errors import PlanError, ErrorCode


def validate_connectivity(plan: TopologyPlan) -> list[PlanError]:
    """Verifica que todos los dispositivos cableados formen UN solo componente.

    Las laptops WiFi quedan fuera del grafo a propósito: no llevan cable porque
    se asocian por RF, así que contarlas como islas marcaría cada topología
    inalámbrica como rota.
    """
    wired = [d for d in plan.devices if not d.wireless]
    if len(wired) <= 1:
        # Cero o un dispositivo es, trivialmente, un solo componente.
        return []

    names = {d.name for d in wired}
    adjacency: dict[str, set[str]] = {name: set() for name in names}
    for link in plan.links:
        if link.device_a in names and link.device_b in names:
            adjacency[link.device_a].add(link.device_b)
            adjacency[link.device_b].add(link.device_a)

    # BFS desde el primer dispositivo: lo que no se alcance es otra isla.
    start = wired[0].name
    seen = {start}
    queue = [start]
    while queue:
        current = queue.pop()
        for neighbour in adjacency[current]:
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)

    unreachable = [d for d in wired if d.name not in seen]
    if not unreachable:
        return []

    orphan_names = ", ".join(d.name for d in unreachable)

    # Un router sin NINGUNA interfaz asignada es la firma del hub que se quedó
    # sin puertos: el IP planner solo direcciona lo que está enlazado.
    starved = [d for d in unreachable if d.category == "router" and not d.interfaces]
    if starved:
        suggestion = (
            "El router que hace de hub se quedó sin puertos libres para tantos "
            "enlaces. Usá un modelo con más puertos, agregá un módulo de "
            "expansión, o reducí la cantidad de routers."
        )
    else:
        suggestion = (
            f"Agregá un enlace que conecte {unreachable[0].name} con el resto "
            "de la topología."
        )

    return [PlanError(
        code=ErrorCode.TOPOLOGY_DISCONNECTED,
        device=unreachable[0].name,
        message=(
            f"La topología está partida: {orphan_names} no tiene(n) camino hacia "
            f"{start}. Los dispositivos aislados no pueden enrutar ni recibir tráfico."
        ),
        suggestion=suggestion,
    )]


def validate_routing(plan: TopologyPlan) -> list[PlanError]:
    """Coherencia básica de los procesos OSPF declarados.

    Un router al que el planner no le pudo asignar interfaces termina con un
    `router ospf` sin redes y con `router-id 0.0.0.0`. Las dos cosas son config
    inválida en IOS y las dos salían del pipeline sin una sola queja.
    """
    errors: list[PlanError] = []

    for cfg in plan.ospf_configs:
        if not cfg.networks:
            errors.append(PlanError(
                code=ErrorCode.OSPF_NO_NETWORKS,
                device=cfg.router,
                message=(
                    f"OSPF proceso {cfg.process_id} en {cfg.router} no anuncia "
                    "ninguna red."
                ),
                suggestion=(
                    "Un proceso OSPF sin sentencias `network` no forma "
                    "adyacencias. Verificá que el router tenga interfaces "
                    "direccionadas y enlazadas."
                ),
            ))

        # `router_id` vacío es legítimo: IOS elige la IP más alta. El que no lo
        # es es 0.0.0.0, que es lo que queda cuando no hay ninguna interfaz.
        if cfg.router_id.strip() == "0.0.0.0":
            errors.append(PlanError(
                code=ErrorCode.OSPF_INVALID_ROUTER_ID,
                device=cfg.router,
                message=(
                    f"OSPF en {cfg.router} tiene router-id 0.0.0.0, que IOS "
                    "rechaza."
                ),
                suggestion=(
                    "El router-id se deriva de las interfaces del router; "
                    "0.0.0.0 significa que no tiene ninguna direccionada."
                ),
            ))

    return errors
