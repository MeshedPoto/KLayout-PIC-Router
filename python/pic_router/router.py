"""Pure-Python photonic waveguide guide & port-to-port TOPIC / Euler router.

Zero numpy/scipy dependencies: runs identically inside KLayout 0.28–0.30+ embedded
Python 3.10+ (`pya`) and external Python (`klayout.db`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from .config import CrossSection, DEFAULT_ERROR_LAYER, DEFAULT_LENGTH_LAYER, get_kdb
from .euler import (
    VALID_BEND_TYPES,
    VALID_SBEND_CURVES,
    bend_metrics,
    bend_points,
    build_multilayer_waveguide_regions,
    euler_bend_metrics,
    euler_bend_points,
    sbend_metrics,
    sbend_points,
    topic_bend_metrics,
    topic_bend_points,
)


@dataclass
class RouteResult:
    """Result of a waveguide routing operation."""

    ok: bool
    centerline: list[tuple[float, float]] = field(default_factory=list)
    guide_points: list[tuple[float, float]] = field(default_factory=list)
    total_length: float = 0.0
    bends: list[dict[str, Any]] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    curve_type: str = "euler"
    sbend_angle_deg: float = 0.0
    bend_type: str = "euler"


@dataclass
class _SimplePort:
    x: float
    y: float
    angle_deg: float
    width: float = 0.70


def _wrap_rad_pi(angle: float) -> float:
    """Wrap angle in radians into (-pi, pi]."""
    a = (angle + math.pi) % (2.0 * math.pi) - math.pi
    if a <= -math.pi:
        a += 2.0 * math.pi
    return a


def _polyline_length(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(pts)):
        total += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
    return total


def _simplify_guide_points(
    guide_points: list[tuple[float, float]],
    dist_eps: float = 1e-6,
    angle_eps_rad: float = 1e-5,
) -> tuple[list[tuple[float, float]], str]:
    """Remove duplicate points and redundant collinear forward vertices.

    Returns (simplified_points, error_message). Detects 180-degree fold-backs.
    """
    dedup: list[tuple[float, float]] = []
    for pt in guide_points:
        x, y = float(pt[0]), float(pt[1])
        if not dedup or math.hypot(x - dedup[-1][0], y - dedup[-1][1]) > dist_eps:
            dedup.append((x, y))

    if len(dedup) < 2:
        return (dedup, "Guide path must contain at least 2 distinct points")

    # Iteratively remove collinear forward interior vertices
    pts = dedup
    changed = True
    while changed and len(pts) >= 3:
        changed = False
        new_pts: list[tuple[float, float]] = [pts[0]]
        for i in range(1, len(pts) - 1):
            p_prev = new_pts[-1]
            p_curr = pts[i]
            p_next = pts[i + 1]
            v1x, v1y = p_curr[0] - p_prev[0], p_curr[1] - p_prev[1]
            v2x, v2y = p_next[0] - p_curr[0], p_next[1] - p_curr[1]
            l1 = math.hypot(v1x, v1y)
            l2 = math.hypot(v2x, v2y)
            if l1 <= dist_eps or l2 <= dist_eps:
                changed = True
                continue
            cross = (v1x * v2y - v1y * v2x) / (l1 * l2)
            dot = (v1x * v2x + v1y * v2y) / (l1 * l2)
            ang = math.atan2(cross, dot)
            if abs(ang) < angle_eps_rad:
                # Collinear forward vertex -> drop redundant vertex
                changed = True
                continue
            if abs(abs(ang) - math.pi) < 1e-4:
                return (
                    pts,
                    f"Corner {i} at ({p_curr[0]:.3f}, {p_curr[1]:.3f}) has a 180-degree "
                    f"fold-back; insert an offset waypoint to form a U-bend",
                )
            new_pts.append(p_curr)
        new_pts.append(pts[-1])
        pts = new_pts

    if len(pts) < 2:
        return (pts, "Guide path collapses to fewer than 2 distinct points")

    return (pts, "")


def route_from_guide_points(
    guide_points: list[tuple[float, float]],
    radius: float = 80.0,
    euler_p: float = 0.5,
    min_straight: float = 0.0,
    use_eff_radius: bool = True,
    min_straight_ends: float = 0.0,
    bend_type: str = "euler",
) -> RouteResult:
    """Convert a guiding polyline into a smooth Euler (C2) or TOPIC (C3) waveguide centerline.

    Checks exact bend tangent offsets at every corner and verifies that:
      - interior segments between bends satisfy `seg_len >= off_prev + off_next + min_straight`
      - start/end stubs satisfy `seg_len >= off + min_straight_ends`
    Returns `RouteResult(ok=False, error=...)` with preserved `guide_points` if infeasible.
    """
    raw_guide = [(float(p[0]), float(p[1])) for p in guide_points]
    radius = float(radius)
    euler_p = float(euler_p)
    min_straight = float(min_straight)
    min_straight_ends = float(min_straight_ends)
    btype = "topic" if str(bend_type or "euler").strip().lower() == "topic" else "euler"

    if radius <= 0.0:
        return RouteResult(
            ok=False,
            guide_points=raw_guide,
            error=f"Bend radius must be > 0, got {radius}",
        )
    if not (0.0 <= euler_p <= 1.0):
        return RouteResult(
            ok=False,
            guide_points=raw_guide,
            error=f"euler_p must be in [0.0, 1.0], got {euler_p}",
        )
    if min_straight < 0.0 or min_straight_ends < 0.0:
        return RouteResult(
            ok=False,
            guide_points=raw_guide,
            error="min_straight must be >= 0.0",
        )

    pts, simp_err = _simplify_guide_points(raw_guide)
    if simp_err:
        return RouteResult(ok=False, guide_points=raw_guide, error=simp_err)

    n = len(pts)
    if n == 2:
        length = math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
        if length < 1e-6:
            return RouteResult(
                ok=False,
                guide_points=raw_guide,
                error="Start and end points are coincident",
            )
        mid_pt = (0.5 * (pts[0][0] + pts[1][0]), 0.5 * (pts[0][1] + pts[1][1]))
        heading = math.degrees(math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0])) % 360.0
        seg = {
            "kind": "straight",
            "index": 1,
            "length": length,
            "start": pts[0],
            "end": pts[1],
            "midpoint": mid_pt,
            "angle_deg": heading,
            "label": f"S1: {length:.3f} um",
        }
        return RouteResult(
            ok=True,
            centerline=[pts[0], pts[1]],
            guide_points=raw_guide,
            total_length=length,
            bends=[],
            segments=[seg],
            warnings=[],
            error="",
        )

    # Compute segment vectors and lengths
    seg_vecs: list[tuple[float, float]] = []
    seg_lens: list[float] = []
    for i in range(n - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        sl = math.hypot(dx, dy)
        seg_vecs.append((dx / sl, dy / sl))
        seg_lens.append(sl)

    # Compute Euler metrics and tangent offsets at every corner j = 1 .. n-2
    corner_metrics: list[dict[str, Any]] = []
    offsets: list[float] = [0.0] * n

    for j in range(1, n - 1):
        u_in = seg_vecs[j - 1]
        u_out = seg_vecs[j]
        cross = u_in[0] * u_out[1] - u_in[1] * u_out[0]
        dot = max(-1.0, min(1.0, u_in[0] * u_out[0] + u_in[1] * u_out[1]))
        turn_rad = math.atan2(cross, dot)
        if abs(abs(turn_rad) - math.pi) < 1e-4:
            return RouteResult(
                ok=False,
                guide_points=raw_guide,
                error=f"Corner {j} has a 180-degree reversal; cannot insert single Euler bend",
            )
        turn_deg = math.degrees(turn_rad)
        m = bend_metrics(
            angle_deg=turn_deg,
            radius=radius,
            p=euler_p,
            use_eff_radius=use_eff_radius,
            bend_type=btype,
        )
        offsets[j] = m["tangent_offset"]
        corner_metrics.append(
            {
                **m,
                "corner_index": j,
                "corner": pts[j],
                "in_angle_deg": math.degrees(math.atan2(u_in[1], u_in[0])) % 360.0,
                "out_angle_deg": math.degrees(math.atan2(u_out[1], u_out[0])) % 360.0,
            }
        )

    # Validate every segment length against required offsets + min_straight
    warnings: list[str] = []
    tol = 1e-5
    for i in range(n - 1):
        off_prev = offsets[i]
        off_next = offsets[i + 1]
        is_interior = (0 < i < n - 2)
        req_straight = min_straight if is_interior else min_straight_ends
        needed = off_prev + off_next + req_straight

        if seg_lens[i] + tol < needed:
            seg_kind = "Interior segment" if is_interior else "End stub segment"
            return RouteResult(
                ok=False,
                guide_points=raw_guide,
                error=(
                    f"{seg_kind} {i} length {seg_lens[i]:.3f} um < required "
                    f"{needed:.3f} um (offsets {off_prev:.3f} + {off_next:.3f} + "
                    f"min_straight {req_straight:.3f} um)"
                ),
            )
        if not is_interior and min_straight > min_straight_ends:
            rem = seg_lens[i] - (off_prev + off_next)
            if rem + tol < min_straight:
                warnings.append(
                    f"End stub segment {i} straight length {rem:.3f} um < "
                    f"min_straight {min_straight:.3f} um"
                )

    # Stitch straight segments and exact transformed Euler bends
    centerline: list[tuple[float, float]] = [pts[0]]
    bends_out: list[dict[str, Any]] = []
    segments_out: list[dict[str, Any]] = []
    total_length = 0.0
    prev_end = pts[0]
    s_idx = 0
    b_idx = 0

    for idx, cm in enumerate(corner_metrics):
        j = cm["corner_index"]
        u_in = seg_vecs[j - 1]
        u_out = seg_vecs[j]
        off = offsets[j]
        cx, cy = pts[j]

        b_start = (cx - off * u_in[0], cy - off * u_in[1])
        b_end = (cx + off * u_out[0], cy + off * u_out[1])

        # Straight section leading into this bend
        straight_len = math.hypot(b_start[0] - prev_end[0], b_start[1] - prev_end[1])
        total_length += straight_len
        if straight_len > 1e-7:
            centerline.append(b_start)
        if straight_len >= 1e-4:
            s_idx += 1
            s_mid = (0.5 * (prev_end[0] + b_start[0]), 0.5 * (prev_end[1] + b_start[1]))
            s_hdg = math.degrees(math.atan2(b_start[1] - prev_end[1], b_start[0] - prev_end[0])) % 360.0
            segments_out.append(
                {
                    "kind": "straight",
                    "index": s_idx,
                    "length": straight_len,
                    "start": prev_end,
                    "end": b_start,
                    "midpoint": s_mid,
                    "angle_deg": s_hdg,
                    "label": f"S{s_idx}: {straight_len:.3f} um",
                }
            )

        # Sample local bend and transform into world coordinates
        local_pts = bend_points(
            angle_deg=cm["angle_deg"],
            radius=radius,
            p=euler_p,
            use_eff_radius=use_eff_radius,
            bend_type=btype,
        )
        cos_a, sin_a = u_in
        mid_k = len(local_pts) // 2
        b_mid = (0.5 * (b_start[0] + b_end[0]), 0.5 * (b_start[1] + b_end[1]))
        for k in range(1, len(local_pts)):
            lx, ly = local_pts[k]
            if k == len(local_pts) - 1:
                wx, wy = b_end
            else:
                wx = b_start[0] + lx * cos_a - ly * sin_a
                wy = b_start[1] + lx * sin_a + ly * cos_a
            if k == mid_k:
                b_mid = (wx, wy)
            if math.hypot(wx - centerline[-1][0], wy - centerline[-1][1]) > 1e-9:
                centerline.append((wx, wy))

        total_length += cm["L_total"]
        prev_end = b_end

        b_idx += 1
        ang_abs = abs(float(cm["angle_deg"]))
        ang_str = f"{ang_abs:.0f}deg" if abs(ang_abs - round(ang_abs)) < 0.05 else f"{ang_abs:.1f}deg"
        r_min_v = float(cm["R_min"])
        r_eff_v = float(cm["R_eff"])
        bt_tag = ", TOPIC" if btype == "topic" else ""
        bend_rec = dict(cm)
        bend_rec["bend_type"] = btype
        bend_rec["start"] = b_start
        bend_rec["end"] = b_end
        bend_rec["midpoint"] = b_mid
        bends_out.append(bend_rec)
        segments_out.append(
            {
                "kind": "euler",
                "bend_type": btype,
                "index": b_idx,
                "length": float(cm["L_total"]),
                "start": b_start,
                "end": b_end,
                "midpoint": b_mid,
                "corner": (cx, cy),
                "angle_deg": float(cm["angle_deg"]),
                "R_min": r_min_v,
                "R_eff": r_eff_v,
                "label": f"B{b_idx}({ang_str}{bt_tag}, Rmin={r_min_v:.1f}/Reff={r_eff_v:.1f}): {float(cm['L_total']):.3f} um",
            }
        )

    # Final straight segment to pts[-1]
    end_straight = math.hypot(pts[-1][0] - prev_end[0], pts[-1][1] - prev_end[1])
    total_length += end_straight
    if math.hypot(pts[-1][0] - centerline[-1][0], pts[-1][1] - centerline[-1][1]) > 1e-7:
        centerline.append(pts[-1])
    else:
        centerline[-1] = pts[-1]
    if end_straight >= 1e-4:
        s_idx += 1
        s_mid = (0.5 * (prev_end[0] + pts[-1][0]), 0.5 * (prev_end[1] + pts[-1][1]))
        s_hdg = math.degrees(math.atan2(pts[-1][1] - prev_end[1], pts[-1][0] - prev_end[0])) % 360.0
        segments_out.append(
            {
                "kind": "straight",
                "index": s_idx,
                "length": end_straight,
                "start": prev_end,
                "end": pts[-1],
                "midpoint": s_mid,
                "angle_deg": s_hdg,
                "label": f"S{s_idx}: {end_straight:.3f} um",
            }
        )

    return RouteResult(
        ok=True,
        centerline=centerline,
        guide_points=raw_guide,
        total_length=total_length,
        bends=bends_out,
        segments=segments_out,
        warnings=warnings,
        error="",
        curve_type="euler",
        bend_type=btype,
    )


def route_sbend_from_guide_points(
    guide_points: list[tuple[float, float]],
    curve_type: str = "sbend_bezier",
    start_angle_deg: float = 0.0,
    target_r_min: float | None = None,
) -> RouteResult:
    """Build a C2 zero-endpoint-curvature S-bend route from a 2-point or 4-point guide.

    - 2-point guide ``[P0, P1]``: pure C2 S-bend from P0 to P1 leaving/arriving along ``start_angle_deg``.
    - 4-point guide ``[P0, P1, P2, P3]``: straight stub ``S1`` (P0->P1) + C2 S-bend (P1->P2) +
      straight stub ``S2`` (P2->P3). Tangent angle is inferred from ``P0->P1`` (or ``P2->P3``).
    Both ``sbend_bezier`` (optimal Quintic Bezier) and ``sbend_sine`` (raised-sine) satisfy
    ``kappa(0) = kappa(1) = 0`` with continuous C-infinity curvature everywhere.
    """
    raw_guide = [(float(p[0]), float(p[1])) for p in guide_points]
    if curve_type not in VALID_SBEND_CURVES:
        return RouteResult(
            ok=False,
            guide_points=raw_guide,
            error=f"Unsupported S-bend curve_type {curve_type!r}",
            curve_type=curve_type,
        )
    if len(raw_guide) < 2:
        return RouteResult(
            ok=False,
            guide_points=raw_guide,
            error="S-bend guide path requires at least 2 points",
            curve_type=curve_type,
        )

    p_start = raw_guide[0]
    p_end = raw_guide[-1]
    if len(raw_guide) == 2:
        th1 = math.radians(float(start_angle_deg))
        p_sb0 = p_start
        p_sb1 = p_end
        l_s1 = 0.0
        l_s2 = 0.0
    elif len(raw_guide) == 3:
        # Asymmetric 1-sided straight stub (e.g. 1-sided linear taper + C2 S-bend)
        p0, pm, p2 = raw_guide
        th_hint = math.radians(float(start_angle_deg))
        dhx, dhy = math.cos(th_hint), math.sin(th_hint)
        v1x, v1y = pm[0] - p0[0], pm[1] - p0[1]
        v2x, v2y = p2[0] - pm[0], p2[1] - pm[1]
        d1_len = math.hypot(v1x, v1y)
        d2_len = math.hypot(v2x, v2y)
        perp1 = abs(v1x * dhy - v1y * dhx) if d1_len > 1e-6 else float("inf")
        perp2 = abs(v2x * dhy - v2y * dhx) if d2_len > 1e-6 else float("inf")
        if d1_len > 1e-6 and perp1 <= perp2 and perp1 < 1e-2 and (v1x * dhx + v1y * dhy) > 0.0:
            th1 = math.atan2(v1y, v1x)
            p_sb0 = pm
            p_sb1 = p2
            l_s1 = d1_len
            l_s2 = 0.0
        elif d2_len > 1e-6 and perp2 < 1e-2 and (v2x * dhx + v2y * dhy) > 0.0:
            th1 = math.atan2(v2y, v2x)
            p_sb0 = p0
            p_sb1 = pm
            l_s1 = 0.0
            l_s2 = d2_len
        else:
            return RouteResult(
                ok=False,
                guide_points=raw_guide,
                error="3-point S-bend guide requires one leg aligned with start_angle_deg",
                curve_type=curve_type,
            )
    elif len(raw_guide) == 4:
        p0, p1, p2, p3 = raw_guide
        v1x, v1y = p1[0] - p0[0], p1[1] - p0[1]
        v2x, v2y = p3[0] - p2[0], p3[1] - p2[1]
        l_s1 = math.hypot(v1x, v1y)
        l_s2 = math.hypot(v2x, v2y)
        if l_s1 > 1e-6:
            th1 = math.atan2(v1y, v1x)
        elif l_s2 > 1e-6:
            th1 = math.atan2(v2y, v2x)
        else:
            th1 = math.radians(float(start_angle_deg))
        p_sb0 = p1
        p_sb1 = p2
    else:
        return RouteResult(
            ok=False,
            guide_points=raw_guide,
            error=f"S-bend guide path must have 2, 3, or 4 points, got {len(raw_guide)}",
            curve_type=curve_type,
        )

    angle_deg_used = math.degrees(th1) % 360.0
    dx_sb, dy_sb = _world_to_local(p_sb1[0], p_sb1[1], p_sb0[0], p_sb0[1], th1)
    if dx_sb <= 1e-4:
        return RouteResult(
            ok=False,
            guide_points=raw_guide,
            error=f"S-bend requires positive forward span (got dx={dx_sb:.3f} um)",
            curve_type=curve_type,
            sbend_angle_deg=angle_deg_used,
        )

    # Collinear degenerate case -> straight waveguide
    if abs(dy_sb) < 1e-4:
        total_l = math.hypot(p_end[0] - p_start[0], p_end[1] - p_start[1])
        mid_pt = (0.5 * (p_start[0] + p_end[0]), 0.5 * (p_start[1] + p_end[1]))
        seg = {
            "kind": "straight",
            "index": 1,
            "length": total_l,
            "start": p_start,
            "end": p_end,
            "midpoint": mid_pt,
            "angle_deg": angle_deg_used,
            "label": f"S1: {total_l:.3f} um",
        }
        return RouteResult(
            ok=True,
            centerline=[p_start, p_end],
            guide_points=[p_start, p_end],
            total_length=total_l,
            bends=[],
            segments=[seg],
            warnings=[],
            error="",
            curve_type="euler",
            sbend_angle_deg=angle_deg_used,
        )

    m = sbend_metrics(dx_sb, dy_sb, curve_type=curve_type)
    local_sb = sbend_points(dx_sb, dy_sb, curve_type=curve_type)
    world_sb = _local_to_world(local_sb, p_sb0[0], p_sb0[1], th1)
    world_sb[0] = p_sb0
    world_sb[-1] = p_sb1
    sb_mid = world_sb[len(world_sb) // 2]

    centerline: list[tuple[float, float]] = []
    segments_out: list[dict[str, Any]] = []
    s_idx = 0

    if l_s1 >= 1e-4:
        centerline.append(p_start)
        s_idx += 1
        segments_out.append(
            {
                "kind": "straight",
                "index": s_idx,
                "length": l_s1,
                "start": p_start,
                "end": p_sb0,
                "midpoint": (0.5 * (p_start[0] + p_sb0[0]), 0.5 * (p_start[1] + p_sb0[1])),
                "angle_deg": angle_deg_used,
                "label": f"S{s_idx}: {l_s1:.3f} um",
            }
        )

    for pt in world_sb:
        if not centerline or math.hypot(pt[0] - centerline[-1][0], pt[1] - centerline[-1][1]) > 1e-9:
            centerline.append(pt)

    short_tag = "C2-Bez" if curve_type == "sbend_bezier" else "C2-Sin"
    r_min_v = float(m["R_min"])
    l_sb = float(m["L_total"])
    sb_label = f"SBend({short_tag}, Rmin={r_min_v:.1f}): {l_sb:.3f} um"
    bend_rec = {
        "kind": curve_type,
        "angle_deg": float(m["max_angle_deg"]),
        "R_min": r_min_v,
        "R_eff": r_min_v,
        "L_total": l_sb,
        "dx": dx_sb,
        "dy": dy_sb,
        "kappa_start": float(m["kappa_start"]),
        "kappa_end": float(m["kappa_end"]),
        "start": p_sb0,
        "end": p_sb1,
        "midpoint": sb_mid,
    }
    segments_out.append(
        {
            "kind": curve_type,
            "index": 1,
            "length": l_sb,
            "start": p_sb0,
            "end": p_sb1,
            "midpoint": sb_mid,
            "angle_deg": float(m["max_angle_deg"]),
            "R_min": r_min_v,
            "R_eff": r_min_v,
            "label": sb_label,
        }
    )

    if l_s2 >= 1e-4:
        if math.hypot(p_end[0] - centerline[-1][0], p_end[1] - centerline[-1][1]) > 1e-9:
            centerline.append(p_end)
        s_idx += 1
        segments_out.append(
            {
                "kind": "straight",
                "index": s_idx,
                "length": l_s2,
                "start": p_sb1,
                "end": p_end,
                "midpoint": (0.5 * (p_sb1[0] + p_end[0]), 0.5 * (p_sb1[1] + p_end[1])),
                "angle_deg": angle_deg_used,
                "label": f"S{s_idx}: {l_s2:.3f} um",
            }
        )
    else:
        centerline[-1] = p_end

    warnings: list[str] = []
    if target_r_min is not None and r_min_v + 1e-3 < float(target_r_min):
        warnings.append(
            f"S-bend Rmin={r_min_v:.2f} um < target Rmin={float(target_r_min):.2f} um"
        )

    total_length = l_s1 + l_sb + l_s2
    return RouteResult(
        ok=True,
        centerline=centerline,
        guide_points=raw_guide,
        total_length=total_length,
        bends=[bend_rec],
        segments=segments_out,
        warnings=warnings,
        error="",
        curve_type=curve_type,
        sbend_angle_deg=angle_deg_used,
    )


def _plan_c2_sbend(
    p1: _SimplePort,
    p2: _SimplePort,
    curve_type: str,
    radius: float,
    euler_p: float,
    s: float,
    use_eff_radius: bool,
    bend_type: str = "euler",
) -> RouteResult | None:
    """Plan a C2 zero-curvature S-bend between facing ports p1 and p2 (if forward span > 1 um)."""
    th1 = math.radians(p1.angle_deg)
    lx, ly = _world_to_local(p2.x, p2.y, p1.x, p1.y, th1)
    if lx <= 1.0:
        return None
    if abs(ly) < 1e-4:
        return route_from_guide_points(
            [(p1.x, p1.y), (p2.x, p2.y)],
            radius=radius,
            euler_p=euler_p,
            min_straight=s,
            use_eff_radius=use_eff_radius,
            bend_type=bend_type,
        )

    r_min_req = (
        float(radius)
        if not use_eff_radius
        else float(bend_metrics(90.0, radius, euler_p, True, bend_type=bend_type)["R_min"])
    )

    # Reject geometrically degenerate cliff S-bends (e.g. tiny forward lx across chip-scale |ly|)
    # where even zero-stub full-span curvature collapses far below physical bend radius, so
    # callers cleanly fall back to 90-deg Bend + Straight routing.
    m_full = sbend_metrics(lx, ly, curve_type=curve_type)
    if abs(ly) > 4.0 * lx or float(m_full["R_min"]) < max(2.0, 0.12 * r_min_req):
        return None

    # Prefer straight stubs of length `s` when they still meet r_min_req;
    # otherwise use full span (stub=0, still kappa=0 at both ends!) to maximize R_min.
    use_stub = 0.0
    if s > 1e-4 and lx > 2.0 * s + 1.0:
        m_stub = sbend_metrics(lx - 2.0 * s, ly, curve_type=curve_type)
        if float(m_stub["R_min"]) >= r_min_req - 1e-3:
            use_stub = s

    if use_stub > 1e-4:
        loc_g = [(0.0, 0.0), (use_stub, 0.0), (lx - use_stub, ly), (lx, ly)]
    else:
        loc_g = [(0.0, 0.0), (lx, ly)]
    world_g = _local_to_world(loc_g, p1.x, p1.y, th1)
    return route_sbend_from_guide_points(
        world_g,
        curve_type=curve_type,
        start_angle_deg=p1.angle_deg,
        target_r_min=r_min_req,
    )


def _solve_sbend_angle(
    abs_y: float,
    radius: float,
    euler_p: float,
    min_straight: float,
    use_eff_radius: bool,
    bend_type: str = "euler",
) -> float | None:
    """Find maximum bend angle alpha in (0, pi/2) with L_mid(alpha) >= min_straight."""
    target_s = min_straight + 1e-3

    def l_mid(alpha_rad: float) -> float:
        m = bend_metrics(
            angle_deg=math.degrees(alpha_rad),
            radius=radius,
            p=euler_p,
            use_eff_radius=use_eff_radius,
            bend_type=bend_type,
        )
        return abs_y / math.sin(alpha_rad) - 2.0 * m["tangent_offset"]

    lo = 1e-5
    hi = 0.5 * math.pi - 1e-6

    if l_mid(hi) >= target_s:
        return hi
    if l_mid(lo) < target_s:
        return None

    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if l_mid(mid) >= target_s:
            lo = mid
        else:
            hi = mid
    return lo


def _local_to_world(
    pts_local: list[tuple[float, float]],
    origin_x: float,
    origin_y: float,
    theta1: float,
) -> list[tuple[float, float]]:
    c = math.cos(theta1)
    s = math.sin(theta1)
    return [
        (origin_x + lx * c - ly * s, origin_y + lx * s + ly * c)
        for lx, ly in pts_local
    ]


def _world_to_local(
    x: float, y: float, origin_x: float, origin_y: float, theta1: float
) -> tuple[float, float]:
    dx = x - origin_x
    dy = y - origin_y
    c = math.cos(theta1)
    s = math.sin(theta1)
    return (dx * c + dy * s, -dx * s + dy * c)


def _evaluate_candidates(
    candidate_guides: list[list[tuple[float, float]]],
    radius: float,
    euler_p: float,
    min_straight: float,
    use_eff_radius: bool,
    bend_type: str = "euler",
) -> RouteResult | None:
    """Evaluate guide candidates and return the shortest feasible route."""
    best: RouteResult | None = None
    best_score = float("inf")
    for guide in candidate_guides:
        res = route_from_guide_points(
            guide_points=guide,
            radius=radius,
            euler_p=euler_p,
            min_straight=min_straight,
            use_eff_radius=use_eff_radius,
            min_straight_ends=min_straight,
            bend_type=bend_type,
        )
        if res.ok:
            # Tiny tie-breaker preferring fewer bends when lengths are practically equal
            score = res.total_length + 0.05 * len(res.bends)
            if score < best_score:
                best_score = score
                best = res
    return best


def _facing_guides_local(
    x: float,
    y: float,
    t90: float,
    s: float,
    radius: float,
    euler_p: float,
    use_eff_radius: bool,
    route_mode: str,
    bend_type: str = "euler",
) -> list[list[tuple[float, float]]]:
    """Generate local-frame guide candidates for facing ports: (0,0)@0 -> (x,y)@180."""
    candidates: list[list[tuple[float, float]]] = []
    abs_y = abs(y)
    sy = 1.0 if y >= 0.0 else -1.0

    # 1. Straight collinear
    if abs_y < 1e-4 and x > 1e-4:
        candidates.append([(0.0, 0.0), (x, 0.0)])
        if route_mode in ("auto", "straight", "Z", "euler_90"):
            return candidates

    if route_mode == "straight":
        return candidates

    # 2. Standard 90+90 Z-bend
    if x >= 2.0 * t90 + 2.0 * s - 1e-6 and abs_y >= 2.0 * t90 + s - 1e-6:
        xm = 0.5 * x
        if route_mode in ("auto", "Z", "euler_90", "S", "sbend_euler"):
            candidates.append([(0.0, 0.0), (xm, 0.0), (xm, y), (x, y)])

    # 3. Smooth 2-bend S-bend (only for explicit legacy sbend_euler / S mode)
    if abs_y >= 1e-4 and x > 2.0 * s and route_mode in ("S", "sbend_euler"):
        alpha = _solve_sbend_angle(abs_y, radius, euler_p, s, use_eff_radius, bend_type=bend_type)
        if alpha is not None and alpha < 0.5 * math.pi - 1e-4:
            m = bend_metrics(
                angle_deg=math.degrees(alpha),
                radius=radius,
                p=euler_p,
                use_eff_radius=use_eff_radius,
                bend_type=bend_type,
            )
            t_a = m["tangent_offset"]
            dx_c = abs_y / math.tan(alpha)
            x_min = 2.0 * s + 2.0 * t_a + dx_c
            if x + 1e-5 >= x_min:
                x1 = 0.5 * (x - dx_c)
                x2 = 0.5 * (x + dx_c)
                candidates.append([(0.0, 0.0), (x1, 0.0), (x2, y), (x, y)])

    # 3b. Compact forward 4-bend 90° bump/step (strictly 90° Euler + straight, zero backtracking!)
    if x >= 4.0 * t90 + 3.0 * s - 1e-6 and abs_y < 2.0 * t90 + s:
        x1_fwd = t90 + s
        x2_fwd = x - (t90 + s)
        for side in (sy, -sy):
            if side > 0.0:
                ym = max(0.0, y) + 2.0 * t90 + s
            else:
                ym = min(0.0, y) - (2.0 * t90 + s)
            candidates.append(
                [(0.0, 0.0), (x1_fwd, 0.0), (x1_fwd, ym), (x2_fwd, ym), (x2_fwd, y), (x, y)]
            )

    # 4. Unconditionally feasible 4-bend Manhattan detour (tight X, backward X, or forced Z)
    x_r = max(0.0, x) + t90 + s
    x_l = min(0.0, x) - t90 - s
    if x_r - x_l < 2.0 * t90 + s:
        pad = 0.5 * ((2.0 * t90 + s) - (x_r - x_l)) + 1.0
        x_r += pad
        x_l -= pad

    if abs_y >= 2.0 * (2.0 * t90 + s):
        ym = 0.5 * y
        candidates.append(
            [(0.0, 0.0), (x_r, 0.0), (x_r, ym), (x_l, ym), (x_l, y), (x, y)]
        )
    else:
        for side in (sy, -sy):
            if side > 0.0:
                ym = max(0.0, y) + 2.0 * t90 + s
            else:
                ym = min(0.0, y) - (2.0 * t90 + s)
            candidates.append(
                [(0.0, 0.0), (x_r, 0.0), (x_r, ym), (x_l, ym), (x_l, y), (x, y)]
            )

    return candidates


def _orthogonal_guides_local(
    x: float,
    y: float,
    arrival_north: bool,
    t90: float,
    s: float,
) -> list[list[tuple[float, float]]]:
    """Generate local-frame guide candidates for orthogonal ports.

    P1 = (0,0) leaving +x; P2 = (x,y) arriving +y (if arrival_north) or -y.
    """
    flip = 1.0 if arrival_north else -1.0
    yf = flip * y
    raw_cands: list[list[tuple[float, float]]] = []

    # 1. Direct 1-bend L-route
    if x >= t90 + s - 1e-6 and yf >= t90 + s - 1e-6:
        raw_cands.append([(0.0, 0.0), (x, 0.0), (x, yf)])

    # 2. 3-bend detour going South first then up into P2 (unconditionally feasible!)
    y_low = min(-(2.0 * t90 + s), yf - (t90 + s))
    if x >= 3.0 * t90 + 2.0 * s:
        x1 = min(x - (2.0 * t90 + s), max(t90 + s, 0.5 * x))
        raw_cands.append([(0.0, 0.0), (x1, 0.0), (x1, y_low), (x, y_low), (x, yf)])
    x1_far = max(t90 + s, x + 2.0 * t90 + s)
    raw_cands.append([(0.0, 0.0), (x1_far, 0.0), (x1_far, y_low), (x, y_low), (x, yf)])

    # 3. 3-bend detour going North first when yf is large enough (often shorter when x < t90 + s)
    if yf >= 3.0 * t90 + 2.0 * s:
        y_mid = max(2.0 * t90 + s, min(yf - (t90 + s), 0.5 * yf))
        raw_cands.append([(0.0, 0.0), (x1_far, 0.0), (x1_far, y_mid), (x, y_mid), (x, yf)])

    # Unflip Y if arrival was South
    if flip > 0.0:
        return raw_cands
    return [[(px, -py) for px, py in cand] for cand in raw_cands]


def _same_dir_guides_local(
    x: float,
    y: float,
    t90: float,
    s: float,
) -> list[list[tuple[float, float]]]:
    """Generate local-frame guide candidates for same-direction U-turn ports.

    P1 = (0,0) leaving +x; P2 = (x,y) arriving -x (both outward normals point +x).
    """
    candidates: list[list[tuple[float, float]]] = []
    abs_y = abs(y)
    sy = 1.0 if y >= 0.0 else -1.0

    # 1. Standard 2-bend U-turn
    if abs_y >= 2.0 * t90 + s - 1e-6:
        x_turn = max(0.0, x) + t90 + s
        candidates.append([(0.0, 0.0), (x_turn, 0.0), (x_turn, y), (x, y)])

    # 2. Unconditionally feasible 4-bend U-turn (handles small |y| including y == 0!)
    for side in (sy, -sy):
        if side > 0.0:
            y_out = max(0.0, y) + 2.0 * t90 + s
        else:
            y_out = min(0.0, y) - (2.0 * t90 + s)

        # Go East on middle bridge
        x1_a = t90 + s
        x2_a = max(x1_a + 2.0 * t90 + s, x + t90 + s)
        candidates.append(
            [(0.0, 0.0), (x1_a, 0.0), (x1_a, y_out), (x2_a, y_out), (x2_a, y), (x, y)]
        )

        # Go West on middle bridge (shorter when x is far negative)
        x2_b = x + t90 + s
        x1_b = max(t90 + s, x2_b + 2.0 * t90 + s)
        candidates.append(
            [(0.0, 0.0), (x1_b, 0.0), (x1_b, y_out), (x2_b, y_out), (x2_b, y), (x, y)]
        )

    return candidates


def _solve_ray_anchor(
    px: float,
    py: float,
    dx: float,
    dy: float,
    wx: float,
    wy: float,
    radius: float,
    euler_p: float,
    s: float,
    use_eff_radius: bool,
    bend_type: str = "euler",
) -> tuple[float, float] | None:
    """Compute anchor A = P + t * d along ray (P, d) toward target W so stub >= s + offset."""
    t = s + 0.35 * bend_metrics(90.0, radius, euler_p, use_eff_radius, bend_type=bend_type)["tangent_offset"]
    for _ in range(12):
        ax, ay = px + t * dx, py + t * dy
        vx, vy = wx - ax, wy - ay
        dist = math.hypot(vx, vy)
        if dist < 1e-6:
            return None
        ux, uy = vx / dist, vy / dist
        cross = dx * uy - dy * ux
        dot = max(-1.0, min(1.0, dx * ux + dy * uy))
        ang = math.atan2(cross, dot)
        if abs(ang) < 1e-5:
            return (ax, ay)
        if abs(ang) > math.radians(170.0):
            return None
        off = bend_metrics(
            angle_deg=math.degrees(ang),
            radius=radius,
            p=euler_p,
            use_eff_radius=use_eff_radius,
            bend_type=bend_type,
        )["tangent_offset"]
        t_new = s + off + 0.05
        if abs(t_new - t) < 1e-5:
            return (px + t_new * dx, py + t_new * dy)
        t = t_new
    return (px + t * dx, py + t * dy)


def _plan_with_waypoints(
    p1: _SimplePort,
    p2: _SimplePort,
    waypoints: list[tuple[float, float]],
    radius: float,
    euler_p: float,
    s: float,
    use_eff_radius: bool,
    bend_type: str = "euler",
) -> RouteResult:
    """Route from p1 to p2 through user-supplied intermediate waypoints (strictly loop-free)."""
    wps = [(float(w[0]), float(w[1])) for w in waypoints]
    th1 = math.radians(p1.angle_deg)
    th2 = math.radians(p2.angle_deg)
    d1 = (math.cos(th1), math.sin(th1))
    d2 = (math.cos(th2), math.sin(th2))

    p1_pt = (p1.x, p1.y)
    p2_pt = (p2.x, p2.y)
    t90 = bend_metrics(90.0, radius, euler_p, use_eff_radius, bend_type=bend_type)["tangent_offset"]

    ortho_candidates: list[list[tuple[float, float]]] = []
    anchor_candidates: list[list[tuple[float, float]]] = []

    def _on_ray(p_pt: tuple[float, float], d: tuple[float, float], w: tuple[float, float]) -> bool:
        vx, vy = w[0] - p_pt[0], w[1] - p_pt[1]
        proj = vx * d[0] + vy * d[1]
        perp = abs(vx * d[1] - vy * d[0])
        return proj > 1e-4 and perp < 1e-3

    def _matches_port_tangents(res: RouteResult) -> bool:
        """Reject any candidate whose entry/exit tangents deviate from port outward normals."""
        if not res.ok or len(res.centerline) < 2:
            return False
        s0x = res.centerline[1][0] - res.centerline[0][0]
        s0y = res.centerline[1][1] - res.centerline[0][1]
        l0 = math.hypot(s0x, s0y)
        s1x = res.centerline[-1][0] - res.centerline[-2][0]
        s1y = res.centerline[-1][1] - res.centerline[-2][1]
        l1 = math.hypot(s1x, s1y)
        if l0 < 1e-9 or l1 < 1e-9:
            return False
        dot1 = (s0x * d1[0] + s0y * d1[1]) / l0
        dot2 = (s1x * (-d2[0]) + s1y * (-d2[1])) / l1
        return dot1 >= 0.999 and dot2 >= 0.999

    def _eval_clean(cands: list[list[tuple[float, float]]], max_bend_deg: float = 135.0) -> RouteResult | None:
        best: RouteResult | None = None
        best_score = float("inf")
        for guide in cands:
            res = route_from_guide_points(
                guide_points=guide,
                radius=radius,
                euler_p=euler_p,
                min_straight=s,
                use_eff_radius=use_eff_radius,
                min_straight_ends=s,
                bend_type=bend_type,
            )
            if not res.ok or not _matches_port_tangents(res):
                continue
            if any(abs(b["angle_deg"]) > max_bend_deg for b in res.bends):
                continue
            score = res.total_length + 0.05 * len(res.bends)
            if score < best_score:
                best_score = score
                best = res
        return best

    on_ray1 = _on_ray(p1_pt, d1, wps[0])
    on_ray2 = _on_ray(p2_pt, d2, wps[-1])

    # Candidate 1: direct [P1, *wps, P2] when both ends already align with port rays
    if on_ray1 and on_ray2:
        ortho_candidates.append([p1_pt] + wps + [p2_pt])

    # Build valid start prefix respecting P1's forward ray
    t1_proj = (wps[0][0] - p1.x) * d1[0] + (wps[0][1] - p1.y) * d1[1]
    t2_proj = (wps[-1][0] - p2.x) * d2[0] + (wps[-1][1] - p2.y) * d2[1]
    start_ok_proj = on_ray1 or (t1_proj > s)
    end_ok_proj = on_ray2 or (t2_proj > s)

    def _start_prefix(wp_head: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
        if _on_ray(p1_pt, d1, wp_head[0]):
            return [p1_pt] + wp_head
        tp = (wp_head[0][0] - p1.x) * d1[0] + (wp_head[0][1] - p1.y) * d1[1]
        if tp > s:
            return [p1_pt, (p1.x + tp * d1[0], p1.y + tp * d1[1])] + wp_head
        return None

    # Candidate 2: orthogonal ray projections at ends not already on port rays
    if start_ok_proj and end_ok_proj:
        g_proj: list[tuple[float, float]] = [p1_pt]
        if not on_ray1:
            g_proj.append((p1.x + t1_proj * d1[0], p1.y + t1_proj * d1[1]))
        g_proj.extend(wps)
        if not on_ray2:
            g_proj.append((p2.x + t2_proj * d2[0], p2.y + t2_proj * d2[1]))
        g_proj.append(p2_pt)
        ortho_candidates.append(g_proj)

    # Candidate 2b: extend/trim last waypoint segment to meet P2's ray perpendicularly
    # (prevents small jogs when a user Shift-clicks orthogonal waypoints near P2's axis).
    if not on_ray2:
        prev_w = wps[-2] if len(wps) >= 2 else p1_pt
        last_w = wps[-1]
        svx, svy = last_w[0] - prev_w[0], last_w[1] - prev_w[1]
        sl = math.hypot(svx, svy)
        if sl > 1e-4:
            svx /= sl
            svy /= sl
            if abs(svx * d2[0] + svy * d2[1]) < 0.05:
                det_r = svx * (-d2[1]) - svy * (-d2[0])
                if abs(det_r) > 1e-6:
                    rx, ry = p2.x - prev_w[0], p2.y - prev_w[1]
                    u_seg = (rx * (-d2[1]) - ry * (-d2[0])) / det_r
                    t2_hit = (svx * ry - svy * rx) / det_r
                    if u_seg > 1e-4 and t2_hit > s:
                        snapped_last = (prev_w[0] + u_seg * svx, prev_w[1] + u_seg * svy)
                        wp_body = wps[:-1] + [snapped_last]
                        pref = _start_prefix(wp_body)
                        if pref is not None:
                            ortho_candidates.append(pref + [p2_pt])

    # Candidate 2c: extend/trim first waypoint segment to meet P1's ray perpendicularly
    if not on_ray1 and len(wps) >= 2:
        w0, w1 = wps[0], wps[1]
        svx, svy = w1[0] - w0[0], w1[1] - w0[1]
        sl = math.hypot(svx, svy)
        if sl > 1e-4:
            svx /= sl
            svy /= sl
            if abs(svx * d1[0] + svy * d1[1]) < 0.05:
                det_r = d1[0] * (-svy) - d1[1] * (-svx)
                if abs(det_r) > 1e-6:
                    rx, ry = w0[0] - p1.x, w0[1] - p1.y
                    t1_hit = (rx * (-svy) - ry * (-svx)) / det_r
                    if t1_hit > s:
                        snapped_first = (p1.x + t1_hit * d1[0], p1.y + t1_hit * d1[1])
                        wp_body = [snapped_first] + wps[1:]
                        if on_ray2:
                            ortho_candidates.append([p1_pt] + wp_body + [p2_pt])
                        elif end_ok_proj:
                            ortho_candidates.append(
                                [p1_pt]
                                + wp_body
                                + [(p2.x + t2_proj * d2[0], p2.y + t2_proj * d2[1]), p2_pt]
                            )

    # Candidate 2d: clean 1-elbow orthogonal step when last segment runs parallel to P2's ray
    # (e.g., user Shift-clicked a horizontal leg whose end is slightly closer to P2 than t90+s).
    if not on_ray2 and len(wps) >= 2:
        prev_w = wps[-2]
        last_w = wps[-1]
        svx, svy = last_w[0] - prev_w[0], last_w[1] - prev_w[1]
        sl = math.hypot(svx, svy)
        if sl > 1e-4:
            svx /= sl
            svy /= sl
            # Parallel to P2's arrival axis (-d2) or outward axis (+d2)
            if abs(svx * d2[0] + svy * d2[1]) > 0.995:
                t2_req = max(t2_proj, t90 + s + 0.05)
                elbow_p2 = (p2.x + t2_req * d2[0], p2.y + t2_req * d2[1])
                # Project elbow_p2 onto line(prev_w, sv)
                u_el = (elbow_p2[0] - prev_w[0]) * svx + (elbow_p2[1] - prev_w[1]) * svy
                if u_el > 1e-4:
                    elbow_leg = (prev_w[0] + u_el * svx, prev_w[1] + u_el * svy)
                    wp_body = wps[:-1] + [elbow_leg, elbow_p2]
                    pref = _start_prefix(wp_body)
                    if pref is not None:
                        ortho_candidates.append(pref + [p2_pt])

    # Prefer clean ray-aligned / orthogonal routes before trying arbitrary-angle ray anchors.
    ortho_best = _eval_clean(ortho_candidates, max_bend_deg=135.0)
    if ortho_best is not None:
        return ortho_best

    # Candidate 3: exact fixed-point ray anchors [P1, A1, *wps, A2, P2] (loop-free only!)
    a1 = None if on_ray1 else _solve_ray_anchor(
        p1.x, p1.y, d1[0], d1[1], wps[0][0], wps[0][1], radius, euler_p, s, use_eff_radius, bend_type=bend_type
    )
    a2 = None if on_ray2 else _solve_ray_anchor(
        p2.x, p2.y, d2[0], d2[1], wps[-1][0], wps[-1][1], radius, euler_p, s, use_eff_radius, bend_type=bend_type
    )
    if (on_ray1 or a1 is not None) and (on_ray2 or a2 is not None):
        g_anc: list[tuple[float, float]] = [p1_pt]
        if a1 is not None:
            g_anc.append(a1)
        g_anc.extend(wps)
        if a2 is not None:
            g_anc.append(a2)
        g_anc.append(p2_pt)
        anchor_candidates.append(g_anc)

    # Candidate 4: mixed (one orthogonal projection, one fixed-point anchor)
    if not on_ray1 and a1 is not None and not on_ray2 and end_ok_proj:
        anchor_candidates.append(
            [p1_pt, a1] + wps + [(p2.x + t2_proj * d2[0], p2.y + t2_proj * d2[1]), p2_pt]
        )
    if not on_ray2 and a2 is not None and not on_ray1 and start_ok_proj:
        anchor_candidates.append(
            [p1_pt, (p1.x + t1_proj * d1[0], p1.y + t1_proj * d1[1])] + wps + [a2, p2_pt]
        )

    best = _eval_clean(anchor_candidates, max_bend_deg=120.0)
    if best is not None:
        return best

    # No loop-free route fits (radius, euler_p, min_straight): return clear diagnostic
    # instead of generating multi-loop pretzel detours!
    fallback = (
        ortho_candidates[0]
        if ortho_candidates
        else (anchor_candidates[0] if anchor_candidates else ([p1_pt] + wps + [p2_pt]))
    )
    failed = route_from_guide_points(
        guide_points=fallback,
        radius=radius,
        euler_p=euler_p,
        min_straight=s,
        use_eff_radius=use_eff_radius,
        min_straight_ends=s,
    )
    if not failed.ok:
        return RouteResult(
            ok=False,
            guide_points=fallback,
            error=(
                f"{failed.error} (R={radius:.1f}, min_straight={s:.1f} um — "
                f"space waypoints farther or reduce Radius)"
            ),
        )
    return RouteResult(
        ok=False,
        guide_points=fallback,
        error=(
            f"Waypoints violate port tangent or require bends > 120 deg "
            f"(leave >= {t90 + s:.1f} um straight out of each port for R={radius:.1f}, min_straight={s:.1f} um)"
        ),
    )


def _plan_arbitrary_angle(
    p1: _SimplePort,
    p2: _SimplePort,
    radius: float,
    euler_p: float,
    s: float,
    use_eff_radius: bool,
    bend_type: str = "euler",
) -> RouteResult | None:
    """Plan route between ports with arbitrary non-orthogonal angles."""
    th1 = math.radians(p1.angle_deg)
    th2 = math.radians(p2.angle_deg)
    d1 = (math.cos(th1), math.sin(th1))
    d2 = (math.cos(th2), math.sin(th2))
    p1_pt = (p1.x, p1.y)
    p2_pt = (p2.x, p2.y)

    candidates: list[list[tuple[float, float]]] = []

    # Strategy 1: Direct single-bend intersection of outgoing ray 1 and backward ray 2
    # Solve P1 + t1*d1 = P2 + t2*d2 => t1*d1 + t2*(-d2) = P2 - P1
    det = d1[0] * (-d2[1]) - d1[1] * (-d2[0])
    if abs(det) > 1e-6:
        rx, ry = p2.x - p1.x, p2.y - p1.y
        t1 = (rx * (-d2[1]) - ry * (-d2[0])) / det
        t2 = (d1[0] * ry - d1[1] * rx) / det
        if t1 > s and t2 > s:
            corner = (p1.x + t1 * d1[0], p1.y + t1 * d1[1])
            candidates.append([p1_pt, corner, p2_pt])

    # Strategy 2: Direct 2-bend connection [P1, A1, A2, P2] via fixed-point ray stubs
    t90 = bend_metrics(90.0, radius, euler_p, use_eff_radius, bend_type=bend_type)["tangent_offset"]
    t1_fp = s + 0.3 * t90
    t2_fp = s + 0.3 * t90
    fp_ok = True
    for _ in range(15):
        a1x, a1y = p1.x + t1_fp * d1[0], p1.y + t1_fp * d1[1]
        a2x, a2y = p2.x + t2_fp * d2[0], p2.y + t2_fp * d2[1]
        vx, vy = a2x - a1x, a2y - a1y
        dist = math.hypot(vx, vy)
        if dist < 1e-4:
            fp_ok = False
            break
        ux, uy = vx / dist, vy / dist
        ang1 = math.atan2(d1[0] * uy - d1[1] * ux, max(-1.0, min(1.0, d1[0] * ux + d1[1] * uy)))
        # Arrival into P2 is -d2
        ang2 = math.atan2(
            ux * (-d2[1]) - uy * (-d2[0]),
            max(-1.0, min(1.0, ux * (-d2[0]) + uy * (-d2[1]))),
        )
        if abs(ang1) > math.radians(170.0) or abs(ang2) > math.radians(170.0):
            fp_ok = False
            break
        off1 = (
            0.0
            if abs(ang1) < 1e-5
            else bend_metrics(math.degrees(ang1), radius, euler_p, use_eff_radius, bend_type=bend_type)["tangent_offset"]
        )
        off2 = (
            0.0
            if abs(ang2) < 1e-5
            else bend_metrics(math.degrees(ang2), radius, euler_p, use_eff_radius, bend_type=bend_type)["tangent_offset"]
        )
        nt1 = s + off1 + 0.05
        nt2 = s + off2 + 0.05
        if abs(nt1 - t1_fp) < 1e-5 and abs(nt2 - t2_fp) < 1e-5:
            t1_fp, t2_fp = nt1, nt2
            break
        t1_fp, t2_fp = nt1, nt2
    if fp_ok:
        candidates.append(
            [
                p1_pt,
                (p1.x + t1_fp * d1[0], p1.y + t1_fp * d1[1]),
                (p2.x + t2_fp * d2[0], p2.y + t2_fp * d2[1]),
                p2_pt,
            ]
        )

    # Strategy 3: Universal align-to-P2-axes solver!
    for delta_target in (math.pi, 0.5 * math.pi, -0.5 * math.pi, 0.0):
        th_align = th2 + delta_target
        turn = _wrap_rad_pi(th_align - th1)
        if abs(turn) < 1e-5 or abs(abs(turn) - math.pi) < 1e-3:
            continue
        m_al = bend_metrics(
            angle_deg=math.degrees(turn),
            radius=radius,
            p=euler_p,
            use_eff_radius=use_eff_radius,
            bend_type=bend_type,
        )
        off_al = m_al["tangent_offset"]
        c1 = (p1.x + (s + off_al) * d1[0], p1.y + (s + off_al) * d1[1])
        u_al = (math.cos(th_align), math.sin(th_align))
        q1 = (c1[0] + off_al * u_al[0], c1[1] + off_al * u_al[1])
        v1 = _SimplePort(q1[0], q1[1], math.degrees(th_align) % 360.0)
        sub = plan_route_ports(
            v1,
            p2,
            radius=radius,
            euler_p=euler_p,
            min_straight=s,
            route_mode="auto",
            waypoints=None,
            use_eff_radius=use_eff_radius,
            bend_type=bend_type,
        )
        if sub.ok and len(sub.guide_points) >= 2:
            candidates.append([p1_pt, c1] + list(sub.guide_points[1:]))

    return _evaluate_candidates(candidates, radius, euler_p, s, use_eff_radius, bend_type=bend_type)


def plan_route_ports(
    port1: Any,
    port2: Any,
    radius: float = 80.0,
    euler_p: float = 0.5,
    min_straight: float = 5.0,
    route_mode: str = "auto",
    waypoints: list[tuple[float, float]] | None = None,
    use_eff_radius: bool = True,
    start_taper_len: float = 0.0,
    end_taper_len: float = 0.0,
    bend_type: str = "euler",
) -> RouteResult:
    """Plan a smooth multi-bend Euler / C2 S-bend waveguide route connecting `port1` to `port2`.

    Duck-types `port1` and `port2` via `.x`, `.y`, `.angle_deg` (and optional `.width`),
    where `angle_deg` is each port's outward normal pointing INTO the route.
    When ``start_taper_len > 0`` or ``end_taper_len > 0``, reserves exact straight linear-taper
    sections outside the main uniform waveguide so tapers NEVER consume straight ``min_straight``
    or spill into bends.
    """
    valid_modes = {
        "auto",
        "straight",
        "L",
        "S",
        "Z",
        "U",
        "sbend_bezier",
        "sbend_sine",
        "sbend_euler",
        "euler_90",
    }
    if route_mode not in valid_modes:
        return RouteResult(
            ok=False,
            error=f"Unsupported route_mode {route_mode!r}; expected one of {sorted(valid_modes)}",
        )
    radius = float(radius)
    euler_p = float(euler_p)
    s = max(0.0, float(min_straight))
    lt1 = max(0.0, float(start_taper_len))
    lt2 = max(0.0, float(end_taper_len))
    if radius <= 0.0:
        return RouteResult(ok=False, error=f"radius must be > 0, got {radius}")
    if not (0.0 <= euler_p <= 1.0):
        return RouteResult(ok=False, error=f"euler_p must be in [0, 1], got {euler_p}")

    p1 = _SimplePort(
        x=float(port1.x),
        y=float(port1.y),
        angle_deg=float(port1.angle_deg) % 360.0,
        width=float(getattr(port1, "width", 0.70)),
    )
    p2 = _SimplePort(
        x=float(port2.x),
        y=float(port2.y),
        angle_deg=float(port2.angle_deg) % 360.0,
        width=float(getattr(port2, "width", 0.70)),
    )

    # 0. Dedicated straight linear-taper reservation (tapers NEVER occupy bends or pure straight length!)
    if lt1 > 1e-6 or lt2 > 1e-6:
        th1_t = math.radians(p1.angle_deg)
        th2_t = math.radians(p2.angle_deg)
        d1_t = (math.cos(th1_t), math.sin(th1_t))
        d2_t = (math.cos(th2_t), math.sin(th2_t))
        phi_t = _wrap_rad_pi(th2_t - th1_t)
        lx_t, ly_t = _world_to_local(p2.x, p2.y, p1.x, p1.y, th1_t)

        # Collinear facing straight ports: check if gap fits lt1 + lt2 directly
        if not waypoints and abs(abs(phi_t) - math.pi) < 1e-3 and abs(ly_t) < 1e-4 and lx_t > 1e-4:
            res_st = route_from_guide_points(
                [(p1.x, p1.y), (p2.x, p2.y)],
                radius=radius,
                euler_p=euler_p,
                min_straight=s,
                use_eff_radius=use_eff_radius,
            )
            if lx_t + 1e-5 < lt1 + lt2:
                res_st.warnings.append(
                    f"Straight gap {lx_t:.2f} um < total taper {lt1 + lt2:.2f} um (tapers clamped to fit)"
                )
            return res_st

        p1_in = _SimplePort(
            x=p1.x + lt1 * d1_t[0],
            y=p1.y + lt1 * d1_t[1],
            angle_deg=p1.angle_deg,
            width=p1.width,
        )
        p2_in = _SimplePort(
            x=p2.x + lt2 * d2_t[0],
            y=p2.y + lt2 * d2_t[1],
            angle_deg=p2.angle_deg,
            width=p2.width,
        )
        inner = plan_route_ports(
            p1_in,
            p2_in,
            radius=radius,
            euler_p=euler_p,
            min_straight=s,
            route_mode=route_mode,
            waypoints=waypoints,
            use_eff_radius=use_eff_radius,
            start_taper_len=0.0,
            end_taper_len=0.0,
            bend_type=bend_type,
        )
        if not inner.ok:
            return inner

        if inner.curve_type in VALID_SBEND_CURVES:
            ig = inner.guide_points
            p_sb0 = ig[0] if len(ig) == 2 else ig[1]
            p_sb1 = ig[-1] if len(ig) == 2 else ig[-2]
            outer_g: list[tuple[float, float]] = [(p1.x, p1.y)]
            if math.hypot(p_sb0[0] - outer_g[-1][0], p_sb0[1] - outer_g[-1][1]) > 1e-5:
                outer_g.append(p_sb0)
            if math.hypot(p_sb1[0] - outer_g[-1][0], p_sb1[1] - outer_g[-1][1]) > 1e-5:
                outer_g.append(p_sb1)
            if math.hypot(p2.x - outer_g[-1][0], p2.y - outer_g[-1][1]) > 1e-5:
                outer_g.append((p2.x, p2.y))
            r_min_req = (
                radius
                if not use_eff_radius
                else float(bend_metrics(90.0, radius, euler_p, True, bend_type=bend_type)["R_min"])
            )
            return route_sbend_from_guide_points(
                outer_g,
                curve_type=inner.curve_type,
                start_angle_deg=p1.angle_deg,
                target_r_min=r_min_req,
            )

        # Bend polyline: P1 -> P1_in is collinear with first leg, P2_in -> P2 collinear with last leg
        ig = inner.guide_points
        if len(ig) >= 3:
            outer_g = [(p1.x, p1.y)] + list(ig[1:-1]) + [(p2.x, p2.y)]
        else:
            outer_g = [(p1.x, p1.y), (p2.x, p2.y)]
        return route_from_guide_points(
            outer_g,
            radius=radius,
            euler_p=euler_p,
            min_straight=s,
            use_eff_radius=use_eff_radius,
            min_straight_ends=s,
            bend_type=bend_type,
        )

    # 1. Explicit intermediate waypoints
    if waypoints:
        return _plan_with_waypoints(p1, p2, list(waypoints), radius, euler_p, s, use_eff_radius, bend_type=bend_type)

    # 2. Transform into Port 1's local frame (P1 at origin leaving +x)
    th1 = math.radians(p1.angle_deg)
    th2 = math.radians(p2.angle_deg)
    phi = _wrap_rad_pi(th2 - th1)
    lx, ly = _world_to_local(p2.x, p2.y, p1.x, p1.y, th1)
    t90 = bend_metrics(90.0, radius, euler_p, use_eff_radius, bend_type=bend_type)["tangent_offset"]

    ang_tol = 1e-3  # ~0.057 deg tolerance for canonical cardinal alignments

    # 3. Straight mode strict check
    if route_mode == "straight":
        if abs(abs(phi) - math.pi) < ang_tol and abs(ly) < 1e-3 and lx > 1e-4:
            return route_from_guide_points(
                [(p1.x, p1.y), (p2.x, p2.y)],
                radius=radius,
                euler_p=euler_p,
                min_straight=s,
                use_eff_radius=use_eff_radius,
                bend_type=bend_type,
            )
        return RouteResult(
            ok=False,
            guide_points=[(p1.x, p1.y), (p2.x, p2.y)],
            error="route_mode='straight' requires collinear facing ports",
        )

    # 4. Canonical facing / anti-parallel ports (phi ~ +-180 deg)
    if abs(abs(phi) - math.pi) < ang_tol:
        if math.hypot(lx, ly) < 1e-5:
            return RouteResult(
                ok=False,
                guide_points=[(p1.x, p1.y), (p2.x, p2.y)],
                error="Facing ports are already coincident (zero gap)",
            )
        # Collinear forward ports -> pure straight waveguide in all modes
        if abs(ly) < 1e-4 and lx > 1e-4:
            return route_from_guide_points(
                [(p1.x, p1.y), (p2.x, p2.y)],
                radius=radius,
                euler_p=euler_p,
                min_straight=s,
                use_eff_radius=use_eff_radius,
                bend_type=bend_type,
            )

        # Explicit C2 zero-curvature S-bend modes (Quintic Bezier or Raised-Sine)
        if route_mode in ("sbend_bezier", "sbend_sine", "S"):
            ctype = "sbend_sine" if route_mode == "sbend_sine" else "sbend_bezier"
            sb_res = _plan_c2_sbend(p1, p2, ctype, radius, euler_p, s, use_eff_radius, bend_type=bend_type)
            if sb_res is not None and sb_res.ok:
                return sb_res
            local_cands = _facing_guides_local(
                lx, ly, t90, s, radius, euler_p, use_eff_radius, "euler_90", bend_type=bend_type
            )
            world_cands = [_local_to_world(c, p1.x, p1.y, th1) for c in local_cands]
            res = _evaluate_candidates(world_cands, radius, euler_p, s, use_eff_radius, bend_type=bend_type)
            if res is not None:
                return res

        # Auto mode
        elif route_mode == "auto":
            if not (lx >= 2.0 * t90 + 2.0 * s - 1e-6 and abs(ly) >= 2.0 * t90 + s - 1e-6):
                sb_res = _plan_c2_sbend(
                    p1, p2, "sbend_bezier", radius, euler_p, s, use_eff_radius, bend_type=bend_type
                )
                if sb_res is not None and sb_res.ok and not sb_res.warnings:
                    return sb_res
            local_cands = _facing_guides_local(
                lx, ly, t90, s, radius, euler_p, use_eff_radius, "euler_90", bend_type=bend_type
            )
            world_cands = [_local_to_world(c, p1.x, p1.y, th1) for c in local_cands]
            res = _evaluate_candidates(world_cands, radius, euler_p, s, use_eff_radius, bend_type=bend_type)
            if res is not None:
                return res

        # Explicit 90-deg Bend + Straight ("euler_90" / "Z") or legacy "sbend_euler"
        else:
            mode_eff = "sbend_euler" if route_mode == "sbend_euler" else "euler_90"
            local_cands = _facing_guides_local(
                lx, ly, t90, s, radius, euler_p, use_eff_radius, mode_eff, bend_type=bend_type
            )
            world_cands = [_local_to_world(c, p1.x, p1.y, th1) for c in local_cands]
            res = _evaluate_candidates(world_cands, radius, euler_p, s, use_eff_radius, bend_type=bend_type)
            if res is not None:
                return res

    # 5. Canonical orthogonal ports (phi ~ +-90 deg) -> always 90-deg bend + straight
    elif abs(abs(phi) - 0.5 * math.pi) < ang_tol:
        arrival_north = phi < 0.0
        local_cands = _orthogonal_guides_local(lx, ly, arrival_north, t90, s)
        world_cands = [_local_to_world(c, p1.x, p1.y, th1) for c in local_cands]
        res = _evaluate_candidates(world_cands, radius, euler_p, s, use_eff_radius, bend_type=bend_type)
        if res is not None:
            return res

    # 6. Canonical same-direction ports (phi ~ 0 deg, U-turn) -> ALWAYS 90-deg bend + straight
    elif abs(phi) < ang_tol:
        local_cands = _same_dir_guides_local(lx, ly, t90, s)
        world_cands = [_local_to_world(c, p1.x, p1.y, th1) for c in local_cands]
        res = _evaluate_candidates(world_cands, radius, euler_p, s, use_eff_radius, bend_type=bend_type)
        if res is not None:
            return res

    # 7. Arbitrary non-orthogonal angles (e.g. +8 deg EC to 180 deg rib port)
    else:
        res = _plan_arbitrary_angle(p1, p2, radius, euler_p, s, use_eff_radius, bend_type=bend_type)
        if res is not None:
            return res

    return RouteResult(
        ok=False,
        guide_points=[(p1.x, p1.y), (p2.x, p2.y)],
        error=(
            f"No feasible Euler route found from ({p1.x:.3f}, {p1.y:.3f})@{p1.angle_deg:.2f}deg "
            f"to ({p2.x:.3f}, {p2.y:.3f})@{p2.angle_deg:.2f}deg "
            f"(R={radius}, p={euler_p}, min_straight={s}, mode={route_mode!r})"
        ),
    )


def draw_route_to_cell(
    layout: Any,
    cell: Any,
    route_result: RouteResult,
    cross_section: CrossSection,
    start_width: float | None = None,
    end_width: float | None = None,
    taper_length: float = 10.0,
    draw_error_layer: tuple[int, int] = DEFAULT_ERROR_LAYER,
) -> bool:
    """Render a `RouteResult` into `cell` across all cross-section layers.

    On failure (`not route_result.ok`), draws the fallback guide spine and an
    error text label on `draw_error_layer` and returns `False`.
    """
    kdb = get_kdb()
    dbu = float(layout.dbu)

    if route_result.ok and len(route_result.centerline) >= 2:
        regions = build_multilayer_waveguide_regions(
            points=route_result.centerline,
            cross_section=cross_section,
            dbu=dbu,
            start_width=start_width,
            end_width=end_width,
            taper_length=taper_length,
        )
        for (layer, datatype), reg in regions.items():
            li = layout.layer(int(layer), int(datatype))
            cell.shapes(li).insert(reg)
        return True

    # Draw diagnostic error marker & fallback guide spine
    li_err = layout.layer(int(draw_error_layer[0]), int(draw_error_layer[1]))
    err_msg = f"ROUTE ERROR: {route_result.error or 'Infeasible route'}"

    if len(route_result.guide_points) >= 2:
        dpts = [kdb.DPoint(float(x), float(y)) for x, y in route_result.guide_points]
        w_err = max(cross_section.core_width, 0.5)
        cell.shapes(li_err).insert(kdb.DPath(dpts, w_err))
        mid = route_result.guide_points[len(route_result.guide_points) // 2]
        cell.shapes(li_err).insert(kdb.DText(err_msg, kdb.DTrans(float(mid[0]), float(mid[1]))))
    else:
        cell.shapes(li_err).insert(kdb.DText(err_msg, kdb.DTrans(0.0, 0.0)))

    return False


def format_segments_summary(
    segments: list[dict[str, Any]],
    total_length: float | None = None,
    prefix: str = "Total",
) -> str:
    """Format a compact 1-line summary of total length, min bend radius, and per-segment lengths."""
    if not segments:
        return f"{prefix}: 0.000 um"
    tot = float(total_length) if total_length is not None else sum(float(s["length"]) for s in segments)
    parts: list[str] = []
    r_mins: list[float] = []
    r_effs: list[float] = []
    for seg in segments:
        kind = seg.get("kind")
        if kind == "euler":
            ang = abs(float(seg.get("angle_deg", 0.0)))
            ang_s = f"{ang:.0f}deg" if abs(ang - round(ang)) < 0.05 else f"{ang:.1f}deg"
            bt_s = ",TOPIC" if seg.get("bend_type") == "topic" else ""
            rm = seg.get("R_min")
            re = seg.get("R_eff")
            if rm is not None:
                r_mins.append(float(rm))
            if re is not None:
                r_effs.append(float(re))
            if rm is not None and re is not None:
                parts.append(f"B{seg['index']}({ang_s}{bt_s},Rmin={float(rm):.1f}/Reff={float(re):.1f}):{float(seg['length']):.3f}")
            else:
                parts.append(f"B{seg['index']}({ang_s}{bt_s}):{float(seg['length']):.3f}")
        elif kind in VALID_SBEND_CURVES:
            short_tag = "C2-Bez" if kind == "sbend_bezier" else "C2-Sin"
            rm = seg.get("R_min")
            if rm is not None:
                r_mins.append(float(rm))
                parts.append(f"SBend({short_tag},Rmin={float(rm):.1f}):{float(seg['length']):.3f}")
            else:
                parts.append(f"SBend({short_tag}):{float(seg['length']):.3f}")
        elif kind == "taper":
            w_from = float(seg.get("w_from", 0.0))
            w_to = float(seg.get("w_to", 0.0))
            parts.append(f"T{seg['index']}({w_from:.2f}->{w_to:.2f}):{float(seg['length']):.3f}")
        else:
            parts.append(f"S{seg['index']}:{float(seg['length']):.3f}")
    rad_note = ""
    if r_mins and r_effs:
        rad_note = f" [Rmin={min(r_mins):.1f}, Reff={min(r_effs):.1f} um]"
    elif r_mins:
        rad_note = f" [Rmin={min(r_mins):.1f} um]"
    if len(parts) == 1:
        return f"{prefix}: {tot:.3f} um{rad_note} ({parts[0]} um)"
    return f"{prefix}: {tot:.3f} um{rad_note} | " + " + ".join(parts) + " um"


def split_taper_segments_list(
    segments: list[dict[str, Any]],
    core_width: float,
    start_width: float | None = None,
    end_width: float | None = None,
    taper_length: float = 0.0,
) -> list[dict[str, Any]]:
    """Split linear-taper sections out of leading/trailing straight segments so pure straight
    lengths S1/S2/... strictly exclude the linear taper length.
    """
    tl = max(0.0, float(taper_length))
    wc = float(core_width)
    has_start = (
        start_width is not None
        and float(start_width) > 0.0
        and abs(float(start_width) - wc) > 1e-4
        and tl > 1e-4
    )
    has_end = (
        end_width is not None
        and float(end_width) > 0.0
        and abs(float(end_width) - wc) > 1e-4
        and tl > 1e-4
    )
    if (not has_start and not has_end) or not segments:
        return [dict(seg) for seg in segments]

    segs = [dict(seg) for seg in segments]
    out: list[dict[str, Any]] = []
    t_idx = 0

    # Check if start and end both live on the SAME single straight segment
    if len(segs) == 1 and segs[0].get("kind") == "straight":
        s0 = segs[0]
        p0 = s0["start"]
        p1 = s0["end"]
        tot_l = float(s0["length"])
        hdg = float(s0.get("angle_deg", 0.0))
        dx = (p1[0] - p0[0]) / max(tot_l, 1e-12)
        dy = (p1[1] - p0[1]) / max(tot_l, 1e-12)
        if has_start and has_end:
            lt1 = min(tl, 0.5 * tot_l)
            lt2 = min(tl, 0.5 * tot_l)
        elif has_start:
            lt1 = min(tl, tot_l)
            lt2 = 0.0
        else:
            lt1 = 0.0
            lt2 = min(tl, tot_l)
        pa = (p0[0] + lt1 * dx, p0[1] + lt1 * dy)
        pb = (p1[0] - lt2 * dx, p1[1] - lt2 * dy)
        if lt1 > 1e-4:
            t_idx += 1
            ws = float(start_width)  # type: ignore[arg-type]
            out.append(
                {
                    "kind": "taper",
                    "index": t_idx,
                    "length": lt1,
                    "start": p0,
                    "end": pa,
                    "midpoint": (0.5 * (p0[0] + pa[0]), 0.5 * (p0[1] + pa[1])),
                    "angle_deg": hdg,
                    "w_from": ws,
                    "w_to": wc,
                    "label": f"T{t_idx}({ws:.2f}->{wc:.2f}): {lt1:.3f} um",
                }
            )
        rem_l = max(0.0, tot_l - lt1 - lt2)
        if rem_l >= 1e-4:
            out.append(
                {
                    "kind": "straight",
                    "index": 1,
                    "length": rem_l,
                    "start": pa,
                    "end": pb,
                    "midpoint": (0.5 * (pa[0] + pb[0]), 0.5 * (pa[1] + pb[1])),
                    "angle_deg": hdg,
                    "label": f"S1: {rem_l:.3f} um",
                }
            )
        if lt2 > 1e-4:
            t_idx += 1
            we = float(end_width)  # type: ignore[arg-type]
            out.append(
                {
                    "kind": "taper",
                    "index": t_idx,
                    "length": lt2,
                    "start": pb,
                    "end": p1,
                    "midpoint": (0.5 * (pb[0] + p1[0]), 0.5 * (pb[1] + p1[1])),
                    "angle_deg": hdg,
                    "w_from": wc,
                    "w_to": we,
                    "label": f"T{t_idx}({wc:.2f}->{we:.2f}): {lt2:.3f} um",
                }
            )
        return out

    # Multi-segment route: split leading straight for start taper, trailing straight for end taper
    for idx, seg in enumerate(segs):
        if idx == 0 and has_start and seg.get("kind") == "straight":
            p0 = seg["start"]
            p1 = seg["end"]
            sl = float(seg["length"])
            lt1 = min(tl, sl)
            dx = (p1[0] - p0[0]) / max(sl, 1e-12)
            dy = (p1[1] - p0[1]) / max(sl, 1e-12)
            pa = (p0[0] + lt1 * dx, p0[1] + lt1 * dy)
            t_idx += 1
            ws = float(start_width)  # type: ignore[arg-type]
            out.append(
                {
                    "kind": "taper",
                    "index": t_idx,
                    "length": lt1,
                    "start": p0,
                    "end": pa,
                    "midpoint": (0.5 * (p0[0] + pa[0]), 0.5 * (p0[1] + pa[1])),
                    "angle_deg": float(seg.get("angle_deg", 0.0)),
                    "w_from": ws,
                    "w_to": wc,
                    "label": f"T{t_idx}({ws:.2f}->{wc:.2f}): {lt1:.3f} um",
                }
            )
            rem = sl - lt1
            if rem >= 1e-4:
                ns = dict(seg)
                ns["start"] = pa
                ns["length"] = rem
                ns["midpoint"] = (0.5 * (pa[0] + p1[0]), 0.5 * (pa[1] + p1[1]))
                out.append(ns)
        elif idx == len(segs) - 1 and has_end and seg.get("kind") == "straight":
            p0 = seg["start"]
            p1 = seg["end"]
            sl = float(seg["length"])
            lt2 = min(tl, sl)
            dx = (p1[0] - p0[0]) / max(sl, 1e-12)
            dy = (p1[1] - p0[1]) / max(sl, 1e-12)
            pb = (p1[0] - lt2 * dx, p1[1] - lt2 * dy)
            rem = sl - lt2
            if rem >= 1e-4:
                ns = dict(seg)
                ns["end"] = pb
                ns["length"] = rem
                ns["midpoint"] = (0.5 * (p0[0] + pb[0]), 0.5 * (p0[1] + pb[1]))
                out.append(ns)
            t_idx += 1
            we = float(end_width)  # type: ignore[arg-type]
            out.append(
                {
                    "kind": "taper",
                    "index": t_idx,
                    "length": lt2,
                    "start": pb,
                    "end": p1,
                    "midpoint": (0.5 * (pb[0] + p1[0]), 0.5 * (pb[1] + p1[1])),
                    "angle_deg": float(seg.get("angle_deg", 0.0)),
                    "w_from": wc,
                    "w_to": we,
                    "label": f"T{t_idx}({wc:.2f}->{we:.2f}): {lt2:.3f} um",
                }
            )
        else:
            out.append(seg)

    # Re-index remaining straight segments cleanly as S1, S2, ...
    s_num = 0
    for seg in out:
        if seg.get("kind") == "straight":
            s_num += 1
            seg["index"] = s_num
            seg["label"] = f"S{s_num}: {float(seg['length']):.3f} um"
    return out


def apply_taper_segments(
    route_result: RouteResult,
    core_width: float,
    start_width: float | None = None,
    end_width: float | None = None,
    taper_length: float = 0.0,
) -> RouteResult:
    """Return a shallow copy of ``route_result`` whose ``segments`` separate linear tapers from straights."""
    if not route_result.ok or not route_result.segments:
        return route_result
    new_segs = split_taper_segments_list(
        route_result.segments,
        core_width=core_width,
        start_width=start_width,
        end_width=end_width,
        taper_length=taper_length,
    )
    return RouteResult(
        ok=route_result.ok,
        centerline=route_result.centerline,
        guide_points=route_result.guide_points,
        total_length=route_result.total_length,
        bends=route_result.bends,
        segments=new_segs,
        warnings=list(route_result.warnings),
        error=route_result.error,
        curve_type=route_result.curve_type,
        sbend_angle_deg=route_result.sbend_angle_deg,
        bend_type=route_result.bend_type,
    )


def _raw_straight_segments(pts: list[tuple[float, float]]) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    s_idx = 0
    for i in range(len(pts) - 1):
        p0, p1 = pts[i], pts[i + 1]
        length = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        if length < 1e-6:
            continue
        s_idx += 1
        mid = (0.5 * (p0[0] + p1[0]), 0.5 * (p0[1] + p1[1]))
        hdg = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0])) % 360.0
        segs.append(
            {
                "kind": "straight",
                "index": s_idx,
                "length": length,
                "start": p0,
                "end": p1,
                "midpoint": mid,
                "angle_deg": hdg,
                "label": f"S{s_idx}: {length:.3f} um",
            }
        )
    return segs


def analyze_live_guide_segments(
    guide_points: list[tuple[float, float]],
    radius: float = 80.0,
    euler_p: float = 0.5,
    min_straight: float = 5.0,
    use_eff_radius: bool = True,
    bend_type: str = "euler",
) -> dict[str, Any]:
    """Compute real-time per-segment straight & bend lengths while interactively drawing.

    Behavior:
      - If all bends fit geometrically (even before the trailing stub hits ``min_straight``),
        returns the exact alternating straight + bend breakdown (``S1, B1, S2, ...``).
      - If the latest dragged leg is still shorter than the bend's tangent offset (radius),
        keeps any already-formed earlier bends and omits the unformed bend so
        only the straight segments (and already-valid bends) are shown until radius is met.
    """
    pts = [(float(p[0]), float(p[1])) for p in guide_points]
    if len(pts) < 2:
        return {
            "feasible_strict": False,
            "feasible_geom": False,
            "has_euler": False,
            "centerline": pts,
            "segments": [],
            "total_length": 0.0,
            "summary": "Total: 0.000 um",
            "error": "Need at least 2 points",
        }

    # 1. Strict check (satisfies both bend radius and min_straight)
    res_strict = route_from_guide_points(
        pts,
        radius=radius,
        euler_p=euler_p,
        min_straight=min_straight,
        use_eff_radius=use_eff_radius,
        min_straight_ends=min_straight,
        bend_type=bend_type,
    )
    if res_strict.ok:
        return {
            "feasible_strict": True,
            "feasible_geom": True,
            "has_euler": len(res_strict.bends) > 0,
            "centerline": res_strict.centerline,
            "segments": res_strict.segments,
            "total_length": res_strict.total_length,
            "summary": format_segments_summary(res_strict.segments, res_strict.total_length),
            "error": "",
        }

    # 2. Geometric-radius-only check (bends fit, trailing straight may still be < min_straight)
    res_geom = route_from_guide_points(
        pts,
        radius=radius,
        euler_p=euler_p,
        min_straight=0.0,
        use_eff_radius=use_eff_radius,
        min_straight_ends=0.0,
        bend_type=bend_type,
    )
    if res_geom.ok:
        return {
            "feasible_strict": False,
            "feasible_geom": True,
            "has_euler": len(res_geom.bends) > 0,
            "centerline": res_geom.centerline,
            "segments": res_geom.segments,
            "total_length": res_geom.total_length,
            "summary": format_segments_summary(res_geom.segments, res_geom.total_length),
            "error": res_strict.error,
        }

    # 3. Latest leg has not reached bend radius yet: preserve committed prefix bends if valid,
    #    append the live straight leg without showing the unformed bend.
    if len(pts) >= 3:
        pref_res = route_from_guide_points(
            pts[:-1],
            radius=radius,
            euler_p=euler_p,
            min_straight=0.0,
            use_eff_radius=use_eff_radius,
            min_straight_ends=0.0,
            bend_type=bend_type,
        )
        if pref_res.ok:
            segs = [dict(s) for s in pref_res.segments]
            p_prev, p_last = pts[-2], pts[-1]
            last_len = math.hypot(p_last[0] - p_prev[0], p_last[1] - p_prev[1])
            s_count = sum(1 for s in segs if s["kind"] == "straight")
            if last_len >= 1e-6:
                s_count += 1
                mid = (0.5 * (p_prev[0] + p_last[0]), 0.5 * (p_prev[1] + p_last[1]))
                hdg = math.degrees(math.atan2(p_last[1] - p_prev[1], p_last[0] - p_prev[0])) % 360.0
                segs.append(
                    {
                        "kind": "straight",
                        "index": s_count,
                        "length": last_len,
                        "start": p_prev,
                        "end": p_last,
                        "midpoint": mid,
                        "angle_deg": hdg,
                        "label": f"S{s_count}: {last_len:.3f} um",
                    }
                )
            tot = pref_res.total_length + last_len
            cl = list(pref_res.centerline)
            if last_len >= 1e-6:
                cl.append(p_last)
            return {
                "feasible_strict": False,
                "feasible_geom": False,
                "has_euler": len(pref_res.bends) > 0,
                "centerline": cl,
                "segments": segs,
                "total_length": tot,
                "summary": format_segments_summary(segs, tot, prefix="Raw Total"),
                "error": res_strict.error,
            }

    # 4. Fallback: raw straight segments only (no Euler bend arcs displayed)
    raw_segs = _raw_straight_segments(pts)
    raw_tot = sum(float(s["length"]) for s in raw_segs)
    return {
        "feasible_strict": False,
        "feasible_geom": False,
        "has_euler": False,
        "centerline": pts,
        "segments": raw_segs,
        "total_length": raw_tot,
        "summary": format_segments_summary(raw_segs, raw_tot, prefix="Raw Total"),
        "error": res_strict.error,
    }


VALID_LABEL_STYLES: tuple[str, ...] = ("card", "per_segment", "total_only")


def _segment_anchor_frame(
    seg: dict[str, Any],
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], float]:
    """Return ``(anchor_pt, unit_tangent, unit_normal, seg_span)`` for a route segment."""
    mx, my = float(seg["midpoint"][0]), float(seg["midpoint"][1])
    if seg.get("kind") == "euler" and "corner" in seg:
        cx, cy = float(seg["corner"][0]), float(seg["corner"][1])
        vx, vy = cx - mx, cy - my
        vl = math.hypot(vx, vy)
        if vl > 1e-6:
            nx, ny = vx / vl, vy / vl
            return ((mx, my), (ny, -nx), (nx, ny), float(seg.get("length", 0.0)))
    s0 = seg.get("start")
    s1 = seg.get("end")
    if s0 is not None and s1 is not None:
        dx, dy = float(s1[0]) - float(s0[0]), float(s1[1]) - float(s0[1])
        sl = math.hypot(dx, dy)
        if sl > 1e-6:
            tx, ty = dx / sl, dy / sl
            nx, ny = -ty, tx
            return ((mx, my), (tx, ty), (nx, ny), sl)
    return ((mx, my), (1.0, 0.0), (0.0, 1.0), float(seg.get("length", 0.0)))


def _segment_label_position(
    seg: dict[str, Any], offset_um: float
) -> tuple[float, float]:
    """Compute a clean offset label anchor beside a straight segment or Euler bend."""
    (mx, my), _, (nx, ny), _ = _segment_anchor_frame(seg)
    return (mx + float(offset_um) * nx, my + float(offset_um) * ny)


def _format_card_segment_item(seg: dict[str, Any], include_radius: bool = False) -> str:
    """Compact per-segment string without redundant radius repetition."""
    kind = seg.get("kind")
    length = float(seg.get("length", 0.0))
    if kind == "euler":
        ang = abs(float(seg.get("angle_deg", 0.0)))
        ang_s = f"{ang:.0f}deg" if abs(ang - round(ang)) < 0.05 else f"{ang:.1f}deg"
        bt_s = ",TOPIC" if seg.get("bend_type") == "topic" else ""
        if include_radius and seg.get("R_min") is not None and seg.get("R_eff") is not None:
            return f"B{seg['index']}({ang_s}{bt_s},R{float(seg['R_min']):.1f}/{float(seg['R_eff']):.1f}): {length:.3f} um"
        return f"B{seg['index']}({ang_s}{bt_s}): {length:.3f} um"
    if kind in VALID_SBEND_CURVES:
        short_tag = "C2-Bez" if kind == "sbend_bezier" else "C2-Sin"
        if include_radius and seg.get("R_min") is not None:
            return f"SBend({short_tag},Rmin={float(seg['R_min']):.1f}): {length:.3f} um"
        return f"SBend({short_tag}): {length:.3f} um"
    if kind == "taper":
        w_from = float(seg.get("w_from", 0.0))
        w_to = float(seg.get("w_to", 0.0))
        return f"T{seg['index']}({w_from:.2f}->{w_to:.2f}): {length:.3f} um"
    return f"S{seg['index']}: {length:.3f} um"


def format_route_header(
    segments: list[dict[str, Any]],
    total_length: float | None = None,
    route_index: int | None = None,
    prefix: str = "Total",
) -> str:
    """Compact 1-line route header: ``[#N ]Total: L um [[TOPIC, ]Rmin=..., Reff=... um]``."""
    tot = float(total_length) if total_length is not None else sum(float(s["length"]) for s in segments)
    r_mins = [float(s["R_min"]) for s in segments if s.get("R_min") is not None]
    r_effs = [float(s["R_eff"]) for s in segments if s.get("kind") == "euler" and s.get("R_eff") is not None]
    has_topic = any(s.get("kind") == "euler" and s.get("bend_type") == "topic" for s in segments)
    b_tag = "TOPIC, " if has_topic else ""
    rad_note = ""
    if r_mins and r_effs:
        rad_note = f" [{b_tag}Rmin={min(r_mins):.1f}, Reff={min(r_effs):.1f} um]"
    elif r_mins:
        rad_note = f" [{b_tag}Rmin={min(r_mins):.1f} um]"
    tag = f"#{int(route_index)} " if route_index is not None else ""
    return f"{tag}{prefix}: {tot:.3f} um{rad_note}"


def format_route_card_text(
    segments: list[dict[str, Any]],
    total_length: float | None = None,
    route_index: int | None = None,
    prefix: str = "Total",
    max_line_chars: int = 46,
) -> str:
    """Format a compact multi-line route summary card (header + wrapped segment breakdown)."""
    header = format_route_header(segments, total_length=total_length, route_index=route_index, prefix=prefix)
    if not segments:
        return header
    items = [_format_card_segment_item(seg, include_radius=False) for seg in segments]
    lines = [header]
    cur = ""
    cur_count = 0
    for item in items:
        if not cur:
            cur = item
            cur_count = 1
        elif cur_count < 2 and len(cur) + 3 + len(item) <= max_line_chars:
            cur = f"{cur} | {item}"
            cur_count += 1
        else:
            lines.append(cur)
            cur = item
            cur_count = 1
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _estimate_text_dims(text: str, line_h: float) -> tuple[float, float]:
    """Estimate bounding box (width, height) in um for a multi-line KLayout DText."""
    lines = str(text).splitlines() or [""]
    max_chars = max(len(ln) for ln in lines)
    w = max(float(max_chars) * 0.68 * float(line_h), 2.0 * float(line_h))
    h = max(float(len(lines)) * 1.32 * float(line_h), 1.2 * float(line_h))
    return (w, h)


def _aligned_text_box(
    x: float, y: float, w: float, h: float, halign: str, valign: str
) -> tuple[float, float, float, float]:
    if halign == "left":
        x0, x1 = x, x + w
    elif halign == "right":
        x0, x1 = x - w, x
    else:
        x0, x1 = x - 0.5 * w, x + 0.5 * w
    if valign == "bottom":
        y0, y1 = y, y + h
    elif valign == "top":
        y0, y1 = y - h, y
    else:
        y0, y1 = y - 0.5 * h, y + 0.5 * h
    return (x0, y0, x1, y1)


def _box_overlap_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    pad: float = 0.0,
) -> float:
    ox = min(a[2] + pad, b[2] + pad) - max(a[0] - pad, b[0] - pad)
    if ox <= 0.0:
        return 0.0
    oy = min(a[3] + pad, b[3] + pad) - max(a[1] - pad, b[1] - pad)
    if oy <= 0.0:
        return 0.0
    return ox * oy


def _sample_polyline_points(
    pts: list[tuple[float, float]], step_um: float = 20.0
) -> list[tuple[float, float]]:
    if len(pts) < 2:
        return list(pts)
    out: list[tuple[float, float]] = [pts[0]]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        seg_l = math.hypot(x1 - x0, y1 - y0)
        if seg_l < 1e-6:
            continue
        n = max(1, int(math.ceil(seg_l / max(step_um, 2.0))))
        for k in range(1, n + 1):
            t = k / n
            out.append((x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
    return out


def _alignment_for_offset(dx: float, dy: float) -> tuple[str, str]:
    """Choose text anchor alignment so text grows strictly AWAY from the anchor point."""
    if abs(dy) >= abs(dx):
        return ("center", "bottom" if dy >= 0.0 else "top")
    return ("left" if dx >= 0.0 else "right", "center")


def _extract_existing_text_boxes(
    cell: Any, li: int, line_h: float
) -> list[tuple[float, float, float, float]]:
    """Collect estimated 2D bounding boxes of existing DText annotations on layer ``li``."""
    boxes: list[tuple[float, float, float, float]] = []
    if cell is None or li < 0:
        return boxes
    for sh in cell.shapes(li).each():
        if not sh.is_text():
            continue
        dt = sh.dtext
        tx = float(dt.trans.disp.x)
        ty = float(dt.trans.disp.y)
        ha_s = str(getattr(dt, "halign", "left")).lower()
        va_s = str(getattr(dt, "valign", "bottom")).lower()
        if "center" in ha_s:
            ha = "center"
        elif "right" in ha_s:
            ha = "right"
        else:
            ha = "left"
        if "center" in va_s:
            va = "center"
        elif "top" in va_s:
            va = "top"
        else:
            va = "bottom"
        w, h = _estimate_text_dims(dt.string, line_h)
        boxes.append(_aligned_text_box(tx, ty, w, h, ha, va))
    return boxes


def _place_collision_free_label(
    anchor: tuple[float, float],
    tangent: tuple[float, float],
    normal: tuple[float, float],
    seg_span: float,
    text: str,
    line_h: float,
    base_offset: float,
    occupied_boxes: list[tuple[float, float, float, float]],
    obstacle_pts: list[tuple[float, float]],
    prefer_sign: float = 1.0,
) -> tuple[float, float, str, str, tuple[float, float, float, float]]:
    """Find a collision-free placement near ``anchor`` that avoids existing text boxes and waveguides."""
    ax, ay = float(anchor[0]), float(anchor[1])
    tx, ty = float(tangent[0]), float(tangent[1])
    nx, ny = float(normal[0]), float(normal[1])
    w, h = _estimate_text_dims(text, line_h)
    pad = 0.28 * line_h
    wg_pad = 0.35 * line_h

    d0 = max(float(base_offset), 0.9 * line_h)
    step_n = max(h * 0.85, 2.0 * line_h)
    t_shifts = [0.0]
    if seg_span > 2.5 * line_h:
        s_a = min(0.26 * seg_span, 160.0)
        s_b = min(0.42 * seg_span, 280.0)
        t_shifts.extend([s_a, -s_a, s_b, -s_b])

    signs = (prefer_sign, -prefer_sign) if prefer_sign >= 0.0 else (-1.0, 1.0)
    candidates: list[tuple[float, float, float]] = []  # (x, y, side_penalty)

    for ring_idx, dist_n in enumerate((
        d0,
        d0 + 0.9 * step_n,
        d0 + 1.9 * step_n,
        d0 + 3.1 * step_n,
        d0 + 4.5 * step_n,
        d0 + 6.2 * step_n,
    )):
        for s_idx, sgn in enumerate(signs):
            for ts in t_shifts if ring_idx < 4 else (0.0,):
                cx = ax + sgn * dist_n * nx + ts * tx
                cy = ay + sgn * dist_n * ny + ts * ty
                candidates.append((cx, cy, 0.05 * s_idx))

    # Cardinal escape candidates for tight multi-route bundles
    for mult in (1.5, 3.0, 5.0, 7.5):
        r_c = d0 + mult * step_n
        for dx_c, dy_c in ((0.0, r_c), (0.0, -r_c), (r_c, 0.0), (-r_c, 0.0)):
            candidates.append((ax + dx_c, ay + dy_c, 0.15))

    best_score = float("inf")
    best_result: tuple[float, float, str, str, tuple[float, float, float, float]] | None = None

    for cx, cy, side_pen in candidates:
        ha, va = _alignment_for_offset(cx - ax, cy - ay)
        box = _aligned_text_box(cx, cy, w, h, ha, va)

        overlap_pen = 0.0
        for ob in occupied_boxes:
            ov = _box_overlap_area(box, ob, pad=pad)
            if ov > 0.0:
                overlap_pen += 1e5 + 1e3 * ov

        wg_hits = 0
        bx0, by0, bx1, by1 = box[0] - wg_pad, box[1] - wg_pad, box[2] + wg_pad, box[3] + wg_pad
        for px, py in obstacle_pts:
            if bx0 <= px <= bx1 and by0 <= py <= by1:
                wg_hits += 1

        dist_pen = math.hypot(cx - ax, cy - ay)
        score = overlap_pen + 3500.0 * wg_hits + dist_pen + side_pen * line_h
        if score < best_score:
            best_score = score
            best_result = (cx, cy, ha, va, box)
            if overlap_pen == 0.0 and wg_hits == 0 and dist_pen <= d0 + 1e-3:
                break

    assert best_result is not None
    return best_result


def _insert_aligned_dtext(
    kdb: Any,
    shapes: Any,
    text: str,
    x: float,
    y: float,
    halign: str,
    valign: str,
) -> Any:
    dt = kdb.DText(str(text), kdb.DTrans(kdb.DVector(float(x), float(y))))
    h_cls = getattr(kdb, "HAlign", None)
    v_cls = getattr(kdb, "VAlign", None)
    if h_cls is not None:
        h_map = {
            "left": getattr(h_cls, "HAlignLeft", None),
            "center": getattr(h_cls, "HAlignCenter", None),
            "right": getattr(h_cls, "HAlignRight", None),
        }
        if h_map.get(halign) is not None:
            dt.halign = h_map[halign]
    if v_cls is not None:
        v_map = {
            "bottom": getattr(v_cls, "VAlignBottom", None),
            "center": getattr(v_cls, "VAlignCenter", None),
            "top": getattr(v_cls, "VAlignTop", None),
        }
        if v_map.get(valign) is not None:
            dt.valign = v_map[valign]
    shapes.insert(dt)
    return dt


def _choose_primary_anchor_segment(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the cleanest, longest segment of a route as the anchor for a summary card."""
    for seg in segments:
        if seg.get("kind") in VALID_SBEND_CURVES:
            return seg
    straights = [s for s in segments if s.get("kind") == "straight"]
    if straights:
        return max(straights, key=lambda s: float(s.get("length", 0.0)))
    return max(segments, key=lambda s: float(s.get("length", 0.0)))


def _infer_label_line_height(
    route_result: RouteResult,
    cell: Any = None,
    ref_span_um: float | None = None,
) -> float:
    """Compute realistic layout-micron line height matching typical KLayout viewing zoom."""
    if ref_span_um is not None and float(ref_span_um) > 0.0:
        span = float(ref_span_um)
    else:
        pts = route_result.centerline or route_result.guide_points
        if pts:
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            r_span = max(max(xs) - min(xs), max(ys) - min(ys), 80.0)
        else:
            r_span = 200.0
        c_span = r_span
        if cell is not None:
            try:
                bb = cell.dbbox()
                if not bb.empty():
                    c_span = max(float(bb.width()), float(bb.height()), r_span)
            except Exception:
                pass
        span = max(r_span, min(c_span, 2400.0))
    return max(14.0, min(38.0, 0.015 * span))


def draw_length_annotations_to_cell(
    layout: Any,
    cell: Any,
    route_result: RouteResult,
    length_layer: tuple[int, int] = DEFAULT_LENGTH_LAYER,
    label_offset: float = 6.0,
    label_style: str = "card",
    route_index: int | None = None,
    occupied_boxes: list[tuple[float, float, float, float]] | None = None,
    obstacle_points: list[tuple[float, float]] | None = None,
    ref_span_um: float | None = None,
) -> int:
    """Write clean, collision-avoiding length annotations onto ``length_layer`` (default 90/2).

    Supported ``label_style`` modes:
      - ``"card"`` (default): 1 compact multi-line summary card per route + subtle leader line
        pointing to the primary segment midpoint (zero clutter & zero overlap across dense routes).
      - ``"per_segment"``: Clean non-overlapping label per segment + concise 1-line Total header.
      - ``"total_only"``: Ultra-clean single-line Total + Rmin/Reff tag + leader line.
    """
    if not route_result.ok or not route_result.segments:
        return 0
    style = str(label_style or "card").strip().lower()
    if style not in VALID_LABEL_STYLES:
        style = "card"

    kdb = get_kdb()
    li = layout.layer(int(length_layer[0]), int(length_layer[1]))
    shapes = cell.shapes(li)
    line_h = _infer_label_line_height(route_result, cell=cell, ref_span_um=ref_span_um)

    if occupied_boxes is None:
        occ = _extract_existing_text_boxes(cell, li, line_h)
    else:
        occ = occupied_boxes

    obs_pts = list(obstacle_points) if obstacle_points is not None else []
    obs_pts.extend(_sample_polyline_points(route_result.centerline, step_um=max(12.0, 0.6 * line_h)))

    def _emit_label_with_leader(
        anchor: tuple[float, float],
        tangent: tuple[float, float],
        normal: tuple[float, float],
        seg_span: float,
        text: str,
        prefer_sign: float = 1.0,
    ) -> None:
        lx, ly, ha, va, box = _place_collision_free_label(
            anchor=anchor,
            tangent=tangent,
            normal=normal,
            seg_span=seg_span,
            text=text,
            line_h=line_h,
            base_offset=max(float(label_offset), 0.75 * line_h),
            occupied_boxes=occ,
            obstacle_pts=obs_pts,
            prefer_sign=prefer_sign,
        )
        _insert_aligned_dtext(kdb, shapes, text, lx, ly, ha, va)
        occ.append(box)
        dist = math.hypot(lx - anchor[0], ly - anchor[1])
        gap = 0.35 * line_h
        if dist > max(float(label_offset) * 1.1, gap + 4.0):
            ux = (lx - anchor[0]) / dist
            uy = (ly - anchor[1]) / dist
            tip_x = lx - gap * ux
            tip_y = ly - gap * uy
            if math.hypot(tip_x - anchor[0], tip_y - anchor[1]) > 2.0:
                leader_w = max(0.35, min(1.2, 0.03 * line_h))
                shapes.insert(
                    kdb.DPath(
                        [
                            kdb.DPoint(float(anchor[0]), float(anchor[1])),
                            kdb.DPoint(float(tip_x), float(tip_y)),
                        ],
                        leader_w,
                    )
                )

    count = 0
    if style == "card":
        primary = _choose_primary_anchor_segment(route_result.segments)
        anc, tan, nrm, span = _segment_anchor_frame(primary)
        card_txt = format_route_card_text(
            route_result.segments,
            total_length=route_result.total_length,
            route_index=route_index,
            prefix="Total",
        )
        _emit_label_with_leader(anc, tan, nrm, span, card_txt, prefer_sign=1.0)
        count += 1
        return count

    if style == "total_only":
        primary = _choose_primary_anchor_segment(route_result.segments)
        anc, tan, nrm, span = _segment_anchor_frame(primary)
        hdr_txt = format_route_header(
            route_result.segments,
            total_length=route_result.total_length,
            route_index=route_index,
            prefix="Total",
        )
        _emit_label_with_leader(anc, tan, nrm, span, hdr_txt, prefer_sign=1.0)
        count += 1
        return count

    # style == "per_segment": clean collision-free label per segment + concise 1-line Total header
    primary = _choose_primary_anchor_segment(route_result.segments)
    for seg in route_result.segments:
        anc, tan, nrm, span = _segment_anchor_frame(seg)
        seg_txt = _format_card_segment_item(seg, include_radius=True)
        _emit_label_with_leader(anc, tan, nrm, span, seg_txt, prefer_sign=1.0)
        count += 1

    anc_p, tan_p, nrm_p, span_p = _segment_anchor_frame(primary)
    hdr_txt = format_route_header(
        route_result.segments,
        total_length=route_result.total_length,
        route_index=route_index,
        prefix="Total",
    )
    _emit_label_with_leader(anc_p, tan_p, nrm_p, span_p, hdr_txt, prefer_sign=-1.0)
    count += 1
    return count


def tune_route_for_integer_length(
    route_result: RouteResult,
    radius: float,
    euler_p: float = 0.5,
    min_straight: float = 5.0,
    use_eff_radius: bool = True,
    step_um: float = 1.0,
    dbu: float = 0.001,
    max_rel_radius_change: float = 0.20,
    min_stub_start: float = 0.0,
    min_stub_end: float = 0.0,
    bend_type: str | None = None,
) -> tuple[RouteResult, float, bool]:
    """Fine-tune bend ``radius`` analytically (without moving devices!) so total route length
    is an exact integer multiple of ``step_um`` (default 1.0 um).

    Respects ``min_stub_start`` and ``min_stub_end`` (e.g. dedicated linear taper lengths)
    so bends/S-bends never encroach into taper sections.
    Returns ``(best_route_result, used_radius, was_tuned)``.
    """
    r0 = float(radius)
    step = max(float(step_um), 0.001)
    lt1 = max(0.0, float(min_stub_start))
    lt2 = max(0.0, float(min_stub_end))
    btype = str(bend_type or getattr(route_result, "bend_type", "euler") or "euler")
    if not route_result.ok or not route_result.bends or len(route_result.guide_points) < 2:
        return (route_result, r0, False)

    # Quantize guide vertices to layout DBU first so PCell reconstruction matches identically
    if dbu > 0.0:
        q_guides = [
            (round(float(x) / dbu) * dbu, round(float(y) / dbu) * dbu)
            for x, y in route_result.guide_points
        ]
    else:
        q_guides = [(float(x), float(y)) for x, y in route_result.guide_points]

    # 0. C2 zero-curvature S-bend integer-length solver (vary extra stubs >= lt1/lt2, zero device move!)
    if route_result.curve_type in VALID_SBEND_CURVES:
        ctype = route_result.curve_type
        p0 = q_guides[0]
        p3 = q_guides[-1]
        th1 = math.radians(float(route_result.sbend_angle_deg))
        if len(q_guides) == 4 and math.hypot(q_guides[1][0] - p0[0], q_guides[1][1] - p0[1]) > 1e-6:
            th1 = math.atan2(q_guides[1][1] - p0[1], q_guides[1][0] - p0[0])
            s1_init = math.hypot(q_guides[1][0] - p0[0], q_guides[1][1] - p0[1])
            s2_init = math.hypot(p3[0] - q_guides[2][0], p3[1] - q_guides[2][1])
            extra0 = max(0.0, min(s1_init - lt1, s2_init - lt2))
        elif len(q_guides) == 3:
            extra0 = 0.0
        else:
            extra0 = 0.0
        ang_deg = math.degrees(th1) % 360.0
        lx, ly = _world_to_local(p3[0], p3[1], p0[0], p0[1], th1)
        lx_avail = lx - lt1 - lt2
        if lx_avail <= 2.0 or abs(ly) < 1e-4:
            return (route_result, r0, False)

        r_min_req = (
            r0
            if not use_eff_radius
            else float(bend_metrics(90.0, r0, euler_p, True, bend_type=btype)["R_min"])
        )

        def _build_c2_guides(extra_stub: float) -> list[tuple[float, float]] | None:
            s1_l = lt1 + extra_stub
            s2_l = lt2 + extra_stub
            if extra_stub < 0.0 or lx - s1_l - s2_l <= 1.0:
                return None
            loc_pts: list[tuple[float, float]] = [(0.0, 0.0)]
            if s1_l >= max(0.5 * dbu, 1e-5):
                loc_pts.append((s1_l, 0.0))
            if s2_l >= max(0.5 * dbu, 1e-5):
                loc_pts.append((lx - s2_l, ly))
            loc_pts.append((lx, ly))
            w_raw = _local_to_world(loc_pts, p0[0], p0[1], th1)
            w_g: list[tuple[float, float]] = [p0]
            for pt in w_raw[1:-1]:
                if dbu > 0.0:
                    w_g.append((round(pt[0] / dbu) * dbu, round(pt[1] / dbu) * dbu))
                else:
                    w_g.append(pt)
            w_g.append(p3)
            return w_g

        def _eval_c2_metrics(extra_stub: float) -> tuple[float, float, list[tuple[float, float]]] | None:
            """Fast analytical arc-length & R_min evaluation (zero polyline sampling during bisection)."""
            w_g = _build_c2_guides(extra_stub)
            if w_g is None:
                return None
            if len(w_g) == 2:
                p_sb0, p_sb1 = w_g[0], w_g[1]
                l_s1, l_s2 = 0.0, 0.0
                th_u = th1
            elif len(w_g) == 3:
                pa, pm, pb = w_g
                dhx, dhy = math.cos(th1), math.sin(th1)
                v1x, v1y = pm[0] - pa[0], pm[1] - pa[1]
                v2x, v2y = pb[0] - pm[0], pb[1] - pm[1]
                d1_l = math.hypot(v1x, v1y)
                d2_l = math.hypot(v2x, v2y)
                perp1 = abs(v1x * dhy - v1y * dhx) if d1_l > 1e-6 else float("inf")
                perp2 = abs(v2x * dhy - v2y * dhx) if d2_l > 1e-6 else float("inf")
                if d1_l > 1e-6 and perp1 <= perp2 and perp1 < 1e-2 and (v1x * dhx + v1y * dhy) > 0.0:
                    th_u = math.atan2(v1y, v1x)
                    p_sb0, p_sb1 = pm, pb
                    l_s1, l_s2 = d1_l, 0.0
                elif d2_l > 1e-6 and perp2 < 1e-2 and (v2x * dhx + v2y * dhy) > 0.0:
                    th_u = math.atan2(v2y, v2x)
                    p_sb0, p_sb1 = pa, pm
                    l_s1, l_s2 = 0.0, d2_l
                else:
                    return None
            else:
                pa, p1_q, p2_q, pb = w_g
                v1x, v1y = p1_q[0] - pa[0], p1_q[1] - pa[1]
                v2x, v2y = pb[0] - p2_q[0], pb[1] - p2_q[1]
                l_s1 = math.hypot(v1x, v1y)
                l_s2 = math.hypot(v2x, v2y)
                th_u = math.atan2(v1y, v1x) if l_s1 > 1e-6 else (math.atan2(v2y, v2x) if l_s2 > 1e-6 else th1)
                p_sb0, p_sb1 = p1_q, p2_q

            dx_sb, dy_sb = _world_to_local(p_sb1[0], p_sb1[1], p_sb0[0], p_sb0[1], th_u)
            if dx_sb <= 1e-4:
                return None
            if abs(dy_sb) < 1e-4:
                return (math.hypot(p3[0] - p0[0], p3[1] - p0[1]), float("inf"), w_g)
            m_sb = sbend_metrics(dx_sb, dy_sb, curve_type=ctype)
            return (l_s1 + float(m_sb["L_total"]) + l_s2, float(m_sb["R_min"]), w_g)

        def _finalize_c2(w_g: list[tuple[float, float]]) -> RouteResult | None:
            cand = route_sbend_from_guide_points(
                w_g, curve_type=ctype, start_angle_deg=ang_deg, target_r_min=r_min_req
            )
            return cand if cand.ok else None

        base_m = _eval_c2_metrics(extra0)
        if base_m is None:
            return (route_result, r0, False)
        l0, base_rmin_raw, base_wg = base_m
        nearest = round(l0 / step) * step
        if abs(l0 - nearest) < 5e-4:
            fin0 = _finalize_c2(base_wg)
            return (fin0 if fin0 is not None else route_result, r0, False)

        base_rmin = base_rmin_raw if math.isfinite(base_rmin_raw) else r_min_req
        rmin_floor = min(base_rmin, r_min_req) * 0.80

        # Prefer decreasing extra_stub toward 0 (never encroaching into lt1/lt2!) to hit floor(L0)
        target_floor = math.floor(l0 / step) * step
        if extra0 > 1e-4 and target_floor >= step:
            m_zero = _eval_c2_metrics(0.0)
            if m_zero is not None and m_zero[0] <= target_floor + 5e-4:
                lo, hi = 0.0, extra0
                best_m = m_zero
                for _ in range(32):
                    mid = 0.5 * (lo + hi)
                    cm = _eval_c2_metrics(mid)
                    if cm is None:
                        hi = mid
                        continue
                    best_m = cm
                    if abs(cm[0] - target_floor) < 5e-4:
                        fin = _finalize_c2(cm[2])
                        if fin is not None:
                            return (fin, r0, True)
                        break
                    if cm[0] < target_floor:
                        lo = mid
                    else:
                        hi = mid
                if abs(best_m[0] - target_floor) < 2e-3:
                    fin = _finalize_c2(best_m[2])
                    if fin is not None:
                        return (fin, r0, True)

        # Otherwise increase extra_stub slightly (shrinking span) to hit ceil(L0) while protecting R_min
        target_ceil = math.ceil(l0 / step) * step
        max_extra_geom = max(extra0, 0.275 * lx_avail)
        hi_extra = None
        for frac in (0.10, 0.25, 0.45, 0.70, 1.0):
            s_try = extra0 + frac * (max_extra_geom - extra0)
            cm = _eval_c2_metrics(s_try)
            if cm is None or not math.isfinite(cm[1]):
                break
            if cm[1] < rmin_floor:
                break
            if cm[0] >= target_ceil - 5e-4:
                hi_extra = s_try
                break
        if hi_extra is not None:
            lo, hi = extra0, hi_extra
            best_m = base_m
            for _ in range(32):
                mid = 0.5 * (lo + hi)
                cm = _eval_c2_metrics(mid)
                if cm is None:
                    hi = mid
                    continue
                best_m = cm
                if abs(cm[0] - target_ceil) < 5e-4:
                    fin = _finalize_c2(cm[2])
                    if fin is not None:
                        return (fin, r0, True)
                    break
                if cm[0] < target_ceil:
                    lo = mid
                else:
                    hi = mid
            if abs(best_m[0] - target_ceil) < 2e-3:
                fin = _finalize_c2(best_m[2])
                if fin is not None:
                    return (fin, r0, True)

        fin_base = _finalize_c2(base_wg)
        return (fin_base if fin_base is not None else route_result, r0, False)

    if len(q_guides) < 3:
        return (route_result, r0, False)

    base = route_from_guide_points(
        q_guides,
        radius=r0,
        euler_p=euler_p,
        min_straight=min_straight,
        use_eff_radius=use_eff_radius,
        min_straight_ends=min_straight,
        bend_type=btype,
    )
    ref_res = base if (base.ok and base.bends) else route_result
    l0 = float(ref_res.total_length)
    nearest = round(l0 / step) * step
    if base.ok and abs(l0 - nearest) < 5e-4:
        return (base, r0, False)

    def _end_tapers_preserved(cand: RouteResult) -> bool:
        if not cand.ok or not cand.segments:
            return False
        if lt1 > 1e-4:
            if cand.segments[0].get("kind") != "straight" or float(cand.segments[0]["length"]) + 1e-4 < lt1 + min_straight:
                return False
        if lt2 > 1e-4:
            if cand.segments[-1].get("kind") != "straight" or float(cand.segments[-1]["length"]) + 1e-4 < lt2 + min_straight:
                return False
        return True

    # 1. Exact analytical linear R_eff fine-tuning (1-shot closed form for fixed guide points)
    if base.ok and base.bends:
        slope = sum(
            (float(b["L_total"]) - 2.0 * float(b["tangent_offset"])) for b in base.bends
        ) / r0
        if abs(slope) >= 1e-6:
            r_lo = max(1.0, r0 * (1.0 - max_rel_radius_change))
            r_hi = r0 * (1.0 + max_rel_radius_change)
            max_abs_dr = max(5.0, r0 * max_rel_radius_change)

            cand_targets: list[float] = []
            for t in (nearest, math.floor(l0 / step) * step, math.ceil(l0 / step) * step):
                if t >= step and all(abs(t - prev) > 1e-6 for prev in cand_targets):
                    cand_targets.append(t)
            cand_targets.sort(key=lambda t: abs(t - l0))

            for target_l in cand_targets:
                r_cand = r0 + (target_l - l0) / slope
                if r_cand < r_lo or r_cand > r_hi or abs(r_cand - r0) > max_abs_dr:
                    continue
                cand_res = route_from_guide_points(
                    q_guides,
                    radius=r_cand,
                    euler_p=euler_p,
                    min_straight=min_straight,
                    use_eff_radius=use_eff_radius,
                    min_straight_ends=min_straight,
                    bend_type=btype,
                )
                if cand_res.ok and _end_tapers_preserved(cand_res) and abs(cand_res.total_length - target_l) < 1e-3:
                    return (cand_res, float(r_cand), True)

    # 2. S-bend exact angle solver: for a 2-bend symmetric facing S-bend (|angle| < 89.5 deg),
    # decreasing alpha slightly keeps R_eff = r0 exact, increases middle straight S2,
    # and smoothly shortens total_length to hit exact floor(L0) without moving either device!
    if (
        len(q_guides) == 4
        and len(ref_res.bends) == 2
        and abs(ref_res.bends[0]["angle_deg"] + ref_res.bends[1]["angle_deg"]) < 0.1
        and abs(ref_res.bends[0]["angle_deg"]) < 89.5
    ):
        p0, p1_g, p2_g, p3 = q_guides
        th1 = math.atan2(p1_g[1] - p0[1], p1_g[0] - p0[0])
        lx, ly = _world_to_local(p3[0], p3[1], p0[0], p0[1], th1)
        abs_y = abs(ly)
        if lx > 2.0 * min_straight and abs_y > 1e-3:
            alpha_hi = math.radians(abs(float(ref_res.bends[0]["angle_deg"])))
            target_l = math.floor(l0 / step) * step

            def _eval_sbend_alpha(alpha_rad: float) -> RouteResult | None:
                dx_c = abs_y / math.tan(alpha_rad)
                lx_inner = lx - lt1 - lt2
                x1_loc = lt1 + 0.5 * (lx_inner - dx_c)
                x2_loc = x1_loc + dx_c
                if x1_loc <= lt1 + min_straight or (lx - x2_loc) <= lt2 + min_straight:
                    return None
                loc_g = [(0.0, 0.0), (x1_loc, 0.0), (x2_loc, ly), (lx, ly)]
                w_g = _local_to_world(loc_g, p0[0], p0[1], th1)
                if dbu > 0.0:
                    w_g = [(round(x / dbu) * dbu, round(y / dbu) * dbu) for x, y in w_g]
                r_try = route_from_guide_points(
                    w_g,
                    radius=r0,
                    euler_p=euler_p,
                    min_straight=min_straight,
                    use_eff_radius=use_eff_radius,
                    min_straight_ends=min_straight,
                    bend_type=btype,
                )
                return r_try if (r_try.ok and _end_tapers_preserved(r_try)) else None

            # Find feasible lower bound alpha_lo where L(alpha_lo) <= target_l
            alpha_lo = None
            for frac in (0.95, 0.85, 0.70, 0.55, 0.40, 0.25, 0.15):
                cand = _eval_sbend_alpha(alpha_hi * frac)
                if cand is not None and cand.total_length <= target_l:
                    alpha_lo = alpha_hi * frac
                    break
            if alpha_lo is not None:
                lo, hi = alpha_lo, alpha_hi
                best_sb: RouteResult | None = None
                for _ in range(36):
                    mid = 0.5 * (lo + hi)
                    cand = _eval_sbend_alpha(mid)
                    if cand is None:
                        lo = mid
                        continue
                    best_sb = cand
                    if abs(cand.total_length - target_l) < 5e-4:
                        return (cand, r0, True)
                    if cand.total_length > target_l:
                        hi = mid
                    else:
                        lo = mid
                if best_sb is not None and abs(best_sb.total_length - target_l) < 2e-3:
                    return (best_sb, r0, True)

    return (ref_res, r0, False)


def advance_manual_straight_waypoints(
    start_pt: tuple[float, float],
    start_angle_deg: float,
    waypoints: list[tuple[float, float]],
    wp_meta: list[dict[str, Any]],
    direction_vec: tuple[float, float] | None,
    straight_len: float,
    radius: float = 80.0,
    euler_p: float = 0.5,
    use_eff_radius: bool = True,
    dbu: float = 0.001,
    min_len_um: float = 1.0,
    start_taper_len: float = 0.0,
    bend_type: str = "euler",
) -> dict[str, Any]:
    """Compute updated ``(waypoints, wp_meta)`` after adding a manual straight segment of exact length.

    Does not mutate input lists, so callers can use it identically for live preview and commit:
      - Leg 1 (no waypoints yet): advances ``straight_len`` along ``start_angle_deg``.
      - Collinear extension: extends the trailing straight by ``+straight_len``.
      - 90° (or non-collinear) turn: when the previous waypoint came from manual straight entry
        (or is too short to fit the new bend's tangent offset), advances the previous corner by
        ``+t_bend`` so the previous straight length stays exact and the new Euler bend starts
        right at the previous tip, then places the new tip at ``C + (t_bend + straight_len) * u_new``
        so the new straight segment has exact length ``straight_len``.
    """
    L = float(straight_len)
    if L < float(min_len_um) - 1e-9:
        return {
            "ok": False,
            "error": f"Straight length must be >= {float(min_len_um):.1f} um (got {L:.3f} um)",
            "waypoints": list(waypoints),
            "wp_meta": [dict(m) for m in wp_meta],
        }

    def _q(x: float, y: float) -> tuple[float, float]:
        if dbu > 0.0:
            return (round(float(x) / dbu) * dbu, round(float(y) / dbu) * dbu)
        return (float(x), float(y))

    wps = [(float(p[0]), float(p[1])) for p in waypoints]
    meta = [
        {
            "kind": m.get("kind", "click"),
            "straight_len": m.get("straight_len"),
            "prev_corner_before": m.get("prev_corner_before"),
            "prev_kind_before": m.get("prev_kind_before"),
            "ext_history": list(m.get("ext_history", [])),
        }
        for m in wp_meta
    ]
    while len(meta) < len(wps):
        meta.append(
            {
                "kind": "click",
                "straight_len": None,
                "prev_corner_before": None,
                "prev_kind_before": None,
                "ext_history": [],
            }
        )

    th0 = math.radians(float(start_angle_deg))
    d0 = (math.cos(th0), math.sin(th0))

    # Case 1: Leg 1 leaving Start Port
    if not wps:
        if direction_vec is not None:
            dl = math.hypot(direction_vec[0], direction_vec[1])
            if dl > 1e-9:
                ux, uy = direction_vec[0] / dl, direction_vec[1] / dl
                if ux * d0[0] + uy * d0[1] < 0.99:
                    return {
                        "ok": False,
                        "error": (
                            f"First straight must exit forward along start port angle "
                            f"({float(start_angle_deg) % 360.0:.1f} deg)."
                        ),
                        "waypoints": wps,
                        "wp_meta": meta,
                    }
        lt1 = max(0.0, float(start_taper_len))
        new_pt = _q(float(start_pt[0]) + (lt1 + L) * d0[0], float(start_pt[1]) + (lt1 + L) * d0[1])
        wps.append(new_pt)
        meta.append(
            {
                "kind": "manual",
                "straight_len": L,
                "prev_corner_before": None,
                "prev_kind_before": None,
                "ext_history": [],
            }
        )
        return {"ok": True, "error": "", "waypoints": wps, "wp_meta": meta, "added_point": new_pt}

    # Case 2: Subsequent leg from trailing waypoint
    prev_pt = wps[-2] if len(wps) >= 2 else (float(start_pt[0]), float(start_pt[1]))
    tip = wps[-1]
    pvx, pvy = tip[0] - prev_pt[0], tip[1] - prev_pt[1]
    pl = math.hypot(pvx, pvy)
    if pl < 1e-9:
        u_prev = d0
    else:
        u_prev = (pvx / pl, pvy / pl)

    if direction_vec is None:
        u_new = u_prev
    else:
        dl = math.hypot(direction_vec[0], direction_vec[1])
        if dl < 1e-9:
            u_new = u_prev
        else:
            u_new = (direction_vec[0] / dl, direction_vec[1] / dl)

    dot = max(-1.0, min(1.0, u_prev[0] * u_new[0] + u_prev[1] * u_new[1]))
    cross = u_prev[0] * u_new[1] - u_prev[1] * u_new[0]
    turn_rad = math.atan2(cross, dot)

    # Subcase 2a: 180-deg reversal forbidden
    if abs(abs(turn_rad) - math.pi) < 1e-2:
        return {
            "ok": False,
            "error": "Cannot reverse 180 deg onto previous segment; point perpendicular to turn.",
            "waypoints": wps,
            "wp_meta": meta,
        }

    # Subcase 2b: Collinear forward extension
    if abs(turn_rad) < 1e-3:
        new_tip = _q(tip[0] + L * u_prev[0], tip[1] + L * u_prev[1])
        meta[-1]["ext_history"].append(tip)
        meta[-1]["kind"] = "manual"
        wps[-1] = new_tip
        return {"ok": True, "error": "", "waypoints": wps, "wp_meta": meta, "added_point": new_tip}

    # Subcase 2c: Turn by turn_rad (typically +-90 deg)
    turn_deg = math.degrees(turn_rad)
    t_bend = float(
        bend_metrics(
            angle_deg=turn_deg,
            radius=float(radius),
            p=float(euler_p),
            use_eff_radius=bool(use_eff_radius),
            bend_type=bend_type,
        )["tangent_offset"]
    )
    # Always append the bend AFTER the previous straight tip so prior straight lengths
    # are 100% preserved (never eaten by the bend's tangent offset).
    last_kind = meta[-1].get("kind", "manual")
    corner = _q(tip[0] + t_bend * u_prev[0], tip[1] + t_bend * u_prev[1])
    prev_corner_before: tuple[float, float] | None = tip
    prev_kind_before: str | None = last_kind
    wps[-1] = corner
    meta[-1]["kind"] = "corner_shifted"

    new_pt = _q(
        corner[0] + (t_bend + L) * u_new[0],
        corner[1] + (t_bend + L) * u_new[1],
    )
    wps.append(new_pt)
    meta.append(
        {
            "kind": "manual",
            "straight_len": L,
            "prev_corner_before": prev_corner_before,
            "prev_kind_before": prev_kind_before,
            "ext_history": [],
        }
    )
    return {"ok": True, "error": "", "waypoints": wps, "wp_meta": meta, "added_point": new_pt}


def undo_last_waypoint(
    waypoints: list[tuple[float, float]],
    wp_meta: list[dict[str, Any]],
) -> bool:
    """Undo the last waypoint (or collinear straight extension) in-place, restoring any shifted corner."""
    if not waypoints:
        return False
    if wp_meta and len(wp_meta) == len(waypoints):
        last_m = wp_meta[-1]
        ext_hist = last_m.get("ext_history")
        if ext_hist:
            waypoints[-1] = ext_hist.pop()
            return True
        popped = wp_meta.pop()
        waypoints.pop()
        prev_corner = popped.get("prev_corner_before")
        if prev_corner is not None and waypoints:
            waypoints[-1] = prev_corner
            if wp_meta and popped.get("prev_kind_before") is not None:
                wp_meta[-1]["kind"] = popped["prev_kind_before"]
        return True
    waypoints.pop()
    if wp_meta:
        wp_meta.pop()
    return True


def compute_manual_drag_waypoints(
    start_pt: tuple[float, float],
    start_angle_deg: float,
    waypoints: list[tuple[float, float]],
    wp_meta: list[dict[str, Any]],
    mouse_pt: tuple[float, float],
    radius: float = 80.0,
    euler_p: float = 0.5,
    use_eff_radius: bool = True,
    step_um: float = 1.0,
    dbu: float = 0.001,
    start_taper_len: float = 0.0,
    bend_type: str = "euler",
) -> dict[str, Any]:
    """Compute pure-straight + appended-90°-Euler-bend preview/commit from mouse position.

    Every placed straight length ``S_k`` is 100% pure uniform straight waveguide (NEVER consumed
    by a leading linear taper ``start_taper_len`` or by any subsequent 90° Euler bend):
      - Leg 1: measures straight length past ``start_taper_len`` along ``start_angle_deg``
        quantized to ``step_um`` (>= 1 um).
      - Leg k >= 2 turning +-90°: attaches a 90° Euler bend (offset ``t90``) AFTER the previous tip;
        if cursor distance ``dist_n`` along the turn axis has not cleared ``t90`` yet, returns
        ``pending_bend=True`` (no bend arc drawn yet); once ``dist_n >= t90 + 0.5*step``, quantizes
        the remaining post-bend distance ``dist_n - t90`` to ``step_um`` integer pure straight ``S_k``.
      - Forward drag along the same axis extends the current straight by ``+dL``.
    """
    step = max(float(step_um), 1.0)
    lt1 = max(0.0, float(start_taper_len))
    mx, my = float(mouse_pt[0]), float(mouse_pt[1])
    sx, sy = float(start_pt[0]), float(start_pt[1])
    th0 = math.radians(float(start_angle_deg))
    d0 = (math.cos(th0), math.sin(th0))

    # Leg 1: strictly forward along start port ray, pure straight measured AFTER start_taper_len
    if not waypoints:
        proj = (mx - sx) * d0[0] + (my - sy) * d0[1]
        if proj <= 0.0:
            return {
                "ok": False,
                "pending_bend": False,
                "pending_taper": False,
                "action": "invalid",
                "error": (
                    f"Drag forward along start port angle ({float(start_angle_deg) % 360.0:.1f} deg, "
                    f">= {lt1 + step:.1f} um)"
                ),
                "waypoints": list(waypoints),
                "wp_meta": [dict(m) for m in wp_meta],
            }
        proj_after = proj - lt1
        if proj_after < 0.5 * step:
            if lt1 > 1e-4:
                return {
                    "ok": False,
                    "pending_bend": False,
                    "pending_taper": True,
                    "needed_taper": lt1,
                    "dist_proj": proj,
                    "guide_tip": (sx + proj * d0[0], sy + proj * d0[1]),
                    "action": "pending_taper",
                    "error": (
                        f"Pull past linear taper (> {lt1:.1f} um) for pure straight S1 "
                        f"(now {proj:.1f} / {lt1:.1f} um), or type straight length + Enter"
                    ),
                    "waypoints": list(waypoints),
                    "wp_meta": [dict(m) for m in wp_meta],
                }
            return {
                "ok": False,
                "pending_bend": False,
                "pending_taper": False,
                "action": "invalid",
                "error": (
                    f"Drag forward along start port angle ({float(start_angle_deg) % 360.0:.1f} deg, "
                    f">= {step:.0f} um)"
                ),
                "waypoints": list(waypoints),
                "wp_meta": [dict(m) for m in wp_meta],
            }
        q_len = max(step, round(proj_after / step) * step)
        adv = advance_manual_straight_waypoints(
            start_pt,
            start_angle_deg,
            [],
            [],
            d0,
            q_len,
            radius=radius,
            euler_p=euler_p,
            use_eff_radius=use_eff_radius,
            dbu=dbu,
            min_len_um=step,
            start_taper_len=lt1,
            bend_type=bend_type,
        )
        adv["pending_bend"] = False
        adv["pending_taper"] = False
        adv["action"] = "straight"
        adv["straight_len"] = q_len
        return adv

    # Leg k >= 2: decompose cursor offset relative to trailing pure-straight tip
    prev_pt = waypoints[-2] if len(waypoints) >= 2 else (sx, sy)
    tip = waypoints[-1]
    pvx, pvy = tip[0] - prev_pt[0], tip[1] - prev_pt[1]
    pl = math.hypot(pvx, pvy)
    u_prev = (pvx / pl, pvy / pl) if pl > 1e-9 else d0
    n_prev = (-u_prev[1], u_prev[0])

    vx, vy = mx - tip[0], my - tip[1]
    proj_f = vx * u_prev[0] + vy * u_prev[1]
    proj_n = vx * n_prev[0] + vy * n_prev[1]

    t90 = float(
        bend_metrics(
            angle_deg=90.0,
            radius=float(radius),
            p=float(euler_p),
            use_eff_radius=bool(use_eff_radius),
            bend_type=bend_type,
        )["tangent_offset"]
    )
    bend_lbl = "TOPIC" if str(bend_type).lower() == "topic" else "Euler"

    # Stable geometric zone test: prevents jitter flip when cursor hovers near the bend exit (proj_f ~ t90)
    is_turn = (abs(proj_n) > abs(proj_f)) or (
        abs(proj_n) >= 0.5 * t90 and abs(proj_f - t90) < abs(proj_n)
    )

    if is_turn:
        u_new = n_prev if proj_n >= 0.0 else (-n_prev[0], -n_prev[1])
        dist_n = abs(proj_n)
        rem = dist_n - t90
        if rem < 0.5 * step:
            return {
                "ok": False,
                "pending_bend": True,
                "action": "pending_turn90",
                "turn_dir": u_new,
                "dist_n": dist_n,
                "needed_t90": t90,
                "guide_tip": (tip[0] + dist_n * u_new[0], tip[1] + dist_n * u_new[1]),
                "error": (
                    f"Pull > {t90:.1f} um to clear 90° {bend_lbl} bend "
                    f"(now {dist_n:.1f} / {t90:.1f} um), or type straight length + Enter"
                ),
                "waypoints": list(waypoints),
                "wp_meta": [dict(m) for m in wp_meta],
            }
        q_straight = max(step, round(rem / step) * step)
        adv = advance_manual_straight_waypoints(
            start_pt,
            start_angle_deg,
            waypoints,
            wp_meta,
            u_new,
            q_straight,
            radius=radius,
            euler_p=euler_p,
            use_eff_radius=use_eff_radius,
            dbu=dbu,
            min_len_um=step,
            bend_type=bend_type,
        )
        adv["pending_bend"] = False
        adv["action"] = "turn90"
        adv["straight_len"] = q_straight
        adv["needed_t90"] = t90
        return adv

    if proj_f >= 0.5 * step:
        q_ext = max(step, round(proj_f / step) * step)
        adv = advance_manual_straight_waypoints(
            start_pt,
            start_angle_deg,
            waypoints,
            wp_meta,
            u_prev,
            q_ext,
            radius=radius,
            euler_p=euler_p,
            use_eff_radius=use_eff_radius,
            dbu=dbu,
            min_len_um=step,
            bend_type=bend_type,
        )
        adv["pending_bend"] = False
        adv["action"] = "extend"
        adv["straight_len"] = q_ext
        return adv

    if proj_f >= 0.0:
        return {
            "ok": False,
            "pending_bend": False,
            "action": "idle",
            "error": (
                f"Drag forward (>= {step:.0f} um) to extend straight, "
                f"or perpendicular (> {t90:.1f} um) to append 90° bend + straight."
            ),
            "waypoints": list(waypoints),
            "wp_meta": [dict(m) for m in wp_meta],
        }

    return {
        "ok": False,
        "pending_bend": False,
        "action": "reverse_blocked",
        "error": "Cannot reverse 180 deg onto previous segment; drag perpendicular to turn 90 deg.",
        "waypoints": list(waypoints),
        "wp_meta": [dict(m) for m in wp_meta],
    }
