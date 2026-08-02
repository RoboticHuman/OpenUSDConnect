"""Shared user-facing defaults for OpenUSDConnect tools and integrations."""

from __future__ import annotations

from urllib.parse import quote

DEFAULT_HOST = "127.0.0.1"
DEFAULT_SYNC_PORT = 7200
DEFAULT_VFS_PORT = 7280

DEFAULT_VFS_SHARE = "usd"
DEFAULT_VFS_NAME = "scene.usd"
DEFAULT_VFS_LAYER_DIR = "_layers"
DEFAULT_VFS_MANIFEST_NAME = "openusdconnect.json"

DEFAULT_EVENT_LOG = "usd_events.db"
DEFAULT_LIVE_OPEN_ROOT = ".ouc_live_mount"
DEFAULT_MIRROR_DIR = f"{DEFAULT_LIVE_OPEN_ROOT}/usd"
DEFAULT_SESSION_STATE_FILE = f"{DEFAULT_LIVE_OPEN_ROOT}/live_open_session.json"
DEFAULT_BRIDGE_STATUS_FILE = (
    f"{DEFAULT_LIVE_OPEN_ROOT}/bridge/openusdconnect_bridge_status.json"
)

DEFAULT_BRIDGE_POLL_INTERVAL = 1.0
DEFAULT_BRIDGE_SETTLE_TIME = 0.5
DEFAULT_STARTUP_TIMEOUT = 20.0

VFS_WRITE_MODES = ("forbid", "drop", "translate")


def host_for_url(host: str) -> str:
    """Return a URL-safe host, adding brackets around bare IPv6 addresses."""

    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def advertise_host_for_bind(host: str) -> str:
    """Choose a reachable local metadata host for a bind interface."""

    return DEFAULT_HOST if host in ("", "0.0.0.0", "::") else host


def vfs_url(host: str, port: int, share: str, name: str) -> str:
    """Build the URL for one file in an OpenUSDConnect VFS share."""

    encoded_share = quote(share.strip("/"), safe="")
    encoded_name = quote(name.strip("/"), safe="/")
    return f"http://{host_for_url(host)}:{port}/{encoded_share}/{encoded_name}"
