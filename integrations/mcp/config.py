"""MCP server configuration resolved from environment and CLI flags."""

from __future__ import annotations

import os
from dataclasses import dataclass

from openusdconnect.cli_common import parse_bool, validate_port, validate_positive_seconds
from openusdconnect.client_id import make_stable_client_id
from openusdconnect.defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return parse_bool(raw)
    except ValueError as exc:
        raise ValueError(f"{name} {exc}") from exc


@dataclass
class McpConfig:
    """Connection + behavior settings for one MCP server process."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_SYNC_PORT
    client_id: str = ""
    department: str = ""
    mirror_enabled: bool = True
    auto_connect: bool = True
    auto_create_ancestors: bool = True
    read_after_write_timeout_s: float = 2.0

    def __post_init__(self):
        self.host = self.host.strip()
        if not self.host:
            raise ValueError("host must not be empty")
        self.port = validate_port(self.port)
        if self.read_after_write_timeout_s <= 0:
            raise ValueError("read_after_write_timeout_s must be greater than zero")
        if not self.client_id:
            self.client_id = make_stable_client_id("mcp")

    @classmethod
    def from_env(cls) -> McpConfig:
        port = validate_port(os.environ.get("OPENUSDCONNECT_PORT", DEFAULT_SYNC_PORT))
        timeout = validate_positive_seconds(
            os.environ.get("OPENUSDCONNECT_READ_TIMEOUT", 2.0)
        )
        return cls(
            host=os.environ.get("OPENUSDCONNECT_HOST", DEFAULT_HOST),
            port=port,
            client_id=os.environ.get("OPENUSDCONNECT_CLIENT_ID", ""),
            department=os.environ.get("OPENUSDCONNECT_DEPARTMENT", ""),
            mirror_enabled=_env_bool("OPENUSDCONNECT_MIRROR", True),
            auto_connect=_env_bool("OPENUSDCONNECT_AUTO_CONNECT", True),
            auto_create_ancestors=_env_bool("OPENUSDCONNECT_AUTO_ANCESTORS", True),
            read_after_write_timeout_s=timeout,
        )

    def merge_args(self, args) -> McpConfig:
        """Overlay non-None argparse values onto this config."""
        for name in ("host", "port", "client_id", "department"):
            val = getattr(args, name, None)
            if val is not None:
                setattr(self, name, val)
        for arg_name, field_name in (
            ("mirror", "mirror_enabled"),
            ("auto_connect", "auto_connect"),
            ("auto_create_ancestors", "auto_create_ancestors"),
            ("read_after_write_timeout", "read_after_write_timeout_s"),
        ):
            value = getattr(args, arg_name, None)
            if value is not None:
                setattr(self, field_name, value)
        self.__post_init__()
        return self
