"""Stream the committed Material Zoo fixture to optional live DCC viewers.

Examples:
    uv run python scripts/run_material_zoo.py --show --renderman
    uv run python scripts/run_material_zoo.py --viewers blender
    uv run python scripts/run_material_zoo.py --viewers usdview --renderman

Both viewers open only test_scene.usda, connect through their integrations, and
receive the same server backlog. The comparison camera and IBL are also sent as
events unless ``--no-presentation`` is supplied. Press Ctrl+C to close the
processes started by this runner.
"""

from __future__ import annotations

import argparse
import hashlib
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
from openusdconnect.cli_common import nonnegative_seconds, port_or_zero

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_PATH = PROJECT_ROOT / "test_scene.usda"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "visual" / "fixtures" / "material_zoo.jsonl"
CAMERA_PATH = "/World/_TestCam"
DOME_PATH = "/World/_Dome"


def _parse_args() -> argparse.Namespace:
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
        choices=("usdview", "blender"),
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
    parser.add_argument("--usdview", help="Explicit usdview executable path.")
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
    args = parser.parse_args()
    viewers = set(args.viewers)
    if args.show:
        viewers.update(("usdview", "blender"))
    if not viewers:
        parser.error("choose --show or at least one --viewers entry")
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
    raise RuntimeError("Blender not found; pass --blender or set BLENDER_EXE")


def _build_presentation_events(fixture_events: list[dict], *, renderman: bool) -> list[dict]:
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
    apply_ibl_dome(stage)
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


def main() -> int:
    args = _parse_args()
    from integrations.visualtest.replay import load_events

    base_hash = _sha256(BASE_PATH)
    fixture_events = load_events(
        str(FIXTURE_PATH),
        subst={"{REPO}": PROJECT_ROOT.as_posix()},
    )
    presentation_events = (
        []
        if args.no_presentation
        else _build_presentation_events(fixture_events, renderman=args.renderman)
    )
    expected_seq = len(fixture_events) + len(presentation_events)
    camera_path = CAMERA_PATH if presentation_events else ""
    port = _select_port(args.port)

    if "blender" in args.viewers and not args.no_build_addon:
        subprocess.run(
            [sys.executable, "scripts/build_blender_addon.py"],
            cwd=PROJECT_ROOT,
            check=True,
        )

    with tempfile.TemporaryDirectory(prefix="openusdconnect-material-zoo-") as temp_dir:
        temp_root = Path(temp_dir)
        server = _start_server(port, temp_root / "events.db")
        viewer_processes: list[subprocess.Popen] = []
        try:
            if "usdview" in args.viewers:
                from integrations.usdview.launcher import launch_usdview

                viewer_processes.append(
                    launch_usdview(
                        BASE_PATH,
                        host="127.0.0.1",
                        port=port,
                        usdview_exe=args.usdview,
                        renderman=args.renderman,
                        camera_path=camera_path or None,
                        expected_seq=expected_seq,
                        scene_lights=bool(presentation_events),
                    )
                )
            if "blender" in args.viewers:
                viewer_processes.append(
                    _launch_blender(
                        _find_blender(args.blender),
                        temp_root,
                        port,
                        expected_seq,
                        camera_path,
                    )
                )

            if args.publish_delay > 0:
                time.sleep(args.publish_delay)
            _publish(port, fixture_events, presentation_events)
            if _sha256(BASE_PATH) != base_hash:
                raise RuntimeError("test_scene.usda changed while publishing live events")

            print(
                f"Material Zoo streamed to {', '.join(sorted(args.viewers))} on port {port}: "
                f"{len(fixture_events)} fixture + {len(presentation_events)} presentation "
                f"events (expected seq={expected_seq}).",
                flush=True,
            )
            print("Close the viewers or press Ctrl+C to stop the runner.", flush=True)
            exit_deadline = (
                time.monotonic() + args.exit_after if args.exit_after > 0 else None
            )
            while any(process.poll() is None for process in viewer_processes):
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


if __name__ == "__main__":
    raise SystemExit(main())
