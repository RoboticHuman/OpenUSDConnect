"""OpenUSDConnect receive-side wiring for usdview.

Receive-only: no emitter, no feedback-loop guard. Stage mutation triggers
``Usd.Notice.ObjectsChanged`` which refreshes the viewport on its own.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid

# Self-bootstrap sys.path so `openusdconnect` resolves when the plugin is
# hand-installed without going through the launcher.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from openusdconnect.cli_common import parse_bool  # noqa: E402
from openusdconnect.defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT  # noqa: E402

LOG = logging.getLogger("openusdconnect.usdview")

_receiver = None
_dispatcher = None
_qtimer = None
_usdview_api = None
_stage = None
_running: bool = False
_host: str = ""
_port: int = 0
_client_id: str = ""
_origin: str = ""
_translate_openpbr: bool = False

_TICK_INTERVAL_MS = 16


def start(
    usdviewApi,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_SYNC_PORT,
    *,
    token: str | None = None,
    client_id: str | None = None,
    origin: str | None = None,
) -> bool:
    """Start the receive pipeline.

    Returns True on success, False if already running or the stage is missing.
    """
    global _receiver, _dispatcher, _qtimer, _usdview_api, _stage, _running
    global _host, _port, _client_id, _origin, _translate_openpbr

    if _running:
        LOG.warning("Already running, call stop() first")
        return False

    stage = usdviewApi.dataModel.stage
    if stage is None:
        LOG.error("usdview has no active stage; cannot start receiver")
        return False

    from pxr.Usdviewq.qt import QtCore

    from openusdconnect.adapters import UsdStageAdapter
    from openusdconnect.client_id import make_stable_client_id
    from openusdconnect.dispatcher import EventDispatcher
    from openusdconnect.receiver import ReceiverThread

    _host = host
    _port = port
    _client_id = client_id or make_stable_client_id("usdview")
    _origin = origin or f"usdview-{uuid.uuid4().hex[:8]}"
    # Receiver-side OpenPBR->standard_surface translation for RenderMan, which
    # can't render the OpenPBR surface node. Enabled by the launcher's
    # --renderman flag (which sets this env var).
    try:
        _translate_openpbr = parse_bool(
            os.environ.get("OPENUSDCONNECT_TRANSLATE_OPENPBR", "0")
        )
    except ValueError:
        LOG.error(
            "OPENUSDCONNECT_TRANSLATE_OPENPBR is not a valid boolean; "
            "OpenPBR translation is disabled"
        )
        _translate_openpbr = False

    _receiver = ReceiverThread(
        host=host,
        port=port,
        sync_from=1,
        client_id=_client_id,
        origin=_origin,
        token=token,
        layered_replay=True,
    )
    _receiver.start()

    _dispatcher = EventDispatcher(
        receiver=_receiver,
        adapter=UsdStageAdapter(stage),
        on_applied=_on_applied if _translate_openpbr else None,
        on_applied_events=_on_applied_events,
    )
    _usdview_api = usdviewApi
    _stage = stage

    # Convert any OpenPBR materials already in the opened scene; subsequent edits
    # are handled incrementally by _on_applied, scoped to the changed prims.
    if _translate_openpbr:
        from integrations.openpbr_translate import translate_openpbr_materials

        translate_openpbr_materials(stage)

    usdviewApi.dataModel.signalStageReplaced.connect(_on_stage_replaced)

    _qtimer = QtCore.QTimer(usdviewApi.qMainWindow)
    _qtimer.timeout.connect(_tick)
    _qtimer.start(_TICK_INTERVAL_MS)

    _running = True
    LOG.info("OpenUSDConnect receiver started → %s:%d (client=%s)", host, port, _client_id)
    return True


def _on_stage_replaced() -> None:
    """Follow a usdview stage swap; a None stage parks the binding until one returns."""
    global _stage

    if _dispatcher is None or _usdview_api is None:
        return

    _stage = _usdview_api.dataModel.stage
    if _stage is not None:
        from openusdconnect.adapters import UsdStageAdapter

        _dispatcher.adapter = UsdStageAdapter(_stage)
        _dispatcher.bind_layered_stage(_stage)
        if _translate_openpbr:
            from integrations.openpbr_translate import translate_openpbr_materials

            translate_openpbr_materials(_stage)
        LOG.info("Stage replaced, rebound receiver to the live stage")


def _tick() -> None:
    """Drain the receive queue each frame; idle while no stage is bound.

    Post-apply fixups (interface-input forwarding, OpenPBR translation) are
    driven by the dispatcher's on_applied callback (:func:`_on_applied`),
    scoped to the prims each batch actually changed.
    """
    if _dispatcher is None or _stage is None:
        return
    _dispatcher.drain_and_apply()


def _forward_interface_edits(stage, events: list[dict]) -> None:
    """Forward edited Material interface-input values onto their consumers.

    Hydra rebuilds a material network when a *shader* prim changes, but an
    edit that only touches a Material's interface inputs leaves the render
    stale. Forwarding is scoped to the inputs each event edited; the
    forwarded local value never wins resolution — connected inputs resolve
    through the connection — it exists to dirty the shader prim so the
    network re-resolves.
    """
    from pxr import UsdShade

    from openusdconnect.protocol_constants import K_SET_CONNECTABLE_INPUT

    for ev in events:
        if ev.get("k") != K_SET_CONNECTABLE_INPUT:
            continue
        edited = ev.get("inputs") or {}
        if not edited:
            continue
        prim = stage.GetPrimAtPath(ev["prim"])
        if not prim or not prim.IsA(UsdShade.Material):
            continue
        material = UsdShade.Material(prim)
        for iface, consumers in material.ComputeInterfaceInputConsumersMap().items():
            if iface.GetBaseName() not in edited:
                continue
            value = iface.Get()
            if value is None:
                continue
            for consumer in consumers:
                if consumer.Get() != value:
                    consumer.Set(value)


def _on_applied_events(events: list[dict]) -> None:
    """Post-apply conditioning scoped to what each event edited."""
    if _stage is None:
        return
    _forward_interface_edits(_stage, events)


def _on_applied(prim_paths: list[str]) -> None:
    """Translate OpenPBR materials owning the just-applied prims."""
    if _stage is None:
        return
    from integrations.openpbr_translate import translate_openpbr_for_paths

    translate_openpbr_for_paths(_stage, prim_paths)


def refresh_asset_dependency(asset_path: str | None = None) -> dict:
    """Retry unresolved references or payloads on usdview's main thread.

    ``asset_path`` is the authored path or resolver identifier. Omit it to
    retry every dependency currently known to be unresolved.
    """
    if _dispatcher is None or _stage is None:
        return {
            "status": "no_stage",
            "reapplied": 0,
            "affected_prims": [],
            "pending": [],
        }
    return _dispatcher.refresh_asset_dependency(asset_path)


def stop() -> None:
    """Stop the receive pipeline and release all state."""
    global _receiver, _dispatcher, _qtimer, _usdview_api, _stage, _running

    if not _running:
        return

    if _qtimer is not None:
        _qtimer.stop()
        _qtimer = None

    if _usdview_api is not None:
        _usdview_api.dataModel.signalStageReplaced.disconnect(_on_stage_replaced)

    if _receiver is not None:
        _receiver.stop()
        _receiver = None

    if _dispatcher is not None:
        _dispatcher.close()
    _dispatcher = None
    _usdview_api = None
    _stage = None
    _running = False
    LOG.info("OpenUSDConnect receiver stopped")


def status() -> dict:
    """Return current connection state for diagnostics."""
    return {
        "running": _running,
        "host": _host,
        "port": _port,
        "client_id": _client_id,
        "origin": _origin,
        "receiver_connected": _receiver.connected if _receiver else False,
        "last_seq": _dispatcher.last_seq if _dispatcher else 0,
        "pending_asset_dependencies": (
            list(_dispatcher.pending_asset_dependencies) if _dispatcher else []
        ),
        "translate_openpbr": _translate_openpbr,
    }
