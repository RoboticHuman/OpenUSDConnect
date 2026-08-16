"""Stable client ID generation for DCC integrations."""

import getpass
import os
import platform


def make_stable_client_id(dcc_name: str) -> str:
    """Generate a stable client ID from username, hostname, and DCC name.

    Format: ``{user}-{hostname}-{dcc}``. It persists across sessions for
    authentication, producer replay, and collaboration attribution. The
    ``OPENUSDCONNECT_CLIENT_ID`` environment variable overrides the computed
    value when two DCC sessions on one machine need distinct client IDs. The
    older ``USD_CONNECT_CLIENT_ID`` spelling remains supported.
    """
    override = (
        os.environ.get("OPENUSDCONNECT_CLIENT_ID", "")
        or os.environ.get("USD_CONNECT_CLIENT_ID", "")
    ).strip()
    if override:
        return override
    return f"{getpass.getuser()}-{platform.node()}-{dcc_name}"
