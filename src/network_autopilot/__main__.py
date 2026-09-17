"""`python -m network_autopilot` — start the Autopilot MCP server (stdio)."""

from __future__ import annotations

from .mcp.server import main

if __name__ == "__main__":
    main()
