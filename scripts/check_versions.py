"""Check release, compatibility, protocol, and toolchain version declarations."""

from __future__ import annotations

import ast
import json
import re
import runpy
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _assignment(path: str, name: str):
    tree = ast.parse(_read(path), filename=path)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return ast.literal_eval(node.value)
    raise ValueError(f"{name} not found in {path}")


def _blender_info() -> dict:
    return _assignment("integrations/blender/__init__.py", "bl_info")


def _requirement_pin(dependencies: list[str], package: str) -> str | None:
    prefix = f"{package}=="
    matches = [item[len(prefix) :] for item in dependencies if item.lower().startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def _dependency_pin(pyproject: dict, group: str, package: str) -> str | None:
    dependencies = (
        pyproject["project"]["dependencies"]
        if group == "project"
        else pyproject["dependency-groups"][group]
    )
    return _requirement_pin(dependencies, package)


def _generated_flatbuffers_version() -> str | None:
    header = _read("native/client_core/include/openusdconnect/client/schema/messages_generated.h")
    parts = []
    for field in ("MAJOR", "MINOR", "REVISION"):
        match = re.search(rf"FLATBUFFERS_VERSION_{field}\s*==\s*(\d+)", header)
        if not match:
            return None
        parts.append(match.group(1))
    return ".".join(parts)


def _docker_instructions(dockerfile: str) -> list[str]:
    instructions: list[str] = []
    current = ""
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        line = line.removesuffix("\\").rstrip()
        current = f"{current} {line}".strip()
        if not continued:
            instructions.append(current)
            current = ""
    if current:
        instructions.append(current)
    return instructions


def _docker_pip_pins(dockerfile: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for instruction in _docker_instructions(dockerfile):
        for command in re.findall(r"\bpip\s+install\b(.*?)(?=\s*(?:&&|;|$))", instruction):
            for package, version in re.findall(
                r"(?<![\w.-])([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", command
            ):
                pins[package.lower()] = version
    return pins


def _docker_base_images(dockerfile: str) -> list[str]:
    images = []
    for instruction in _docker_instructions(dockerfile):
        match = re.fullmatch(
            r"FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+\S+)?", instruction, re.I
        )
        if match:
            images.append(match.group(1))
    return images


def _cpp_constant(name: str) -> int | None:
    header = _read("native/client_core/include/openusdconnect/client/protocol_codec.h")
    match = re.search(rf"{name}\s*=\s*(\d+)", header)
    return int(match.group(1)) if match else None


def collect_errors() -> list[str]:
    errors: list[str] = []
    pyproject = tomllib.loads(_read("pyproject.toml"))
    release = str(runpy.run_path(ROOT / "openusdconnect" / "_version.py")["__version__"])
    openusd = json.loads(_read("openusd.lock.json"))

    if not SEMVER.fullmatch(release):
        errors.append(f"OpenUSDConnect release must use X.Y.Z SemVer, got {release!r}")
    if pyproject["project"].get("dynamic") != ["version"]:
        errors.append("pyproject project.version must be dynamic")
    if pyproject.get("tool", {}).get("hatch", {}).get("version", {}).get("path") != (
        "openusdconnect/_version.py"
    ):
        errors.append("Hatch must read the release from openusdconnect/_version.py")
    blender = _blender_info()
    blender_release = ".".join(str(part) for part in blender["version"])
    if blender_release != release:
        errors.append(f"Blender addon {blender_release} must match core release {release}")

    unreal_path = "integrations/unreal/OpenUSDConnect/OpenUSDConnect.uplugin"
    unreal = json.loads(_read(unreal_path))
    if not isinstance(unreal.get("Version"), int) or unreal["Version"] < 1:
        errors.append("Unreal plugin Version must be a positive build integer")
    if not SEMVER.fullmatch(str(unreal.get("VersionName", ""))):
        errors.append("Unreal plugin VersionName must use X.Y.Z SemVer")

    protocol = int(_assignment("openusdconnect/protocol_constants.py", "PROTOCOL_VERSION"))
    schema = int(_assignment("openusdconnect/codec.py", "SCHEMA_VERSION"))
    if _cpp_constant("kProtocolVersion") != protocol:
        errors.append("Native kProtocolVersion does not match Python PROTOCOL_VERSION")
    if _cpp_constant("kSchemaVersion") != schema:
        errors.append("Native kSchemaVersion does not match Python SCHEMA_VERSION")

    flatbuffers = _dependency_pin(pyproject, "project", "flatbuffers")
    setup_flatbuffers = str(
        _assignment("integrations/unreal/OpenUSDConnect/setup_flatbuffers.py", "DEFAULT_VERSION")
    )
    generated_flatbuffers = _generated_flatbuffers_version()
    if not flatbuffers or flatbuffers != setup_flatbuffers or flatbuffers != generated_flatbuffers:
        errors.append("FlatBuffers Python, Unreal setup, and generated-header versions must match")
    vendored_version = ROOT / (
        "integrations/unreal/OpenUSDConnect/Source/OpenUSDConnectPXR/ThirdParty/flatbuffers/VERSION"
    )
    if (
        vendored_version.is_file()
        and vendored_version.read_text(encoding="utf-8").strip() != flatbuffers
    ):
        errors.append("Vendored Unreal FlatBuffers headers do not match the configured version")

    python_minor = _read(".python-version").strip()
    if pyproject["project"]["requires-python"] != f">={python_minor}":
        errors.append(".python-version and project.requires-python must match")
    if pyproject["tool"]["ruff"]["target-version"] != f"py{python_minor.replace('.', '')}":
        errors.append(".python-version and Ruff target-version must match")
    docker = _read("Dockerfile")
    python_images = [image for image in _docker_base_images(docker) if image.startswith("python:")]
    if not python_images or any(
        not re.fullmatch(rf"python:{re.escape(python_minor)}-slim(?:-[a-z]+)?", image)
        for image in python_images
    ):
        errors.append("Docker Python image must match .python-version")

    profiles = pyproject["project"]["optional-dependencies"]
    for profile in ("runtime", "vfs", "complete", "mcp"):
        if f"--requirement /requirements/{profile}.txt" not in docker:
            errors.append(f"Docker must install its {profile} package profile")
    complete = profiles.get("complete", [])
    for group, package in (
        ("bundled-usd", "usd-core"),
        ("vfs", "wsgidav"),
        ("vfs", "cheroot"),
        ("dashboard", "nicegui"),
    ):
        expected = _dependency_pin(pyproject, group, package)
        packaged = _requirement_pin(complete, package)
        if not expected or packaged != expected:
            errors.append(
                f"Complete package {package} pin must match pyproject dependency group {group}"
            )
    for profile, requirements in (
        ("runtime", (("bundled-usd", "usd-core"),)),
        (
            "vfs",
            (
                ("bundled-usd", "usd-core"),
                ("vfs", "wsgidav"),
                ("vfs", "cheroot"),
            ),
        ),
        ("mcp", (("bundled-usd", "usd-core"),)),
    ):
        packaged_requirements = profiles.get(profile, [])
        for group, package in requirements:
            expected = _dependency_pin(pyproject, group, package)
            packaged = _requirement_pin(packaged_requirements, package)
            if not expected or packaged != expected:
                errors.append(
                    f"{profile.title()} package {package} pin must match "
                    f"pyproject dependency group {group}"
                )
    mcp_requirements = pyproject["dependency-groups"]["mcp"]
    if len(mcp_requirements) != 1 or mcp_requirements[0] not in profiles.get("mcp", []):
        errors.append("MCP package requirement must match pyproject dependency group mcp")

    bundled_openusd = _dependency_pin(pyproject, "bundled-usd", "usd-core")
    if openusd.get("usd_core") != bundled_openusd:
        errors.append("OpenUSD source lock and bundled usd-core version must match")
    source_version = str(openusd.get("version", ""))
    if source_version.removeprefix("0.") != bundled_openusd:
        errors.append("OpenUSD source version and bundled usd-core version must match")

    return errors


def version_summary() -> list[str]:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    release = str(runpy.run_path(ROOT / "openusdconnect" / "_version.py")["__version__"])
    unreal = json.loads(_read("integrations/unreal/OpenUSDConnect/OpenUSDConnect.uplugin"))
    openusd = json.loads(_read("openusd.lock.json"))
    return [
        f"OpenUSDConnect / Blender: {release}",
        f"Unreal plugin: {unreal['VersionName']} (build {unreal['Version']})",
        f"Protocol: {_assignment('openusdconnect/protocol_constants.py', 'PROTOCOL_VERSION')}",
        f"Wire schema: {_assignment('openusdconnect/codec.py', 'SCHEMA_VERSION')}",
        f"FlatBuffers: {_dependency_pin(pyproject, 'project', 'flatbuffers')}",
        f"Python: {pyproject['project']['requires-python']}",
        f"Blender: >={'.'.join(str(part) for part in _blender_info()['blender'])}",
        f"Unreal Engine: {unreal['EngineVersion']}",
        f"OpenUSD: {openusd['version']} ({openusd['tag']})",
        f"Bundled OpenUSD: {_dependency_pin(pyproject, 'bundled-usd', 'usd-core')}",
    ]


def main() -> int:
    errors = collect_errors()
    for line in version_summary():
        print(line)
    if errors:
        print("\nVersion consistency errors:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("\nAll version declarations are consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
