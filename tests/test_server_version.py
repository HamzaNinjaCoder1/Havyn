"""El handshake tiene que anunciar la version del servidor, no la del SDK.

`Server.create_initialization_options()` resuelve el numero asi:

    server_version=self.version if self.version else pkg_version("mcp")

FastMCP nunca le pasa `version` al server lowlevel -- no existe el parametro en
su `__init__` --, asi que sin el fix el fallback gana y el servidor se presenta
con la version de la libreria `mcp`. Medido contra PT 9.0.1 con un handshake
real por stdio, el servidor contestaba:

    {"name": "Packet Tracer MCP", "version": "1.28.1"}

1.28.1 es el SDK. El servidor estaba en 0.8.0. Todo cliente MCP muestra ese
numero en su panel, y ademas se mueve solo cuando se actualiza la dependencia:
un usuario reportando "estoy en la 1.28.1" no dice nada util sobre que codigo
esta corriendo.

No requieren Packet Tracer.
"""

import tomllib
from importlib.metadata import version as dist_version
from pathlib import Path

from src.packet_tracer_mcp import __version__
from src.packet_tracer_mcp.server import mcp

RAIZ = Path(__file__).resolve().parents[1]


def _opciones():
    """Lo mismo que el servidor le contesta a un cliente en `initialize`."""
    return mcp._mcp_server.create_initialization_options()


def test_el_handshake_anuncia_nuestra_version():
    assert _opciones().server_version == __version__


def test_el_handshake_no_anuncia_la_version_del_sdk():
    # El guard de la regresion: si alguien saca la linea que fija la version,
    # este numero vuelve a ser el del paquete `mcp` y el test cae.
    assert _opciones().server_version != dist_version("mcp")


def test_el_nombre_sigue_siendo_el_del_servidor():
    # Fijar la version no tiene por que tocar el nombre, que es lo que el
    # usuario ve en la lista de servidores de su cliente.
    assert _opciones().server_name == "Packet Tracer MCP"


def test_la_version_del_paquete_coincide_con_pyproject():
    # Una sola fuente de verdad: `__version__` sale de la metadata instalada,
    # que a su vez sale de `pyproject.toml`. Si se despegan, es que el paquete
    # instalado quedo viejo y todo lo de arriba mide otra cosa.
    declarada = tomllib.loads(
        (RAIZ / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    assert __version__ == declarada
