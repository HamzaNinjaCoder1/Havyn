"""Auto-layout: que el canvas quede alineado, prolijo y sin dispositivos encima.

Regresiones que motivan este archivo (visto desplegando a mano y por LLM):

- `pt_add_device` usaba x=200, y=200 de default para TODO, así que dos
  dispositivos agregados sin coordenadas caían en exactamente el mismo punto
  y uno tapaba al otro.
- No existía forma de re-ordenar un canvas ya desordenado: pt_move_device
  mueve de a uno, sin noción de colisión.
"""

import pytest

from src.network_autopilot.pt import layout as al


def _pairs(plan_devices, plan_links):
    return [(d.name, d.category) for d in plan_devices], [
        (l.device_a, l.device_b) for l in plan_links
    ]


class TestFootprints:
    def test_known_category_has_icon_plus_padding(self):
        w, h = al.footprint_of("router")
        raw = al.DEVICE_FOOTPRINTS["router"]
        assert w == raw[0] + 2 * al.PAD_X
        assert h == raw[1] + 2 * al.PAD_Y

    def test_unknown_category_falls_back(self):
        w, h = al.footprint_of("totally-invented-model")
        expected = (al.FALLBACK_FOOTPRINT[0] + 2 * al.PAD_X,
                    al.FALLBACK_FOOTPRINT[1] + 2 * al.PAD_Y)
        assert (w, h) == expected

    @pytest.mark.parametrize("category", ["router", "switch", "pc", "cloud"])
    def test_every_box_is_larger_than_min_visual_gap(self, category):
        # Un dispositivo nunca puede ser más chico que el aire mínimo que
        # queremos entre iconos.
        w, h = al.footprint_of(category)
        assert w >= 60 and h >= 50


class TestCollisionModel:
    def test_identical_positions_collide(self):
        pos = {"A": (200, 200), "B": (200, 200)}
        cats = {"A": "router", "B": "router"}
        assert al.find_collisions(pos, cats) == [("A", "B")]

    def test_distant_positions_do_not_collide(self):
        pos = {"A": (100, 100), "B": (900, 700)}
        cats = {"A": "router", "B": "pc"}
        assert al.find_collisions(pos, cats) == []

    def test_close_but_not_touching_is_fine(self):
        # Dos routers a 220px de centro a centro: con padding ~130 de ancho
        # efectivo, sobra aire.
        pos = {"A": (110, 100), "B": (330, 100)}
        cats = {"A": "router", "B": "router"}
        assert al.find_collisions(pos, cats) == []

    def test_resolve_overlaps_removes_every_collision(self):
        pos = {"A": (200, 200), "B": (200, 200), "C": (200, 200)}
        cats = {"A": "router", "B": "switch", "C": "pc"}
        out = al.resolve_overlaps(pos, cats)
        assert not al.find_collisions(out, cats)

    def test_resolve_overlaps_handles_horizontal_pileup(self):
        pos = {f"D{i}": (200, 200) for i in range(6)}
        cats = {f"D{i}": "pc" for i in range(6)}
        out = al.resolve_overlaps(pos, cats)
        assert not al.find_collisions(out, cats)
        assert len(out) == 6


class TestComputeLayout:
    def test_no_overlap_for_a_real_topology(self):
        devices = [("R1", "router"), ("SW1", "switch"),
                   ("PC1", "pc"), ("PC2", "pc"), ("SRV1", "server")]
        links = [("R1", "SW1"), ("SW1", "PC1"), ("SW1", "PC2"),
                 ("SW1", "SRV1")]
        res = al.compute_layout(devices, links)
        assert not al.find_collisions(res.positions, dict(devices))

    def test_no_overlap_for_many_hosts(self):
        devices = [("R1", "router"), ("SW1", "switch")] + [
            (f"PC{i}", "pc") for i in range(1, 13)]
        links = [("R1", "SW1")] + [("SW1", f"PC{i}") for i in range(1, 13)]
        res = al.compute_layout(devices, links)
        assert not al.find_collisions(res.positions, dict(devices))

    def test_every_position_is_on_the_grid(self):
        devices = [("R1", "router"), ("R2", "router"), ("SW1", "switch"),
                   ("PC1", "pc")]
        links = [("R1", "R2"), ("R1", "SW1"), ("SW1", "PC1")]
        res = al.compute_layout(devices, links)
        for x, y in res.positions.values():
            assert x % al.GRID == 0, f"x={x} no está en la grilla"
            assert y % al.GRID == 0, f"y={y} no está en la grilla"

    def test_no_negative_or_zero_coordinates(self):
        devices = [("R1", "router"), ("SW1", "switch"),
                   ("PC1", "pc"), ("PC2", "pc")]
        links = [("R1", "SW1"), ("SW1", "PC1"), ("SW1", "PC2")]
        res = al.compute_layout(devices, links)
        for name, (x, y) in res.positions.items():
            assert x >= al.MARGIN and y >= al.MARGIN, f"{name} en ({x},{y})"

    def test_role_hierarchy_is_respected(self):
        """Routers arriba de switches, switches arriba de hosts."""
        devices = [("R1", "router"), ("SW1", "switch"),
                   ("PC1", "pc"), ("SRV1", "server")]
        links = [("R1", "SW1"), ("SW1", "PC1"), ("SW1", "SRV1")]
        res = al.compute_layout(devices, links)
        p = res.positions
        assert p["R1"][1] < p["SW1"][1] < p["PC1"][1]
        assert p["SW1"][1] < p["SRV1"][1]

    def test_access_point_sits_between_switch_and_hosts(self):
        devices = [("SW1", "switch"), ("AP1", "accesspoint"), ("LT1", "laptop")]
        links = [("SW1", "AP1")]
        res = al.compute_layout(devices, links)
        p = res.positions
        assert p["SW1"][1] < p["AP1"][1] < p["LT1"][1]

    def test_layout_is_deterministic(self):
        devices = [("R1", "router"), ("R2", "router"), ("SW1", "switch"),
                   ("PC1", "pc"), ("PC2", "pc")]
        links = [("R1", "R2"), ("R2", "SW1"), ("SW1", "PC1"), ("SW1", "PC2")]
        a = al.compute_layout(devices, links)
        b = al.compute_layout(devices, links)
        assert a.positions == b.positions

    def test_links_between_adjacent_tiers_do_not_cross(self):
        """El orden de la fila de hosts sigue al del switch que los cablea."""
        devices = [("SW1", "switch")] + [(f"PC{i}", "pc") for i in range(1, 5)]
        links = [("SW1", f"PC{i}") for i in range(1, 5)]
        res = al.compute_layout(devices, links)
        p = res.positions
        for i in range(1, 4):
            assert p[f"PC{i}"][0] < p[f"PC{i + 1}"][0]

    def test_isolated_devices_still_get_placed_without_overlap(self):
        devices = [("R1", "router"), ("PC1", "pc"), ("SRV1", "server")]
        res = al.compute_layout(devices, [])
        assert set(res.positions) == {"R1", "PC1", "SRV1"}
        assert not al.find_collisions(res.positions, dict(devices))

    def test_two_disjoint_networks_do_not_interleave(self):
        net_a = [("R1", "router"), ("SW1", "switch"), ("PC1", "pc")]
        net_b = [("R2", "router"), ("SW2", "switch"), ("PC2", "pc")]
        devices = net_a + net_b
        links = [("R1", "SW1"), ("SW1", "PC1"), ("R2", "SW2"), ("SW2", "PC2")]
        res = al.compute_layout(devices, links)
        assert not al.find_collisions(res.positions, dict(devices))
        # Cada componente queda en su bloque horizontal contiguo.
        p = res.positions
        a_xs = [p[n][0] for n, _ in net_a]
        b_xs = [p[n][0] for n, _ in net_b]
        assert max(a_xs) < min(b_xs) or max(b_xs) < min(a_xs)

    def test_unknown_category_is_placed_not_dropped(self):
        devices = [("W1", "wifi-mesh-thing"), ("R1", "router")]
        links = []
        res = al.compute_layout(devices, links)
        assert "W1" in res.positions
        assert not al.find_collisions(res.positions, dict(devices))


class TestFindFreePosition:
    def _existing(self, n, category, x, y):
        return [(n, category, float(x), float(y))]

    def test_empty_canvas_returns_wanted_spot(self):
        pos = al.find_free_position([], "router", want_x=300, want_y=200)
        assert pos == (al._snap(300), al._snap(200))

    def test_occupied_spot_moves_to_nearest_free(self):
        existing = self._existing("R1", "router", 300, 200)
        pos = al.find_free_position(existing, "router",
                                    want_x=300, want_y=200)
        cats = {"R1": "router", "NEW": "router"}
        assert not al.find_collisions(
            {"R1": (300, 200), "NEW": pos}, cats)

    def test_two_appends_without_coordinates_do_not_stack(self):
        """La regresión original: ambos caían en (200, 200)."""
        first = al.find_free_position([], "router")
        existing = [("R1", "router", float(first[0]), float(first[1]))]
        second = al.find_free_position(existing, "router")
        assert first != second
        cats = {"R1": "router", "R2": "router"}
        assert not al.find_collisions(
            {"R1": first, "R2": second}, cats)

    def test_result_never_collides_with_many_existing(self):
        existing = [(f"PC{i}", "pc", float(100 + i * al.SLOT_SPACING_X),
                     400.0) for i in range(10)]
        pos = al.find_free_position(existing, "pc")
        assert not al.find_collisions(
            {n: (x, y) for n, _, x, y in existing} | {"NEW": pos},
            {n: "pc" for n, _, _, _ in existing} | {"NEW": "pc"})
