"""Tests for the RenderMan DLL-dir bootstrap helpers (integrations.renderman)."""

import os
import sys

from integrations import renderman


def _fake_winreg(value):
    """A stand-in winreg module. QueryValueEx returns *value*, or raises
    FileNotFoundError when *value* is None (the absent-value case)."""

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeWinreg:
        HKEY_LOCAL_MACHINE = object()

        @staticmethod
        def OpenKey(root, path):
            return _Key()

        @staticmethod
        def QueryValueEx(key, name):
            if value is None:
                raise FileNotFoundError(name)
            return (value, 1)  # (value, REG_SZ)

    return _FakeWinreg()


def test_rmantree_prefers_process_env(monkeypatch):
    monkeypatch.setenv("RMANTREE", r"C:\Custom\RMan  ")  # trailing whitespace stripped
    # Registry would resolve to something else; process env must win regardless.
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(r"C:\Registry\RMan"))
    assert renderman.rmantree() == r"C:\Custom\RMan"


def test_rmantree_non_windows_returns_env_only(monkeypatch):
    monkeypatch.delenv("RMANTREE", raising=False)
    monkeypatch.setattr(os, "name", "posix")
    assert renderman.rmantree() == ""


def test_rmantree_registry_fallback_when_env_absent(monkeypatch):
    monkeypatch.delenv("RMANTREE", raising=False)
    monkeypatch.setattr(os, "name", "nt")
    # Real registry value keeps its trailing backslash; _rmantree strips only
    # whitespace (dll_dirs trims the separator). Mirror that exactly.
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg("C:\\Pixar\\RMan-27.2\\  "))
    assert renderman.rmantree() == "C:\\Pixar\\RMan-27.2\\"


def test_rmantree_missing_registry_value_returns_empty(monkeypatch):
    monkeypatch.delenv("RMANTREE", raising=False)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(None))
    assert renderman.rmantree() == ""


def test_dll_dirs_builds_bin_and_lib_from_resolved_value(monkeypatch):
    monkeypatch.setenv("RMANTREE", r"C:\Pixar\RMan\\")  # trailing seps trimmed by dll_dirs
    assert renderman.dll_dirs() == [
        os.path.join(r"C:\Pixar\RMan", "bin"),
        os.path.join(r"C:\Pixar\RMan", "lib"),
    ]


def test_dll_dirs_empty_when_rmantree_unresolved(monkeypatch):
    monkeypatch.delenv("RMANTREE", raising=False)
    monkeypatch.setattr(os, "name", "posix")
    assert renderman.dll_dirs() == []
