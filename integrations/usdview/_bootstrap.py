"""Run usdview after appending OpenUSDConnect's environment to its Python path."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

APPEND_PATHS_ENV = "OPENUSDCONNECT_PYTHONPATH_APPEND"


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usdview bootstrap requires the usdview script path")

    usdview_script = Path(sys.argv[1]).resolve()
    for raw_path in os.environ.pop(APPEND_PATHS_ENV, "").split(os.pathsep):
        if not raw_path:
            continue
        resolved = str(Path(raw_path).resolve())
        if resolved not in sys.path:
            sys.path.append(resolved)

    sys.argv = [str(usdview_script), *sys.argv[2:]]
    runpy.run_path(str(usdview_script), run_name="__main__")


if __name__ == "__main__":
    main()
