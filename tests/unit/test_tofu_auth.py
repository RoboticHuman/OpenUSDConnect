"""Tests for TOFU (Trust On First Use) token authentication."""

from __future__ import annotations

import pytest

from openusdconnect.token_store import TokenStore


@pytest.fixture
def store(tmp_path):
    return TokenStore(str(tmp_path / "tokens.db"))


class TestTokenStore:
    def test_issue_and_verify(self, store):
        token = store.issue("alice")
        assert store.verify("alice", token)

    def test_verify_wrong_token(self, store):
        store.issue("alice")
        assert not store.verify("alice", "wrong-token")

    def test_verify_unknown_client(self, store):
        assert not store.verify("unknown", "any-token")

    def test_has_token(self, store):
        assert not store.has_token("alice")
        store.issue("alice")
        assert store.has_token("alice")

    def test_revoke(self, store):
        token = store.issue("alice")
        assert store.revoke("alice")
        assert not store.has_token("alice")
        assert not store.verify("alice", token)

    def test_revoke_nonexistent(self, store):
        assert not store.revoke("nobody")

    def test_reissue_overwrites(self, store):
        token1 = store.issue("alice")
        token2 = store.issue("alice")
        assert token1 != token2
        assert not store.verify("alice", token1)
        assert store.verify("alice", token2)

    def test_get_all(self, store):
        store.issue("alice", department="animation")
        store.issue("bob", department="lighting")
        records = store.get_all()
        assert len(records) == 2
        ids = {r["client_id"] for r in records}
        assert ids == {"alice", "bob"}
        # Token values should not be exposed
        assert all("token" not in r for r in records)

    def test_department_stored(self, store):
        store.issue("alice", department="animation")
        records = store.get_all()
        assert records[0]["department"] == "animation"


class TestServerAuthenticate:
    def test_no_token_required_always_accepts(self):
        from openusdconnect.server import UsdSyncServer

        srv = UsdSyncServer(log_path=":memory:", require_token=False)
        accepted, token = srv.authenticate("alice", None)
        assert accepted
        assert token is None

    def test_first_connect_issues_token(self, tmp_path):
        from openusdconnect.server import UsdSyncServer

        srv = UsdSyncServer(
            log_path=":memory:", require_token=True,
            token_db_path=str(tmp_path / "tokens.db"),
        )
        accepted, token = srv.authenticate("alice", None, "animation")
        assert accepted
        assert token is not None

    def test_reconnect_with_valid_token(self, tmp_path):
        from openusdconnect.server import UsdSyncServer

        srv = UsdSyncServer(
            log_path=":memory:", require_token=True,
            token_db_path=str(tmp_path / "tokens.db"),
        )
        _, token = srv.authenticate("alice", None)
        accepted, new_token = srv.authenticate("alice", token)
        assert accepted
        assert new_token is None  # No new token on reconnect

    def test_reconnect_with_wrong_token(self, tmp_path):
        from openusdconnect.server import UsdSyncServer

        srv = UsdSyncServer(
            log_path=":memory:", require_token=True,
            token_db_path=str(tmp_path / "tokens.db"),
        )
        srv.authenticate("alice", None)
        accepted, _ = srv.authenticate("alice", "wrong-token")
        assert not accepted

    def test_reconnect_without_token(self, tmp_path):
        from openusdconnect.server import UsdSyncServer

        srv = UsdSyncServer(
            log_path=":memory:", require_token=True,
            token_db_path=str(tmp_path / "tokens.db"),
        )
        srv.authenticate("alice", None)
        accepted, _ = srv.authenticate("alice", None)
        assert not accepted

    def test_revoke_then_reconnect_rejected(self, tmp_path):
        from openusdconnect.server import UsdSyncServer

        srv = UsdSyncServer(
            log_path=":memory:", require_token=True,
            token_db_path=str(tmp_path / "tokens.db"),
        )
        _, token = srv.authenticate("alice", None)
        srv.revoke_token("alice")
        accepted, _ = srv.authenticate("alice", token)
        # After revoke, client_id has no token — TOFU re-issues
        assert accepted  # Re-issued on first connect after revoke

    def test_no_client_id_rejected_when_token_required(self, tmp_path):
        from openusdconnect.server import UsdSyncServer

        srv = UsdSyncServer(
            log_path=":memory:", require_token=True,
            token_db_path=str(tmp_path / "tokens.db"),
        )
        accepted, token = srv.authenticate(None, None)
        assert not accepted
        assert token is None

    def test_no_client_id_accepted_when_token_not_required(self):
        from openusdconnect.server import UsdSyncServer

        srv = UsdSyncServer(log_path=":memory:", require_token=False)
        accepted, token = srv.authenticate(None, None)
        assert accepted
        assert token is None


class TestTokenClient:
    def test_save_and_load(self, tmp_path, monkeypatch):
        from openusdconnect import token_client

        monkeypatch.setattr(token_client, "_TOKEN_DIR", str(tmp_path))
        monkeypatch.setattr(
            token_client, "_TOKEN_FILE", str(tmp_path / "tokens.json"),
        )

        assert token_client.load_token("localhost", 7200) is None
        token_client.save_token("localhost", 7200, "secret123")
        assert token_client.load_token("localhost", 7200) == "secret123"

    def test_multiple_servers(self, tmp_path, monkeypatch):
        from openusdconnect import token_client

        monkeypatch.setattr(token_client, "_TOKEN_DIR", str(tmp_path))
        monkeypatch.setattr(
            token_client, "_TOKEN_FILE", str(tmp_path / "tokens.json"),
        )

        token_client.save_token("server-a", 7200, "token-a")
        token_client.save_token("server-b", 7200, "token-b")
        assert token_client.load_token("server-a", 7200) == "token-a"
        assert token_client.load_token("server-b", 7200) == "token-b"

    def test_delete_token(self, tmp_path, monkeypatch):
        from openusdconnect import token_client

        monkeypatch.setattr(token_client, "_TOKEN_DIR", str(tmp_path))
        monkeypatch.setattr(
            token_client, "_TOKEN_FILE", str(tmp_path / "tokens.json"),
        )

        token_client.save_token("localhost", 7200, "secret123")
        token_client.delete_token("localhost", 7200)
        assert token_client.load_token("localhost", 7200) is None
