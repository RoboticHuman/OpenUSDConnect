"""Make externally-installed USD plugin DLLs loadable at runtime (Windows).

Some USD plugins (third-party Sdr parsers, Hydra render delegates) ship
shared libraries whose dependencies live outside the USD install. On Windows
those dependencies are resolved via ``PATH`` at ``LoadLibrary`` time, and the
plugin registry instantiates every registered plugin on first use, so the
directories holding those dependencies must be on ``PATH`` before the registry
is first touched (or the whole registry load fails).

These helpers prepend such directories to the current process's ``PATH``.
They are generic: the caller supplies the directories; this module knows
nothing about any specific renderer or DCC.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

PLUGIN_DLL_DIRS_ENV = "OPENUSDCONNECT_DLL_DIRS"


def prepend_dll_dirs(dirs: Iterable[str]) -> list[str]:
    """Prepend existing directories to the process ``PATH`` for DLL resolution.

    Returns the directories actually added (skips missing ones, those already
    on ``PATH``, and is a no-op off Windows). Mutating ``os.environ['PATH']``
    affects subsequent ``LoadLibrary`` calls, the mechanism USD's plugin
    loader uses to find a plugin's external dependencies. ``os.add_dll_directory``
    is intentionally not used: the loader does not consult those user dirs.
    """
    if os.name != "nt":
        return []
    path = os.environ.get("PATH", "")
    have = path.split(os.pathsep)
    added = [d for d in dirs if d and os.path.isdir(d) and d not in have]
    if added:
        os.environ["PATH"] = os.pathsep.join([*added, path])
    return added


def apply_env_dll_dirs(env_var: str = PLUGIN_DLL_DIRS_ENV) -> list[str]:
    """Prepend the ``os.pathsep``-separated directories named in ``env_var``."""
    return prepend_dll_dirs(os.environ.get(env_var, "").split(os.pathsep))
