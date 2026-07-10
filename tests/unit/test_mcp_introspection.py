"""Mirror introspection: pagination, filtering, projection, summary, diffs."""

from __future__ import annotations

import pytest
from pxr import Usd, UsdGeom, UsdShade

from integrations.mcp import introspection
from integrations.mcp.errors import ToolError


def _scene():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    for i in range(5):
        UsdGeom.Mesh.Define(stage, f"/World/M{i}")
    UsdGeom.Sphere.Define(stage, "/World/S")
    UsdGeom.Xform.Define(stage, "/World/Grp")
    UsdGeom.Mesh.Define(stage, "/World/Grp/Deep")
    UsdShade.Material.Define(stage, "/World/Looks")
    return stage


# -- list_prims --------------------------------------------------------------

def test_list_prims_pagination_cursor():
    stage = _scene()
    total = introspection.list_prims(stage)["count"]
    page1 = introspection.list_prims(stage, max=3)
    assert page1["returned"] == 3
    assert page1["next_offset"] == 3
    seen = list(page1["prims"])
    page2 = introspection.list_prims(stage, max=3, offset=page1["next_offset"])
    seen += page2["prims"]
    # paging eventually exhausts and the cursor goes null
    last = introspection.list_prims(stage, max=total, offset=0)
    assert last["next_offset"] is None
    assert {p["path"] for p in seen} <= {p["path"] for p in last["prims"]}


def test_list_prims_is_a_filter():
    stage = _scene()
    meshes = introspection.list_prims(stage, is_a="UsdGeomMesh")
    assert meshes["count"] == 6  # 5 flat + 1 deep
    assert all(p["type"] == "Mesh" for p in meshes["prims"])
    gprims = introspection.list_prims(stage, is_a="UsdGeomGprim")["count"]
    assert gprims == 7  # 6 meshes + 1 sphere


def test_list_prims_is_a_unknown_raises():
    with pytest.raises(ToolError) as exc:
        introspection.list_prims(_scene(), is_a="NotASchema")
    assert exc.value.to_dict()["error"]["field"] == "is_a"


def test_list_prims_depth_limits_descent():
    stage = _scene()
    shallow = {p["path"] for p in introspection.list_prims(stage, under="/World", depth=1)["prims"]}
    assert "/World/Grp" in shallow
    assert "/World/Grp/Deep" not in shallow  # grandchild pruned
    deep = {p["path"] for p in introspection.list_prims(stage, under="/World")["prims"]}
    assert "/World/Grp/Deep" in deep


def test_list_prims_type_name_exact():
    res = introspection.list_prims(_scene(), type_name="Sphere")
    assert [p["path"] for p in res["prims"]] == ["/World/S"]


# -- get_prim / get_prims ----------------------------------------------------

def test_get_prim_field_projection():
    stage = _scene()
    full = introspection.get_prim(stage, "/World/M0")
    assert {"instanceable", "api_schemas", "children"} <= set(full)
    projected = introspection.get_prim(stage, "/World/M0", fields=["material_binding"])
    # only the always-on keys remain (M0 has no binding, so nothing extra)
    assert set(projected) == {"ok", "path", "type", "active"}
    assert "api_schemas" not in projected and "children" not in projected


def test_get_prim_children_capped():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Big")
    for i in range(70):
        UsdGeom.Xform.Define(stage, f"/Big/c{i}")
    info = introspection.get_prim(stage, "/Big")
    assert info["child_count"] == 70
    assert len(info["children"]) == introspection._MAX_CHILDREN
    assert info["children_truncated"] is True


def test_get_prims_batch_with_missing():
    stage = _scene()
    res = introspection.get_prims(stage, ["/World/M0", "/nope"])
    assert res["count"] == 2
    assert res["prims"][0]["ok"] is True and res["prims"][0]["path"] == "/World/M0"
    assert res["prims"][1]["ok"] is False and res["prims"][1]["path"] == "/nope"


# -- get_bounds --------------------------------------------------------------

def test_get_bounds_world_space():
    from pxr import Gf
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/W")
    b = UsdGeom.Xform.Define(stage, "/W/B")
    b.AddTranslateOp().Set(Gf.Vec3d(0, 0, 3))
    b.AddScaleOp().Set(Gf.Vec3f(15, 15, 15))
    UsdGeom.Cube.Define(stage, "/W/B/C")  # size 2 -> local extent +-1
    bounds = introspection.get_bounds(stage, "/W/B")
    assert bounds["min"] == pytest.approx([-15, -15, -12])
    assert bounds["max"] == pytest.approx([15, 15, 18])
    assert bounds["center"] == pytest.approx([0, 0, 3])
    assert bounds["size"] == pytest.approx([30, 30, 30])


def test_get_bounds_empty_for_pure_xform():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Empty")
    assert introspection.get_bounds(stage, "/Empty").get("empty") is True


# -- get_attributes ----------------------------------------------------------

def test_get_attributes_index_and_targeted():
    from pxr import Gf, Vt
    stage = Usd.Stage.CreateInMemory()
    c = UsdGeom.Cube.Define(stage, "/C")
    c.GetSizeAttr().Set(2.0)
    c.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(-1, -1, -1), Gf.Vec3f(1, 1, 1)]))

    # discovery: names + types + array length, no values
    idx = {a["name"]: a for a in introspection.get_attributes(stage, "/C")["attributes"]}
    assert "size" in idx
    assert idx["extent"]["array"] is True and idx["extent"]["len"] == 2
    assert "value" not in idx["size"]  # index carries no values

    # targeted: only the requested attribute
    got = introspection.get_attributes(stage, "/C", names=["size"])
    assert got["attributes"]["size"] == pytest.approx(2.0)
    assert "extent" not in got["attributes"]

    # missing attribute -> None, not an error
    assert introspection.get_attributes(stage, "/C", names=["nope"])["attributes"]["nope"] is None


def test_get_attributes_array_truncation_controlled_by_max_items():
    from pxr import Gf, Vt
    stage = Usd.Stage.CreateInMemory()
    c = UsdGeom.Cube.Define(stage, "/C")
    c.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(-1, -1, -1), Gf.Vec3f(1, 1, 1)]))
    full = introspection.get_attributes(stage, "/C", names=["extent"], max_items=12)
    assert len(full["attributes"]["extent"]) == 2  # short array returned in full
    trunc = introspection.get_attributes(stage, "/C", names=["extent"], max_items=1)
    assert trunc["attributes"]["extent"]["_array"] is True
    assert trunc["attributes"]["extent"]["len"] == 2


# -- scene_summary -----------------------------------------------------------

def test_scene_summary_counts():
    summary = introspection.scene_summary(_scene())
    assert summary["by_type"]["Mesh"] == 6
    assert summary["materials"] == 1
    assert summary["max_depth"] >= 3  # /World/Grp/Deep
    assert summary["total_prims"] == summary["active"]  # nothing deactivated


# -- select_changes ----------------------------------------------------------

def test_select_changes_filters_and_orders():
    dirty = {"/a": 5, "/b": 10, "/c": 2, "/d": 7}
    res = introspection.select_changes(dirty, since_seq=4, max=10, last_seq=10)
    assert [c["prim"] for c in res["changes"]] == ["/a", "/d", "/b"]  # oldest first, >4
    assert res["count"] == 3 and res["since_seq"] == 4 and res["last_seq"] == 10


def test_select_changes_caps_at_max():
    dirty = {f"/p{i}": i + 1 for i in range(10)}
    res = introspection.select_changes(dirty, since_seq=0, max=4, last_seq=10)
    assert res["count"] == 10 and res["returned"] == 4
    assert [c["seq"] for c in res["changes"]] == [1, 2, 3, 4]
