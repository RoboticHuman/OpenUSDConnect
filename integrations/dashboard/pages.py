"""NiceGUI dashboard pages for the OpenUSDConnect server."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING

from nicegui import ui

from openusdconnect.protocol_constants import EVENT_KEYS
from openusdconnect.server.types import ReplayModeConflictError

if TYPE_CHECKING:
    from openusdconnect.server import UsdSyncServer

DASHBOARD_STYLE = """
    .nicegui-content {
        max-width: 1400px; margin: 0 auto;
        padding-bottom: 2rem;
    }
    .usda-viewer {
        font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
        font-size: 12px;
        line-height: 1.4;
        white-space: pre;
        tab-size: 4;
        overflow: auto;
    }

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

        # Shared refresh registry — sections register callbacks,
        # single timer drives them all. Heavier panels that normally
        # refresh manually register in the full registry instead; the
        # timer runs those only when the refresh-all toggle is on.
        _refresh_callbacks: list[Callable] = []
        _full_refresh_callbacks: list[Callable] = []
        _refresh_interval = {"value": 5.0}
        _refresh_all = {"value": False}

        def register_refresh(fn):
            _refresh_callbacks.append(fn)

        def register_full_refresh(fn):
            _full_refresh_callbacks.append(fn)

        def _tick():
            if _refresh_interval["value"] <= 0:
                return
            for fn in _refresh_callbacks:
                fn()
            if _refresh_all["value"]:
                for fn in _full_refresh_callbacks:
                    fn()

        timer = ui.timer(5.0, _tick)

        def _set_rate(val):
            _refresh_interval["value"] = val
            if val <= 0:
                timer.deactivate()
            else:
                timer.interval = val
                timer.activate()

        # Header
        with ui.row().classes("items-center w-full mb-4"):
            ui.icon("hub", size="sm").classes("dash-accent")
            ui.label("OpenUSDConnect Dashboard").classes(
                "text-lg font-semibold dash-accent"
            )
            ui.space()
            ui.select(
                {0: "Off", 2: "2s", 5: "5s", 10: "10s", 30: "30s"},
                value=5,
                on_change=lambda e: _set_rate(e.value),
            ).props('dense outlined label="Auto-refresh"').classes("w-28")

            def _toggle_refresh_all():
                _refresh_all["value"] = not _refresh_all["value"]
                if _refresh_all["value"]:
                    refresh_all_btn.props(add="color=primary")
                    for fn in _full_refresh_callbacks:
                        fn()
                else:
                    refresh_all_btn.props(remove="color=primary")

            refresh_all_btn = ui.button(
                icon="sync", on_click=_toggle_refresh_all,
            ).props("flat round dense size=sm")
            refresh_all_btn.tooltip(
                "Refresh ALL panels on the auto-refresh timer, including the "
                "manual ones (composed stage, prim tree, proposals)"
            )
            ui.button(
                icon="dark_mode",
                on_click=lambda: dark.set_value(not dark.value),
            ).props("flat round dense size=sm")

        feed_api: dict = {}

        def _focus_prim(path):
            focus = feed_api.get("focus")
            if focus:
                focus(path)

        _build_server_info(srv)
        _build_stage_metadata(srv, register_refresh)
        _build_status_cards(srv, register_refresh)
        _build_operations(srv)
        _build_layer_stack(srv, register_refresh, register_full_refresh)
        _build_proposals_panel(srv, register_refresh, register_full_refresh)
        _build_clients_table(srv, register_refresh)
        _build_wire_metrics(srv, register_refresh)
        _build_prim_tree(srv, on_focus=_focus_prim,
                         register_full_refresh=register_full_refresh)
        _build_event_feed(srv, register_refresh, feed_api=feed_api)

    _register_api_routes(srv)


def _build_server_info(srv: UsdSyncServer):
    """Server configuration summary."""
    info = srv.get_server_info()
    base = info["base_usd_path"] or "In-memory stage"
    with ui.row().classes("gap-4 items-center mb-2 text-xs"):
        ui.icon("info", size="xs").classes("dash-muted")
        ui.label(f"Base: {base}").classes("dash-muted font-mono")
        if srv.require_token:
            ui.badge("TOFU AUTH", color="positive").props("dense")


def _build_stage_metadata(srv: UsdSyncServer, register_refresh=None):
    """Compact strip of authored stage units + timeline metadata."""
    UNIT_NAMES = {1.0: "m", 0.01: "cm", 0.001: "mm", 0.0254: "in", 0.3048: "ft"}
    row = ui.row().classes("gap-4 items-center mb-3 text-xs flex-wrap")

    def _chip(icon, text):
        with ui.row().classes("items-center gap-1"):
            ui.icon(icon, size="xs").classes("dash-muted")
            ui.label(text).classes("dash-muted font-mono")

    def refresh():
        meta = srv.get_stage_metadata_payload()
        row.clear()
        with row:
            if not meta:
                ui.label("No authored stage metadata").classes("dash-muted")
                return
            if meta.get("upAxis"):
                _chip("height", f"Up: {meta['upAxis']}")
            mpu = meta.get("metersPerUnit")
            if mpu is not None:
                unit = UNIT_NAMES.get(round(float(mpu), 6))
                _chip("straighten", f"Units: {unit}" if unit else f"metersPerUnit: {mpu:g}")
            fps = meta.get("framesPerSecond")
            tcps = meta.get("timeCodesPerSecond")
            if fps is not None:
                label = f"FPS: {fps:g}"
                if tcps is not None and abs(float(tcps) - float(fps)) > 1e-6:
                    label += f"  (tcps {tcps:g})"
                _chip("movie", label)
            elif tcps is not None:
                _chip("movie", f"tcps: {tcps:g}")
            start = meta.get("startTimeCode")
            end = meta.get("endTimeCode")
            if start is not None and end is not None:
                _chip("schedule", f"Frames: {start:g}–{end:g}")

    if register_refresh:
        register_refresh(refresh)
    refresh()


def _fmt_vec(v) -> str:
    return "(" + ", ".join(f"{x:g}" for x in v) + ")"


def _render_instancing_block(detail: dict):
    """Render instancing flags plus PointInstancer summary when present."""
    flags = []
    if detail.get("isInstance"):
        flags.append(("INSTANCE", "positive"))
    elif detail.get("isInstanceable"):
        flags.append(("INSTANCEABLE", "warning"))
    if detail.get("isInstanceProxy"):
        flags.append(("PROXY", "grey-7"))
    proto = detail.get("prototype")
    pi = detail.get("pointInstancer")
    if not (flags or proto or pi):
        return
    with ui.row().classes("gap-2 items-center text-xs flex-wrap mt-1"):
        for label, color in flags:
            ui.badge(label, color=color).props("dense")
        if proto:
            with ui.row().classes("items-center gap-1"):
                ui.icon("device_hub", size="xs").classes("dash-muted")
                ui.label("prototype").classes("dash-muted")
                ui.label(proto).classes("dash-prim font-mono")
    if pi:
        with ui.column().classes("w-full gap-1 mt-1 p-2").style(
            "border: 1px solid rgba(128,128,128,0.18); border-radius: 4px"
        ):
            with ui.row().classes("items-center gap-2 text-xs"):
                ui.icon("grain", size="xs").classes("dash-accent")
                ui.label("POINT INSTANCER").classes(
                    "text-xs font-semibold dash-accent uppercase"
                )
                ui.space()
                ui.label(f"{pi.get('instanceCount', 0):,} instances").classes(
                    "dash-muted font-mono"
                )
            protos = pi.get("prototypes") or []
            if protos:
                _i_arc = ui.row().classes("items-baseline gap-2 text-xs font-mono w-full")
                with _i_arc:
                    ui.icon("link", size="xs").classes("dash-muted")
                    ui.label(f"prototypes ({len(protos)})").classes("dash-muted")
                    ui.label(", ".join(protos)).classes("dash-prim flex-1 truncate")
            animated = pi.get("animatedArrays") or []
            if animated:
                with ui.row().classes("items-baseline gap-2 text-xs font-mono w-full"):
                    ui.icon("schedule", size="xs").classes("dash-muted")
                    ui.label("animated").classes("dash-muted")
                    ui.label(", ".join(animated)).classes("dash-kind flex-1 truncate")
            inactive = pi.get("inactiveIdCount")
            if inactive:
                with ui.row().classes("items-baseline gap-2 text-xs font-mono w-full"):
                    ui.icon("visibility_off", size="xs").classes("dash-muted")
                    ui.label("inactiveIds").classes("dash-muted")
                    ui.label(str(inactive)).classes("dash-prim")


def _render_prim_detail(detail: dict):
    """Render a get_prim_detail() dict into the current container."""
    if not detail.get("exists"):
        ui.label("Prim not on stage").classes("dash-muted text-sm")
        return

    with ui.row().classes("items-center gap-2 w-full"):
        active = detail.get("active", True)
        ui.icon("circle", size="xs").classes(
            "text-positive" if active else "text-grey-6"
        ).tooltip("active" if active else "inactive")
        ui.label(detail["path"]).classes(
            "dash-prim font-mono text-sm font-semibold flex-1 truncate"
        )
        if detail.get("typeName"):
            ui.badge(detail["typeName"]).props("dense")

    if detail.get("visibility") or detail.get("apiSchemas"):
        with ui.row().classes("gap-2 items-center text-xs flex-wrap"):
            if detail.get("visibility"):
                ui.icon("visibility", size="xs").classes("dash-muted")
                ui.label(detail["visibility"]).classes("dash-muted font-mono")
            for s in detail.get("apiSchemas", []):
                ui.badge(s, color="primary").props("dense outline")

    xf = detail.get("xform")
    if xf:
        with ui.row().classes("gap-3 items-baseline font-mono text-xs"):
            if xf.get("t"):
                ui.label(f"T {_fmt_vec(xf['t'])}").classes("dash-accent")
            if xf.get("r"):
                ui.label(f"R {_fmt_vec(xf['r'])}").classes("dash-kind")
            if xf.get("s"):
                ui.label(f"S {_fmt_vec(xf['s'])}").classes("dash-muted")

    def _arc_line(icon, label, items):
        if not items:
            return
        with ui.row().classes("items-baseline gap-2 text-xs font-mono w-full"):
            ui.icon(icon, size="xs").classes("dash-muted")
            ui.label(label).classes("dash-muted")
            ui.label(", ".join(items)).classes("dash-prim flex-1 truncate")

    _arc_line("link", "refs", detail.get("references", []))
    _arc_line("download", "payloads", detail.get("payloads", []))
    vsel = detail.get("variantSelections") or {}
    if vsel:
        _arc_line("tune", "variants", [f"{k}={v}" for k, v in vsel.items()])
    if detail.get("materialBinding"):
        _arc_line("palette", "material", [detail["materialBinding"]])

    _render_instancing_block(detail)

    attrs = detail.get("attributes", [])
    ui.separator().classes("my-1")
    with ui.row().classes("items-center gap-2"):
        ui.label("AUTHORED ATTRIBUTES").classes(
            "text-xs font-semibold dash-muted uppercase"
        )
        ui.label(str(len(attrs))).classes("text-xs dash-muted")
        ui.icon("info", size="xs").classes("dash-muted").tooltip(
            "Attributes with an authored opinion — schema defaults not shown"
        )
    if not attrs:
        ui.label("No authored attributes").classes("dash-muted text-xs")
    for a in attrs:
        with ui.row().classes(
            "items-baseline gap-2 w-full font-mono text-xs py-0.5"
        ).style("border-bottom: 1px solid rgba(128,128,128,0.12)"):
            ui.label(a["name"]).classes("dash-prim").style("min-width: 9rem")
            ui.label(a["type"]).classes("dash-muted").style("min-width: 6rem")
            ui.label(a["value"]).classes("dash-kind flex-1 truncate")
            if a.get("numTimeSamples"):
                with ui.row().classes("items-center gap-0.5").tooltip(
                    f"{a['numTimeSamples']} time samples"
                ):
                    ui.icon("schedule", size="xs").classes("dash-muted")
                    ui.label(str(a["numTimeSamples"])).classes("dash-muted")


def _build_prim_tree(srv: UsdSyncServer, on_focus=None, register_full_refresh=None):
    """Stage prim tree with a composed-state inspector.

    Selecting a prim renders its composed detail and (via on_focus) filters
    the event log to that path.
    """
    selected = {"path": None}

    with ui.row().classes("w-full gap-4 mb-4 items-start"):
        with ui.column().classes("flex-1"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.label("STAGE PRIMS").classes(
                    "text-xs font-semibold dash-muted uppercase"
                )
                ui.button(
                    icon="refresh", on_click=lambda: _refresh_tree(),
                ).props("flat round dense size=sm").tooltip("Refresh")
            tree_container = ui.column().classes("w-full")
        with ui.column().classes("flex-1"):
            with ui.row().classes("items-center gap-2 mb-1 w-full"):
                ui.label("PRIM INSPECTOR").classes(
                    "text-xs font-semibold dash-muted uppercase"
                )
                ui.space()
                ui.button(
                    icon="refresh", on_click=lambda: _refresh_inspector(),
                ).props("flat round dense size=sm").tooltip("Refresh inspector")
                ui.button(
                    "Clear", icon="close", on_click=lambda: _clear(),
                ).props("flat dense no-caps size=sm").tooltip(
                    "Clear inspector and event-log filter"
                )
            inspector_scroll = ui.scroll_area().classes("w-full").style(
                "height: 360px"
            )
            inspector_container = ui.column().classes("w-full gap-1")
            inspector_container.move(inspector_scroll)

    def _build_tree_nodes(prims: list[dict]) -> list[dict]:
        nodes_by_path: dict[str, dict] = {}
        for p in prims:
            badges = []
            if p.get("instanceable"):
                badges.append("[I]")
            if p.get("is_point_instancer"):
                badges.append("[PI]")
            suffix = (" " + " ".join(badges)) if badges else ""
            nodes_by_path[p["path"]] = {
                "id": p["path"],
                "label": (
                    p["path"].rsplit("/", 1)[-1]
                    + f"  ({p['typeName']})"
                    + suffix
                ),
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

    def _render_inspector(path):
        inspector_container.clear()
        with inspector_container:
            _render_prim_detail(srv.get_prim_detail(path))

    def _refresh_inspector():
        if selected["path"]:
            _render_inspector(selected["path"])

    def _show_placeholder():
        inspector_container.clear()
        with inspector_container:
            ui.label("Select a prim to inspect").classes("dash-muted text-sm")

    def _clear():
        selected["path"] = None
        _show_placeholder()
        if on_focus:
            on_focus("")

    def _on_select(path):
        if not path:
            _clear()
            return
        selected["path"] = path
        _render_inspector(path)
        if on_focus:
            on_focus(path)

    def _refresh_tree():
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
                on_select=lambda e: _on_select(getattr(e, "value", None)),
            ).props("dense default-expand-all").classes(
                "w-full text-xs font-mono"
            )

    _show_placeholder()
    _refresh_tree()
    if register_full_refresh:
        register_full_refresh(_refresh_tree)


def _build_status_cards(srv: UsdSyncServer, register_refresh=None):
    """Five stat cards plus an instancing strip."""
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
            ui.label("PRIMS (STAGE / TRACKED)").classes("text-xs dash-muted uppercase")
            prim_val = ui.label("--").classes("text-2xl font-bold dash-accent")
        with ui.card().classes("flex-1").tooltip(
            "Instanceable-flag prims (left) and the composition's resolved prototype count"
        ):
            ui.label("INSTANCES / PROTOTYPES").classes("text-xs dash-muted uppercase")
            inst_val = ui.label("--").classes("text-2xl font-bold dash-accent")

    def _fmt(s):
        h, r = divmod(int(s), 3600)
        m, s = divmod(r, 60)
        return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"

    def refresh():
        uptime_val.text = _fmt(srv.get_uptime())
        event_val.text = f"{srv.get_event_count():,}"
        client_val.text = str(len(srv.get_client_list()))
        prim_val.text = f"{srv.get_prim_count()} / {srv.get_tracked_prim_count()}"
        inst_val.text = f"{srv.get_instance_count()} / {srv.get_prototype_count()}"

    if register_refresh:
        register_refresh(refresh)
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

    async def purge():
        with ui.dialog() as dialog, ui.card():
            ui.label("Purge all data?").classes("text-lg font-bold")
            ui.label(
                "This will delete all events from the log, clear the edit layer, "
                "and reset all connected receivers. The base scene will be restored. "
                "This cannot be undone."
            ).classes("text-sm dash-muted")
            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                ui.button("Cancel", on_click=dialog.close).props("flat dense no-caps")

                def confirm():
                    count = srv.get_event_count()
                    srv.purge()
                    dialog.close()
                    ui.notify(f"Purged {count} events", type="warning")

                ui.button("Purge", icon="delete_forever", on_click=confirm).props(
                    "dense no-caps color=negative"
                )
        dialog.open()

    def _set_auto_compact(e):
        seconds = float(e.value or 0)
        srv.set_compact_interval(seconds)
        ui.notify(
            f"Auto-compact every {seconds:.0f}s" if seconds > 0 else "Auto-compact off",
            type="info",
        )

    def _set_auto_reclaim(e):
        seconds = float(e.value or 0)
        srv.set_reclaim_interval(seconds)
        ui.notify(
            f"Reclaim storage every {seconds:.0f}s" if seconds > 0 else "Storage reclaim off",
            type="info",
        )

    with ui.row().classes("gap-2 mb-4 items-center"):
        ui.button("Compact Log", icon="compress", on_click=compact).props(
            "outline dense no-caps"
        )
        ui.number(
            label="Auto-compact (s)", value=srv.get_compact_interval(), min=0,
            on_change=_set_auto_compact,
        ).props("dense outlined").classes("w-32").tooltip(
            "Compact the event log every N seconds (0 = off). Skips when "
            "idle; incoming edits queue during the compaction commit."
        )
        ui.number(
            label="Reclaim disk (s)", value=srv.get_reclaim_interval(), min=0,
            on_change=_set_auto_reclaim,
        ).props("dense outlined").classes("w-32").tooltip(
            "Return freed event-log disk space at most every N seconds "
            "(0 = off). Runs at compaction/purge, so pair with auto-compact."
        )
        ui.button("Export Diff", icon="difference", on_click=export_diff).props(
            "outline dense no-caps"
        )
        ui.button("Export Flat", icon="download", on_click=export_flatten).props(
            "outline dense no-caps"
        )
        ui.button("Purge", icon="delete_forever", on_click=purge).props(
            "outline dense no-caps color=negative"
        )


def _build_transforms_view(srv: UsdSyncServer):
    """Show composed transforms for all Xform prims on the stage."""
    def _fmt(vals, places):
        return "(" + ", ".join(f"{v:.{places}f}" for v in vals) + ")"

    for row in srv.get_transforms_snapshot():
        t, r, s = row.get("t"), row.get("r"), row.get("s")
        with ui.row().classes(
            "items-baseline gap-2 w-full font-mono text-xs py-1"
        ).style("border-bottom: 1px solid rgba(128,128,128,0.15)"):
            ui.label(row["path"]).classes("dash-prim w-48 truncate")
            if t:
                ui.label(f"T {_fmt(t, 2)}").classes("dash-accent")
            if r:
                ui.label(f"R {_fmt(r, 3)}").classes("dash-kind")
            if s:
                ui.label(f"S {_fmt(s, 2)}").classes("dash-muted")


def _build_per_layer_view(srv: UsdSyncServer):
    """Show each department/client layer's USDA contribution."""
    layers = srv.get_layer_stack_info()
    if not layers:
        ui.label("No layers").classes("dash-muted text-sm")
        return

    for i, info in enumerate(layers):
        dept = info.get("department")
        clients = info.get("clients", [])
        muted = info.get("muted", False)
        name = dept or (clients[0] if clients else info.get("identifier", ""))
        usda = srv.export_layer(name)
        badge = "  [MUTED]" if muted else ""
        header = f"#{i + 1}  {name}{badge}"
        icon = "layers" if dept else "person"
        has_opinions = len(usda.strip().split("\n")) > 1
        with ui.expansion(
            header, icon=icon,
        ).classes("w-full").props(
            "dense" + (" default-opened" if has_opinions else "")
        ):
            ui.label(usda).classes("usda-viewer")


def _build_layer_stack(srv: UsdSyncServer, register_refresh=None,
                       register_full_refresh=None):
    """Layer stack panel with composed USDA viewer side-by-side."""

    with ui.row().classes("w-full gap-4 mb-4 items-start"):
        # -- Left column: layer controls -----------------------------------
        with ui.column().classes("flex-1"):
            ui.label("LAYER STACK").classes(
                "text-xs font-semibold dash-muted uppercase mb-1"
            )

            layer_container = ui.column().classes("w-full")

        # -- Right column: composed USDA viewer ----------------------------
        with ui.column().classes("flex-1"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.label("COMPOSED STAGE").classes(
                    "text-xs font-semibold dash-muted uppercase"
                )
                view_toggle = ui.toggle(
                    {"transforms": "Transforms", "layers": "Per Layer", "usda": "Full USDA"},
                    value="transforms",
                ).props("dense no-caps size=sm")
                ui.button(
                    icon="refresh",
                    on_click=lambda: _refresh_composed(),
                ).props("flat round dense size=sm").tooltip("Refresh")
            usda_scroll = ui.scroll_area().classes("w-full").style("height: 400px")
            with usda_scroll:
                composed_container = ui.column().classes("w-full gap-0")

    # -- Refresh logic (needs references from both columns) ----------------

    def _refresh_composed():
        composed_container.clear()
        with composed_container:
            if view_toggle.value == "usda":
                try:
                    text = srv.export_flattened_string()
                except Exception:
                    text = "# (unable to flatten stage)"
                ui.label(text).classes("usda-viewer")
            elif view_toggle.value == "layers":
                _build_per_layer_view(srv)
            else:
                _build_transforms_view(srv)

    view_toggle.on_value_change(lambda _: _refresh_composed())

    # Per-layer roster expansion open/closed state, keyed by layer
    # identifier. The timer rebuilds the cards every tick; without this
    # an open roster would snap shut on every refresh.
    roster_open: dict[str, bool] = {}

    def _refresh_layer_cards():
        """Refresh only the layer card list (safe for timer — no expansion reset)."""
        layers = srv.get_layer_stack_info()
        connected_clients = {
            c["client_id"] for c in srv.get_client_list() if c.get("client_id")
        }
        n = len(layers)
        layer_container.clear()
        with layer_container:
            if not layers:
                ui.label("No layers").classes("dash-muted text-sm")
            else:
                for i, info in enumerate(layers):
                    _build_layer_card(
                        i, info, connected_clients,
                        _mute, _unmute, _merge, _move_layer,
                        is_first=(i == 0), is_last=(i == n - 1),
                        roster_open=roster_open,
                    )

    def _refresh_all():
        """Full refresh — layer cards + composed view. For user actions only."""
        _refresh_layer_cards()
        _refresh_composed()

    def _move_layer(index, direction):
        """Move a department layer up (stronger) or down (weaker)."""
        layers = srv.get_layer_stack_info()
        new_index = index + direction
        if new_index < 0 or new_index >= len(layers):
            return
        layers[index], layers[new_index] = layers[new_index], layers[index]
        new_order = [
            info["department"] for info in layers if info.get("department")
        ]
        try:
            srv.set_department_priority(new_order)
        except ReplayModeConflictError as exc:
            ui.notify(str(exc), type="warning")
            return
        _refresh_all()

    def _mute(key):
        try:
            srv.mute_layer(key)
        except ReplayModeConflictError as exc:
            ui.notify(str(exc), type="warning")
            return
        ui.notify(f"Muted: {key}", type="info")
        _refresh_all()

    def _unmute(key):
        try:
            srv.unmute_layer(key)
        except ReplayModeConflictError as exc:
            ui.notify(str(exc), type="warning")
            return
        ui.notify(f"Unmuted: {key}", type="positive")
        _refresh_all()

    def _merge(client_id):
        ok = srv.merge_layer(client_id)
        if ok:
            ui.notify(f"Merged: {client_id}", type="positive")
        else:
            ui.notify(f"Merge failed: {client_id}", type="negative")
        _refresh_all()

    _refresh_all()

    def _timer_tick():
        _refresh_layer_cards()
        if view_toggle.value == "transforms":
            _refresh_composed()

    if register_refresh:
        register_refresh(_timer_tick)
    if register_full_refresh:
        # Transforms mode already refreshes on the light timer; the full
        # pass covers the Per Layer / Full USDA modes.
        register_full_refresh(
            lambda: None if view_toggle.value == "transforms" else _refresh_composed()
        )


def _build_layer_card(
    i, info, connected_clients,
    on_mute, on_unmute, on_merge, on_move,
    is_first=False, is_last=False,
    roster_open=None,
):
    """Render a single layer card in the stack."""
    dept = info.get("department")
    clients = info.get("clients", [])
    muted = info.get("muted", False)
    identifier = info.get("identifier", "")
    shared = info.get("shared", False)

    with ui.card().classes("w-full p-2"):
        with ui.row().classes("items-center w-full gap-2"):
            # Reorder buttons
            with ui.column().classes("gap-0"):
                ui.button(
                    icon="keyboard_arrow_up",
                    on_click=lambda idx=i: on_move(idx, -1),
                ).props("flat round dense size=xs").set_enabled(not is_first)
                ui.button(
                    icon="keyboard_arrow_down",
                    on_click=lambda idx=i: on_move(idx, 1),
                ).props("flat round dense size=xs").set_enabled(not is_last)

            # Strength indicator
            ui.label(f"#{i + 1}").classes(
                "text-xs dash-muted font-mono w-6 text-right"
            )

            # Layer icon + name
            if dept:
                ui.icon("layers", size="xs").classes("dash-accent")
                ui.label(dept).classes("font-semibold text-sm dash-accent")
            elif shared:
                ui.icon("group", size="xs").classes("dash-muted")
                ui.label("shared edits").classes("font-semibold text-sm").tooltip(
                    "Communal fallback layer for clients without a department"
                )
            else:
                ui.icon("person", size="xs").classes("dash-muted")
                label = clients[0] if clients else identifier
                ui.label(label).classes("font-semibold text-sm")

            ui.space()

            # Connection summary: one dot + connected/total, not a dot
            # per client (unreadable past a handful).
            online_count = sum(1 for cid in clients if cid in connected_clients)
            if clients:
                dot = "positive" if online_count else "grey-6"
                ui.icon("circle", size="xs").classes(f"text-{dot}")
                ui.label(f"{online_count}/{len(clients)}").classes(
                    "text-xs dash-muted font-mono"
                ).tooltip(f"{online_count} of {len(clients)} clients connected")

            # Muted badge
            if muted:
                ui.badge("MUTED", color="warning").props("dense")

            # Mute/unmute toggle
            key = dept or (clients[0] if clients else None)
            if key:
                if muted:
                    ui.button(
                        icon="volume_up",
                        on_click=lambda k=key: on_unmute(k),
                    ).props("flat round dense size=sm").tooltip("Unmute")
                else:
                    ui.button(
                        icon="volume_off",
                        on_click=lambda k=key: on_mute(k),
                    ).props("flat round dense size=sm").tooltip("Mute")

            # Merge button (per-client layers only; the shared layer is
            # communal and merge_layer refuses it)
            if not dept and not shared and clients:
                ui.button(
                    icon="merge",
                    on_click=lambda cid=clients[0]: on_merge(cid),
                ).props("flat round dense size=sm").tooltip("Merge into root")

        # Full roster in a collapsed, scrollable expansion (connected first)
        # so a layer with hundreds of clients stays one compact card. Open
        # state persists across timer rebuilds via roster_open.
        if (dept or shared) and clients:
            exp = ui.expansion(f"clients ({len(clients)})", value=bool(
                (roster_open or {}).get(identifier, False)
            )).props("dense header-class=text-caption").classes("w-full")
            if roster_open is not None:
                exp.on_value_change(
                    lambda e, ident=identifier: roster_open.__setitem__(
                        ident, bool(e.value)
                    )
                )
            with exp, ui.row().classes("gap-x-3 gap-y-1 flex-wrap pl-2").style(
                "max-height: 130px; overflow-y: auto"
            ):
                ordered = sorted(
                    clients, key=lambda c: (c not in connected_clients, c)
                )
                for cid in ordered:
                    online = cid in connected_clients
                    with ui.row().classes("items-center gap-1"):
                        ui.icon("circle", size="8px").classes(
                            "text-positive" if online else "text-grey-6"
                        )
                        ui.label(cid).classes(
                            "text-xs font-mono" + ("" if online else " dash-muted")
                        )


def _build_proposals_panel(srv: UsdSyncServer, register_refresh=None,
                           register_full_refresh=None):
    """Cross-department edit proposals panel."""
    with ui.row().classes("items-center gap-2 mb-1"):
        ui.label("PROPOSALS").classes(
            "text-xs font-semibold dash-muted uppercase"
        )
        ui.button(
            icon="refresh",
            on_click=lambda: _refresh_proposals(),
        ).props("flat round dense size=sm").tooltip("Refresh")

    proposal_container = ui.column().classes("w-full mb-4")

    def _refresh_proposals():
        proposals = srv.list_proposals()
        proposal_container.clear()
        with proposal_container:
            pending = [p for p in proposals if p["status"] == "pending"]
            if not pending:
                ui.label("No pending proposals").classes("dash-muted text-sm")
                return

            for p in pending:
                _build_proposal_card(p)

    def _build_proposal_card(p):
        pid = p["proposal_id"]
        with ui.card().classes("w-full p-3"):
            with ui.row().classes("items-center w-full gap-2"):
                ui.icon("rate_review", size="xs").classes("dash-accent")
                # Title: first line of description or proposal ID
                title = (p["description"] or pid).split("\n")[0]
                ui.label(title).classes("font-semibold text-sm flex-1")
                ui.badge(p["status"], color="warning").props("dense")

            with ui.row().classes("items-center gap-3 text-xs dash-muted mt-1"):
                ui.label(f"From: {p['from_client']}")
                if p.get("from_department"):
                    ui.badge(p["from_department"]).props("dense outline")
                ui.icon("arrow_forward", size="xs")
                ui.badge(
                    p["target_department"], color="primary",
                ).props("dense")

            # Full description (multi-line body after the title)
            desc = p.get("description", "")
            body = "\n".join(desc.split("\n")[1:]).strip()
            if body:
                ui.label(body).classes("text-xs dash-muted mt-1").style(
                    "white-space: pre-wrap"
                )

            # Proposal USDA content
            usda = p.get("layer_usda", "")
            lines = usda.strip().split("\n")
            has_content = len(lines) > 1
            if has_content:
                with ui.expansion("View changes", icon="code").classes(
                    "w-full mt-1"
                ).props("dense"):
                    ui.label(usda).classes("usda-viewer")

            # Action buttons
            with ui.row().classes("gap-2 mt-2"):
                ui.button(
                    "Approve", icon="check",
                    on_click=lambda pid=pid: _approve(pid),
                ).props("dense no-caps size=sm color=positive")
                ui.button(
                    "Reject", icon="close",
                    on_click=lambda pid=pid: _reject(pid),
                ).props("dense no-caps size=sm color=negative")

    def _approve(pid):
        if srv.approve_proposal(pid):
            ui.notify(f"Approved: {pid}", type="positive")
        else:
            ui.notify("Approval failed", type="negative")
        _refresh_proposals()

    def _reject(pid):
        if srv.reject_proposal(pid):
            ui.notify(f"Rejected: {pid}", type="warning")
        else:
            ui.notify("Rejection failed", type="negative")
        _refresh_proposals()

    _refresh_proposals()

    _last_proposal_count = {"value": srv.get_proposal_count()}

    def _check_proposals():
        count = srv.get_proposal_count()
        if count != _last_proposal_count["value"]:
            _last_proposal_count["value"] = count
            _refresh_proposals()

    if register_refresh:
        register_refresh(_check_proposals)
    if register_full_refresh:
        # The light timer only reacts to count changes; the full pass
        # also picks up status/content changes on existing proposals.
        register_full_refresh(_refresh_proposals)


def _build_clients_table(srv: UsdSyncServer, register_refresh=None):
    """Client roster."""
    ui.label("CLIENTS").classes("text-xs font-semibold dash-muted uppercase mb-1")

    columns = [
        {"name": "role", "label": "Role", "field": "role", "align": "left"},
        {"name": "id", "label": "Client ID", "field": "client_id",
         "align": "left"},
        {"name": "origin", "label": "Origin", "field": "origin",
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
                "origin": c.get("origin") or "—",
                "event_count": f"{c['event_count']:,}",
                "last_activity": f"{c['last_activity_ago']:.0f}s ago",
            }
            for c in clients
        ]

    if register_refresh:
        register_refresh(refresh)
    refresh()

    # Token management (only when TOFU is enabled)
    if srv.require_token:
        _build_token_panel(srv)


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _build_wire_metrics(srv: UsdSyncServer, register_refresh=None):
    """Encoded bytes per event kind (only when --wire-metrics is on)."""
    if srv.wire_metrics is None:
        return
    ui.label("WIRE TRAFFIC BY EVENT KIND").classes(
        "text-xs font-semibold dash-muted uppercase mb-1"
    )

    columns = [
        {"name": "kind", "label": "Kind", "field": "kind", "align": "left"},
        {"name": "count", "label": "Count", "field": "count", "align": "right"},
        {"name": "bytes", "label": "Bytes", "field": "bytes", "align": "right"},
        {"name": "avg", "label": "Avg", "field": "avg", "align": "right"},
        {"name": "share", "label": "Share", "field": "share", "align": "right"},
    ]
    table = ui.table(
        columns=columns, rows=[], row_key="kind",
    ).props("dense flat bordered").classes("w-full mb-4")

    def refresh():
        m = srv.get_wire_metrics()
        total = m["total_bytes"] or 1
        rows = [
            {
                "kind": kind,
                "count": f"{v['count']:,}",
                "bytes": _fmt_bytes(v["bytes"]),
                "avg": _fmt_bytes(v["bytes"] // max(v["count"], 1)),
                "share": f"{v['bytes'] / total * 100:.1f}%",
                "_bytes": v["bytes"],
            }
            for kind, v in m["kinds"].items()
        ]
        rows.sort(key=lambda r: -r["_bytes"])
        table.rows = rows

    if register_refresh:
        register_refresh(refresh)
    refresh()


def _build_token_panel(srv: UsdSyncServer):
    """Token management panel — list issued tokens with revoke buttons."""
    ui.label("TOKENS").classes("text-xs font-semibold dash-muted uppercase mb-1")

    token_container = ui.column().classes("w-full mb-4")

    def _refresh_tokens():
        tokens = srv.get_token_list()
        token_container.clear()
        with token_container:
            if not tokens:
                ui.label("No tokens issued").classes("dash-muted text-sm")
                return
            for t in tokens:
                created = datetime.datetime.fromtimestamp(
                    t["created_at"],
                ).strftime("%Y-%m-%d %H:%M")
                seen = datetime.datetime.fromtimestamp(
                    t["last_seen"],
                ).strftime("%Y-%m-%d %H:%M")
                with ui.row().classes(
                    "items-center w-full gap-3 font-mono text-xs py-1"
                ).style("border-bottom: 1px solid rgba(128,128,128,0.15)"):
                    ui.label(t["client_id"]).classes("dash-prim flex-1")
                    if t.get("department"):
                        ui.badge(t["department"]).props("dense outline")
                    ui.label(f"created {created}").classes("dash-muted")
                    ui.label(f"seen {seen}").classes("dash-muted")
                    ui.button(
                        icon="delete",
                        on_click=lambda cid=t["client_id"]: _revoke(cid),
                    ).props("flat round dense size=sm color=negative").tooltip(
                        "Revoke token"
                    )

    def _revoke(client_id):
        ok = srv.revoke_token(client_id)
        if ok:
            ui.notify(f"Revoked token: {client_id}", type="warning")
        _refresh_tokens()

    _refresh_tokens()


def _build_event_feed(srv: UsdSyncServer, register_refresh=None, feed_api=None):
    """Paginated event feed with inline JSON detail.

    If *feed_api* is provided, publishes a ``focus`` callback into it so the
    prim tree can filter the log to a selected prim path.
    """
    ui.label("EVENT LOG").classes(
        "text-xs font-semibold dash-muted uppercase mb-1"
    )

    kind_opts = {"": "All kinds", **{k: k for k in sorted(EVENT_KEYS)}}
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

    feed_scroll = ui.scroll_area().classes("w-full").style("height: 600px")
    feed_container = ui.column().classes("w-full gap-0")
    feed_container.move(feed_scroll)

    # Expansions are reconstructed on every _rebuild, so we remember
    # which seqs the user opened and restore them after the rebuild.
    expanded_seqs: set = set()

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
        origin = rec.get("origin", "")

        exp = ui.expansion(
            icon="chevron_right", value=seq in expanded_seqs,
        ).classes("w-full")

        def _on_toggle(e, s=seq):
            if e.value:
                expanded_seqs.add(s)
            else:
                expanded_seqs.discard(s)

        exp.on_value_change(_on_toggle)

        with exp.add_slot("header"):
            with ui.row().classes(
                "items-center gap-3 w-full font-mono text-xs"
            ):
                ui.label(str(seq)).classes("dash-muted w-10 text-right")
                ui.label(k).classes("dash-kind w-44")
                ui.label(prim).classes("dash-prim flex-1 truncate")
                if origin:
                    ui.label(origin).classes(
                        "dash-muted w-36 text-right truncate"
                    )
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

    def focus_prim(path):
        prim_filter.value = path or ""
        kind_filter.value = ""
        _reset()

    if feed_api is not None:
        feed_api["focus"] = focus_prim

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

    _last_count = {"value": srv.get_event_count()}

    def _check_live():
        count = srv.get_event_count()
        changed = _has_new_events["value"] or count != _last_count["value"]
        _last_count["value"] = count
        if changed:
            _has_new_events["value"] = False
            _rebuild()

    if register_refresh:
        register_refresh(_check_live)
    srv.add_event_listener(_on_live)

    # Clean up listener when browser tab disconnects
    from nicegui import app
    app.on_disconnect(lambda: srv.remove_event_listener(_on_live))


def _register_api_routes(srv: UsdSyncServer):
    """REST API for scripting."""
    from nicegui import app
    from starlette.exceptions import HTTPException
    from starlette.requests import Request

    def _change_layer_stack(operation):
        try:
            return operation()
        except ReplayModeConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/status")
    def api_status():
        return {
            "uptime_seconds": round(srv.get_uptime(), 1),
            "event_count": srv.get_event_count(),
            "client_count": len(srv.get_client_list()),
            "prim_count": srv.get_prim_count(),
            "tracked_prim_count": srv.get_tracked_prim_count(),
            "instance_count": srv.get_instance_count(),
            "prototype_count": srv.get_prototype_count(),
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

    @app.get("/api/wire-metrics")
    def api_wire_metrics():
        return srv.get_wire_metrics()

    @app.get("/api/compact-interval")
    def api_get_compact_interval():
        return {"seconds": srv.get_compact_interval()}

    @app.post("/api/compact-interval")
    async def api_set_compact_interval(request: Request):
        body = await request.json()
        srv.set_compact_interval(float(body.get("seconds", 0)))
        return {"ok": True, "seconds": srv.get_compact_interval()}

    @app.get("/api/reclaim-interval")
    def api_get_reclaim_interval():
        return {"seconds": srv.get_reclaim_interval()}

    @app.post("/api/reclaim-interval")
    async def api_set_reclaim_interval(request: Request):
        body = await request.json()
        srv.set_reclaim_interval(float(body.get("seconds", 0)))
        return {"ok": True, "seconds": srv.get_reclaim_interval()}

    @app.get("/api/prim-tree")
    def api_prim_tree():
        return srv.get_prim_tree()

    @app.get("/api/prim-detail")
    def api_prim_detail(path: str):
        return srv.get_prim_detail(path)

    # -- Layer management endpoints ----------------------------------------

    @app.get("/api/layers")
    def api_layers():
        return srv.get_layer_stack_info()

    @app.post("/api/layers/reorder")
    async def api_layers_reorder(request: Request):
        body = await request.json()
        ordered = body.get("departments", body.get("order", []))
        _change_layer_stack(lambda: srv.set_department_priority(ordered))
        return {"ok": True}

    @app.post("/api/layers/{key}/mute")
    def api_layer_mute(key: str):
        ok = _change_layer_stack(lambda: srv.mute_layer(key))
        return {"ok": ok}

    @app.post("/api/layers/{key}/unmute")
    def api_layer_unmute(key: str):
        ok = _change_layer_stack(lambda: srv.unmute_layer(key))
        return {"ok": ok}

    @app.post("/api/layers/{client_id}/merge")
    def api_layer_merge(client_id: str):
        ok = srv.merge_layer(client_id)
        return {"ok": ok}

    @app.delete("/api/layers/{client_id}")
    def api_layer_delete(client_id: str):
        ok = srv.delete_layer(client_id)
        return {"ok": ok}

    @app.get("/api/departments")
    def api_departments():
        return {"departments": srv.department_priority}

    @app.post("/api/departments")
    async def api_departments_set(request: Request):
        body = await request.json()
        ordered = body.get("departments", [])
        _change_layer_stack(lambda: srv.set_department_priority(ordered))
        return {"ok": True}

    # -- Token management endpoints ----------------------------------------

    @app.get("/api/tokens")
    def api_tokens():
        return srv.get_token_list()

    @app.delete("/api/tokens/{client_id}")
    def api_token_revoke(client_id: str):
        ok = srv.revoke_token(client_id)
        return {"ok": ok}

    # -- Proposal endpoints ------------------------------------------------

    @app.get("/api/proposals")
    def api_proposals(department: str | None = None):
        return srv.list_proposals(department=department)

    @app.post("/api/proposals")
    async def api_proposal_create(request: Request):
        body = await request.json()
        pid = srv.create_proposal(
            body.get("from_client", ""),
            body.get("target_department", ""),
            body.get("description", ""),
        )
        return {"proposal_id": pid}

    @app.post("/api/proposals/{proposal_id}/approve")
    def api_proposal_approve(proposal_id: str):
        return {"ok": srv.approve_proposal(proposal_id)}

    @app.post("/api/proposals/{proposal_id}/reject")
    def api_proposal_reject(proposal_id: str):
        return {"ok": srv.reject_proposal(proposal_id)}
