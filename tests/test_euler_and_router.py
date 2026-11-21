"""Self-contained tests for neutral presets, Fresnel math, TOPIC (C3) & Euler (C2) bends,
C2 S-bends, multi-layer extrusion, auto/manual routing, PCells, and RouteLength annotations.

Uses synthetic in-memory geometry via klayout.db / pya only (zero external GDS files).
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

from pic_router import __version__
from pic_router.config import (
    CROSS_SECTION_PRESETS,
    DEFAULT_ERROR_LAYER,
    DEFAULT_LENGTH_LAYER,
    DEFAULT_PIN_LAYER,
    DEFAULT_PRESET_NAME,
    CrossSection,
    LayerSpec,
    delete_user_preset,
    get_all_presets,
    get_cross_section,
    get_kdb,
    load_user_presets,
    save_user_preset,
)
from pic_router.euler import (
    bend_metrics,
    bend_points,
    build_multilayer_waveguide_regions,
    euler_bend_metrics,
    euler_bend_points,
    extrude_centerline,
    fresnel,
    sbend_metrics,
    sbend_points,
    topic_bend_metrics,
    topic_bend_points,
)
from pic_router.gui import parse_layer_list_text
from pic_router.pcells import (
    LIBRARY_NAME,
    instantiate_euler_bend,
    instantiate_euler_waveguide,
    instantiate_topic_bend,
    register_pcell_library,
)
from pic_router.ports import Port, read_cell_ports
from pic_router.router import (
    VALID_LABEL_STYLES,
    _box_overlap_area,
    _extract_existing_text_boxes,
    advance_manual_straight_waypoints,
    analyze_live_guide_segments,
    apply_taper_segments,
    compute_manual_drag_waypoints,
    draw_length_annotations_to_cell,
    draw_route_to_cell,
    format_route_card_text,
    format_route_header,
    plan_route_ports,
    route_from_guide_points,
    route_sbend_from_guide_points,
    split_taper_segments_list,
    tune_route_for_integer_length,
    undo_last_waypoint,
)

kdb = get_kdb()


def _polyline_length(pts: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        for i in range(1, len(pts))
    )


def _angle_diff_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _discrete_curvature_profile(
    pts: list[tuple[float, float]],
) -> tuple[list[float], list[float]]:
    """Return arc-length s and signed discrete curvature kappa(s) at interior vertices."""
    cum_s = [0.0]
    for i in range(1, len(pts)):
        cum_s.append(cum_s[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))

    s_vals: list[float] = []
    k_vals: list[float] = []
    for i in range(1, len(pts) - 1):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        ax, ay = x1 - x0, y1 - y0
        bx, by = x2 - x1, y2 - y1
        cx, cy = x2 - x0, y2 - y0
        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)
        lc = math.hypot(cx, cy)
        if la < 1e-12 or lb < 1e-12 or lc < 1e-12:
            continue
        cross = ax * by - ay * bx
        kappa = 2.0 * cross / (la * lb * lc)
        s_vals.append(cum_s[i])
        k_vals.append(kappa)
    return s_vals, k_vals


# ---------------------------------------------------------------------------
# 1. Neutral Presets & User JSON Persistence
# ---------------------------------------------------------------------------


def test_neutral_builtin_presets():
    assert __version__ == "0.1.0"
    assert DEFAULT_PRESET_NAME == "Core + Trench (Rib)"
    assert set(CROSS_SECTION_PRESETS.keys()) == {
        "Core Only (Single Layer)",
        "Core + Trench (Rib)",
        "Double Etch (2 Trenches)",
    }

    cs_core = get_cross_section("Core Only (Single Layer)")
    assert cs_core.core_layer == (1, 0)
    assert math.isclose(cs_core.core_width, 0.50, abs_tol=1e-9)
    assert cs_core.trench_layer is None
    assert math.isclose(cs_core.radius, 20.0, abs_tol=1e-9)
    assert math.isclose(cs_core.euler_p, 0.50, abs_tol=1e-9)
    assert math.isclose(cs_core.min_straight, 5.0, abs_tol=1e-9)

    cs_rib = get_cross_section("Core + Trench (Rib)")
    assert cs_rib.core_layer == (1, 0)
    assert math.isclose(cs_rib.core_width, 0.80, abs_tol=1e-9)
    assert cs_rib.trench_layer == (1, 1)
    assert math.isclose(cs_rib.trench_width, 10.0, abs_tol=1e-9)
    assert math.isclose(cs_rib.radius, 50.0, abs_tol=1e-9)

    cs_dbl = get_cross_section("Double Etch (2 Trenches)")
    assert len(cs_dbl.layers) == 3
    assert [(ls.layer, ls.datatype, ls.width, ls.role) for ls in cs_dbl.layers] == [
        (1, 0, 0.80, "core"),
        (1, 1, 10.0, "trench"),
        (2, 1, 25.0, "trench"),
    ]

    # Round-trip JSON serialization
    cs_rt = CrossSection.from_json(cs_dbl.to_json())
    assert cs_rt == cs_dbl


def test_user_json_preset_persistence_and_protection(tmp_path: Path):
    preset_file = tmp_path / "pic_router_presets.json"
    assert load_user_presets(preset_file) == {}

    custom_cs = CrossSection(
        name="SiN_400nm",
        layers=[
            LayerSpec(layer=10, datatype=0, width=1.10, role="core"),
            LayerSpec(layer=10, datatype=1, width=14.0, role="trench"),
            LayerSpec(layer=11, datatype=1, width=30.0, role="trench"),
        ],
        radius=65.0,
        euler_p=0.45,
        min_straight=8.0,
    )

    saved_path = save_user_preset("SiN_400nm", custom_cs, path=preset_file)
    assert saved_path == preset_file
    assert preset_file.is_file()

    loaded = load_user_presets(preset_file)
    assert "SiN_400nm" in loaded
    assert loaded["SiN_400nm"].core_layer == (10, 0)
    assert math.isclose(loaded["SiN_400nm"].core_width, 1.10)
    assert len(loaded["SiN_400nm"].layers) == 3
    assert math.isclose(loaded["SiN_400nm"].radius, 65.0)

    merged = get_all_presets(path=preset_file)
    assert set(CROSS_SECTION_PRESETS.keys()).issubset(merged.keys())
    assert "SiN_400nm" in merged
    assert get_cross_section("SiN_400nm", path=preset_file).radius == 65.0

    # Built-in templates and "Custom" / empty names are strictly protected
    for protected in list(CROSS_SECTION_PRESETS.keys()) + ["Custom", "", "   "]:
        with pytest.raises(ValueError):
            save_user_preset(protected, custom_cs, path=preset_file)
        assert delete_user_preset(protected, path=preset_file) is False

    # Even if a hand-edited JSON attempts to shadow a built-in name, loader ignores it
    raw_payload = json.loads(preset_file.read_text(encoding="utf-8"))
    raw_payload["presets"]["Core Only (Single Layer)"] = custom_cs.to_dict()
    preset_file.write_text(json.dumps(raw_payload), encoding="utf-8")
    loaded_clean = load_user_presets(preset_file)
    assert "Core Only (Single Layer)" not in loaded_clean

    assert delete_user_preset("SiN_400nm", path=preset_file) is True
    assert delete_user_preset("SiN_400nm", path=preset_file) is False
    assert "SiN_400nm" not in load_user_presets(preset_file)


# ---------------------------------------------------------------------------
# 2. Pure-Python Fresnel Accuracy
# ---------------------------------------------------------------------------

_FRESNEL_REF_TABLE: list[tuple[float, float, float]] = [
    (0.0, 0.0000000000000000, 0.0000000000000000),
    (0.1, 0.0005235895476122, 0.0999975326270851),
    (0.5, 0.0647324328599993, 0.4923442258714464),
    (1.0, 0.4382591473903547, 0.7798934003768230),
    (1.5, 0.6975049600820930, 0.4452611760398216),
    (2.0, 0.3434156783636982, 0.4882534060753407),
    (5.0, 0.4991913819171169, 0.5636311887040122),
    (10.0, 0.4681699785848822, 0.4998986942055157),
    (20.0, 0.4840845359259539, 0.4999873349723444),
    (30.0, 0.4893896744421938, 0.4999962473706099),
    (50.0, 0.4936338025859388, 0.4999991894307280),
]


def test_pure_python_fresnel_accuracy():
    for u, s_ref, c_ref in _FRESNEL_REF_TABLE:
        s_val, c_val = fresnel(u)
        assert abs(s_val - s_ref) < 1e-13, f"S({u}) err={abs(s_val - s_ref)}"
        assert abs(c_val - c_ref) < 1e-13, f"C({u}) err={abs(c_val - c_ref)}"
        # Odd symmetry S(-u) = -S(u), C(-u) = -C(u)
        sn, cn = fresnel(-u)
        assert abs(sn + s_val) < 1e-15
        assert abs(cn + c_val) < 1e-15

    # Cross-check dense grid against scipy.special.fresnel when scipy is available
    try:
        from scipy.special import fresnel as sp_fresnel  # type: ignore[import-not-found]
    except ImportError:
        return

    for idx in range(-40, 41):
        u = idx * 0.85
        s_val, c_val = fresnel(u)
        s_sp, c_sp = sp_fresnel(u)
        assert abs(s_val - float(s_sp)) < 1e-13
        assert abs(c_val - float(c_sp)) < 1e-13


# ---------------------------------------------------------------------------
# 3. Euler (C2) & TOPIC (C3 Hermite) Bends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bend_type", ["euler", "topic"])
@pytest.mark.parametrize("angle_deg", [90.0, -90.0, 45.0, -45.0, 180.0, 12.0, -82.0])
def test_bend_exact_geometry_symmetry_and_angles(bend_type: str, angle_deg: float):
    r_eff = 50.0
    p = 0.50
    m = bend_metrics(angle_deg, r_eff, p=p, use_eff_radius=True, bend_type=bend_type)
    pts = bend_points(
        angle_deg, r_eff, p=p, use_eff_radius=True, max_chord_err=0.0005, bend_type=bend_type
    )

    abs_rad = math.radians(abs(angle_deg))
    sign = 1.0 if angle_deg > 0 else -1.0
    exp_dx = r_eff * math.sin(abs_rad)
    exp_dy = sign * r_eff * (1.0 - math.cos(abs_rad))

    assert math.isclose(m["R_eff"], r_eff, rel_tol=1e-12)
    assert math.isclose(m["dx"], exp_dx, abs_tol=1e-9)
    assert math.isclose(m["dy"], exp_dy, abs_tol=1e-9)
    assert pts[0] == (0.0, 0.0)
    assert math.isclose(pts[-1][0], exp_dx, abs_tol=1e-9)
    assert math.isclose(pts[-1][1], exp_dy, abs_tol=1e-9)

    if abs(abs(angle_deg) - 180.0) < 1e-9:
        assert math.isinf(m["tangent_offset"])
    else:
        assert math.isclose(m["tangent_offset"], r_eff * math.tan(0.5 * abs_rad), rel_tol=1e-12)

    # Start tangent along +X (0 deg) and exit tangent along angle_deg
    start_ang = math.degrees(math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]))
    end_ang = math.degrees(math.atan2(pts[-1][1] - pts[-2][1], pts[-1][0] - pts[-2][0]))
    assert _angle_diff_deg(start_ang, 0.0) < 0.08
    assert _angle_diff_deg(end_ang, angle_deg % 360.0) < 0.08

    # Arc length closely matches analytical L_total
    poly_l = _polyline_length(pts)
    assert abs(poly_l - m["L_total"]) < 0.02

    # Mirror symmetry across the bisector line through (0, sign*R_eff)
    half_t = 0.5 * abs_rad
    nx_b, ny_b = math.cos(half_t), math.sin(half_t)
    n_pts = len(pts)
    for i in range(n_pts):
        x_a, y_a = pts[i][0], sign * pts[i][1]
        x_b, y_b = pts[n_pts - 1 - i][0], sign * pts[n_pts - 1 - i][1]
        sd = x_a * nx_b + (y_a - r_eff) * ny_b
        rx, ry = x_a - 2.0 * sd * nx_b, y_a - 2.0 * sd * ny_b
        assert math.hypot(rx - x_b, ry - y_b) < 1e-6

    # use_eff_radius=False consistency check
    m_min = bend_metrics(angle_deg, m["R_min"], p=p, use_eff_radius=False, bend_type=bend_type)
    assert math.isclose(m_min["R_eff"], r_eff, rel_tol=1e-10)
    assert math.isclose(m_min["L_total"], m["L_total"], rel_tol=1e-10)


def test_topic_c3_smoothness_and_larger_rc_than_euler():
    r_eff = 50.0
    for p in (0.25, 0.50, 0.75, 1.0):
        mt = topic_bend_metrics(90.0, r_eff, p=p, use_eff_radius=True)
        me = euler_bend_metrics(90.0, r_eff, p=p, use_eff_radius=True)
        assert mt["R_min"] > me["R_min"], f"Expected TOPIC Rc > Euler Rmin at p={p}"

    # High-resolution sampling to verify kappa(0)=0 and kappa'(0)=0 for TOPIC vs Euler jump
    pts_t = topic_bend_points(90.0, r_eff, p=0.50, max_chord_err=1e-5, min_points=401)
    pts_e = euler_bend_points(90.0, r_eff, p=0.50, max_chord_err=1e-5, min_points=401)
    mt = topic_bend_metrics(90.0, r_eff, p=0.50)
    me = euler_bend_metrics(90.0, r_eff, p=0.50)

    st, kt = _discrete_curvature_profile(pts_t)
    se, ke = _discrete_curvature_profile(pts_e)

    # Both have ~0 curvature at entry and exit
    k_peak_t = 1.0 / mt["R_min"]
    k_peak_e = 1.0 / me["R_min"]
    assert abs(kt[0]) < 1e-3 * k_peak_t
    assert abs(kt[-1]) < 1e-3 * k_peak_t
    assert abs(ke[0]) < 0.02 * k_peak_e
    assert abs(ke[-1]) < 0.02 * k_peak_e

    # Curvature derivative kappa'(0): TOPIC -> 0 (C3), Euler -> 1/(R_min * L_p) != 0 (C2 jump)
    dk_t_start = (kt[1] - kt[0]) / max(st[1] - st[0], 1e-12)
    dk_t_end = (kt[-1] - kt[-2]) / max(st[-1] - st[-2], 1e-12)
    dk_e_start = (ke[1] - ke[0]) / max(se[1] - se[0], 1e-12)
    expected_dk_e = 1.0 / (me["R_min"] * (0.5 * me["R_min"] * 0.5 * math.pi))

    assert abs(dk_t_start) < 0.05 * expected_dk_e
    assert abs(dk_t_end) < 0.05 * expected_dk_e
    assert abs(dk_e_start - expected_dk_e) / expected_dk_e < 0.05


def test_bend_boundary_p_zero_and_p_one():
    r_eff = 40.0
    for btype in ("euler", "topic"):
        m0 = bend_metrics(90.0, r_eff, p=0.0, bend_type=btype)
        assert math.isclose(m0["R_min"], r_eff, rel_tol=1e-12)
        assert math.isclose(m0["L_total"], r_eff * 0.5 * math.pi, rel_tol=1e-12)
        pts0 = bend_points(90.0, r_eff, p=0.0, bend_type=btype)
        for x, y in pts0:
            # Pure circular arc centered at (0, r_eff)
            assert abs(math.hypot(x, y - r_eff) - r_eff) < 1e-9

        m1 = bend_metrics(90.0, r_eff, p=1.0, bend_type=btype)
        assert m1["R_min"] < r_eff
        pts1 = bend_points(90.0, r_eff, p=1.0, bend_type=btype)
        assert math.isclose(pts1[-1][0], r_eff, abs_tol=1e-9)
        assert math.isclose(pts1[-1][1], r_eff, abs_tol=1e-9)
        if btype == "topic":
            assert math.isclose(m1["L_circ"], 0.0, abs_tol=1e-12)
            assert math.isclose(m1["theta_p_deg"], 45.0, abs_tol=1e-12)


# ---------------------------------------------------------------------------
# 4. C2 Zero-Curvature S-Bends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("curve_type", ["sbend_bezier", "sbend_sine"])
@pytest.mark.parametrize("offset", [25.0, -25.0, 5.0])
def test_c2_sbend_metrics_and_geometry(curve_type: str, offset: float):
    length = 120.0
    m = sbend_metrics(length, offset, curve_type=curve_type)
    pts = sbend_points(length, offset, curve_type=curve_type, max_chord_err=0.0005)

    assert math.isclose(m["kappa_start"], 0.0, abs_tol=1e-12)
    assert math.isclose(m["kappa_end"], 0.0, abs_tol=1e-12)
    assert pts[0] == (0.0, 0.0)
    assert pts[-1] == (length, offset)
    assert m["L_total"] > math.hypot(length, offset)
    assert abs(_polyline_length(pts) - m["L_total"]) < 0.01

    # Horizontal tangents at both ends
    ang0 = math.degrees(math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]))
    ang1 = math.degrees(math.atan2(pts[-1][1] - pts[-2][1], pts[-1][0] - pts[-2][0]))
    assert abs(ang0) < 0.25
    assert abs(ang1) < 0.25

    # Degenerate zero-offset S-bend returns clean straight line
    m0 = sbend_metrics(length, 0.0, curve_type=curve_type)
    assert math.isinf(m0["R_min"])
    assert math.isclose(m0["L_total"], length)
    assert sbend_points(length, 0.0, curve_type=curve_type) == [(0.0, 0.0), (length, 0.0)]


def test_sbend_bezier_larger_rmin_than_sine():
    m_bez = sbend_metrics(100.0, 20.0, curve_type="sbend_bezier")
    m_sin = sbend_metrics(100.0, 20.0, curve_type="sbend_sine")
    assert m_bez["R_min"] > m_sin["R_min"]


# ---------------------------------------------------------------------------
# 5. Multi-Layer Extrusion & Bow-Tie-Free Wide Cladding Trenches on Tight Bends
# ---------------------------------------------------------------------------


def test_extrude_and_bowtie_free_wide_trench_on_tight_bend():
    # Straight taper test on extrude_centerline
    poly = extrude_centerline(
        [(0.0, 0.0), (50.0, 0.0)],
        width=0.80,
        start_width=1.60,
        end_width=0.50,
        taper_length=10.0,
    )
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    assert math.isclose(min(xs), 0.0, abs_tol=1e-9)
    assert math.isclose(max(xs), 50.0, abs_tol=1e-9)
    assert math.isclose(max(ys) - min(ys), 1.60, abs_tol=1e-9)

    # Tight bend where half outer trench width (12.5 um) > bend R_min (~10.5 um)!
    tight_cs = get_cross_section("Double Etch (2 Trenches)").with_overrides(radius=15.0, euler_p=0.50)
    pts = topic_bend_points(90.0, tight_cs.radius, p=tight_cs.euler_p)
    regions = build_multilayer_waveguide_regions(
        pts, tight_cs, dbu=0.001, start_width=1.20, end_width=0.60, taper_length=8.0
    )

    assert set(regions.keys()) == {(1, 0), (1, 1), (2, 1)}
    for lp, reg in regions.items():
        assert not reg.is_empty(), f"Layer {lp} empty"
        assert reg.count() == 1, f"Layer {lp} fractured into {reg.count()} polygons"
        p_obj = next(reg.each())
        assert p_obj.holes() == 0, f"Layer {lp} has unexpected pinhole/bowtie hole"

    # Outer trench (2/1) strictly contains inner trench (1/1), which contains core (1/0)
    assert (regions[(1, 0)] - regions[(1, 1)]).is_empty()
    assert (regions[(1, 1)] - regions[(2, 1)]).is_empty()

    # Start taper applies ONLY to core (1/0), NOT trench (1/1)
    core_slice = regions[(1, 0)] & kdb.Region(kdb.Box(0, -5000, 100, 5000))
    trench_slice = regions[(1, 1)] & kdb.Region(kdb.Box(0, -20000, 100, 20000))
    assert abs(core_slice.bbox().height() * 0.001 - 1.20) < 0.015
    assert abs(trench_slice.bbox().height() * 0.001 - 10.0) < 0.02


# ---------------------------------------------------------------------------
# 6. Auto Routing, 1-Shot Integer Tuning, & Non-Consuming Tapers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bend_type", ["topic", "euler"])
def test_auto_routing_all_modes_and_arbitrary_angle(bend_type: str):
    cases = [
        ("straight", Port("p1", 0, 0, 0, 0.8), Port("p2", 150, 0, 180, 0.8)),
        ("L", Port("p1", 0, 0, 0, 0.8), Port("p2", 150, 140, 270, 0.8)),
        ("Z", Port("p1", 0, 0, 0, 0.8), Port("p2", 260, 150, 180, 0.8)),
        ("U", Port("p1", 0, 0, 0, 0.8), Port("p2", 40, 160, 0, 0.8)),
        ("sbend_bezier", Port("p1", 0, 0, 0, 0.8), Port("p2", 180, 30, 180, 0.8)),
        ("sbend_sine", Port("p1", 0, 0, 0, 0.8), Port("p2", 180, -30, 180, 0.8)),
        ("euler_90", Port("p1", 0, 0, 0, 0.8), Port("p2", 260, 140, 180, 0.8)),
        ("auto", Port("p1", 0, 0, 0, 0.8), Port("p2", 220, 25, 180, 0.8)),
        ("auto", Port("p1", 0, 0, 8.0, 0.8), Port("p2", 220, 60, 180, 0.8)),
    ]

    for mode, p1, p2 in cases:
        res = plan_route_ports(
            p1, p2, radius=40.0, euler_p=0.50, min_straight=5.0, route_mode=mode, bend_type=bend_type
        )
        assert res.ok, f"mode={mode}, bend_type={bend_type} failed: {res.error}"
        assert math.hypot(res.centerline[0][0] - p1.x, res.centerline[0][1] - p1.y) < 1e-6
        assert math.hypot(res.centerline[-1][0] - p2.x, res.centerline[-1][1] - p2.y) < 1e-6

        # Entry & exit tangents align with p1 outward and -p2 outward
        t0 = math.degrees(
            math.atan2(res.centerline[1][1] - res.centerline[0][1], res.centerline[1][0] - res.centerline[0][0])
        )
        t1 = math.degrees(
            math.atan2(res.centerline[-1][1] - res.centerline[-2][1], res.centerline[-1][0] - res.centerline[-2][0])
        )
        assert _angle_diff_deg(t0, p1.angle_deg) < 0.25
        assert _angle_diff_deg(t1, (p2.angle_deg + 180.0) % 360.0) < 0.25

        if res.curve_type == "euler" and res.bends:
            assert res.bend_type == bend_type


@pytest.mark.parametrize("bend_type", ["topic", "euler"])
def test_analytical_integer_length_tuning_and_non_consuming_tapers(bend_type: str):
    p1 = Port("p1", 0.0, 0.0, 0.0, 1.20)
    p2 = Port("p2", 260.0, 150.0, 180.0, 0.50)
    taper_l = 15.0
    min_s = 5.0

    # Plan with dedicated start & end linear tapers (15 um each)
    res = plan_route_ports(
        p1,
        p2,
        radius=45.0,
        euler_p=0.50,
        min_straight=min_s,
        route_mode="euler_90",
        start_taper_len=taper_l,
        end_taper_len=taper_l,
        bend_type=bend_type,
    )
    assert res.ok, res.error

    # Verify raw first/last straight segments include full taper_l + >= min_straight
    assert res.segments[0]["kind"] == "straight"
    assert res.segments[-1]["kind"] == "straight"
    assert res.segments[0]["length"] >= taper_l + min_s - 1e-4
    assert res.segments[-1]["length"] >= taper_l + min_s - 1e-4

    # Tune analytically for exact integer um total length while protecting tapers
    tuned, used_r, was_tuned = tune_route_for_integer_length(
        res,
        radius=45.0,
        euler_p=0.50,
        min_straight=min_s,
        step_um=1.0,
        min_stub_start=taper_l,
        min_stub_end=taper_l,
        bend_type=bend_type,
    )
    assert tuned.ok
    assert was_tuned or abs(res.total_length - round(res.total_length)) < 5e-4
    assert abs(tuned.total_length - round(tuned.total_length)) < 1e-3
    assert 35.0 <= used_r <= 55.0

    # Endpoints strictly unchanged (zero device motion!)
    assert math.hypot(tuned.centerline[0][0] - p1.x, tuned.centerline[0][1] - p1.y) < 1e-6
    assert math.hypot(tuned.centerline[-1][0] - p2.x, tuned.centerline[-1][1] - p2.y) < 1e-6

    # Splitting tapers leaves pure S1 & S_last >= min_straight (tapers NEVER occupy S1/S2!)
    res_split = apply_taper_segments(
        tuned, core_width=0.80, start_width=1.20, end_width=0.50, taper_length=taper_l
    )
    tapers = [s for s in res_split.segments if s["kind"] == "taper"]
    straights = [s for s in res_split.segments if s["kind"] == "straight"]
    assert len(tapers) == 2
    assert math.isclose(tapers[0]["length"], taper_l, abs_tol=1e-4)
    assert math.isclose(tapers[1]["length"], taper_l, abs_tol=1e-4)
    assert all(s["length"] >= min_s - 1e-4 for s in straights)
    assert math.isclose(sum(s["length"] for s in res_split.segments), tuned.total_length, abs_tol=1e-4)

    # Also test C2 S-bend integer-length tuning with non-consuming taper
    sb_res = plan_route_ports(
        Port("s1", 0.0, 0.0, 0.0, 1.20),
        Port("s2", 220.0, 28.0, 180.0, 0.80),
        radius=45.0,
        euler_p=0.50,
        min_straight=min_s,
        route_mode="sbend_bezier",
        start_taper_len=taper_l,
        end_taper_len=0.0,
        bend_type=bend_type,
    )
    assert sb_res.ok
    sb_tuned, _, _ = tune_route_for_integer_length(
        sb_res,
        radius=45.0,
        euler_p=0.50,
        min_straight=min_s,
        step_um=1.0,
        min_stub_start=taper_l,
        min_stub_end=0.0,
        bend_type=bend_type,
    )
    assert sb_tuned.ok
    assert abs(sb_tuned.total_length - round(sb_tuned.total_length)) < 2e-3


def test_infeasible_route_draws_error_layer():
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("ERR_TEST")
    cs = get_cross_section(DEFAULT_PRESET_NAME)

    bad = route_from_guide_points([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], radius=50.0, euler_p=0.5)
    assert not bad.ok
    assert draw_route_to_cell(ly, top, bad, cs) is False
    li_err = ly.find_layer(*DEFAULT_ERROR_LAYER)
    assert li_err >= 0 and top.shapes(li_err).size() >= 2


# ---------------------------------------------------------------------------
# 7. Manual Pure-Straight + Appended 90° Bend & Lossless Undo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bend_type", ["topic", "euler"])
def test_manual_pure_straight_drag_and_lossless_undo(bend_type: str):
    start_pt = (0.0, 0.0)
    radius = 50.0
    euler_p = 0.50
    taper_l = 12.0
    t90 = bend_metrics(90.0, radius, p=euler_p, bend_type=bend_type)["tangent_offset"]

    # 1. Leg 1 via advance_manual_straight_waypoints: pure straight 40 um past 12 um taper
    a1 = advance_manual_straight_waypoints(
        start_pt, 0.0, [], [], (1.0, 0.0), 40.0,
        radius=radius, euler_p=euler_p, start_taper_len=taper_l, bend_type=bend_type,
    )
    assert a1["ok"]
    wps1, meta1 = a1["waypoints"], a1["wp_meta"]
    assert math.isclose(wps1[0][0], taper_l + 40.0, abs_tol=1e-3)
    assert math.isclose(wps1[0][1], 0.0, abs_tol=1e-3)

    # 2. Collinear extension by +15 um -> total straight 55 um
    a2 = advance_manual_straight_waypoints(
        start_pt, 0.0, wps1, meta1, (1.0, 0.0), 15.0,
        radius=radius, euler_p=euler_p, bend_type=bend_type,
    )
    assert a2["ok"]
    wps2, meta2 = a2["waypoints"], a2["wp_meta"]
    assert len(wps2) == 1
    assert math.isclose(wps2[0][0], taper_l + 55.0, abs_tol=1e-3)

    # Lossless undo of collinear extension restores exact wps1
    wps_u = list(wps2)
    meta_u = [dict(m, ext_history=list(m["ext_history"])) for m in meta2]
    assert undo_last_waypoint(wps_u, meta_u) is True
    assert wps_u == wps1

    # 3. 90° turn (+Y) with 30 um pure straight: preserves prior S1=55 um + taper 12 um!
    a3 = advance_manual_straight_waypoints(
        start_pt, 0.0, wps2, meta2, (0.0, 1.0), 30.0,
        radius=radius, euler_p=euler_p, bend_type=bend_type,
    )
    assert a3["ok"]
    wps3, meta3 = a3["waypoints"], a3["wp_meta"]
    assert len(wps3) == 2

    res = route_from_guide_points(
        [start_pt] + wps3, radius=radius, euler_p=euler_p, min_straight=1.0, bend_type=bend_type
    )
    assert res.ok
    split_segs = split_taper_segments_list(
        res.segments, core_width=0.80, start_width=1.20, end_width=None, taper_length=taper_l
    )
    assert split_segs[0]["kind"] == "taper" and math.isclose(split_segs[0]["length"], taper_l, abs_tol=1e-3)
    assert split_segs[1]["kind"] == "straight" and math.isclose(split_segs[1]["length"], 55.0, abs_tol=2e-3)
    assert split_segs[2]["kind"] == "euler"
    assert split_segs[3]["kind"] == "straight" and math.isclose(split_segs[3]["length"], 30.0, abs_tol=2e-3)

    # 180-deg reversal is cleanly rejected
    rev = advance_manual_straight_waypoints(
        start_pt, 0.0, wps3, meta3, (0.0, -1.0), 20.0, radius=radius, euler_p=euler_p, bend_type=bend_type
    )
    assert not rev["ok"]

    # Lossless undo of 90° turn restores shifted corner back to exact wps2!
    wps_u3 = list(wps3)
    meta_u3 = [dict(m, ext_history=list(m["ext_history"])) for m in meta3]
    assert undo_last_waypoint(wps_u3, meta_u3) is True
    assert wps_u3 == wps2

    # 4. compute_manual_drag_waypoints: pending_taper -> straight -> pending_bend -> turn90
    d_pt = compute_manual_drag_waypoints(
        start_pt, 0.0, [], [], (6.0, 0.0), radius=radius, euler_p=euler_p,
        start_taper_len=taper_l, bend_type=bend_type,
    )
    assert not d_pt["ok"] and d_pt["pending_taper"] is True

    d_s1 = compute_manual_drag_waypoints(
        start_pt, 0.0, [], [], (taper_l + 35.2, 1.0), radius=radius, euler_p=euler_p,
        start_taper_len=taper_l, bend_type=bend_type,
    )
    assert d_s1["ok"] and d_s1["action"] == "straight" and math.isclose(d_s1["straight_len"], 35.0)

    tip = d_s1["waypoints"][-1]
    d_pb = compute_manual_drag_waypoints(
        start_pt, 0.0, d_s1["waypoints"], d_s1["wp_meta"], (tip[0], tip[1] + 0.5 * t90),
        radius=radius, euler_p=euler_p, bend_type=bend_type,
    )
    assert not d_pb["ok"] and d_pb["pending_bend"] is True

    d_turn = compute_manual_drag_waypoints(
        start_pt, 0.0, d_s1["waypoints"], d_s1["wp_meta"], (tip[0], tip[1] + t90 + 22.3),
        radius=radius, euler_p=euler_p, bend_type=bend_type,
    )
    assert d_turn["ok"] and d_turn["action"] == "turn90" and math.isclose(d_turn["straight_len"], 22.0)

    live = analyze_live_guide_segments(
        [start_pt] + d_turn["waypoints"], radius=radius, euler_p=euler_p, min_straight=1.0, bend_type=bend_type
    )
    assert live["feasible_strict"] and live["has_euler"]


# ---------------------------------------------------------------------------
# 8. Semicolon Multi-Layer Parser & PCells (EulerWaveguide, EulerBend, TOPICBend)
# ---------------------------------------------------------------------------


def test_parse_layer_list_text():
    assert parse_layer_list_text("1/0") == [(1, 0, None)]
    assert parse_layer_list_text("1/1; 2/1") == [(1, 1, None), (2, 1, None)]
    assert parse_layer_list_text("1/1=10; 2/1=25") == [(1, 1, 10.0), (2, 1, 25.0)]
    assert parse_layer_list_text("1/1；2/1@25") == [(1, 1, None), (2, 1, 25.0)]
    assert parse_layer_list_text("1/1=10; 1/1=10") == [(1, 1, 10.0)]
    assert parse_layer_list_text("  ") == []


def test_pcells_euler_waveguide_euler_bend_topic_bend():
    lib = register_pcell_library()
    assert lib is not None
    assert sorted(lib.layout().pcell_names()) == ["EulerBend", "EulerWaveguide", "TOPICBend"]

    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("PCELL_TOP")
    cs_dbl = get_cross_section("Double Etch (2 Trenches)")

    # 1. EulerWaveguide with add_ports=True (Free-Draw style), TOPIC bends, and tapers
    inst_wg = instantiate_euler_waveguide(
        ly,
        top,
        [(0.0, 0.0), (100.0, 0.0), (100.0, 120.0)],
        cs_dbl,
        start_width=1.20,
        end_width=0.60,
        taper_length=10.0,
        bend_type="topic",
        add_ports=True,
    )
    wg_cell = inst_wg.cell
    assert wg_cell is not None and wg_cell.pcell_name() == "EulerWaveguide"
    for lp in ((1, 0), (1, 1), (2, 1)):
        li = ly.find_layer(*lp)
        assert li >= 0 and wg_cell.shapes(li).size() > 0

    wg_ports = {p.name: p for p in read_cell_ports(ly, wg_cell, pin_layer=DEFAULT_PIN_LAYER)}
    assert set(wg_ports.keys()) == {"o1", "o2"}
    assert math.hypot(wg_ports["o1"].x - 0.0, wg_ports["o1"].y - 0.0) < 1e-6
    assert _angle_diff_deg(wg_ports["o1"].angle_deg, 180.0) < 1e-3
    assert math.isclose(wg_ports["o1"].width, 1.20, abs_tol=1e-3)
    assert math.hypot(wg_ports["o2"].x - 100.0, wg_ports["o2"].y - 120.0) < 1e-6
    assert _angle_diff_deg(wg_ports["o2"].angle_deg, 90.0) < 1e-3
    assert math.isclose(wg_ports["o2"].width, 0.60, abs_tol=1e-3)

    # PinRec markers must sit strictly flush inside waveguide core bbox (zero overhang!)
    li_core = ly.find_layer(1, 0)
    li_pin = ly.find_layer(*DEFAULT_PIN_LAYER)
    core_bb = wg_cell.bbox_per_layer(li_core)
    pin_bb = wg_cell.bbox_per_layer(li_pin)
    assert pin_bb.left >= core_bb.left
    assert pin_bb.right <= core_bb.right
    assert pin_bb.bottom >= core_bb.bottom
    assert pin_bb.top <= core_bb.top

    # EulerWaveguide with add_ports=False stamps zero PinRec ports
    inst_wg_np = instantiate_euler_waveguide(
        ly, top, [(0.0, -50.0), (80.0, -50.0)], cs_dbl, bend_type="euler", add_ports=False
    )
    assert read_cell_ports(ly, inst_wg_np.cell, pin_layer=DEFAULT_PIN_LAYER) == []

    # 2. EulerBend PCell
    inst_eb = instantiate_euler_bend(ly, top, angle_deg=-90.0, cross_section=cs_dbl, add_ports=True)
    eb_ports = {p.name: p for p in read_cell_ports(ly, inst_eb.cell, pin_layer=DEFAULT_PIN_LAYER)}
    assert set(eb_ports.keys()) == {"o1", "o2"}
    assert math.hypot(eb_ports["o2"].x - 50.0, eb_ports["o2"].y + 50.0) < 1e-6
    assert _angle_diff_deg(eb_ports["o2"].angle_deg, 270.0) < 1e-3

    # 3. TOPICBend PCell with straight_in & straight_out leads
    inst_tb = instantiate_topic_bend(
        ly,
        top,
        angle_deg=90.0,
        cross_section=cs_dbl,
        r_eff=40.0,
        p=0.50,
        straight_in=10.0,
        straight_out=15.0,
        add_ports=True,
    )
    tb_ports = {p.name: p for p in read_cell_ports(ly, inst_tb.cell, pin_layer=DEFAULT_PIN_LAYER)}
    assert set(tb_ports.keys()) == {"o1", "o2"}
    assert math.hypot(tb_ports["o1"].x - 0.0, tb_ports["o1"].y - 0.0) < 1e-6
    assert math.hypot(tb_ports["o2"].x - 50.0, tb_ports["o2"].y - 55.0) < 1e-6
    assert _angle_diff_deg(tb_ports["o2"].angle_deg, 90.0) < 1e-3


# ---------------------------------------------------------------------------
# 9. Collision-Free RouteLength (90/2) Annotations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", VALID_LABEL_STYLES)
def test_collision_free_route_length_annotations(style: str):
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell(f"ANNOT_{style}")

    # 3 closely spaced parallel L-routes to stress collision avoidance
    routes = []
    for k in range(3):
        y0 = k * 12.0
        r = route_from_guide_points(
            [(0.0, y0), (140.0 + k * 12.0, y0), (140.0 + k * 12.0, 140.0 + y0)],
            radius=40.0,
            euler_p=0.50,
            min_straight=5.0,
            bend_type="topic",
        )
        assert r.ok
        routes.append(r)

    occ: list[tuple[float, float, float, float]] = []
    obs: list[tuple[float, float]] = []
    for r in routes:
        obs.extend(r.centerline)

    total_labels = 0
    for idx, r in enumerate(routes, start=1):
        n_emitted = draw_length_annotations_to_cell(
            ly,
            top,
            r,
            length_layer=DEFAULT_LENGTH_LAYER,
            label_offset=8.0,
            label_style=style,
            route_index=idx,
            occupied_boxes=occ,
            obstacle_points=obs,
            ref_span_um=400.0,
        )
        total_labels += n_emitted

    li = ly.find_layer(*DEFAULT_LENGTH_LAYER)
    assert li >= 0
    texts = [sh.dtext.string for sh in top.shapes(li).each() if sh.is_text()]
    assert len(texts) == total_labels

    if style == "card":
        assert len(texts) == 3
        assert all("TOPIC" in t and "S1:" in t and "B1(" in t for t in texts)
    elif style == "total_only":
        assert len(texts) == 3
        assert all("Total:" in t and "TOPIC" in t for t in texts)
    else:
        # 3 segments + 1 Total header per route = 4 per route -> 12 labels
        assert len(texts) == 12

    # Verify zero pairwise overlap among all placed annotation boxes!
    for i in range(len(occ)):
        for j in range(i + 1, len(occ)):
            assert _box_overlap_area(occ[i], occ[j]) == 0.0, (
                f"Overlap between label boxes {i} and {j} in style={style}"
            )

    # Sanity check formatting helpers
    hdr = format_route_header(routes[0].segments, routes[0].total_length, route_index=1)
    card = format_route_card_text(routes[0].segments, routes[0].total_length, route_index=1)
    assert "#1 Total:" in hdr and "TOPIC" in hdr
    assert card.startswith(hdr)
    extracted = _extract_existing_text_boxes(top, li, line_h=14.0)
    assert len(extracted) == len(texts)
