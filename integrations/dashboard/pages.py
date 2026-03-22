"""NiceGUI dashboard pages for the OpenUSDConnect server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nicegui import ui

if TYPE_CHECKING:
    from openusdconnect.server import UsdSyncServer

DASHBOARD_STYLE = """
    .q-page { height: 100vh; }
    .nicegui-content {
        max-width: 1100px; margin: 0 auto;
        height: 100%; display: flex; flex-direction: column;
    }
    .event-feed-scroll { flex: 1; min-height: 0; }

    /* JSON editor theme — desaturated to match dashboard */
    body.body--dark .jse-theme-dark {
        --jse-theme-color: #7eb8da;
        --jse-background-color: #1a1f27;
        --jse-text-color: #c8cdd3;
        --jse-main-border: 1px solid #2a303a;
        --jse-panel-background: #14181e;
        --jse-panel-color: #7a838c;
        --jse-panel-border: 1px solid #2a303a;
        --jse-key-color: #88b4cc;
        --jse-value-color-string: #9dbfcc;
        --jse-value-color-number: #b8a0d2;
        --jse-value-color-boolean: #cc8a84;
        --jse-value-color-null: #7a838c;
        --jse-delimiter-color: #454c56;
    }
    body.body--light .jse-theme-dark {
        --jse-theme-color: #2b6fa0;
        --jse-background-color: #ffffff;
        --jse-text-color: #2c333a;
        --jse-main-border: 1px solid #d4dae0;
        --jse-panel-background: #f5f7f9;
        --jse-panel-color: #5e6872;
        --jse-panel-border: 1px solid #d4dae0;
        --jse-key-color: #2b6fa0;
        --jse-value-color-string: #1a5276;
        --jse-value-color-number: #6e5b96;
        --jse-value-color-boolean: #a33b33;
        --jse-value-color-null: #5e6872;
    }

    /* Theme-aware colors — desaturated for low eye strain */
    .dash-accent { color: var(--dash-accent); }
    .dash-muted { color: var(--dash-muted) !important; }
    .dash-kind { color: var(--dash-kind); }
    .dash-prim { color: var(--dash-prim); }
    body.body--dark {
        --dash-accent: #7eb8da;
        --dash-muted: #7a838c;
        --dash-kind: #b8a0d2;
        --dash-prim: #88b4cc;
        --q-dark: #14181e;
        --q-dark-page: #10141a;
    }
    body.body--light {
        --dash-accent: #2b6fa0;
        --dash-muted: #5e6872;
        --dash-kind: #6e5b96;
        --dash-prim: #2b6fa0;
    }

    /* Dark mode surface overrides */
    body.body--dark .q-card {
        background: #1a1f27 !important;
        border: 1px solid #2a303a;
    }
    body.body--dark .q-table {
        background: #1a1f27 !important;
    }
    body.body--dark .q-expansion-item {
        border-color: #2a303a !important;
    }
"""


def setup_pages(srv: UsdSyncServer):
    """Register all dashboard pages and API routes."""

    @ui.page("/")
    def index():
        ui.add_css(DASHBOARD_STYLE)
        dark = ui.dark_mode(True)

        # Header
        with ui.row().classes("items-center w-full mb-4"):
            ui.icon("hub", size="sm").classes("dash-accent")
            ui.label("OpenUSDConnect Dashboard").classes(
                "text-lg font-semibold dash-accent"
            )
            ui.space()
            ui.button(
                icon="dark_mode",
                on_click=lambda: dark.set_value(not dark.value),
            ).props("flat round dense size=sm")

        _build_server_info(srv)
        _build_status_cards(srv)
        _build_operations(srv)
        _build_clients_table(srv)
        _build_prim_tree(srv)
        _build_event_feed(srv)

    _register_api_routes(srv)


def _build_server_info(srv: UsdSyncServer):
    """Server configuration summary."""
    info = srv.get_server_info()
    base = info["base_usd_path"] or "In-memory stage"
    with ui.row().classes("gap-4 items-center mb-2 text-xs"):
        ui.icon("info", size="xs").classes("dash-muted")
        ui.label(f"Base: {base}").classes("dash-muted font-mono")


def _build_prim_tree(srv: UsdSyncServer):
    """Stage prim tree reconstructed from the event log."""
    ui.label("STAGE PRIMS").classes(
        "text-xs font-semibold dash-muted uppercase mb-1"
    )

    tree_container = ui.column().classes("w-full")

    def _build_tree_nodes(prims: list[dict]) -> list[dict]:
        """Convert flat prim list to NiceGUI ui.tree node format."""
        nodes_by_path: dict[str, dict] = {}
        for p in prims:
            nodes_by_path[p["path"]] = {
                "id": p["path"],
                "label": p["path"].rsplit("/", 1)[-1] + f"  ({p['typeName']})",
                "children": [],
            }
        roots = []
        for p in prims:
            node = nodes_by_path[p["path"]]
            parent_path = p["parent"]
            if parent_path in nodes_by_path:
                nodes_by_path[parent_path]["children"].append(node)
            else:
                roots.append(node)
        return roots

    def refresh():
        tree_container.clear()
        prims = srv.get_prim_tree()
        if not prims:
            with tree_container:
                ui.label("No prims").classes("dash-muted text-sm")
            return
        nodes = _build_tree_nodes(prims)
        with tree_container:
            ui.tree(
                nodes, node_key="id", label_key="label",
            ).props("dense default-expand-all").classes(
                "w-full text-xs font-mono"
            )

    with ui.row().classes("mb-2"):
        ui.button(
            "Refresh", icon="refresh", on_click=refresh,
        ).props("outline dense no-caps size=sm")

    refresh()


def _build_status_cards(srv: UsdSyncServer):
    """Four stat cards."""
    with ui.row().classes("w-full gap-4 mb-4"):
        with ui.card().classes("flex-1"):
            ui.label("UPTIME").classes("text-xs dash-muted uppercase")
            uptime_val = ui.label("--").classes("text-2xl font-bold dash-accent")
        with ui.card().classes("flex-1"):
            ui.label("EVENTS").classes("text-xs dash-muted uppercase")
            event_val = ui.label("--").classes("text-2xl font-bold dash-accent")
        with ui.card().classes("flex-1"):
            ui.label("CLIENTS").classes("text-xs dash-muted uppercase")
            client_val = ui.label("--").classes("text-2xl font-bold dash-accent")
        with ui.card().classes("flex-1"):
            ui.label("PRIMS").classes("text-xs dash-muted uppercase")
            prim_val = ui.label("--").classes("text-2xl font-bold dash-accent")

    def _fmt(s):
        h, r = divmod(int(s), 3600)
        m, s = divmod(r, 60)
        return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"

    def refresh():
        uptime_val.text = _fmt(srv.get_uptime())
        event_val.text = f"{srv.get_event_count():,}"
        client_val.text = str(len(srv.get_client_list()))
        prim_val.text = str(srv.get_prim_count())

    ui.timer(2.0, refresh)
    refresh()


def _build_operations(srv: UsdSyncServer):
    """Operation buttons in a row."""

    async def compact():
        before = srv.get_event_count()
        srv.compact_log()
        after = srv.get_event_count()
        ui.notify(f"Compacted: {before} → {after} events", type="positive")

    async def export_diff():
        ui.download(srv.export_edit_layer().encode(), "edit_layer.usda")

    async def export_flatten():
        ui.download(srv.export_flattened_string().encode(), "flattened.usda")

    with ui.row().classes("gap-2 mb-4"):
        ui.button("Compact Log", icon="compress", on_click=compact).props(
            "outline dense no-caps"
        )
        ui.button("Export Diff", icon="difference", on_click=export_diff).props(
            "outline dense no-caps"
        )
        ui.button("Export Flat", icon="download", on_click=export_flatten).props(
            "outline dense no-caps"
        )


def _build_clients_table(srv: UsdSyncServer):
    """Client roster."""
    ui.label("CLIENTS").classes("text-xs font-semibold dash-muted uppercase mb-1")

    columns = [
        {"name": "role", "label": "Role", "field": "role", "align": "left"},
        {"name": "id", "label": "Client ID", "field": "client_id",
         "align": "left"},
        {"name": "events", "label": "Events", "field": "event_count",
         "align": "right"},
        {"name": "last", "label": "Last Activity",
         "field": "last_activity", "align": "right"},
    ]
    table = ui.table(
        columns=columns, rows=[], row_key="key",
    ).props("dense flat bordered").classes("w-full mb-4")

    def refresh():
        clients = srv.get_client_list()
        table.rows = [
            {
                "key": c["key"],
                "role": c["role"],
                "client_id": c["client_id"] or "—",
                "event_count": f"{c['event_count']:,}",
                "last_activity": f"{c['last_activity_ago']:.0f}s ago",
            }
            for c in clients
        ]

    ui.timer(3.0, refresh)
    refresh()


def _build_event_feed(srv: UsdSyncServer):
    """Paginated event feed with inline JSON detail."""
    ui.label("EVENT LOG").classes(
        "text-xs font-semibold dash-muted uppercase mb-1"
    )

    ALL_KINDS = [
        "ensure_prim", "ensure_xform_ops", "set_xform_trs",
        "set_visibility", "set_gprim_attrs", "set_reference",
        "set_payload", "load_payload", "unload_payload",
        "set_variant_selections", "delete_prim", "deactivate_prim",
        "rename_prim",
    ]
    kind_opts = {"": "All kinds", **{k: k for k in ALL_KINDS}}
    page_sizes = {25: "25", 50: "50", 100: "100", 200: "200"}
    current_page = {"value": 0}

    with ui.row().classes("gap-2 items-end mb-2"):
        kind_filter = ui.select(
            options=kind_opts, value="",
        ).props('dense outlined label="Kind"').classes("w-44")
        prim_filter = ui.input(
            placeholder="Filter prim path…",
        ).props("dense outlined").classes("w-56")
        ui.space()
        page_size_sel = ui.select(
            options=page_sizes, value=50,
        ).props('dense outlined label="Per page"').classes("w-24")

    with ui.row().classes("gap-1 items-center mb-2"):
        btn_prev = ui.button(
            icon="chevron_left", on_click=lambda: _go(-1),
        ).props("flat dense round size=sm")
        page_label = ui.label("—").classes("text-xs dash-muted")
        btn_next = ui.button(
            icon="chevron_right", on_click=lambda: _go(1),
        ).props("flat dense round size=sm")

    feed_scroll = ui.scroll_area().classes("w-full event-feed-scroll")
    feed_container = ui.column().classes("w-full gap-0")
    feed_container.move(feed_scroll)

    def _query():
        limit = page_size_sel.value
        offset = current_page["value"] * limit
        return srv.query_events(
            offset=offset, limit=limit,
            kind=kind_filter.value, prim_contains=prim_filter.value,
        )

    def _expansion(rec):
        ev = rec.get("event", {})
        k = ev.get("k", "?")
        prim = ev.get("prim", "")
        seq = rec.get("seq", "?")
        client = rec.get("client_id") or rec.get("client", "")

        exp = ui.expansion(icon="chevron_right").classes("w-full")
        with exp.add_slot("header"):
            with ui.row().classes(
                "items-center gap-3 w-full font-mono text-xs"
            ):
                ui.label(str(seq)).classes("dash-muted w-10 text-right")
                ui.label(k).classes("dash-kind w-44")
                ui.label(prim).classes("dash-prim flex-1 truncate")
                ui.label(client).classes(
                    "dash-muted w-36 text-right truncate"
                )
        with exp:
            editor = ui.json_editor(
                {"content": {"json": ev}},
            ).classes("w-full jse-theme-dark")
            editor.run_editor_method("updateProps", {"readOnly": True})

    def _rebuild():
        recs, total = _query()
        limit = page_size_sel.value
        pages = max(1, (total + limit - 1) // limit)
        if current_page["value"] >= pages:
            current_page["value"] = max(0, pages - 1)
        feed_container.clear()
        with feed_container:
            if not recs:
                ui.label("No events").classes(
                    "dash-muted text-sm py-4 text-center"
                )
            else:
                for r in recs:
                    _expansion(r)
        p = current_page["value"]
        page_label.text = f"{p + 1} / {pages}  •  {total:,} total"
        btn_prev.set_enabled(p > 0)
        btn_next.set_enabled(p < pages - 1)

    def _go(delta):
        current_page["value"] += delta
        _rebuild()

    def _reset(_=None):
        current_page["value"] = 0
        _rebuild()

    kind_filter.on_value_change(_reset)
    prim_filter.on("keydown.enter", _reset)
    page_size_sel.on_value_change(_reset)

    _rebuild()

    # Live event listener — called from the TCP server thread, so we
    # must NOT touch NiceGUI UI elements directly.  Instead, set a flag
    # and let a timer on the NiceGUI thread pick it up.
    _has_new_events = {"value": False}

    def _on_live(_rec):
        _has_new_events["value"] = True

    def _check_live():
        if _has_new_events["value"] and current_page["value"] == 0:
            _has_new_events["value"] = False
            _rebuild()

    ui.timer(1.0, _check_live)
    srv.add_event_listener(_on_live)

    # Clean up listener when browser tab disconnects
    from nicegui import app
    app.on_disconnect(lambda: srv.remove_event_listener(_on_live))


def _register_api_routes(srv: UsdSyncServer):
    """REST API for scripting."""
    from nicegui import app

    @app.get("/api/status")
    def api_status():
        return {
            "uptime_seconds": round(srv.get_uptime(), 1),
            "event_count": srv.get_event_count(),
            "client_count": len(srv.get_client_list()),
            "prim_count": srv.get_prim_count(),
        }

    @app.get("/api/clients")
    def api_clients():
        return srv.get_client_list()

    @app.get("/api/events")
    def api_events(offset: int = 0, limit: int = 100):
        events, _total = srv.query_events(offset=offset, limit=min(limit, 1000))
        return events

    @app.get("/api/server-info")
    def api_server_info():
        return srv.get_server_info()

    @app.get("/api/prim-tree")
    def api_prim_tree():
        return srv.get_prim_tree()
