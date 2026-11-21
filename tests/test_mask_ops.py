"""Self-contained synthetic tests for mask-prep operations:
  1. Single- & multi-layer trench generation (solid, hollow sleeve, flush port trim, PCell update)
  2. Length-preserving CD linewidth bias (+120 nm, +70 nm, -80 nm, anisotropic X/Y)
  3. Automatic PinRec (90/0) width synchronization on in-place CD bias
  4. Parametric PCell CD bias & repeated-cell deduplication (zero double-biasing)

All fixtures are synthesized in-memory via klayout.db / pya (zero external GDS files).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIR = REPO_ROOT / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from pic_router.config import DEFAULT_PIN_LAYER, get_cross_section, get_kdb
from pic_router.euler import extrude_centerline, topic_bend_points
from pic_router.mask_ops import (
    add_trench_to_cell,
    add_trench_to_pcell_inst,
    bias_cell_linewidth,
    bias_pcell_inst_linewidth,
    bias_region_preserve_length,
    build_trench_region,
    collect_owning_static_cells,
    detect_open_ports_on_region,
)
from pic_router.pcells import instantiate_euler_waveguide, instantiate_topic_bend
from pic_router.ports import Port, auto_detect_ports, read_cell_ports, tag_detected_ports

kdb = get_kdb()


def _dpoly(pts: list[tuple[float, float]]) -> object:
    return kdb.DPolygon([kdb.DPoint(float(x), float(y)) for x, y in pts])


def _make_mmi_1x2(layout: object, name: str = "MMI_1x2", port_w: float = 0.80) -> object:
    cell = layout.create_cell(name)
    li = layout.layer(1, 0)
    hw = 0.5 * port_w
    reg = kdb.Region()
    reg.insert(kdb.DBox(0.0, -hw, 20.0, hw).to_itype(layout.dbu))
    reg.insert(_dpoly([(20.0, -hw), (35.0, -1.1), (35.0, 1.1), (20.0, hw)]).to_itype(layout.dbu))
    reg.insert(kdb.DBox(35.0, -5.0, 85.0, 5.0).to_itype(layout.dbu))
    for sy in (-3.0, 3.0):
        reg.insert(
            _dpoly([(85.0, sy - 1.1), (100.0, sy - hw), (100.0, sy + hw), (85.0, sy + 1.1)]).to_itype(layout.dbu)
        )
        reg.insert(kdb.DBox(100.0, sy - hw, 120.0, sy + hw).to_itype(layout.dbu))
    cell.shapes(li).insert(reg.merged())
    return cell


def _make_topic_bend90(
    layout: object,
    name: str = "TOPIC_Bend90",
    r_eff: float = 40.0,
    lead: float = 15.0,
    port_w: float = 0.80,
) -> tuple[object, list[tuple[float, float]]]:
    """90-deg TOPIC bend with horizontal lead [0, lead] and vertical lead up to y = r_eff + lead."""
    cell = layout.create_cell(name)
    li = layout.layer(1, 0)
    bpts = topic_bend_points(90.0, r_eff, p=0.50, max_chord_err=0.0005)
    cl = [(0.0, 0.0)] + [(lead + x, y) for x, y in bpts] + [(lead + r_eff, r_eff + lead)]
    poly_pts = extrude_centerline(cl, width=port_w)
    cell.shapes(li).insert(_dpoly(poly_pts).to_itype(layout.dbu))
    return cell, cl


def _make_angled_8deg_coupler(
    layout: object, name: str = "EC_8deg", port_w: float = 0.80
) -> tuple[object, tuple[float, float]]:
    cell = layout.create_cell(name)
    li = layout.layer(1, 0)
    bpts = topic_bend_points(8.0, 80.0, p=0.50, max_chord_err=0.0005)
    rad = math.radians(8.0)
    cl = [(0.0, 0.0)] + [(20.0 + x, y) for x, y in bpts]
    ex = cl[-1][0] + 40.0 * math.cos(rad)
    ey = cl[-1][1] + 40.0 * math.sin(rad)
    cl.append((ex, ey))
    cell.shapes(li).insert(_dpoly(extrude_centerline(cl, width=port_w)).to_itype(layout.dbu))
    return cell, (ex, ey)


def _measure_normal_width(
    region: object,
    cx: float,
    cy: float,
    angle_deg: float,
    dbu: float = 0.001,
    half_scan: float = 5.0,
) -> float:
    """Measure exact local waveguide width perpendicular to tangent ``angle_deg`` at ``(cx, cy)``."""
    rad = math.radians(angle_deg)
    tx, ty = math.cos(rad), math.sin(rad)
    nx, ny = -ty, tx
    eps_t = 0.004  # thin 8 nm slice along tangent
    pts = [
        (cx - eps_t * tx - half_scan * nx, cy - eps_t * ty - half_scan * ny),
        (cx + eps_t * tx - half_scan * nx, cy + eps_t * ty - half_scan * ny),
        (cx + eps_t * tx + half_scan * nx, cy + eps_t * ty + half_scan * ny),
        (cx - eps_t * tx + half_scan * nx, cy - eps_t * ty + half_scan * ny),
    ]
    probe = kdb.Region(_dpoly(pts).to_itype(dbu))
    hit = (region & probe).merged()
    assert not hit.is_empty(), f"Probe at ({cx:.3f}, {cy:.3f}) missed region"
    proj_vals: list[float] = []
    for poly in hit.each():
        for pt in poly.each_point_hull():
            v = (pt.x * dbu - cx) * nx + (pt.y * dbu - cy) * ny
            proj_vals.append(v)
    return max(proj_vals) - min(proj_vals)


# ---------------------------------------------------------------------------
# 1. Single- & Multi-Layer Trench Generation (Solid, Hollow, Flush Trim, PCells)
# ---------------------------------------------------------------------------


def test_add_trench_single_and_multilayer_solid_hollow_and_flush_trim():
    ly = kdb.Layout()
    ly.dbu = 0.001
    mmi = _make_mmi_1x2(ly, port_w=0.80)
    ec, (ex, ey) = _make_angled_8deg_coupler(ly, port_w=0.80)
    tag_detected_ports(ly, mmi, core_layers=((1, 0),), overwrite=True)
    tag_detected_ports(ly, ec, core_layers=((1, 0),), overwrite=True)

    # Multi-layer solid trench on 1x2 MMI (1/1=10 um, 2/1=25 um) trimmed flush at ports
    res = add_trench_to_cell(
        ly,
        mmi,
        core_layers=((1, 0),),
        trench_specs=((1, 1, 10.0), (2, 1, 25.0)),
        nominal_core_width=0.80,
        width_mode="total_width",
        trim_at_ports=True,
        hollow_minus_core=False,
    )
    assert res["ok"] is True
    assert res["ports_trimmed"] == 3
    assert len(res["layers_written"]) == 2

    li_core = ly.find_layer(1, 0)
    li_t1 = ly.find_layer(1, 1)
    li_t2 = ly.find_layer(2, 1)
    core_reg = kdb.Region(mmi.shapes(li_core)).merged()
    t1_reg = kdb.Region(mmi.shapes(li_t1)).merged()
    t2_reg = kdb.Region(mmi.shapes(li_t2)).merged()

    # Solid trench encloses core; outer trench encloses inner trench
    assert (core_reg - t1_reg).is_empty()
    assert (t1_reg - t2_reg).is_empty()

    # Exact flush trim at West (x=0) and East (x=120) ports: ZERO protrusion!
    core_bb = core_reg.bbox()
    assert t1_reg.bbox().left == core_bb.left
    assert t1_reg.bbox().right == core_bb.right
    assert t2_reg.bbox().left == core_bb.left
    assert t2_reg.bbox().right == core_bb.right

    # Measure trench widths on the input access waveguide at x = 10 um
    w_t1 = _measure_normal_width(t1_reg, 10.0, 0.0, 0.0, dbu=ly.dbu, half_scan=15.0)
    w_t2 = _measure_normal_width(t2_reg, 10.0, 0.0, 0.0, dbu=ly.dbu, half_scan=20.0)
    assert abs(w_t1 - 10.0) < 0.01
    assert abs(w_t2 - 25.0) < 0.01

    # Hollow sleeve mode on 8-deg angled coupler (side_margin = 4.6 um -> total 10.0 um minus core)
    res_h = add_trench_to_cell(
        ly,
        ec,
        core_layers=((1, 0),),
        trench_specs=((1, 1, 4.6),),
        nominal_core_width=0.80,
        width_mode="side_margin",
        trim_at_ports=True,
        hollow_minus_core=True,
    )
    assert res_h["ok"] is True
    ec_core = kdb.Region(ec.shapes(li_core)).merged()
    ec_trench = kdb.Region(ec.shapes(ly.find_layer(1, 1))).merged()
    assert not ec_trench.is_empty()
    assert (ec_trench & ec_core).is_empty(), "Hollow sleeve must subtract core completely"

    # Verify zero protrusion past the +8 deg angled facet!
    rad = math.radians(8.0)
    ux, uy = math.cos(rad), math.sin(rad)
    tx, ty = -uy, ux
    out_probe = kdb.Region(
        _dpoly(
            [
                (ex + 0.02 * ux - 6.0 * tx, ey + 0.02 * uy - 6.0 * ty),
                (ex + 5.0 * ux - 6.0 * tx, ey + 5.0 * uy - 6.0 * ty),
                (ex + 5.0 * ux + 6.0 * tx, ey + 5.0 * uy + 6.0 * ty),
                (ex + 0.02 * ux + 6.0 * tx, ey + 0.02 * uy + 6.0 * ty),
            ]
        ).to_itype(ly.dbu)
    )
    assert (ec_trench & out_probe).is_empty(), "Trench protruded past 8-deg angled port face"

    # Untrimmed build_trench_region protrudes beyond West port by margin
    untrimmed = build_trench_region(core_reg, margin_um=4.6, dbu=ly.dbu, ports=(), trim_at_ports=False)
    assert untrimmed.bbox().left < core_bb.left - 4500


def test_add_trench_to_pcell_inst():
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("PCELL_TRENCH")
    cs_core_only = get_cross_section("Core Only (Single Layer)")

    inst_wg = instantiate_euler_waveguide(
        ly, top, [(0.0, 0.0), (80.0, 0.0), (80.0, 80.0)], cs_core_only, bend_type="topic"
    )
    inst_tb = instantiate_topic_bend(ly, top, angle_deg=90.0, cross_section=cs_core_only, r_eff=30.0)

    assert add_trench_to_pcell_inst(ly, inst_wg, ((1, 1, 10.0), (2, 1, 25.0))) is True
    assert add_trench_to_pcell_inst(ly, inst_tb, ((1, 1, 4.6),), nominal_core_width=0.50, width_mode="side_margin") is True

    wg_params = inst_wg.pcell_parameters_by_name()
    assert math.isclose(float(wg_params["trench_width"]), 10.0, abs_tol=1e-6)
    extras = json.loads(str(wg_params["extra_layers_json"]))
    assert extras == [[2, 1, 25.0, 0.0, "trench"]]
    assert inst_wg.cell.shapes(ly.find_layer(1, 1)).size() > 0
    assert inst_wg.cell.shapes(ly.find_layer(2, 1)).size() > 0

    tb_params = inst_tb.pcell_parameters_by_name()
    assert math.isclose(float(tb_params["trench_width"]), 0.50 + 2.0 * 4.6, abs_tol=1e-6)
    assert inst_tb.cell.shapes(ly.find_layer(1, 1)).size() > 0


# ---------------------------------------------------------------------------
# 2. Length-Preserving CD Linewidth Bias (+120 nm, +70 nm, -80 nm, Anisotropic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bias_um", [0.120, 0.070, -0.080])
def test_bias_region_and_cell_preserve_length_on_bend_h_v_45deg(bias_um: float):
    ly = kdb.Layout()
    ly.dbu = 0.001
    w0 = 0.80
    r_eff = 40.0
    lead = 15.0
    bend_cell, cl = _make_topic_bend90(ly, r_eff=r_eff, lead=lead, port_w=w0)
    li_core = ly.find_layer(1, 0)
    orig_reg = kdb.Region(bend_cell.shapes(li_core)).merged()
    orig_bb = orig_reg.bbox()

    # Open ports at (0, 0)@180° and (55, 55)@90°
    open_ports = detect_open_ports_on_region(orig_reg, core_layer=(1, 0), dbu=ly.dbu)
    assert len(open_ports) == 2

    biased = bias_region_preserve_length(
        orig_reg, bias_um=bias_um, dbu=ly.dbu, ports=open_ports, preserve_port_faces=True
    )
    assert not biased.is_empty()
    exp_w = w0 + bias_um

    # 1. Exact width on Horizontal lead (x=7.5, y=0), Vertical lead (x=55, y=47.5), AND 45° curved midpoint!
    w_h = _measure_normal_width(biased, 7.5, 0.0, 0.0, dbu=ly.dbu)
    w_v = _measure_normal_width(biased, lead + r_eff, r_eff + 7.5, 90.0, dbu=ly.dbu)
    mid_pt = cl[len(cl) // 2]
    w_45 = _measure_normal_width(biased, mid_pt[0], mid_pt[1], 45.0, dbu=ly.dbu)

    assert abs(w_h - exp_w) <= 0.003, f"H width {w_h:.4f} != {exp_w:.4f}"
    assert abs(w_v - exp_w) <= 0.003, f"V width {w_v:.4f} != {exp_w:.4f}"
    assert abs(w_45 - exp_w) <= 0.004, f"45deg width {w_45:.4f} != {exp_w:.4f}"

    # 2. STRICT 0.000 nm port-face / centerline length shift!
    # West port face is at x = 0 (orig_bb.left == 0), North port face is at y = 55 (orig_bb.top == 55000).
    assert biased.bbox().left == orig_bb.left, "West port face shifted!"
    assert biased.bbox().top == orig_bb.top, "North port face shifted!"

    # Also verify non-destructive output_layer_map on bias_cell_linewidth
    r_nd = bias_cell_linewidth(
        ly,
        bend_cell,
        core_layers=((1, 0),),
        bias_um=bias_um,
        preserve_port_faces=True,
        output_layer_map={(1, 0): (1, 10)},
    )
    assert r_nd["ok"] is True
    # Original layer (1, 0) untouched
    assert kdb.Region(bend_cell.shapes(li_core)).merged().area() == orig_reg.area()
    li_out = ly.find_layer(1, 10)
    out_reg = kdb.Region(bend_cell.shapes(li_out)).merged()
    assert out_reg.bbox().left == orig_bb.left
    assert out_reg.bbox().top == orig_bb.top


def test_anisotropic_cd_bias_and_pinrec_width_sync():
    ly = kdb.Layout()
    ly.dbu = 0.001
    w0 = 0.80
    bend_cell, _ = _make_topic_bend90(ly, r_eff=40.0, lead=15.0, port_w=w0)
    tag_detected_ports(ly, bend_cell, core_layers=((1, 0),), overwrite=True)

    # Anisotropic: vertical WG (X-bias) +100 nm, horizontal WG (Y-bias) +40 nm
    r_aniso = bias_cell_linewidth(
        ly,
        bend_cell,
        core_layers=((1, 0),),
        bias_um=0.0,
        bias_x_um=0.100,
        bias_y_um=0.040,
        preserve_port_faces=True,
        output_layer_map=None,  # in-place -> auto-syncs PinRec widths!
    )
    assert r_aniso["ok"] is True

    li_core = ly.find_layer(1, 0)
    reg = kdb.Region(bend_cell.shapes(li_core)).merged()
    w_h = _measure_normal_width(reg, 7.5, 0.0, 0.0, dbu=ly.dbu)
    w_v = _measure_normal_width(reg, 55.0, 47.5, 90.0, dbu=ly.dbu)
    assert abs(w_h - (w0 + 0.040)) <= 0.003
    assert abs(w_v - (w0 + 0.100)) <= 0.003
    assert reg.bbox().left == 0
    assert reg.bbox().top == 55000

    # Verify automatic PinRec (90/0) width sync matched anisotropic directions!
    pins = read_cell_ports(ly, bend_cell, pin_layer=DEFAULT_PIN_LAYER)
    assert len(pins) == 2
    p_west = next(p for p in pins if abs(p.x) < 1e-2)
    p_north = next(p for p in pins if abs(p.y - 55.0) < 1e-2)
    assert math.isclose(p_west.width, w0 + 0.040, abs_tol=2e-3)
    assert math.isclose(p_north.width, w0 + 0.100, abs_tol=2e-3)

    # Isotropic in-place PinRec width sync on generic 1x2 MMI (+120 nm)
    mmi = _make_mmi_1x2(ly, port_w=0.80)
    tag_detected_ports(ly, mmi, core_layers=((1, 0),), overwrite=True)
    r_mmi = bias_cell_linewidth(ly, mmi, core_layers=((1, 0),), bias_um=0.120, output_layer_map=None)
    assert r_mmi["ok"] is True
    mmi_pins = read_cell_ports(ly, mmi, pin_layer=DEFAULT_PIN_LAYER)
    assert len(mmi_pins) == 3
    for p in mmi_pins:
        assert math.isclose(p.width, 0.920, abs_tol=2e-3)


def test_cd_bias_on_8deg_angled_coupler_preserves_port_face():
    ly = kdb.Layout()
    ly.dbu = 0.001
    w0 = 0.80
    ec, (ex, ey) = _make_angled_8deg_coupler(ly, port_w=w0)
    tag_detected_ports(ly, ec, core_layers=((1, 0),), overwrite=True)

    res = bias_cell_linewidth(ly, ec, core_layers=((1, 0),), bias_um=0.120, preserve_port_faces=True)
    assert res["ok"] is True

    # Re-detect actual geometry ports after bias: position shift ~0, width = 0.920 um
    detected = auto_detect_ports(ly, ec, core_layers=((1, 0),))
    assert len(detected) == 2
    assert math.hypot(detected[0].x - 0.0, detected[0].y - 0.0) <= 0.002
    assert math.hypot(detected[1].x - ex, detected[1].y - ey) <= 0.002
    assert math.isclose(detected[0].width, 0.920, abs_tol=0.004)
    assert math.isclose(detected[1].width, 0.920, abs_tol=0.004)


# ---------------------------------------------------------------------------
# 3. Parametric PCell CD Bias & Repeated-Cell Deduplication
# ---------------------------------------------------------------------------


def test_bias_pcell_inst_and_repeated_cell_deduplication():
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("MULTI_INST_CHIP")

    # Shared static cell instantiated TWICE + connected EulerWaveguide & TOPICBend PCells
    mmi = _make_mmi_1x2(ly, name="SHARED_MMI", port_w=0.80)
    tag_detected_ports(ly, mmi, core_layers=((1, 0),), overwrite=True)
    top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(0.0, 0.0)))
    top.insert(kdb.DCellInstArray(mmi.cell_index(), kdb.DTrans(300.0, 0.0)))

    cs = get_cross_section("Core + Trench (Rib)")
    inst_wg = instantiate_euler_waveguide(
        ly,
        top,
        [(120.0, 3.0), (300.0, 3.0)],
        cs,
        start_width=0.80,
        end_width=1.00,
        taper_length=10.0,
        bend_type="topic",
        add_ports=True,
    )
    inst_tb = instantiate_topic_bend(
        ly, top, angle_deg=90.0, cross_section=cs, r_eff=40.0, trans=kdb.DTrans(420.0, 3.0), add_ports=True
    )

    static_cells, pcell_insts = collect_owning_static_cells(ly, top, core_layers=((1, 0),))
    # SHARED_MMI is instantiated twice, yet MUST appear ONCE so it is NEVER double-biased!
    assert [c.name for c in static_cells] == ["SHARED_MMI"]
    assert len(pcell_insts) == 2

    # Apply +120 nm bias across collected static cells & PCells
    for sc in static_cells:
        r = bias_cell_linewidth(ly, sc, core_layers=((1, 0),), bias_um=0.120, output_layer_map=None)
        assert r["ok"] is True

    for pci in pcell_insts:
        rp = bias_pcell_inst_linewidth(pci, bias_um=0.120, target_core_layers=((1, 0),))
        assert rp["ok"] is True

    # Verify SHARED_MMI widened by +120 nm (0.920 um), NOT +240 nm (1.040 um)!
    mmi_reg = kdb.Region(mmi.shapes(ly.find_layer(1, 0))).merged()
    w_mmi = _measure_normal_width(mmi_reg, 10.0, 0.0, 0.0, dbu=ly.dbu)
    assert abs(w_mmi - 0.920) <= 0.003

    # Verify EulerWaveguide core_width, start_width, end_width updated parametrically
    wg_p = inst_wg.pcell_parameters_by_name()
    assert math.isclose(float(wg_p["core_width"]), 0.920, abs_tol=1e-6)
    assert math.isclose(float(wg_p["start_width"]), 0.920, abs_tol=1e-6)
    assert math.isclose(float(wg_p["end_width"]), 1.120, abs_tol=1e-6)

    # Verify TOPICBend core_width updated and port o2 location remained strictly invariant
    tb_p = inst_tb.pcell_parameters_by_name()
    assert math.isclose(float(tb_p["core_width"]), 0.920, abs_tol=1e-6)
    tb_ports = {p.name: p for p in read_cell_ports(ly, inst_tb.cell)}
    assert math.hypot(tb_ports["o2"].x - 40.0, tb_ports["o2"].y - 40.0) < 1e-6
    assert math.isclose(tb_ports["o2"].width, 0.920, abs_tol=2e-3)

    # Guardrail: excessive negative bias leaving width <= 0.02 um is rejected cleanly
    bad = bias_pcell_inst_linewidth(inst_tb, bias_um=-0.91)
    assert bad["ok"] is False and "error" in bad
