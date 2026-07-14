"""Opt-in Cycles runtime configuration in the usdview launcher."""

from integrations.usdview import launcher


class _Proc:
    pass


def test_cycles_flag_configures_runtime_and_renderer(monkeypatch, tmp_path):
    executable = tmp_path / "usdview"
    executable.write_text("usdview placeholder\n")
    cycles_root = tmp_path / "cycles"
    hydra_dir = cycles_root / "hydra"
    runtime_dir = cycles_root / "lib"
    hydra_dir.mkdir(parents=True)
    runtime_dir.mkdir()
    (hydra_dir / "hdCycles.dylib").touch()
    captured = {}

    def fake_popen(cmd, env):
        captured.update(cmd=cmd, env=env)
        return _Proc()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.platform, "machine", lambda: "arm64")
    monkeypatch.setattr("integrations.renderman.dll_dirs", lambda: [])
    monkeypatch.setenv("PXR_PLUGINPATH_NAME", str(hydra_dir))
    monkeypatch.delenv("CYCLES_DEVICE", raising=False)
    monkeypatch.delenv("DYLD_LIBRARY_PATH", raising=False)

    result = launcher.launch_usdview("scene.usda", usdview_exe=executable, cycles=True)

    assert isinstance(result, _Proc)
    assert captured["env"]["CYCLES_DEVICE"] == "METAL"
    assert captured["env"]["DYLD_LIBRARY_PATH"] == str(runtime_dir)
    assert captured["cmd"][-3:] == [
        "scene.usda",
        "--renderer",
        launcher.CYCLES_RENDERER_ID,
    ]


def test_explicit_renderer_wins_over_cycles_default(monkeypatch, tmp_path):
    executable = tmp_path / "usdview"
    executable.write_text("usdview placeholder\n")
    captured = {}

    def fake_popen(cmd, env):
        captured.update(cmd=cmd, env=env)
        return _Proc()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr("integrations.renderman.dll_dirs", lambda: [])

    launcher.launch_usdview(
        "scene.usda",
        usdview_exe=executable,
        cycles=True,
        extra_args=("--renderer", "HdStormRendererPlugin"),
    )

    assert captured["cmd"].count("--renderer") == 1
    assert "HdStormRendererPlugin" in captured["cmd"]


def test_explicit_cycles_device_is_preserved(monkeypatch, tmp_path):
    executable = tmp_path / "usdview"
    executable.write_text("usdview placeholder\n")
    captured = {}

    def fake_popen(cmd, env):
        captured.update(cmd=cmd, env=env)
        return _Proc()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.platform, "machine", lambda: "arm64")
    monkeypatch.setattr("integrations.renderman.dll_dirs", lambda: [])
    monkeypatch.setenv("CYCLES_DEVICE", "CPU")

    launcher.launch_usdview("scene.usda", usdview_exe=executable, cycles=True)

    assert captured["env"]["CYCLES_DEVICE"] == "CPU"
