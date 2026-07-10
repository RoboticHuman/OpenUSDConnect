"""Error type and result-envelope helpers for the MCP tools."""

from __future__ import annotations


class ToolError(Exception):
    """An invalid request the caller can fix from the message.

    Carries a machine-readable ``code`` plus optional locating context so the
    error Claude sees points at the exact event/field to correct. Tool wrappers
    catch this and return :meth:`to_dict`; unexpected exceptions propagate.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_request",
        event_index: int | None = None,
        field: str | None = None,
        hint: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.event_index = event_index
        self.field = field
        self.hint = hint

    def to_dict(self) -> dict:
        err: dict = {"code": self.code, "message": str(self)}
        if self.event_index is not None:
            err["event_index"] = self.event_index
        if self.field is not None:
            err["field"] = self.field
        if self.hint is not None:
            err["hint"] = self.hint
        return {"ok": False, "error": err}


def ok(**payload) -> dict:
    """Build a success result envelope."""
    return {"ok": True, **payload}
