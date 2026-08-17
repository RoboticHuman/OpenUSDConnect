"""Select the bundled or project-provided OpenUSD bindings."""

from __future__ import annotations

import os
import sys
from importlib import metadata

BUNDLED_USD_ENV = "OPENUSDCONNECT_BUNDLED_USD"


def _bundled_usd_installed() -> bool:
    try:
        metadata.version("usd-core")
    except metadata.PackageNotFoundError:
        return False
    return True


def _prefer_bundled_usd() -> None:
    configured = os.environ.pop("PYTHONPATH", "")
    if not configured:
        return
    distribution = metadata.distribution("usd-core")
    bundled_pxr = os.path.normcase(os.path.abspath(distribution.locate_file("pxr")))
    conflicting = set()
    retained = []
    for path in configured.split(os.pathsep):
        resolved = os.path.normcase(os.path.abspath(path or os.curdir))
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
        if os.path.normcase(os.path.abspath(path or os.curdir)) not in conflicting
    ]


def select_runtime() -> None:
    if _bundled_usd_installed():
        _prefer_bundled_usd()
        os.environ[BUNDLED_USD_ENV] = "1"
    else:
        os.environ.pop(BUNDLED_USD_ENV, None)
