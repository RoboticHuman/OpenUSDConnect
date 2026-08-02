"""FastMCP server assembly: register every tool over one ConnectionSession.

All tool logic lives in ``session``/``registry``/``validation``/``introspection``
/``discovery`` as plain functions; this module only adapts them to MCP tools and
converts :class:`ToolError` into a structured ``{ok: false, error}`` result.
Unexpected exceptions propagate so real bugs stay visible.
"""

import functools
import inspect
import typing

from . import discovery, introspection
from .config import McpConfig
from .errors import ToolError
from .registry import TOOL_TABLE
from .session import ConnectionSession
from .validation import validate_and_prepare

if typing.TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def _catch(fn):
    """Wrap a tool so ToolError becomes a structured result, preserving signature."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return exc.to_dict()

    return wrapper


def _make_kind_tool(row, author):
    """Build an MCP tool for one event kind with the builder's resolved signature."""
    build = row.build
    sig = inspect.signature(build)
    hints = typing.get_type_hints(build)
    params = [
        p.replace(annotation=hints.get(name, p.annotation)) for name, p in sig.parameters.items()
    ]
    resolved_sig = sig.replace(
        parameters=params, return_annotation=hints.get("return", sig.return_annotation)
    )

    def tool(**kwargs):
        try:
            event = build(**kwargs)
        except ToolError as exc:
            return exc.to_dict()
        except (ValueError, TypeError) as exc:
            return ToolError(str(exc), code="invalid_request").to_dict()
        try:
            return author([event])
        except ToolError as exc:
            return exc.to_dict()

    tool.__name__ = f"usd_{row.kind}"
    tool.__doc__ = row.summary
    tool.__signature__ = resolved_sig
    tool.__annotations__ = dict(hints)
    return tool


def build_server(config: McpConfig | None = None) -> "FastMCP":
    """Construct the FastMCP server with all OpenUSDConnect tools registered.

    FastMCP is imported lazily so the package stays importable without the
    optional 'mcp' dependency group; only building/running the server needs it."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(
            "The MCP server requires the 'mcp' dependency group. "
            "Install with: uv sync --group server --group mcp"
        ) from exc
    config = config or McpConfig.from_env()
    session = ConnectionSession(config)
    mcp = FastMCP("openusdconnect")

    def _ensure_connected():
        if session.connected:
            return
        if config.auto_connect:
            session.connect()
        else:
            raise ToolError("not connected, call usd_connect first", code="not_connected")

    def _author(events: list) -> dict:
        _ensure_connected()
        prepared, warnings = validate_and_prepare(
            events,
            stage=session.mirror_stage,
            auto_create_ancestors=config.auto_create_ancestors,
            node_exists=discovery.node_exists,
        )
        result = session.send(prepared)
        result["ok"] = True
        if warnings:
            result["warnings"] = warnings
        added = len(prepared) - len(events)
        if added:
            result["ancestors_created"] = added
        return result

    # -- session / lifecycle ----------------------------------------------

    @mcp.tool()
    @_catch
    def usd_connect(
        host: str = None,
        port: int = None,
        client_id: str = None,
        department: str = None,
    ) -> dict:
        """Connect to the OpenUSDConnect sync server and start the read-only
        mirror stage. Uses env defaults (OPENUSDCONNECT_HOST/PORT) when args are
        omitted. Authoring/introspection tools auto-connect, so calling this is
        optional unless you want to target a specific server."""
        return session.connect(host, port, client_id, department)

    @mcp.tool()
    def usd_status() -> dict:
        """Report connection state, mirror prim count, last applied sequence, and
        stage metadata."""
        return session.status()

    @mcp.tool()
    def usd_disconnect() -> dict:
        """Disconnect from the server and tear down the mirror stage."""
        return session.disconnect()

    # -- generic escape hatch ---------------------------------------------

    @mcp.tool()
    @_catch
    def usd_send_events(events: list) -> dict:
        """Validate and send a list of raw event dicts as ONE atomic transaction.

        This is the primary tool for composite authoring (a whole mesh or shader
        network at once). Each event is a dict with a 'k' field naming its kind
        (see the per-kind usd_* tools for the shape of each). Validation is
        all-or-nothing: any invalid event aborts the batch and nothing is sent.
        Missing ancestor prims are auto-created as Xform when a mirror is active.
        """
        return _author(events)

    # -- per-kind authoring tools (table-driven) --------------------------

    for kind, row in TOOL_TABLE.items():
        if not row.expose:
            continue
        mcp.add_tool(_make_kind_tool(row, _author), name=f"usd_{kind}", description=row.summary)

    # -- introspection (mirror-backed) ------------------------------------

    @mcp.tool()
    @_catch
    def usd_list_prims(
        under: str = "/",
        type_name: str = "",
        is_a: str = "",
        max: int = 500,
        offset: int = 0,
        depth: int = 0,
    ) -> dict:
        """List prims under a path as {path, type, active}. Filter by exact
        type_name and/or schema base is_a ('UsdGeomMesh','UsdGeomGprim',
        'UsdGeomImageable', ...). depth limits descent (0=all, 1=under plus its
        immediate children). Page large scenes with offset/max; pass the returned
        next_offset back as offset for the next page (null when exhausted)."""
        return introspection.list_prims(
            session.require_mirror(), under, type_name, is_a, max, offset, depth
        )

    @mcp.tool()
    @_catch
    def usd_get_prim(path: str, fields: list = None) -> dict:
        """Read a prim: type, api_schemas, transform (t/r/s), authored attributes
        (long arrays summarized), variant selections, and material binding. Pass
        fields (e.g. ['xform','material_binding']) to return only those sections
        for leaner replies; path/type/active are always included."""
        return introspection.get_prim(session.require_mirror(), path, fields)

    @mcp.tool()
    @_catch
    def usd_get_prims(paths: list, fields: list = None) -> dict:
        """Batch usd_get_prim: read many prims in one call (pairs with
        usd_changes_since). Each entry has the get_prim shape; an unresolved path
        yields an error entry rather than failing the batch."""
        return introspection.get_prims(session.require_mirror(), paths, fields)

    @mcp.tool()
    @_catch
    def usd_scene_summary(under: str = "/") -> dict:
        """Cheap orientation for a large scene: total/active/material counts, max
        depth, and a count-by-type histogram. Use before listing to size up the
        scene without dumping every prim."""
        return introspection.scene_summary(session.require_mirror(), under)

    @mcp.tool()
    @_catch
    def usd_changes_since(since_seq: int = 0, max: int = 500) -> dict:
        """Prims changed since a sequence number (oldest first) -- efficient diffs
        on large scenes. Poll with the last_seq from a prior call or usd_status to
        find just what you and other clients touched, then usd_get_prims those
        paths instead of re-listing the whole scene."""
        return session.changes_since(since_seq, max)

    @mcp.tool()
    @_catch
    def usd_get_bounds(path: str) -> dict:
        """World-space bounding box of a prim and its subtree as {min, max,
        center, size}. Composes the full transform chain (scale, nesting,
        references), so use it to place objects relative to each other (beside /
        on top of / aligned with) without fetching geometry or doing transform
        math by hand."""
        return introspection.get_bounds(session.require_mirror(), path)

    @mcp.tool()
    @_catch
    def usd_get_attributes(path: str, names: list = None, max_items: int = 12) -> dict:
        """Read just the attributes you need. With names: returns {name: value}
        for those only (arrays summarized to max_items; raise it for more, or to
        pull a whole array). Without names: a cheap index of authored attributes
        (name, type, array length) with no values, to see what is there first."""
        return introspection.get_attributes(session.require_mirror(), path, names, max_items)

    @mcp.tool()
    @_catch
    def usd_describe_shader_network(material_path: str) -> dict:
        """Describe the UsdShade topology under a Material: each shader's info_id,
        input values/types, and connection edges."""
        return introspection.describe_shader_network(session.require_mirror(), material_path)

    @mcp.tool()
    @_catch
    def usd_get_stage_metadata() -> dict:
        """Read stage units and timeline (upAxis, metersPerUnit, fps, time range)."""
        return introspection.get_stage_metadata(session.require_mirror())

    # -- Sdr shader-node discovery (no connection needed) -----------------

    @mcp.tool()
    @_catch
    def usd_list_shader_nodes(filter: str = "", source_type: str = "", max: int = 200) -> dict:
        """Discover shader node ids. source_type 'mtlx' (MaterialX), 'glslfx'
        (UsdPreviewSurface family), or 'USD'. filter is a substring on the id."""
        return discovery.list_shader_nodes(filter or None, source_type or None, max)

    @mcp.tool()
    @_catch
    def usd_describe_shader_node(info_id: str) -> dict:
        """Get a shader node's exact input/output names + Sdf types (and input
        defaults), e.g. 'UsdPreviewSurface' or 'ND_standard_surface_surfaceshader'.
        Use this to author correct info_id, input_types, and connection ends."""
        return discovery.describe_shader_node(info_id)

    # -- playback ----------------------------------------------------------

    @mcp.tool()
    @_catch
    def usd_claim_playback(time: float = None) -> dict:
        """Claim the single playback-leader role (optionally setting the time)."""
        return session.claim_playback(time)

    @mcp.tool()
    @_catch
    def usd_playback_control(action: str, time: float = None, rate: float = None) -> dict:
        """Drive shared playback (leader only): action play|pause|stop|set_time|
        set_rate; time for set_time; rate for set_rate."""
        return session.playback_control(action, time, rate)

    @mcp.tool()
    @_catch
    def usd_playback_status() -> dict:
        """Read the shared playhead: playing, time, rate, the leader's client id,
        and whether this client is the leader. Reflects the latest PlaybackState
        the server broadcast (claim/drive it with usd_claim_playback /
        usd_playback_control). 'observed' is False until the first state arrives."""
        return session.playback_status()

    mcp.usdconnect_session = session  # exposed for tests/diagnostics
    return mcp
