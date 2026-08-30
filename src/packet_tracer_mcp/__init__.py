"""Packet Tracer MCP - Servidor MCP para Cisco Packet Tracer."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _dist_version

# La version se declara UNA sola vez, en `pyproject.toml`, y se lee de la
# metadata del paquete instalado. Copiarla aca como literal es la forma
# clasica de que un release salga anunciando el numero anterior.
try:
    __version__ = _dist_version("packet-tracer-mcp")
except PackageNotFoundError:  # pragma: no cover - solo corriendo sin instalar
    # Ejecutado desde el arbol de fuentes sin `pip install -e .`. No es un
    # error: el servidor arranca igual, solo que no sabe decir en que version
    # esta. Mejor un marcador honesto que una mentira plausible.
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
