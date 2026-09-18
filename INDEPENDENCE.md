# Project Independence

This repository is the independent home of the Packet Tracer automation
engine. Its runtime uses only code packaged in this repository plus declared
Python dependencies. It does not clone, download, or import code from a
remote Git repository at runtime.

The former Git remote was removed from `.git/config` on September 18, 2026.
The low-level `packet_tracer_mcp` package and the Packet Tracer extension are
retained as local source because they are part of this checkout. Their
third-party attribution remains documented in `PROVENANCE.md` and
`THIRD_PARTY_NOTICES.md`.

The project-owned live adapter is
`network_autopilot.adapter.packet_tracer`. The former
`network_autopilot.adapter.upstream` path is compatibility-only and is not
used by the application pipeline.
