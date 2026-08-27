"""Exercise packaged USD composition and VFS without a DCC or a renderer."""

from __future__ import annotations

import argparse
import json
import tempfile
import urllib.request
from pathlib import Path

from pxr import Sdf, Usd, UsdShade


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--materialx", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--http-url", action="append", default=[])
    args = parser.parse_args()
    if args.http_url:
        for url in args.http_url:
            with urllib.request.urlopen(url, timeout=3) as response:
                assert response.status == 200
                assert response.read(), f"Empty response from {url}"
        print(json.dumps({"http": "PASS", "urls": args.http_url}))
        return
    if args.snapshot:
        stage = Usd.Stage.Open(str(args.snapshot))
        assert stage and stage.GetRootLayer().customLayerData.get("openusdconnect")
        return

    from openusdconnect.server import UsdSyncServer
    from openusdconnect.server.vfs import VirtualStageFile

    with tempfile.TemporaryDirectory(prefix="ouc-packaged-materialx-") as directory:
        root = Path(directory)
        scene = root / "scene.usda"
        if args.materialx:
            (root / "material.mtlx").write_text(
                """<?xml version="1.0"?>
<materialx version="1.38">
  <standard_surface name="Surface" type="surfaceshader">
    <input name="base_color" type="color3" value="0.2, 0.4, 0.7" />
    <input name="specular_roughness" type="float" value="0.3" />
  </standard_surface>
  <surfacematerial name="Blue" type="material">
    <input name="surfaceshader" type="surfaceshader" nodename="Surface" />
  </surfacematerial>
</materialx>
""",
                encoding="utf-8",
            )
            assert Sdf.Layer.FindOrOpen(str(root / "material.mtlx"), {"target": "usd"})
            scene.write_text(
                """#usda 1.0
def Scope "MaterialX" (references = @material.mtlx@</MaterialX>) {}
def Sphere "Shape" {
    rel material:binding = </MaterialX/Materials/Blue>
}
""",
                encoding="utf-8",
            )
        else:
            stage = Usd.Stage.CreateNew(str(scene))
            material = UsdShade.Material.Define(stage, "/Material")
            shader = UsdShade.Shader.Define(stage, "/Material/Surface")
            shader.CreateIdAttr("ND_standard_surface_surfaceshader")
            shader.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).Set((0.2, 0.4, 0.7))
            material.CreateSurfaceOutput("mtlx").ConnectToSource(
                shader.CreateOutput("out", Sdf.ValueTypeNames.Token)
            )
            stage.GetRootLayer().Save()
            stage = None
        server = UsdSyncServer(base_usd_path=str(scene), log_path=str(root / "events.db"))
        try:
            shaders = [prim for prim in server.stage.Traverse() if prim.IsA(UsdShade.Shader)]
            assert shaders, "MaterialX composition produced no shaders"
            shader_path = str(shaders[0].GetPath())
            server.apply_txn(
                [
                    {
                        "k": "set_connectable_input",
                        "prim": shader_path,
                        "info_id": UsdShade.Shader(shaders[0]).GetIdAttr().Get() or "",
                        "inputs": {"specular_roughness": 0.65},
                        "input_types": {"specular_roughness": "float"},
                    }
                ]
            )
            snapshot = VirtualStageFile(
                server, name="scene.usd", advertise_host="127.0.0.1", sync_port=7200
            )
            result = root / "snapshot.usda"
            result.write_bytes(snapshot.read())
            composed = Usd.Stage.Open(str(result))
            assert (
                composed.GetPrimAtPath(shader_path).GetAttribute("inputs:specular_roughness").Get()
                > 0.64
            )
            assert composed.GetRootLayer().customLayerData.get("openusdconnect")
            if args.materialx:
                assert composed.GetPrimAtPath("/MaterialX/Materials/Blue")
        finally:
            server.shutdown()
            server.store.close()
    print(json.dumps({"packaged_usd": "PASS", "external_materialx": args.materialx}))


if __name__ == "__main__":
    main()
