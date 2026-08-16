"""Public setup helpers for project USD plugins."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .dll_paths import PLUGIN_DLL_DIRS_ENV, prepend_dll_dirs

DEFAULT_SDR_SHADER_IDS = ("UsdPreviewSurface",)


class PluginEnvironmentError(RuntimeError):
    """Raised when the process cannot initialize its configured USD plugins."""


@dataclass(frozen=True, slots=True)
class PluginEnvironmentResult:
    """Result of preparing the current process for USD plugin use."""

    requested_dll_dirs: tuple[str, ...]
    added_dll_dirs: tuple[str, ...]
    missing_dll_dirs: tuple[str, ...]
    shader_ids: tuple[str, ...]
    unresolved_shader_ids: tuple[str, ...]
    elapsed_ms: float


def _unique_nonempty(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def prepare_usd_plugin_environment(
    *,
    dll_dirs: Iterable[str] = (),
    shader_ids: Iterable[str] = DEFAULT_SDR_SHADER_IDS,
    registry_factory: Callable | None = None,
) -> PluginEnvironmentResult:
    """Configure plugin DLL paths and initialize Sdr in the current process.

    On Windows, explicit ``dll_dirs`` are combined with the path-separator
    delimited ``OPENUSDCONNECT_DLL_DIRS`` value. Other USD plugin environment,
    including ``PXR_PLUGINPATH_NAME``, is inherited normally by the process.
    Call this before constructing a server or otherwise touching Sdr.
    """
    env_dirs = os.environ.get(PLUGIN_DLL_DIRS_ENV, "").split(os.pathsep)
    requested_dirs = _unique_nonempty((*dll_dirs, *env_dirs))
    missing_dirs = tuple(path for path in requested_dirs if not os.path.isdir(path))

    started = time.perf_counter()
    added_dirs = tuple(prepend_dll_dirs(requested_dirs))
    if registry_factory is None:
        from pxr import Sdr

        registry_factory = Sdr.Registry

    requested_shader_ids = _unique_nonempty(shader_ids)
    try:
        registry = registry_factory()
        unresolved = tuple(
            shader_id
            for shader_id in requested_shader_ids
            if registry.GetShaderNodeByIdentifier(shader_id) is None
        )
    except Exception as exc:
        raise PluginEnvironmentError(
            "USD plugin initialization failed. Launch the server from the project's "
            "USD environment; on Windows, provide dependency directories with "
            "--plugin-dll-dir or OPENUSDCONNECT_DLL_DIRS."
        ) from exc
    return PluginEnvironmentResult(
        requested_dll_dirs=requested_dirs,
        added_dll_dirs=added_dirs,
        missing_dll_dirs=missing_dirs,
        shader_ids=requested_shader_ids,
        unresolved_shader_ids=unresolved,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )
