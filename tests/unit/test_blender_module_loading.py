import importlib.util
from pathlib import Path
from types import ModuleType

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "integrations" / "blender" / "_module_loading.py"
)
SPEC = importlib.util.spec_from_file_location("blender_module_loading", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module_loading = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module_loading)
refresh_core_modules = module_loading.refresh_core_modules


def _module(name: str, path: str) -> ModuleType:
    module = ModuleType(name)
    module.__file__ = path
    return module


def test_addon_reload_retains_native_client_and_refreshes_python_modules(tmp_path):
    bundled = tmp_path / "usd_connect" / "openusdconnect"
    modules = {
        "openusdconnect": _module("openusdconnect", str(bundled / "__init__.py")),
        "openusdconnect.codec": _module("openusdconnect.codec", str(bundled / "codec.py")),
        "openusdconnect._native_client": _module(
            "openusdconnect._native_client",
            str(bundled / "_native_client.pyd"),
        ),
    }
    native = modules["openusdconnect._native_client"]

    refresh_core_modules(modules, str(bundled), addon_reload=True)

    assert modules == {"openusdconnect._native_client": native}


def test_packaged_addon_retains_loaded_native_client_when_replacing_checkout(tmp_path):
    bundled = tmp_path / "usd_connect" / "openusdconnect"
    checkout = tmp_path / "checkout" / "openusdconnect"
    modules = {
        "openusdconnect": _module("openusdconnect", str(checkout / "__init__.py")),
        "openusdconnect.codec": _module("openusdconnect.codec", str(checkout / "codec.py")),
        "openusdconnect._native_client": _module(
            "openusdconnect._native_client",
            str(checkout / "_native_client.pyd"),
        ),
        "openusdconnectivity": _module("openusdconnectivity", str(tmp_path / "other.py")),
    }
    native = modules["openusdconnect._native_client"]
    unrelated = modules["openusdconnectivity"]

    refresh_core_modules(modules, str(bundled), addon_reload=False)

    assert modules == {
        "openusdconnect._native_client": native,
        "openusdconnectivity": unrelated,
    }


def test_first_addon_import_keeps_python_modules_already_loaded_from_bundle(tmp_path):
    bundled = tmp_path / "usd_connect" / "openusdconnect"
    package = _module("openusdconnect", str(bundled / "__init__.py"))
    codec = _module("openusdconnect.codec", str(bundled / "codec.py"))
    modules = {
        "openusdconnect": package,
        "openusdconnect.codec": codec,
    }

    refresh_core_modules(modules, str(bundled), addon_reload=False)

    assert modules == {
        "openusdconnect": package,
        "openusdconnect.codec": codec,
    }
