"""The single stateful object: connection + mirror stage + read-after-write.

Emit side: an ``EventSender`` (origin ``...-emit``). Mirror side: a
``ReceiverThread`` + ``EventDispatcher`` + ``UsdStageAdapter`` over an in-memory
``Usd.Stage`` (origin ``...-recv``). The two origins differ so the server
echoes the MCP's own writes back to the receiver, keeping the mirror the
server's authoritative composed result. Same ``client_id`` keeps all MCP edits
in one per-client layer.
"""

from __future__ import annotations

import time
import uuid

from openusdconnect import token_client
from openusdconnect.adapters import UsdStageAdapter
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.event_apply import apply_events
from openusdconnect.protocol_constants import K_SET_STAGE_METADATA, STAGE_METADATA_KEYS
from openusdconnect.receiver import ReceiverThread
from openusdconnect.sender import EventSender

from .config import McpConfig
from .errors import ToolError
from .introspection import select_changes


class ConnectionSession:
    """Owns the network client and the mirror stage for one MCP process."""

    def __init__(self, config: McpConfig):
        self.config = config
        self.sender: EventSender | None = None
        self.receiver: ReceiverThread | None = None
        self.dispatcher: EventDispatcher | None = None
        self.mirror_stage = None
        self.auth_rejected = False
        self._origin_base = f"mcp-{uuid.uuid4().hex[:8]}"
        # prim_path -> sequence it last changed at, fed by the dispatcher's
        # on_applied hook; powers changes_since() diff queries.
        self._dirty: dict[str, int] = {}
        # Latest PlaybackState the server broadcast, set on the receiver thread.
        self._playback_state: dict | None = None

    @property
    def connected(self) -> bool:
        return self.sender is not None and self.sender.is_connected

    # -- lifecycle ---------------------------------------------------------

    def connect(
        self,
        host: str | None = None,
        port: int | None = None,
        client_id: str | None = None,
        department: str | None = None,
    ) -> dict:
        if self.connected:
            return self.status()
        # A dropped connection leaves stale state: send() nulls only self.sender,
        # so the previous ReceiverThread is still running. Stop it before rebuilding
        # or each reconnect leaks the thread (and opens a duplicate mirror socket).
        self._teardown()
        cfg = self.config
        if host is not None:
            cfg.host = host
        if port is not None:
            cfg.port = port
        if client_id:
            cfg.client_id = client_id
        if department is not None:
            cfg.department = department

        token = token_client.load_token(cfg.host, cfg.port)
        self.auth_rejected = False
        self.sender = EventSender(
            cfg.host,
            cfg.port,
            client_id=cfg.client_id,
            role="emitter",
            origin=f"{self._origin_base}-emit",
            department=cfg.department or None,
            token=token,
            on_token_issued=lambda t: token_client.save_token(cfg.host, cfg.port, t),
        )
        if not self.sender.connect():
            rejected = self.sender.auth_rejected
            self.sender = None
            if rejected:
                self.auth_rejected = True
                raise ToolError(
                    f"authentication rejected by {cfg.host}:{cfg.port}",
                    code="auth_rejected",
                    hint="The server requires a token you don't have, or yours was revoked.",
                )
            raise ToolError(
                f"could not connect to {cfg.host}:{cfg.port}",
                code="connect_failed",
                hint="Is the sync server running? Start it with "
                "`uv run python -m openusdconnect.server --port <port>`.",
            )

        if cfg.mirror_enabled:
            self._start_mirror()
        return self.status()

    def _start_mirror(self) -> None:
        from pxr import Usd

        cfg = self.config
        self.mirror_stage = Usd.Stage.CreateInMemory()
        self._dirty = {}
        self._playback_state = None
        self._seed_metadata(self.sender.stage_metadata)
        recv_token = token_client.load_token(cfg.host, cfg.port) or self.sender.token
        self.receiver = ReceiverThread(
            host=cfg.host,
            port=cfg.port,
            sync_from=1,
            client_id=cfg.client_id,
            origin=f"{self._origin_base}-recv",
            token=recv_token,
            on_playback_state=self._on_playback_state,
            layered_replay=True,
        )
        self.receiver.start()
        self.dispatcher = EventDispatcher(
            receiver=self.receiver,
            adapter=UsdStageAdapter(self.mirror_stage),
            on_applied=self._on_applied,
        )

    def _on_applied(self, prim_paths: list) -> None:
        """Stamp each applied prim with the current sequence so changes_since can
        report it. Coarse at drain granularity (a whole drain shares its final
        seq), which is fine for 'what changed since N' polling."""
        seq = self.dispatcher.last_seq if self.dispatcher else 0
        for path in prim_paths:
            self._dirty[path] = seq

    def _on_playback_state(self, msg: dict) -> None:
        """Store the latest shared-playhead snapshot. Runs on the receiver
        thread, so assign a fresh dict (an atomic reference swap) rather than
        mutating in place."""
        self._playback_state = {
            "playing": msg.get("playing"),
            "time": msg.get("time"),
            "rate": msg.get("rate"),
            "leader_client_id": msg.get("leader_client_id") or "",
        }

    def _seed_metadata(self, metadata: dict | None) -> None:
        payload = {k: v for k, v in (metadata or {}).items() if k in STAGE_METADATA_KEYS}
        if payload:
            apply_events(self.mirror_stage, [{"k": K_SET_STAGE_METADATA, **payload}])

    def _teardown(self) -> None:
        """Stop the receiver thread and sender, clearing all connection state."""
        if self.receiver is not None:
            self.receiver.stop()
        if self.sender is not None:
            self.sender.disconnect()
        if self.dispatcher is not None:
            self.dispatcher.close()
        self.sender = None
        self.receiver = None
        self.dispatcher = None
        self.mirror_stage = None
        self._dirty = {}
        self._playback_state = None

    def disconnect(self) -> dict:
        self._teardown()
        return {"ok": True}

    # -- I/O ---------------------------------------------------------------

    def send(self, events: list[dict]) -> dict:
        """Send one txn and drain the mirror until it reflects the write."""
        if not self.connected:
            raise ToolError("not connected, call usd_connect first", code="not_connected")
        pre_seq = self.dispatcher.last_seq if self.dispatcher else 0
        if not self.sender.send_events(events):
            self.sender = None
            raise ToolError("send failed, connection lost", code="disconnected")
        synced = self._drain_until(pre_seq + len(events))
        return {
            "sent": True,
            "event_count": len(events),
            "last_seq": self.dispatcher.last_seq if self.dispatcher else None,
            "mirror_synced": synced,
        }

    def _drain_until(self, target_seq: int) -> bool:
        """Bounded drain until last_seq reaches target (best-effort RAW sync)."""
        if self.dispatcher is None:
            return False
        deadline = time.monotonic() + self.config.read_after_write_timeout_s
        while self.dispatcher.last_seq < target_seq and time.monotonic() < deadline:
            if self.dispatcher.drain_and_apply() == 0:
                time.sleep(0.005)
        return self.dispatcher.last_seq >= target_seq

    def pump(self) -> int:
        """Non-blocking drain so introspection reflects recent foreign edits."""
        if self.dispatcher is None:
            return 0
        return self.dispatcher.drain_and_apply()

    def require_mirror(self):
        """Return the mirror stage or raise if introspection is unavailable."""
        if not self.connected:
            raise ToolError("not connected, call usd_connect first", code="not_connected")
        if self.mirror_stage is None:
            raise ToolError(
                "the mirror stage is disabled; introspection is unavailable",
                code="no_mirror",
            )
        self.pump()
        return self.mirror_stage

    def changes_since(self, since_seq: int = 0, max: int = 500) -> dict:
        """Prims changed since a sequence (oldest first), capped at ``max``. Poll
        with the previous ``last_seq`` to find own + foreign edits without
        re-listing the scene; then batch usd_get_prims the returned paths."""
        if not self.connected:
            raise ToolError("not connected, call usd_connect first", code="not_connected")
        if self.mirror_stage is None:
            raise ToolError(
                "the mirror stage is disabled; introspection is unavailable",
                code="no_mirror",
            )
        self.pump()
        last = self.dispatcher.last_seq if self.dispatcher else 0
        return select_changes(self._dirty, since_seq, max, last)

    # -- playback ----------------------------------------------------------

    def claim_playback(self, time_code: float | None = None) -> dict:
        if not self.connected:
            raise ToolError("not connected, call usd_connect first", code="not_connected")
        ok = self.sender.claim_playback(time=time_code)
        return {"ok": ok, "requested": True}

    def playback_control(
        self, action: str, time_code: float | None = None, rate: float | None = None
    ) -> dict:
        if not self.connected:
            raise ToolError("not connected, call usd_connect first", code="not_connected")
        if action not in ("play", "pause", "stop", "set_time", "set_rate"):
            raise ToolError(
                f"unknown playback action {action!r}",
                code="invalid_request",
                field="action",
                hint="One of: play, pause, stop, set_time, set_rate.",
            )
        ok = self.sender.send_playback_control(action, time=time_code, rate=rate)
        return {"ok": ok, "action": action}

    def playback_status(self) -> dict:
        """Read the shared playhead from the latest broadcast PlaybackState:
        playing/time/rate, the leader's client id, and whether this client leads.
        ``observed`` is False until the first PlaybackState arrives (needs the
        mirror's receiver; disabled under --no-mirror)."""
        if not self.connected:
            raise ToolError("not connected, call usd_connect first", code="not_connected")
        state = self._playback_state
        if state is None:
            return {"ok": True, "observed": False}
        leader = state.get("leader_client_id") or ""
        return {
            "ok": True,
            "observed": True,
            "playing": bool(state.get("playing")),
            "time": state.get("time"),
            "rate": state.get("rate"),
            "leader_client_id": leader,
            "has_leader": bool(leader),
            "is_leader": bool(leader) and leader == self.config.client_id,
        }

    # -- status ------------------------------------------------------------

    def _mirror_prim_count(self) -> int:
        if self.mirror_stage is None:
            return 0
        from pxr import Usd

        return sum(
            1 for p in Usd.PrimRange(self.mirror_stage.GetPseudoRoot()) if not p.IsPseudoRoot()
        )

    def status(self) -> dict:
        return {
            "ok": True,
            "connected": self.connected,
            "host": self.config.host,
            "port": self.config.port,
            "client_id": self.config.client_id,
            "department": self.config.department,
            "mirror_enabled": self.config.mirror_enabled,
            "mirror_prim_count": self._mirror_prim_count(),
            "last_seq": self.dispatcher.last_seq if self.dispatcher else 0,
            "auth_rejected": self.auth_rejected,
            "stage_metadata": dict(self.sender.stage_metadata) if self.sender else {},
        }
