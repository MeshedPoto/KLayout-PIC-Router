"""Precision port-to-port and nearest-neighbor cell instance snapping.

100% pure Python 3.10+ stdlib + pya / klayout.db (zero numpy/scipy dependencies).
Supports arbitrary rotation, mirroring (mirx), non-orthogonal angled ports,
and out-of-the-box snapping on raw untagged GDS cells via in-memory auto-detection.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

try:
    from .config import DEFAULT_PIN_LAYER, get_kdb
except Exception:  # pragma: no cover - standalone fallback during parallel bring-up
    DEFAULT_PIN_LAYER: tuple[int, int] = (90, 0)

    def get_kdb() -> Any:
        try:
            import pya as _kdb  # type: ignore[import-not-found]
            return _kdb
        except ImportError:
            import klayout.db as _kdb  # type: ignore[import-not-found]
            return _kdb

from .ports import (
    DEFAULT_MARKER_LEN,
    Port,
    _angle_diff_deg,
    _expand_cell_instances,
    _normalize_angle_deg,
    _read_cell_port_records,
    _transform_port_record,
    auto_detect_ports,
)


def _normalize_signed_deg(angle_deg: float) -> float:
    """Normalize an angle in degrees to (-180.0, 180.0]."""
    ang = float(angle_deg) % 360.0
    if ang > 180.0:
        ang -= 360.0
    if abs(ang) < 1e-9:
        return 0.0
    if abs(ang + 180.0) < 1e-9:
        return 180.0
    return ang


def _snap_near_90_deg(angle_deg: float, tol: float = 1e-6) -> float:
    ang = _normalize_angle_deg(angle_deg)
    q90 = round(ang / 90.0) * 90.0
    if abs(ang - q90) < tol:
        return _normalize_angle_deg(q90)
    return ang


def _as_dcplx_trans(trans: Any, kdb: Any) -> Any:
    if isinstance(trans, kdb.DCplxTrans):
        return trans
    return kdb.DCplxTrans(trans)


def compute_snap_dcplx_trans(
    src_inst_dcplx: Any,
    src_world_port: Port,
    tgt_world_port: Port,
    allow_rotate: bool = True,
    dbu: float = 0.001,
) -> Any:
    """Compute new ``kdb.DCplxTrans`` snapping ``src_world_port`` onto ``tgt_world_port``.

    When ``allow_rotate=True``, rotates the instance around ``src_world_port`` so
    the source outward normal opposes ``tgt_world_port.angle_deg`` (``+180 deg``),
    then translates ``src_world_port`` onto ``(tgt_world_port.x, tgt_world_port.y)``.
    Exact under arbitrary rotation, mirroring (``mirx=True``), and magnification.
    """
    kdb = get_kdb()
    t_old = _as_dcplx_trans(src_inst_dcplx, kdb)

    if allow_rotate:
        dtheta = _normalize_signed_deg(
            (float(tgt_world_port.angle_deg) + 180.0) - float(src_world_port.angle_deg)
        )
        q90 = round(dtheta / 90.0) * 90.0
        if abs(dtheta - q90) < 1e-6:
            dtheta = _normalize_signed_deg(q90)

        t_delta = (
            kdb.DCplxTrans(
                1.0,
                dtheta,
                False,
                kdb.DVector(float(tgt_world_port.x), float(tgt_world_port.y)),
            )
            * kdb.DCplxTrans(
                1.0,
                0.0,
                False,
                kdb.DVector(-float(src_world_port.x), -float(src_world_port.y)),
            )
        )
        t_new = t_delta * t_old
    else:
        dx = float(tgt_world_port.x) - float(src_world_port.x)
        dy = float(tgt_world_port.y) - float(src_world_port.y)
        t_delta = kdb.DCplxTrans(1.0, 0.0, False, kdb.DVector(dx, dy))
        t_new = t_delta * t_old

    ang = _snap_near_90_deg(float(t_new.angle))
    disp_x = float(t_new.disp.x)
    disp_y = float(t_new.disp.y)
    if dbu is not None and float(dbu) > 0.0:
        d = float(dbu)
        disp_x = round(disp_x / d) * d
        disp_y = round(disp_y / d) * d

    return kdb.DCplxTrans(
        float(t_new.mag),
        ang,
        bool(t_new.is_mirror()),
        kdb.DVector(disp_x, disp_y),
    )


def snap_instance_to_port(
    inst: Any,
    src_world_port: Port,
    tgt_world_port: Port,
    allow_rotate: bool = True,
    dbu: float | None = 0.001,
) -> dict[str, Any]:
    """Snap ``inst`` so ``src_world_port`` mates cleanly with ``tgt_world_port``."""
    kdb = get_kdb()
    if dbu is None:
        try:
            dbu = float(inst.cell.layout().dbu)
        except Exception:
            dbu = 0.001

    old_trans = _as_dcplx_trans(inst.dcplx_trans, kdb)
    if allow_rotate:
        dtheta = _normalize_signed_deg(
            (float(tgt_world_port.angle_deg) + 180.0) - float(src_world_port.angle_deg)
        )
        q90 = round(dtheta / 90.0) * 90.0
        if abs(dtheta - q90) < 1e-6:
            dtheta = _normalize_signed_deg(q90)
    else:
        dtheta = 0.0

    t_new = compute_snap_dcplx_trans(
        old_trans,
        src_world_port,
        tgt_world_port,
        allow_rotate=allow_rotate,
        dbu=float(dbu),
    )
    inst.dcplx_trans = t_new
    applied_trans = _as_dcplx_trans(inst.dcplx_trans, kdb)

    # Evaluate exact post-snap world port position & outward normal
    inv_old = old_trans.inverted()
    rad = math.radians(float(src_world_port.angle_deg))
    p0_local = inv_old * kdb.DPoint(float(src_world_port.x), float(src_world_port.y))
    p1_local = inv_old * kdb.DPoint(
        float(src_world_port.x) + DEFAULT_MARKER_LEN * math.cos(rad),
        float(src_world_port.y) + DEFAULT_MARKER_LEN * math.sin(rad),
    )
    new_wp0 = applied_trans * p0_local
    new_wp1 = applied_trans * p1_local
    new_ang = _normalize_angle_deg(
        math.degrees(math.atan2(new_wp1.y - new_wp0.y, new_wp1.x - new_wp0.x))
    )

    residual_err_um = math.hypot(
        float(new_wp0.x) - float(tgt_world_port.x),
        float(new_wp0.y) - float(tgt_world_port.y),
    )
    angle_err_deg = _angle_diff_deg(
        new_ang, _normalize_angle_deg(float(tgt_world_port.angle_deg) + 180.0)
    )
    dx = float(new_wp0.x) - float(src_world_port.x)
    dy = float(new_wp0.y) - float(src_world_port.y)

    return {
        "dx": dx,
        "dy": dy,
        "dtheta": float(dtheta),
        "residual_err_um": float(residual_err_um),
        "angle_err_deg": float(angle_err_deg),
        "src_port": src_world_port,
        "tgt_port": tgt_world_port,
    }


def _port_to_local_record(port: Port, kdb: Any) -> dict[str, Any]:
    rad = math.radians(port.angle_deg)
    p_in = kdb.DPoint(
        port.x - DEFAULT_MARKER_LEN * math.cos(rad),
        port.y - DEFAULT_MARKER_LEN * math.sin(rad),
    )
    p_face = kdb.DPoint(port.x, port.y)
    return {
        "port": port,
        "p0": p_in,
        "p1": p_face,
        "anchor": p_face,
        "anchor_at_end": True,
    }


def _apply_pure_translation(
    inst: Any,
    dx: float,
    dy: float,
    dbu: float | None = 0.001,
) -> tuple[float, float, Any, Any]:
    """Translate ``inst`` by ``(dx, dy)`` in world um (DBU-snapped), preserving rot/mirror."""
    kdb = get_kdb()
    if dbu is None:
        try:
            dbu = float(inst.cell.layout().dbu)
        except Exception:
            dbu = 0.001

    old_trans = _as_dcplx_trans(inst.dcplx_trans, kdb)
    new_disp_x = float(old_trans.disp.x) + float(dx)
    new_disp_y = float(old_trans.disp.y) + float(dy)
    if dbu is not None and float(dbu) > 0.0:
        d = float(dbu)
        new_disp_x = round(new_disp_x / d) * d
        new_disp_y = round(new_disp_y / d) * d

    t_new = kdb.DCplxTrans(
        float(old_trans.mag),
        float(old_trans.angle),
        bool(old_trans.is_mirror()),
        kdb.DVector(new_disp_x, new_disp_y),
    )
    inst.dcplx_trans = t_new
    applied = _as_dcplx_trans(inst.dcplx_trans, kdb)
    actual_dx = float(applied.disp.x) - float(old_trans.disp.x)
    actual_dy = float(applied.disp.y) - float(old_trans.disp.y)
    return (actual_dx, actual_dy, old_trans, applied)


def align_instance_to_port_axis(
    inst: Any,
    src_world_port: Port,
    tgt_world_port: Port,
    axis: str = "y",
    dbu: float | None = 0.001,
) -> dict[str, Any]:
    """Align ``inst`` along a single coordinate axis ('x' or 'y') to match ``tgt_world_port``.

    - ``axis='y'``: horizontal alignment (``src.y == tgt.y``), preserving X distance so a
      subsequent route between facing horizontal ports is a pure straight line.
    - ``axis='x'``: vertical alignment (``src.x == tgt.x``), preserving Y distance so a
      subsequent route between facing vertical ports is a pure straight line.
    """
    ax = str(axis).strip().lower()
    if ax not in ("x", "y"):
        raise ValueError(f"axis must be 'x' or 'y', got {axis!r}")

    req_dx = (float(tgt_world_port.x) - float(src_world_port.x)) if ax == "x" else 0.0
    req_dy = (float(tgt_world_port.y) - float(src_world_port.y)) if ax == "y" else 0.0
    actual_dx, actual_dy, _, _ = _apply_pure_translation(inst, req_dx, req_dy, dbu=dbu)

    new_x = float(src_world_port.x) + actual_dx
    new_y = float(src_world_port.y) + actual_dy
    residual = (
        abs(new_x - float(tgt_world_port.x))
        if ax == "x"
        else abs(new_y - float(tgt_world_port.y))
    )
    return {
        "axis": ax,
        "dx": float(actual_dx),
        "dy": float(actual_dy),
        "residual_err_um": float(residual),
        "new_port_pos": (new_x, new_y),
        "src_port": src_world_port,
        "tgt_port": tgt_world_port,
    }


def align_instance_center_between_ports(
    inst: Any,
    src_port_a: Port,
    src_port_b: Port,
    tgt_port_a: Port,
    tgt_port_b: Port,
    axis: str = "auto",
    dbu: float | None = 0.001,
) -> dict[str, Any]:
    """Center a movable port pair ``(src_port_a, src_port_b)`` symmetrically between a target
    port pair ``(tgt_port_a, tgt_port_b)`` so upper/lower (or left/right) route offsets match.

    When ``axis='auto'``:
      - If the target pair spans primarily in Y (e.g., a 1x2 splitter's outputs vs a 2-port component's inputs),
        aligns the Y midpoint (``dx = 0``), preserving horizontal spacing.
      - If the target pair spans primarily in X, aligns the X midpoint (``dy = 0``),
        preserving vertical spacing.
    Also accepts explicit ``axis='x'``, ``'y'``, or ``'both'``.
    """
    ax = str(axis).strip().lower()
    if ax not in ("auto", "x", "y", "both"):
        raise ValueError(f"axis must be 'auto', 'x', 'y', or 'both', got {axis!r}")

    src_mid_x = 0.5 * (float(src_port_a.x) + float(src_port_b.x))
    src_mid_y = 0.5 * (float(src_port_a.y) + float(src_port_b.y))
    tgt_mid_x = 0.5 * (float(tgt_port_a.x) + float(tgt_port_b.x))
    tgt_mid_y = 0.5 * (float(tgt_port_a.y) + float(tgt_port_b.y))

    if ax == "auto":
        span_x = abs(float(tgt_port_b.x) - float(tgt_port_a.x))
        span_y = abs(float(tgt_port_b.y) - float(tgt_port_a.y))
        if span_x < 1e-6 and span_y < 1e-6:
            span_x = abs(float(src_port_b.x) - float(src_port_a.x))
            span_y = abs(float(src_port_b.y) - float(src_port_a.y))
        ax = "y" if span_y >= span_x else "x"

    req_dx = (tgt_mid_x - src_mid_x) if ax in ("x", "both") else 0.0
    req_dy = (tgt_mid_y - src_mid_y) if ax in ("y", "both") else 0.0
    actual_dx, actual_dy, _, _ = _apply_pure_translation(inst, req_dx, req_dy, dbu=dbu)

    new_mid_x = src_mid_x + actual_dx
    new_mid_y = src_mid_y + actual_dy

    if ax == "x":
        residual = abs(new_mid_x - tgt_mid_x)
        src_lo, src_hi = sorted((float(src_port_a.x) + actual_dx, float(src_port_b.x) + actual_dx))
        tgt_lo, tgt_hi = sorted((float(tgt_port_a.x), float(tgt_port_b.x)))
    elif ax == "y":
        residual = abs(new_mid_y - tgt_mid_y)
        src_lo, src_hi = sorted((float(src_port_a.y) + actual_dy, float(src_port_b.y) + actual_dy))
        tgt_lo, tgt_hi = sorted((float(tgt_port_a.y), float(tgt_port_b.y)))
    else:
        residual = math.hypot(new_mid_x - tgt_mid_x, new_mid_y - tgt_mid_y)
        src_lo, src_hi = sorted((float(src_port_a.y) + actual_dy, float(src_port_b.y) + actual_dy))
        tgt_lo, tgt_hi = sorted((float(tgt_port_a.y), float(tgt_port_b.y)))

    offset_hi = abs(tgt_hi - src_hi)
    offset_lo = abs(src_lo - tgt_lo)
    return {
        "axis": ax,
        "dx": float(actual_dx),
        "dy": float(actual_dy),
        "src_mid": (new_mid_x, new_mid_y),
        "tgt_mid": (tgt_mid_x, tgt_mid_y),
        "residual_err_um": float(residual),
        "offset_pair_1_um": float(offset_hi),
        "offset_pair_2_um": float(offset_lo),
        "src_ports": (src_port_a, src_port_b),
        "tgt_ports": (tgt_port_a, tgt_port_b),
    }


def snap_instance_relative_grid(
    inst: Any,
    movable_port: Port,
    ref_port: Port,
    step_um: float = 1.0,
    dbu: float | None = 0.001,
) -> dict[str, Any]:
    """Nudge ``inst`` by <= step_um/2 so ``(movable_port - ref_port)`` lies on an exact
    ``step_um`` (default 1.0 um) grid in both X and Y before routing.

    Eliminates fractional distances like ``100.0115 um`` when devices were placed freely.
    Preserves zero-offset axes (e.g. already-aligned Y stays 0.0) and avoids collapsing a
    non-zero single-axis separation down to 0.
    """
    step = max(float(step_um), 0.001)
    gap_x = float(movable_port.x) - float(ref_port.x)
    gap_y = float(movable_port.y) - float(ref_port.y)

    tgt_gx = 0.0 if abs(gap_x) < 1e-6 else round(gap_x / step) * step
    tgt_gy = 0.0 if abs(gap_y) < 1e-6 else round(gap_y / step) * step

    # Never collapse two non-coincident ports onto the exact same point
    if abs(tgt_gx) < 1e-9 and abs(tgt_gy) < 1e-9 and math.hypot(gap_x, gap_y) > 1e-6:
        if abs(gap_x) >= abs(gap_y):
            tgt_gx = step if gap_x >= 0.0 else -step
        else:
            tgt_gy = step if gap_y >= 0.0 else -step

    req_dx = tgt_gx - gap_x
    req_dy = tgt_gy - gap_y
    if abs(req_dx) < 1e-9 and abs(req_dy) < 1e-9:
        return {
            "moved": False,
            "dx": 0.0,
            "dy": 0.0,
            "gap_x_um": gap_x,
            "gap_y_um": gap_y,
            "port": movable_port,
        }

    actual_dx, actual_dy, _, _ = _apply_pure_translation(inst, req_dx, req_dy, dbu=dbu)
    updated_port = Port(
        name=movable_port.name,
        x=float(movable_port.x) + actual_dx,
        y=float(movable_port.y) + actual_dy,
        angle_deg=movable_port.angle_deg,
        width=movable_port.width,
        core_layer=movable_port.core_layer,
        cell_name=movable_port.cell_name,
        inst_id=movable_port.inst_id,
        inst_path=movable_port.inst_path,
        inst_obj=movable_port.inst_obj,
    )
    return {
        "moved": True,
        "dx": float(actual_dx),
        "dy": float(actual_dy),
        "gap_x_um": updated_port.x - float(ref_port.x),
        "gap_y_um": updated_port.y - float(ref_port.y),
        "port": updated_port,
    }


def _collect_instance_ports_with_fallback(
    layout: Any,
    child_cell: Any,
    inst_trans: Any,
    inst_id: str,
    inst_path: tuple[int, ...],
    top_inst_obj: Any,
    pin_layer: tuple[int, int],
    core_layers: Sequence[tuple[int, int]],
    pin_cache: dict[int, list[dict[str, Any]]],
    auto_cache: dict[int, list[dict[str, Any]]],
    max_depth: int = 8,
) -> list[Port]:
    """Collect PinRec ports in an instance subtree; auto-detect in memory if none exist."""
    kdb = get_kdb()
    ports: list[Port] = []

    def _get_pin_recs(c: Any) -> list[dict[str, Any]]:
        ci = c.cell_index()
        if ci not in pin_cache:
            pin_cache[ci] = _read_cell_port_records(layout, c, pin_layer=pin_layer)
        return pin_cache[ci]

    def _walk_pins(
        curr_cell: Any,
        acc_trans: Any,
        curr_id: str,
        curr_path: tuple[int, ...],
        depth: int,
    ) -> None:
        for rec in _get_pin_recs(curr_cell):
            ports.append(
                _transform_port_record(
                    rec,
                    acc_trans,
                    inst_id=curr_id,
                    inst_path=curr_path,
                    top_inst_obj=top_inst_obj,
                )
            )
        if depth < max_depth and curr_cell.child_instances() > 0:
            for sub in _expand_cell_instances(curr_cell):
                _walk_pins(
                    sub["cell"],
                    acc_trans * sub["trans"],
                    f"{curr_id}/{sub['local_id']}",
                    curr_path + (sub["det_idx"],),
                    depth + 1,
                )

    _walk_pins(child_cell, inst_trans, inst_id, inst_path, 1)
    if ports:
        return ports

    # Fallback: auto-detect ports in memory WITHOUT mutating child_cell
    ci = child_cell.cell_index()
    if ci not in auto_cache:
        detected = auto_detect_ports(
            layout,
            child_cell,
            core_layers=core_layers,
            include_hierarchy=True,
        )
        auto_cache[ci] = [_port_to_local_record(p, kdb) for p in detected]

    for rec in auto_cache[ci]:
        ports.append(
            _transform_port_record(
                rec,
                inst_trans,
                inst_id=inst_id,
                inst_path=inst_path,
                top_inst_obj=top_inst_obj,
            )
        )
    return ports


def _same_instance(inst_a: Any, inst_b: Any) -> bool:
    try:
        if inst_a == inst_b:
            return True
    except Exception:
        pass
    try:
        if inst_a.cell_index != inst_b.cell_index:
            return False
        ta = inst_a.dcplx_trans
        tb = inst_b.dcplx_trans
        return (
            abs(float(ta.disp.x) - float(tb.disp.x)) < 1e-6
            and abs(float(ta.disp.y) - float(tb.disp.y)) < 1e-6
            and _angle_diff_deg(float(ta.angle), float(tb.angle)) < 1e-6
            and bool(ta.is_mirror()) == bool(tb.is_mirror())
            and abs(float(ta.mag) - float(tb.mag)) < 1e-9
        )
    except Exception:
        return False


def snap_instance_nearest(
    layout: Any,
    top_cell: Any,
    movable_inst: Any,
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
    max_dist: float | None = None,
    allow_rotate: bool = False,
    core_layers: Sequence[tuple[int, int]] = ((1, 0), (2, 0)),
) -> dict[str, Any] | None:
    """Snap ``movable_inst`` to the nearest compatible port in ``top_cell``.

    Automatically detects ports in memory (without mutating cells) whenever
    PinRec markers are absent on either ``movable_inst`` or neighbor cells.
    """
    kdb = get_kdb()
    pin_cache: dict[int, list[dict[str, Any]]] = {}
    auto_cache: dict[int, list[dict[str, Any]]] = {}

    src_ports: list[Port] = []
    tgt_ports: list[Port] = []

    # Direct PinRec ports on top_cell are valid static snap targets
    for rec in _read_cell_port_records(layout, top_cell, pin_layer=pin_layer):
        p = rec["port"]
        tgt_ports.append(
            Port(
                name=p.name,
                x=p.x,
                y=p.y,
                angle_deg=p.angle_deg,
                width=p.width,
                core_layer=p.core_layer,
                cell_name=top_cell.name,
                inst_id="TOP",
                inst_path=(),
                inst_obj=None,
            )
        )

    matched_movable = False
    for entry in _expand_cell_instances(top_cell):
        is_movable = _same_instance(entry["inst"], movable_inst)
        inst_ports = _collect_instance_ports_with_fallback(
            layout=layout,
            child_cell=entry["cell"],
            inst_trans=entry["trans"],
            inst_id=entry["local_id"],
            inst_path=(entry["det_idx"],),
            top_inst_obj=entry["inst"],
            pin_layer=pin_layer,
            core_layers=core_layers,
            pin_cache=pin_cache,
            auto_cache=auto_cache,
        )
        if is_movable:
            matched_movable = True
            src_ports.extend(inst_ports)
        else:
            tgt_ports.extend(inst_ports)

    # Safety fallback if movable_inst was not matched via _expand_cell_instances
    if not matched_movable:
        child_cell = movable_inst.cell
        if child_cell is not None:
            src_ports.extend(
                _collect_instance_ports_with_fallback(
                    layout=layout,
                    child_cell=child_cell,
                    inst_trans=_as_dcplx_trans(movable_inst.dcplx_trans, kdb),
                    inst_id=f"{child_cell.name}#1",
                    inst_path=(),
                    top_inst_obj=movable_inst,
                    pin_layer=pin_layer,
                    core_layers=core_layers,
                    pin_cache=pin_cache,
                    auto_cache=auto_cache,
                )
            )

    if not src_ports or not tgt_ports:
        return None

    best_same: tuple[tuple[float, float, float], Port, Port] | None = None
    best_any: tuple[tuple[float, float, float], Port, Port] | None = None

    for sp in src_ports:
        for tp in tgt_ports:
            dist = math.hypot(tp.x - sp.x, tp.y - sp.y)
            if max_dist is not None and dist > float(max_dist) + 1e-9:
                continue
            anti_err = _angle_diff_deg(
                sp.angle_deg, _normalize_angle_deg(tp.angle_deg + 180.0)
            )
            if not allow_rotate and anti_err >= 5.0:
                continue

            score = (dist, anti_err, abs(sp.width - tp.width))
            if best_any is None or score < best_any[0]:
                best_any = (score, sp, tp)
            if sp.core_layer == tp.core_layer:
                if best_same is None or score < best_same[0]:
                    best_same = (score, sp, tp)

    if best_any is None:
        return None

    # Prefer same core_layer unless another layer's port is substantially closer
    chosen = best_any
    if best_same is not None:
        same_dist = best_same[0][0]
        any_dist = best_any[0][0]
        if same_dist <= min(50.0, 2.0 * any_dist + 5.0):
            chosen = best_same

    _, best_sp, best_tp = chosen
    return snap_instance_to_port(
        movable_inst,
        best_sp,
        best_tp,
        allow_rotate=allow_rotate,
        dbu=float(layout.dbu),
    )
