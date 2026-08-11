"""Live-server coverage for ManagedClient's single-stage bidirectional flow.

The tight-loop test is a regression guard for a C++ crash (0x80000003 inside
``event_apply.get_or_define_prim``) seen when the app thread authors into the
same ``Sdf.Layer`` the in-process server stage composes: ``Sdf.FindOrOpen``
deduplicates by identifier, so a client stage opened from the server's base
file shares the root layer, and the app thread's ``UsdAttribute.Set`` races
the server thread's ChangeBlock-driven recomposition. The safe in-process
pattern keeps the server and client on separate base files.
"""

from __future__ import annotations

import shutil
import threading
import time

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom

from openusdconnect.managed_client import ManagedClient
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer


@pytest.fixture
def live_server(tmp_path):
    base = tmp_path / "server-base.usda"
    Sdf.Layer.CreateNew(str(base)).Save()
    sync_server = UsdSyncServer(
        base_usd_path=str(base),
        log_path=str(tmp_path / "managed.db"),
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0),
        ConnectionHandler,
        sync_server,
        max_workers=8,
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    try:
        yield sync_server, tcp_server.server_address[1]
    finally:
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()


def _client_stage(tmp_path, name="client"):
    """A stage opened from a separate copy of the server base.

    Opening the server's own base file in-process would hand both stages the
    same ``Sdf.Layer`` (identifier dedup), recreating the crash scenario.
    """
    client_base = tmp_path / f"{name}-base.usda"
    shutil.copyfile(tmp_path / "server-base.usda", client_base)
    return Usd.Stage.Open(
        Sdf.Layer.FindOrOpen(str(client_base)),
        Sdf.Layer.CreateAnonymous("managed-client"),
    )


def _translation(prim: Usd.Prim):
    m = UsdGeom.Xformable(prim).GetLocalTransformation(Usd.TimeCode.Default())
    return (m[3][0], m[3][1], m[3][2])


def _drain_until(client: ManagedClient, predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client.update()
        if predicate():
            return True
        time.sleep(0.01)
    client.update()
    return bool(predicate())


def test_managed_client_tight_loop_round_trips_without_crash(live_server, tmp_path):
    """Full-speed edit loop: every authored value reaches the server and the
    composed stage, without the shared-layer C++ crash."""
    sync_server, port = live_server
    stage = _client_stage(tmp_path)
    client = ManagedClient(
        stage,
        app_name="managed-tight-loop",
        host="127.0.0.1",
        port=port,
        persist_token=False,
        reconnect=False,
    )
    client.start()
    assert client.connect(timeout=5)

    prim = stage.DefinePrim("/World/Test", "Xform")
    tr = UsdGeom.Xformable(prim).AddTranslateOp()
    tr.Set(Gf.Vec3d(0, 0, 0))
    for _ in range(5):
        client.update()
    for i in range(200):
        tr.Set(Gf.Vec3d(float(i), 0, 0))
        client.update()

    try:
        assert _drain_until(
            client,
            lambda: _translation(sync_server.stage.GetPrimAtPath("/World/Test"))[0] == 199.0,
        )
        assert _drain_until(
            client,
            lambda: _translation(stage.GetPrimAtPath("/World/Test"))[0] == 199.0,
        )
        assert _translation(sync_server.stage.GetPrimAtPath("/World/Test")) == (199.0, 0.0, 0.0)
        assert _translation(stage.GetPrimAtPath("/World/Test")) == (199.0, 0.0, 0.0)
    finally:
        client.close()


def test_managed_client_emits_structural_events_exactly_once(live_server, tmp_path):
    """The initial batch carries one ensure_prim per locally defined prim; the
    tight loop must never re-emit them (feedback-loop guard)."""
    sync_server, port = live_server
    stage = _client_stage(tmp_path)
    client = ManagedClient(
        stage,
        app_name="managed-once",
        host="127.0.0.1",
        port=port,
        persist_token=False,
        reconnect=False,
    )
    client.start()
    assert client.connect(timeout=5)

    counts: dict[str, int] = {}
    original_send = client._send

    def counting_send(events):
        for event in events:
            counts[event["k"]] = counts.get(event["k"], 0) + 1
        return original_send(events)

    client._send = counting_send

    prim = stage.DefinePrim("/World/Test", "Xform")
    tr = UsdGeom.Xformable(prim).AddTranslateOp()
    tr.Set(Gf.Vec3d(0, 0, 0))
    for _ in range(5):
        client.update()
    for i in range(50):
        tr.Set(Gf.Vec3d(float(i), 0, 0))
        client.update()
    _drain_until(client, lambda: _translation(stage.GetPrimAtPath("/World/Test"))[0] == 49.0)
    client.close()

    # /World and /World/Test are both locally defined by the first
    # DefinePrim, so the initial batch legitimately contains two
    # ensure_prim. Everything after must be value-only.
    assert counts["ensure_prim"] == 2
    assert counts["ensure_xform_ops"] == 1
    assert counts["set_xform_trs"] == 50


def test_managed_client_redirects_session_authoring_and_converges(live_server, tmp_path):
    sync_server, port = live_server
    first_stage = _client_stage(tmp_path, "first")
    second_stage = _client_stage(tmp_path, "second")
    first_stage.SetEditTarget(Usd.EditTarget(first_stage.GetSessionLayer()))
    second_stage.SetEditTarget(Usd.EditTarget(second_stage.GetSessionLayer()))
    first = ManagedClient(
        first_stage,
        app_name="managed-first",
        port=port,
        persist_token=False,
        reconnect=False,
    )
    second = ManagedClient(
        second_stage,
        app_name="managed-second",
        port=port,
        persist_token=False,
        reconnect=False,
    )
    try:
        assert first.authoring_layer is not first_stage.GetSessionLayer()
        assert second.authoring_layer is not second_stage.GetSessionLayer()
        assert first_stage.GetEditTarget().GetLayer() is first.authoring_layer
        assert second_stage.GetEditTarget().GetLayer() is second.authoring_layer
        first.start()
        second.start()
        assert first.connect(timeout=5)
        assert second.connect(timeout=5)

        prim = first_stage.DefinePrim("/World/Shared", "Xform")
        prim.CreateAttribute("value", Sdf.ValueTypeNames.Int).Set(1)
        assert first.update().submitted_events > 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            first.update()
            second.update()
            attr = second_stage.GetAttributeAtPath("/World/Shared.value")
            if attr and attr.Get() == 1:
                break
            time.sleep(0.01)
        assert second_stage.GetAttributeAtPath("/World/Shared.value").Get() == 1

        second_stage.GetAttributeAtPath("/World/Shared.value").Set(2)
        assert second.update().submitted_events > 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            first.update()
            second.update()
            if first_stage.GetAttributeAtPath("/World/Shared.value").Get() == 2:
                break
            time.sleep(0.01)
        assert first_stage.GetAttributeAtPath("/World/Shared.value").Get() == 2
        assert sync_server.stage.GetAttributeAtPath("/World/Shared.value").Get() == 2
        assert first.authoring_layer.GetAttributeAtPath("/World/Shared.value").default == 1
    finally:
        first.close()
        second.close()


def test_managed_clients_apply_complete_commit_order_without_echo(live_server, tmp_path):
    sync_server, port = live_server
    stages = [_client_stage(tmp_path, name) for name in ("race-first", "race-second")]
    clients = [
        ManagedClient(
            stage,
            app_name=f"managed-race-{index}",
            port=port,
            persist_token=False,
            reconnect=False,
        )
        for index, stage in enumerate(stages)
    ]
    try:
        for client in clients:
            client.start()
            assert client.connect(timeout=5)

        first_attr = stages[0].DefinePrim("/World/Shared", "Xform").CreateAttribute(
            "value",
            Sdf.ValueTypeNames.Int,
        )
        first_attr.Set(0)
        assert clients[0].update().submitted_events > 0
        assert _drain_until(
            clients[1],
            lambda: (
                bool(stages[1].GetAttributeAtPath("/World/Shared.value"))
                and stages[1].GetAttributeAtPath("/World/Shared.value").Get() == 0
            ),
        )

        sent_kinds: list[list[str]] = [[], []]
        for index, client in enumerate(clients):
            original_send = client._send

            def capture(events, *, _index=index, _send=original_send):
                sent_kinds[_index].extend(event["k"] for event in events)
                return _send(events)

            client._send = capture

        # Both opinions exist before either client processes the peer's commit.
        stages[0].GetAttributeAtPath("/World/Shared.value").Set(10)
        stages[1].GetAttributeAtPath("/World/Shared.value").Set(20)
        assert clients[0].update().submitted_events > 0
        assert clients[1].update().submitted_events > 0
        assert clients[0].flush(timeout=5)
        assert clients[1].flush(timeout=5)

        committed_head = sync_server.store.get_max_seq()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for client in clients:
                client.update()
            if all(client.last_seq == committed_head for client in clients):
                break
            time.sleep(0.01)

        authoritative = sync_server.stage.GetAttributeAtPath("/World/Shared.value").Get()
        assert all(client.last_seq == committed_head for client in clients)
        assert [
            stage.GetAttributeAtPath("/World/Shared.value").Get() for stage in stages
        ] == [authoritative, authoritative]

        emitted_after_local_changes = [list(kinds) for kinds in sent_kinds]
        assert all(emitted_after_local_changes)
        for _ in range(10):
            for client in clients:
                client.update()
        assert sent_kinds == emitted_after_local_changes
    finally:
        for client in clients:
            client.close()


def test_managed_client_rebinds_and_parks_the_emitter():
    old_stage = Usd.Stage.CreateInMemory("old.usda")
    new_stage = Usd.Stage.CreateInMemory("new.usda")
    old_stage.SetEditTarget(Usd.EditTarget(old_stage.GetSessionLayer()))
    new_stage.SetEditTarget(Usd.EditTarget(new_stage.GetSessionLayer()))
    client = ManagedClient(
        old_stage,
        app_name="managed-rebind",
        persist_token=False,
        reconnect=False,
    )
    try:
        client.rebind_stage(new_stage)
        assert client.stage is new_stage
        assert client.emitter.stage is new_stage
        old_stage.DefinePrim("/OldOnly", "Scope")
        assert client.emitter.build_events_for_dirty() == []
        new_stage.DefinePrim("/NewOnly", "Scope")
        assert client.emitter.build_events_for_dirty()

        client.rebind_stage(None)
        new_stage.DefinePrim("/AfterPark", "Scope")
        assert client.emitter.build_events_for_dirty() == []
    finally:
        client.close()


def test_managed_client_hands_ephemeral_tofu_token_to_sender(tmp_path):
    base = tmp_path / "auth-base.usda"
    Sdf.Layer.CreateNew(str(base)).Save()
    sync_server = UsdSyncServer(
        base_usd_path=str(base),
        log_path=str(tmp_path / "auth-events.db"),
        require_token=True,
        token_db_path=str(tmp_path / "auth-tokens.db"),
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0),
        ConnectionHandler,
        sync_server,
        max_workers=4,
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    stage = Usd.Stage.Open(str(base))
    client = ManagedClient(
        stage,
        app_name="managed-ephemeral-auth",
        port=tcp_server.server_address[1],
        persist_token=False,
        reconnect=False,
    )
    try:
        client.start()
        assert client.connect(timeout=5)
        assert client.connected
        assert client.receiver.token
        assert client.sender.token == client.receiver.token
        assert not client.auth_rejected
    finally:
        client.close()
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()
