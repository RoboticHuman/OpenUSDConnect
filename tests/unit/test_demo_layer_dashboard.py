from __future__ import annotations

from scripts import demo_layer_dashboard


def test_demo_populates_only_supported_transactions(monkeypatch):
    connections = [object(), object(), object()]
    connected = iter(connections)
    transactions = []
    monkeypatch.setattr(
        demo_layer_dashboard,
        "_connect_emitter",
        lambda *_args: next(connected),
    )
    monkeypatch.setattr(
        demo_layer_dashboard,
        "_send_txn",
        lambda sock, txn_id, events: transactions.append((sock, txn_id, events)),
    )

    assert demo_layer_dashboard._populate_demo("127.0.0.1", 7200) == connections
    assert len(transactions) == 5
    assert [txn_id for _sock, txn_id, _events in transactions] == [1, 2, 1, 1, 2]
    assert all(
        event["k"] in {"ensure_prim", "ensure_xform_ops", "set_xform_trs"}
        for _sock, _txn_id, events in transactions
        for event in events
    )


def test_demo_forwards_plugin_directories(monkeypatch, tmp_path):
    base = tmp_path / "scene.usda"
    base.write_text("#usda 1.0", encoding="utf-8")
    args = demo_layer_dashboard._parse_args(
        [
            "--base",
            str(base),
            "--plugin-dll-dir",
            "C:/renderer/bin",
            "--exit-after",
            "0.01",
        ]
    )
    captured = {}

    class Process:
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(
        demo_layer_dashboard,
        "start_server_process",
        lambda server_args, **kwargs: captured.update(args=server_args, kwargs=kwargs) or Process(),
    )
    monkeypatch.setattr(demo_layer_dashboard, "wait_until_listening", lambda *_args: None)
    monkeypatch.setattr(demo_layer_dashboard, "_populate_demo", lambda *_args: [])
    monkeypatch.setattr(demo_layer_dashboard, "stop_process", lambda *_args: None)
    monkeypatch.setattr(demo_layer_dashboard.time, "sleep", lambda _seconds: None)

    assert demo_layer_dashboard._run(args, tmp_path / "events.db") == 0
    index = captured["args"].index("--plugin-dll-dir")
    assert captured["args"][index + 1] == "C:/renderer/bin"
