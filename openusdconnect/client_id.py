"""Stable client ID generation for DCC integrations."""

import getpass
import platform


def make_stable_client_id(dcc_name: str) -> str:
    """Generate a stable client ID from username, hostname, and DCC name.

    Format: ``{user}-{hostname}-{dcc}``

    Persists across sessions so the server can map reconnections to the
    same per-client layer.
    """
    return f"{getpass.getuser()}-{platform.node()}-{dcc_name}"
