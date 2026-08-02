"""Process-level shutdown behavior for the command-line server."""

from __future__ import annotations

import os
import subprocess

from tests.helpers import start_server


def test_server_terminates_promptly(tmp_path, free_port):
    proc = start_server(tmp_path, free_port)
    proc.terminate()
    try:
        return_code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError("server did not terminate within 5 seconds") from None

    # On Windows Popen.terminate() calls TerminateProcess with exit code 1;
    # POSIX delivers SIGTERM, which the server handles as a clean shutdown.
    expected_return_code = 1 if os.name == "nt" else 0
    assert return_code == expected_return_code
