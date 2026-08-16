"""Tests for the project-aware server launcher."""

import openusdconnect.server
from integrations import run_server


def test_launcher_configures_renderman_paths_before_standard_server(monkeypatch):
    events = []
    monkeypatch.setattr(run_server, "apply_dll_dirs", lambda: events.append("renderman_paths"))

    def server_main():
        events.append("server")
        return 7

    monkeypatch.setattr(openusdconnect.server, "main", server_main)

    result = run_server.main()

    assert events == ["renderman_paths", "server"]
    assert result == 7
