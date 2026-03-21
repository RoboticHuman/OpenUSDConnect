"""Generate .vscode/launch.json and .vscode/tasks.json for OpenUSDConnect.

Idempotent — safe to re-run. Overwrites existing files.

If .blender_addon_path exists (written by the bootstrap script on first
launch), pathMappings will map the Blender addon install directory back to
your workspace source files so debugpy breakpoints work.

Usage:
    python scripts/setup_vscode.py
"""

from __future__ import annotations

import json
import os


def _read_addon_path(repo_root: str) -> str | None:
    """Read the Blender addon install path written by the bootstrap script."""
    path_file = os.path.join(repo_root, ".blender_addon_path")
    if not os.path.isfile(path_file):
        return None
    try:
        content = open(path_file, encoding="utf-8").read().strip()
        return content if content else None
    except OSError:
        return None


def _build_path_mappings(repo_root: str, addon_path: str | None) -> list[dict]:
    """Build debugpy pathMappings for launch.json.

    The addon is installed to Blender's addon directory as:
        <addon_dir>/capture.py          <- from integrations/blender/
        <addon_dir>/openusdconnect/     <- from openusdconnect/

    We need two mappings so VS Code can match breakpoints in your workspace
    source files to the code actually running inside Blender.
    """
    if addon_path:
        return [
            {
                "localRoot": "${workspaceFolder}/integrations/blender",
                "remoteRoot": addon_path,
            },
            {
                "localRoot": "${workspaceFolder}/openusdconnect",
                "remoteRoot": os.path.join(addon_path, "openusdconnect"),
            },
        ]
    # Fallback when addon path is unknown — won't hit breakpoints but
    # at least the attach config exists.
    return [
        {
            "localRoot": "${workspaceFolder}",
            "remoteRoot": "${workspaceFolder}",
        }
    ]


def _build_launch_json(path_mappings: list[dict]) -> dict:
    return {
        "version": "0.2.0",
        "configurations": [
            {
                "name": "Attach: Blender A (debugpy :5678)",
                "type": "debugpy",
                "request": "attach",
                "connect": {"host": "127.0.0.1", "port": 5678},
                "justMyCode": False,
                "pathMappings": path_mappings,
            },
            {
                "name": "Attach: Blender B (debugpy :5679)",
                "type": "debugpy",
                "request": "attach",
                "connect": {"host": "127.0.0.1", "port": 5679},
                "justMyCode": False,
                "pathMappings": path_mappings,
            },
        ],
        "compounds": [
            {
                "name": "Attach All USD Connect",
                "configurations": [
                    "Attach: Blender A (debugpy :5678)",
                    "Attach: Blender B (debugpy :5679)",
                ],
            }
        ],
    }


TASKS_JSON = {
    "version": "2.0.0",
    "tasks": [
        {
            "label": "Launch USD Connect (1 Blender)",
            "type": "shell",
            "command": ".\\scripts\\start_usdconnect_debug.ps1 -WaitForDebugger",
            "problemMatcher": [],
            "presentation": {"reveal": "always", "panel": "new"},
        },
        {
            "label": "Launch USD Connect (2 Blenders)",
            "type": "shell",
            "command": ".\\scripts\\start_usdconnect_debug.ps1 -WaitForDebugger -TwoBlenders",
            "problemMatcher": [],
            "presentation": {"reveal": "always", "panel": "new"},
        },
    ],
}


def main() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vscode_dir = os.path.join(repo_root, ".vscode")
    os.makedirs(vscode_dir, exist_ok=True)

    addon_path = _read_addon_path(repo_root)
    if addon_path:
        print(f"[setup_vscode] Using addon path: {addon_path}")
    else:
        print(
            "[setup_vscode] WARNING: .blender_addon_path not found. "
            "Launch Blender once with the debug script, then re-run this."
        )

    path_mappings = _build_path_mappings(repo_root, addon_path)
    launch_json = _build_launch_json(path_mappings)

    launch_path = os.path.join(vscode_dir, "launch.json")
    tasks_path = os.path.join(vscode_dir, "tasks.json")

    for path, data in [(launch_path, launch_json), (tasks_path, TASKS_JSON)]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        print(f"[setup_vscode] Wrote {os.path.relpath(path, repo_root)}")

    print("[setup_vscode] Done.")


if __name__ == "__main__":
    main()
