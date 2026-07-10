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
For headless hdPrman *rendering*, :func:`ensure_renderman` additionally exports
``RMANTREE`` and the ``RMAN_*`` search paths hdPrman needs to resolve MaterialX
shaders (:func:`renderman_env`); the usdview launcher applies the same env.
``RMANTREE`` is set machine-wide by the RenderMan installer; :func:`dll_dirs`
reads that machine value directly when the process env lacks it, so this needs
no extra configuration and never modifies the system-wide ``PATH``.
"""

from __future__ import annotations

import os
from pathlib import Path

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


def usd_install_root() -> str:
    """The OpenUSD install prefix (the dir whose ``plugin/usd`` holds hdPrman).

    Derived from ``pxr``'s location: ``<root>/lib/python/pxr/__init__.py``.
    """
    import pxr

    return str(Path(pxr.__file__).resolve().parents[3])


def renderman_env(install_root: str | os.PathLike | None = None) -> dict[str, str]:
    """Build the ``RMAN_*`` environment hdPrman needs to load as a Hydra delegate.

    ``install_root`` is the OpenUSD install prefix (parent of ``bin``/``plugin``);
    derived from ``pxr`` when omitted. ``RMAN_SHADERPATH`` includes the install's
    ``plugin/usd/resources/shaders`` so hdPrman resolves MaterialX shaders.
    Returns a dict for ``os.environ.update``; empty when RenderMan is not found.
    See https://openusd.org/release/plugins_renderman.html
    """
    rt = rmantree()
    if not rt:
        return {}
    root = Path(rt.rstrip("\\/"))
    inst = Path(str(install_root) if install_root else usd_install_root())
    plugin_usd = inst / "plugin" / "usd"
    plugins = root / "lib" / "plugins"
    sep = os.pathsep
    return {
        "RMANTREE": str(root),
        "RMAN_SHADERPATH": sep.join(
            [str(root / "lib" / "shaders"), str(plugin_usd / "resources" / "shaders")]
        ),
        "RMAN_RIXPLUGINPATH": str(plugins),
        "RMAN_TEXTUREPATH": sep.join(
            [str(root / "lib" / "textures"), str(plugins), str(plugin_usd)]
        ),
        "RMAN_DISPLAYPATH": str(plugins),
        "RMAN_PROCEDURALPATH": str(plugins),
    }


def ensure_renderman() -> str:
    """Bootstrap RenderMan for headless hdPrman rendering; return ``RMANTREE`` or "".

    Exports ``RMANTREE``, prepends its DLL dirs (so the Sdr registry and hdPrman
    load), and sets the ``RMAN_*`` search paths hdPrman needs to resolve MaterialX
    shaders. No-op-returns-"" when RenderMan is not installed. Safe to call again.
    """
    rt = rmantree()
    if not rt:
        return ""
    os.environ.setdefault("RMANTREE", rt)
    apply_dll_dirs()
    for key, val in renderman_env().items():
        os.environ.setdefault(key, val)
    return rt
