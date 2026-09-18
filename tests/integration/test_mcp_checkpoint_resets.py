"""A transaction acknowledgement must not adopt a later history reset."""

import threading

from integrations.mcp.config import McpConfig
from integrations.mcp.session import ConnectionSession
from tests.integration.test_receiver_replay_identity import _event, _server


def test_ack_retains_commit_checkpoint_when_purged_before_delivery(monkeypatch):
    with _server() as (state, port):
        session = ConnectionSession(McpConfig(port=port, read_after_write_timeout_s=0.1))
        purged = threading.Event()
        wait = state.wait_for_transaction

        def purge_after_commit(request):
            commit = wait(request)
            state.purge()
            purged.set()
            return commit

        monkeypatch.setattr(state, "wait_for_transaction", purge_after_commit)
        try:
            session.connect()
            send = session.sender.send_events

            def hold_drain_until_purged(events):
                result = send(events)
                assert purged.wait(5)
                return result

            monkeypatch.setattr(session.sender, "send_events", hold_drain_until_purged)
            result = session.send([_event("/Own")])
            assert session.sender.acknowledged_checkpoint == (state.server_instance, 0, 1)
            assert not session.mirror_stage.GetPrimAtPath("/Own")
            assert result["mirror_synced"] is False
        finally:
            session.disconnect()


def test_duplicate_after_purge_has_no_original_visibility_proof():
    with _server() as (state, _port):
        transaction = dict(session_id="producer", txn_id=1, client_id="producer")
        committed = state.process_idempotent_txn([_event("/Own")], **transaction)
        assert committed.checkpoint == (0, 1)
        state.purge()
        duplicate = state.process_idempotent_txn([_event("/Own")], **transaction)
        assert duplicate.status == "duplicate"
        assert duplicate.checkpoint is None
