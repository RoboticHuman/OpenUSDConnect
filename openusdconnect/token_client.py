"""Client-side TOFU token persistence.

Stores and retrieves authentication tokens in ~/.openusdconnect/tokens.json,
keyed by server host:port.
"""

from __future__ import annotations

import json
import logging
import os

LOG = logging.getLogger(__name__)

_TOKEN_DIR = os.path.join(os.path.expanduser("~"), ".openusdconnect")
_TOKEN_FILE = os.path.join(_TOKEN_DIR, "tokens.json")


def load_token(host: str, port: int) -> str | None:
    """Load a saved token for a server, or None if not found."""
    key = f"{host}:{port}"
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as f:
            tokens = json.load(f)
        return tokens.get(key)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def save_token(host: str, port: int, token: str) -> None:
    """Save a token for a server."""
    key = f"{host}:{port}"
    os.makedirs(_TOKEN_DIR, exist_ok=True)
    tokens = {}
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as f:
            tokens = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    tokens[key] = token
    with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    LOG.info("Saved token for %s", key)


def delete_token(host: str, port: int) -> None:
    """Delete a saved token (e.g. after revocation)."""
    key = f"{host}:{port}"
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as f:
            tokens = json.load(f)
        if key in tokens:
            del tokens[key]
            with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump(tokens, f, indent=2)
            LOG.info("Deleted token for %s", key)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
