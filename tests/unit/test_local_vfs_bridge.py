from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_bridge():
    path = Path(__file__).parents[2] / "scripts" / "local_vfs_bridge.py"
    name = "local_vfs_bridge"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_status_file_roundtrip(tmp_path, capsys):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"

    bridge._write_status(
        status_file,
        state="running",
        exposure_kind="local-directory",
        root_path=str(tmp_path),
        file_path=str(tmp_path / "scene.usd"),
        etag='"1-2"',
    )
    assert bridge.main(["status", "--status-file", str(status_file)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "running"
    assert payload["exposure_kind"] == "local-directory"
    assert payload["file_path"] == str(tmp_path / "scene.usd")
    assert payload["etag"] == '"1-2"'
    assert payload["updated_at"]


def test_default_control_files_live_outside_mirror(tmp_path):
    bridge = _load_bridge()
    mount_dir = tmp_path / "usd"

    assert bridge._default_status_file(mount_dir) == (
        tmp_path / "bridge" / "openusdconnect_bridge_status.json"
    )
    assert bridge._default_log_file(mount_dir) == (
        tmp_path / "bridge" / "openusdconnect_bridge.log"
    )


def test_control_files_are_removed_from_mirror(tmp_path):
    bridge = _load_bridge()
    mount_dir = tmp_path / "usd"
    mount_dir.mkdir()
    status = mount_dir / "openusdconnect_bridge_status.json"
    log = mount_dir / "openusdconnect_bridge.log"
    scene = mount_dir / "scene.usd"
    status.write_text("{}", encoding="utf-8")
    log.write_text("log", encoding="utf-8")
    scene.write_text("#usda 1.0\n", encoding="utf-8")

    bridge._remove_control_files_from_mirror(mount_dir)

    assert not status.exists()
    assert not log.exists()
    assert scene.exists()


def test_content_changed_ignores_same_bytes(tmp_path):
    bridge = _load_bridge()
    path = tmp_path / "scene.usd"
    path.write_bytes(b"#usda 1.0\n")
    initial_hash = bridge._hash_file(path)

    path.write_bytes(b"#usda 1.0\n")
    changed, current_hash = bridge._content_changed(path, initial_hash)

    assert changed is False
    assert current_hash == initial_hash


def test_content_changed_detects_new_bytes(tmp_path):
    bridge = _load_bridge()
    path = tmp_path / "scene.usd"
    path.write_bytes(b"#usda 1.0\n")
    initial_hash = bridge._hash_file(path)

    path.write_bytes(b'#usda 1.0\ndef Xform "World" {}\n')
    changed, current_hash = bridge._content_changed(path, initial_hash)

    assert changed is True
    assert current_hash != initial_hash


def test_upload_sends_if_match_header(tmp_path, monkeypatch):
    bridge = _load_bridge()
    path = tmp_path / "scene.usd"
    path.write_bytes(b"#usda 1.0\n")
    calls = []

    def fake_request(method, url, body=None, headers=None):
        calls.append((method, url, body, headers))
        return 200, {}, b""

    monkeypatch.setattr(bridge, "_request", fake_request)

    bridge._upload("http://127.0.0.1:7280/usd/scene.usd", path, '"1-2"')

    assert calls == [
        (
            "PUT",
            "http://127.0.0.1:7280/usd/scene.usd",
            b"#usda 1.0\n",
            {"If-Match": '"1-2"'},
        )
    ]


def test_request_normalizes_response_header_names(monkeypatch):
    bridge = _load_bridge()

    class Response:
        status = 200

        @staticmethod
        def read():
            return b"data"

        @staticmethod
        def getheaders():
            return [("eTaG", '"1-2"'), ("CONTENT-LENGTH", "4")]

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(bridge.http.client, "HTTPConnection", Connection)

    status, headers, data = bridge._request("GET", "http://localhost/scene.usd")

    assert status == 200
    assert data == b"data"
    assert headers == {"etag": '"1-2"', "content-length": "4"}


def test_request_rejects_non_http_vfs_url():
    bridge = _load_bridge()

    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        bridge._request("GET", "file:///tmp/scene.usd")


def test_url_filename_decodes_escaped_characters():
    bridge = _load_bridge()

    assert bridge._url_filename("http://localhost/usd/live%20scene.usd") == "live scene.usd"


def test_remote_download_refuses_to_overwrite_edit_made_during_get(tmp_path, monkeypatch):
    bridge = _load_bridge()
    path = tmp_path / "scene.usd"
    path.write_bytes(b"old local")
    baseline = bridge._hash_file(path)

    def request(_method, _url, body=None, headers=None):
        assert body is None
        assert headers is None
        path.write_bytes(b"new local edit")
        return 200, {"etag": '"2-0"'}, b"remote update"

    monkeypatch.setattr(bridge, "_request", request)

    with pytest.raises(bridge.LocalFileChangedError, match="refusing to overwrite"):
        bridge._download("http://localhost/scene.usd", path, expected_local_hash=baseline)

    assert path.read_bytes() == b"new local edit"


def test_unchanged_stat_does_not_reopen_mirror_file(tmp_path, monkeypatch):
    bridge = _load_bridge()
    target = tmp_path / "scene.usd"
    target.write_bytes(b"content")
    observation = bridge._observe_file(target)
    monkeypatch.setattr(
        bridge,
        "_observe_file",
        lambda _path: pytest.fail("unchanged polls must not reopen the mirror file"),
    )

    assert bridge._observe_if_changed(target, observation) is observation


def test_save_candidate_waits_for_one_stable_version():
    bridge = _load_bridge()
    first = bridge.FileObservation(1, 10, "first")
    final = bridge.FileObservation(2, 20, "final")

    candidate, ready = bridge._advance_save_candidate(
        None,
        first,
        synced_hash="base",
        blocked_hash="",
        now=1.0,
        settle=0.5,
    )
    assert ready is False

    candidate, ready = bridge._advance_save_candidate(
        candidate,
        final,
        synced_hash="base",
        blocked_hash="",
        now=1.6,
        settle=0.5,
    )
    assert ready is False

    candidate, ready = bridge._advance_save_candidate(
        candidate,
        final,
        synced_hash="base",
        blocked_hash="",
        now=2.11,
        settle=0.5,
    )
    assert ready is True


def test_status_write_retries_sharing_violations_and_skips_unchanged(tmp_path, monkeypatch):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"
    real_replace = bridge.os.replace
    calls = 0

    def flaky_replace(source, target):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("shared")
        real_replace(source, target)

    monkeypatch.setattr(bridge.os, "replace", flaky_replace)
    monkeypatch.setattr(bridge.time, "sleep", lambda _delay: None)

    assert bridge._write_status(status_file, state="running", pid=123) is True
    assert calls == 3
    assert bridge._write_status(status_file, state="running", pid=123) is True
    assert calls == 3


def test_status_write_failure_is_nonfatal(tmp_path, monkeypatch):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"
    monkeypatch.setattr(
        bridge.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(PermissionError("shared")),
    )
    monkeypatch.setattr(bridge.time, "sleep", lambda _delay: None)

    assert bridge._write_status(status_file, state="running") is False
    assert not status_file.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_directory_exposure_uses_local_paths(tmp_path):
    bridge = _load_bridge()

    exposure = bridge._prepare_exposure(
        tmp_path,
        "scene.usd",
        config=bridge.DirectoryExposureConfig(),
    )

    assert exposure.kind == "local-directory"
    assert exposure.root_path == str(tmp_path)
    assert exposure.file_path == str(tmp_path / "scene.usd")
    assert exposure.drive == ""


def test_bridge_help_only_shows_platform_exposure_options():
    bridge = _load_bridge()

    macos_help = bridge._build_run_parser(is_windows=False).format_help()
    windows_help = bridge._build_run_parser(is_windows=True).format_help()

    for option in ("--drive", "--no-drive", "--force", "--release-on-exit"):
        assert option not in macos_help
        assert option in windows_help


def test_bridge_parser_rejects_windows_exposure_options_on_other_platforms():
    bridge = _load_bridge()

    with pytest.raises(SystemExit):
        bridge._parse_bridge_config(["--drive", "O:"], is_windows=False)


@pytest.mark.parametrize("modifier", ["--force", "--release-on-exit"])
def test_bridge_parser_rejects_drive_modifiers_for_directory_exposure(modifier):
    bridge = _load_bridge()

    with pytest.raises(SystemExit) as error:
        bridge._parse_bridge_config(["--no-drive", modifier], is_windows=True)

    assert error.value.code == 2


def test_bridge_parser_builds_typed_platform_exposure_configs(tmp_path):
    bridge = _load_bridge()

    directory = bridge._parse_bridge_config(
        ["--mirror-dir", str(tmp_path)],
        is_windows=False,
    )
    drive = bridge._parse_bridge_config(
        ["--mirror-dir", str(tmp_path), "--force", "--release-on-exit"],
        is_windows=True,
    )

    assert isinstance(directory.exposure, bridge.DirectoryExposureConfig)
    assert isinstance(drive.exposure, bridge.WindowsDriveExposureConfig)
    assert drive.exposure.drive == "O:"
    assert drive.exposure.force is True
    assert drive.exposure.release_on_exit is True


def test_bridge_parser_uses_canonical_names_and_accepts_legacy_aliases(tmp_path):
    bridge = _load_bridge()
    canonical = bridge._parse_bridge_config(
        [
            "--vfs-url",
            "http://localhost/live.usd",
            "--mirror-dir",
            str(tmp_path),
            "--poll-interval",
            "0.25",
            "--settle-time",
            "0.75",
        ],
        is_windows=False,
    )
    legacy = bridge._parse_bridge_config(
        [
            "--url",
            "http://localhost/live.usd",
            "--mirror-dir",
            str(tmp_path),
            "--poll",
            "0.25",
            "--settle",
            "0.75",
        ],
        is_windows=False,
    )

    assert canonical.vfs_url == legacy.vfs_url == "http://localhost/live.usd"
    assert canonical.poll_interval == legacy.poll_interval == 0.25
    assert canonical.settle_time == legacy.settle_time == 0.75
    help_text = bridge._build_run_parser(is_windows=False).format_help()
    for legacy_option in ("--url ", "--poll ", "--settle "):
        assert legacy_option not in help_text


def test_windows_exposure_uses_subst(tmp_path, monkeypatch):
    bridge = _load_bridge()
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(bridge, "_is_windows", lambda: True)
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    exposure = bridge._prepare_exposure(
        tmp_path,
        "scene.usd",
        config=bridge.WindowsDriveExposureConfig(
            drive="O:",
            force=True,
            release_on_exit=False,
        ),
    )
    bridge._release_exposure(exposure)

    assert exposure.kind == "windows-drive"
    assert exposure.root_path == "O:\\"
    assert exposure.file_path == "O:\\scene.usd"
    assert calls == [
        ["subst", "O:", "/D"],
        ["subst", "O:", str(tmp_path)],
        ["subst", "O:", "/D"],
    ]


def test_stop_releases_windows_drive_and_process(tmp_path, monkeypatch):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "pid": 12345,
                "drive": "Q:",
                "exposure_kind": "windows-drive",
                "root_path": "Q:\\",
                "file_path": "Q:\\scene.usd",
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(bridge, "_is_windows", lambda: True)
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    assert (
        bridge.main(
            [
                "stop",
                "--status-file",
                str(status_file),
                "--stop-process",
            ]
        )
        == 0
    )

    assert ["taskkill", "/PID", "12345", "/T", "/F"] in calls
    assert ["subst", "Q:", "/D"] in calls
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["state"] == "stopped"
    assert payload["pid"] == 0
    assert payload["file_path"] == "Q:\\scene.usd"


def test_stop_directory_exposure_does_not_release_drive(tmp_path, monkeypatch):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "pid": 0,
                "drive": "",
                "exposure_kind": "local-directory",
                "root_path": str(tmp_path),
                "file_path": str(tmp_path / "scene.usd"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge, "_is_windows", lambda: False)
    monkeypatch.setattr(
        bridge,
        "_unsubst",
        lambda _drive: pytest.fail("directory exposure must not call subst"),
    )

    assert bridge.main(["stop", "--status-file", str(status_file)]) == 0
    assert json.loads(status_file.read_text(encoding="utf-8"))["state"] == "stopped"


def test_stop_uses_explicit_pid_when_status_is_missing(monkeypatch):
    bridge = _load_bridge()
    calls = []
    monkeypatch.setattr(bridge, "_is_windows", lambda: False)
    monkeypatch.setattr(bridge.os, "kill", lambda pid, signal: calls.append((pid, signal)))
    monkeypatch.setattr(bridge, "_process_exists", lambda _pid: False)

    assert bridge.main(["stop", "--pid", "12345", "--stop-process"]) == 0

    assert calls == [(12345, bridge.signal.SIGTERM)]


def test_stop_uses_explicit_fallback_when_status_is_corrupt(tmp_path, monkeypatch):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"
    status_file.write_text("{not-json", encoding="utf-8")
    stopped = []
    released = []
    monkeypatch.setattr(bridge, "_is_windows", lambda: True)
    monkeypatch.setattr(bridge, "_stop_pid", stopped.append)
    monkeypatch.setattr(bridge, "_unsubst", released.append)

    result = bridge.main(
        [
            "stop",
            "--status-file",
            str(status_file),
            "--pid",
            "12345",
            "--stop-process",
            "--drive",
            "Q:",
        ]
    )

    assert result == 0
    assert stopped == [12345]
    assert released == ["Q:"]
    assert json.loads(status_file.read_text(encoding="utf-8"))["state"] == "stopped"


def test_stop_does_not_mark_stopped_when_taskkill_fails(tmp_path, monkeypatch):
    bridge = _load_bridge()
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({"state": "running", "pid": 12345}), encoding="utf-8")

    class Result:
        returncode = 5
        stdout = ""
        stderr = "Access is denied"

    monkeypatch.setattr(bridge, "_is_windows", lambda: True)
    monkeypatch.setattr(bridge, "_process_exists", lambda _pid: True)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *_args, **_kwargs: Result())

    assert bridge.main(["stop", "--status-file", str(status_file), "--stop-process"]) == 1
    assert json.loads(status_file.read_text(encoding="utf-8"))["state"] == "running"


def test_duplicate_bridge_lock_rejects_live_owner(tmp_path, monkeypatch):
    bridge = _load_bridge()
    lock = tmp_path / "bridge.lock"
    monkeypatch.setattr(bridge, "_process_exists", lambda pid: pid == 100)
    lock.write_text(json.dumps({"pid": 100, "owner_id": "first"}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="already owned"):
        bridge._acquire_bridge_lock(
            lock,
            owner_id="second",
            url="http://localhost/scene.usd",
            mirror_dir=tmp_path / "mirror",
        )


def _bridge_config(bridge, tmp_path, *, once=False, settle=0.0):
    mirror = tmp_path / "mirror"
    return bridge.BridgeConfig(
        vfs_url="http://localhost/usd/scene.usd",
        mirror_dir=mirror,
        poll_interval=0.001,
        settle_time=settle,
        once=once,
        background=False,
        open=False,
        status_file=tmp_path / "control" / "status.json",
        log_file=None,
        verbose=False,
        exposure=bridge.DirectoryExposureConfig(),
        owner_id="test-owner",
    )


def test_restart_preserves_untrusted_local_work_outside_mirror(tmp_path, monkeypatch):
    bridge = _load_bridge()
    config = _bridge_config(bridge, tmp_path, once=True)
    config.mirror_dir.mkdir()
    local_file = config.mirror_dir / "scene.usd"
    local_file.write_bytes(b"unsynchronized local work")
    monkeypatch.setattr(
        bridge,
        "_fetch",
        lambda _url: ('"2-0"', b"remote snapshot", bridge._hash_bytes(b"remote snapshot")),
    )

    assert bridge._run_bridge(config) == 0

    status = json.loads(config.status_file.read_text(encoding="utf-8"))
    recovery = Path(status["recovery_file"])
    assert status["state"] == "conflict"
    assert local_file.read_bytes() == b"unsynchronized local work"
    assert recovery.read_bytes() == b"unsynchronized local work"
    assert not recovery.is_relative_to(config.mirror_dir)


def test_acknowledged_put_is_not_repeated_when_refresh_fails(tmp_path, monkeypatch):
    bridge = _load_bridge()
    config = _bridge_config(bridge, tmp_path, settle=0.0)
    initial = b"remote snapshot"
    edited = b"local save"
    fetches = 0
    puts = []
    sleeps = 0

    def fetch(_url):
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            return '"1-0"', initial, bridge._hash_bytes(initial)
        raise OSError("refresh unavailable")

    def sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            (config.mirror_dir / "scene.usd").write_bytes(edited)
        elif sleeps >= 4:
            raise KeyboardInterrupt

    def request(method, _url, body=None, headers=None):
        assert method == "HEAD"
        assert body is None
        assert headers is None
        return 200, {"etag": '"1-1"'}, b""

    def upload(_url, data, etag):
        puts.append((data, etag))
        return ""

    monkeypatch.setattr(bridge, "_fetch", fetch)
    monkeypatch.setattr(bridge, "_request", request)
    monkeypatch.setattr(bridge, "_upload_bytes", upload)
    monkeypatch.setattr(bridge.time, "sleep", sleep)

    assert bridge._run_bridge(config) == 0
    assert puts == [(edited, '"1-0"')]
    status = json.loads(config.status_file.read_text(encoding="utf-8"))
    assert status["last_upload_at"]


def test_stop_help_only_shows_drive_override_on_windows():
    bridge = _load_bridge()

    assert "--drive" not in bridge._build_stop_parser(is_windows=False).format_help()
    assert "--drive" in bridge._build_stop_parser(is_windows=True).format_help()


def test_open_uses_macos_open(monkeypatch):
    bridge = _load_bridge()
    calls = []
    monkeypatch.setattr(bridge, "_is_windows", lambda: False)
    monkeypatch.setattr(bridge, "_is_macos", lambda: True)
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda command: calls.append(command))

    bridge._maybe_open("/tmp/usd")

    assert calls == [["/usr/bin/open", "/tmp/usd"]]
