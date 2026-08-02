from __future__ import annotations

import argparse

import pytest

from integrations.mcp.config import McpConfig


def test_mcp_environment_uses_shared_endpoint_defaults(monkeypatch):
    for name in (
        "OPENUSDCONNECT_HOST",
        "OPENUSDCONNECT_PORT",
        "OPENUSDCONNECT_READ_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    config = McpConfig.from_env()

    assert (config.host, config.port) == ("127.0.0.1", 7200)
    assert config.read_after_write_timeout_s == 2.0


def test_mcp_environment_rejects_invalid_port(monkeypatch):
    monkeypatch.setenv("OPENUSDCONNECT_PORT", "70000")

    with pytest.raises(ValueError, match="65535"):
        McpConfig.from_env()


def test_mcp_environment_rejects_invalid_boolean(monkeypatch):
    monkeypatch.setenv("OPENUSDCONNECT_MIRROR", "sometimes")

    with pytest.raises(ValueError, match="OPENUSDCONNECT_MIRROR"):
        McpConfig.from_env()


def test_mcp_environment_rejects_invalid_timeout(monkeypatch):
    monkeypatch.setenv("OPENUSDCONNECT_READ_TIMEOUT", "0")

    with pytest.raises(ValueError, match="greater than zero"):
        McpConfig.from_env()


def test_mcp_cli_behavior_values_overlay_environment():
    config = McpConfig()
    args = argparse.Namespace(
        host=None,
        port=None,
        client_id=None,
        department=None,
        mirror=False,
        auto_connect=False,
        auto_create_ancestors=False,
        read_after_write_timeout=0.5,
    )

    config.merge_args(args)

    assert config.mirror_enabled is False
    assert config.auto_connect is False
    assert config.auto_create_ancestors is False
    assert config.read_after_write_timeout_s == 0.5


def test_mcp_cli_overlay_is_revalidated():
    config = McpConfig()
    args = argparse.Namespace(host="", port=None)

    with pytest.raises(ValueError, match="host must not be empty"):
        config.merge_args(args)
