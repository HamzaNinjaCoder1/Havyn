"""Acceptance evaluation: spec vs. observed state, UNKNOWN never passes."""

from __future__ import annotations

from .evaluator import evaluate_criterion, evaluate_spec, resolve_device

__all__ = ["evaluate_criterion", "evaluate_spec", "resolve_device"]
