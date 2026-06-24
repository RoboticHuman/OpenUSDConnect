"""MCP (Model Context Protocol) server for OpenUSDConnect.

Exposes the OpenUSDConnect event protocol as MCP tools so an LLM can author
USD scenes (procedural meshes, references/payloads, transforms, instancing,
and UsdShade.ConnectableAPI shader networks (UsdPreviewSurface + MaterialX))
and stream them to the sync server, which fans them out to every connected DCC.

The server is a network client built on the core library (``EventSender`` +
``ReceiverThread`` + ``EventDispatcher`` + ``UsdStageAdapter``), the same shape
as the ``usdview`` integration. It introduces no protocol changes.

Launch with ``uv run python -m integrations.mcp`` (stdio transport).

Submodules are deliberately importable in isolation (this package ``__init__``
pulls in nothing heavy) so unit tests can import ``registry``/``validation``
without constructing a FastMCP server. The CLI imports ``tools.build_server``.
"""

from __future__ import annotations
