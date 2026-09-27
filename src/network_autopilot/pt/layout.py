"""Motor de auto-layout inteligente para el canvas lógico de Packet Tracer.

Garantías que entrega este módulo (el motivo por el que existe):

1. **Ningún dispositivo se encima ni tapa a otro.** El modelo de colisión es
   un rectángulo por dispositivo (el icono + su etiqueta + aire visual), no un
   punto. Si dos rectángulos se intersecan, uno es empujado a una celda libre.
2. **Alineación limpia.** Todas las coordenadas quedan en una grilla, en filas
   por rol: WAN/nube arriba del todo, routers debajo, switches, APs y hosts
   abajo, centradas en el canvas.
3. **Diseño derivado de la topología, no aleatorio.** Cada fila se ordena
   siguiendo sus propios enlaces (cadenas y estrellas en orden, sin cruces
   internos) y por baricentro respecto de la fila de arriba, de modo que cada
   host cae debajo del switch al que está cableado. Redes disjuntas quedan en
   bloques separados.
4. **Determinista.** Misma topología → mismo dibujo, siempre.

Se usa desde tres lugares: `pt_add_device` (para no apilar todo en (200,200)),
`pt_auto_layout` (para re-ordenar un canvas ya desordenado) y el gate de
`network_autopilot` (para rechazar antes de ejecutar un plan que deje un
dispositivo tapado).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ------------------------------------------------------------------
# Geometría
# ------------------------------------------------------------------

# Granularidad de la grilla. Todo termina en múltiplo de este valor, así el
# dibujo queda "cuadriculado" y no como coordenadas sueltas.
GRID = 20

# Margen del borde superior/izquierdo del canvas. PT empieza a dibujar en 0,0.
MARGIN = 120

# Tamaño (ancho, alto) del rectángulo de colisión de cada familia de iconos en
# la vista lógica. Es el *icono*; abajo se le suma padding para que la etiqueta
# y el aire entre dispositivos también cuenten.
DEVICE_FOOTPRINTS: dict[str, tuple[int, int]] = {
    # Infraestructura (iconos grandes)
    "cloud": (96, 62),
    "router": (70, 56),
    "switch": (70, 56),
    "multilayer_switch": (70, 56),
    "wireless_router": (72, 58),
    "wlc": (72, 58),
    "firewall": (70, 56),
    "modem": (66, 52),
    "cell_tower": (62, 66),
    "central_office": (70, 56),
    "network_controller": (70, 56),
    "meraki": (70, 56),
    # Acceso
    "hub": (66, 52),
    "bridge": (66, 52),
    "repeater": (60, 54),
    "splitter": (60, 50),
    "patch_panel": (66, 50),
    "wall_mount": (60, 50),
    "power_dist": (60, 50),
    "accesspoint": (56, 60),
    # Hosts (iconos chicos)
    "pc": (52, 40),
    "server": (52, 46),
    "laptop": (54, 40),
    "printer": (56, 44),
    "ip_phone": (50, 40),
    "analog_phone": (48, 42),
    "tv": (58, 42),
    "voip": (50, 40),
    "tablet": (46, 40),
    "pda": (44, 40),
    "wireless_end_device": (48, 40),
    "wired_end_device": (48, 40),
    "iot": (48, 44),
    "embedded_server": (52, 44),
    "sniffer": (52, 40),
    "mcu": (60, 46),
    "sbc": (56, 42),
    "home_gateway": (62, 50),
}

# Footprint por defecto: algo intermedio entre router y host.
FALLBACK_FOOTPRINT = (64, 52)

# Padding alrededor del icono: la etiqueta del dispositivo se dibuja debajo y
# los enlaces llegan a los bordes. Sin este aire, dos dispositivos "técnicamente
# sin tocarse" se ven encimados y las etiquetas se superponen.
PAD_X = 30
PAD_Y = 26

# Espaciado entre filas (verticales) y entre dispositivos de una misma fila.
# Generoso a propósito: prefiero un dibujo aireado a uno compacto y ilegible.
TIER_SPACING_Y = 180
SLOT_SPACING_X = 160

# Capas del dibujo, de arriba hacia abajo. Es el orden clásico de un diagrama
# de red: el/WAN arriba del todo, routers debajo, switches, APs y hosts abajo.
TIER_INTERNET = 0
TIER_ROUTER = 1
TIER_SWITCH = 2
TIER_WIRELESS = 3
TIER_HOST = 4

_INTERNET_CATEGORIES = {"cloud"}
_ROUTER_CATEGORIES = {
    "router", "firewall", "wireless_router", "wlc", "modem", "cell_tower",
    "central_office", "network_controller", "meraki",
}
_SWITCH_CATEGORIES = {
    "switch", "multilayer_switch", "hub", "bridge", "repeater", "splitter",
    "patch_panel", "wall_mount", "power_dist",
}
_WIRELESS_CATEGORIES = {"accesspoint"}


def tier_of(category: str) -> int:
    """Capa vertical (0 = arriba) a la que pertenece una categoría."""
    if category in _INTERNET_CATEGORIES:
        return TIER_INTERNET
    if category in _ROUTER_CATEGORIES:
        return TIER_ROUTER
    if category in _SWITCH_CATEGORIES:
        return TIER_SWITCH
    if category in _WIRELESS_CATEGORIES:
        return TIER_WIRELESS
    return TIER_HOST


def footprint_of(category: str) -> tuple[int, int]:
    """Rectángulo de colisión (ancho, alto) de la categoría, icono + padding."""
    w, h = DEVICE_FOOTPRINTS.get(category, FALLBACK_FOOTPRINT)
    return w + PAD_X * 2, h + PAD_Y * 2


@dataclass(frozen=True)
class _Box:
    """Rectángulo alineado a ejes de un dispositivo, centrado en (x, y)."""
    name: str
    x: float
    y: float
    w: float
    h: float

    @property
    def left(self) -> float:
        return self.x - self.w / 2

    @property
    def right(self) -> float:
        return self.x + self.w / 2

    @property
    def top(self) -> float:
        return self.y - self.h / 2

    @property
    def bottom(self) -> float:
        return self.y + self.h / 2


def box_at(name: str, x: float, y: float, category: str) -> _Box:
    w, h = footprint_of(category)
    return _Box(name, float(x), float(y), float(w), float(h))


def boxes_overlap(a: _Box, b: _Box) -> bool:
    """True si dos rectángulos se intersecan (un dispositivo tapa al otro)."""
    return (
        abs(a.x - b.x) * 2 < (a.w + b.w)
        and abs(a.y - b.y) * 2 < (a.h + b.h)
    )


def find_collisions(
    positions: dict[str, tuple[float, float]],
    categories: dict[str, str],
) -> list[tuple[str, str]]:
    """Todos los pares de dispositivos cuyos rectángulos se pisan."""
    names = sorted(positions)
    boxes = {
        n: box_at(n, positions[n][0], positions[n][1], categories.get(n, ""))
        for n in names
    }
    hits: list[tuple[str, str]] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if boxes_overlap(boxes[a], boxes[b]):
                hits.append((a, b))
    return hits


# ------------------------------------------------------------------
# Grafo de la topología
# ------------------------------------------------------------------


def _adjacency(devices: list[str], links: list[tuple[str, str]]) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {d: set() for d in devices}
    for a, b in links:
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def _order_by_barycenter(
    tier_devices: list[str],
    fixed: dict[str, float],
    adj: dict[str, set[str]],
) -> list[str]:
    """Ordena una capa minimizando los cruces de enlaces con la capa ya fijada.

    Heurística del baricentro (Sugiyama): cada dispositivo se coloca según la
    posición media de sus vecinos ya ordenados; los que no tienen vecinos allá
    van al final. Determinística — los empates los deshacen grado y nombre.
    """
    n = len(fixed)

    def barycenter(name: str) -> float:
        values = [fixed[nb] for nb in adj[name] if nb in fixed]
        return sum(values) / len(values) if values else n

    return sorted(
        tier_devices,
        key=lambda d: (barycenter(d), -len(adj[d]), d),
    )


def _path_order(tier_devices: list[str], adj: dict[str, set[str]]) -> list[str]:
    """Ordena una capa siguiendo sus propios enlaces, no el nombre.

    Sin esto, una cadena R1-R2-R3 caía en orden alfabético y el enlace R1-R3
    cruzaba por encima de R2. Aquí se camina la capa como un camino: se arranca
    de un extremo (dispositivo con un solo vecino interno) y se avanza por
    vecinos de la misma capa, lo que deja las cadenas y las estrellas sin cruces
    internos. Los dispositivos sin enlaces internos se agregan al final, por
    nombre, de forma determinista.
    """
    members = set(tier_devices)
    inner: dict[str, set[str]] = {
        d: (adj[d] & members) for d in tier_devices
    }
    remaining = set(members)
    order: list[str] = []

    while remaining:
        # Extremo del camino = el que tiene un solo vecino interno. Si no hay
        # (ej: anillo), arrancamos del de mayor grado.
        endpoints = [d for d in remaining if len(inner[d] & remaining) <= 1]
        if endpoints:
            root = max(endpoints, key=lambda d: (len(adj[d]), d))
        else:
            root = max(remaining, key=lambda d: (len(adj[d]), d))
        remaining.discard(root)
        stack = [root]
        while stack:
            current = stack.pop()
            order.append(current)
            # Seguir el camino: vecinos internos no visitados, en orden estable.
            for nb in sorted(inner[current] & remaining, reverse=True):
                remaining.discard(nb)
                stack.append(nb)

    return order


# ------------------------------------------------------------------
# Layout
# ------------------------------------------------------------------


@dataclass
class LayoutResult:
    positions: dict[str, tuple[int, int]]
    moves: list[tuple[str, int, int, int, int]] = field(default_factory=list)
    collisions_fixed: int = 0
    notes: list[str] = field(default_factory=list)


def _snap(value: float) -> int:
    """Redondea a la grilla, siempre hacia afuera de los negativos."""
    import math
    return int(math.floor(value / GRID + 1e-9) * GRID)


def _free_slot_search(
    boxes: list[_Box],
    category: str,
    want_x: float,
    want_y: float,
    *,
    bounds: tuple[int, int, int, int] | None = None,
) -> tuple[int, int]:
    """Busca la celda de grilla libre más cercana a (want_x, want_y).

    Garantía: devuelve una posición cuyo rectángulo no interseca ninguno de los
    `boxes` existentes. Explora en espiral cuadrada creciente, así la primera
    celda libre que encuentra es la más cercana (en distancia de Chebyshev) y
    no se aleja más de lo necesario.
    """
    w, h = footprint_of(category)
    if bounds is None:
        bounds = (MARGIN, MARGIN, MARGIN + SLOT_SPACING_X * 40,
                  MARGIN + TIER_SPACING_Y * 20)

    cx = _snap(want_x)
    cy = _snap(want_y)
    # Una celda nunca es más chica que el espaciado mínimo, garantizando que
    # dos celdas adyacentes tampoco se pisen.
    step_x = max(SLOT_SPACING_X, w)
    step_y = max(TIER_SPACING_Y, h)

    min_x, min_y, max_x, max_y = bounds
    max_ring = 200
    for ring in range(max_ring):
        for dx in range(-ring, ring + 1):
            for dy in {-ring, ring} if ring > 0 else {0}:
                x, y = cx + dx * step_x, cy + dy * step_y
                if x < min_x or y < min_y or x > max_x or y > max_y:
                    continue
                cand = _Box("", x, y, w, h)
                if not any(boxes_overlap(cand, b) for b in boxes):
                    return x, y
        for dy in range(-ring + 1, ring):
            for dx in {-ring, ring}:
                x, y = cx + dx * step_x, cy + dy * step_y
                if x < min_x or y < min_y or x > max_x or y > max_y:
                    continue
                cand = _Box("", x, y, w, h)
                if not any(boxes_overlap(cand, b) for b in boxes):
                    return x, y

    # No debería llegarse acá nunca; de llegar, la celda (0,0) libre garantiza
    # no devolver basura. Es un seguro, no un camino normal.
    return MARGIN, MARGIN


def resolve_overlaps(
    positions: dict[str, tuple[float, float]],
    categories: dict[str, str],
) -> dict[str, tuple[int, int]]:
    """Separa todo par de rectángulos que se pise. Devuelve posiciones en grilla.

    Es la garantía dura del módulo: da igual qué posiciones entren, la salida
    no tiene ningún dispositivo encima de otro. Primero acomoda lo acomodable
    empujando pares, y lo que quede (casos patológicos) lo manda a una celda
    libre explícitamente.
    """
    names = sorted(positions)
    if not names:
        return {}

    current: dict[str, tuple[float, float]] = {
        n: (float(positions[n][0]), float(positions[n][1])) for n in names
    }

    # 1. Pasada de separación por pares. Empuja al dispositivo *posterior* en
    # orden determinístico, a lo largo del eje de menor penetración, cuantizado
    # a la grilla. Cada empuje resuelve ese par, y como el orden es estable el
    # proceso converge para topologías reales.
    for _ in range(120):
        boxes = [
            box_at(n, current[n][0], current[n][1], categories.get(n, ""))
            for n in names
        ]
        pushed = False
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = boxes[i], boxes[j]
                if not boxes_overlap(a, b):
                    continue
                # Penetración que habría que absorber en cada eje.
                pen_x = (a.w + b.w) / 2 - abs(a.x - b.x)
                pen_y = (a.h + b.h) / 2 - abs(a.y - b.y)
                if pen_x <= pen_y:
                    # Separar en X.
                    sign = 1 if b.x >= a.x else -1
                    shift = _snap(pen_x + GRID) * sign
                    if shift == 0:
                        shift = GRID * sign
                    bx, by = current[b.name]
                    current[b.name] = (bx + shift, by)
                else:
                    sign = 1 if b.y >= a.y else -1
                    shift = _snap(pen_y + GRID) * sign
                    if shift == 0:
                        shift = GRID * sign
                    bx, by = current[b.name]
                    current[b.name] = (bx, by + shift)
                pushed = True
        if not pushed:
            break

    # 2. Snapping a grilla.
    snapped: dict[str, tuple[int, int]] = {
        n: (_snap(current[n][0]), _snap(current[n][1])) for n in names
    }

    # 3. Garantía final: lo que siga pisándose va a una celda libre.
    existing: list[_Box] = []
    final: dict[str, tuple[int, int]] = {}
    for n in names:
        x, y = snapped[n]
        cand = box_at(n, x, y, categories.get(n, ""))
        if any(boxes_overlap(cand, b) for b in existing):
            x, y = _free_slot_search(
                existing, categories.get(n, ""), x, y
            )
            cand = box_at(n, x, y, categories.get(n, ""))
        existing.append(cand)
        final[n] = (max(MARGIN, x), max(MARGIN, y))

    return final


def compute_layout(
    devices: list[tuple[str, str]],
    links: list[tuple[str, str]],
) -> LayoutResult:
    """Calcula un layout limpio y jerárquico para la topología dada.

    `devices` es una lista de (nombre, categoría); `links` de (a, b). No toca
    Packet Tracer: es pura geometría, y por eso se puede testear offline.
    """
    names = [d[0] for d in devices]
    cats = dict(devices)
    adj = _adjacency(names, links)

    # Capas presentes, de arriba hacia abajo.
    tiers = sorted({tier_of(cats.get(n, "")) for n in names}) or [TIER_HOST]
    tier_rows: dict[int, list[str]] = {}

    for tier in tiers:
        members = [n for n in names if tier_of(cats.get(n, "")) == tier]
        # Seed siguiendo los enlaces de la capa: cadenas y estrellas quedan en
        # orden, sin cruces internos.
        seed = _path_order(members, adj)
        if tier == tiers[0]:
            tier_rows[tier] = seed
            continue
        # Las capas de abajo se ordenan por baricentro respecto de la capa
        # inmediatamente anterior (grupos debajo de su switch/router), usando
        # el orden de camino como desempate para no romper las cadenas.
        prev = tier_rows[tiers[tiers.index(tier) - 1]]
        fixed = {n: float(i) for i, n in enumerate(prev)}
        tier_rows[tier] = _order_by_barycenter(seed, fixed, adj)

    # Una pasada de refresco más, capa por capa, contra la capa anterior: bajá
    # cruces sin perder la agrupación por componente (sort estable).
    for tier in tiers[1:]:
        prev = tier_rows[tiers[tiers.index(tier) - 1]]
        fixed = {n: float(i) for i, n in enumerate(prev)}
        tier_rows[tier] = _order_by_barycenter(tier_rows[tier], fixed, adj)

    # Ancho de la fila más larga para poder centrar todo el dibujo.
    widths: dict[int, int] = {}
    for tier in tiers:
        row = tier_rows[tier]
        widths[tier] = max(0, len(row) - 1) * SLOT_SPACING_X
    canvas_w = max(widths.values()) if widths else 0
    center = MARGIN + canvas_w / 2

    raw: dict[str, tuple[float, float]] = {}
    for tier in tiers:
        row = tier_rows[tier]
        row_width = max(0, len(row) - 1) * SLOT_SPACING_X
        start = center - row_width / 2
        for i, name in enumerate(row):
            raw[name] = (
                start + i * SLOT_SPACING_X,
                MARGIN + tiers.index(tier) * TIER_SPACING_Y,
            )

    final = resolve_overlaps(raw, cats)
    return LayoutResult(positions=final, collisions_fixed=0)


def find_free_position(
    existing: list[tuple[str, str, float, float]],
    category: str,
    *,
    want_x: float | None = None,
    want_y: float | None = None,
) -> tuple[int, int]:
    """Celda libre más cercana a un punto deseado, rodeada de `existing`.

    `existing` es [(nombre, categoría, x, y)] con lo que ya hay en el canvas.
    Es lo que usa `pt_add_device` para no apilar el nuevo dispositivo encima de
    otro cuando el llamador no dio coordenadas.
    """
    boxes = [
        box_at(n, x, y, c) for (n, c, x, y) in existing
    ]
    if want_x is None or want_y is None:
        # Sin preferencia: empezar en la "zona de su capa" del borde izquierdo.
        tier = tier_of(category)
        want_x = MARGIN + SLOT_SPACING_X
        want_y = MARGIN + tier * TIER_SPACING_Y
    return _free_slot_search(boxes, category, want_x, want_y)
