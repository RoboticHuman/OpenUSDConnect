from __future__ import annotations

import argparse

import pytest

from openusdconnect.cli_common import (
    add_sync_endpoint_args,
    add_vfs_resource_args,
    comma_separated,
    file_name,
    parse_bool,
    path_segment,
    port_number,
    positive_float,
    positive_seconds,
    validate_nonnegative_int,
)


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_port_number_rejects_invalid_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        port_number(value)


def test_positive_seconds_and_comma_separated_validation():
    assert positive_seconds("0.25") == 0.25
    assert positive_float("1.5") == 1.5
    assert comma_separated(" lighting, fx ,, layout ") == ["lighting", "fx", "layout"]
    with pytest.raises(argparse.ArgumentTypeError):
        positive_seconds("0")
    with pytest.raises(argparse.ArgumentTypeError):
        positive_float("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        comma_separated(" , ")


def test_plain_config_validators():
    assert validate_nonnegative_int("0") == 0
    assert parse_bool("YES") is True
    assert parse_bool("off") is False
    with pytest.raises(ValueError, match="zero or greater"):
        validate_nonnegative_int("-1")
    with pytest.raises(ValueError, match="boolean"):
        parse_bool("sometimes")


def test_endpoint_helpers_share_defaults_and_support_prefixes():
    parser = argparse.ArgumentParser()
    add_sync_endpoint_args(parser)
    add_vfs_resource_args(parser)

    args = parser.parse_args([])

    assert (args.host, args.port) == ("127.0.0.1", 7200)
    assert (args.vfs_host, args.vfs_port) == ("127.0.0.1", 7280)
    assert (args.vfs_share, args.vfs_name) == ("usd", "scene.usd")


@pytest.mark.parametrize("value", ["", " ", ".", "../usd", "nested/usd", r"nested\usd"])
def test_path_segment_rejects_non_segment_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        path_segment(value)


@pytest.mark.parametrize("value", ["", " ", "..", "nested/scene.usd", r"nested\scene.usd"])
def test_file_name_rejects_paths(value):
    with pytest.raises(argparse.ArgumentTypeError):
        file_name(value)
