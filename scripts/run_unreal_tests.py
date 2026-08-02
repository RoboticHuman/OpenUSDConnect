#!/usr/bin/env python3
"""Provision an Unreal project and run the OpenUSDConnect E2E scenario."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from integrations.unreal.test_harness import (  # noqa: E402
    UnrealTestError,
    discover_engines,
    install_plugin_in_project,
    package_plugin,
    resolve_engine,
    run_unreal_e2e,
    temporary_run_directory,
)
from openusdconnect.cli_common import positive_seconds  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run OpenUSDConnect against a generated or existing Unreal project.",
    )
    parser.add_argument(
        "--engine-root",
        help="Unreal Engine root or UnrealEditor executable; auto-detected when omitted",
    )
    parser.add_argument("--project", type=Path, help="Use an existing .uproject")
    parser.add_argument(
        "--plugin-package",
        type=Path,
        help="Use an existing BuildPlugin output instead of the package cache",
    )
    parser.add_argument(
        "--rebuild-plugin",
        action="store_true",
        help="Rebuild the plugin even when a matching cached package exists",
    )
    parser.add_argument(
        "--install-plugin",
        action="store_true",
        help="Install and enable the test plugins in an existing --project",
    )
    parser.add_argument(
        "--replace-plugin",
        action="store_true",
        help="Replace an existing project plugin; requires --install-plugin",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Open the GUI and leave the verified scene and server running",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Keep the generated project, stage, logs, and results in this directory",
    )
    parser.add_argument("--timeout", type=positive_seconds, default=240.0)
    parser.add_argument(
        "--list-engines",
        action="store_true",
        help="List detected Unreal installations and exit",
    )
    return parser


def _run(args) -> int:
    if args.replace_plugin and not args.install_plugin:
        raise UnrealTestError("--replace-plugin requires --install-plugin")
    if args.install_plugin and args.project is None:
        raise UnrealTestError("--install-plugin is only needed with --project")

    if args.list_engines:
        engines = discover_engines(explicit=args.engine_root, project=args.project)
        for engine in engines:
            flavor = "installed" if engine.installed_build else "source"
            print(
                f"{engine.version} ({flavor}, {engine.source})\n"
                f"  root: {engine.root}\n"
                f"  editor: {engine.editor_cmd}"
            )
        return 0 if engines else 1

    engine = resolve_engine(explicit=args.engine_root, project=args.project)
    plugin_package = args.plugin_package
    if plugin_package is not None:
        plugin_package = plugin_package.expanduser().resolve()
        if not (plugin_package / "OpenUSDConnect.uplugin").is_file():
            raise UnrealTestError(f"invalid plugin package: {plugin_package}")
    if args.install_plugin:
        plugin_package = plugin_package or package_plugin(
            engine,
            force=args.rebuild_plugin,
        )
        install_plugin_in_project(
            args.project,
            plugin_package,
            replace=args.replace_plugin,
        )

    def execute(work_dir: Path):
        result = run_unreal_e2e(
            engine,
            work_dir,
            project=args.project,
            plugin_package=plugin_package,
            rebuild_plugin=args.rebuild_plugin,
            interactive=args.interactive,
            timeout=args.timeout,
        )
        print(json.dumps(result.result, indent=2, sort_keys=True))
        print(f"Project: {result.project}")
        print(f"Unreal log: {result.unreal_log}")
        print(f"Unreal console log: {result.unreal_console_log}")
        print(f"Server log: {result.server_log}")

    if args.work_dir:
        execute(args.work_dir.expanduser().resolve())
    else:
        with temporary_run_directory() as directory:
            execute(Path(directory))
    return 0


def main() -> None:
    try:
        raise SystemExit(_run(_parser().parse_args()))
    except UnrealTestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
