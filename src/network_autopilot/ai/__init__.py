"""Havyn Stage 5 AI layer: intent schema, lint, and the deterministic expander.

The AI model proposes a HavynIntent; the deterministic engine owns everything
below it. See ``intent.py`` (the schema contract), ``lint.py`` (structured
contradiction/missing-info detection) and ``expander.py`` (intent -> complete
NetworkSpec through the existing IPAM/layout/compiler pipeline).
"""
