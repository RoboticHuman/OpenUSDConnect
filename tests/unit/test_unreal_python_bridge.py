"""Headless tests for the optional Unreal Python bridge plumbing."""

from unittest.mock import MagicMock

from integrations.unreal import usd_connect


def test_emitter_releases_batch_only_after_send_succeeds(monkeypatch):
    events = [{"k": "rename_prim", "prim": "/World/Old", "new_name": "New"}]
    emitter = MagicMock()
    emitter.prepare_events_for_send.return_value = events
    sender = MagicMock()
    sender.send_events.side_effect = [False, True]
    monkeypatch.setattr(usd_connect, "_emitter", emitter)
    monkeypatch.setattr(usd_connect, "_sender", sender)

    usd_connect._flush_emitter()
    emitter.mark_prepared_events_sent.assert_not_called()

    usd_connect._flush_emitter()

    assert sender.send_events.call_count == 2
    emitter.mark_prepared_events_sent.assert_called_once_with(events)


def test_manual_reconnect_retries_the_retained_batch(monkeypatch):
    events = [{"k": "delete_prim", "prim": "/World/Thing"}]
    emitter = MagicMock()
    emitter.prepare_events_for_send.return_value = events
    sender = MagicMock()
    sender.connected = False
    sender.auth_rejected = False
    sender.connect.return_value = True
    sender.send_events.return_value = True
    monkeypatch.setattr(usd_connect, "_running", True)
    monkeypatch.setattr(usd_connect, "_emitter", emitter)
    monkeypatch.setattr(usd_connect, "_sender", sender)

    assert usd_connect.reconnect_emitter() is True
    sender.connect.assert_called_once_with()
    sender.send_events.assert_called_once_with(events)
    emitter.mark_prepared_events_sent.assert_called_once_with(events)


def test_manual_reconnect_reports_a_failed_retry(monkeypatch):
    events = [{"k": "delete_prim", "prim": "/World/Thing"}]
    emitter = MagicMock()
    emitter.prepare_events_for_send.return_value = events
    sender = MagicMock()
    sender.connected = False
    sender.auth_rejected = False
    sender.connect.return_value = True
    sender.send_events.return_value = False
    monkeypatch.setattr(usd_connect, "_running", True)
    monkeypatch.setattr(usd_connect, "_emitter", emitter)
    monkeypatch.setattr(usd_connect, "_sender", sender)

    assert usd_connect.reconnect_emitter() is False
    emitter.mark_prepared_events_sent.assert_not_called()
