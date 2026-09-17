"""Semantic diff layer: intended vs observed, with honest unknowns."""

from __future__ import annotations

from .engine import DiffEntry, DiffKind, DiffResult, diff_expected_vs_actual

__all__ = ["DiffEntry", "DiffKind", "DiffResult", "diff_expected_vs_actual"]
