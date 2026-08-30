"""El catalogo tiene que declarar los puertos REALES de la Cloud-PT.

Medido contra PT 9.0.1 creando una nube y leyendo `d.getPorts()`:

    nports=8 ports=Serial0,Serial1,Serial2,Serial3,Modem4,Modem5,Ethernet6,Coaxial7

El catalogo declaraba UNO solo (`Ethernet6`), asi que cualquier enlace a los
otros siete lo rechazaba `pt_add_link` y lo marcaba invalido `validate_plan`
antes de llegar a PT -- puertos que el dispositivo si tiene.

Ademas `Ethernet6` estaba declarado con velocidad FastEthernet y el nombre
forzado a mano. Funcionaba de casualidad: el nombre correcto salia del override,
no de la velocidad.
"""

import pytest

from src.packet_tracer_mcp.infrastructure.catalog.devices import (
    resolve_model, get_valid_ports, get_ports_by_speed,
)
from src.packet_tracer_mcp.domain.models.requests import TopologyRequest
from src.packet_tracer_mcp.domain.services.orchestrator import plan_from_request
from src.packet_tracer_mcp.shared.enums import PortSpeed

# Lo que PT 9.0.1 reporta, en su orden.
REAL_PORTS = [
    "Serial0", "Serial1", "Serial2", "Serial3",
    "Modem4", "Modem5", "Ethernet6", "Coaxial7",
]


class TestCloudPortInventory:
    def test_cloud_declares_every_real_port(self):
        model = resolve_model("Cloud-PT")
        assert model is not None
        assert [p.full_name for p in model.ports] == REAL_PORTS

    def test_valid_ports_accepts_the_serial_ports(self):
        """Eran rechazados aunque el dispositivo los tiene."""
        valid = get_valid_ports("Cloud-PT")
        for port in ("Serial0", "Serial1", "Serial2", "Serial3"):
            assert port in valid

    def test_valid_ports_accepts_modem_and_coaxial(self):
        valid = get_valid_ports("Cloud-PT")
        assert "Modem4" in valid
        assert "Modem5" in valid
        assert "Coaxial7" in valid


class TestPortSpeedsAreHonest:
    def test_ethernet6_is_declared_as_ethernet(self):
        """Estaba como FastEthernet con el nombre forzado a mano."""
        model = resolve_model("Cloud-PT")
        eth = [p for p in model.ports if p.full_name == "Ethernet6"]
        assert len(eth) == 1
        assert eth[0].speed == PortSpeed.ETHERNET

    def test_serial_ports_are_declared_as_serial(self):
        model = resolve_model("Cloud-PT")
        serials = get_ports_by_speed(model, PortSpeed.SERIAL)
        assert [p.full_name for p in serials] == [
            "Serial0", "Serial1", "Serial2", "Serial3"
        ]

    def test_names_come_from_the_speed_not_from_an_override(self):
        """Si el nombre sale solo de la velocidad + slot, no hay que mantenerlo a mano."""
        model = resolve_model("Cloud-PT")
        for port in model.ports:
            speed = port.speed.value if hasattr(port.speed, "value") else port.speed
            assert port.full_name == f"{speed}{port.slot}"


class TestWanLinkStillWorks:
    """Guard: el orquestador buscaba el puerto de la nube por FAST_ETHERNET.

    Al declarar Ethernet6 con su velocidad real esa busqueda deja de encontrarlo,
    asi que el enlace router-nube desapareceria en silencio -- exactamente el tipo
    de fallo mudo que esta rama viene arreglando.
    """

    def _wan_plan(self):
        req = TopologyRequest(routers=2, switches_per_router=1, pcs_per_lan=1,
                              has_wan=True)
        return plan_from_request(req)

    def test_router_is_linked_to_the_cloud(self):
        plan, _ = self._wan_plan()
        cloud = next(d for d in plan.devices if d.category == "cloud")
        links = [l for l in plan.links
                 if cloud.name in (l.device_a, l.device_b)]
        assert len(links) == 1, f"esperaba 1 enlace a la nube, hay {len(links)}"

    def test_the_cloud_side_lands_on_ethernet6(self):
        plan, _ = self._wan_plan()
        cloud = next(d for d in plan.devices if d.category == "cloud")
        link = next(l for l in plan.links if cloud.name in (l.device_a, l.device_b))
        port = link.port_b if link.device_b == cloud.name else link.port_a
        assert port == "Ethernet6"

    def test_wan_plan_stays_valid(self):
        _, result = self._wan_plan()
        assert result.is_valid, [str(e) for e in result.errors]
