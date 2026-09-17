"""Verification layer: multi-level, evidence-backed verdicts.

Per-criterion verification (§H): every acceptance criterion gets
its own independently traceable verification operation/evidence chain.
"""

from __future__ import annotations

from .engine import VerificationEngine, VerificationReport, VerificationTest
from .per_criterion import (
    CriterionVerification,
    PerCriterionVerifier,
    PerCriterionVerificationReport,
    VerificationKind,
)

__all__ = [
    "VerificationEngine",
    "VerificationReport",
    "VerificationTest",
    "CriterionVerification",
    "PerCriterionVerifier",
    "PerCriterionVerificationReport",
    "VerificationKind",
]
