"""Stream the committed Material Zoo fixture to optional live DCC viewers.

Examples:
    uv run python scripts/run_material_zoo.py --show --renderman
    uv run python scripts/run_material_zoo.py --viewers blender
    uv run python scripts/run_material_zoo.py --viewers usdview --renderman
    uv run python scripts/run_material_zoo.py --viewers blender usdview unreal --renderman

The viewers open only test_scene.usda, connect through their integrations, and
receive the same server backlog. The comparison camera and IBL are also sent as
events unless ``--no-presentation`` is supplied. Press Ctrl+C to close the
processes started by this runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from integrations.server_process import start as start_server_process
from integrations.server_process import stop as stop_process
from integrations.server_process import wait_until_listening
from openusdconnect.cli_common import (
    nonnegative_seconds,
    port_or_zero,
    positive_seconds,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_PATH = PROJECT_ROOT / "test_scene.usda"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "visual" / "fixtures" / "material_zoo.jsonl"
CAMERA_PATH = "/World/_TestCam"
DOME_PATH = "/World/_Dome"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream the Material Zoo through a real server into live viewers."
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Launch both usdview and Blender (shortcut for --viewers usdview blender).",
    )
    parser.add_argument(
        "--viewers",
        nargs="+",
        choices=("usdview", "blender", "unreal"),
        default=(),
        help="Launch only the selected viewers.",
    )
    parser.add_argument(
        "--port",
        type=port_or_zero,
        default=0,
        help="Server port; 0 selects a free port.",
    )
    parser.add_argument("--blender", help="Explicit Blender executable path.")
    parser.add_argument(
        "--download-blender",
        action="store_true",
        help="Download a repo-local portable Blender when none is installed.",
    )
    parser.add_argument("--usdview", help="Explicit usdview executable path.")
    parser.add_argument(
        "--unreal-engine-root",
        help="Explicit Unreal Engine root; otherwise use normal harness discovery.",
    )
    parser.add_argument(
        "--unreal-plugin-package",
        help="Reuse an existing packaged OpenUSDConnect Unreal plugin.",
    )
    parser.add_argument(
        "--rebuild-unreal-plugin",
        action="store_true",
        help="Rebuild the cached Unreal plugin package before launching.",
    )
    parser.add_argument(
        "--unreal-timeout",
        type=positive_seconds,
        default=240.0,
        help="Seconds to wait for Unreal to open the stage and connect.",
    )
    parser.add_argument(
        "--renderman",
        action="store_true",
        help="Start usdview with hdPrman and enable OpenPBR translation.",
    )
    parser.add_argument(
        "--no-presentation",
        action="store_true",
        help="Do not stream the shared comparison camera and StinsonBeach IBL.",
    )
    parser.add_argument(
        "--no-build-addon",
        action="store_true",
        help="Reuse dist/usd_connect_blender.zip instead of rebuilding it.",
    )
    parser.add_argument(
        "--publish-delay",
        type=nonnegative_seconds,
        default=2.0,
        help="Seconds to let viewers connect before publishing the events.",
    )
    parser.add_argument(
        "--exit-after",
        type=nonnegative_seconds,
        default=0.0,
        help="Automatically close launched viewers after N seconds; 0 waits until Ctrl+C.",
    )
    args = parser.parse_args(argv)
    viewers = set(args.viewers)
    if args.show:
        viewers.update(("usdview", "blender"))
    if not viewers:
        parser.error("choose --show or at least one --viewers entry")
    if args.download_blender and "blender" not in viewers:
        parser.error("--download-blender requires the blender viewer")
    args.viewers = viewers
    return args


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_port(requested: int) -> int:
    if requested:
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_server(port: int, event_log: Path) -> subprocess.Popen:
    process = start_server_process(
        [
            "--port",
            str(port),
            "--base",
            str(BASE_PATH),
            "--event-log",
            str(event_log),
        ],
        project_root=PROJECT_ROOT,
    )
    wait_until_listening(process, "127.0.0.1", port)
    return process


def _find_blender(explicit: str | None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("BLENDER_EXE"):
        candidates.append(Path(os.environ["BLENDER_EXE"]))
    config = PROJECT_ROOT / "blender.test.cfg"
    if config.is_file():
        configured = config.read_text(encoding="utf-8").strip()
        if configured and not configured.startswith("#"):
            candidates.append(Path(configured))
    candidates.extend(sorted((PROJECT_ROOT / ".blender").glob("blender*/blender.exe")))
    discovered = shutil.which("blender")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("Blender not found")


def _resolve_blender(explicit: str | None, *, download_missing: bool) -> Path:
    try:
        return _find_blender(explicit)
    except RuntimeError:
        if not download_missing:
            raise RuntimeError(
                "Blender not found; pass --blender, set BLENDER_EXE, run "
                "`uv run python scripts/setup_blender_test.py`, or add "
                "--download-blender"
            ) from None

    print("Blender not found; downloading a portable runtime...", flush=True)
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "setup_blender_test.py")],
        cwd=PROJECT_ROOT,
        check=True,
    )
    return _find_blender(explicit)


def _stinson_beach_for_usdview(executable: Path) -> str | None:
    from integrations.usdview.install import usd_install_root
    from integrations.visualtest.scene import stinson_beach_hdr

    install_root = usd_install_root(executable)
    if install_root is None:
        return None
    texture = Path(stinson_beach_hdr(install_root))
    if not texture.is_file():
        raise RuntimeError(f"selected usdview install is missing {texture.name}: {texture}")
    return str(texture)


def _build_presentation_events(
    fixture_events: list[dict], *, renderman: bool, dome_texture: str | None = None
) -> list[dict]:
    from integrations.renderman import apply_dll_dirs, ensure_renderman

    if renderman:
        ensure_renderman()
    else:
        apply_dll_dirs()

    from integrations.visualtest.replay import reconstruct
    from integrations.visualtest.scene import apply_ibl_dome, frame_front_camera
    from openusdconnect.emitter import NoticeEmitter

    stage = reconstruct(str(BASE_PATH), fixture_events)
    emitter = NoticeEmitter(stage)
    frame_front_camera(stage)
    apply_ibl_dome(stage, texture=dome_texture)
    events = emitter.build_events_for_dirty()
    unexpected = sorted(
        {
            event.get("prim", "")
            for event in events
            if event.get("prim") not in {CAMERA_PATH, DOME_PATH}
        }
    )
    if unexpected:
        raise RuntimeError(f"presentation changed non-presentation prims: {unexpected}")
    if not events:
        raise RuntimeError("presentation event generation produced no events")
    return events


def _camera_horizontal_fov(events: list[dict], camera_path: str) -> float | None:
    attributes = {}
    for event in events:
        if event.get("k") == "set_gprim_attrs" and event.get("prim") == camera_path:
            attributes.update(event.get("attrs", {}))
    aperture = attributes.get("horizontalAperture")
    focal_length = attributes.get("focalLength")
    if not aperture or not focal_length:
        return None
    return math.degrees(2.0 * math.atan(float(aperture) / (2.0 * float(focal_length))))


def _connectable_input_value(events: list[dict], prim_path: str, input_name: str):
    value = None
    for event in events:
        if event.get("k") == "set_connectable_input" and event.get("prim") == prim_path:
            if input_name in event.get("inputs", {}):
                value = event["inputs"][input_name]
    return value


def _launch_blender(
    executable: Path,
    temp_root: Path,
    port: int,
    expected_seq: int,
    camera_path: str,
) -> subprocess.Popen:
    addon_zip = PROJECT_ROOT / "dist" / "usd_connect_blender.zip"
    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = str(temp_root / "blender_user")
    return subprocess.Popen(
        [
            str(executable),
            "--factory-startup",
            "--python",
            str(PROJECT_ROOT / "scripts" / "material_zoo_blender_viewer.py"),
            "--",
            "--addon-zip",
            str(addon_zip),
            "--base",
            str(BASE_PATH),
            "--repo",
            str(PROJECT_ROOT),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--expected-seq",
            str(expected_seq),
            "--camera",
            camera_path,
        ],
        cwd=PROJECT_ROOT,
        env=env,
    )


def _prepare_unreal(temp_root: Path, port: int, args: argparse.Namespace):
    from integrations.unreal.test_harness import (
        UnrealTestError,
        create_test_project,
        package_plugin,
        resolve_engine,
    )

    engine = resolve_engine(explicit=args.unreal_engine_root)
    if args.unreal_plugin_package:
        plugin_package = Path(args.unreal_plugin_package).expanduser().resolve()
        if not (plugin_package / "OpenUSDConnect.uplugin").is_file():
            raise UnrealTestError(f"invalid Unreal plugin package: {plugin_package}")
    else:
        plugin_package = package_plugin(engine, force=args.rebuild_unreal_plugin)
    project = create_test_project(
        temp_root / "unreal_project",
        engine,
        plugin_package,
        port=port,
        enable_substrate=True,
    )
    return engine, project


def _launch_unreal(
    engine,
    project: Path,
    temp_root: Path,
    port: int,
    camera_path: str,
    camera_field_of_view: float | None,
    dome_path: str,
    dome_intensity: float | None,
    timeout: float,
) -> tuple[subprocess.Popen, Path, Path, Path, Path]:
    driver = PROJECT_ROOT / "scripts" / "material_zoo_unreal_viewer.py"
    startup_script = project.parent / "Content" / "Python" / "init_unreal.py"
    startup_script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(driver, startup_script)
    config_path = temp_root / "unreal_viewer.json"
    ready_path = temp_root / "unreal_ready"
    focused_path = temp_root / "unreal_camera_focused"
    failure_path = temp_root / "unreal_failure.json"
    unreal_log = temp_root / "unreal.log"
    config_path.write_text(
        json.dumps(
            {
                "base_stage": str(BASE_PATH),
                "camera_field_of_view": camera_field_of_view,
                "camera_path": camera_path,
                "dome_path": dome_path,
                "dome_intensity": dome_intensity,
                "failure_path": str(failure_path),
                "focused_path": str(focused_path),
                "port": port,
                "ready_path": str(ready_path),
                "timeout": timeout,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["OUC_MATERIAL_ZOO_UNREAL_CONFIG"] = str(config_path)
    process = subprocess.Popen(
        [
            str(engine.editor),
            str(project),
            "-nop4",
            "-nosplash",
            "-nosound",
            f"-abslog={unreal_log}",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    return process, ready_path, focused_path, failure_path, unreal_log


def _file_tail(path: Path, lines: int = 80) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _wait_for_unreal_signal(
    process: subprocess.Popen,
    signal_path: Path,
    failure_path: Path,
    unreal_log: Path,
    timeout: float,
    description: str,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if signal_path.is_file():
            return
        if failure_path.is_file():
            raise RuntimeError(
                "Unreal Material Zoo viewer failed:\n"
                + failure_path.read_text(encoding="utf-8", errors="replace")
            )
        return_code = process.poll()
        if return_code is not None:
            detail = _file_tail(unreal_log)
            suffix = f"\n{detail}" if detail else ""
            raise RuntimeError(
                f"Unreal viewer exited before {description} with code {return_code}{suffix}"
            )
        time.sleep(0.1)
    detail = _file_tail(unreal_log)
    suffix = f"\n{detail}" if detail else ""
    raise RuntimeError(f"Unreal viewer did not {description} within {timeout:g} seconds{suffix}")


def _wait_for_unreal_ready(
    process: subprocess.Popen,
    ready_path: Path,
    failure_path: Path,
    unreal_log: Path,
    timeout: float,
) -> None:
    _wait_for_unreal_signal(
        process,
        ready_path,
        failure_path,
        unreal_log,
        timeout,
        "connect",
    )


def _wait_for_unreal_camera(
    process: subprocess.Popen,
    focused_path: Path,
    failure_path: Path,
    unreal_log: Path,
    timeout: float,
) -> None:
    _wait_for_unreal_signal(
        process,
        focused_path,
        failure_path,
        unreal_log,
        timeout,
        "focus the Material Zoo camera",
    )


def _publish(port: int, fixture_events: list[dict], presentation_events: list[dict]) -> None:
    from openusdconnect.sender import EventSender

    sender = EventSender(
        host="127.0.0.1",
        port=port,
        client_id="material-zoo-live-runner",
        role="emitter",
        origin="material-zoo-live-runner",
    )
    if not sender.connect():
        raise RuntimeError(f"fixture emitter could not connect to 127.0.0.1:{port}")
    try:
        if not sender.send_events(fixture_events):
            raise RuntimeError("Material Zoo fixture transaction failed")
        if presentation_events and not sender.send_events(presentation_events):
            raise RuntimeError("Material Zoo presentation transaction failed")
    finally:
        sender.disconnect()


def _raise_if_viewer_failed(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        return_code = process.poll()
        if return_code not in (None, 0):
            raise RuntimeError(f"viewer process {process.pid} exited early with code {return_code}")


def main() -> int:
    args = _parse_args()
    from integrations.visualtest.replay import load_events

    blender_executable = None
    if "blender" in args.viewers:
        blender_executable = _resolve_blender(
            args.blender,
            download_missing=args.download_blender,
        )

    usdview_executable = None
    if "usdview" in args.viewers:
        from integrations.usdview.launcher import find_usdview

        usdview_executable = Path(args.usdview).resolve() if args.usdview else find_usdview()

    base_hash = _sha256(BASE_PATH)
    fixture_events = load_events(
        str(FIXTURE_PATH),
        subst={"{REPO}": PROJECT_ROOT.as_posix()},
    )
    presentation_events = (
        []
        if args.no_presentation
        else _build_presentation_events(
            fixture_events,
            renderman=args.renderman,
            dome_texture=(
                _stinson_beach_for_usdview(usdview_executable)
                if usdview_executable is not None
                else None
            ),
        )
    )
    expected_seq = len(fixture_events) + len(presentation_events)
    camera_path = CAMERA_PATH if presentation_events else ""
    camera_field_of_view = (
        _camera_horizontal_fov(presentation_events, camera_path) if camera_path else None
    )
    port = _select_port(args.port)

    if "blender" in args.viewers and not args.no_build_addon:
        subprocess.run(
            [sys.executable, "scripts/build_blender_addon.py"],
            cwd=PROJECT_ROOT,
            check=True,
        )

    with tempfile.TemporaryDirectory(prefix="openusdconnect-material-zoo-") as temp_dir:
        temp_root = Path(temp_dir)
        unreal_launch = None
        if "unreal" in args.viewers:
            unreal_launch = _prepare_unreal(temp_root, port, args)
        server = _start_server(port, temp_root / "events.db")
        viewer_processes: list[subprocess.Popen] = []
        unreal_camera_wait = None
        try:
            if "usdview" in args.viewers:
                from integrations.usdview.launcher import launch_usdview

                viewer_processes.append(
                    launch_usdview(
                        BASE_PATH,
                        host="127.0.0.1",
                        port=port,
                        usdview_exe=usdview_executable,
                        renderman=args.renderman,
                        camera_path=camera_path or None,
                        expected_seq=expected_seq,
                        scene_lights=bool(presentation_events),
                    )
                )
            if "blender" in args.viewers:
                assert blender_executable is not None
                viewer_processes.append(
                    _launch_blender(
                        blender_executable,
                        temp_root,
                        port,
                        expected_seq,
                        camera_path,
                    )
                )
            if unreal_launch is not None:
                (
                    unreal_process,
                    ready_path,
                    focused_path,
                    failure_path,
                    unreal_log,
                ) = _launch_unreal(
                    *unreal_launch,
                    temp_root,
                    port,
                    camera_path,
                    camera_field_of_view,
                    DOME_PATH if presentation_events else "",
                    _connectable_input_value(
                        presentation_events,
                        DOME_PATH,
                        "intensity",
                    ),
                    args.unreal_timeout,
                )
                viewer_processes.append(unreal_process)
                unreal_camera_wait = (
                    unreal_process,
                    focused_path,
                    failure_path,
                    unreal_log,
                    args.unreal_timeout,
                )
                print("Waiting for Unreal to open the stage and connect...", flush=True)
                _wait_for_unreal_ready(
                    unreal_process,
                    ready_path,
                    failure_path,
                    unreal_log,
                    args.unreal_timeout,
                )

            if args.publish_delay > 0:
                time.sleep(args.publish_delay)
            _raise_if_viewer_failed(viewer_processes)
            _publish(port, fixture_events, presentation_events)
            if unreal_camera_wait is not None:
                print("Waiting for Unreal to focus the shared camera...", flush=True)
                _wait_for_unreal_camera(*unreal_camera_wait)
            if _sha256(BASE_PATH) != base_hash:
                raise RuntimeError("test_scene.usda changed while publishing live events")

            print(
                f"Material Zoo streamed to {', '.join(sorted(args.viewers))} on port {port}: "
                f"{len(fixture_events)} fixture + {len(presentation_events)} presentation "
                f"events (expected seq={expected_seq}).",
                flush=True,
            )
            print("Close the viewers or press Ctrl+C to stop the runner.", flush=True)
            exit_deadline = time.monotonic() + args.exit_after if args.exit_after > 0 else None
            while any(process.poll() is None for process in viewer_processes):
                _raise_if_viewer_failed(viewer_processes)
                if exit_deadline is not None and time.monotonic() >= exit_deadline:
                    break
                time.sleep(0.25)
        except KeyboardInterrupt:
            print("Stopping Material Zoo viewers...", flush=True)
        finally:
            for process in reversed(viewer_processes):
                stop_process(process)
            stop_process(server)
    return 0


def _run_cli() -> int:
    try:
        return main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_run_cli())
