"""Launch the OpenUSDConnect sync server with renderer plugin DLLs resolvable.

The core server (``openusdconnect.server``) is renderer-agnostic, but the shared
USD install may contain a renderer's Sdr/Hydra plugins (e.g. RenderMan) whose
DLLs must be on ``PATH`` or the plugin registry fails to load, which would
crash the server during event-log replay. This thin, non-core wrapper makes
those DLLs loadable, then hands off to the real server entry point. All
arguments are forwarded verbatim.

    uv run python -m integrations.run_server --port 7200 --base scene.usda --log events.db
"""

from __future__ import annotations

from openusdconnect.dll_paths import apply_env_dll_dirs

from .renderman import apply_dll_dirs


def main() -> None:
    apply_dll_dirs()  # RenderMan, discovered from $RMANTREE
    apply_env_dll_dirs()  # any extra dirs from $OPENUSDCONNECT_DLL_DIRS
    # Import after the DLL search path is set up, before the registry is touched.
    from openusdconnect.server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
