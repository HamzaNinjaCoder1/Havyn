"""Hugging Face authentication helpers for Colab and local runs.

Rules:
  - the token is read from HF_TOKEN or a Colab secret named HF_TOKEN;
  - it is never printed, logged, written to a file, or embedded in a command;
  - a missing token produces one clear instruction, not a stack trace.

Usage:
    from hf_auth import get_token, status
    token = get_token(required=True)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

TOKEN_ENV = "HF_TOKEN"
USERNAME_ENV = "HF_USERNAME"

MISSING_TOKEN_MESSAGE = (
    "HF_TOKEN is not set.\n"
    "Colab: open the key icon in the left sidebar -> Secrets -> add a secret\n"
    "named HF_TOKEN with your Hugging Face read token, then enable notebook\n"
    "access for it. Locally: export HF_TOKEN=... in your shell (never commit it).\n"
    "Optional: set HF_USERNAME for the dataset/model repository owner.\n"
    "The token is never printed by this tooling."
)


@dataclass(frozen=True)
class AuthStatus:
    present: bool
    source: str | None
    username: str | None

    def to_dict(self) -> dict:
        # Deliberately contains no token, prefix, length or hash.
        return {
            "present": self.present,
            "source": self.source,
            "username": self.username,
            "token": "hidden" if self.present else None,
        }


def _colab_secret(name: str) -> str | None:
    try:
        from google.colab import userdata  # type: ignore[import-not-found]

        value = userdata.get(name)
        return str(value) if value else None
    except Exception:
        return None


def get_token(required: bool = False) -> str | None:
    token = os.environ.get(TOKEN_ENV)
    if token:
        return token
    token = _colab_secret(TOKEN_ENV)
    if token:
        # Export it so transformers/huggingface_hub pick it up, without ever
        # writing it anywhere persistent.
        os.environ[TOKEN_ENV] = token
        return token
    if required:
        raise SystemExit(MISSING_TOKEN_MESSAGE)
    return None


def status() -> AuthStatus:
    env_token = os.environ.get(TOKEN_ENV)
    if env_token:
        return AuthStatus(True, "environment", os.environ.get(USERNAME_ENV))
    if _colab_secret(TOKEN_ENV):
        return AuthStatus(True, "colab_secret", os.environ.get(USERNAME_ENV))
    return AuthStatus(False, None, os.environ.get(USERNAME_ENV))


def whoami(token: str | None = None) -> dict:
    """Validate the token against the Hub. Network call; never echoes it."""
    from huggingface_hub import HfApi

    token = token or get_token(required=True)
    info = HfApi(token=token).whoami()
    return {
        "name": info.get("name"),
        "type": info.get("type"),
        "orgs": [org.get("name") for org in info.get("orgs", [])],
    }


if __name__ == "__main__":
    import json

    print(json.dumps(status().to_dict(), indent=2))
