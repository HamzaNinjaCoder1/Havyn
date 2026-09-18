"""Deterministic topology-aware canvas layout."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.network_spec import DeviceRole, NetworkSpec


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
    if role in (DeviceRole.ROUTER, DeviceRole.CLOUD):
        return 0
    if role in (DeviceRole.SWITCH, DeviceRole.L3_SWITCH):
        return 1
    return 2


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
    """Place graph layers in stable rows, grouped by connected component."""
    adjacency = {device.id: set() for device in spec.devices}
    for link in spec.links:
        adjacency[link.a.device].add(link.b.device)
        adjacency[link.b.device].add(link.a.device)

    positions: dict[str, tuple[int, int]] = {}
    # The seed is an explicit part of NetworkSpec identity. It provides a
    # small deterministic canvas offset while graph structure still controls
    # hierarchy, grouping, and spacing.
    origin_x = margin_x + (spec.layout_seed % 11) * 10
    for component in _components(spec):
        rows: dict[int, list[str]] = {0: [], 1: [], 2: []}
        for device_id in component:
            device = spec.device_by_id(device_id)
            assert device is not None
            rows.setdefault(_layer(device.role), []).append(device_id)
        width = max((len(row) for row in rows.values()), default=1)
        center = origin_x + (width - 1) * horizontal_spacing // 2
        for row, device_ids in rows.items():
            ordered = sorted(device_ids, key=lambda name: (-len(adjacency[name]), name))
            row_width = max(0, len(ordered) - 1) * horizontal_spacing
            start = center - row_width // 2
            for index, device_id in enumerate(ordered):
                positions[device_id] = (
                    start + index * horizontal_spacing,
                    margin_y + row * vertical_spacing,
                )
        origin_x += width * horizontal_spacing + horizontal_spacing * 2
    return LayoutPlan(positions, tuple(validate_layout(spec, positions)))


def validate_layout(
    spec: NetworkSpec,
    positions: dict[str, tuple[int, int]],
    *,
    min_spacing: int = 80,
    max_distance: int = 1200,
    canvas_width: int = 5000,
    canvas_height: int = 3000,
) -> list[LayoutIssue]:
    """Detect placement defects before a build can touch Packet Tracer."""
    issues: list[LayoutIssue] = []
    expected = {device.id for device in spec.devices}
    for device_id in sorted(expected - set(positions)):
        issues.append(LayoutIssue("missing_position", "device has no position", (device_id,)))
    seen: dict[tuple[int, int], str] = {}
    for device_id, (x, y) in positions.items():
        if x < 0 or y < 0 or x > canvas_width or y > canvas_height:
            issues.append(LayoutIssue("off_canvas", f"position ({x},{y}) is outside canvas", (device_id,)))
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
    for link in spec.links:
        start = positions.get(link.a.device)
        end = positions.get(link.b.device)
        if start and end and abs(start[0] - end[0]) + abs(start[1] - end[1]) > max_distance:
            issues.append(LayoutIssue("excessive_distance", f"link {link.id} is too long", (link.a.device, link.b.device)))
    return issues
