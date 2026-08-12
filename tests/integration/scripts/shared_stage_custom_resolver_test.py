"""Run a server and two processes through OpenUSD's resolver example."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from pxr import Ar, Plug, Sdf, Usd

from openusdconnect.protocol_constants import LayerMode
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer

_SUBLAYER_URI = "asset:Scene/{$VERSION}/content.usda"


def _create_layer(path: Path) -> Sdf.Layer:
    layer = Sdf.Layer.CreateNew(str(path))
    if layer is None:
        raise RuntimeError(f"could not create {path}")
    return layer


def _create_stage_files(
    directory: Path,
    asset_root: Path,
    version: str,
) -> tuple[Path, Path]:
    directory.mkdir()
    content_dir = asset_root / "Scene" / version
    content_dir.mkdir(parents=True)
    content = _create_layer(content_dir / "content.usda")
    prim = Sdf.CreatePrimInLayer(content, "/World")
    Sdf.AttributeSpec(prim, "value", Sdf.ValueTypeNames.Int).default = 1
    content.Save()
    mapping = directory / "versions.json"
    mapping.write_text(json.dumps({"Scene": version}), encoding="utf-8")
    root = _create_layer(directory / "scene.usda")
    root.subLayerPaths.append(_SUBLAYER_URI)
    root.Save()
    return Path(root.realPath), mapping


def _open_stage(root: Path, mapping: Path) -> Usd.Stage:
    context = Ar.GetResolver().CreateContextFromString("asset", str(mapping))
    if context.IsEmpty():
        raise RuntimeError("usdResolverExample did not create a resolver context")
    stage = Usd.Stage.Open(str(root), context)
    if stage is None:
        raise RuntimeError(f"could not open {root}")
    return stage


def _client_result(stdout: str) -> dict:
    line = next(item for item in stdout.splitlines() if item.startswith("RESULT:"))
    return json.loads(line.removeprefix("RESULT:"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--plugin-resources", type=Path, required=True)
    args = parser.parse_args()

    asset_root = args.workspace / "resolver-assets"
    os.environ["USD_RESOLVER_EXAMPLE_ASSET_DIR"] = str(asset_root)
    Plug.Registry().RegisterPlugins(str(args.plugin_resources))
    if Plug.Registry().GetPluginWithName("usdResolverExample") is None:
        raise RuntimeError("usdResolverExample plugin was not registered")

    server_root, server_mapping = _create_stage_files(
        args.workspace / "server", asset_root, "server"
    )
    first_root, first_mapping = _create_stage_files(
        args.workspace / "first", asset_root, "first"
    )
    second_root, second_mapping = _create_stage_files(
        args.workspace / "second", asset_root, "second"
    )
    server_stage = _open_stage(server_root, server_mapping)
    sync_server = UsdSyncServer(
        stage=server_stage,
        log_path=str(args.workspace / "resolver-events.db"),
        layer_mode=LayerMode.SHARED_STAGE,
    )
    tcp_server = ThreadedTCPServer(
        ("127.0.0.1", 0), ConnectionHandler, sync_server, max_workers=8
    )
    thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    thread.start()
    client_script = Path(__file__).with_name("shared_stage_custom_resolver_client.py")
    env = os.environ.copy()

    def _start(role: str, root: Path, mapping: Path) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable,
                str(client_script),
                "--role",
                role,
                "--stage",
                str(root),
                "--mapping",
                str(mapping),
                "--plugin-resources",
                str(args.plugin_resources),
                "--port",
                str(tcp_server.server_address[1]),
            ],
            cwd=Path(__file__).parents[3],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    clients = [
        _start("first", first_root, first_mapping),
        _start("second", second_root, second_mapping),
    ]
    try:
        outputs = [client.communicate(timeout=30) for client in clients]
        for client, (stdout, stderr) in zip(clients, outputs, strict=True):
            if client.returncode:
                raise RuntimeError(
                    f"custom-resolver client failed\nstdout:\n{stdout}\nstderr:\n{stderr}"
                )
        results = [_client_result(stdout) for stdout, _stderr in outputs]
        server_content = server_stage.GetLayerStack(includeSessionLayers=False)[1]
        value = server_stage.GetAttributeAtPath("/World.value").Get()
        print(
            "RESULT:"
            + json.dumps(
                {
                    "clients": results,
                    "server": {
                        "resolved_path": str(server_content.resolvedPath),
                        "value": value,
                    },
                },
                sort_keys=True,
            )
        )
    finally:
        for client in clients:
            if client.poll() is None:
                client.kill()
                client.wait(timeout=5)
        tcp_server.shutdown()
        tcp_server.server_close()
        thread.join(timeout=5)
        sync_server.shutdown()
        sync_server.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
