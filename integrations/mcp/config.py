"""MCP server configuration resolved from environment and CLI flags."""

from __future__ import annotations

import os
from dataclasses import dataclass

from openusdconnect.client_id import make_stable_client_id

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7200


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class McpConfig:
    """Connection + behavior settings for one MCP server process."""

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    client_id: str = ""
    department: str = ""
    mirror_enabled: bool = True
    auto_connect: bool = True
    auto_create_ancestors: bool = True
    read_after_write_timeout_s: float = 2.0

    def __post_init__(self):
        if not self.client_id:
            self.client_id = make_stable_client_id("mcp")

    @classmethod
    def from_env(cls) -> McpConfig:
        return cls(
            host=os.environ.get("OPENUSDCONNECT_HOST", _DEFAULT_HOST),
            port=int(os.environ.get("OPENUSDCONNECT_PORT", _DEFAULT_PORT)),
            client_id=os.environ.get("OPENUSDCONNECT_CLIENT_ID", ""),
            department=os.environ.get("OPENUSDCONNECT_DEPARTMENT", ""),
            mirror_enabled=_env_bool("OPENUSDCONNECT_MIRROR", True),
            auto_connect=_env_bool("OPENUSDCONNECT_AUTO_CONNECT", True),
            auto_create_ancestors=_env_bool("OPENUSDCONNECT_AUTO_ANCESTORS", True),
        )

    def merge_args(self, args) -> McpConfig:
        """Overlay non-None argparse values onto this config."""
        for name in ("host", "port", "client_id", "department"):
            val = getattr(args, name, None)
            if val is not None:
                setattr(self, name, val)
        if getattr(args, "no_mirror", False):
            self.mirror_enabled = False
        return self
