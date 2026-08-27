"""Stage and validate a relocatable OpenUSD runtime for release packages."""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import build_openusd  # noqa: E402

PROFILES = ("core", "full", "external")
LAUNCHER = REPO_ROOT / "packaging" / "server_launcher.py"


def validate_options(
    profile: str, root: Path | None, plugins: list[Path], allow_unpinned: bool
) -> None:
    if profile not in PROFILES:
        raise ValueError(f"Unknown OpenUSD profile: {profile}")
    if profile == "external":
        if root is None or not root.is_dir():
            raise ValueError("--usd-profile external requires an existing --usd-root installation")
        for plugin in plugins:
            if not plugin.is_dir():
                raise ValueError(f"USD plugin directory does not exist: {plugin}")
    elif root is not None or plugins or allow_unpinned:
        raise ValueError(
            "--usd-root, --usd-plugin-path and --allow-unpinned-usd require --usd-profile external"
        )


def _run(command: list[str], *, capture: bool = False) -> str:
    print("+ " + subprocess.list2cmdline(command), flush=True)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "OPENUSDCONNECT_USD_ROOT", "PXR_PLUGINPATH_NAME"):
        env.pop(key, None)
    result = subprocess.run(
        command,
        check=False,
        env=env,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise RuntimeError(f"OpenUSD command failed ({result.returncode}): {result.stdout or ''}")
    return result.stdout or ""


def managed_install(python: Path) -> Path:
    pin = build_openusd.load_pin()
    version = _run(
        [
            str(python),
            "-I",
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        capture=True,
    ).strip()
    root = (
        REPO_ROOT
        / "build"
        / "openusd-runtime"
        / pin.version
        / build_openusd._platform_key()
        / version
    )
    install = root / "install"
    manifest = install / "openusdconnect-build.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            data.get("pin", {}).get("commit") == pin.commit
            and data.get("features", {}).get("materialx")
            and data.get("host", {}).get("python", "").startswith(version + ".")
        ):
            return install
    _run(
        [
            str(python),
            str(REPO_ROOT / "scripts" / "build_openusd.py"),
            "--root",
            str(root),
            "--profile",
            "runtime",
            "--materialx",
            "--no-tools",
            "--no-register-runtime",
            *(["--generator", "Visual Studio 17 2022"] if os.name == "nt" else []),
        ]
    )
    return install


def _copy_runtime(source: Path, destination: Path) -> None:
    # Preserve the install layout, including plugin resources and shared-library
    # symlinks, but omit build/developer artifacts from customer runtimes.
    for link in source.rglob("*"):
        if link.is_symlink() and not link.resolve().is_relative_to(source.resolve()):
            raise RuntimeError(f"Runtime contains a symlink outside its installation: {link}")

    def ignore(directory: str, names: list[str]) -> set[str]:
        omitted = set(
            shutil.ignore_patterns(
                "__pycache__",
                "*.pyc",
                "*.pdb",
                "*.lib",
                "*.a",
                "*.exp",
                "*.la",
                "openusdconnect-build.json",
            )(directory, names)
        )
        if Path(directory) == source:
            omitted.update({"include", "cmake"} & set(names))
        if Path(directory) in (source / "lib", source / "lib64"):
            omitted.update({"cmake", "pkgconfig"} & set(names))
        return omitted

    shutil.copytree(source, destination, ignore=ignore)


def _plugin_data(info: Path) -> dict:
    # USD plugInfo.json permits full-line comments; remove only those lines.
    content = "\n".join(
        line
        for line in info.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    return json.loads(content)


def _validate_plugin_paths(root: Path) -> list[Path]:
    infos = set(root.rglob("plugInfo.json"))
    # Entry points register descendants through Includes. Resource directories can
    # contain code-generation templates also named plugInfo.json; those are data.
    pending = [
        info
        for info in sorted(infos)
        if not any(
            parent / "plugInfo.json" in infos
            for parent in info.parent.parents
            if parent.is_relative_to(root)
        )
    ]
    visited: set[Path] = set()
    while pending:
        info = pending.pop()
        if info in visited:
            continue
        visited.add(info)
        data = _plugin_data(info)
        for plugin in data.get("Plugins", []):
            value = plugin.get("Root", ".")
            plugin_root = (info.parent / value).resolve()
            if PureWindowsPath(value).anchor or not plugin_root.is_relative_to(
                root.resolve()
            ):
                raise RuntimeError(f"Non-relocatable Root in {info}: {value}")
            for key in ("LibraryPath", "ResourcePath"):
                value = plugin.get(key, "")
                if not value:
                    continue
                target = plugin_root / value
                if PureWindowsPath(value).anchor or not target.resolve().is_relative_to(
                    root.resolve()
                ):
                    raise RuntimeError(f"Non-relocatable {key} in {info}: {value}")
                if not target.exists():
                    raise RuntimeError(f"Missing plugin {key} in {info}: {value}")
        for value in data.get("Includes", []):
            if PureWindowsPath(value).anchor or not (
                info.parent / value
            ).resolve().is_relative_to(root.resolve()):
                raise RuntimeError(f"Non-relocatable plugin include in {info}: {value}")
            for match in glob.glob(str(info.parent / value), recursive=True):
                included = Path(match)
                if included.is_dir():
                    included /= "plugInfo.json"
                if included.is_file():
                    pending.append(included.resolve())
    return sorted(visited)


def _copy_managed_licenses(source: Path, destination: Path) -> None:
    manifest = json.loads((source / "openusdconnect-build.json").read_text(encoding="utf-8"))
    checkout = Path(manifest["paths"]["checkout"])
    dependencies = Path(manifest["paths"]["dependency_sources"])
    for component in (checkout, *sorted(dependencies.iterdir())):
        if not component.is_dir():
            continue
        for path in component.iterdir():
            if path.is_file() and path.name.upper().startswith(("LICENSE", "NOTICE", "COPYING")):
                name = "OpenUSD" if component == checkout else component.name
                target = destination / name / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)


def _materialx_version(root: Path) -> str | None:
    for path in root.rglob("MaterialXConfigVersion.cmake"):
        match = re.search(r'set\(PACKAGE_VERSION\s+"([\d.]+)"', path.read_text(encoding="utf-8"))
        if match:
            return match[1]
    return None


def prepare_runtime(
    output: Path,
    python: Path,
    *,
    profile: str = "full",
    usd_root: Path | None = None,
    plugin_paths: list[Path] | None = None,
    allow_unpinned: bool = False,
) -> dict:
    plugins = plugin_paths or []
    validate_options(profile, usd_root, plugins, allow_unpinned)
    pin = build_openusd.load_pin()
    source = managed_install(python) if profile == "full" else usd_root
    for path in [source, *plugins]:
        if path is not None and (
            output.resolve().is_relative_to(path.resolve())
            or path.resolve().is_relative_to(output.resolve())
        ):
            raise ValueError("Runtime output and source installations must be separate directories")
    output.mkdir(parents=True, exist_ok=True)
    config = {
        "schema": 1,
        "profile": profile,
        "python_path": None,
        "library_dirs": [],
        "plugin_paths": [],
        "required_plugins": [],
    }
    materialx_version = None
    if source is not None:
        source = source.resolve()
        python_path = build_openusd._python_package(source)
        if python_path is None:
            raise RuntimeError(f"No OpenUSD Python bindings under {source}")
        materialx_version = _materialx_version(source)
        _copy_runtime(source, output / "openusd")
        if profile == "full":
            _copy_managed_licenses(source, output / "openusd" / "licenses")
        config["python_path"] = (Path("openusd") / python_path.relative_to(source)).as_posix()
        roots = [output / "openusd"]
        for index, plugin in enumerate(plugins):
            destination = output / "plugins" / str(index)
            _copy_runtime(plugin.resolve(), destination)
            roots.append(destination)
        for root in roots:
            config["library_dirs"].extend(
                p.relative_to(output).as_posix()
                for name in ("bin", "lib", "lib64")
                if (p := root / name).is_dir()
            )
            infos = _validate_plugin_paths(root)
            config["plugin_paths"].extend(p.relative_to(output).as_posix() for p in infos)
            for info in infos:
                for plugin in _plugin_data(info).get("Plugins", []):
                    if root != output / "openusd":
                        config["required_plugins"].append(plugin["Name"])
                    if plugin.get("LibraryPath"):
                        library = (
                            info.parent / plugin.get("Root", ".") / plugin["LibraryPath"]
                        ).resolve()
                        config["library_dirs"].append(library.parent.relative_to(output).as_posix())
        config["library_dirs"] = list(dict.fromkeys(config["library_dirs"]))
    shutil.copy2(LAUNCHER, output / "_launch.py")
    manifest = output / "usd-runtime.json"
    manifest.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    text = _run([str(python), "-I", str(output / "_launch.py"), "--runtime-info"], capture=True)
    info = json.loads(text.strip().splitlines()[-1])
    if source is not None:
        expected = (output / "openusd").resolve()
        if not Path(info.pop("pxr_path")).is_relative_to(expected):
            raise RuntimeError("Runtime probe loaded OpenUSD outside the staged package")
    else:
        info.pop("pxr_path", None)
    if info["version"] != pin.version and not allow_unpinned:
        raise RuntimeError(
            f"OpenUSD {info['version']} does not match pinned {pin.version}; "
            "use --allow-unpinned-usd for an intentional external build"
        )
    if profile == "full" and not info["materialx"]:
        raise RuntimeError("Full OpenUSD package is missing the MaterialX file-format plugin")
    info.update(
        {
            "pinned": info["version"] == pin.version,
            "materialx_version": materialx_version,
            "source_commit": pin.commit if profile == "full" else None,
        }
    )
    config["capabilities"] = info
    manifest.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usd-profile", choices=PROFILES, default="full")
    parser.add_argument("--usd-root", type=Path)
    parser.add_argument("--usd-plugin-path", type=Path, action="append", default=[])
    parser.add_argument("--allow-unpinned-usd", action="store_true")
    parser.add_argument("--python-executable", type=Path, default=Path(sys.executable))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        info = prepare_runtime(
            args.output_dir.resolve(),
            args.python_executable.resolve(),
            profile=args.usd_profile,
            usd_root=args.usd_root,
            plugin_paths=args.usd_plugin_path,
            allow_unpinned=args.allow_unpinned_usd,
        )
        print(json.dumps(info, sort_keys=True))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
