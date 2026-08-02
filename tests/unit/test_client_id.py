from __future__ import annotations

from openusdconnect.client_id import make_stable_client_id


def test_canonical_client_id_environment_variable_takes_precedence(monkeypatch):
    monkeypatch.setenv("USD_CONNECT_CLIENT_ID", "legacy")
    monkeypatch.setenv("OPENUSDCONNECT_CLIENT_ID", "canonical")

    assert make_stable_client_id("test") == "canonical"


def test_legacy_client_id_environment_variable_remains_supported(monkeypatch):
    monkeypatch.delenv("OPENUSDCONNECT_CLIENT_ID", raising=False)
    monkeypatch.setenv("USD_CONNECT_CLIENT_ID", "legacy")

    assert make_stable_client_id("test") == "legacy"
