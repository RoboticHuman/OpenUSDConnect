"""Module-lifetime helpers for the packaged Blender addon."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from types import ModuleType

_CORE_PACKAGE = "openusdconnect"
_NATIVE_CLIENT = f"{_CORE_PACKAGE}._native_client"


def refresh_core_modules(
    modules: MutableMapping[str, ModuleType],
    bundled_prefix: str,
    *,
    addon_reload: bool,
) -> None:
    """Discard replaceable core modules while retaining the native extension.

    CPython extension modules are process-lifetime objects. Removing the
    nanobind module from ``sys.modules`` and importing it again attempts to
    register its C++ types twice, which terminates Blender. Pure-Python modules
    may still be refreshed when the addon is reloaded or replaces a checkout
    imported through ``PYTHONPATH``.
    """

    normalized_prefix = os.path.normcase(os.path.abspath(bundled_prefix))
    for name in list(modules):
        if name != _CORE_PACKAGE and not name.startswith(f"{_CORE_PACKAGE}."):
            continue
        if name == _NATIVE_CLIENT:
            continue

        module_file = getattr(modules[name], "__file__", "") or ""
        normalized_file = os.path.normcase(os.path.abspath(module_file)) if module_file else ""
        loaded_from_bundle = normalized_file == normalized_prefix or normalized_file.startswith(
            normalized_prefix + os.sep
        )
        if addon_reload or not loaded_from_bundle:
            del modules[name]
