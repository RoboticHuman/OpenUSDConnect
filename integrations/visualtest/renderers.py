"""Hydra renderer registry for the visual harness.

Each ``Renderer`` pairs a Hydra delegate plugin id with optional environment
setup and per-renderer material conditioning. hdPrman needs the ``RMAN_*``
search paths (``ensure_renderman``) plus an OpenPBR->standard_surface
translation (it has no OpenPBR MaterialX adapter); delegates that render
MaterialX natively register with no conditioning. Add a renderer (Cycles,
Mitsuba, ...) by adding one entry to ``RENDERERS``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Renderer:
    """A Hydra delegate plus its headless setup and material conditioning.

    ``setup`` returns a truthy token when the renderer is usable (and applies any
    process env it needs), or "" when unavailable. ``condition`` mutates a stage
    in place before rendering (renderer-specific material fixups).
    """

    name: str
    plugin_id: str
    setup: Callable[[], str] | None = None
    condition: Callable[[object], None] | None = None

    def is_available(self) -> bool:
        return bool(self.setup()) if self.setup is not None else True


def _renderman_setup() -> str:
    from integrations.renderman import ensure_renderman

    return ensure_renderman()


def _renderman_condition(stage) -> None:
    from integrations.openpbr_translate import translate_openpbr_materials

    translate_openpbr_materials(stage)


RENDERERS: dict[str, Renderer] = {
    "renderman": Renderer(
        "renderman", "HdPrmanLoaderRendererPlugin", _renderman_setup, _renderman_condition
    ),
    "embree": Renderer("embree", "HdEmbreeRendererPlugin"),
    "storm": Renderer("storm", "HdStormRendererPlugin"),
    # Cycles / Mitsuba: register their delegate ids here once the plugins are
    # installed (e.g. Renderer("cycles", "HdCyclesRendererPlugin")). Give a
    # setup/condition only if the delegate needs env or material fixups.
}


def get_renderer(renderer: str | Renderer) -> Renderer:
    """Resolve a renderer by name; pass a ``Renderer`` through unchanged."""
    if isinstance(renderer, Renderer):
        return renderer
    try:
        return RENDERERS[renderer]
    except KeyError:
        raise ValueError(f"unknown renderer {renderer!r}; known: {sorted(RENDERERS)}") from None
