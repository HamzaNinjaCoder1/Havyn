"""Secret redaction for evidence payloads.

Two layers, because secrets arrive in two shapes:
- structured: a dict key named like a credential ("password", "enable_secret",
  "token", ...) — the value is replaced wholesale;
- inline: IOS-style lines inside command strings ("enable secret cisco",
  "username ops password s3cret") — the secret argument is masked, the line
  itself stays useful as evidence.
"""

from __future__ import annotations

import re
from typing import Any

SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "private_key",
    "community",
    "api_key",
)

# IOS-ish credential lines: keep the command words, mask the value.
_IOS_SECRET_LINE = re.compile(
    r"^(\s*(?:enable\s+secret(?:\s+\d)?|enable\s+password|username\s+\S+\s+"
    r"(?:secret|password)(?:\s+\d)?|password)(?:\s+level\s+\d+)?\s+)(\S+)",
    re.IGNORECASE,
)

REDACTED = "***REDACTED***"


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SECRET_KEY_PARTS)


def redact_string(value: str) -> str:
    def _mask(match: re.Match[str]) -> str:
        # group(1) ends with the separating whitespace; rstrip keeps the mask
        # exactly one space after the command words.
        return f"{match.group(1).rstrip()} {REDACTED}"

    return "\n".join(
        _IOS_SECRET_LINE.sub(_mask, line) if _IOS_SECRET_LINE.match(line) else line
        for line in value.splitlines()
    ) or value


def redact(value: Any) -> Any:
    """Deep-redact a JSON-like structure. Returns a new structure."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if is_secret_key(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_string(value)
    return value
