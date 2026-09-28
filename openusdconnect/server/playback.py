"""Playback leadership and playhead state, independent of server transport."""

from __future__ import annotations

import threading


class PlaybackController:
    """Serialize leader changes and controls under the shared playback lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = {
            "time": 0.0,
            "playing": False,
            "rate": 1.0,
            "leader_client_id": None,
        }

    def _snapshot_locked(self) -> dict:
        return {
            "time": self.state["time"],
            "playing": self.state["playing"],
            "rate": self.state["rate"],
            "leader_client_id": self.state["leader_client_id"] or "",
        }

    def snapshot(self) -> dict:
        """Return an independent snapshot in the protocol's dictionary shape."""
        with self.lock:
            return self._snapshot_locked()

    def claim(self, client_id: str, initial_time: float | None = None) -> tuple[bool, str]:
        """Grant a vacant role or renew the current leader's claim.

        Setting an initial playhead and granting leadership are atomic. The
        returned leader also describes a rejected claim without another read.
        """
        with self.lock:
            current = self.state["leader_client_id"] or ""
            if not client_id:
                return False, current
            if current and current != client_id:
                return False, current
            self.state["leader_client_id"] = client_id
            if initial_time is not None:
                self.state["time"] = float(initial_time)
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
        with self.lock:
            leader = self.state["leader_client_id"] or ""
            if leader != client_id:
                return False, f"not the playback leader (current: {leader})", leader
            if action == "play":
                self.state["playing"] = True
            elif action == "pause":
                self.state["playing"] = False
            elif action == "stop":
                self.state["playing"] = False
                self.state["time"] = 0.0
            elif action == "set_time":
                self.state["time"] = float(time_value)
            elif action == "set_rate":
                self.state["rate"] = float(rate)
            else:
                return False, f"unknown playback action {action!r}", leader
            return True, self._snapshot_locked(), leader

    def release(self, client_id: str) -> bool:
        """Release leadership on disconnect, retaining the current playhead."""
        if not client_id:
            return False
        with self.lock:
            if self.state["leader_client_id"] == client_id:
                self.state["leader_client_id"] = None
                return True
            return False
