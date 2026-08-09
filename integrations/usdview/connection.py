"""OpenUSDConnect receive-side wiring for usdview.

Receive-only: no emitter, no feedback-loop guard. Stage mutation triggers
``Usd.Notice.ObjectsChanged`` which refreshes the viewport on its own.
Built on ``UsdReceiver`` — token persistence, identity, and the underlying
``ReceiverThread`` + ``EventDispatcher`` composition are handled by the client.
"""

from __future__ import annotations

import logging
import os
import sys

# Self-bootstrap sys.path so `openusdconnect` resolves when the plugin is
# hand-installed without going through the launcher.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from openusdconnect.cli_common import parse_bool  # noqa: E402
from openusdconnect.defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT  # noqa: E402
from openusdconnect.usd_client import UsdReceiver  # noqa: E402

LOG = logging.getLogger("openusdconnect.usdview")

_receiver: UsdReceiver | None = None
_qtimer = None
_usdview_api = None
_translate_openpbr: bool = False

_TICK_INTERVAL_MS = 16


def start(
    usdviewApi,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_SYNC_PORT,
    *,
    token: str | None = None,
) -> bool:
    """Start the receive pipeline.

    Returns True on success, False if already running or the stage is missing.
    """
    global _receiver, _qtimer, _usdview_api, _translate_openpbr

    if _receiver is not None:
        LOG.warning("Already running, call stop() first")
        return False

    stage = usdviewApi.dataModel.stage
    if stage is None:
        LOG.error("usdview has no active stage; cannot start receiver")
        return False

    from pxr.Usdviewq.qt import QtCore

    # Receiver-side OpenPBR->standard_surface translation for RenderMan.
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

    try:
        _receiver = UsdReceiver(
            stage,
            app_name="usdview",
            host=host,
            port=port,
            token=token,
            on_applied=_on_applied if _translate_openpbr else None,
            on_applied_events=_on_applied_events,
        )
    except ValueError as exc:
        LOG.error("Cannot start receiver: %s", exc)
        return False

    _receiver.start()

    # Convert any OpenPBR materials already in the opened scene.
    if _translate_openpbr:
        from integrations.openpbr_translate import translate_openpbr_materials

        translate_openpbr_materials(stage)

    usdviewApi.dataModel.signalStageReplaced.connect(_on_stage_replaced)

    _qtimer = QtCore.QTimer(usdviewApi.qMainWindow)
    _qtimer.timeout.connect(_tick)
    _qtimer.start(_TICK_INTERVAL_MS)

    _usdview_api = usdviewApi
    LOG.info(
        "OpenUSDConnect receiver started -> %s:%d (client=%s)",
        host,
        port,
        _receiver.receiver.client_id,
    )
    return True


def _on_stage_replaced() -> None:
    """Follow a usdview stage swap; park the receiver while no stage is loaded."""
    global _receiver

    if _receiver is None or _usdview_api is None:
        return

    stage = _usdview_api.dataModel.stage
    _receiver.rebind_stage(stage)

    if stage is not None and _translate_openpbr:
        from integrations.openpbr_translate import translate_openpbr_materials

        translate_openpbr_materials(stage)
    LOG.info("Stage replaced, %s", "rebound receiver" if stage else "parked receiver")


def _tick() -> None:
    """Drain the receive queue each frame.

    Post-apply fixups (interface-input forwarding, OpenPBR translation) are
    driven by the receiver's ``on_applied`` callback, scoped to the prims
    each batch actually changed.
    """
    if _receiver is not None:
        _receiver.update()


def _forward_interface_edits(stage, events: list[dict]) -> None:
    """Forward edited Material interface-input values onto their consumers.

    Hydra rebuilds a material network when a *shader* prim changes, but an
    edit that only touches a Material's interface inputs leaves the render
    stale. Forwarding is scoped to the inputs each event edited; the
    forwarded local value never wins resolution -- connected inputs resolve
    through the connection -- it exists to dirty the shader prim so the
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
    if _usdview_api is None:
        return
    stage = _usdview_api.dataModel.stage
    if stage is None:
        return
    _forward_interface_edits(stage, events)


def _on_applied(prim_paths: list[str]) -> None:
    """Translate OpenPBR materials owning the just-applied prims."""
    if _usdview_api is None:
        return
    stage = _usdview_api.dataModel.stage
    if stage is None:
        return
    from integrations.openpbr_translate import translate_openpbr_for_paths

    translate_openpbr_for_paths(stage, prim_paths)


def refresh_asset_dependency(asset_path: str | None = None) -> dict:
    """Retry unresolved references or payloads on usdview's main thread.

    ``asset_path`` is the authored path or resolver identifier. Omit it to
    retry every dependency currently known to be unresolved.
    """
    if _receiver is None:
        return {
            "status": "no_stage",
            "reapplied": 0,
            "affected_prims": [],
            "pending": [],
        }
    return _receiver.refresh_asset_dependency(asset_path)


def stop() -> None:
    """Stop the receive pipeline and release all state."""
    global _receiver, _qtimer, _usdview_api

    if _receiver is None:
        return

    if _qtimer is not None:
        _qtimer.stop()
        _qtimer = None

    if _usdview_api is not None:
        _usdview_api.dataModel.signalStageReplaced.disconnect(_on_stage_replaced)

    _receiver.close()
    _receiver = None
    _usdview_api = None
    LOG.info("OpenUSDConnect receiver stopped")


def status() -> dict:
    """Return current connection state for diagnostics."""
    if _receiver is None:
        return {
            "running": False,
            "receiver_connected": False,
            "last_seq": 0,
            "pending_asset_dependencies": [],
            "translate_openpbr": _translate_openpbr,
        }
    return {
        "running": True,
        "host": f"{_receiver.receiver.host}:{_receiver.receiver.port}",
        "client_id": _receiver.receiver.client_id or "",
        "receiver_connected": _receiver.connected,
        "last_seq": _receiver.last_seq,
        "pending_asset_dependencies": list(_receiver.pending_asset_dependencies),
        "translate_openpbr": _translate_openpbr,
    }
