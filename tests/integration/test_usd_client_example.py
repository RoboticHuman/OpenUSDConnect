"""Executable onboarding coverage for the USD-native client example."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

from examples.usd_native_client import run as onboarding
from integrations.usdview import launcher as usdview_launcher

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_two_peer_usd_client_onboarding_example(tmp_path):
    env = {**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    result = subprocess.run(
        [
            sys.executable,
            "examples/usd_native_client/run.py",
            "--no-usdview",
            "--seconds",
            "0.1",
            "--peer-delay",
            "0",
            "--port",
            str(_unused_port()),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )

    diagnostic = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode == 0, diagnostic
    assert "local_valid=True" in result.stdout, diagnostic
    assert "peer_valid=True" in result.stdout, diagnostic
    assert "peer published /World/PeerCube" in result.stdout, diagnostic
    assert list(tmp_path.glob("usd_native_client_*.db*")) == []


def test_usdview_spawn_error_continues_headless(monkeypatch, capsys):
    def fail_launch(*_args, **_kwargs):
        raise OSError("usdview executable is not runnable")

    monkeypatch.setattr(usdview_launcher, "launch_usdview", fail_launch)

    process = onboarding._try_launch_usdview(Path("scene.usda"), "127.0.0.1", 7200)

    assert process is None
    assert capsys.readouterr().out == (
        "usdview unavailable (usdview executable is not runnable); continuing headless\n"
    )
