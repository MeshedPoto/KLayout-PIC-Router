"""Self-contained synthetic tests for PinRec port management, auto-detection,
GDSII roundtrip with persistent instance aliases, and precision cell snapping/alignment.

All photonic fixtures are synthesized in-memory via klayout.db / pya (zero external GDS files).
"""

from __future__ import annotations

import math
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIR = REPO_ROOT / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from pic_router.config import DEFAULT_PIN_LAYER, get_kdb
from pic_router.euler import extrude_centerline, topic_bend_points
from pic_router.ports import (
    PIN_ANCHOR_END,
    PROP_CORE_DATATYPE,
    PROP_CORE_LAYER,
    PROP_PIN_ANCHOR,
    PROP_PORT_NAME,
    Port,
    add_port_to_cell,
    auto_detect_ports,
    collect_world_ports,
    detect_port_near_point,
    get_instance_alias,
    read_cell_ports,
    remove_port_from_cell,
    set_instance_alias,
    tag_detected_ports,
)
from pic_router.snap import (
    align_instance_center_between_ports,
    align_instance_to_port_axis,
    snap_instance_nearest,
    snap_instance_relative_grid,
    snap_instance_to_port,
)

kdb = get_kdb()


def _angle_diff_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _dpoly(pts: list[tuple[float, float]]) -> object:
    return kdb.DPolygon([kdb.DPoint(float(x), float(y)) for x, y in pts])


# ---------------------------------------------------------------------------
# 100% Synthetic Generic Photonic Fixtures
# ---------------------------------------------------------------------------


def make_mmi_1x2_cell(
    layout: object,
    name: str = "MMI_1x2",
    core_layer: tuple[int, int] = (1, 0),
    port_w: float = 0.80,
) -> object:
    """Generic 1x2 MMI splitter: West port (0, 0) and East ports (120, -3.0) & (120, +3.0)."""
    cell = layout.create_cell(name)
    li = layout.layer(*core_layer)
    hw = 0.5 * port_w

    reg = kdb.Region()
    # Input access waveguide: x in [0, 20], y in [-hw, +hw]
    reg.insert(kdb.DBox(0.0, -hw, 20.0, hw).to_itype(layout.dbu))
    # Input taper: x in [20, 35], width 0.80 -> 2.20
    reg.insert(_dpoly([(20.0, -hw), (35.0, -1.1), (35.0, 1.1), (20.0, hw)]).to_itype(layout.dbu))
    # MMI body: x in [35, 85], width 10.0
    reg.insert(kdb.DBox(35.0, -5.0, 85.0, 5.0).to_itype(layout.dbu))
    # Upper & lower output tapers: x in [85, 100], centered at y = +-3.0
    for sy in (-3.0, 3.0):
        reg.insert(
            _dpoly(
                [
                    (85.0, sy - 1.1),
                    (100.0, sy - hw),
                    (100.0, sy + hw),
                    (85.0, sy + 1.1),
                ]
            ).to_itype(layout.dbu)
        )
        # Output access waveguides: x in [100, 120]
        reg.insert(kdb.DBox(100.0, sy - hw, 120.0, sy + hw).to_itype(layout.dbu))

    cell.shapes(li).insert(reg.merged())
    return cell


def make_angled_edge_coupler_cell(
    layout: object,
    name: str = "EdgeCoupler_8deg",
    core_layer: tuple[int, int] = (1, 0),
    angle_deg: float = 8.0,
    port_w: float = 0.80,
) -> tuple[object, tuple[float, float]]:
    """Generic 8-deg angled edge coupler: West port (0, 0)@180° and East facet @ +8.0°."""
    cell = layout.create_cell(name)
    li = layout.layer(*core_layer)

    bend_pts = topic_bend_points(angle_deg, 80.0, p=0.50, max_chord_err=0.0005)
    rad = math.radians(angle_deg)
    ca, sa = math.cos(rad), math.sin(rad)

    centerline: list[tuple[float, float]] = [(0.0, 0.0)]
    for bx, by in bend_pts:
        centerline.append((20.0 + bx, by))
    ex = centerline[-1][0] + 40.0 * ca
    ey = centerline[-1][1] + 40.0 * sa
    centerline.append((ex, ey))

    poly_pts = extrude_centerline(centerline, width=port_w)
    cell.shapes(li).insert(_dpoly(poly_pts).to_itype(layout.dbu))
    return cell, (ex, ey)


def make_dual_arm_4port_cell(
    layout: object,
    name: str = "DualArm_4Port",
    core_layer: tuple[int, int] = (1, 0),
    pitch: float = 250.0,
    length: float = 200.0,
    port_w: float = 0.80,
) -> object:
    """Generic 4-port directional coupler / dual-arm block with 250 um port pitch on West & East."""
    cell = layout.create_cell(name)
    li = layout.layer(*core_layer)
    hw = 0.5 * port_w
    half_p = 0.5 * pitch

    reg = kdb.Region()
    for y_arm in (-half_p, half_p):
        reg.insert(kdb.DBox(0.0, y_arm - hw, length, y_arm + hw).to_itype(layout.dbu))
    # Central non-port coupling bridge far from end faces
    reg.insert(kdb.DBox(80.0, -half_p + hw, 120.0, half_p - hw).to_itype(layout.dbu))
    cell.shapes(li).insert(reg.merged())
    return cell


def make_bend90_cell(
    layout: object,
    name: str = "Bend90_Generic",
    core_layer: tuple[int, int] = (1, 0),
    radius: float = 40.0,
    port_w: float = 0.80,
) -> object:
    """Generic 90-deg curved bend with straight stubs: West (0, 0)@180° and North (50, 50)@90°."""
    cell = layout.create_cell(name)
    li = layout.layer(*core_layer)
    bend_pts = topic_bend_points(90.0, radius, p=0.50, max_chord_err=0.001)
    pts = [(0.0, 0.0)] + [(10.0 + bx, by) for bx, by in bend_pts] + [(10.0 + radius, radius + 10.0)]
    poly_pts = extrude_centerline(pts, width=port_w)
    cell.shapes(li).insert(_dpoly(poly_pts).to_itype(layout.dbu))
    return cell


def make_nested_interior_port_cell(
    layout: object,
    name: str = "Nested_Interior_Block",
    core_layer: tuple[int, int] = (1, 0),
    port_w: float = 0.80,
) -> object:
    """Hierarchical cell with a frame subcell and an interior open waveguide port."""
    parent = layout.create_cell(name)
    child = layout.create_cell(f"{name}_SubWG")
    li = layout.layer(*core_layer)
    hw = 0.5 * port_w

    # Child owns an interior waveguide stub terminating at x = 40 inside the parent frame
    child.shapes(li).insert(kdb.DBox(0.0, -hw, 40.0, hw))
    parent.insert(kdb.DCellInstArray(child.cell_index(), kdb.DTrans(0.0, 0.0)))

    # Parent adds an outer C-shaped frame far from the interior port face at (40, 0)
    frame = kdb.Region()
    frame.insert(kdb.DBox(-10.0, -30.0, 0.0, 30.0).to_itype(layout.dbu))
    frame.insert(kdb.DBox(-10.0, 22.0, 80.0, 30.0).to_itype(layout.dbu))
    frame.insert(kdb.DBox(-10.0, -30.0, 80.0, -22.0).to_itype(layout.dbu))
    parent.shapes(li).insert(frame.merged())
    return parent


# ---------------------------------------------------------------------------
# 1. Auto-Detection & Point-Near Detection
# ---------------------------------------------------------------------------


def test_auto_detect_ports_across_all_synthetic_fixtures():
    ly = kdb.Layout()
    ly.dbu = 0.001

    # 1. Generic 1x2 MMI splitter
    mmi = make_mmi_1x2_cell(ly)
    mmi_ports = auto_detect_ports(ly, mmi, core_layers=((1, 0),))
    assert len(mmi_ports) == 3
    # Deterministic sort: leftmost (0, 0) first, then (120, -3), (120, +3)
    assert math.hypot(mmi_ports[0].x - 0.0, mmi_ports[0].y - 0.0) < 1e-3
    assert _angle_diff_deg(mmi_ports[0].angle_deg, 180.0) < 0.1
    assert math.isclose(mmi_ports[0].width, 0.80, abs_tol=2e-3)
    assert math.hypot(mmi_ports[1].x - 120.0, mmi_ports[1].y + 3.0) < 1e-3
    assert _angle_diff_deg(mmi_ports[1].angle_deg, 0.0) < 0.1
    assert math.hypot(mmi_ports[2].x - 120.0, mmi_ports[2].y - 3.0) < 1e-3
    assert _angle_diff_deg(mmi_ports[2].angle_deg, 0.0) < 0.1

    # 2. Generic 8-deg angled edge coupler
    ec, (ex, ey) = make_angled_edge_coupler_cell(ly, angle_deg=8.0, port_w=0.80)
    ec_ports = auto_detect_ports(ly, ec, core_layers=((1, 0),))
    assert len(ec_ports) == 2
    assert math.hypot(ec_ports[0].x - 0.0, ec_ports[0].y - 0.0) < 1e-3
    assert _angle_diff_deg(ec_ports[0].angle_deg, 180.0) < 0.1
    assert math.hypot(ec_ports[1].x - ex, ec_ports[1].y - ey) < 5e-3
    assert _angle_diff_deg(ec_ports[1].angle_deg, 8.0) < 0.15
    assert math.isclose(ec_ports[1].width, 0.80, abs_tol=5e-3)

    # 3. Generic 4-port dual-arm block (250 um pitch on West & East)
    dual = make_dual_arm_4port_cell(ly, pitch=250.0, length=200.0, port_w=0.80)
    dual_ports = auto_detect_ports(ly, dual, core_layers=((1, 0),))
    assert len(dual_ports) == 4
    west = [p for p in dual_ports if _angle_diff_deg(p.angle_deg, 180.0) < 1.0]
    east = [p for p in dual_ports if _angle_diff_deg(p.angle_deg, 0.0) < 1.0]
    assert len(west) == 2 and len(east) == 2
    assert math.isclose(abs(west[1].y - west[0].y), 250.0, abs_tol=1e-3)
    assert math.isclose(abs(east[1].y - east[0].y), 250.0, abs_tol=1e-3)

    # 4. Generic 90-deg bend & nested interior-port cell
    bend = make_bend90_cell(ly, radius=40.0, port_w=0.80)
    bend_ports = auto_detect_ports(ly, bend, core_layers=((1, 0),))
    assert len(bend_ports) == 2
    assert _angle_diff_deg(bend_ports[0].angle_deg, 180.0) < 0.1
    assert _angle_diff_deg(bend_ports[1].angle_deg, 90.0) < 0.1
    assert math.hypot(bend_ports[1].x - 50.0, bend_ports[1].y - 50.0) < 5e-3

    nested = make_nested_interior_port_cell(ly, port_w=0.80)
    nested_ports = auto_detect_ports(ly, nested, core_layers=((1, 0),), include_hierarchy=True)
    assert any(
        math.hypot(p.x - 40.0, p.y - 0.0) < 1e-2 and _angle_diff_deg(p.angle_deg, 0.0) < 0.5
        for p in nested_ports
    )


def test_detect_port_near_point():
    ly = kdb.Layout()
    ly.dbu = 0.001
    mmi = make_mmi_1x2_cell(ly)

    hit = detect_port_near_point(ly, mmi, 120.5, 3.2, search_radius=3.0, core_layers=((1, 0),))
    assert hit is not None
    assert math.hypot(hit.x - 120.0, hit.y - 3.0) < 1e-3
    assert _angle_diff_deg(hit.angle_deg, 0.0) < 0.1
    assert math.isclose(hit.width, 0.80, abs_tol=2e-3)

    miss = detect_port_near_point(ly, mmi, 500.0, 500.0, search_radius=3.0, core_layers=((1, 0),))
    assert miss is None


# ---------------------------------------------------------------------------
# 2. tag_detected_ports (overwrite=False Legacy Upgrade & overwrite=True Refresh)
# ---------------------------------------------------------------------------


def test_tag_detected_ports_legacy_upgrade_and_full_refresh():
    ly = kdb.Layout()
    ly.dbu = 0.001
    mmi = make_mmi_1x2_cell(ly, port_w=0.80)
    li_core = ly.find_layer(1, 0)
    li_pin = ly.layer(*DEFAULT_PIN_LAYER)
    core_bb = mmi.bbox_per_layer(li_core)

    # Stamp a LEGACY outward-protruding PinRec at West port (0, 0) -> (-0.5, 0) without PROP_PIN_ANCHOR
    legacy_path = kdb.DPath([kdb.DPoint(0.0, 0.0), kdb.DPoint(-0.5, 0.0)], 0.80)
    sh = mmi.shapes(li_pin).insert(legacy_path)
    sh.set_property(PROP_CORE_LAYER, 1)
    sh.set_property(PROP_CORE_DATATYPE, 0)
    sh.set_property(PROP_PORT_NAME, "in_west")
    mmi.shapes(li_pin).insert(kdb.DText("in_west", kdb.DTrans(0.0, 0.0)))
    assert mmi.bbox_per_layer(li_pin).left < core_bb.left

    # overwrite=False: upgrades legacy outward marker to flush-inside AND adds missing East ports!
    ports_inc = tag_detected_ports(ly, mmi, core_layers=((1, 0),), overwrite=False)
    assert len(ports_inc) == 3
    names_inc = {p.name for p in ports_inc}
    assert "in_west" in names_inc
    # Verify legacy protrusion is gone (flush inside core bbox!)
    pin_bb = mmi.bbox_per_layer(li_pin)
    assert pin_bb.left >= core_bb.left and pin_bb.right <= core_bb.right
    for pin_sh in mmi.shapes(li_pin).each():
        if pin_sh.is_path():
            assert str(pin_sh.property(PROP_PIN_ANCHOR)) == PIN_ANCHOR_END

    # Now stamp a stale/wrong-width port + an orphan bogus port, plus rename an East port to custom name
    add_port_to_cell(ly, mmi, "out_upper", 120.0, 3.0, 0.0, width=0.55, core_layer=(1, 0))
    add_port_to_cell(ly, mmi, "stale_ghost", 60.0, 20.0, 90.0, width=0.80, core_layer=(1, 0))
    # Also simulate stale width on in_west (0.55 instead of true 0.80)
    add_port_to_cell(ly, mmi, "in_west", 0.0, 0.0, 180.0, width=0.55, core_layer=(1, 0))

    # overwrite=True: clears stale_ghost, updates widths back to 0.80, AND preserves custom user names!
    ports_ref = tag_detected_ports(ly, mmi, core_layers=((1, 0),), overwrite=True)
    assert len(ports_ref) == 3
    by_name = {p.name: p for p in ports_ref}
    assert "stale_ghost" not in by_name
    assert "in_west" in by_name
    assert "out_upper" in by_name
    assert math.isclose(by_name["in_west"].width, 0.80, abs_tol=2e-3)
    assert math.isclose(by_name["out_upper"].width, 0.80, abs_tol=2e-3)

    # remove_port_from_cell cleanly deletes a single port
    assert remove_port_from_cell(ly, mmi, "out_upper") is True
    assert remove_port_from_cell(ly, mmi, "out_upper") is False
    assert len(read_cell_ports(ly, mmi)) == 2


# ---------------------------------------------------------------------------
# 3. Repeated Instances, Persistent Aliases, & GDSII Roundtrip
# ---------------------------------------------------------------------------


def test_repeated_instances_gdsii_roundtrip_and_persistent_aliases(tmp_path: Path):
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("CHIP_TOP")
    mmi = make_mmi_1x2_cell(ly, name="MMI_UNIT")
    tag_detected_ports(ly, mmi, core_layers=((1, 0),), overwrite=True)

    inst1 = top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(0.0, 0.0)))
    inst2 = top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(2, False, 400.0, 0.0)))  # R180
    # Unaliased repeated instance (auto-assigned deterministic "<cell>#<idx>")
    top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(0.0, -120.0)))
    # Regular 2x2 array of instances
    inst_arr = top.insert(
        kdb.DCellInstArray(
            mmi.cell_index(),
            kdb.DTrans(0.0, 200.0),
            kdb.DVector(200.0, 0.0),
            kdb.DVector(0.0, 100.0),
            2,
            2,
        )
    )
    set_instance_alias(inst1, "SPLITTER_IN")
    set_instance_alias(inst2, "COMBINER_OUT")
    set_instance_alias(inst_arr, "ARR_MMI")
    assert get_instance_alias(inst1) == "SPLITTER_IN"
    assert get_instance_alias(inst2) == "COMBINER_OUT"

    gds_path = tmp_path / "synthetic_chip_roundtrip.gds"
    ly.write(str(gds_path))

    ly2 = kdb.Layout()
    ly2.read(str(gds_path))
    top2 = ly2.cell("CHIP_TOP")
    assert top2 is not None

    world_ports = collect_world_ports(ly2, top2, pin_layer=DEFAULT_PIN_LAYER)
    # 3 single MMIs (9 ports) + 2x2 array (12 ports) = 21 ports
    assert len(world_ports) == 21
    full_names = {p.full_name for p in world_ports}
    assert len(full_names) == 21  # strictly unique across all repeated instances!
    assert "SPLITTER_IN:o1" in full_names
    assert "COMBINER_OUT:o1" in full_names
    assert "MMI_UNIT#1:o1" in full_names
    for ia in (0, 1):
        for ib in (0, 1):
            assert f"ARR_MMI[{ia},{ib}]:o1" in full_names

    # Verify all 4 array element o1 world coordinates survived GDSII roundtrip
    arr_o1_coords = {
        (round(p.x, 3), round(p.y, 3))
        for p in world_ports
        if p.inst_id.startswith("ARR_MMI[") and p.name == "o1"
    }
    assert arr_o1_coords == {(0.0, 200.0), (200.0, 200.0), (0.0, 300.0), (200.0, 300.0)}

    # Verify R180 transform on COMBINER_OUT flipped West port (0,0)@180° -> (400,0)@0°
    p_comb_in = next(p for p in world_ports if p.full_name == "COMBINER_OUT:o1")
    assert math.hypot(p_comb_in.x - 400.0, p_comb_in.y - 0.0) < 1e-3
    assert _angle_diff_deg(p_comb_in.angle_deg, 0.0) < 0.1
    assert math.isclose(p_comb_in.width, 0.80, abs_tol=2e-3)


# ---------------------------------------------------------------------------
# 4. Precision Snapping (0/90/180/270°, Mirrored, Arbitrary 8°, Nearest)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("init_rot", [0.0, 90.0, 180.0, 270.0])
@pytest.mark.parametrize("mirror", [False, True])
def test_snap_instance_to_port_rotations_and_mirroring(init_rot: float, mirror: bool):
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("SNAP_ROT")
    mmi = make_mmi_1x2_cell(ly)
    tag_detected_ports(ly, mmi, core_layers=((1, 0),), overwrite=True)

    inst_fixed = top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DCplxTrans(1.0, 0.0, False, kdb.DVector(0.0, 0.0))))
    inst_mov = top.insert(
        kdb.DCellInstArray(
            mmi.cell_index(),
            kdb.DCplxTrans(1.0, init_rot, mirror, kdb.DVector(250.0, 120.0)),
        )
    )
    set_instance_alias(inst_fixed, "FIXED")
    set_instance_alias(inst_mov, "MOV")

    ports = collect_world_ports(ly, top)
    tgt = next(p for p in ports if p.full_name == "FIXED:o3")  # (120, +3) @ 0°
    src = next(p for p in ports if p.full_name == "MOV:o1")  # West port

    res = snap_instance_to_port(inst_mov, src, tgt, allow_rotate=True, dbu=ly.dbu)
    assert res["residual_err_um"] < 0.002
    assert res["angle_err_deg"] < 0.01

    # Re-collect world ports and confirm exact face-to-face mating
    ports_after = collect_world_ports(ly, top)
    src_after = next(p for p in ports_after if p.full_name == "MOV:o1")
    assert math.hypot(src_after.x - tgt.x, src_after.y - tgt.y) < 0.002
    assert _angle_diff_deg(src_after.angle_deg, (tgt.angle_deg + 180.0) % 360.0) < 0.05


def test_snap_instance_arbitrary_8deg_angle_and_no_rotate():
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("SNAP_8DEG")
    ec, _ = make_angled_edge_coupler_cell(ly, angle_deg=8.0, port_w=0.80)
    tag_detected_ports(ly, ec, core_layers=((1, 0),), overwrite=True)

    # Fixed coupler 1 placed at origin (angled port faces +8.0 deg)
    inst1 = top.insert(kdb.DCellInstArray(ec.cell_index(), kdb.DCplxTrans(1.0, 0.0, False, kdb.DVector(0.0, 0.0))))
    # Movable coupler 2 placed rotated 37 deg away
    inst2 = top.insert(kdb.DCellInstArray(ec.cell_index(), kdb.DCplxTrans(1.0, 37.0, False, kdb.DVector(200.0, 80.0))))
    set_instance_alias(inst1, "EC_FIXED")
    set_instance_alias(inst2, "EC_MOV")

    ports = collect_world_ports(ly, top)
    tgt_ang = next(p for p in ports if p.full_name == "EC_FIXED:o2")
    src_ang = next(p for p in ports if p.full_name == "EC_MOV:o2")

    res = snap_instance_to_port(inst2, src_ang, tgt_ang, allow_rotate=True, dbu=ly.dbu)
    assert res["residual_err_um"] < 0.003
    assert res["angle_err_deg"] < 0.02

    ports_after = collect_world_ports(ly, top)
    mated = next(p for p in ports_after if p.full_name == "EC_MOV:o2")
    assert math.hypot(mated.x - tgt_ang.x, mated.y - tgt_ang.y) < 0.003
    assert _angle_diff_deg(mated.angle_deg, (tgt_ang.angle_deg + 180.0) % 360.0) < 0.15

    # Pure translation (allow_rotate=False) preserves instance rotation
    mmi = make_mmi_1x2_cell(ly)
    tag_detected_ports(ly, mmi, core_layers=((1, 0),), overwrite=True)
    im1 = top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(0.0, 300.0)))
    im2 = top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(140.0, 315.0)))
    set_instance_alias(im1, "M1")
    set_instance_alias(im2, "M2")
    p_all = collect_world_ports(ly, top)
    p_t = next(p for p in p_all if p.full_name == "M1:o3")
    p_s = next(p for p in p_all if p.full_name == "M2:o1")
    r_tr = snap_instance_to_port(im2, p_s, p_t, allow_rotate=False, dbu=ly.dbu)
    assert r_tr["dtheta"] == 0.0
    assert r_tr["residual_err_um"] < 0.002


def test_snap_instance_nearest_tagged_and_untagged_fallback():
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("SNAP_NEAREST")
    # Deliberately untagged MMI cells to verify in-memory auto-detect fallback without mutating cells!
    mmi = make_mmi_1x2_cell(ly)
    assert read_cell_ports(ly, mmi) == []

    top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(0.0, 0.0)))
    inst_mov = top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(128.0, 5.5)))

    # Too-small max_dist returns None
    assert snap_instance_nearest(ly, top, inst_mov, max_dist=1.0, allow_rotate=False) is None

    res = snap_instance_nearest(ly, top, inst_mov, max_dist=25.0, allow_rotate=False)
    assert res is not None
    assert res["residual_err_um"] < 0.002
    assert res["angle_err_deg"] < 0.1
    # Must NOT mutate cell with PinRec shapes during fallback
    assert read_cell_ports(ly, mmi) == []
    # Movable MMI's West port (0,0) snapped onto fixed MMI's upper East port (120, +3)
    assert math.isclose(inst_mov.dcplx_trans.disp.x, 120.0, abs_tol=2e-3)
    assert math.isclose(inst_mov.dcplx_trans.disp.y, 3.0, abs_tol=2e-3)


# ---------------------------------------------------------------------------
# 5. Axis Alignment, 2->2 Symmetric Center Pair, & Relative Integer Grid
# ---------------------------------------------------------------------------


def test_align_axis_center_pair_and_relative_grid():
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("ALIGN_TOP")

    mmi = make_mmi_1x2_cell(ly)
    dual = make_dual_arm_4port_cell(ly, pitch=250.0, length=200.0)
    tag_detected_ports(ly, mmi, core_layers=((1, 0),), overwrite=True)
    tag_detected_ports(ly, dual, core_layers=((1, 0),), overwrite=True)

    # Place MMI shifted off-center in Y (+37.423 um) and fractional X (+260.371 um)
    inst_dual = top.insert(kdb.DCellInstArray(dual.cell_index(), kdb.DTrans(300.0, 0.0)))
    inst_mmi = top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(40.371, 37.423)))
    set_instance_alias(inst_dual, "DUAL")
    set_instance_alias(inst_mmi, "MMI")

    ports = collect_world_ports(ly, top)
    mmi_o2 = next(p for p in ports if p.full_name == "MMI:o2")
    mmi_o3 = next(p for p in ports if p.full_name == "MMI:o3")
    dual_w1 = next(p for p in ports if p.full_name == "DUAL:o1")  # (300, -125)
    dual_w2 = next(p for p in ports if p.full_name == "DUAL:o2")  # (300, +125)

    # 1. Center Pair (2->2) symmetrically in Y (auto detects vertical span)
    ctr = align_instance_center_between_ports(
        inst_mmi, mmi_o2, mmi_o3, dual_w1, dual_w2, axis="auto", dbu=ly.dbu
    )
    assert ctr["axis"] == "y"
    assert ctr["dx"] == 0.0
    assert ctr["residual_err_um"] < 0.002
    assert math.isclose(ctr["offset_pair_1_um"], ctr["offset_pair_2_um"], abs_tol=2e-3)
    assert math.isclose(ctr["offset_pair_1_um"], 122.0, abs_tol=2e-3)

    # 2. Relative 1 um grid snap eliminates fractional X gap while preserving aligned Y
    ports = collect_world_ports(ly, top)
    mmi_o3_now = next(p for p in ports if p.full_name == "MMI:o3")
    dual_w2_now = next(p for p in ports if p.full_name == "DUAL:o2")
    grid_res = snap_instance_relative_grid(inst_mmi, mmi_o3_now, dual_w2_now, step_um=1.0, dbu=ly.dbu)
    assert grid_res["moved"] is True
    assert abs(grid_res["gap_x_um"] - round(grid_res["gap_x_um"])) < 1e-3
    assert abs(grid_res["gap_y_um"] - round(grid_res["gap_y_um"])) < 1e-3

    # 3. Single-axis Align Y and Align X
    ports = collect_world_ports(ly, top)
    mmi_o3_b = next(p for p in ports if p.full_name == "MMI:o3")
    dual_w2_b = next(p for p in ports if p.full_name == "DUAL:o2")
    x_before = mmi_o3_b.x

    ay = align_instance_to_port_axis(inst_mmi, mmi_o3_b, dual_w2_b, axis="y", dbu=ly.dbu)
    assert ay["dx"] == 0.0
    assert ay["residual_err_um"] < 0.002
    assert math.isclose(ay["new_port_pos"][0], x_before, abs_tol=1e-6)
    assert math.isclose(ay["new_port_pos"][1], dual_w2_b.y, abs_tol=2e-3)

    ports = collect_world_ports(ly, top)
    mmi_o3_c = next(p for p in ports if p.full_name == "MMI:o3")
    y_before = mmi_o3_c.y
    ax = align_instance_to_port_axis(inst_mmi, mmi_o3_c, dual_w2_b, axis="x", dbu=ly.dbu)
    assert ax["dy"] == 0.0
    assert ax["residual_err_um"] < 0.002
    assert math.isclose(ax["new_port_pos"][0], dual_w2_b.x, abs_tol=2e-3)
    assert math.isclose(ax["new_port_pos"][1], y_before, abs_tol=1e-6)
