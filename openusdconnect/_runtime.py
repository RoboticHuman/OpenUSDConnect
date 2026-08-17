"""Select the bundled or project-provided OpenUSD bindings."""

from __future__ import annotations

import os
import sys
from importlib import metadata

BUNDLED_USD_ENV = "OPENUSDCONNECT_BUNDLED_USD"
USD_ROOT_ENV = "OPENUSDCONNECT_USD_ROOT"


def _normalized_path(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(path))


def _bundled_pxr_path() -> str | None:
    try:
        distribution = metadata.distribution("usd-core")
    except metadata.PackageNotFoundError:
        return None
    return _normalized_path(distribution.locate_file("pxr"))


def _loaded_pxr_path() -> str | None:
    module = sys.modules.get("pxr")
    if module is None:
        return None
    module_file = getattr(module, "__file__", "")
    return _normalized_path(os.path.dirname(module_file)) if module_file else ""


def _prefer_bundled_usd(bundled_pxr: str) -> None:
    configured = os.environ.pop("PYTHONPATH", "")
    if not configured:
        return
    conflicting = set()
    retained = []
    for path in configured.split(os.pathsep):
        resolved = _normalized_path(path or os.curdir)
        candidate_pxr = os.path.join(resolved, "pxr")
        if os.path.isdir(candidate_pxr) and candidate_pxr != bundled_pxr:
            conflicting.add(resolved)
        else:
            retained.append(path)
    if retained:
        os.environ["PYTHONPATH"] = os.pathsep.join(retained)
    sys.path[:] = [
        path
        for path in sys.path
        if _normalized_path(path or os.curdir) not in conflicting
    ]


def select_runtime() -> None:
    if os.environ.get(USD_ROOT_ENV):
        os.environ.pop(BUNDLED_USD_ENV, None)
        return
    bundled_pxr = _bundled_pxr_path()
    if bundled_pxr is None:
        os.environ.pop(BUNDLED_USD_ENV, None)
        return
    loaded_pxr = _loaded_pxr_path()
    if loaded_pxr is None:
        _prefer_bundled_usd(bundled_pxr)
    if loaded_pxr in (None, bundled_pxr):
        os.environ[BUNDLED_USD_ENV] = "1"
    else:
        os.environ.pop(BUNDLED_USD_ENV, None)
