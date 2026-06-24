"""Make Pixar RenderMan's USD plugins loadable for any pxr process (Windows).

Installing RenderMan drops its Sdr parser and Hydra delegate plugins into the
shared USD install. Those plugins' DLLs resolve their dependencies (``libprman``
etc.) via ``PATH`` at load time, so ``$RMANTREE/bin`` and ``$RMANTREE/lib`` must
be on ``PATH`` before anything instantiates the Sdr registry, otherwise the
registry load fails and unrelated work (the sync server's log replay, MCP
introspection, even Storm in usdview) crashes.

This is the single place that knows about RenderMan. The core stays renderer-
agnostic; entry points that may touch USD plugins (the server wrapper, the MCP,
the usdview launcher, the test suite) call :func:`apply_dll_dirs` at startup.
Any standalone receiver that builds a receive pipeline (``ReceiverThread`` /
``EventDispatcher``) directly must do the same before its first apply, or set
``OPENUSDCONNECT_DLL_DIRS`` so ``openusdconnect.dll_paths.apply_env_dll_dirs``
picks the dirs up. ``RMANTREE`` is set machine-wide by the RenderMan installer;
:func:`dll_dirs` reads that machine value directly when the process env lacks it
(e.g. a shell started before the install was run), so this needs no extra
configuration and never modifies the system-wide ``PATH``.
"""

from __future__ import annotations

import os

from openusdconnect.dll_paths import prepend_dll_dirs


def rmantree() -> str:
    """Resolve ``RMANTREE``: process env first, then the machine-scope value.

    A process whose env predates the RenderMan install carries no ``RMANTREE``
    even though the installer set it machine-wide; reading the registry value
    directly self-heals that without refreshing the shell or touching PATH.
    """
    rt = os.environ.get("RMANTREE", "").strip()
    if rt or os.name != "nt":
        return rt
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"System\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            return winreg.QueryValueEx(key, "RMANTREE")[0].strip()
    except FileNotFoundError:  # RMANTREE absent / RenderMan not installed
        return ""


def dll_dirs() -> list[str]:
    """Return ``[$RMANTREE/bin, $RMANTREE/lib]`` if ``RMANTREE`` resolves, else ``[]``."""
    root = rmantree()
    if not root:
        return []
    root = root.rstrip("\\/")
    return [os.path.join(root, "bin"), os.path.join(root, "lib")]


def apply_dll_dirs() -> list[str]:
    """Prepend RenderMan's runtime dirs to the process DLL search path.

    Returns the dirs actually added. No-op when ``RMANTREE`` is unset or off
    Windows. Safe to call more than once.
    """
    return prepend_dll_dirs(dll_dirs())
