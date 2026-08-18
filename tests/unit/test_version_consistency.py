"""Repository-wide version source and mirror checks."""

import argparse
import importlib.metadata

import pytest

from openusdconnect import __version__
from openusdconnect.cli_common import add_version_argument
from scripts.check_versions import _docker_base_images, _docker_pip_pins, collect_errors


def test_version_declarations_are_consistent():
    assert collect_errors() == []


def test_installed_distribution_uses_canonical_version():
    assert importlib.metadata.version("openusdconnect") == __version__


def test_standard_cli_version_output(capsys):
    parser = argparse.ArgumentParser(prog="openusdconnect-tool")
    add_version_argument(parser)

    with pytest.raises(SystemExit) as error:
        parser.parse_args(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out == f"openusdconnect-tool {__version__}\n"


def test_docker_version_parser_ignores_comments_and_stale_active_pins():
    dockerfile = """\
    # FROM python:3.13-slim
    FROM python:3.12-slim AS base
    # usd-core==26.8
    RUN pip install --no-cache-dir usd-core==26.7
    """

    assert _docker_base_images(dockerfile) == ["python:3.12-slim"]
    assert _docker_pip_pins(dockerfile) == {"usd-core": "26.7"}
