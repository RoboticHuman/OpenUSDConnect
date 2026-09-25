"""Playback leadership and playhead state, independent of server transport."""

from __future__ import annotations

import threading


class PlaybackController:
    """Serialize leader changes and controls under one privately owned lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._time = 0.0
        self._playing = False
        self._rate = 1.0
        self._leader_client_id = ""

    def _snapshot_locked(self) -> dict:
        return {
            "time": self._time,
            "playing": self._playing,
            "rate": self._rate,
            "leader_client_id": self._leader_client_id,
        }

    def snapshot(self) -> dict:
        """Return an independent snapshot in the protocol's dictionary shape."""
        with self._lock:
            return self._snapshot_locked()

    def claim(self, client_id: str, initial_time: float | None = None) -> tuple[bool, str]:
        """Grant a vacant role or renew the current leader's claim.

        Setting an initial playhead and granting leadership are atomic. The
        returned leader also describes a rejected claim without another read.
        """
        with self._lock:
            current = self._leader_client_id
            if not client_id:
                return False, current
            if current and current != client_id:
                return False, current
            self._leader_client_id = client_id
            if initial_time is not None:
                self._time = float(initial_time)
            return True, client_id

    def apply_control(
        self,
        client_id: str,
        action: str,
        time_value: float = 0.0,
        rate: float = 1.0,
    ) -> tuple[bool, dict | str, str]:
        """Apply a leader's command and return its resulting state or rejection.

        Both outcomes include the leader observed under the same lock, so the
        connection can construct its response without reading mutable state.
        """
        with self._lock:
            leader = self._leader_client_id
            if leader != client_id:
                return False, f"not the playback leader (current: {leader})", leader
            if action == "play":
                self._playing = True
            elif action == "pause":
                self._playing = False
            elif action == "stop":
                self._playing = False
                self._time = 0.0
            elif action == "set_time":
                self._time = float(time_value)
            elif action == "set_rate":
                self._rate = float(rate)
            else:
                return False, f"unknown playback action {action!r}", leader
            return True, self._snapshot_locked(), leader

    def release(self, client_id: str) -> bool:
        """Release leadership on disconnect, retaining the current playhead."""
        if not client_id:
            return False
        with self._lock:
            if self._leader_client_id == client_id:
                self._leader_client_id = ""
                return True
            return False
