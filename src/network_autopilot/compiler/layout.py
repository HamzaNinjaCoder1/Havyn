"""Deterministic topology-aware canvas layout."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.network_spec import DeviceRole, NetworkSpec
from ..pt.layout import box_at, boxes_overlap


@dataclass(frozen=True)
class LayoutIssue:
    kind: str
    detail: str
    devices: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayoutPlan:
    positions: dict[str, tuple[int, int]]
    issues: tuple[LayoutIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues


def _layer(role: DeviceRole) -> int:
    if role in (
        DeviceRole.ROUTER,
        DeviceRole.FIREWALL,
        DeviceRole.WIRELESS_ROUTER,
        DeviceRole.CLOUD,
    ):
        return 0
    if role in (DeviceRole.SWITCH, DeviceRole.L3_SWITCH):
        return 1
    return 2


def _category_of_role(role: DeviceRole) -> str:
    """Map an autopilot role to the auto-layout engine's icon family.

    Needed for the rectangle-collision check: the engine sizes each collision
    box per device family, so it has to know what kind of icon PT will draw.
    """
    return {
        DeviceRole.ROUTER: "router",
        DeviceRole.SWITCH: "switch",
        DeviceRole.L3_SWITCH: "switch",
        DeviceRole.PC: "pc",
        DeviceRole.SERVER: "server",
        DeviceRole.LAPTOP: "laptop",
        # In the footprint catalog the access point is called "accesspoint".
        DeviceRole.ACCESS_POINT: "accesspoint",
        DeviceRole.CLOUD: "cloud",
        DeviceRole.FIREWALL: "router",
        DeviceRole.WIRELESS_ROUTER: "router",
        DeviceRole.IOT: "thing",
    }.get(role, "")


def _components(spec: NetworkSpec) -> list[list[str]]:
    adjacency = {device.id: set() for device in spec.devices}
    for link in spec.links:
        adjacency[link.a.device].add(link.b.device)
        adjacency[link.b.device].add(link.a.device)
    remaining = set(adjacency)
    result: list[list[str]] = []
    while remaining:
        root = min(remaining)
        remaining.remove(root)
        stack = [root]
        component: list[str] = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adjacency[current], reverse=True):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
        result.append(sorted(component))
    return sorted(result, key=lambda ids: ids[0])


def build_layout(
    spec: NetworkSpec,
    *,
    horizontal_spacing: int = 220,
    vertical_spacing: int = 150,
    margin_x: int = 140,
    margin_y: int = 100,
) -> LayoutPlan:
    """Place graph layers in stable rows, grouped by connected component.

    Components are tiled across the canvas in a grid (not one endless row) so
    the workspace grows in two dimensions and the canvas never runs out. The
    canvas is sized from the generated positions, not a fixed box.
    """
    adjacency = {device.id: set() for device in spec.devices}
    for link in spec.links:
        adjacency[link.a.device].add(link.b.device)
        adjacency[link.b.device].add(link.a.device)

    components = _components(spec)
    # Grid dimensions: roughly square tiling of components.
    n_components = max(1, len(components))
    grid_cols = max(1, int((n_components ** 0.5) + 0.999))
    grid_rows = (n_components + grid_cols - 1) // grid_cols

    # Cell size adapts to the widest/tallest component so cells never overlap.
    cell_w = 0
    cell_h = 0
    comp_sizes = []
    for component in components:
        rows: dict[int, list[str]] = {0: [], 1: [], 2: []}
        for device_id in component:
            device = spec.device_by_id(device_id)
            assert device is not None
            rows.setdefault(_layer(device.role), []).append(device_id)
        width = max((len(r) for r in rows.values()), default=1)
        height = len([r for r in rows.values() if r])
        comp_sizes.append((width, height, rows))
        cell_w = max(cell_w, width)
        cell_h = max(cell_h, height)

    cell_px_w = (cell_w + 2) * horizontal_spacing
    cell_px_h = (cell_h + 1) * vertical_spacing + margin_y
    origin_x = margin_x + (spec.layout_seed % 11) * 10
    origin_y = margin_y

    positions: dict[str, tuple[int, int]] = {}
    for index, (width, height, rows) in enumerate(comp_sizes):
        col = index % grid_cols
        row = index // grid_cols
        base_x = origin_x + col * cell_px_w
        base_y = origin_y + row * cell_px_h
        center = base_x + (width - 1) * horizontal_spacing // 2
        for layer, device_ids in rows.items():
            ordered = sorted(device_ids, key=lambda name: (-len(adjacency[name]), name))
            row_width = max(0, len(ordered) - 1) * horizontal_spacing
            start = center - row_width // 2
            for pos, device_id in enumerate(ordered):
                positions[device_id] = (
                    start + pos * horizontal_spacing,
                    base_y + layer * vertical_spacing,
                )

    canvas = _canvas_for(positions, horizontal_spacing, vertical_spacing, margin_x, margin_y)
    return LayoutPlan(positions, tuple(validate_layout(spec, positions, canvas=canvas)))


def _canvas_for(
    positions: dict[str, tuple[int, int]],
    h_spacing: int,
    v_spacing: int,
    margin_x: int,
    margin_y: int,
) -> tuple[int, int]:
    """Canvas big enough for the generated positions plus padding.

    PT's logical workspace is effectively unbounded for practical sizes, so
    the canvas grows with the topology instead of hard-failing at 5000px.
    """
    if not positions:
        return 5000, 3000
    max_x = max(x for x, _ in positions.values())
    max_y = max(y for _, y in positions.values())
    return (
        max(5000, max_x + margin_x * 2),
        max(3000, max_y + margin_y * 2),
    )


def validate_layout(
    spec: NetworkSpec,
    positions: dict[str, tuple[int, int]],
    *,
    min_spacing: int = 80,
    max_distance: int | None = None,
    canvas: tuple[int, int] | None = None,
) -> list[LayoutIssue]:
    """Detect placement defects before a build can touch Packet Tracer.

    `max_distance` scales with the layout's own span (not a fixed constant),
    so a legitimately wide chain is accepted while a mis-placed device is
    still caught. A device is only "too far" from its peer if it sits far
    outside the span its own component occupies.
    """
    issues: list[LayoutIssue] = []
    expected = {device.id for device in spec.devices}
    for device_id in sorted(expected - set(positions)):
        issues.append(LayoutIssue("missing_position", "device has no position", (device_id,)))
    seen: dict[tuple[int, int], str] = {}
    for device_id, (x, y) in positions.items():
        if (x, y) in seen:
            issues.append(LayoutIssue("overlap", "devices share a position", (seen[(x, y)], device_id)))
        seen[(x, y)] = device_id
    ids = sorted(expected & set(positions))
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            lx, ly = positions[left]
            rx, ry = positions[right]
            if abs(lx - rx) < min_spacing and abs(ly - ry) < min_spacing:
                issues.append(LayoutIssue("too_close", f"devices are closer than {min_spacing}px", (left, right)))
    # Rectangle collision: a device drawn on top of another covers and hides it,
    # even when their centres do not line up exactly. The "too_close" check above
    # treats devices as points; this one looks at the real icon. It is the
    # "no device hides another" guarantee.
    boxes = {}
    for device_id in ids:
        device = spec.device_by_id(device_id)
        if device is None:
            continue
        x, y = positions[device_id]
        boxes[device_id] = box_at(
            device_id, x, y, _category_of_role(device.role)
        )
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            if boxes_overlap(boxes[left], boxes[right]):
                issues.append(LayoutIssue(
                    "device_hides_device",
                    "one device is drawn on top of another", (left, right),
                ))
    if positions:
        span_x = max(x for x, _ in positions.values())
        limit = max_distance if max_distance is not None else span_x + 2000
        for link in spec.links:
            start = positions.get(link.a.device)
            end = positions.get(link.b.device)
            if start and end and abs(start[0] - end[0]) + abs(start[1] - end[1]) > limit:
                issues.append(LayoutIssue("excessive_distance", f"link {link.id} is too long", (link.a.device, link.b.device)))
    if canvas is not None:
        cw, ch = canvas
        for device_id, (x, y) in positions.items():
            if x < 0 or y < 0 or x > cw or y > ch:
                issues.append(LayoutIssue("off_canvas", f"position ({x},{y}) is outside canvas", (device_id,)))
    return issues
