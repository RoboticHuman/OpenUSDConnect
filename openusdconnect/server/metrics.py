"""Thread-safe server wire traffic counters."""

import threading


class WireMetrics:
    """Thread-safe logical-record and actual transport byte counters."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._bytes: dict[str, int] = {}
        self._transport_counts: dict[str, int] = {}
        self._transport_bytes: dict[str, int] = {}

    def record(self, kind: str, nbytes: int) -> None:
        with self._lock:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._bytes[kind] = self._bytes.get(kind, 0) + nbytes

    def record_transport(self, channel: str, nbytes: int, *, count: int = 1) -> None:
        """Record framed bytes actually read or successfully written."""
        with self._lock:
            self._transport_counts[channel] = (
                self._transport_counts.get(channel, 0) + count
            )
            self._transport_bytes[channel] = (
                self._transport_bytes.get(channel, 0) + nbytes
            )

    def snapshot(self) -> dict:
        with self._lock:
            kinds = {
                k: {"count": self._counts[k], "bytes": self._bytes[k]} for k in sorted(self._counts)
            }
            transport = {
                channel: {
                    "count": self._transport_counts[channel],
                    "bytes": self._transport_bytes[channel],
                }
                for channel in sorted(self._transport_counts)
            }
        return {
            "kinds": kinds,
            "total_count": sum(v["count"] for v in kinds.values()),
            "total_bytes": sum(v["bytes"] for v in kinds.values()),
            "transport": transport,
            "transport_total_count": sum(v["count"] for v in transport.values()),
            "transport_total_bytes": sum(v["bytes"] for v in transport.values()),
        }
