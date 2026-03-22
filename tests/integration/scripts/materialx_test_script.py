"""Blender headless test for MaterialX shader mappers.

Tests both the vendored ActivisionMtlxMapper (delegates to io_blender_mtlx
handlers at runtime) and the fallback MaterialXStandardSurfaceMapper.

Run via: blender --background --python materialx_test_script.py
Exit code 0 = all tests pass, 1 = failure.
"""

import os
import sys
import traceback

import bpy

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(_scripts_dir)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
for _k in [k for k in sys.modules if k.startswith("openusdconnect")]:
    del sys.modules[_k]

from integrations.blender.shader_mapper import (
    ActivisionMtlxMapper,
    MaterialXStandardSurfaceMapper,
    create_default_registry,
)


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


def _fresh_material(name):
    """Create a new material with use_nodes=True and return it."""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    return mat


def _clear_materials():
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)


# ------------------------------------------------------------------
# Test: registry returns vendored mapper for Standard Surface
# ------------------------------------------------------------------

def test_registry_returns_vendored_mapper(r):
    name = "registry_returns_vendored_mapper"
    reg = create_default_registry()
    mapper = reg.get("ND_standard_surface_surfaceshader")
    if mapper is None:
        r.fail(name, "mapper not found in registry")
        return
    if not mapper.is_multi_node:
        r.fail(name, f"is_multi_node={mapper.is_multi_node}, expected True")
        return
    if not isinstance(mapper, ActivisionMtlxMapper):
        r.fail(name, f"expected ActivisionMtlxMapper, got {type(mapper).__name__}")
        return
    r.ok(name)


# ------------------------------------------------------------------
# Test: registry has OpenPBR and utility handlers from io_blender_mtlx
# ------------------------------------------------------------------

def test_registry_vendored_coverage(r):
    name = "registry_vendored_coverage"
    reg = create_default_registry()

    expected_vendored = [
        "ND_standard_surface_surfaceshader",
        "ND_open_pbr_surface_surfaceshader",
        "ND_surfacematerial",
        "ND_normalmap_float",
        "ND_multiply_float",
        "ND_mix_color3",
    ]
    missing = []
    for sid in expected_vendored:
        mapper = reg.get(sid)
        if mapper is None:
            missing.append(sid)
        elif not isinstance(mapper, ActivisionMtlxMapper):
            missing.append(f"{sid} (got {type(mapper).__name__})")

    if missing:
        r.fail(name, f"missing vendored mappers: {missing}")
        return

    # Texture nodes should NOT be vendored (our TextureShaderMapper handles them)
    tex_mapper = reg.get("ND_image_color3")
    if tex_mapper is not None and isinstance(tex_mapper, ActivisionMtlxMapper):
        r.fail(name, "ND_image_color3 should use TextureShaderMapper, not vendored")
        return

    r.ok(name)


# ------------------------------------------------------------------
# Test: our mapper creates 5 nodes of the correct types
# ------------------------------------------------------------------

def test_our_mapper_node_count_and_types(r):
    name = "our_mapper_node_count_and_types"
    _clear_materials()
    mat = _fresh_material("TestStdSurf")
    mapper = MaterialXStandardSurfaceMapper(
        "ND_standard_surface_surfaceshader", "ShaderNodeBsdfPrincipled", {},
    )
    nodes, input_map, output_map = mapper.create_network(
        mat.node_tree, {},
    )
    if len(nodes) != 5:
        r.fail(name, f"expected 5 nodes, got {len(nodes)}")
        return

    expected_types = [
        "ShaderNodeBsdfPrincipled",
        "ShaderNodeMix",
        "ShaderNodeHueSaturation",
        "ShaderNodeMix",
        "ShaderNodeHueSaturation",
    ]
    actual_types = [n.bl_idname for n in nodes]
    if actual_types != expected_types:
        r.fail(name, f"types={actual_types}")
        return
    r.ok(name)


# ------------------------------------------------------------------
# Test: 4 internal links wired correctly
# ------------------------------------------------------------------

def _link_key(link):
    """Return a comparable tuple for a node tree link."""
    return (
        link.from_node.bl_idname, link.from_node.name,
        link.from_socket.name,
        link.to_node.bl_idname, link.to_node.name,
        link.to_socket.name,
    )


def test_our_mapper_internal_links(r):
    name = "our_mapper_internal_links"
    _clear_materials()
    mat = _fresh_material("TestLinks")
    # Remove default nodes so we only see our links
    for node in list(mat.node_tree.nodes):
        mat.node_tree.nodes.remove(node)

    mapper = MaterialXStandardSurfaceMapper(
        "ND_standard_surface_surfaceshader", "ShaderNodeBsdfPrincipled", {},
    )
    nodes, _, _ = mapper.create_network(mat.node_tree, {})
    bsdf, mix_base, huesat_base, mix_spec, huesat_spec = nodes

    links = mat.node_tree.links
    if len(links) != 4:
        r.fail(name, f"expected 4 links, got {len(links)}")
        return

    # Build set of (from_node, from_socket_idx, to_node, to_socket_idx)
    link_set = set()
    for link in links:
        link_set.add((
            link.from_node.name, link.from_socket.name,
            link.to_node.name, link.to_socket.name,
        ))

    expected = {
        (huesat_base.name, huesat_base.outputs[0].name,
         mix_base.name, mix_base.inputs[6].name),
        (mix_base.name, mix_base.outputs[2].name,
         bsdf.name, bsdf.inputs["Base Color"].name),
        (huesat_spec.name, huesat_spec.outputs[0].name,
         mix_spec.name, mix_spec.inputs[6].name),
        (mix_spec.name, mix_spec.outputs[2].name,
         bsdf.name, bsdf.inputs["Specular Tint"].name),
    }

    if link_set != expected:
        r.fail(name, f"link mismatch:\n  got={link_set}\n  expected={expected}")
        return
    r.ok(name)


# ------------------------------------------------------------------
# Test: all 25 Standard Surface inputs are mapped
# ------------------------------------------------------------------

_EXPECTED_INPUTS = {
    "base", "base_color", "metalness", "diffuse_roughness",
    "specular", "specular_color", "specular_IOR",
    "specular_anisotropy", "specular_rotation", "specular_roughness",
    "transmission",
    "subsurface", "subsurface_radius", "subsurface_scale",
    "subsurface_anisotropy",
    "sheen", "sheen_color", "sheen_roughness",
    "coat", "coat_color", "coat_roughness", "coat_ior", "coat_normal",
    "thin_film_thickness", "thin_film_IOR",
    "emission", "emission_color",
    "normal", "tangent",
}


def test_our_mapper_input_coverage(r):
    name = "our_mapper_input_coverage"
    _clear_materials()
    mat = _fresh_material("TestCoverage")
    mapper = MaterialXStandardSurfaceMapper(
        "ND_standard_surface_surfaceshader", "ShaderNodeBsdfPrincipled", {},
    )
    _, input_map, output_map = mapper.create_network(mat.node_tree, {})

    missing = _EXPECTED_INPUTS - set(input_map.keys())
    if missing:
        r.fail(name, f"missing inputs: {missing}")
        return

    if "out" not in output_map:
        r.fail(name, "missing 'out' in output_map")
        return
    r.ok(name)


# ------------------------------------------------------------------
# Test: values are applied to the correct sockets
# ------------------------------------------------------------------

def test_our_mapper_value_application(r):
    name = "our_mapper_value_application"
    _clear_materials()
    mat = _fresh_material("TestValues")
    mapper = MaterialXStandardSurfaceMapper(
        "ND_standard_surface_surfaceshader", "ShaderNodeBsdfPrincipled", {},
    )
    test_inputs = {
        "base_color": [0.8, 0.2, 0.1],
        "metalness": 0.9,
        "specular_roughness": 0.3,
        "coat": 0.5,
    }
    _, input_map, _ = mapper.create_network(mat.node_tree, test_inputs)

    # Apply values (same logic as _apply_multi_node_shader)
    for usd_name, value in test_inputs.items():
        socket = input_map.get(usd_name)
        if socket is None:
            r.fail(name, f"no socket for {usd_name}")
            return
        if isinstance(value, list) and len(value) == 3:
            socket.default_value = (*value, 1.0)
        else:
            socket.default_value = value

    # Verify values landed on the right sockets
    # base_color → Mix.B input (RGBA socket)
    bc = input_map["base_color"].default_value
    if abs(bc[0] - 0.8) > 0.01 or abs(bc[1] - 0.2) > 0.01:
        r.fail(name, f"base_color value={tuple(bc)}")
        return

    # metalness → Principled BSDF.Metallic
    met = input_map["metalness"].default_value
    if abs(met - 0.9) > 0.01:
        r.fail(name, f"metalness value={met}")
        return

    # specular_roughness → Principled BSDF.Roughness
    rough = input_map["specular_roughness"].default_value
    if abs(rough - 0.3) > 0.01:
        r.fail(name, f"specular_roughness value={rough}")
        return

    # coat → Principled BSDF.Coat Weight
    coat = input_map["coat"].default_value
    if abs(coat - 0.5) > 0.01:
        r.fail(name, f"coat value={coat}")
        return

    r.ok(name)


# ------------------------------------------------------------------
# Test: idempotent — second call reuses existing nodes
# ------------------------------------------------------------------

def test_our_mapper_idempotent(r):
    name = "our_mapper_idempotent"
    _clear_materials()
    mat = _fresh_material("TestIdemp")
    mapper = MaterialXStandardSurfaceMapper(
        "ND_standard_surface_surfaceshader", "ShaderNodeBsdfPrincipled", {},
    )
    nodes1, _, _ = mapper.create_network(mat.node_tree, {})
    nodes2, _, _ = mapper.create_network(mat.node_tree, {})

    # Same Blender node objects returned
    if nodes1[0] != nodes2[0]:
        r.fail(name, "BSDF node not reused")
        return
    for i in range(1, 5):
        if nodes1[i] != nodes2[i]:
            r.fail(name, f"node {i} not reused: {nodes1[i].name} vs {nodes2[i].name}")
            return

    # Node count should not increase
    total = len(mat.node_tree.nodes)
    # 5 our nodes + 1 Material Output (from use_nodes=True)
    if total > 6:
        r.fail(name, f"expected <=6 nodes after 2 calls, got {total}")
        return
    r.ok(name)


# ------------------------------------------------------------------
# Test: parity with io_blender_mtlx handler
# ------------------------------------------------------------------

def _socket_identity(socket):
    """Comparable identity: (node_type, node_name, socket_name)."""
    return (socket.node.bl_idname, socket.name)


def _compare_against_reference(r, name, our_tree, our_nodes, our_inputs, our_outputs):
    """Compare a mapper's output against io_blender_mtlx's handler."""
    import MaterialX as mx

    vendor_path = os.path.join(
        project_root, "vendor", "io_blender_mtlx",
        "bl_env", "addons",
    )
    if vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)

    from io_data_mtlx.lib.standard_surface import (
        ND_standard_surface_surfaceshader as ref_handler,
    )

    doc = mx.createDocument()
    mx.loadLibraries(mx.getDefaultDataLibraryFolders(), mx.getDefaultDataSearchPath(), doc)
    node_graph = doc.addNodeGraph(f"NG_{name}")
    mx_node = node_graph.addNode(
        "standard_surface", "test_std_surf", "surfaceshader",
    )

    ref_mat = _fresh_material(f"Ref_{name}")
    for node in list(ref_mat.node_tree.nodes):
        ref_mat.node_tree.nodes.remove(node)
    ref_nodes, ref_inputs, ref_outputs = ref_handler(
        ref_mat.node_tree, mx_node,
    )

    # Compare node count
    if len(ref_nodes) != len(our_nodes):
        r.fail(name, f"node count: ref={len(ref_nodes)} ours={len(our_nodes)}")
        return

    # Compare node types in order
    ref_types = [n.bl_idname for n in ref_nodes]
    our_types = [n.bl_idname for n in our_nodes]
    if ref_types != our_types:
        r.fail(name, f"node types: ref={ref_types} ours={our_types}")
        return

    # Compare link topology
    def link_topology(tree):
        result = set()
        for link in tree.links:
            result.add((
                link.from_node.bl_idname, link.from_socket.name,
                link.to_node.bl_idname, link.to_socket.name,
            ))
        return result

    ref_links = link_topology(ref_mat.node_tree)
    our_links = link_topology(our_tree)
    if ref_links != our_links:
        r.fail(name, f"link mismatch:\n  ref={ref_links}\n  ours={our_links}")
        return

    # Compare input mapping
    for input_name, ref_socket in ref_inputs.items():
        if input_name not in our_inputs:
            r.fail(name, f"missing input '{input_name}'")
            return
        ref_id = _socket_identity(ref_socket)
        our_id = _socket_identity(our_inputs[input_name])
        if ref_id != our_id:
            r.fail(name, f"input '{input_name}': ref={ref_id} ours={our_id}")
            return

    # Compare output mapping
    for out_name, ref_socket in ref_outputs.items():
        if out_name not in our_outputs:
            r.fail(name, f"missing output '{out_name}'")
            return
        ref_id = _socket_identity(ref_socket)
        our_id = _socket_identity(our_outputs[out_name])
        if ref_id != our_id:
            r.fail(name, f"output '{out_name}': ref={ref_id} ours={our_id}")
            return

    r.ok(name)


def test_fallback_parity_with_io_blender_mtlx(r):
    """Compare fallback MaterialXStandardSurfaceMapper against reference."""
    name = "fallback_parity_with_io_blender_mtlx"
    _clear_materials()

    our_mat = _fresh_material("FallbackMaterial")
    for node in list(our_mat.node_tree.nodes):
        our_mat.node_tree.nodes.remove(node)
    mapper = MaterialXStandardSurfaceMapper(
        "ND_standard_surface_surfaceshader", "ShaderNodeBsdfPrincipled", {},
    )
    our_nodes, our_inputs, our_outputs = mapper.create_network(
        our_mat.node_tree, {},
    )
    _compare_against_reference(
        r, name, our_mat.node_tree, our_nodes, our_inputs, our_outputs,
    )


def test_vendored_parity_with_io_blender_mtlx(r):
    """Compare ActivisionMtlxMapper against reference.

    Since the vendored mapper delegates to the same handler,
    this validates the mx.Node creation + handler delegation path.
    """
    name = "vendored_parity_with_io_blender_mtlx"
    _clear_materials()

    our_mat = _fresh_material("VendoredMaterial")
    for node in list(our_mat.node_tree.nodes):
        our_mat.node_tree.nodes.remove(node)
    mapper = ActivisionMtlxMapper(
        "ND_standard_surface_surfaceshader", "", {},
    )
    our_nodes, our_inputs, our_outputs = mapper.create_network(
        our_mat.node_tree, {},
    )
    _compare_against_reference(
        r, name, our_mat.node_tree, our_nodes, our_inputs, our_outputs,
    )


def test_vendored_open_pbr(r):
    """Vendored OpenPBR mapper creates a valid node network."""
    name = "vendored_open_pbr"
    _clear_materials()

    mat = _fresh_material("TestOpenPBR")
    for node in list(mat.node_tree.nodes):
        mat.node_tree.nodes.remove(node)
    mapper = ActivisionMtlxMapper(
        "ND_open_pbr_surface_surfaceshader", "", {},
    )
    nodes, input_map, output_map = mapper.create_network(mat.node_tree, {})

    if len(nodes) < 5:
        r.fail(name, f"expected >=5 nodes, got {len(nodes)}")
        return
    if "out" not in output_map:
        r.fail(name, "missing 'out' in output_map")
        return
    if "base_color" not in input_map:
        r.fail(name, "missing 'base_color' in input_map")
        return
    r.ok(name)


# ------------------------------------------------------------------
# End-to-end: full adapter flow with brass material from basicTextured
# ------------------------------------------------------------------

def test_adapter_brass_material(r):
    """End-to-end: BlenderAdapter creates Standard Surface brass material.

    Simulates the event sequence that the emitter would produce for
    the Tiled_Brass material from basicTextured_flatten.usda:
    - set_shader_input for ND_standard_surface_surfaceshader (brass values)
    - set_shader_input for ND_tiledimage_color3 (texture node)
    - set_shader_connection wiring texture → Standard Surface
    """
    name = "adapter_brass_material"
    _clear_materials()
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)

    from integrations.blender.blender_adapter import BlenderAdapter

    adapter = BlenderAdapter()

    # 1. Create a mesh and bind the material to it
    adapter.ensure_prim("/World/Teapot", "Mesh")
    adapter.set_material_binding(
        "/World/Teapot",
        "/MaterialX/Materials/Tiled_Brass",
    )

    # 2. set_shader_input for the Standard Surface shader (brass values)
    adapter.set_shader_input(
        "/MaterialX/Materials/Tiled_Brass/ND_standard_surface_surfaceshader",
        "ND_standard_surface_surfaceshader",
        {
            "base": 1.0,
            "base_color": [1.0, 1.0, 1.0],
            "metalness": 1.0,
            "specular": 0.0,
            "coat": 1.0,
            "specular_roughness": 0.3,
        },
        {
            "base": "float",
            "base_color": "color3f",
            "metalness": "float",
            "specular": "float",
            "coat": "float",
            "specular_roughness": "float",
        },
    )

    # 3. set_shader_input for the texture node
    adapter.set_shader_input(
        "/MaterialX/Materials/Tiled_Brass/NG_brass1/image_color",
        "ND_tiledimage_color3",
        {"file": "textures/brass_color.jpg"},
        {"file": "asset"},
    )

    # 4. set_shader_connection: wire texture → Standard Surface coat_color
    adapter.set_shader_connection(
        "/MaterialX/Materials/Tiled_Brass/ND_standard_surface_surfaceshader",
        {
            "coat_color": {
                "source_prim": "/MaterialX/Materials/Tiled_Brass/NG_brass1/image_color",
                "source_output": "out",
            },
        },
    )

    # --- Verify ---
    mat = bpy.data.materials.get("Tiled_Brass")
    if mat is None:
        r.fail(name, "material Tiled_Brass not created")
        return
    if not mat.use_nodes:
        r.fail(name, "material has no node tree")
        return

    tree = mat.node_tree

    # Should have: Principled BSDF, 2 Mix, 2 HueSat (from Standard Surface)
    # + Material Output (auto-created) + Image Texture (from texture event)
    bsdf = None
    img_tex = None
    for node in tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            bsdf = node
        if node.bl_idname == "ShaderNodeTexImage":
            img_tex = node

    if bsdf is None:
        r.fail(name, "no Principled BSDF found")
        return
    if img_tex is None:
        r.fail(name, "no Image Texture found")
        return

    # Check brass values were applied (via multi-node input_map)
    cached_inputs = adapter._prim_cache.get(
        "/MaterialX/Materials/Tiled_Brass/ND_standard_surface_surfaceshader:input_map",
    )
    if cached_inputs is None:
        r.fail(name, "no cached input_map for Standard Surface")
        return

    met_val = cached_inputs["metalness"].default_value
    if abs(met_val - 1.0) > 0.01:
        r.fail(name, f"metalness={met_val}, expected 1.0")
        return

    coat_val = cached_inputs["coat"].default_value
    if abs(coat_val - 1.0) > 0.01:
        r.fail(name, f"coat={coat_val}, expected 1.0")
        return

    spec_val = cached_inputs["specular"].default_value
    if abs(spec_val - 0.0) > 0.01:
        r.fail(name, f"specular={spec_val}, expected 0.0")
        return

    # Check coat_color socket has a link (texture connected)
    coat_color_socket = cached_inputs.get("coat_color")
    if coat_color_socket is None:
        r.fail(name, "no coat_color in input_map")
        return
    if not coat_color_socket.links:
        r.fail(name, "coat_color has no link (texture not connected)")
        return

    # Verify the link source is the Image Texture
    link_from = coat_color_socket.links[0].from_node
    if link_from.bl_idname != "ShaderNodeTexImage":
        r.fail(name, f"coat_color link from {link_from.bl_idname}, expected ShaderNodeTexImage")
        return

    # Verify object has the material assigned
    obj = None
    for o in bpy.data.objects:
        if o.get("usd_prim_path") == "/World/Teapot":
            obj = o
            break
    if obj is None:
        r.fail(name, "Teapot object not found")
        return
    if not obj.data.materials or obj.data.materials[0] != mat:
        r.fail(name, "material not assigned to Teapot")
        return

    r.ok(name)


# ------------------------------------------------------------------
# Post-import MaterialX enrichment from USD reference
# ------------------------------------------------------------------

def test_enrichment_from_reference(r):
    """set_reference imports teapot, enrichment applies MaterialX material."""
    name = "enrichment_from_reference"
    _clear_materials()
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)

    from integrations.blender.blender_adapter import BlenderAdapter

    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Teapot", "Xform")
    adapter.set_reference("/World/Teapot", [{
        "asset_path": os.path.join(
            project_root, "assets", "intent-vfx", "assets", "teapot", "teapot.usd",
        ),
        "prim_path": "/teapot",
    }])

    # Verify: material should exist and have MaterialX Standard Surface network
    mat = bpy.data.materials.get("default_material")
    if mat is None:
        r.fail(name, "default_material not found")
        return
    if not mat.use_nodes:
        r.fail(name, "material has no node tree")
        return

    # Should have more than just the default Principled BSDF + Material Output
    # (enrichment adds HueSat + Mix preprocessing nodes)
    tree = mat.node_tree
    bsdf = None
    for node in tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            bsdf = node

    if bsdf is None:
        r.fail(name, "no Principled BSDF found")
        return

    # Check the cached input_map exists (proves multi-node mapper ran)
    mtlx_path = "/World/Teapot/mtl/default_material/default_shader_mtlx"
    cached = adapter._prim_cache.get(mtlx_path + ":input_map")
    if cached is None:
        r.fail(name, "no cached input_map — enrichment didn't run")
        return

    # Verify a MaterialX-specific value was applied (base=1.0 from the teapot)
    base_socket = cached.get("base")
    if base_socket is None:
        r.fail(name, "no 'base' in input_map")
        return
    if abs(base_socket.default_value - 1.0) > 0.01:
        r.fail(name, f"base={base_socket.default_value}, expected 1.0")
        return

    r.ok(name)


# ------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------

def main():
    print("\n=== MaterialX Shader Mapper Tests ===\n")
    r = TestResult()

    tests = [
        # Registry
        test_registry_returns_vendored_mapper,
        test_registry_vendored_coverage,
        # Fallback mapper (MaterialXStandardSurfaceMapper)
        test_our_mapper_node_count_and_types,
        test_our_mapper_internal_links,
        test_our_mapper_input_coverage,
        test_our_mapper_value_application,
        test_our_mapper_idempotent,
        # Parity: both mappers vs io_blender_mtlx reference
        test_fallback_parity_with_io_blender_mtlx,
        test_vendored_parity_with_io_blender_mtlx,
        # Vendored OpenPBR
        test_vendored_open_pbr,
        # End-to-end adapter with real material values
        test_adapter_brass_material,
        # Post-import MaterialX enrichment
        test_enrichment_from_reference,
    ]

    for t in tests:
        try:
            t(r)
        except Exception:
            r.fail(t.__name__, traceback.format_exc())

    print(f"\n=== Results: {r.passed} passed, {r.failed} failed ===\n")
    if r.errors:
        for err_name, msg in r.errors:
            print(f"  FAILED: {err_name}: {msg}")

    sys.exit(0 if r.failed == 0 else 1)


if __name__ == "__main__":
    main()
