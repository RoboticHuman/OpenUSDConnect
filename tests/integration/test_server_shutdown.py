"""Process-level shutdown behavior for the command-line server."""

from __future__ import annotations

import subprocess

from tests.helpers import start_server


def test_server_terminates_cleanly(tmp_path, free_port):
    proc = start_server(tmp_path, free_port)
    proc.terminate()
    try:
        return_code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError("server did not terminate within 5 seconds") from None

    assert return_code == 0
