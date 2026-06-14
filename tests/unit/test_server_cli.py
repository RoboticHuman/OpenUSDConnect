"""Unit tests for server CLI helper behavior."""

import pytest

from openusdconnect.server.cli import (
    _default_advertise_host,
    _host_for_url,
    _normalize_vfs_share,
    _validate_vfs_name,
)


def test_default_advertise_host_for_wildcard_binds():
    assert _default_advertise_host("0.0.0.0") == "127.0.0.1"
    assert _default_advertise_host("::") == "127.0.0.1"
    assert _default_advertise_host("") == "127.0.0.1"
    assert _default_advertise_host("10.0.0.5") == "10.0.0.5"


def test_host_for_url_wraps_ipv6():
    assert _host_for_url("127.0.0.1") == "127.0.0.1"
    assert _host_for_url("::1") == "[::1]"
    assert _host_for_url("[::1]") == "[::1]"


def test_normalize_vfs_share_accepts_single_segment():
    assert _normalize_vfs_share("/usd/") == "usd"


@pytest.mark.parametrize("share", ["", "/", ".", "..", "usd/live", r"usd\live"])
def test_normalize_vfs_share_rejects_paths(share):
    with pytest.raises(ValueError, match="vfs-share"):
        _normalize_vfs_share(share)


def test_validate_vfs_name_accepts_file_name():
    assert _validate_vfs_name("scene.usd") == "scene.usd"


@pytest.mark.parametrize("name", ["", ".", "..", "usd/scene.usd", r"usd\scene.usd"])
def test_validate_vfs_name_rejects_paths(name):
    with pytest.raises(ValueError, match="vfs-name"):
        _validate_vfs_name(name)
