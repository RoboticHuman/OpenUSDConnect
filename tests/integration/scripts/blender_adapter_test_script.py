"""Blender headless test script for BlenderAdapter.

Run via: blender --background --python tests/blender_adapter_test_script.py
Exit code 0 = all tests pass, 1 = failure.

This script runs inside Blender's Python interpreter with real bpy/bmesh.
"""

# Add project root and tests dir to path
import os
import sys
import traceback

import bpy

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(_scripts_dir)))
tests_dir = os.path.dirname(os.path.dirname(_scripts_dir))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
_venv_sp = os.path.join(project_root, ".venv", "Lib", "site-packages")
if os.path.isdir(_venv_sp) and _venv_sp not in sys.path:
    sys.path.append(_venv_sp)
if tests_dir not in sys.path:
    sys.path.insert(0, tests_dir)
for _k in [k for k in sys.modules if k.startswith("openusdconnect")]:
    del sys.modules[_k]

from integrations.blender.blender_adapter import BlenderAdapter


def _make_stage_with_mpu(mpu=1.0):
    """Return an in-memory Usd.Stage with the given metersPerUnit.

    Camera attr conversion reads metersPerUnit off the adapter's mirror_stage;
    tests bind one explicitly so the conversion math is deterministic.
    """
    from pxr import Sdf, Usd, UsdGeom

    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, mpu)
    stage.DefinePrim(Sdf.Path("/World"), "Xform")
    return stage


def _clear_scene():
    """Remove all objects from the scene."""
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def _find_by_prim(adapter, prim_path):
    return adapter._find_object_by_prim(prim_path)


class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def ok(self, name):
        self.passed += 1
        print(f"  PASS: {name}")

    def fail(self, name, msg):
        self.failed += 1
        self.errors.append((name, msg))
        print(f"  FAIL: {name} — {msg}")


def test_ensure_prim_xform(r):
    """ensure_prim with Xform creates an Empty."""
    name = "test_ensure_prim_xform"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Empty", "Xform")
    obj = _find_by_prim(adapter, "/World/Empty")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != "EMPTY":
        r.fail(name, f"expected EMPTY, got {obj.type}")
        return
    if obj.get("usd_type_name") != "Xform":
        r.fail(name, f"usd_type_name={obj.get('usd_type_name')}")
        return
    r.ok(name)


def test_ensure_prim_cube(r):
    """ensure_prim with Cube creates a mesh object with vertices."""
    name = "test_ensure_prim_cube"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MyCube", "Cube")
    obj = _find_by_prim(adapter, "/World/MyCube")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != "MESH":
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    if len(obj.data.vertices) == 0:
        r.fail(name, "mesh has no vertices")
        return
    if obj.get("usd_type_name") != "Cube":
        r.fail(name, f"usd_type_name={obj.get('usd_type_name')}")
        return
    r.ok(name)


def test_ensure_prim_sphere(r):
    """ensure_prim with Sphere creates a mesh with vertices."""
    name = "test_ensure_prim_sphere"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MySphere", "Sphere")
    obj = _find_by_prim(adapter, "/World/MySphere")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != "MESH":
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    if len(obj.data.vertices) < 10:
        r.fail(name, f"expected many vertices for sphere, got {len(obj.data.vertices)}")
        return
    r.ok(name)


def test_ensure_prim_cylinder(r):
    """ensure_prim with Cylinder creates a mesh."""
    name = "test_ensure_prim_cylinder"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MyCylinder", "Cylinder")
    obj = _find_by_prim(adapter, "/World/MyCylinder")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != "MESH":
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    if len(obj.data.vertices) == 0:
        r.fail(name, "mesh has no vertices")
        return
    r.ok(name)


def test_ensure_prim_cone(r):
    """ensure_prim with Cone creates a mesh."""
    name = "test_ensure_prim_cone"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MyCone", "Cone")
    obj = _find_by_prim(adapter, "/World/MyCone")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != "MESH":
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    if len(obj.data.vertices) == 0:
        r.fail(name, "mesh has no vertices")
        return
    r.ok(name)


def test_ensure_prim_mesh(r):
    """ensure_prim with Mesh creates an empty mesh object."""
    name = "test_ensure_prim_mesh"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MyMesh", "Mesh")
    obj = _find_by_prim(adapter, "/World/MyMesh")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != "MESH":
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    r.ok(name)


def test_ensure_prim_supported_light_types_create_blender_lights(r):
    """The 4 light types we translate (SphereLight→POINT, DistantLight→SUN,
    RectLight→AREA RECTANGLE, DiskLight→AREA DISK) must create a Blender
    LIGHT object with the right data type."""
    name = "test_ensure_prim_supported_light_types_create_blender_lights"
    cases = [
        ("SphereLight", "POINT", None),
        ("DistantLight", "SUN", None),
        ("RectLight", "AREA", "RECTANGLE"),
        ("DiskLight", "AREA", "DISK"),
    ]
    for usd_type, expected_blender_type, expected_shape in cases:
        _clear_scene()
        adapter = BlenderAdapter()
        prim_path = f"/World/Test{usd_type}"
        if not adapter.ensure_prim(prim_path, usd_type):
            r.fail(name, f"{usd_type} ensure_prim returned False")
            return
        obj = _find_by_prim(adapter, prim_path)
        if obj is None:
            r.fail(name, f"{usd_type} did not create a Blender object")
            return
        if obj.type != "LIGHT":
            r.fail(name, f"{usd_type} → expected LIGHT object, got {obj.type}")
            return
        if obj.data.type != expected_blender_type:
            r.fail(
                name,
                f"{usd_type} → expected light.data.type={expected_blender_type}, "
                f"got {obj.data.type}",
            )
            return
        if expected_shape is not None and obj.data.shape != expected_shape:
            r.fail(
                name,
                f"{usd_type} → expected light.data.shape={expected_shape}, "
                f"got {obj.data.shape}",
            )
            return
    r.ok(name)


def test_ensure_prim_skipped_light_types_create_no_object(r):
    """Lights without any Blender equivalent (CylinderLight, GeometryLight,
    PortalLight, PluginLight) must NOT create a stray Empty object — they
    should be skipped via _NON_SCENE_TYPES. DomeLight is handled separately
    by test_ensure_prim_domelight_sets_up_world_network."""
    name = "test_ensure_prim_skipped_light_types_create_no_object"
    for light_type in ("CylinderLight", "GeometryLight", "PortalLight", "PluginLight"):
        _clear_scene()
        adapter = BlenderAdapter()
        prim_path = f"/World/Test{light_type}"
        if not adapter.ensure_prim(prim_path, light_type):
            r.fail(name, f"{light_type} ensure_prim returned False")
            return
        obj = _find_by_prim(adapter, prim_path)
        if obj is not None:
            r.fail(name, f"{light_type} created a stray Blender object: {obj.name} ({obj.type})")
            return
        stray = [o for o in bpy.data.objects if o.get("usd_prim_path") == prim_path]
        if stray:
            r.fail(name, f"{light_type} left {len(stray)} stray object(s) in bpy.data")
            return
    r.ok(name)


def test_ensure_prim_domelight_sets_up_world_network(r):
    """DomeLight is not a scene object — it sets up the World shader network
    (TexCoord → Mapping → EnvTex → VectorMath → Background → WorldOutput)
    matching what Blender's own USD importer produces."""
    name = "test_ensure_prim_domelight_sets_up_world_network"
    _clear_scene()
    adapter = BlenderAdapter()
    if not adapter.ensure_prim("/World/Dome", "DomeLight"):
        r.fail(name, "DomeLight ensure_prim returned False")
        return
    # No scene object.
    obj = _find_by_prim(adapter, "/World/Dome")
    if obj is not None:
        r.fail(name, f"DomeLight created a stray object: {obj.name}")
        return
    # World network exists.
    world = bpy.context.scene.world
    if world is None or not world.use_nodes:
        r.fail(name, "World missing or use_nodes False after ensure_prim(DomeLight)")
        return
    tree = world.node_tree
    expected = {
        "USD Dome World Output":         "ShaderNodeOutputWorld",
        "USD Dome Background":           "ShaderNodeBackground",
        "USD Dome Color Multiply":       "ShaderNodeVectorMath",
        "USD Dome Environment Texture":  "ShaderNodeTexEnvironment",
        "USD Dome Mapping":              "ShaderNodeMapping",
        "USD Dome Texture Coordinate":   "ShaderNodeTexCoord",
    }
    for node_name, bl_idname in expected.items():
        node = tree.nodes.get(node_name)
        if node is None:
            r.fail(name, f"missing node {node_name!r}")
            return
        if node.bl_idname != bl_idname:
            r.fail(name, f"{node_name}: expected {bl_idname}, got {node.bl_idname}")
            return
    # Verify the wiring chain ends at WorldOutput.Surface from Background.
    out = tree.nodes["USD Dome World Output"]
    surface_input = out.inputs["Surface"]
    if not surface_input.is_linked:
        r.fail(name, "WorldOutput.Surface is not linked")
        return
    from_node = surface_input.links[0].from_node.name
    if from_node != "USD Dome Background":
        r.fail(name, f"WorldOutput.Surface from={from_node}, expected USD Dome Background")
        return
    # Output node is tagged with the dome's prim path.
    if out.get("usd_dome_path") != "/World/Dome":
        r.fail(name, f"WorldOutput usd_dome_path={out.get('usd_dome_path')}, expected /World/Dome")
        return
    r.ok(name)


def test_set_connectable_input_domelight_writes_world_shader(r):
    """USD DomeLight inputs (intensity, color, texture:file) write to the
    correct nodes on the World shader."""
    import os
    import tempfile

    name = "test_set_connectable_input_domelight_writes_world_shader"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Dome", "DomeLight")

    # intensity → Background.Strength; color → VectorMath factor.
    adapter.set_connectable_input(
        "/World/Dome",
        "",
        {"intensity": 2.5, "color": [1.0, 0.8, 0.5]},
        {"intensity": "float", "color": "color3f"},
    )
    world = bpy.context.scene.world
    tree = world.node_tree
    bg = tree.nodes["USD Dome Background"]
    vm = tree.nodes["USD Dome Color Multiply"]

    if abs(bg.inputs["Strength"].default_value - 2.5) > 1e-4:
        r.fail(name, f"Background.Strength={bg.inputs['Strength'].default_value}, expected 2.5")
        return
    factor = tuple(vm.inputs[1].default_value)[:3]
    if tuple(round(c, 3) for c in factor) != (1.0, 0.8, 0.5):
        r.fail(name, f"VectorMath factor={factor}, expected (1.0,0.8,0.5)")
        return

    # texture:file with a real loadable image. Use a tiny 1x1 EXR by writing
    # a Blender-internal image to disk, then loading via the dome path.
    tmpdir = tempfile.mkdtemp()
    exr_path = os.path.join(tmpdir, "tiny.hdr")
    src_img = bpy.data.images.new("__tmp_hdr", width=4, height=2, float_buffer=True)
    src_img.pixels = [0.5] * (4 * 2 * 4)
    src_img.filepath_raw = exr_path
    src_img.file_format = "HDR"
    src_img.save()

    adapter.set_connectable_input(
        "/World/Dome",
        "",
        {"texture:file": exr_path},
        {"texture:file": "asset"},
    )
    env = tree.nodes["USD Dome Environment Texture"]
    if env.image is None:
        r.fail(name, f"EnvironmentTexture.image is None after texture:file={exr_path!r}")
        return
    if env.image.colorspace_settings.name != "Non-Color":
        r.fail(
            name,
            f"EnvironmentTexture colorspace={env.image.colorspace_settings.name!r}, "
            f"expected 'Non-Color'",
        )
        return
    r.ok(name)


def test_set_xform_trs_domelight_writes_mapping_rotation(r):
    """DomeLight Xform rotation should land on the Mapping node, not raise
    a 'no object' warning (DomeLight has no scene object)."""
    import math

    name = "test_set_xform_trs_domelight_writes_mapping_rotation"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Dome", "DomeLight")

    # 90° rotation around Y axis: quat = (cos(45°), 0, sin(45°), 0)
    s = math.sin(math.radians(45))
    c = math.cos(math.radians(45))
    adapter.set_xform_trs("/World/Dome", r=[c, 0.0, s, 0.0])

    world = bpy.context.scene.world
    mapping = world.node_tree.nodes["USD Dome Mapping"]
    rot = mapping.inputs["Rotation"].default_value
    # Y of 90° = π/2 rad, X and Z should be 0.
    if abs(rot[1] - math.pi / 2) > 1e-3:
        r.fail(name, f"Mapping rotation Y={rot[1]}, expected π/2 (~1.5708)")
        return
    if abs(rot[0]) > 1e-3 or abs(rot[2]) > 1e-3:
        r.fail(name, f"Mapping rotation X={rot[0]}, Z={rot[2]}, expected ~0")
        return
    r.ok(name)


def test_multi_domelight_last_wins(r):
    """When two DomeLights are sequenced through ensure_prim, the most
    recently-ensured one becomes the active dome and drives subsequent
    inputs. The earlier dome's inputs are silently ignored."""
    name = "test_multi_domelight_last_wins"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/DomeA", "DomeLight")
    # First dome owns the World, set its intensity.
    adapter.set_connectable_input(
        "/World/DomeA", "",
        {"intensity": 1.0}, {"intensity": "float"},
    )
    bg = bpy.context.scene.world.node_tree.nodes["USD Dome Background"]
    if abs(bg.inputs["Strength"].default_value - 1.0) > 1e-4:
        r.fail(name, "DomeA failed to set initial intensity")
        return

    # Second dome takes over.
    adapter.ensure_prim("/World/DomeB", "DomeLight")
    adapter.set_connectable_input(
        "/World/DomeB", "",
        {"intensity": 7.0}, {"intensity": "float"},
    )
    if abs(bg.inputs["Strength"].default_value - 7.0) > 1e-4:
        r.fail(name, f"DomeB intensity not applied; got {bg.inputs['Strength'].default_value}")
        return

    # DomeA's later updates are silently ignored (last-wins).
    adapter.set_connectable_input(
        "/World/DomeA", "",
        {"intensity": 99.0}, {"intensity": "float"},
    )
    if abs(bg.inputs["Strength"].default_value - 7.0) > 1e-4:
        r.fail(name, f"DomeA stale update leaked; intensity={bg.inputs['Strength'].default_value}")
        return
    r.ok(name)


def test_ensure_prim_light_with_api_schemas(r):
    """Light ensure_prim must accept the api_schemas kwarg without error
    (lights flow with api_schemas=['ShapingAPI'] etc. — the kwarg is
    USD-stage-only on the Blender side, but it must not raise)."""
    name = "test_ensure_prim_light_with_api_schemas"
    _clear_scene()
    adapter = BlenderAdapter()
    result = adapter.ensure_prim(
        "/World/SpotLight", "SphereLight", api_schemas=["ShapingAPI"]
    )
    if not result:
        r.fail(name, "ensure_prim with api_schemas returned False")
        return
    # The SpotLight should be a Blender LIGHT object even with api_schemas.
    obj = _find_by_prim(adapter, "/World/SpotLight")
    if obj is None or obj.type != "LIGHT":
        r.fail(name, f"expected LIGHT, got {obj.type if obj else 'no object'}")
        return
    r.ok(name)


def test_set_connectable_input_writes_light_attributes(r):
    """USD light inputs (intensity, color, radius, etc.) must land on the
    corresponding Blender light data attributes via set_connectable_input
    with empty info_id (the wire form for non-Shader connectables)."""
    import math

    name = "test_set_connectable_input_writes_light_attributes"
    _clear_scene()
    adapter = BlenderAdapter()

    # SphereLight: intensity → energy, color → color, radius → shadow_soft_size.
    adapter.ensure_prim("/World/Sphere", "SphereLight")
    adapter.set_connectable_input(
        "/World/Sphere",
        "",  # info_id empty for non-Shader connectables
        {"intensity": 7.5, "color": [1.0, 0.5, 0.25], "radius": 0.4},
        {"intensity": "float", "color": "color3f", "radius": "float"},
    )
    sphere = _find_by_prim(adapter, "/World/Sphere")
    if sphere is None or sphere.type != "LIGHT":
        r.fail(name, "SphereLight not created as LIGHT")
        return
    if abs(sphere.data.energy - 7.5) > 1e-4:
        r.fail(name, f"SphereLight energy={sphere.data.energy}, expected 7.5")
        return
    if abs(sphere.data.shadow_soft_size - 0.4) > 1e-4:
        r.fail(
            name,
            f"SphereLight shadow_soft_size={sphere.data.shadow_soft_size}, expected 0.4",
        )
        return
    if tuple(round(c, 3) for c in sphere.data.color) != (1.0, 0.5, 0.25):
        r.fail(name, f"SphereLight color={tuple(sphere.data.color)}, expected (1.0,0.5,0.25)")
        return

    # DistantLight: intensity + angle (USD degrees → Blender radians).
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Sun", "DistantLight")
    adapter.set_connectable_input(
        "/World/Sun",
        "",
        {"intensity": 3.0, "angle": 0.53},  # USD angle in degrees
        {"intensity": "float", "angle": "float"},
    )
    sun = _find_by_prim(adapter, "/World/Sun")
    if sun is None or sun.data.type != "SUN":
        r.fail(name, "DistantLight not created as SUN")
        return
    if abs(sun.data.energy - 3.0) > 1e-4:
        r.fail(name, f"DistantLight energy={sun.data.energy}, expected 3.0")
        return
    expected_rad = math.radians(0.53)
    if abs(sun.data.angle - expected_rad) > 1e-5:
        r.fail(name, f"DistantLight angle={sun.data.angle}, expected {expected_rad}")
        return

    # RectLight: width/height → size/size_y.
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Rect", "RectLight")
    adapter.set_connectable_input(
        "/World/Rect",
        "",
        {"intensity": 5.0, "width": 2.5, "height": 1.5},
        {"intensity": "float", "width": "float", "height": "float"},
    )
    rect = _find_by_prim(adapter, "/World/Rect")
    if rect is None or rect.data.type != "AREA" or rect.data.shape != "RECTANGLE":
        r.fail(name, "RectLight not created as AREA/RECTANGLE")
        return
    if abs(rect.data.size - 2.5) > 1e-4 or abs(rect.data.size_y - 1.5) > 1e-4:
        r.fail(name, f"RectLight size=({rect.data.size},{rect.data.size_y}), expected (2.5,1.5)")
        return

    # DiskLight: radius → size (Blender uses diameter for AREA DISK).
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Disk", "DiskLight")
    adapter.set_connectable_input(
        "/World/Disk",
        "",
        {"intensity": 4.0, "radius": 1.0},
        {"intensity": "float", "radius": "float"},
    )
    disk = _find_by_prim(adapter, "/World/Disk")
    if disk is None or disk.data.type != "AREA" or disk.data.shape != "DISK":
        r.fail(name, "DiskLight not created as AREA/DISK")
        return
    # USD radius=1.0 → Blender size=2.0 (diameter).
    if abs(disk.data.size - 2.0) > 1e-4:
        r.fail(name, f"DiskLight size={disk.data.size}, expected 2.0")
        return

    r.ok(name)


def test_set_connectable_input_on_non_light_is_noop(r):
    """Empty info_id on a non-light prim should not raise — the light input
    handler returns True silently when the prim isn't a Blender LIGHT."""
    name = "test_set_connectable_input_on_non_light_is_noop"
    _clear_scene()
    adapter = BlenderAdapter()
    # Send a connectable input event for a path that has no Blender object.
    result = adapter.set_connectable_input(
        "/World/NotExistent",
        "",
        {"intensity": 1.0},
        {"intensity": "float"},
    )
    if not result:
        r.fail(name, "set_connectable_input on missing path returned False")
        return
    r.ok(name)


def test_ensure_prim_idempotent(r):
    """Calling ensure_prim twice doesn't create duplicates."""
    name = "test_ensure_prim_idempotent"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Thing", "Cube")
    adapter.ensure_prim("/World/Thing", "Cube")
    count = sum(1 for o in bpy.data.objects if o.get("usd_prim_path") == "/World/Thing")
    if count != 1:
        r.fail(name, f"expected 1 object, got {count}")
        return
    r.ok(name)


def test_set_xform_trs(r):
    """set_xform_trs updates location and scale."""
    name = "test_set_xform_trs"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Box", "Cube")
    adapter.set_xform_trs(
        "/World/Box",
        t=[3.0, 4.0, 5.0],
        s=[2.0, 2.0, 2.0],
    )
    obj = _find_by_prim(adapter, "/World/Box")
    if obj is None:
        r.fail(name, "object not found")
        return
    loc = obj.location
    # Adapter converts Y-up (USD) to Z-up (Blender): (x, y, z) → (x, -z, y)
    if abs(loc.x - 3.0) > 1e-4 or abs(loc.y + 5.0) > 1e-4 or abs(loc.z - 4.0) > 1e-4:
        r.fail(name, f"location wrong: {tuple(loc)} (expected ~(3, -5, 4) after Y→Z)")

        return
    scl = obj.scale
    if abs(scl.x - 2.0) > 1e-4:
        r.fail(name, f"scale wrong: {tuple(scl)}")
        return
    r.ok(name)


def test_set_visibility(r):
    """set_visibility hides/unhides objects."""
    name = "test_set_visibility"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Vis", "Cube")

    adapter.set_visibility("/World/Vis", False)
    obj = _find_by_prim(adapter, "/World/Vis")
    if not obj.hide_viewport:
        r.fail(name, "hide_viewport should be True after visible=False")
        return

    adapter.set_visibility("/World/Vis", True)
    if obj.hide_viewport:
        r.fail(name, "hide_viewport should be False after visible=True")
        return
    r.ok(name)


def test_set_gprim_attrs_sphere(r):
    """set_gprim_attrs maps sphere radius to uniform scale."""
    name = "test_set_gprim_attrs_sphere"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/S", "Sphere")
    adapter.set_gprim_attrs("/World/S", {"radius": 3.0})
    obj = _find_by_prim(adapter, "/World/S")
    if obj is None:
        r.fail(name, "object not found")
        return
    if abs(obj.scale.x - 3.0) > 1e-4 or abs(obj.scale.y - 3.0) > 1e-4:
        r.fail(name, f"scale wrong: {tuple(obj.scale)}")
        return
    r.ok(name)


def test_set_gprim_attrs_cube(r):
    """set_gprim_attrs maps cube size to half-scale."""
    name = "test_set_gprim_attrs_cube"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/C", "Cube")
    adapter.set_gprim_attrs("/World/C", {"size": 4.0})
    obj = _find_by_prim(adapter, "/World/C")
    if obj is None:
        r.fail(name, "object not found")
        return
    expected = 4.0 / 2.0  # Blender default cube is 2 units
    if abs(obj.scale.x - expected) > 1e-4:
        r.fail(name, f"scale.x={obj.scale.x}, expected {expected}")
        return
    r.ok(name)


def test_set_gprim_attrs_cylinder(r):
    """set_gprim_attrs maps cylinder height/radius to scale axes."""
    name = "test_set_gprim_attrs_cylinder"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Cyl", "Cylinder")
    adapter.set_gprim_attrs("/World/Cyl", {"radius": 2.0, "height": 6.0})
    obj = _find_by_prim(adapter, "/World/Cyl")
    if obj is None:
        r.fail(name, "object not found")
        return
    if abs(obj.scale.x - 2.0) > 1e-4 or abs(obj.scale.y - 2.0) > 1e-4:
        r.fail(name, f"scale xy wrong: {obj.scale.x}, {obj.scale.y}")
        return
    if abs(obj.scale.z - 3.0) > 1e-4:  # height/2
        r.fail(name, f"scale.z={obj.scale.z}, expected 3.0")
        return
    r.ok(name)


def test_delete_prim(r):
    """delete_prim removes the object."""
    name = "test_delete_prim"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Del", "Cube")
    assert _find_by_prim(adapter, "/World/Del") is not None
    adapter.delete_prim("/World/Del")
    if _find_by_prim(adapter, "/World/Del") is not None:
        r.fail(name, "object still exists after delete")
        return
    r.ok(name)


def test_rename_prim(r):
    """rename_prim updates the custom property."""
    name = "test_rename_prim"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/OldName", "Xform")
    adapter.rename_prim("/World/OldName", "NewName")
    if _find_by_prim(adapter, "/World/OldName") is not None:
        r.fail(name, "old path still resolves")
        return
    obj = _find_by_prim(adapter, "/World/NewName")
    if obj is None:
        r.fail(name, "new path not found")
        return
    r.ok(name)


def test_ensure_xform_ops_resets_mpi(r):
    """ensure_xform_ops resets non-identity matrix_parent_inverse to Identity."""
    name = "test_ensure_xform_ops_resets_mpi"
    _clear_scene()
    adapter = BlenderAdapter()
    import mathutils

    # Create parent and child
    adapter.ensure_prim("/World", "Xform")
    adapter.ensure_prim("/World/Child", "Cube")
    child = _find_by_prim(adapter, "/World/Child")
    if child is None:
        r.fail(name, "child object not created")
        return
    if child.parent is None:
        r.fail(name, "child has no parent")
        return

    # Simulate what Blender's USD importer does: set MPI to a non-identity matrix
    # (e.g. a 90° X rotation that converts Y-up USD to Z-up Blender)
    non_identity = mathutils.Matrix.Rotation(1.5708, 4, "X")
    child.matrix_parent_inverse = non_identity

    # Verify MPI is non-identity
    identity = mathutils.Matrix.Identity(4)
    if child.matrix_parent_inverse == identity:
        r.fail(name, "MPI should be non-identity before ensure_xform_ops")
        return

    # ensure_xform_ops should reset it
    adapter.ensure_xform_ops("/World/Child")

    if child.matrix_parent_inverse != identity:
        r.fail(name, f"MPI not reset to Identity: {child.matrix_parent_inverse}")
        return
    r.ok(name)


def test_ensure_xform_ops_preserves_world(r):
    """ensure_xform_ops preserves matrix_world when resetting MPI."""
    name = "test_ensure_xform_ops_preserves_world"
    _clear_scene()
    adapter = BlenderAdapter()
    import mathutils

    # Create parent and child
    adapter.ensure_prim("/World", "Xform")
    _find_by_prim(adapter, "/World")
    adapter.ensure_prim("/World/Child", "Cube")
    child = _find_by_prim(adapter, "/World/Child")
    if child is None or child.parent is None:
        r.fail(name, "child not created or not parented")
        return

    # Place the child at a known world position
    child.location = (3.0, 4.0, 5.0)
    bpy.context.view_layer.update()

    # Set a non-identity MPI (simulating USD Y-up → Blender Z-up import)
    rot_x_90 = mathutils.Matrix.Rotation(1.5708, 4, "X")
    child.matrix_parent_inverse = rot_x_90
    bpy.context.view_layer.update()

    # Record world position before
    world_before = child.matrix_world.copy()

    # ensure_xform_ops should reset MPI but preserve matrix_world
    adapter.ensure_xform_ops("/World/Child")
    bpy.context.view_layer.update()

    world_after = child.matrix_world.copy()

    # Compare element-by-element with tolerance
    eps = 1e-3
    for i in range(4):
        for j in range(4):
            if abs(world_before[i][j] - world_after[i][j]) > eps:
                r.fail(
                    name,
                    f"matrix_world changed at [{i}][{j}]: "
                    f"{world_before[i][j]:.4f} -> {world_after[i][j]:.4f}",
                )
                return
    r.ok(name)


def test_ensure_xform_ops_identity_noop(r):
    """ensure_xform_ops is a no-op when MPI is already identity."""
    name = "test_ensure_xform_ops_identity_noop"
    _clear_scene()
    adapter = BlenderAdapter()

    adapter.ensure_prim("/World", "Xform")
    adapter.ensure_prim("/World/Child", "Cube")
    child = _find_by_prim(adapter, "/World/Child")
    if child is None or child.parent is None:
        r.fail(name, "child not created or not parented")
        return

    child.location = (1.0, 2.0, 3.0)
    bpy.context.view_layer.update()

    # MPI is already identity (set by ensure_prim)
    basis_before = child.matrix_basis.copy()
    adapter.ensure_xform_ops("/World/Child")
    basis_after = child.matrix_basis.copy()

    if basis_before != basis_after:
        r.fail(name, "matrix_basis changed when MPI was already identity")
        return
    r.ok(name)


def test_ensure_xform_ops_no_parent(r):
    """ensure_xform_ops on unparented object is a no-op (no crash)."""
    name = "test_ensure_xform_ops_no_parent"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/Standalone", "Cube")
    result = adapter.ensure_xform_ops("/Standalone")
    if not result:
        r.fail(name, "ensure_xform_ops returned False")
        return
    r.ok(name)


def test_set_reference_imports_usd(r):
    """set_reference imports a USD file and creates mesh objects under container."""
    import tempfile

    from unit.test_asset_builder import (
        EXPECTED_MESH_COUNT,
        EXPECTED_VERTEX_COUNT,
        create_chair_asset,
    )

    name = "test_set_reference_imports_usd"
    _clear_scene()
    adapter = BlenderAdapter()

    # Create parent prim so parenting works
    adapter.ensure_prim("/World", "Xform")
    # Create the container (as would happen in normal event flow)
    adapter.ensure_prim("/World/Asset", "Xform")

    # Create the shared chair test asset
    tmp_dir = tempfile.mkdtemp()
    tmp_usd = os.path.join(tmp_dir, "test_chair.usda")
    try:
        create_chair_asset(tmp_usd)
    except Exception as e:
        r.fail(name, f"Failed to create test USD: {e}")
        return

    refs = [{"asset_path": tmp_usd}]
    result = adapter.set_reference("/World/Asset", refs)
    if not result:
        r.fail(name, "set_reference returned False")
        return

    # Verify container has Reference type tag
    container = adapter._find_object_by_prim("/World/Asset")
    if container is None:
        r.fail(name, "container not found by prim path /World/Asset")
        return
    if container.get("usd_type_name") != "Reference":
        r.fail(name, f"usd_type_name={container.get('usd_type_name')}, expected Reference")
        return

    # Verify child objects match the chair asset (6 meshes, 48 vertices)
    children = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Asset/")
    ]
    mesh_children = [o for o in children if o.type == "MESH" and o.data is not None]
    if len(mesh_children) != EXPECTED_MESH_COUNT:
        r.fail(
            name,
            f"expected {EXPECTED_MESH_COUNT} meshes, "
            f"got {len(mesh_children)} (types: {[o.type for o in children]})",
        )
        return

    total_verts = sum(len(o.data.vertices) for o in mesh_children)
    if total_verts != EXPECTED_VERTEX_COUNT:
        r.fail(name, f"expected {EXPECTED_VERTEX_COUNT} vertices, got {total_verts}")
        return

    # Verify idempotent — calling again doesn't duplicate
    count_before = len(bpy.data.objects)
    adapter.set_reference("/World/Asset", refs)
    count_after = len(bpy.data.objects)
    if count_after != count_before:
        r.fail(name, f"idempotent check failed: {count_before} -> {count_after} objects")
        return

    r.ok(name)


def test_set_reference_reimport_after_delete(r):
    """set_reference re-imports when cached children have been deleted."""
    import tempfile

    from unit.test_asset_builder import EXPECTED_MESH_COUNT, create_chair_asset

    name = "test_set_reference_reimport_after_delete"
    _clear_scene()
    adapter = BlenderAdapter()

    adapter.ensure_prim("/World", "Xform")
    adapter.ensure_prim("/World/Asset", "Xform")

    tmp_dir = tempfile.mkdtemp()
    tmp_usd = os.path.join(tmp_dir, "test_chair.usda")
    try:
        create_chair_asset(tmp_usd)
    except Exception as e:
        r.fail(name, f"Failed to create test USD: {e}")
        return

    # First import
    refs = [{"asset_path": tmp_usd}]
    result = adapter.set_reference("/World/Asset", refs)
    if not result:
        r.fail(name, "first set_reference returned False")
        return

    children_before = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Asset/")
    ]
    if len(children_before) == 0:
        r.fail(name, "no children after first import")
        return

    # Delete all child objects using the adapter's own cleanup method,
    # which clears registry entries, aliases, and reference children.
    adapter._remove_imported_ref_children("/World/Asset")

    # Verify children are gone
    children_mid = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Asset/")
    ]
    if len(children_mid) != 0:
        r.fail(name, f"children still exist after delete: {len(children_mid)}")
        return

    # Diagnostic: check adapter state before second import
    has_children = adapter._registry.children_exist("/World/Asset")
    has_ref = adapter._registry.get_imported_ref("/World/Asset")
    container = adapter._registry.find("/World/Asset")
    remaining = [p for p in adapter._registry._objects
                 if p.startswith("/World/Asset/")]
    print(f"  info: before reimport: children_exist={has_children} "
          f"imported_ref={has_ref} container={container} "
          f"remaining_children={remaining}")

    # Second import — same asset path, but stale cache should be detected
    all_before = set(bpy.data.objects)
    result2 = adapter.set_reference("/World/Asset", refs)
    if not result2:
        r.fail(name, "second set_reference returned False")
        return
    all_after = set(bpy.data.objects)
    new_from_reimport = all_after - all_before
    print(f"  info: reimport created {len(new_from_reimport)} new objects: "
          f"{[o.name for o in new_from_reimport]}")
    print(f"  info: all objects after: {[o.name for o in bpy.data.objects]}")

    # Verify children were re-imported
    children_after = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Asset/")
    ]
    mesh_children = [o for o in children_after if o.type == "MESH" and o.data is not None]
    if len(mesh_children) != EXPECTED_MESH_COUNT:
        r.fail(
            name, f"expected {EXPECTED_MESH_COUNT} meshes after re-import, got {len(mesh_children)}"
        )
        return
    else:
        print(f"  info: re-imported {len(mesh_children)} meshes after stale cache detection")
    r.ok(name)


def test_set_payload_import_preserves_y_up_orientation(r):
    """A Y-up payload must remain upright after Blender's USD import."""
    import tempfile

    import mathutils
    from unit.test_asset_builder import create_chair_asset

    name = "test_set_payload_import_preserves_y_up_orientation"
    _clear_scene()
    adapter = BlenderAdapter(scene_up_axis="Y")
    adapter.ensure_prim("/World", "Xform")
    adapter.ensure_prim("/World/Asset", "Xform")
    adapter.ensure_xform_ops("/World/Asset")
    adapter.set_xform_trs(
        "/World/Asset",
        t=[0, 0, 0],
        r=[1, 0, 0, 0],
        s=[1, 1, 1],
    )

    tmp_dir = tempfile.mkdtemp()
    tmp_usd = os.path.join(tmp_dir, "chair.usda")
    create_chair_asset(tmp_usd)
    payloads = [{"asset_path": tmp_usd, "prim_path": "/Model"}]
    if not adapter.set_payload("/World/Asset", payloads):
        r.fail(name, "set_payload returned False")
        return
    if not adapter.load_payload("/World/Asset"):
        r.fail(name, "load_payload returned False")
        return

    seat = next(
        (
            obj
            for obj in bpy.data.objects
            if obj.type == "MESH"
            and obj.get("usd_prim_path", "").endswith("/Seat")
        ),
        None,
    )
    if seat is None:
        r.fail(name, "imported Seat mesh not found")
        return

    corners = [seat.matrix_world @ mathutils.Vector(corner) for corner in seat.bound_box]
    dimensions = [
        max(corner[axis] for corner in corners)
        - min(corner[axis] for corner in corners)
        for axis in range(3)
    ]
    if dimensions[2] > min(dimensions[0], dimensions[1]):
        r.fail(name, f"seat tipped after payload import: world dimensions={dimensions}")
        return
    r.ok(name)


def test_ensure_prim_camera_creates_camera_object(r):
    """ensure_prim with typeName=Camera creates a Blender CAMERA object."""
    name = "test_ensure_prim_camera_creates_camera_object"
    _clear_scene()
    adapter = BlenderAdapter()
    if not adapter.ensure_prim("/World/Cam", "Camera"):
        r.fail(name, "ensure_prim returned False")
        return
    obj = _find_by_prim(adapter, "/World/Cam")
    if obj is None or obj.type != "CAMERA":
        r.fail(name, f"expected CAMERA, got {obj.type if obj else 'no object'}")
        return
    if obj.get("usd_type_name") != "Camera":
        r.fail(name, f"usd_type_name={obj.get('usd_type_name')}")
        return
    r.ok(name)


def test_set_gprim_attrs_camera_full_roundtrip(r):
    """Every UsdGeomCamera attr lands on the right Blender camera field.

    Author stage at metersPerUnit=1.0 → tenth_unit_to_mm=100, scene_scale=1.0.
    Inputs are written in USD-native units (e.g. focalLength=0.85 stage-tenths
    of a metre = 85 mm); outputs check the Blender mm/metre values that
    result from the documented conversion.
    """
    name = "test_set_gprim_attrs_camera_full_roundtrip"
    _clear_scene()
    adapter = BlenderAdapter(mirror_stage=_make_stage_with_mpu(1.0))
    adapter.ensure_prim("/World/Cam", "Camera")
    adapter.set_gprim_attrs(
        "/World/Cam",
        {
            "focalLength": 0.85,
            "horizontalAperture": 0.36,
            "verticalAperture": 0.24,
            "horizontalApertureOffset": 0.018,
            "verticalApertureOffset": -0.006,
            "clippingRange": [0.1, 1000.0],
            "focusDistance": 3.5,
            "fStop": 2.8,
            "projection": "perspective",
        },
    )
    obj = _find_by_prim(adapter, "/World/Cam")
    cam = obj.data
    cases = [
        ("lens", cam.lens, 85.0),
        ("sensor_width", cam.sensor_width, 36.0),
        ("sensor_height", cam.sensor_height, 24.0),
        ("clip_start", cam.clip_start, 0.1),
        ("clip_end", cam.clip_end, 1000.0),
        ("dof.focus_distance", cam.dof.focus_distance, 3.5),
        ("dof.aperture_fstop", cam.dof.aperture_fstop, 2.8),
    ]
    for field, got, want in cases:
        if abs(got - want) > 1e-3:
            r.fail(name, f"{field}={got}, expected {want}")
            return
    # shift normalised by sensor_size: shift_x = (offset*tenth_unit_to_mm) / 36
    if abs(cam.shift_x - (0.018 * 100 / 36.0)) > 1e-4:
        r.fail(name, f"shift_x={cam.shift_x}, expected {0.018 * 100 / 36.0}")
        return
    if abs(cam.shift_y - (-0.006 * 100 / 36.0)) > 1e-4:
        r.fail(name, f"shift_y={cam.shift_y}, expected {-0.006 * 100 / 36.0}")
        return
    if not cam.dof.use_dof:
        r.fail(name, "use_dof should be True when fStop > 0")
        return
    if cam.type != "PERSP":
        r.fail(name, f"camera type={cam.type}, expected PERSP")
        return
    r.ok(name)


def test_set_gprim_attrs_camera_receive_only_uses_scene_units(r):
    """Receive-only adapters use hello metadata stored on the Blender scene."""
    name = "test_set_gprim_attrs_camera_receive_only_uses_scene_units"
    _clear_scene()
    scene = bpy.context.scene
    previous_scale = scene.unit_settings.scale_length
    try:
        scene.unit_settings.scale_length = 1.0
        adapter = BlenderAdapter()
        adapter.ensure_prim("/World/Cam", "Camera")
        adapter.set_gprim_attrs(
            "/World/Cam",
            {
                "focalLength": 0.85,
                "horizontalAperture": 0.36,
                "verticalAperture": 0.24,
                "clippingRange": [0.1, 1000.0],
            },
        )
        cam = _find_by_prim(adapter, "/World/Cam").data
        cases = [
            ("lens", cam.lens, 85.0),
            ("sensor_width", cam.sensor_width, 36.0),
            ("sensor_height", cam.sensor_height, 24.0),
            ("clip_start", cam.clip_start, 0.1),
            ("clip_end", cam.clip_end, 1000.0),
        ]
        for field, got, want in cases:
            if abs(got - want) > 1e-3:
                r.fail(name, f"{field}={got}, expected {want}")
                return
    finally:
        scene.unit_settings.scale_length = previous_scale
    r.ok(name)


def test_collapsed_import_parent_xform_alias_receives_early_event(r):
    """A base Xform collapsed into its Geom object still receives parent TRS.

    The material-zoo fixture intentionally sends these edits before its later
    ensure_prim events because the prims already exist in test_scene.usda.
    """
    name = "test_collapsed_import_parent_xform_alias_receives_early_event"
    _clear_scene()
    from integrations.blender.capture import USD_CONNECT_Hook, _ensure_scene_props

    _ensure_scene_props()
    registered_here = not hasattr(bpy.types, USD_CONNECT_Hook.__name__)
    if registered_here:
        bpy.utils.register_class(USD_CONNECT_Hook)
    try:
        bpy.ops.wm.usd_import(filepath=os.path.join(project_root, "test_scene.usda"))
        adapter = BlenderAdapter()
        parent_obj = _find_by_prim(adapter, "/World/Sphere")
        geom_obj = _find_by_prim(adapter, "/World/Sphere/Geom")
        if parent_obj is None or parent_obj is not geom_obj:
            r.fail(name, "collapsed parent and Geom paths did not resolve to one object")
            return
        before_count = len(bpy.data.objects)
        if not adapter.set_xform_trs("/World/Sphere", t=[0.0, 1.5, 0.0]):
            r.fail(name, "parent transform event was not applied")
            return
        if tuple(round(float(value), 6) for value in geom_obj.location) != (0.0, 1.5, 0.0):
            r.fail(name, f"collapsed object location={tuple(geom_obj.location)}")
            return
        if not adapter.ensure_prim("/World/Sphere", "Xform"):
            r.fail(name, "ensure_prim rejected collapsed parent alias")
            return
        if len(bpy.data.objects) != before_count:
            r.fail(name, "ensure_prim created a duplicate parent Empty")
            return
    finally:
        if registered_here:
            bpy.utils.unregister_class(USD_CONNECT_Hook)
    r.ok(name)


def test_set_gprim_attrs_camera_orthographic(r):
    """projection=orthographic sets Blender camera.type to ORTHO."""
    name = "test_set_gprim_attrs_camera_orthographic"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/OrthoCam", "Camera")
    adapter.set_gprim_attrs("/World/OrthoCam", {"projection": "orthographic"})
    obj = _find_by_prim(adapter, "/World/OrthoCam")
    if obj.data.type != "ORTHO":
        r.fail(name, f"camera type={obj.data.type}, expected ORTHO")
        return
    r.ok(name)


def test_set_gprim_attrs_camera_fstop_zero_disables_dof(r):
    """fStop=0 turns off use_dof; non-zero turns it on."""
    name = "test_set_gprim_attrs_camera_fstop_zero_disables_dof"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Cam", "Camera")
    adapter.set_gprim_attrs("/World/Cam", {"fStop": 1.4})
    obj = _find_by_prim(adapter, "/World/Cam")
    if not obj.data.dof.use_dof:
        r.fail(name, "use_dof should be True for fStop=1.4")
        return
    adapter.set_gprim_attrs("/World/Cam", {"fStop": 0.0})
    if obj.data.dof.use_dof:
        r.fail(name, "use_dof should be False for fStop=0.0")
        return
    r.ok(name)


def test_capture_authors_camera_attrs_to_stage(r):
    """BlenderStageAuthor mirrors a Blender camera's fields onto the USD stage,
    and NoticeEmitter picks them up as a set_gprim_attrs event."""
    import tempfile

    from integrations.blender.capture import BlenderStageAuthor
    from openusdconnect.emitter import NoticeEmitter
    from openusdconnect.protocol_constants import K_ENSURE_PRIM, K_SET_GPRIM_ATTRS

    name = "test_capture_authors_camera_attrs_to_stage"
    _clear_scene()

    base_path = os.path.join(
        tempfile.gettempdir(), "openusdconnect_test_camera_base.usda"
    )
    with open(base_path, "w") as f:
        f.write('#usda 1.0\n(\n    metersPerUnit = 1\n)\ndef Xform "World" {}\n')

    author = BlenderStageAuthor(base_usd_path=base_path)
    author.enabled = True
    emitter = NoticeEmitter(author.stage)

    # Create a Blender camera with non-default fields.
    cam_data = bpy.data.cameras.new("TestCam_data")
    cam_data.lens = 85.0
    cam_data.sensor_width = 36.0
    cam_data.sensor_height = 24.0
    cam_data.shift_x = 0.05
    cam_data.shift_y = -0.02
    cam_data.clip_start = 0.1
    cam_data.clip_end = 1000.0
    cam_data.type = "PERSP"
    cam_data.dof.use_dof = True
    cam_data.dof.aperture_fstop = 2.8
    cam_data.dof.focus_distance = 3.5

    cam_obj = bpy.data.objects.new("TestCam", cam_data)
    cam_obj["usd_prim_path"] = "/World/TestCam"
    cam_obj["usd_type_name"] = "Camera"
    bpy.context.scene.collection.objects.link(cam_obj)
    # Parent under /World so _resolve_prim_path works.
    world = bpy.data.objects.new("World", None)
    world["usd_prim_path"] = "/World"
    bpy.context.scene.collection.objects.link(world)
    cam_obj.parent = world

    # Simulate a depsgraph update.
    class _Update:
        def __init__(self, obj):
            self.id = obj

    author.on_depsgraph_update([_Update(cam_obj)])

    events = emitter.build_events_for_dirty()
    ensure_evs = [
        e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == "/World/TestCam"
    ]
    gprim_evs = [
        e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/World/TestCam"
    ]
    if not ensure_evs or ensure_evs[0].get("typeName") != "Camera":
        r.fail(name, f"missing ensure_prim Camera; got {ensure_evs}")
        return
    if not gprim_evs:
        r.fail(name, "no set_gprim_attrs event for camera")
        return
    attrs = gprim_evs[0]["attrs"]
    # Temp stage has metersPerUnit=1.0 → scene_scale=1.0, tenth_unit_to_mm=100.
    # Blender mm → USD tenths-of-metre = value / 100.
    expected = {
        "focalLength": 0.85,                       # 85 mm / 100
        "horizontalAperture": 0.36,                # 36 mm / 100
        "verticalAperture": 0.24,                  # 24 mm / 100
        "horizontalApertureOffset": 0.05 * 36 / 100,
        "verticalApertureOffset": -0.02 * 36 / 100,
        "focusDistance": 3.5,                      # metres pass through
        "fStop": 2.8,
        "projection": "perspective",
    }
    for k, want in expected.items():
        got = attrs.get(k)
        if got is None:
            r.fail(name, f"missing attr {k}")
            return
        if isinstance(want, str):
            if got != want:
                r.fail(name, f"{k}={got!r}, expected {want!r}")
                return
        else:
            if abs(float(got) - want) > 1e-3:
                r.fail(name, f"{k}={got}, expected {want}")
                return
    clip = attrs.get("clippingRange")
    if clip is None or abs(clip[0] - 0.1) > 1e-3 or abs(clip[1] - 1000.0) > 1e-2:
        r.fail(name, f"clippingRange={clip}, expected ~[0.1, 1000.0]")
        return
    r.ok(name)


def test_texture_swap_reverse_sync(r):
    """A tracked Image Texture node emits an asset-typed set_connectable_input
    when its image is swapped: receive creates + tags the node, the seeding
    pass baselines it, and the capture path authors the new path with the
    synced input's exact USD type."""
    import tempfile

    from pxr import Sdf, UsdShade

    from integrations.blender import receiver_addon
    from integrations.blender.capture import BlenderStageAuthor
    from openusdconnect.emitter import NoticeEmitter
    from openusdconnect.protocol_constants import K_SET_CONNECTABLE_INPUT

    name = "test_texture_swap_reverse_sync"
    _clear_scene()

    tex_path = "/World/Looks/SwapMat/SwapTex"

    # Two real image files so Blender can load/resolve them.
    img_paths = []
    for tag in ("a", "b"):
        p = os.path.join(tempfile.gettempdir(), f"openusdconnect_swap_{tag}.png")
        img = bpy.data.images.new(f"swap_src_{tag}", 4, 4)
        img.filepath_raw = p
        img.file_format = "PNG"
        img.save()
        bpy.data.images.remove(img)
        img_paths.append(p)
    path_a, path_b = img_paths

    # Receive side: the framework creates and tags the texture node.
    adapter = BlenderAdapter()
    adapter.set_connectable_input(tex_path, "UsdUVTexture", {"file": path_a}, {"file": "asset"})
    mat = adapter._find_material_for_shader(tex_path)
    node = adapter._find_shader_node(mat.node_tree, tex_path) if mat else None
    if node is None or node.image is None:
        r.fail(name, f"receive did not create a tagged, loaded texture node (mat={mat})")
        return

    # Emitter mirror stage carrying the synced state (what the dispatcher's
    # stage commit authors), so type resolution sees the real asset input.
    base_path = os.path.join(tempfile.gettempdir(), "openusdconnect_swap_base.usda")
    with open(base_path, "w") as f:
        f.write('#usda 1.0\ndef Xform "World" {}\n')
    author = BlenderStageAuthor(base_usd_path=base_path)
    author.enabled = True
    shader = UsdShade.Shader(author.stage.DefinePrim(tex_path, "Shader"))
    shader.CreateIdAttr("UsdUVTexture")
    shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(path_a))

    # Post-apply seeding: baseline the tracked node from its current state.
    receiver_addon._refresh_shader_reverse_sync_state(
        author,
        adapter,
        "/World/Looks/SwapMat",
    )
    seeded = author._last_shader_values.get(tex_path)
    if not seeded or os.path.normcase(seeded.get("file", "")) != os.path.normcase(
        os.path.normpath(path_a)
    ):
        r.fail(name, f"baseline not seeded from tracked node: {seeded}")
        return

    emitter = NoticeEmitter(author.stage)
    emitter.build_events_for_dirty()  # flush first-encounter state

    # Local edit: swap the image and run the capture path.
    node.image = bpy.data.images.load(path_b, check_existing=True)
    author.on_material_update([mat])

    events = [
        e
        for e in emitter.build_events_for_dirty()
        if e["k"] == K_SET_CONNECTABLE_INPUT and e["prim"] == tex_path
    ]
    if not events:
        r.fail(name, "no set_connectable_input emitted for the image swap")
        return
    ev = events[0]
    got_file = ev.get("inputs", {}).get("file", "")
    if os.path.normcase(os.path.normpath(got_file)) != os.path.normcase(
        os.path.normpath(path_b)
    ):
        r.fail(name, f"file={got_file!r}, expected {path_b!r}")
        return
    if ev.get("input_types", {}).get("file") != "asset":
        r.fail(name, f"input_types={ev.get('input_types')}, expected file: asset")
        return
    if ev.get("info_id") != "UsdUVTexture":
        r.fail(name, f"info_id={ev.get('info_id')!r}")
        return
    r.ok(name)


def test_capture_camera_move_emits_trs_only(r):
    """After first encounter, moving the camera emits set_xform_trs but no
    camera attrs event (params unchanged → CameraAttrsChannel.diff is empty)."""
    import tempfile

    from integrations.blender.capture import BlenderStageAuthor
    from openusdconnect.emitter import NoticeEmitter
    from openusdconnect.protocol_constants import K_SET_GPRIM_ATTRS, K_SET_XFORM_TRS

    name = "test_capture_camera_move_emits_trs_only"
    _clear_scene()

    base_path = os.path.join(
        tempfile.gettempdir(), "openusdconnect_test_cam_move_base.usda"
    )
    with open(base_path, "w") as f:
        f.write('#usda 1.0\n(\n    metersPerUnit = 1\n)\ndef Xform "World" {}\n')

    author = BlenderStageAuthor(base_usd_path=base_path)
    author.enabled = True
    emitter = NoticeEmitter(author.stage)

    cam_data = bpy.data.cameras.new("MoveCam_data")
    cam_obj = bpy.data.objects.new("MoveCam", cam_data)
    cam_obj["usd_prim_path"] = "/World/MoveCam"
    cam_obj["usd_type_name"] = "Camera"
    bpy.context.scene.collection.objects.link(cam_obj)
    world = bpy.data.objects.new("World", None)
    world["usd_prim_path"] = "/World"
    bpy.context.scene.collection.objects.link(world)
    cam_obj.parent = world

    class _Update:
        def __init__(self, obj, *, transform=True, geometry=False):
            self.id = obj
            self.is_updated_transform = transform
            self.is_updated_geometry = geometry

    # First encounter — drain initial events.
    author.on_depsgraph_update([_Update(cam_obj, transform=True, geometry=True)])
    emitter.build_events_for_dirty()

    # Move only — change location, leave camera data alone.
    cam_obj.location = (3.0, 4.0, 5.0)
    bpy.context.view_layer.update()
    author.on_depsgraph_update([_Update(cam_obj, transform=True, geometry=False)])

    events = emitter.build_events_for_dirty()
    trs = [e for e in events if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/MoveCam"]
    cam_attrs = [
        e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/World/MoveCam"
    ]
    if not trs:
        r.fail(name, "expected set_xform_trs after moving camera, got none")
        return
    if cam_attrs:
        r.fail(name, f"unexpected set_gprim_attrs on pure move: {cam_attrs[0]['attrs']}")
        return
    r.ok(name)


def test_capture_camera_lens_only_emits_attrs(r):
    """Changing only the lens (no transform change) must still emit a
    set_gprim_attrs event with the new focalLength."""
    import tempfile

    from integrations.blender.capture import BlenderStageAuthor
    from openusdconnect.emitter import NoticeEmitter
    from openusdconnect.protocol_constants import K_SET_GPRIM_ATTRS

    name = "test_capture_camera_lens_only_emits_attrs"
    _clear_scene()

    base_path = os.path.join(
        tempfile.gettempdir(), "openusdconnect_test_cam_lens_base.usda"
    )
    with open(base_path, "w") as f:
        f.write('#usda 1.0\n(\n    metersPerUnit = 1\n)\ndef Xform "World" {}\n')

    author = BlenderStageAuthor(base_usd_path=base_path)
    author.enabled = True
    emitter = NoticeEmitter(author.stage)

    cam_data = bpy.data.cameras.new("LensCam_data")
    cam_data.lens = 50.0
    cam_obj = bpy.data.objects.new("LensCam", cam_data)
    cam_obj["usd_prim_path"] = "/World/LensCam"
    cam_obj["usd_type_name"] = "Camera"
    bpy.context.scene.collection.objects.link(cam_obj)
    world = bpy.data.objects.new("World", None)
    world["usd_prim_path"] = "/World"
    bpy.context.scene.collection.objects.link(world)
    cam_obj.parent = world

    class _Update:
        def __init__(self, obj, *, transform=True, geometry=False):
            self.id = obj
            self.is_updated_transform = transform
            self.is_updated_geometry = geometry

    author.on_depsgraph_update([_Update(cam_obj, transform=True, geometry=True)])
    emitter.build_events_for_dirty()

    # Change only the lens.
    cam_data.lens = 85.0
    author.on_depsgraph_update([_Update(cam_obj, transform=False, geometry=True)])
    events = emitter.build_events_for_dirty()
    cam_attrs = [
        e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/World/LensCam"
    ]
    if not cam_attrs:
        r.fail(name, "expected set_gprim_attrs after lens change, got none")
        return
    attrs = cam_attrs[0]["attrs"]
    # mpu=1.0 → tenth_unit_to_mm=100; Blender lens=85 mm → focalLength=0.85.
    if abs(float(attrs.get("focalLength", 0.0)) - 0.85) > 1e-3:
        r.fail(name, f"focalLength={attrs.get('focalLength')}, expected 0.85")
        return
    r.ok(name)


def test_set_reference_missing_file(r):
    """set_reference with non-existent file returns False gracefully."""
    name = "test_set_reference_missing_file"
    _clear_scene()
    adapter = BlenderAdapter()
    result = adapter.set_reference(
        "/World/Missing", [{"asset_path": "/nonexistent/path/fake.usda"}]
    )
    if result:
        r.fail(name, "set_reference should return False for missing file")
        return
    # Verify no objects were created
    count = sum(
        1 for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Missing")
    )
    if count != 0:
        r.fail(name, f"expected 0 objects, got {count}")
        return
    r.ok(name)


def main():
    print("\n=== BlenderAdapter Headless Tests ===\n")
    r = TestResult()

    tests = [
        test_ensure_prim_xform,
        test_ensure_prim_cube,
        test_ensure_prim_sphere,
        test_ensure_prim_cylinder,
        test_ensure_prim_cone,
        test_ensure_prim_mesh,
        test_ensure_prim_supported_light_types_create_blender_lights,
        test_ensure_prim_skipped_light_types_create_no_object,
        test_ensure_prim_light_with_api_schemas,
        test_set_connectable_input_writes_light_attributes,
        test_set_connectable_input_on_non_light_is_noop,
        test_ensure_prim_domelight_sets_up_world_network,
        test_set_connectable_input_domelight_writes_world_shader,
        test_set_xform_trs_domelight_writes_mapping_rotation,
        test_multi_domelight_last_wins,
        test_ensure_prim_idempotent,
        test_set_xform_trs,
        test_set_visibility,
        test_set_gprim_attrs_sphere,
        test_set_gprim_attrs_cube,
        test_set_gprim_attrs_cylinder,
        test_delete_prim,
        test_rename_prim,
        test_ensure_xform_ops_resets_mpi,
        test_ensure_xform_ops_preserves_world,
        test_ensure_xform_ops_identity_noop,
        test_ensure_xform_ops_no_parent,
        test_set_reference_imports_usd,
        test_set_reference_reimport_after_delete,
        test_set_payload_import_preserves_y_up_orientation,
        test_set_reference_missing_file,
        test_ensure_prim_camera_creates_camera_object,
        test_set_gprim_attrs_camera_full_roundtrip,
        test_set_gprim_attrs_camera_receive_only_uses_scene_units,
        test_collapsed_import_parent_xform_alias_receives_early_event,
        test_set_gprim_attrs_camera_orthographic,
        test_set_gprim_attrs_camera_fstop_zero_disables_dof,
        test_capture_authors_camera_attrs_to_stage,
        test_capture_camera_move_emits_trs_only,
        test_capture_camera_lens_only_emits_attrs,
        test_texture_swap_reverse_sync,
    ]

    for t in tests:
        try:
            t(r)
        except Exception:
            r.fail(t.__name__, traceback.format_exc())

    print(f"\n=== Results: {r.passed} passed, {r.failed} failed ===\n")
    if r.errors:
        for name, msg in r.errors:
            print(f"  FAILED: {name}: {msg}")

    sys.exit(0 if r.failed == 0 else 1)


if __name__ == "__main__":
    main()
