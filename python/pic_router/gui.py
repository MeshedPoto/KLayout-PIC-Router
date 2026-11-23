"""Interactive KLayout GUI, Dock Panel, Menu & Canvas Plugins for KLayout-PIC Router.

100% pure Python 3.10+ + pya (Qt5 / Qt6 compatible, KLayout 0.28–0.30+).
Headless-safe: can be imported in ``klayout -b`` or external Python without a GUI.
All shortcuts use non-conflicting ``Ctrl+Shift+...`` combinations.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
import traceback
from typing import Any, Sequence

try:
    import pya
except ImportError:  # pragma: no cover - external klayout.db test environment
    pya = None

from .config import (
    CROSS_SECTION_PRESETS,
    DEFAULT_ERROR_LAYER,
    DEFAULT_GUIDE_LAYER,
    DEFAULT_LENGTH_LAYER,
    DEFAULT_LENGTH_LAYER_COLOR,
    DEFAULT_PIN_LAYER,
    DEFAULT_PRESET_NAME,
    DEFAULT_SHIFT_STEP_UM,
    CrossSection,
    LayerSpec,
    delete_user_preset,
    get_all_presets,
    load_user_presets,
    save_user_preset,
)
from .euler import VALID_BEND_TYPES, VALID_SBEND_CURVES, topic_bend_metrics
from .mask_ops import (
    add_trench_to_cell,
    add_trench_to_pcell_inst,
    bias_cell_linewidth,
    bias_pcell_inst_linewidth,
    bias_region_preserve_length,
    build_trench_region,
    collect_owning_static_cells,
    detect_open_ports_on_region,
)
from .pcells import (
    LIBRARY_NAME,
    _effective_preset_name,
    instantiate_euler_waveguide,
    instantiate_topic_bend,
    register_pcell_library,
)
from .ports import (
    Port,
    add_port_to_cell,
    collect_world_ports,
    detect_port_near_point,
    read_cell_ports,
    set_instance_alias,
    tag_detected_ports,
)
from .router import (
    VALID_LABEL_STYLES,
    _choose_primary_anchor_segment,
    _format_card_segment_item,
    _infer_label_line_height,
    _place_collision_free_label,
    _sample_polyline_points,
    _segment_anchor_frame,
    _segment_label_position,
    advance_manual_straight_waypoints,
    analyze_live_guide_segments,
    apply_taper_segments,
    compute_manual_drag_waypoints,
    draw_length_annotations_to_cell,
    format_route_card_text,
    format_route_header,
    format_segments_summary,
    plan_route_ports,
    route_from_guide_points,
    route_sbend_from_guide_points,
    split_taper_segments_list,
    tune_route_for_integer_length,
    undo_last_waypoint,
)
from .snap import (
    align_instance_center_between_ports,
    align_instance_to_port_axis,
    snap_instance_nearest,
    snap_instance_relative_grid,
    snap_instance_to_port,
)

# Non-conflicting Ctrl+Shift+... shortcuts.
SHORTCUTS: dict[str, str] = {
    "toggle_panel": "Ctrl+Shift+T",
    "snap_mode": "Ctrl+Shift+S",
    "route_mode": "Ctrl+Shift+R",
    "free_draw_mode": "Ctrl+Shift+F",
    "add_port_mode": "Ctrl+Shift+P",
    "align_x_mode": "Ctrl+Shift+X",
    "align_y_mode": "Ctrl+Shift+Y",
    "center_pair_mode": "Ctrl+Shift+M",
    "quick_snap": "Ctrl+Shift+K",
    "auto_detect_ports": "Ctrl+Shift+D",
    "path_to_waveguide": "Ctrl+Shift+W",
    "revert_waveguide": "Ctrl+Shift+E",
}

MODE_SNAP = "pic_router_snap"
MODE_ROUTE = "pic_router_route"
MODE_FREE_DRAW = "pic_router_free_draw"
MODE_ADD_PORT = "pic_router_add_port"
MODE_ALIGN_X = "pic_router_align_x"
MODE_ALIGN_Y = "pic_router_align_y"
MODE_CENTER_PAIR = "pic_router_center_pair"

_COLOR_HOVER = 0x0088AA
_COLOR_SOURCE = 0x00AA44
_COLOR_TARGET = 0xD96600
_COLOR_PREVIEW_OK = 0xCC8800
_COLOR_PREVIEW_ERR = 0xCC1111
_COLOR_HIGHLIGHT = 0xAA00AA
_COLOR_LABEL_DARK = DEFAULT_LENGTH_LAYER_COLOR

BEND_TYPE_ITEMS: list[tuple[str, str]] = [
    ("TOPIC (C3)", "topic"),
    ("Euler (C2)", "euler"),
]
_BEND_TYPE_LABEL_TO_CODE: dict[str, str] = {lbl: code for lbl, code in BEND_TYPE_ITEMS}

ROUTE_MODE_ITEMS: list[tuple[str, str]] = [
    ("S-Bend (C2 Bezier)", "sbend_bezier"),
    ("S-Bend (C2 Sine)", "sbend_sine"),
    ("90° Bend + Straight", "euler_90"),
    ("Auto (Best)", "auto"),
    ("Straight Only", "straight"),
]
_ROUTE_MODE_LABEL_TO_CODE: dict[str, str] = {
    **{lbl: code for lbl, code in ROUTE_MODE_ITEMS},
    "90° Euler + Straight": "euler_90",
}

LABEL_STYLE_ITEMS: list[tuple[str, str]] = [
    ("Compact Card", "card"),
    ("Per-Segment", "per_segment"),
    ("Total Only", "total_only"),
]
_LABEL_STYLE_LABEL_TO_CODE: dict[str, str] = {lbl: code for lbl, code in LABEL_STYLE_ITEMS}

# Module-level state keeps strong references to Qt/KLayout objects to prevent GC.
_GUI_STATE: dict[str, Any] = {
    "initialized": False,
    "dock": None,
    "active_route_plugin": None,
    "active_free_draw_plugin": None,
    "actions": [],
    "factories": [],
    "highlight_markers": [],
}

_QDockWidgetBase = pya.QDockWidget if (pya is not None and hasattr(pya, "QDockWidget")) else object
_PluginBase = pya.Plugin if (pya is not None and hasattr(pya, "Plugin")) else object
_PluginFactoryBase = pya.PluginFactory if (pya is not None and hasattr(pya, "PluginFactory")) else object


class _ViewerModeError(RuntimeError):
    """Raised when attempting layout edits while KLayout is in non-editable Viewer mode."""


@contextmanager
def _view_transaction(view: Any, description: str, layout: Any = None):
    """Context manager wrapping KLayout's view.transaction() / view.commit()."""
    ly = layout
    if ly is None:
        try:
            cv = view.active_cellview()
            if cv is not None and cv.is_valid():
                ly = cv.layout()
        except Exception:
            ly = None
    if ly is not None and hasattr(ly, "is_editable") and not ly.is_editable():
        raise _ViewerModeError(
            "KLayout is in Viewer mode — launch with 'klayout -e' or enable Editing Mode "
            "(File -> Setup -> Application -> Editing Mode)."
        )
    view.transaction(description)
    try:
        yield
    finally:
        view.commit()


def parse_layer_text(text: str) -> tuple[int, int] | None:
    """Parse layer specs like '1/0', '1,0', '1:0', or '1' -> (1, 0)."""
    raw = (text or "").strip()
    if not raw:
        return None
    for sep in ("/", ",", ":", " "):
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep) if p.strip()]
            if len(parts) == 2:
                try:
                    return (int(parts[0]), int(parts[1]))
                except ValueError:
                    return None
            return None
    try:
        return (int(raw), 0)
    except ValueError:
        return None


def parse_layer_list_text(text: str) -> list[tuple[int, int, float | None]]:
    """Parse one or more semicolon-separated layer specs with optional per-layer width.

    Supports both ASCII ``;`` and Chinese fullwidth ``；`` separators:
      - ``'1/0'`` -> ``[(1, 0, None)]``
      - ``'1/1; 2/1'`` -> ``[(1, 1, None), (2, 1, None)]``
      - ``'1/1=10; 2/1=25'`` or ``'1/1; 2/1@25'`` -> ``[(1, 1, 10.0), (2, 1, 25.0)]``
    """
    raw = (text or "").replace("；", ";").strip()
    if not raw:
        return []
    out: list[tuple[int, int, float | None]] = []
    seen: set[tuple[int, int, float | None]] = set()
    for token in raw.split(";"):
        tok = token.strip()
        if not tok:
            continue
        width_override: float | None = None
        layer_part = tok
        for wsep in ("=", "@"):
            if wsep in tok:
                lp, wp = tok.split(wsep, 1)
                layer_part = lp.strip()
                wp_s = wp.strip()
                if wp_s:
                    try:
                        wv = float(wp_s)
                        if wv > 0.0:
                            width_override = wv
                    except ValueError:
                        width_override = None
                break
        parsed = parse_layer_text(layer_part)
        if parsed is None:
            continue
        item = (parsed[0], parsed[1], width_override)
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _max_cs_width(cs: CrossSection) -> float:
    return max((float(ls.width) for ls in cs.layers), default=float(cs.core_width or 0.8))


def _get_active_context() -> tuple[Any, Any, Any, Any] | None:
    """Return (main_window, view, layout, top_cell) or None if no active layout."""
    if pya is None:
        return None
    app = pya.Application.instance()
    if app is None:
        return None
    mw = app.main_window()
    if mw is None:
        return None
    view = mw.current_view()
    if view is None:
        return None
    cv = view.active_cellview()
    if cv is None or not cv.is_valid():
        return None
    layout = cv.layout()
    cell = cv.cell
    if layout is None or cell is None:
        return None
    return mw, view, layout, cell


def _set_status(msg: str, is_error: bool = False) -> None:
    dock = _GUI_STATE.get("dock")
    if dock is not None:
        dock.set_status(msg, is_error=is_error)
    else:
        prefix = "[pic_router ERROR]" if is_error else "[pic_router]"
        print(f"{prefix} {msg}")


def _refresh_ports_table() -> None:
    dock = _GUI_STATE.get("dock")
    if dock is not None:
        dock.refresh_ports()


def _ensure_length_layer_style(view: Any) -> None:
    """Style the RouteLength (90/2) layer in a deep high-contrast dark navy color."""
    try:
        if not hasattr(view, "begin_layers"):
            return
        it = view.begin_layers()
        while not it.at_end():
            lp = it.current()
            if (
                int(getattr(lp, "source_layer", -1)) == DEFAULT_LENGTH_LAYER[0]
                and int(getattr(lp, "source_datatype", -1)) == DEFAULT_LENGTH_LAYER[1]
            ):
                lp.frame_color = int(DEFAULT_LENGTH_LAYER_COLOR)
                lp.fill_color = int(DEFAULT_LENGTH_LAYER_COLOR)
                lp.width = 2
                if not str(getattr(lp, "name", "") or "").strip():
                    lp.name = "RouteLength"
                view.set_layer_properties(it, lp)
                break
            it.next()
    except Exception:
        pass


def _refresh_view_layers(view: Any) -> None:
    try:
        if hasattr(view, "add_missing_layers"):
            view.add_missing_layers()
        _ensure_length_layer_style(view)
    except Exception:
        pass


def _dynamic_scale(view: Any) -> tuple[float, float]:
    """Return (pick_radius_um, arrow_len_um) adapted to current viewport zoom."""
    try:
        box = view.box()
        span = max(float(box.width()), float(box.height()))
        pick_r = max(2.0, min(150.0, 0.03 * span))
        arrow_len = max(1.5, min(80.0, 0.025 * span))
        return pick_r, arrow_len
    except Exception:
        return 10.0, 5.0


def _find_nearest_port(
    ports: Sequence[Port],
    x: float,
    y: float,
    max_dist: float | None = None,
    exclude_full_name: str | None = None,
    exclude_inst: Any = None,
    exclude_inst_id: str | None = None,
    require_movable: bool = False,
) -> tuple[Port | None, float]:
    best_port = None
    best_dist = float("inf")
    limit = float(max_dist) if max_dist is not None else float("inf")
    fx = float(x)
    fy = float(y)
    for port in ports:
        dx = port.x - fx
        if dx > limit or dx < -limit:
            continue
        dy = port.y - fy
        if dy > limit or dy < -limit:
            continue
        if require_movable and port.inst_obj is None:
            continue
        if exclude_full_name is not None and port.full_name == exclude_full_name:
            continue
        if exclude_inst_id and port.inst_id and port.inst_id == exclude_inst_id:
            continue
        if exclude_inst is not None and port.inst_obj is not None:
            try:
                if port.inst_obj == exclude_inst:
                    continue
            except Exception:
                pass
        d = math.hypot(dx, dy)
        if d < best_dist:
            best_dist = d
            best_port = port
            if d < limit:
                limit = d
    if best_port is None:
        return None, float("inf")
    if max_dist is not None and best_dist > max_dist:
        return None, best_dist
    return best_port, best_dist


class _MarkerBag:
    """Safe manager for temporary pya.Marker overlays in a KLayout LayoutView."""

    def __init__(self, view: Any) -> None:
        self._view = view
        self._markers: list[Any] = []

    def clear(self) -> None:
        for marker in self._markers:
            try:
                marker._destroy()
            except Exception:
                pass
        self._markers.clear()

    def _new_marker(self, color: int, line_width: int = 3, vertex_size: int = 4) -> Any:
        m = pya.Marker(self._view)
        m.color = int(color)
        m.frame_color = int(color)
        m.line_width = int(line_width)
        m.vertex_size = int(vertex_size)
        m.dither_pattern = -1
        self._markers.append(m)
        return m

    def add_port(self, port: Port, color: int, arrow_len: float = 5.0, show_label: bool = True) -> None:
        rad = math.radians(port.angle_deg)
        dx = math.cos(rad) * arrow_len
        dy = math.sin(rad) * arrow_len
        p0 = pya.DPoint(port.x, port.y)
        p1 = pya.DPoint(port.x + dx, port.y + dy)

        # Port center-to-outward arrow shaft
        m_shaft = self._new_marker(color, line_width=3, vertex_size=5)
        m_shaft.set_edge(pya.DEdge(p0, p1))

        # Port facet bar perpendicular to outward direction
        half_w = max(0.5 * port.width, 0.2 * arrow_len)
        nx = -math.sin(rad) * half_w
        ny = math.cos(rad) * half_w
        m_bar = self._new_marker(color, line_width=3, vertex_size=0)
        m_bar.set_edge(pya.DEdge(pya.DPoint(port.x - nx, port.y - ny), pya.DPoint(port.x + nx, port.y + ny)))

        # Arrowhead wings
        head_len = 0.3 * arrow_len
        back_x = p1.x - math.cos(rad) * head_len
        back_y = p1.y - math.sin(rad) * head_len
        wing_x = -math.sin(rad) * (0.18 * arrow_len)
        wing_y = math.cos(rad) * (0.18 * arrow_len)
        for sign in (-1.0, 1.0):
            m_wing = self._new_marker(color, line_width=2, vertex_size=0)
            m_wing.set_edge(
                pya.DEdge(p1, pya.DPoint(back_x + sign * wing_x, back_y + sign * wing_y))
            )

        if show_label:
            m_txt = self._new_marker(color, line_width=1, vertex_size=0)
            label = f"{port.full_name} ({port.width:.2f}um, {port.angle_deg:.1f}deg)"
            m_txt.set_text(pya.DText(label, pya.DTrans(p1.x, p1.y)))

    def add_polyline(
        self,
        points: Sequence[tuple[float, float]],
        color: int,
        width: float = 0.0,
        max_points: int = 320,
    ) -> None:
        n = len(points)
        if n < 2:
            return
        if max_points >= 4 and n > max_points:
            stride = max(1, (n - 1) // (max_points - 1))
            sampled = [points[i] for i in range(0, n - 1, stride)]
            sampled.append(points[-1])
            dpts = [pya.DPoint(float(x), float(y)) for x, y in sampled]
        else:
            dpts = [pya.DPoint(float(x), float(y)) for x, y in points]
        m = self._new_marker(color, line_width=2, vertex_size=3)
        m.set_path(pya.DPath(dpts, float(width)))

    def add_label(
        self,
        x: float,
        y: float,
        text: str,
        color: int = _COLOR_LABEL_DARK,
        halign: str = "left",
        valign: str = "bottom",
    ) -> None:
        m = self._new_marker(color, line_width=1, vertex_size=0)
        dt = pya.DText(str(text), pya.DTrans(pya.DVector(float(x), float(y))))
        h_cls = getattr(pya, "HAlign", None)
        v_cls = getattr(pya, "VAlign", None)
        if h_cls is not None:
            hm = {
                "left": getattr(h_cls, "HAlignLeft", None),
                "center": getattr(h_cls, "HAlignCenter", None),
                "right": getattr(h_cls, "HAlignRight", None),
            }
            if hm.get(halign) is not None:
                dt.halign = hm[halign]
        if v_cls is not None:
            vm = {
                "bottom": getattr(v_cls, "VAlignBottom", None),
                "center": getattr(v_cls, "VAlignCenter", None),
                "top": getattr(v_cls, "VAlignTop", None),
            }
            if vm.get(valign) is not None:
                dt.valign = vm[valign]
        m.set_text(dt)

    def add_point_marker(
        self,
        x: float,
        y: float,
        color: int,
        size: float = 5.0,
        label: str = "",
    ) -> None:
        h = 0.5 * max(0.5, float(size))
        fx, fy = float(x), float(y)
        mh = self._new_marker(color, line_width=3, vertex_size=5)
        mh.set_edge(pya.DEdge(pya.DPoint(fx - h, fy), pya.DPoint(fx + h, fy)))
        mv = self._new_marker(color, line_width=3, vertex_size=5)
        mv.set_edge(pya.DEdge(pya.DPoint(fx, fy - h), pya.DPoint(fx, fy + h)))
        if label:
            self.add_label(fx + h * 0.6, fy + h * 0.6, label, color=color)


def _draw_live_segment_labels(
    bag: _MarkerBag,
    segments: Sequence[dict[str, Any]],
    total_length: float,
    label_offset: float = 6.0,
    color: int = _COLOR_LABEL_DARK,
    total_prefix: str = "Total",
) -> None:
    """Render clean, collision-avoiding live on-canvas length markers using pya.Marker."""
    if not segments:
        return
    seg_list = list(segments)
    pts_all: list[tuple[float, float]] = []
    for seg in seg_list:
        for k in ("start", "midpoint", "end"):
            if seg.get(k) is not None:
                pts_all.append((float(seg[k][0]), float(seg[k][1])))
    if pts_all:
        xs = [p[0] for p in pts_all]
        ys = [p[1] for p in pts_all]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 100.0)
    else:
        span = 200.0
    line_h = max(12.0, min(32.0, 0.018 * span))
    occ: list[tuple[float, float, float, float]] = []

    # Show 1 clean card for multi-segment auto-previews when segments are crowded,
    # or clean collision-free per-segment labels when drawing manually.
    for seg in seg_list:
        anc, tan, nrm, seg_span = _segment_anchor_frame(seg)
        txt = _format_card_segment_item(seg, include_radius=True)
        lx, ly, ha, va, box = _place_collision_free_label(
            anchor=anc,
            tangent=tan,
            normal=nrm,
            seg_span=seg_span,
            text=txt,
            line_h=line_h,
            base_offset=max(float(label_offset), 0.75 * line_h),
            occupied_boxes=occ,
            obstacle_pts=pts_all,
            prefer_sign=1.0,
        )
        bag.add_label(lx, ly, txt, color=color, halign=ha, valign=va)
        occ.append(box)
    if len(seg_list) >= 2:
        primary = _choose_primary_anchor_segment(seg_list)
        anc, tan, nrm, seg_span = _segment_anchor_frame(primary)
        hdr = format_route_header(seg_list, total_length=total_length, prefix=total_prefix)
        lx, ly, ha, va, box = _place_collision_free_label(
            anchor=anc,
            tangent=tan,
            normal=nrm,
            seg_span=seg_span,
            text=hdr,
            line_h=line_h,
            base_offset=max(float(label_offset) * 1.4, 1.0 * line_h),
            occupied_boxes=occ,
            obstacle_pts=pts_all,
            prefer_sign=-1.0,
        )
        bag.add_label(lx, ly, hdr, color=color, halign=ha, valign=va)
        occ.append(box)


def _clear_global_highlights() -> None:
    for m in _GUI_STATE["highlight_markers"]:
        try:
            m._destroy()
        except Exception:
            pass
    _GUI_STATE["highlight_markers"].clear()


def _refresh_routes_table() -> None:
    dock = _GUI_STATE.get("dock")
    if dock is not None and hasattr(dock, "refresh_routes"):
        try:
            dock.refresh_routes()
        except Exception:
            pass


def _collect_euler_waveguide_routes(layout: Any, top_cell: Any) -> list[dict[str, Any]]:
    """Inspect all ``EulerWaveguide`` PCell instances in ``top_cell`` and compute exact route metrics."""
    if layout is None or top_cell is None or pya is None:
        return []
    dbu = float(layout.dbu)
    records: list[dict[str, Any]] = []
    idx = 0
    for inst in top_cell.each_inst():
        if not (inst.is_pcell() and inst.cell is not None and inst.cell.pcell_name() == "EulerWaveguide"):
            continue
        params = inst.pcell_parameters_by_name()
        path_shape = params.get("path")
        if path_shape is None:
            continue
        if isinstance(path_shape, pya.DPath):
            raw_pts = [(float(pt.x), float(pt.y)) for pt in path_shape.each_point()]
        elif isinstance(path_shape, pya.Path):
            raw_pts = [(float(pt.x) * dbu, float(pt.y) * dbu) for pt in path_shape.each_point()]
        else:
            continue
        t = inst.dcplx_trans
        wpts = [(float((t * pya.DPoint(x, y)).x), float((t * pya.DPoint(x, y)).y)) for x, y in raw_pts]
        ctype = str(params.get("curve_type", "euler") or "euler")
        btype = str(params.get("bend_type", "euler") or "euler").lower()
        if ctype in VALID_SBEND_CURVES:
            sb_ang = (float(params.get("sbend_angle_deg", 0.0)) + float(t.angle)) % 360.0
            res = route_sbend_from_guide_points(
                wpts,
                curve_type=ctype,
                start_angle_deg=sb_ang,
            )
        else:
            res = route_from_guide_points(
                wpts,
                radius=float(params.get("radius", 50.0)),
                euler_p=float(params.get("euler_p", 0.5)),
                min_straight=float(params.get("min_straight", 5.0)),
                use_eff_radius=bool(params.get("use_eff_radius", True)),
                min_straight_ends=0.0,
                bend_type=btype,
            )
        if not res.ok:
            continue
        tw = float(params.get("trench_width", 0.0))
        cw = float(params.get("core_width", 0.80))
        max_w = max(tw, cw)
        extra_raw = str(params.get("extra_layers_json", "") or "").strip()
        if extra_raw:
            try:
                import json as _json
                for item in _json.loads(extra_raw):
                    if isinstance(item, (list, tuple)) and len(item) >= 3:
                        max_w = max(max_w, float(item[2]))
            except Exception:
                pass
        sw_p = float(params.get("start_width", 0.0))
        ew_p = float(params.get("end_width", 0.0))
        tl_p = float(params.get("taper_length", 0.0))
        res_ann = apply_taper_segments(
            res,
            core_width=cw,
            start_width=(sw_p if sw_p > 0.0 else None),
            end_width=(ew_p if ew_p > 0.0 else None),
            taper_length=tl_p,
        )
        idx += 1
        r_mins = [float(s["R_min"]) for s in res_ann.segments if s.get("R_min") is not None]
        r_effs = [float(s["R_eff"]) for s in res_ann.segments if s.get("kind") == "euler" and s.get("R_eff") is not None]
        if r_mins and r_effs:
            rad_str = f"{min(r_mins):.1f} / {min(r_effs):.1f}"
        elif r_mins:
            rad_str = f"{min(r_mins):.1f}"
        else:
            rad_str = "-"
        seg_str = " + ".join(_format_card_segment_item(s, include_radius=False) for s in res_ann.segments)
        records.append(
            {
                "index": idx,
                "inst": inst,
                "result": res_ann,
                "max_width": max_w,
                "total_length": float(res_ann.total_length),
                "radius_str": rad_str,
                "segments_str": seg_str,
                "bbox": inst.dbbox(),
            }
        )
    return records


def _get_route_settings() -> dict[str, Any]:
    dock = _GUI_STATE.get("dock")
    if dock is not None:
        return dock.get_settings()
    ps = CROSS_SECTION_PRESETS[DEFAULT_PRESET_NAME]
    return {
        "preset": ps.name,
        "core_width": ps.core_width,
        "trench_width": ps.trench_width or 0.0,
        "core_layer": ps.core_layer,
        "trench_layer": ps.trench_layer or (1, 1),
        "radius": ps.radius,
        "euler_p": ps.euler_p,
        "bend_type": "topic",
        "min_straight": ps.min_straight,
        "route_mode": "auto",
        "auto_taper": True,
        "allow_rotate": True,
        "integer_length": True,
        "annotate_lengths": True,
        "label_style": "card",
        "use_eff_radius": True,
        "taper_length": 10.0,
        "cross_section": ps,
    }


def _compute_port_taper(
    port_width: float, core_width: float, settings: dict[str, Any]
) -> tuple[float | None, float]:
    """Return ``(end_width_override, taper_len)`` if port width mismatches core_width and Auto Taper is ON."""
    if not settings.get("auto_taper", True):
        return (None, 0.0)
    tl = max(0.0, float(settings.get("taper_length", 10.0)))
    pw = float(port_width)
    if tl > 1e-4 and pw > 0.0 and abs(pw - float(core_width)) > 1e-4:
        return (pw, tl)
    return (None, 0.0)


def _execute_port_route(
    view: Any,
    layout: Any,
    top_cell: Any,
    port1: Port,
    port2: Port,
    waypoints: Sequence[tuple[float, float]] | None = None,
    tx_title: str = "PIC Euler Route",
) -> bool:
    settings = _get_route_settings()
    cs: CrossSection = settings["cross_section"]
    dbu = float(layout.dbu)
    want_int = bool(settings.get("integer_length", True))
    want_annot = bool(settings.get("annotate_lengths", True))
    wp_list = list(waypoints) if waypoints else None
    taper_len = max(0.0, float(settings.get("taper_length", 10.0)))
    sw, lt1 = _compute_port_taper(port1.width, cs.core_width, settings)
    ew, lt2 = _compute_port_taper(port2.width, cs.core_width, settings)

    cs_used = cs
    btype = str(settings.get("bend_type", "topic") or "topic").lower()
    if btype not in VALID_BEND_TYPES:
        btype = "topic"
    res = plan_route_ports(
        port1,
        port2,
        radius=cs.radius,
        euler_p=cs.euler_p,
        min_straight=cs.min_straight,
        route_mode=settings["route_mode"],
        waypoints=wp_list,
        use_eff_radius=settings["use_eff_radius"],
        start_taper_len=lt1,
        end_taper_len=lt2,
        bend_type=btype,
    )
    if not res.ok and wp_list and cs.min_straight > DEFAULT_SHIFT_STEP_UM:
        res_relaxed = plan_route_ports(
            port1,
            port2,
            radius=cs.radius,
            euler_p=cs.euler_p,
            min_straight=DEFAULT_SHIFT_STEP_UM,
            route_mode=settings["route_mode"],
            waypoints=wp_list,
            use_eff_radius=settings["use_eff_radius"],
            start_taper_len=lt1,
            end_taper_len=lt2,
            bend_type=btype,
        )
        if res_relaxed.ok:
            res = res_relaxed
            cs_used = cs.with_overrides(min_straight=DEFAULT_SHIFT_STEP_UM)
    if not res.ok:
        _set_status(
            f"Route infeasible ({port1.full_name} -> {port2.full_name}): {res.error}",
            is_error=True,
        )
        return False

    int_note = ""
    snap_port2_needed = False
    snap_delta_axial = 0.0

    # Integer-um tuning applies to auto-routes; manual waypoint routes strictly preserve
    # exact user straight lengths (S1, S2, ...) and identical fixed bend radius R_eff.
    if want_int and not wp_list and abs(res.total_length - round(res.total_length)) >= 5e-4:
        if res.bends:
            # 1st choice: zero-device-move analytical R_eff / S-bend stub tuning -> exact N.000 um!
            tuned_res, used_r, was_tuned = tune_route_for_integer_length(
                res,
                radius=cs.radius,
                euler_p=cs.euler_p,
                min_straight=cs.min_straight,
                use_eff_radius=settings["use_eff_radius"],
                step_um=1.0,
                dbu=dbu,
                min_stub_start=lt1,
                min_stub_end=lt2,
                bend_type=btype,
            )
            if was_tuned:
                res = tuned_res
                if res.curve_type in VALID_SBEND_CURVES:
                    int_note = ", S-bend stub tuned for integer um"
                else:
                    cs_used = cs.with_overrides(radius=used_r)
                    int_note = f", Reff tuned {cs.radius:.1f}->{used_r:.3f}"
            elif port2.inst_obj is not None and not _same_inst_ref(port1.inst_obj, port2.inst_obj):
                # Fallback for very shallow S-bend: small <= 0.5 um axial slide on movable Port 2
                snap_port2_needed = True
                snap_delta_axial = round(res.total_length) - float(res.total_length)
        elif port2.inst_obj is not None and not _same_inst_ref(port1.inst_obj, port2.inst_obj):
            # Straight 0-bend route with fractional gap (e.g. 100.0115 um): snap Port 2 to 1 um step!
            snap_port2_needed = True

    label_offset = max(4.0, 0.5 * _max_cs_width(cs_used) + 2.0)

    with _view_transaction(view, tx_title):
        eff_port2 = port2
        if snap_port2_needed and port2.inst_obj is not None:
            if not res.bends:
                sn = snap_instance_relative_grid(
                    port2.inst_obj,
                    port2,
                    port1,
                    step_um=DEFAULT_SHIFT_STEP_UM,
                    dbu=dbu,
                )
                if sn["moved"]:
                    eff_port2 = sn["port"]
                    res2 = plan_route_ports(
                        port1,
                        eff_port2,
                        radius=cs_used.radius,
                        euler_p=cs_used.euler_p,
                        min_straight=cs_used.min_straight,
                        route_mode=settings["route_mode"],
                        waypoints=wp_list,
                        use_eff_radius=settings["use_eff_radius"],
                        start_taper_len=lt1,
                        end_taper_len=lt2,
                        bend_type=btype,
                    )
                    if res2.ok:
                        res = res2
                        int_note = f", snapped {port2.inst_id} ({sn['dx']:+.3f},{sn['dy']:+.3f} um)"
            elif abs(snap_delta_axial) <= 0.51:
                # Slide Port 2 along its arrival direction (-d2) by <= 0.5 um to hit exact integer length
                rad2 = math.radians(float(port2.angle_deg))
                slide_dx = -math.cos(rad2) * snap_delta_axial
                slide_dy = -math.sin(rad2) * snap_delta_axial
                old_t = port2.inst_obj.dcplx_trans
                nx = round((float(old_t.disp.x) + slide_dx) / dbu) * dbu
                ny = round((float(old_t.disp.y) + slide_dy) / dbu) * dbu
                adx = nx - float(old_t.disp.x)
                ady = ny - float(old_t.disp.y)
                port2.inst_obj.dcplx_trans = pya.DCplxTrans(
                    float(old_t.mag), float(old_t.angle), bool(old_t.is_mirror()), pya.DVector(nx, ny)
                )
                eff_port2 = Port(
                    name=port2.name,
                    x=float(port2.x) + adx,
                    y=float(port2.y) + ady,
                    angle_deg=port2.angle_deg,
                    width=port2.width,
                    core_layer=port2.core_layer,
                    cell_name=port2.cell_name,
                    inst_id=port2.inst_id,
                    inst_path=port2.inst_path,
                    inst_obj=port2.inst_obj,
                )
                res2 = plan_route_ports(
                    port1,
                    eff_port2,
                    radius=cs_used.radius,
                    euler_p=cs_used.euler_p,
                    min_straight=cs_used.min_straight,
                    route_mode=settings["route_mode"],
                    waypoints=wp_list,
                    use_eff_radius=settings["use_eff_radius"],
                    start_taper_len=lt1,
                    end_taper_len=lt2,
                    bend_type=btype,
                )
                if res2.ok:
                    res = res2
                    int_note = f", nudged {port2.inst_id} ({adx:+.3f},{ady:+.3f} um)"

        res_annot = apply_taper_segments(
            res,
            core_width=cs_used.core_width,
            start_width=sw,
            end_width=ew,
            taper_length=taper_len,
        )
        instantiate_euler_waveguide(
            layout,
            top_cell,
            res.guide_points,
            cs_used,
            start_width=sw,
            end_width=ew,
            taper_length=taper_len,
            use_eff_radius=settings["use_eff_radius"],
            min_straight=cs_used.min_straight,
            curve_type=res.curve_type,
            sbend_angle_deg=res.sbend_angle_deg,
            bend_type=btype,
        )
        if want_annot:
            existing_routes = _collect_euler_waveguide_routes(layout, top_cell)
            obs_pts: list[tuple[float, float]] = []
            for rec in existing_routes:
                obs_pts.extend(_sample_polyline_points(rec["result"].centerline, step_um=20.0))
            draw_length_annotations_to_cell(
                layout,
                top_cell,
                res_annot,
                length_layer=DEFAULT_LENGTH_LAYER,
                label_offset=label_offset,
                label_style=str(settings.get("label_style", "card")),
                route_index=max(1, len(existing_routes)),
                obstacle_points=obs_pts,
            )

    _refresh_view_layers(view)
    if snap_port2_needed:
        _refresh_ports_table()
    _refresh_routes_table()
    taper_note = ""
    if sw is not None or ew is not None:
        taper_note = f", taper L={taper_len:.1f}um ({sw or cs_used.core_width:.3f}->{cs_used.core_width:.3f}->{ew or cs_used.core_width:.3f})"
    warn_note = f" | WARN: {'; '.join(res.warnings)}" if res.warnings else ""
    summary = format_segments_summary(res_annot.segments, res_annot.total_length, prefix="Total")
    _set_status(
        f"Routed {port1.full_name} -> {port2.full_name}: {summary}{int_note}{taper_note}{warn_note}",
        is_error=bool(res.warnings),
    )
    return True


def _constrain_ortho_point(
    px: float,
    py: float,
    anchor_x: float,
    anchor_y: float,
    buttons: int,
    port_angle_deg: float | None = None,
    step_um: float = DEFAULT_SHIFT_STEP_UM,
) -> tuple[float, float, bool]:
    """Lock (px, py) orthogonally on an exact ``step_um`` (1.0 um minimum) grid when Shift is held.

    If ``port_angle_deg`` is provided (first waypoint leaving a start port), projects
    strictly along the port's forward ray with length quantized to >= ``step_um``.
    """
    shift_mask = int(getattr(pya.ButtonState, "ShiftKey", 1)) if pya is not None else 1
    if buttons & shift_mask:
        step = max(float(step_um), 1.0)
        if port_angle_deg is not None:
            rad = math.radians(float(port_angle_deg))
            dx, dy = math.cos(rad), math.sin(rad)
            proj = (float(px) - float(anchor_x)) * dx + (float(py) - float(anchor_y)) * dy
            q_proj = max(step, round(proj / step) * step) if proj > 0.0 else step
            wx = round((float(anchor_x) + q_proj * dx) * 1000.0) / 1000.0
            wy = round((float(anchor_y) + q_proj * dy) * 1000.0) / 1000.0
            return (wx, wy, True)
        dx_raw = float(px) - float(anchor_x)
        dy_raw = float(py) - float(anchor_y)
        if abs(dx_raw) >= abs(dy_raw):
            sign = 1.0 if dx_raw >= 0.0 else -1.0
            q_len = max(step, round(abs(dx_raw) / step) * step)
            wx = round((float(anchor_x) + sign * q_len) * 1000.0) / 1000.0
            wy = round(float(anchor_y) * 1000.0) / 1000.0
            return (wx, wy, True)
        sign = 1.0 if dy_raw >= 0.0 else -1.0
        q_len = max(step, round(abs(dy_raw) / step) * step)
        wx = round(float(anchor_x) * 1000.0) / 1000.0
        wy = round((float(anchor_y) + sign * q_len) * 1000.0) / 1000.0
        return (wx, wy, True)
    return (float(px), float(py), False)


def _same_inst_ref(inst_a: Any, inst_b: Any) -> bool:
    if inst_a is None or inst_b is None:
        return False
    try:
        return bool(inst_a == inst_b)
    except Exception:
        return inst_a is inst_b


def _resolve_live_ports(layout: Any, top_cell: Any, ports: Sequence[Port]) -> list[Port]:
    """Re-fetch current world coordinates for previously listed Port objects."""
    live = collect_world_ports(layout, top_cell, pin_layer=DEFAULT_PIN_LAYER)
    by_path = {(p.inst_path, p.name): p for p in live}
    by_name = {p.full_name: p for p in live}
    resolved: list[Port] = []
    for p in ports:
        hit = by_path.get((p.inst_path, p.name)) or by_name.get(p.full_name)
        resolved.append(hit if hit is not None else p)
    return resolved


def _execute_port_snap(
    view: Any,
    layout: Any,
    src_port: Port,
    tgt_port: Port,
    tx_title: str = "PIC Port Snap",
) -> bool:
    if src_port.inst_obj is None:
        _set_status(f"Cannot snap '{src_port.full_name}': source port is not on a movable instance.", is_error=True)
        return False
    if tgt_port.inst_obj is not None and _same_inst_ref(src_port.inst_obj, tgt_port.inst_obj):
        _set_status("Cannot snap a cell instance onto one of its own ports.", is_error=True)
        return False
    settings = _get_route_settings()
    with _view_transaction(view, tx_title):
        res = snap_instance_to_port(
            src_port.inst_obj,
            src_port,
            tgt_port,
            allow_rotate=bool(settings["allow_rotate"]),
            dbu=float(layout.dbu),
        )
    _set_status(
        f"Snapped {src_port.full_name} -> {tgt_port.full_name} "
        f"(dx={res['dx']:.3f}, dy={res['dy']:.3f}, dtheta={res['dtheta']:.2f} deg, "
        f"err={res['residual_err_um']:.4f} um)"
    )
    _refresh_ports_table()
    return True


def _execute_port_align_axis(
    view: Any,
    layout: Any,
    src_port: Port,
    tgt_port: Port,
    axis: str = "y",
    tx_title: str | None = None,
) -> bool:
    ax = str(axis).strip().lower()
    if src_port.inst_obj is None:
        _set_status(f"Cannot align '{src_port.full_name}': source port is not on a movable instance.", is_error=True)
        return False
    if tgt_port.inst_obj is not None and _same_inst_ref(src_port.inst_obj, tgt_port.inst_obj):
        _set_status("Cannot align a cell instance to one of its own ports.", is_error=True)
        return False
    title = tx_title or f"PIC Align {ax.upper()}"
    with _view_transaction(view, title):
        res = align_instance_to_port_axis(
            src_port.inst_obj,
            src_port,
            tgt_port,
            axis=ax,
            dbu=float(layout.dbu),
        )
    _set_status(
        f"Aligned {ax.upper()}: {src_port.full_name} -> {tgt_port.full_name} "
        f"(dx={res['dx']:.3f}, dy={res['dy']:.3f} um, err={res['residual_err_um']:.4f} um)"
    )
    _refresh_ports_table()
    return True


def _execute_center_pair_ports(
    view: Any,
    layout: Any,
    src_a: Port,
    src_b: Port,
    tgt_a: Port,
    tgt_b: Port,
    axis: str = "auto",
    tx_title: str = "PIC Center Pair Ports",
) -> bool:
    if src_a.inst_obj is None or src_b.inst_obj is None:
        _set_status("Movable port pair must belong to a child cell instance.", is_error=True)
        return False
    if not _same_inst_ref(src_a.inst_obj, src_b.inst_obj):
        _set_status("Movable pair (Port 1 & Port 2) must belong to the SAME cell instance.", is_error=True)
        return False
    if src_a.full_name == src_b.full_name:
        _set_status("Select two distinct ports on the movable cell.", is_error=True)
        return False
    if tgt_a.full_name == tgt_b.full_name:
        _set_status("Select two distinct ports on the target cell.", is_error=True)
        return False
    if (tgt_a.inst_obj is not None and _same_inst_ref(src_a.inst_obj, tgt_a.inst_obj)) or (
        tgt_b.inst_obj is not None and _same_inst_ref(src_a.inst_obj, tgt_b.inst_obj)
    ):
        _set_status("Target pair ports cannot belong to the same movable cell instance.", is_error=True)
        return False

    with _view_transaction(view, tx_title):
        res = align_instance_center_between_ports(
            src_a.inst_obj,
            src_a,
            src_b,
            tgt_a,
            tgt_b,
            axis=axis,
            dbu=float(layout.dbu),
        )
    _set_status(
        f"Centered {res['axis'].upper()} midpoint: ({src_a.full_name}, {src_b.name}) <-> "
        f"({tgt_a.full_name}, {tgt_b.name}) | dx={res['dx']:.3f}, dy={res['dy']:.3f} um | "
        f"side offsets={res['offset_pair_1_um']:.3f} / {res['offset_pair_2_um']:.3f} um "
        f"(err={res['residual_err_um']:.4f} um)"
    )
    _refresh_ports_table()
    return True


# ---------------------------------------------------------------------------
# Quick Actions (usable from Menu & Dock Buttons)
# ---------------------------------------------------------------------------

def action_toggle_panel() -> None:
    try:
        dock = _GUI_STATE.get("dock")
        if dock is None:
            init_gui()
            dock = _GUI_STATE.get("dock")
        if dock is None:
            return
        if dock.isVisible():
            dock.hide()
        else:
            dock.show()
            dock.raise_()
            dock.refresh_ports()
    except Exception as exc:
        _set_status(f"Toggle panel failed: {exc}", is_error=True)
        traceback.print_exc()


def action_activate_mode(mode_name: str, label: str) -> None:
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status(f"Open a layout before activating {label}.", is_error=True)
            return
        _, view, _, _ = ctx
        view.switch_mode(mode_name)
    except Exception as exc:
        _set_status(f"Failed to activate {label}: {exc}", is_error=True)
        traceback.print_exc()


def action_quick_snap_selected() -> None:
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("No active layout.", is_error=True)
            return
        _, view, layout, top_cell = ctx
        selected_insts = []
        for obj in view.each_object_selected():
            if obj.is_cell_inst():
                selected_insts.append(obj.inst())
        if not selected_insts:
            _set_status("Select a cell instance first for Quick Snap.", is_error=True)
            return

        inst = selected_insts[0]
        settings = _get_route_settings()
        core_list = list(settings.get("core_layers") or [settings["core_layer"]])
        for base_cl in ((1, 0), (2, 0)):
            if base_cl not in core_list:
                core_list.append(base_cl)
        core_layers = tuple(core_list)

        with _view_transaction(view, "PIC Quick Snap Selected Cell"):
            res = snap_instance_nearest(
                layout,
                top_cell,
                inst,
                pin_layer=DEFAULT_PIN_LAYER,
                allow_rotate=bool(settings["allow_rotate"]),
                core_layers=core_layers,
            )
        if res is None:
            _set_status(
                "Quick Snap: no compatible neighbor port found (try enabling 'Allow Rotate on Snap' or Auto-Detect Ports).",
                is_error=True,
            )
            return
        src_p = res.get("src_port")
        tgt_p = res.get("tgt_port")
        src_name = src_p.full_name if hasattr(src_p, "full_name") else str(src_p)
        tgt_name = tgt_p.full_name if hasattr(tgt_p, "full_name") else str(tgt_p)
        _set_status(
            f"Quick Snapped {src_name} -> {tgt_name} "
            f"(dx={res['dx']:.3f}, dy={res['dy']:.3f}, err={res['residual_err_um']:.4f} um)"
        )
        _refresh_ports_table()
    except Exception as exc:
        _set_status(f"Quick Snap failed: {exc}", is_error=True)
        traceback.print_exc()


def action_auto_detect_ports() -> None:
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("No active layout.", is_error=True)
            return
        _, view, layout, top_cell = ctx

        target_cells = []
        seen_cidx = set()
        skipped_pcells = 0
        for obj in view.each_object_selected():
            if obj.is_cell_inst():
                c = obj.inst().cell
                if c is None or c.cell_index() in seen_cidx:
                    continue
                if c.is_pcell_variant():
                    skipped_pcells += 1
                    continue
                seen_cidx.add(c.cell_index())
                target_cells.append(c)

        if not target_cells:
            if skipped_pcells > 0:
                _refresh_ports_table()
                _set_status(
                    f"Selected {skipped_pcells} PCell(s) already maintain dynamic PinRec ports automatically."
                )
                return
            # If nothing selected, refresh ports on all non-PCell child cells of top_cell (or top_cell itself).
            for inst in top_cell.each_inst():
                c = inst.cell
                if c is None or c.cell_index() in seen_cidx:
                    continue
                if c.is_pcell_variant():
                    continue
                seen_cidx.add(c.cell_index())
                target_cells.append(c)
            if not target_cells:
                target_cells.append(top_cell)

        settings = _get_route_settings()
        explicit_cores = list(settings.get("core_layers") or [settings["core_layer"]])
        # If user explicitly specified custom layer(s) (e.g. '1/10' after non-destructive CD Bias),
        # detect strictly on those layers so original '1/0' never shadows the biased width.
        if all(cl in ((1, 0), (2, 0)) for cl in explicit_cores):
            core_layers = list(explicit_cores)
            for base_cl in ((1, 0), (2, 0)):
                if base_cl not in core_layers:
                    core_layers.append(base_cl)
        else:
            core_layers = explicit_cores

        total_ports = 0
        cell_summaries = []
        with _view_transaction(view, "PIC Auto-Detect / Refresh Ports"):
            for cell in target_cells:
                ports = tag_detected_ports(
                    layout,
                    cell,
                    core_layers=tuple(core_layers),
                    pin_layer=DEFAULT_PIN_LAYER,
                    overwrite=True,
                )
                total_ports += len(ports)
                cell_summaries.append(f"{cell.name}:{len(ports)}")

        _refresh_view_layers(view)
        _refresh_ports_table()
        summary_str = ", ".join(cell_summaries[:5])
        if len(cell_summaries) > 5:
            summary_str += f", +{len(cell_summaries) - 5} more"
        _set_status(
            f"Refreshed {total_ports} port(s) across {len(target_cells)} cell(s) [{summary_str}] "
            "(old PinRec cleared & re-detected from current geometry)"
        )
    except Exception as exc:
        _set_status(f"Auto-Detect Ports failed: {exc}", is_error=True)
        traceback.print_exc()


def action_path_to_euler_waveguide(snap_radius_um: float = 5.0) -> None:
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("No active layout.", is_error=True)
            return
        _, view, layout, top_cell = ctx

        selected_paths = []
        for obj in view.each_object_selected():
            if not obj.is_cell_inst() and obj.shape is not None and obj.shape.is_path():
                selected_paths.append(obj)

        if not selected_paths:
            _set_status("Select one or more Path shapes first to convert to EulerWaveguide.", is_error=True)
            return

        settings = _get_route_settings()
        cs: CrossSection = settings["cross_section"]
        world_ports = collect_world_ports(layout, top_cell, pin_layer=DEFAULT_PIN_LAYER)

        converted = 0
        snapped_ends = 0
        with _view_transaction(view, "PIC Path -> Euler Waveguide"):
            for obj in selected_paths:
                dpath = obj.shape.dpath
                dtrans = obj.dtrans()
                pts = []
                for pt in dpath.each_point():
                    wpt = dtrans * pt
                    pts.append((float(wpt.x), float(wpt.y)))
                if len(pts) < 2:
                    continue

                sw = None
                ew = None
                lt1 = 0.0
                lt2 = 0.0
                p_start, _ = _find_nearest_port(world_ports, pts[0][0], pts[0][1], max_dist=snap_radius_um)
                if p_start is not None:
                    pts[0] = (p_start.x, p_start.y)
                    snapped_ends += 1
                    sw, lt1 = _compute_port_taper(p_start.width, cs.core_width, settings)

                p_end, _ = _find_nearest_port(
                    world_ports,
                    pts[-1][0],
                    pts[-1][1],
                    max_dist=snap_radius_um,
                    exclude_full_name=(p_start.full_name if p_start is not None else None),
                )
                if p_end is not None:
                    pts[-1] = (p_end.x, p_end.y)
                    snapped_ends += 1
                    ew, lt2 = _compute_port_taper(p_end.width, cs.core_width, settings)

                cs_use = cs
                btype = str(settings.get("bend_type", "topic") or "topic").lower()
                if btype not in VALID_BEND_TYPES:
                    btype = "topic"
                res_p = route_from_guide_points(
                    pts,
                    radius=cs.radius,
                    euler_p=cs.euler_p,
                    min_straight=cs.min_straight,
                    use_eff_radius=settings["use_eff_radius"],
                    min_straight_ends=0.0,
                    bend_type=btype,
                )
                if res_p.ok and settings.get("integer_length", True) and res_p.bends:
                    tuned_res, used_r, was_tuned = tune_route_for_integer_length(
                        res_p,
                        radius=cs.radius,
                        euler_p=cs.euler_p,
                        min_straight=cs.min_straight,
                        use_eff_radius=settings["use_eff_radius"],
                        step_um=1.0,
                        dbu=float(layout.dbu),
                        min_stub_start=lt1,
                        min_stub_end=lt2,
                        bend_type=btype,
                    )
                    if was_tuned:
                        res_p = tuned_res
                        cs_use = cs.with_overrides(radius=used_r)

                instantiate_euler_waveguide(
                    layout,
                    top_cell,
                    res_p.guide_points if res_p.ok else pts,
                    cs_use,
                    start_width=sw,
                    end_width=ew,
                    taper_length=settings["taper_length"],
                    use_eff_radius=settings["use_eff_radius"],
                    min_straight=cs_use.min_straight,
                    bend_type=btype,
                )
                if res_p.ok and settings.get("annotate_lengths", True):
                    res_p_ann = apply_taper_segments(
                        res_p,
                        core_width=cs_use.core_width,
                        start_width=sw,
                        end_width=ew,
                        taper_length=float(settings["taper_length"]),
                    )
                    label_offset = max(4.0, 0.5 * _max_cs_width(cs_use) + 2.0)
                    existing_routes = _collect_euler_waveguide_routes(layout, top_cell)
                    obs_pts_p: list[tuple[float, float]] = []
                    for rec in existing_routes:
                        obs_pts_p.extend(_sample_polyline_points(rec["result"].centerline, step_um=20.0))
                    draw_length_annotations_to_cell(
                        layout,
                        top_cell,
                        res_p_ann,
                        length_layer=DEFAULT_LENGTH_LAYER,
                        label_offset=label_offset,
                        label_style=str(settings.get("label_style", "card")),
                        route_index=max(1, len(existing_routes)),
                        obstacle_points=obs_pts_p,
                    )
                obj.shape.delete()
                converted += 1

        view.clear_selection()
        _refresh_view_layers(view)
        _refresh_ports_table()
        _refresh_routes_table()
        _set_status(
            f"Converted {converted} Path(s) -> EulerWaveguide PCell "
            f"({snapped_ends} endpoint(s) snapped to nearby ports within {snap_radius_um:.1f} um)"
        )
    except Exception as exc:
        _set_status(f"Path -> EulerWaveguide failed: {exc}", is_error=True)
        traceback.print_exc()


def action_refresh_length_layer() -> None:
    """Regenerate clean, globally collision-free RouteLength (90/2) annotations for all EulerWaveguide PCells."""
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("No active layout.", is_error=True)
            return
        _, view, layout, top_cell = ctx
        settings = _get_route_settings()
        label_style = str(settings.get("label_style", "card"))

        routes = _collect_euler_waveguide_routes(layout, top_cell)
        all_obs: list[tuple[float, float]] = []
        for rec in routes:
            all_obs.extend(_sample_polyline_points(rec["result"].centerline, step_um=20.0))

        annotated = 0
        occ_boxes: list[tuple[float, float, float, float]] = []
        with _view_transaction(view, "PIC Refresh RouteLength (90/2)"):
            li_old = layout.find_layer(DEFAULT_LENGTH_LAYER[0], DEFAULT_LENGTH_LAYER[1])
            if li_old is not None and li_old >= 0:
                top_cell.shapes(li_old).clear()
            for rec in routes:
                label_offset = max(4.0, 0.5 * float(rec["max_width"]) + 2.0)
                draw_length_annotations_to_cell(
                    layout,
                    top_cell,
                    rec["result"],
                    length_layer=DEFAULT_LENGTH_LAYER,
                    label_offset=label_offset,
                    label_style=label_style,
                    route_index=int(rec["index"]),
                    occupied_boxes=occ_boxes,
                    obstacle_points=all_obs,
                )
                annotated += 1

        _refresh_view_layers(view)
        _refresh_ports_table()
        _refresh_routes_table()
        _set_status(
            f"Refreshed RouteLength ({DEFAULT_LENGTH_LAYER[0]}/{DEFAULT_LENGTH_LAYER[1]}, {label_style}) "
            f"for {annotated} EulerWaveguide PCell(s)."
        )
    except Exception as exc:
        _set_status(f"Refresh RouteLength failed: {exc}", is_error=True)
        traceback.print_exc()


def action_delete_length_layer() -> None:
    """Permanently delete the RouteLength (90/2) annotation layer from the active layout."""
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("No active layout.", is_error=True)
            return
        _, view, layout, _ = ctx
        li = layout.find_layer(DEFAULT_LENGTH_LAYER[0], DEFAULT_LENGTH_LAYER[1])
        if li is None or li < 0:
            _set_status(f"RouteLength layer ({DEFAULT_LENGTH_LAYER[0]}/{DEFAULT_LENGTH_LAYER[1]}) is already empty.")
            return
        with _view_transaction(view, "PIC Delete RouteLength (90/2)"):
            layout.delete_layer(li)
        _set_status(f"Deleted RouteLength layer ({DEFAULT_LENGTH_LAYER[0]}/{DEFAULT_LENGTH_LAYER[1]}).")
    except Exception as exc:
        _set_status(f"Delete RouteLength layer failed: {exc}", is_error=True)
        traceback.print_exc()


def action_add_manual_straight(length_um: float, dir_name: str = "Cursor") -> bool:
    """Add an exact-length straight segment to the active Route 2 Ports or Free Draw session."""
    try:
        plugin = _GUI_STATE.get("active_route_plugin") or _GUI_STATE.get("active_free_draw_plugin")
        if plugin is None:
            _set_status(
                "Activate Route 2 Ports (Ctrl+Shift+R) or Free Draw Waveguide (Ctrl+Shift+F) "
                "and place a Start Port/Point first.",
                is_error=True,
            )
            return False
        return bool(plugin.commit_manual_straight(float(length_um), dir_name=str(dir_name)))
    except Exception as exc:
        _set_status(f"Add manual straight failed: {exc}", is_error=True)
        traceback.print_exc()
        return False


def action_revert_waveguide_to_path() -> None:
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("No active layout.", is_error=True)
            return
        _, view, layout, top_cell = ctx

        wg_insts = []
        for obj in view.each_object_selected():
            if obj.is_cell_inst():
                inst = obj.inst()
                if inst.is_pcell() and inst.cell is not None and inst.cell.pcell_name() == "EulerWaveguide":
                    wg_insts.append(inst)

        if not wg_insts:
            _set_status("Select one or more EulerWaveguide PCell instances to revert.", is_error=True)
            return

        reverted = 0
        with _view_transaction(view, "PIC Revert EulerWaveguide -> Path"):
            for inst in wg_insts:
                params = inst.pcell_parameters_by_name()
                path_shape = params.get("path")
                if path_shape is None:
                    continue
                core_layer = params.get("core_layer")
                core_w = float(params.get("core_width", 0.80))
                if isinstance(path_shape, pya.DPath):
                    raw_dpath = path_shape
                elif isinstance(path_shape, pya.Path):
                    dbu = float(layout.dbu)
                    raw_dpath = pya.DPath(
                        [pya.DPoint(p.x * dbu, p.y * dbu) for p in path_shape.each_point()],
                        core_w,
                    )
                else:
                    continue

                t = inst.dcplx_trans
                wpts = [t * pt for pt in raw_dpath.each_point()]
                out_path = pya.DPath(wpts, core_w)
                if core_layer is not None and getattr(core_layer, "layer", -1) >= 0:
                    lidx = layout.layer(core_layer.layer, core_layer.datatype)
                else:
                    lidx = layout.layer(DEFAULT_GUIDE_LAYER[0], DEFAULT_GUIDE_LAYER[1])
                top_cell.shapes(lidx).insert(out_path)
                inst.delete()
                reverted += 1

        view.clear_selection()
        _refresh_view_layers(view)
        _refresh_ports_table()
        _refresh_routes_table()
        _set_status(f"Reverted {reverted} EulerWaveguide PCell(s) back to raw DPath.")
    except Exception as exc:
        _set_status(f"Revert Waveguide -> Path failed: {exc}", is_error=True)
        traceback.print_exc()


def _exec_dialog(dlg: Any) -> bool:
    """Qt5 & Qt6 compatible QDialog execution returning True when accepted."""
    fn = getattr(dlg, "exec_", None) or getattr(dlg, "exec", None)
    if fn is None:
        return False
    res = fn()
    accepted = getattr(pya.QDialog, "Accepted", 1)
    return bool(int(res) == int(accepted)) if isinstance(res, int) else bool(res)


def _describe_selection_scope(view: Any, top_cell: Any) -> tuple[list[Any], list[Any], str]:
    sel_insts: list[Any] = []
    sel_shapes: list[Any] = []
    for obj in view.each_object_selected():
        if obj.is_cell_inst():
            sel_insts.append(obj.inst())
        elif obj.shape is not None and (obj.shape.is_polygon() or obj.shape.is_box() or obj.shape.is_path()):
            sel_shapes.append(obj)
    if sel_insts:
        names = []
        seen = set()
        for inst in sel_insts:
            nm = inst.cell.name if inst.cell is not None else "?"
            if nm not in seen:
                seen.add(nm)
                names.append(nm)
        preview = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
        return (sel_insts, sel_shapes, f"Selected {len(sel_insts)} instance(s) [{preview}]")
    if sel_shapes:
        return (sel_insts, sel_shapes, f"Selected {len(sel_shapes)} shape(s) in '{top_cell.name}'")
    return (sel_insts, sel_shapes, f"Active cell '{top_cell.name}' (nothing selected -> whole cell)")


def execute_add_trench(
    core_layers_text: str,
    trench_layers_text: str,
    default_trench_w: float = 10.0,
    nominal_core_w: float = 0.80,
    width_mode: str = "total_width",
    trim_at_ports: bool = True,
    hollow_minus_core: bool = False,
    overwrite_existing: bool = True,
) -> bool:
    """Generate single- or multi-layer trench(es) around selected cell(s)/shapes or the active cell."""
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("No active layout.", is_error=True)
            return False
        _, view, layout, top_cell = ctx
        dbu = float(layout.dbu)

        core_entries = parse_layer_list_text(core_layers_text)
        trench_entries = parse_layer_list_text(trench_layers_text)
        if not core_entries:
            _set_status("Add Trench: specify at least one valid Core L/D (e.g. '1/0' or '1/0; 2/0').", is_error=True)
            return False
        if not trench_entries:
            _set_status("Add Trench: specify at least one valid Trench L/D (e.g. '1/1; 2/1=25').", is_error=True)
            return False

        core_layers = [(l, d) for l, d, _ in core_entries]
        trench_specs: list[tuple[int, int, float]] = [
            (l, d, float(w_ovr) if (w_ovr is not None and w_ovr > 0.0) else float(default_trench_w))
            for l, d, w_ovr in trench_entries
        ]
        sel_insts, sel_shapes, _ = _describe_selection_scope(view, top_cell)

        pcells_done = 0
        cells_done = 0
        total_layers_written = 0
        errors: list[str] = []

        with _view_transaction(view, "PIC Add Trench (Multi-Layer)"):
            if sel_insts:
                seen_static: set[int] = set()
                for inst in sel_insts:
                    if inst.is_pcell() and inst.cell is not None and inst.cell.pcell_name() in ("EulerWaveguide", "EulerBend", "TOPICBend"):
                        if add_trench_to_pcell_inst(
                            layout, inst, trench_specs, nominal_core_width=nominal_core_w, width_mode=width_mode
                        ):
                            pcells_done += 1
                    elif inst.cell is not None:
                        cidx = inst.cell.cell_index()
                        if cidx in seen_static:
                            continue
                        seen_static.add(cidx)
                        r = add_trench_to_cell(
                            layout,
                            inst.cell,
                            core_layers=core_layers,
                            trench_specs=trench_specs,
                            nominal_core_width=nominal_core_w,
                            width_mode=width_mode,
                            trim_at_ports=trim_at_ports,
                            hollow_minus_core=hollow_minus_core,
                            overwrite_existing=overwrite_existing,
                            pin_layer=DEFAULT_PIN_LAYER,
                        )
                        if r["ok"]:
                            cells_done += 1
                            total_layers_written += len(r["layers_written"])
                        else:
                            errors.append(str(r.get("error", "")))
            elif sel_shapes:
                core_reg = pya.Region()
                for obj in sel_shapes:
                    li_obj = obj.layer
                    linfo = layout.get_info(li_obj)
                    if (int(linfo.layer), int(linfo.datatype)) not in core_layers:
                        continue
                    tr = obj.trans()
                    sh = obj.shape
                    if sh.is_polygon():
                        core_reg.insert(sh.polygon.transformed(tr))
                    elif sh.is_box():
                        core_reg.insert(pya.Polygon(sh.box).transformed(tr))
                    elif sh.is_path():
                        core_reg.insert(sh.path.polygon().transformed(tr))
                core_reg.merge()
                if core_reg.is_empty():
                    errors.append(f"Selected shape(s) do not lie on Core L/D {core_layers}.")
                else:
                    ports_reg = (
                        detect_open_ports_on_region(core_reg, core_layer=core_layers[0], dbu=dbu)
                        if trim_at_ports
                        else []
                    )
                    r = add_trench_to_cell(
                        layout,
                        top_cell,
                        core_layers=core_layers,
                        trench_specs=trench_specs,
                        nominal_core_width=nominal_core_w,
                        width_mode=width_mode,
                        trim_at_ports=trim_at_ports,
                        hollow_minus_core=hollow_minus_core,
                        overwrite_existing=False,
                        source_region=core_reg,
                        ports_override=ports_reg,
                    )
                    if r["ok"]:
                        cells_done += 1
                        total_layers_written += len(r["layers_written"])
                    else:
                        errors.append(str(r.get("error", "")))
            else:
                r = add_trench_to_cell(
                    layout,
                    top_cell,
                    core_layers=core_layers,
                    trench_specs=trench_specs,
                    nominal_core_width=nominal_core_w,
                    width_mode=width_mode,
                    trim_at_ports=trim_at_ports,
                    hollow_minus_core=hollow_minus_core,
                    overwrite_existing=overwrite_existing,
                    pin_layer=DEFAULT_PIN_LAYER,
                )
                if r["ok"]:
                    cells_done += 1
                    total_layers_written += len(r["layers_written"])
                else:
                    errors.append(str(r.get("error", "")))

        _refresh_view_layers(view)
        if cells_done == 0 and pcells_done == 0:
            _set_status(f"Add Trench failed: {'; '.join(e for e in errors if e) or 'no matching core shapes'}", is_error=True)
            return False
        trench_desc = "; ".join(f"{l}/{d}={w:.1f}um" for l, d, w in trench_specs)
        _set_status(
            f"Added trench [{trench_desc}] to {cells_done} cell(s)"
            + (f" & {pcells_done} PCell(s)" if pcells_done else "")
            + (" (trimmed flush at ports)" if trim_at_ports else "")
        )
        return True
    except Exception as exc:
        _set_status(f"Add Trench failed: {exc}", is_error=True)
        traceback.print_exc()
        return False


def action_add_trench_dialog() -> None:
    """Open interactive dialog to add single- or multi-layer trenches to existing PIC structures."""
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("Open a layout first before adding trench.", is_error=True)
            return
        mw, view, _, top_cell = ctx
        _, _, scope_desc = _describe_selection_scope(view, top_cell)
        dock = _GUI_STATE.get("dock")
        core_init = str(dock.core_layer_edit.text) if dock is not None else "1/0"
        trench_init = str(dock.trench_layer_edit.text) if dock is not None else "1/1"
        trench_w_init = float(dock.trench_w_spin.value) if dock is not None else 10.0
        core_w_init = float(dock.core_w_spin.value) if dock is not None else 0.80

        dlg = pya.QDialog(mw)
        dlg.setWindowTitle("Add Trench to Existing PIC Structure(s)")
        refs: list[Any] = []
        keep = lambda o: (refs.append(o), o)[1]
        lay = keep(pya.QVBoxLayout(dlg))

        lbl_scope = keep(pya.QLabel(f"Scope: {scope_desc}", dlg))
        lbl_scope.setStyleSheet("QLabel { font-weight: bold; }")
        lay.addWidget(lbl_scope)

        grid = keep(pya.QGridLayout())
        grid.addWidget(keep(pya.QLabel("Core L/D (source):", dlg)), 0, 0)
        ed_core = keep(pya.QLineEdit(core_init, dlg))
        ed_core.setToolTip("Source core layer(s), e.g. '1/0' or '1/0; 2/0'")
        grid.addWidget(ed_core, 0, 1)

        grid.addWidget(keep(pya.QLabel("Trench L/D (target):", dlg)), 1, 0)
        ed_trench = keep(pya.QLineEdit(trench_init, dlg))
        ed_trench.setToolTip("Single or ';'-separated multi-layer trenches, e.g. '1/1; 2/1=25' or '1/1=10; 2/1=25'")
        grid.addWidget(ed_trench, 1, 1)

        grid.addWidget(keep(pya.QLabel("Default Trench W / Margin (um):", dlg)), 2, 0)
        sp_tw = keep(pya.QDoubleSpinBox(dlg))
        sp_tw.setRange(0.1, 500.0)
        sp_tw.setDecimals(2)
        sp_tw.setSingleStep(1.0)
        sp_tw.setValue(trench_w_init if trench_w_init > 0.1 else 10.0)
        grid.addWidget(sp_tw, 2, 1)

        grid.addWidget(keep(pya.QLabel("Width Interpretation:", dlg)), 3, 0)
        cb_wmode = keep(pya.QComboBox(dlg))
        cb_wmode.addItem("Total Trench Width: margin = (W - Core W) / 2")
        cb_wmode.addItem("Single-Side Margin: expand each side by W")
        grid.addWidget(cb_wmode, 3, 1)

        grid.addWidget(keep(pya.QLabel("Nominal Core W (um):", dlg)), 4, 0)
        sp_cw = keep(pya.QDoubleSpinBox(dlg))
        sp_cw.setRange(0.05, 50.0)
        sp_cw.setDecimals(3)
        sp_cw.setSingleStep(0.05)
        sp_cw.setValue(core_w_init)
        grid.addWidget(sp_cw, 4, 1)
        lay.addLayout(grid)

        chk_trim = keep(pya.QCheckBox("Trim trench flush at port faces (recommended: zero protrusion)", dlg))
        chk_trim.setChecked(True)
        lay.addWidget(chk_trim)

        chk_over = keep(pya.QCheckBox("Replace existing shapes on target trench layer(s)", dlg))
        chk_over.setChecked(True)
        lay.addWidget(chk_over)

        chk_hollow = keep(pya.QCheckBox("Hollow sleeve (Trench minus Core; unchecked = solid trench)", dlg))
        chk_hollow.setChecked(False)
        lay.addWidget(chk_hollow)

        hint = keep(pya.QLabel("Tip: Use ';' for multi-layer double-etch, e.g. '1/1=10; 2/1=25'.", dlg))
        hint.setStyleSheet("QLabel { color: #666; font-size: 11px; }")
        lay.addWidget(hint)

        btn_row = keep(pya.QHBoxLayout())
        btn_row.addStretch()
        btn_ok = keep(pya.QPushButton("Apply Trench", dlg))
        btn_cancel = keep(pya.QPushButton("Cancel", dlg))
        btn_ok.clicked.connect(lambda *_a: dlg.accept())
        btn_cancel.clicked.connect(lambda *_a: dlg.reject())
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        lay.addLayout(btn_row)

        if _exec_dialog(dlg):
            wmode = "side_margin" if cb_wmode.currentIndex == 1 else "total_width"
            execute_add_trench(
                core_layers_text=str(ed_core.text),
                trench_layers_text=str(ed_trench.text),
                default_trench_w=float(sp_tw.value),
                nominal_core_w=float(sp_cw.value),
                width_mode=wmode,
                trim_at_ports=bool(chk_trim.isChecked()),
                hollow_minus_core=bool(chk_hollow.isChecked()),
                overwrite_existing=bool(chk_over.isChecked()),
            )
    except Exception as exc:
        _set_status(f"Add Trench dialog error: {exc}", is_error=True)
        traceback.print_exc()


def execute_cd_bias(
    core_layers_text: str,
    bias_nm: float = 120.0,
    anisotropic: bool = False,
    bias_x_nm: float | None = None,
    bias_y_nm: float | None = None,
    preserve_port_faces: bool = True,
    output_mode: str = "in_place",
    dt_offset: int = 10,
    custom_output_text: str = "",
) -> bool:
    """Apply length-preserving CD linewidth compensation (e.g. +120 nm) to selected cells/shapes or active cell."""
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("No active layout.", is_error=True)
            return False
        _, view, layout, top_cell = ctx
        dbu = float(layout.dbu)

        core_entries = parse_layer_list_text(core_layers_text)
        if not core_entries:
            _set_status("CD Bias: specify at least one valid Target Core L/D (e.g. '1/0' or '1/0; 2/0').", is_error=True)
            return False
        core_layers = [(l, d) for l, d, _ in core_entries]

        bias_um = float(bias_nm) * 1e-3
        bx_um = float(bias_x_nm) * 1e-3 if (anisotropic and bias_x_nm is not None) else None
        by_um = float(bias_y_nm) * 1e-3 if (anisotropic and bias_y_nm is not None) else None
        if not anisotropic and abs(bias_nm) < 1e-6:
            _set_status("CD Bias: ΔW is 0.0 nm (nothing to change).", is_error=True)
            return False
        if anisotropic and abs(bias_x_nm or 0.0) < 1e-6 and abs(bias_y_nm or 0.0) < 1e-6:
            _set_status("CD Bias: both ΔW_X and ΔW_Y are 0.0 nm.", is_error=True)
            return False

        # Resolve output layer map
        out_map: dict[tuple[int, int], tuple[int, int]] | None = None
        if output_mode == "dt_offset":
            off = int(dt_offset)
            if off == 0:
                _set_status("CD Bias: Datatype offset must be non-zero for New Layer mode.", is_error=True)
                return False
            out_map = {(l, d): (l, d + off) for l, d in core_layers}
        elif output_mode == "custom_layer":
            out_entries = parse_layer_list_text(custom_output_text)
            if not out_entries:
                _set_status("CD Bias: specify valid Output L/D (e.g. '101/0' or '101/0; 102/0').", is_error=True)
                return False
            out_map = {}
            for idx, src_ld in enumerate(core_layers):
                dst_e = out_entries[idx] if idx < len(out_entries) else (out_entries[0][0] + idx, out_entries[0][1], None)
                out_map[src_ld] = (int(dst_e[0]), int(dst_e[1]))

        in_place = (out_map is None)
        sel_insts, sel_shapes, _ = _describe_selection_scope(view, top_cell)

        cells_done = 0
        pcells_done = 0
        layers_done = 0
        errors: list[str] = []

        with _view_transaction(view, f"PIC CD Bias ({bias_nm:+.1f} nm)"):
            if not in_place:
                # Non-destructive overlay mode: merge target shapes across scope, bias preserving open ports,
                # write cleanly to new output layer(s) in top_cell without modifying any source cells or PCells!
                for src_ld in core_layers:
                    dst_ld = out_map[src_ld]  # type: ignore[index]
                    li_src = layout.find_layer(*src_ld)
                    if li_src is None or li_src < 0:
                        continue
                    reg = pya.Region()
                    if sel_insts:
                        for inst in sel_insts:
                            if inst.cell is None:
                                continue
                            it = inst.cell.begin_shapes_rec(li_src)
                            reg += pya.Region(it, inst.cplx_trans)
                    elif sel_shapes:
                        for obj in sel_shapes:
                            linfo = layout.get_info(obj.layer)
                            if (int(linfo.layer), int(linfo.datatype)) != src_ld:
                                continue
                            tr = obj.trans()
                            sh = obj.shape
                            if sh.is_polygon():
                                reg.insert(sh.polygon.transformed(tr))
                            elif sh.is_box():
                                reg.insert(pya.Polygon(sh.box).transformed(tr))
                            elif sh.is_path():
                                reg.insert(sh.path.polygon().transformed(tr))
                    else:
                        reg = pya.Region(top_cell.begin_shapes_rec(li_src))
                    reg.merge()
                    if reg.is_empty():
                        continue
                    ports_open = (
                        detect_open_ports_on_region(reg, core_layer=src_ld, dbu=dbu)
                        if preserve_port_faces
                        else []
                    )
                    biased = bias_region_preserve_length(
                        reg,
                        bias_um=bias_um,
                        dbu=dbu,
                        ports=ports_open,
                        bias_x_um=bx_um,
                        bias_y_um=by_um,
                        preserve_port_faces=preserve_port_faces,
                    )
                    if not biased.is_empty():
                        li_dst = layout.layer(*dst_ld)
                        if not sel_insts and not sel_shapes:
                            top_cell.shapes(li_dst).clear()
                        top_cell.shapes(li_dst).insert(biased)
                        layers_done += 1
                        cells_done = 1
            else:
                # In-place mode: preserve hierarchy, deduplicate cells, keep PCells parametric when isotropic!
                if sel_insts:
                    seen_cells: set[int] = set()
                    for inst in sel_insts:
                        if inst.is_pcell() and inst.cell is not None and inst.cell.pcell_name() in ("EulerWaveguide", "EulerBend", "TOPICBend"):
                            if anisotropic:
                                errors.append(
                                    f"Skipped anisotropic in-place on parametric PCell '{inst.cell.pcell_name()}' "
                                    "(use New Layer output mode for anisotropic PCell masks)."
                                )
                                continue
                            rp = bias_pcell_inst_linewidth(inst, bias_um=bias_um, target_core_layers=core_layers)
                            if rp.get("ok"):
                                pcells_done += 1
                            elif rp.get("error"):
                                errors.append(str(rp["error"]))
                        elif inst.cell is not None:
                            sub_static, sub_pcells = collect_owning_static_cells(layout, inst.cell, core_layers)
                            for sc in sub_static:
                                if sc.cell_index() in seen_cells:
                                    continue
                                seen_cells.add(sc.cell_index())
                                r = bias_cell_linewidth(
                                    layout, sc, core_layers, bias_um=bias_um,
                                    bias_x_um=bx_um, bias_y_um=by_um,
                                    preserve_port_faces=preserve_port_faces,
                                    output_layer_map=None, pin_layer=DEFAULT_PIN_LAYER,
                                )
                                if r["ok"]:
                                    cells_done += 1
                                    layers_done += len(r["layers_done"])
                            for spc in sub_pcells:
                                if not anisotropic:
                                    rp = bias_pcell_inst_linewidth(spc, bias_um=bias_um, target_core_layers=core_layers)
                                    if rp.get("ok"):
                                        pcells_done += 1
                elif sel_shapes:
                    # Only replace the selected shapes on matching core layers (never touch unselected shapes!)
                    by_layer: dict[tuple[int, int], list[Any]] = {}
                    for obj in sel_shapes:
                        linfo = layout.get_info(obj.layer)
                        ld = (int(linfo.layer), int(linfo.datatype))
                        if ld in core_layers:
                            by_layer.setdefault(ld, []).append(obj)
                    for src_ld, objs in by_layer.items():
                        reg = pya.Region()
                        for obj in objs:
                            tr = obj.trans()
                            sh = obj.shape
                            if sh.is_polygon():
                                reg.insert(sh.polygon.transformed(tr))
                            elif sh.is_box():
                                reg.insert(pya.Polygon(sh.box).transformed(tr))
                            elif sh.is_path():
                                reg.insert(sh.path.polygon().transformed(tr))
                        reg.merge()
                        if reg.is_empty():
                            continue
                        ports_open = (
                            detect_open_ports_on_region(reg, core_layer=src_ld, dbu=dbu)
                            if preserve_port_faces
                            else []
                        )
                        biased = bias_region_preserve_length(
                            reg, bias_um=bias_um, dbu=dbu, ports=ports_open,
                            bias_x_um=bx_um, bias_y_um=by_um,
                            preserve_port_faces=preserve_port_faces,
                        )
                        if not biased.is_empty():
                            for obj in objs:
                                obj.shape.delete()
                            li_dst = layout.layer(*src_ld)
                            top_cell.shapes(li_dst).insert(biased)
                            cells_done = 1
                            layers_done += 1
                else:
                    # Whole active cell in-place: deduplicate every static child cell + parametric PCells
                    sub_static, sub_pcells = collect_owning_static_cells(layout, top_cell, core_layers)
                    for sc in sub_static:
                        r = bias_cell_linewidth(
                            layout, sc, core_layers, bias_um=bias_um,
                            bias_x_um=bx_um, bias_y_um=by_um,
                            preserve_port_faces=preserve_port_faces,
                            output_layer_map=None, pin_layer=DEFAULT_PIN_LAYER,
                        )
                        if r["ok"]:
                            cells_done += 1
                            layers_done += len(r["layers_done"])
                    for spc in sub_pcells:
                        if anisotropic:
                            errors.append("Skipped anisotropic in-place on PCell (use New Layer mode).")
                            continue
                        rp = bias_pcell_inst_linewidth(spc, bias_um=bias_um, target_core_layers=core_layers)
                        if rp.get("ok"):
                            pcells_done += 1
                        elif rp.get("error"):
                            errors.append(str(rp["error"]))

        _refresh_view_layers(view)
        _refresh_ports_table()
        _refresh_routes_table()
        if cells_done == 0 and pcells_done == 0 and layers_done == 0:
            _set_status(f"CD Bias failed: {'; '.join(e for e in errors if e) or 'no matching core shapes'}", is_error=True)
            return False

        bias_txt = (
            f"ΔWx={bias_x_nm:+.1f}nm, ΔWy={bias_y_nm:+.1f}nm"
            if anisotropic
            else f"ΔW={bias_nm:+.1f}nm ({0.5 * bias_nm:+.1f}nm/side)"
        )
        out_txt = (
            "in-place"
            if in_place
            else ", ".join(f"{s[0]}/{s[1]}->{d[0]}/{d[1]}" for s, d in out_map.items())  # type: ignore[union-attr]
        )
        warn_s = f" | Note: {'; '.join(errors)}" if errors else ""
        _set_status(
            f"CD Bias [{bias_txt}] -> {out_txt}: {cells_done} cell(s)"
            + (f", {pcells_done} PCell(s)" if pcells_done else "")
            + (" | length & port faces preserved (0.000 nm shift)" if preserve_port_faces else "")
            + warn_s
        )
        return True
    except Exception as exc:
        _set_status(f"CD Bias failed: {exc}", is_error=True)
        traceback.print_exc()
        return False


def action_cd_bias_dialog() -> None:
    """Open interactive dialog for length-preserving PIC linewidth compensation (CD bias)."""
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("Open a layout first before running CD Bias.", is_error=True)
            return
        mw, view, _, top_cell = ctx
        _, _, scope_desc = _describe_selection_scope(view, top_cell)
        dock = _GUI_STATE.get("dock")
        core_init = str(dock.core_layer_edit.text) if dock is not None else "1/0"

        dlg = pya.QDialog(mw)
        dlg.setWindowTitle("PIC Linewidth Compensation (CD Bias)")
        refs: list[Any] = []
        keep = lambda o: (refs.append(o), o)[1]
        lay = keep(pya.QVBoxLayout(dlg))

        lbl_scope = keep(pya.QLabel(f"Scope: {scope_desc}", dlg))
        lbl_scope.setStyleSheet("QLabel { font-weight: bold; }")
        lay.addWidget(lbl_scope)

        grid = keep(pya.QGridLayout())
        grid.addWidget(keep(pya.QLabel("Target Core L/D:", dlg)), 0, 0)
        ed_core = keep(pya.QLineEdit(core_init, dlg))
        ed_core.setToolTip("Core layer(s) to compensate, e.g. '1/0' or '1/0; 2/0'")
        grid.addWidget(ed_core, 0, 1)

        grid.addWidget(keep(pya.QLabel("Total Width Bias ΔW (nm):", dlg)), 1, 0)
        sp_dw = keep(pya.QDoubleSpinBox(dlg))
        sp_dw.setRange(-5000.0, 5000.0)
        sp_dw.setDecimals(1)
        sp_dw.setSingleStep(10.0)
        sp_dw.setValue(120.0)
        sp_dw.setToolTip("Total linewidth change: +120 nm turns 700 nm waveguide into 820 nm (+60 nm per sidewall)")
        grid.addWidget(sp_dw, 1, 1)

        chk_aniso = keep(pya.QCheckBox("Anisotropic X/Y Bias (optional, e.g. X-cut crystal)", dlg))
        chk_aniso.setChecked(False)
        grid.addWidget(chk_aniso, 2, 0, 1, 2)

        lbl_bx = keep(pya.QLabel("ΔW Vertical WG (X-bias, nm):", dlg))
        sp_bx = keep(pya.QDoubleSpinBox(dlg))
        sp_bx.setRange(-5000.0, 5000.0)
        sp_bx.setDecimals(1)
        sp_bx.setSingleStep(10.0)
        sp_bx.setValue(120.0)
        grid.addWidget(lbl_bx, 3, 0)
        grid.addWidget(sp_bx, 3, 1)

        lbl_by = keep(pya.QLabel("ΔW Horizontal WG (Y-bias, nm):", dlg))
        sp_by = keep(pya.QDoubleSpinBox(dlg))
        sp_by.setRange(-5000.0, 5000.0)
        sp_by.setDecimals(1)
        sp_by.setSingleStep(10.0)
        sp_by.setValue(120.0)
        grid.addWidget(lbl_by, 4, 0)
        grid.addWidget(sp_by, 4, 1)

        def _sync_aniso(*_a: Any) -> None:
            on = bool(chk_aniso.isChecked())
            sp_dw.setEnabled(not on)
            lbl_bx.setEnabled(on)
            sp_bx.setEnabled(on)
            lbl_by.setEnabled(on)
            sp_by.setEnabled(on)

        chk_aniso.toggled.connect(_sync_aniso)
        _sync_aniso()

        grid.addWidget(keep(pya.QLabel("Output Mode:", dlg)), 5, 0)
        cb_out = keep(pya.QComboBox(dlg))
        cb_out.addItem("New Layer: Datatype + Offset (Safe Overlay)")
        cb_out.addItem("In-Place: Modify Original Layer (Ctrl+Z Undo)")
        cb_out.addItem("New Layer: Custom Output L/D")
        grid.addWidget(cb_out, 5, 1)

        lbl_off = keep(pya.QLabel("Datatype Offset (+N):", dlg))
        sp_off = keep(pya.QSpinBox(dlg))
        sp_off.setRange(1, 999)
        sp_off.setValue(10)
        sp_off.setToolTip("Example: offset +10 writes 1/0 -> 1/10, 2/0 -> 2/10")
        grid.addWidget(lbl_off, 6, 0)
        grid.addWidget(sp_off, 6, 1)

        lbl_cust = keep(pya.QLabel("Custom Output L/D:", dlg))
        ed_cust = keep(pya.QLineEdit("101/0", dlg))
        ed_cust.setToolTip("Matching output layer(s), e.g. '101/0' or '101/0; 102/0'")
        grid.addWidget(lbl_cust, 7, 0)
        grid.addWidget(ed_cust, 7, 1)

        def _sync_out(*_a: Any) -> None:
            idx = int(cb_out.currentIndex)
            lbl_off.setEnabled(idx == 0)
            sp_off.setEnabled(idx == 0)
            lbl_cust.setEnabled(idx == 2)
            ed_cust.setEnabled(idx == 2)

        cb_out.currentIndexChanged.connect(_sync_out)
        _sync_out()
        lay.addLayout(grid)

        chk_preserve = keep(
            pya.QCheckBox("Preserve port faces & total optical length (0.000 nm shift on bends/straights)", dlg)
        )
        chk_preserve.setChecked(True)
        lay.addWidget(chk_preserve)

        hint = keep(
            pya.QLabel(
                "Uses isotropic normal offset (+ΔW/2 per sidewall) with Extend -> Size -> Flush Trim:\n"
                "compensates H, V, and curved bends identically while keeping total centerline length fixed.",
                dlg,
            )
        )
        hint.setStyleSheet("QLabel { color: #666; font-size: 11px; }")
        lay.addWidget(hint)

        btn_row = keep(pya.QHBoxLayout())
        btn_row.addStretch()
        btn_ok = keep(pya.QPushButton("Apply CD Bias", dlg))
        btn_cancel = keep(pya.QPushButton("Cancel", dlg))
        btn_ok.clicked.connect(lambda *_a: dlg.accept())
        btn_cancel.clicked.connect(lambda *_a: dlg.reject())
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        lay.addLayout(btn_row)

        if _exec_dialog(dlg):
            idx = int(cb_out.currentIndex)
            omode = "dt_offset" if idx == 0 else ("in_place" if idx == 1 else "custom_layer")
            execute_cd_bias(
                core_layers_text=str(ed_core.text),
                bias_nm=float(sp_dw.value),
                anisotropic=bool(chk_aniso.isChecked()),
                bias_x_nm=float(sp_bx.value),
                bias_y_nm=float(sp_by.value),
                preserve_port_faces=bool(chk_preserve.isChecked()),
                output_mode=omode,
                dt_offset=int(sp_off.value),
                custom_output_text=str(ed_cust.text),
            )
    except Exception as exc:
        _set_status(f"CD Bias dialog error: {exc}", is_error=True)
        traceback.print_exc()


def execute_create_topic_bend(
    r_eff: float = 50.0,
    angle_deg: float = 90.0,
    p: float = 0.50,
    straight_in: float = 0.0,
    straight_out: float = 0.0,
    use_eff_radius: bool = True,
    add_ports: bool = True,
    annotate_length: bool = True,
    origin: tuple[float, float] | None = None,
    rotation_deg: float = 0.0,
    cross_section: CrossSection | None = None,
) -> bool:
    """Create and insert a standalone constant-width multi-layer ``TOPICBend`` PCell into the active cell."""
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("Open a layout first before creating a TOPIC bend.", is_error=True)
            return False
        _, view, layout, top_cell = ctx
        settings = _get_route_settings()
        cs_base: CrossSection = cross_section if cross_section is not None else settings["cross_section"]
        p_clamped = max(0.0, min(1.0, float(p)))
        r_val = max(0.5, float(r_eff))
        s_in = max(0.0, float(straight_in))
        s_out = max(0.0, float(straight_out))
        ang = float(angle_deg)
        if abs(ang) < 0.1 or abs(ang) > 359.0:
            _set_status(f"Create TOPIC Bend: angle must be in [-359, -0.1] or [0.1, 359] deg (got {ang}).", is_error=True)
            return False

        cs_use = cs_base.with_overrides(radius=r_val, euler_p=p_clamped)
        m = topic_bend_metrics(ang, r_val, p=p_clamped, use_eff_radius=bool(use_eff_radius))

        if origin is not None:
            ox, oy = float(origin[0]), float(origin[1])
        else:
            try:
                vbox = view.box()
                ox = round(0.5 * (float(vbox.left) + float(vbox.right)))
                oy = round(0.5 * (float(vbox.bottom) + float(vbox.top)))
            except Exception:
                ox, oy = 0.0, 0.0

        rot_norm = float(rotation_deg) % 360.0
        rot_int = int(round(rot_norm / 90.0)) % 4
        if abs(rot_norm - rot_int * 90.0) < 1e-6:
            dtrans: Any = pya.DTrans(rot_int, False, ox, oy)
        else:
            dtrans = pya.DCplxTrans(1.0, rot_norm, False, pya.DVector(ox, oy))

        total_l = float(m["L_total"]) + s_in + s_out
        with _view_transaction(view, f"PIC Create TOPIC Bend ({ang:.1f}deg, Reff={r_val:.1f}, p={p_clamped:.2f})"):
            inst = instantiate_topic_bend(
                layout,
                top_cell,
                angle_deg=ang,
                cross_section=cs_use,
                r_eff=r_val,
                p=p_clamped,
                straight_in=s_in,
                straight_out=s_out,
                trans=dtrans,
                use_eff_radius=bool(use_eff_radius),
                add_ports=bool(add_ports),
            )
            if annotate_length:
                li_len = layout.layer(DEFAULT_LENGTH_LAYER[0], DEFAULT_LENGTH_LAYER[1])
                bb = inst.dbbox()
                lbl_x = float(bb.left)
                lbl_y = float(bb.top) + max(4.0, 0.5 * _max_cs_width(cs_use) + 2.0)
                ang_s = f"{ang:.0f}deg" if abs(ang - round(ang)) < 0.05 else f"{ang:.1f}deg"
                lead_s = f" (+S_in={s_in:.1f}, S_out={s_out:.1f})" if (s_in > 1e-4 or s_out > 1e-4) else ""
                txt = (
                    f"TOPICBend {ang_s}: L={total_l:.3f} um{lead_s}\n"
                    f"[Reff={m['R_eff']:.2f}, Rc={m['R_min']:.2f} um, p={m['p']:.3f}, θp={m['theta_p_deg']:.2f}°]"
                )
                top_cell.shapes(li_len).insert(pya.DText(txt, pya.DTrans(pya.DVector(lbl_x, lbl_y))))

        _refresh_view_layers(view)
        if add_ports:
            _refresh_ports_table()
        _set_status(
            f"Created TOPICBend ({ang:.1f}°, Reff={m['R_eff']:.2f} um, Rc={m['R_min']:.2f} um, "
            f"p={m['p']:.3f} / θp={m['theta_p_deg']:.2f}°): "
            f"L_bend={m['L_total']:.3f} um (2×L_TOP={2.0 * m['L_top']:.3f} + L_CIRC={m['L_circ']:.3f}), "
            f"Total={total_l:.3f} um at ({ox:.1f}, {oy:.1f})"
        )
        return True
    except Exception as exc:
        _set_status(f"Create TOPIC Bend failed: {exc}", is_error=True)
        traceback.print_exc()
        return False


def action_create_topic_bend_dialog() -> None:
    """Open interactive dialog to generate a constant-width multi-layer TOPIC bend (default 90°)."""
    try:
        ctx = _get_active_context()
        if ctx is None:
            _set_status("Open a layout first before creating a TOPIC bend.", is_error=True)
            return
        mw, _, _, _ = ctx
        settings = _get_route_settings()
        r_init = float(settings.get("radius", 50.0))
        p_init = float(settings.get("euler_p", 0.50))
        cs_init: CrossSection = settings["cross_section"]

        dlg = pya.QDialog(mw)
        dlg.setWindowTitle("Create Constant-Width TOPIC Bend (C3 Hermite)")
        refs: list[Any] = []
        keep = lambda o: (refs.append(o), o)[1]
        lay = keep(pya.QVBoxLayout(dlg))

        cs_desc = ", ".join(f"{ls.layer}/{ls.datatype}({ls.width:.2f}um)" for ls in cs_init.layers)
        lbl_cs = keep(pya.QLabel(f"Cross-Section (from Dock): {cs_init.name} [{cs_desc}]", dlg))
        lbl_cs.setStyleSheet("QLabel { font-weight: bold; }")
        lbl_cs.setWordWrap(True)
        lay.addWidget(lbl_cs)

        grid = keep(pya.QGridLayout())

        grid.addWidget(keep(pya.QLabel("Effective Radius R_eff (um):", dlg)), 0, 0)
        sp_r = keep(pya.QDoubleSpinBox(dlg))
        sp_r.setRange(1.0, 5000.0)
        sp_r.setDecimals(2)
        sp_r.setSingleStep(5.0)
        sp_r.setValue(r_init)
        sp_r.setToolTip("Effective bend radius R_eff = footprint dx = dy for 90° bend")
        grid.addWidget(sp_r, 0, 1)

        grid.addWidget(keep(pya.QLabel("Bend Angle θ_t (deg):", dlg)), 1, 0)
        sp_ang = keep(pya.QDoubleSpinBox(dlg))
        sp_ang.setRange(-359.0, 359.0)
        sp_ang.setDecimals(2)
        sp_ang.setSingleStep(15.0)
        sp_ang.setValue(90.0)
        sp_ang.setToolTip("Total bend angle (+90° left turn, -90° right turn, 45°, 180°, etc.)")
        grid.addWidget(sp_ang, 1, 1)

        grid.addWidget(keep(pya.QLabel("TOPIC Fraction p (0..1):", dlg)), 2, 0)
        sp_p = keep(pya.QDoubleSpinBox(dlg))
        sp_p.setRange(0.0, 1.0)
        sp_p.setDecimals(4)
        sp_p.setSingleStep(0.05)
        sp_p.setValue(p_init)
        sp_p.setToolTip("p = 2·θ_p / |θ_t|: 0 = circular arc, 0.50 = 50% transition + 50% circular, 1 = pure cubic TOP")
        grid.addWidget(sp_p, 2, 1)

        grid.addWidget(keep(pya.QLabel("Transition Angle θ_p (deg):", dlg)), 3, 0)
        sp_tp = keep(pya.QDoubleSpinBox(dlg))
        sp_tp.setRange(0.0, 179.5)
        sp_tp.setDecimals(3)
        sp_tp.setSingleStep(1.0)
        sp_tp.setValue(0.5 * p_init * 90.0)
        sp_tp.setToolTip("Synced with p: θ_p = 0.5 · p · |θ_t| (e.g. θ_p = 26° at 90° corresponds to p = 0.5778)")
        grid.addWidget(sp_tp, 3, 1)

        grid.addWidget(keep(pya.QLabel("Input Straight Lead (um):", dlg)), 4, 0)
        sp_sin = keep(pya.QDoubleSpinBox(dlg))
        sp_sin.setRange(0.0, 5000.0)
        sp_sin.setDecimals(2)
        sp_sin.setSingleStep(5.0)
        sp_sin.setValue(0.0)
        grid.addWidget(sp_sin, 4, 1)

        grid.addWidget(keep(pya.QLabel("Output Straight Lead (um):", dlg)), 5, 0)
        sp_sout = keep(pya.QDoubleSpinBox(dlg))
        sp_sout.setRange(0.0, 5000.0)
        sp_sout.setDecimals(2)
        sp_sout.setSingleStep(5.0)
        sp_sout.setValue(0.0)
        grid.addWidget(sp_sout, 5, 1)

        grid.addWidget(keep(pya.QLabel("Orientation (deg):", dlg)), 6, 0)
        cb_rot = keep(pya.QComboBox(dlg))
        for rlbl in ("0° (o1 West -> o2 North)", "90° (o1 South -> o2 West)", "180° (o1 East -> o2 South)", "270° (o1 North -> o2 East)"):
            cb_rot.addItem(rlbl)
        grid.addWidget(cb_rot, 6, 1)
        lay.addLayout(grid)

        chk_ports = keep(pya.QCheckBox("Stamp PinRec Ports (o1, o2 on 90/0) for instant Snap & Route", dlg))
        chk_ports.setChecked(True)
        lay.addWidget(chk_ports)

        chk_ann = keep(pya.QCheckBox("Annotate R_eff / R_c / Length on RouteLength (90/2)", dlg))
        chk_ann.setChecked(bool(settings.get("annotate_lengths", True)))
        lay.addWidget(chk_ann)

        lbl_metrics = keep(pya.QLabel("", dlg))
        lbl_metrics.setStyleSheet(
            "QLabel { background: #182338; color: #dbe9ff; padding: 6px; "
            "border: 1px solid #304970; border-radius: 3px; font-size: 11px; }"
        )
        lbl_metrics.setWordWrap(True)
        lay.addWidget(lbl_metrics)

        sync_guard = [False]

        def _refresh_metrics() -> None:
            ang_v = float(sp_ang.value)
            if abs(ang_v) < 0.1:
                lbl_metrics.setText("Enter a non-zero bend angle (|θ_t| >= 0.1°).")
                return
            try:
                m = topic_bend_metrics(ang_v, float(sp_r.value), p=float(sp_p.value), use_eff_radius=True)
                s_tot = float(m["L_total"]) + float(sp_sin.value) + float(sp_sout.value)
                lbl_metrics.setText(
                    f"R_c (R_min) = {m['R_min']:.3f} um  |  θ_p = {m['theta_p_deg']:.2f}° (central arc = {abs(ang_v) - 2.0 * m['theta_p_deg']:.2f}°)\n"
                    f"L_TOP (each) = {m['L_top']:.3f} um  |  L_CIRC = {m['L_circ']:.3f} um  |  L_bend = {m['L_total']:.3f} um\n"
                    f"Footprint (dx, dy) = ({m['dx']:.3f}, {m['dy']:.3f}) um  |  Total L = {s_tot:.3f} um"
                )
            except Exception as exc_m:
                lbl_metrics.setText(f"Invalid parameters: {exc_m}")

        def _on_p_or_ang_changed(*_a: Any) -> None:
            if sync_guard[0]:
                return
            sync_guard[0] = True
            try:
                half_ang = 0.5 * abs(float(sp_ang.value))
                sp_tp.setRange(0.0, max(0.05, half_ang))
                sp_tp.setValue(float(sp_p.value) * half_ang)
            finally:
                sync_guard[0] = False
            _refresh_metrics()

        def _on_tp_changed(*_a: Any) -> None:
            if sync_guard[0]:
                return
            sync_guard[0] = True
            try:
                half_ang = 0.5 * abs(float(sp_ang.value))
                if half_ang > 1e-6:
                    sp_p.setValue(max(0.0, min(1.0, float(sp_tp.value) / half_ang)))
            finally:
                sync_guard[0] = False
            _refresh_metrics()

        sp_p.valueChanged.connect(_on_p_or_ang_changed)
        sp_ang.valueChanged.connect(_on_p_or_ang_changed)
        sp_tp.valueChanged.connect(_on_tp_changed)
        sp_r.valueChanged.connect(lambda *_a: _refresh_metrics())
        sp_sin.valueChanged.connect(lambda *_a: _refresh_metrics())
        sp_sout.valueChanged.connect(lambda *_a: _refresh_metrics())
        _on_p_or_ang_changed()

        btn_row = keep(pya.QHBoxLayout())
        btn_row.addStretch()
        btn_ok = keep(pya.QPushButton("Create TOPIC Bend", dlg))
        btn_cancel = keep(pya.QPushButton("Cancel", dlg))
        btn_ok.clicked.connect(lambda *_a: dlg.accept())
        btn_cancel.clicked.connect(lambda *_a: dlg.reject())
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        lay.addLayout(btn_row)

        if _exec_dialog(dlg):
            rot_deg = float(int(cb_rot.currentIndex) * 90)
            execute_create_topic_bend(
                r_eff=float(sp_r.value),
                angle_deg=float(sp_ang.value),
                p=float(sp_p.value),
                straight_in=float(sp_sin.value),
                straight_out=float(sp_sout.value),
                use_eff_radius=True,
                add_ports=bool(chk_ports.isChecked()),
                annotate_length=bool(chk_ann.isChecked()),
                rotation_deg=rot_deg,
                cross_section=cs_init,
            )
    except Exception as exc:
        _set_status(f"Create TOPIC Bend dialog error: {exc}", is_error=True)
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Interactive Canvas Plugins (pya.Plugin + pya.PluginFactory)
# ---------------------------------------------------------------------------

class PICPortSnapPlugin(_PluginBase):
    """Click Port A (on movable cell) -> Click Port B (target) to precision-snap."""

    def __init__(self, view: Any) -> None:
        super().__init__()
        self._view = view
        self._world_ports: list[Port] = []
        self._src_port: Port | None = None
        self._hover_bag = _MarkerBag(view)
        self._sel_bag = _MarkerBag(view)

    def _reload_ports(self) -> None:
        cv = self._view.active_cellview()
        if cv is None or not cv.is_valid() or cv.cell is None:
            self._world_ports = []
            return
        self._world_ports = collect_world_ports(cv.layout(), cv.cell, pin_layer=DEFAULT_PIN_LAYER)

    def _reset(self) -> None:
        self._src_port = None
        self._hover_bag.clear()
        self._sel_bag.clear()

    def activated(self) -> None:
        self._reset()
        self._reload_ports()
        if not self._world_ports:
            _set_status(
                "Snap Mode: No PinRec ports found. Run Auto-Detect Ports (Ctrl+Shift+D) or Add Port (Ctrl+Shift+P).",
                is_error=True,
            )
        else:
            _set_status(
                f"Snap Mode ({len(self._world_ports)} ports): Click Source Port on movable cell (Esc to cancel)."
            )

    def deactivated(self) -> None:
        self._reset()

    def drag_cancel(self) -> None:
        self._reset()

    def mouse_moved_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            self._hover_bag.clear()
            pick_r, arrow_len = _dynamic_scale(self._view)
            if self._src_port is None:
                port, _ = _find_nearest_port(
                    self._world_ports, p.x, p.y, max_dist=pick_r, require_movable=True
                )
            else:
                port, _ = _find_nearest_port(
                    self._world_ports,
                    p.x,
                    p.y,
                    max_dist=pick_r,
                    exclude_full_name=self._src_port.full_name,
                    exclude_inst=self._src_port.inst_obj,
                    exclude_inst_id=self._src_port.inst_id,
                )
            if port is not None:
                color = _COLOR_HOVER if self._src_port is None else _COLOR_TARGET
                self._hover_bag.add_port(port, color, arrow_len=arrow_len, show_label=True)
        except Exception:
            traceback.print_exc()
        return False

    def mouse_click_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            if buttons & pya.ButtonState.RightButton:
                self._reset()
                _set_status("Snap Mode: selection cleared. Click Source Port.")
                return True
            if not (buttons & pya.ButtonState.LeftButton):
                return False

            pick_r, arrow_len = _dynamic_scale(self._view)
            cv = self._view.active_cellview()
            if cv is None or not cv.is_valid():
                return False
            layout = cv.layout()

            if self._src_port is None:
                self._reload_ports()
                port, _ = _find_nearest_port(
                    self._world_ports, p.x, p.y, max_dist=pick_r, require_movable=True
                )
                if port is None:
                    _set_status("Snap Mode: click closer to a movable cell's port.", is_error=True)
                    return True
                self._src_port = port
                self._sel_bag.clear()
                self._sel_bag.add_port(port, _COLOR_SOURCE, arrow_len=arrow_len, show_label=True)
                _set_status(
                    f"Snap Source: {port.full_name}. Now click Target Port (Right-click / Esc to cancel)."
                )
                return True

            tgt_port, _ = _find_nearest_port(
                self._world_ports,
                p.x,
                p.y,
                max_dist=pick_r,
                exclude_full_name=self._src_port.full_name,
                exclude_inst=self._src_port.inst_obj,
                exclude_inst_id=self._src_port.inst_id,
            )
            if tgt_port is None:
                _set_status("Snap Mode: click closer to a Target Port on another cell.", is_error=True)
                return True

            _execute_port_snap(self._view, layout, self._src_port, tgt_port, tx_title="PIC Port Snap")
            self._reset()
            self._reload_ports()
            return True
        except Exception as exc:
            _set_status(f"Snap Mode error: {exc}", is_error=True)
            traceback.print_exc()
            self._reset()
            return True

    def key_event(self, key: int, buttons: int) -> bool:
        if key == pya.KeyCode.Escape:
            if self._src_port is not None:
                self._reset()
                _set_status("Snap Mode: selection cleared.")
            else:
                self._view.switch_mode("select")
                _set_status("Returned to Select mode.")
            return True
        return False


class PICPortRoutePlugin(_PluginBase):
    """Click Port A -> optional waypoints (Shift / manual length typing) -> Click Port B."""

    def __init__(self, view: Any) -> None:
        super().__init__()
        self._view = view
        self._world_ports: list[Port] = []
        self._start_port: Port | None = None
        self._waypoints: list[tuple[float, float]] = []
        self._wp_meta: list[dict[str, Any]] = []
        self._typed_buf: str = ""
        self._last_mouse_pt: tuple[float, float] | None = None
        self._forced_dir: tuple[float, float] | None = None
        self._hover_bag = _MarkerBag(view)
        self._sel_bag = _MarkerBag(view)
        self._preview_bag = _MarkerBag(view)
        self._port_preview_cache: tuple[Any, Any] | None = None

    def _reload_ports(self) -> None:
        cv = self._view.active_cellview()
        if cv is None or not cv.is_valid() or cv.cell is None:
            self._world_ports = []
            return
        self._world_ports = collect_world_ports(cv.layout(), cv.cell, pin_layer=DEFAULT_PIN_LAYER)

    def _reset(self) -> None:
        self._start_port = None
        self._waypoints.clear()
        self._wp_meta.clear()
        self._typed_buf = ""
        self._forced_dir = None
        self._port_preview_cache = None
        self._hover_bag.clear()
        self._sel_bag.clear()
        self._preview_bag.clear()

    def _redraw_selected(self) -> None:
        self._sel_bag.clear()
        if self._start_port is None:
            return
        _, arrow_len = _dynamic_scale(self._view)
        self._sel_bag.add_port(self._start_port, _COLOR_SOURCE, arrow_len=arrow_len, show_label=True)
        if self._waypoints:
            pts = [(self._start_port.x, self._start_port.y)] + list(self._waypoints)
            self._sel_bag.add_polyline(pts, _COLOR_TARGET, width=0.0)

    def _resolve_manual_direction(self, dir_name: str = "Cursor") -> tuple[float, float]:
        """Resolve unit direction vector for manual straight entry."""
        if self._start_port is None:
            return (1.0, 0.0)
        th0 = math.radians(float(self._start_port.angle_deg))
        d0 = (math.cos(th0), math.sin(th0))

        dn = (dir_name or "Cursor").strip()
        if dn.startswith("+X"):
            return (1.0, 0.0)
        if dn.startswith("+Y"):
            return (0.0, 1.0)
        if dn.startswith("-X"):
            return (-1.0, 0.0)
        if dn.startswith("-Y"):
            return (0.0, -1.0)

        # Leg 1 always leaves along Start Port's outward tangent unless explicitly forced
        if not self._waypoints:
            return d0
        if self._forced_dir is not None:
            return self._forced_dir

        prev_pt = self._waypoints[-2] if len(self._waypoints) >= 2 else (self._start_port.x, self._start_port.y)
        anchor = self._waypoints[-1]
        pvx, pvy = anchor[0] - prev_pt[0], anchor[1] - prev_pt[1]
        pl = math.hypot(pvx, pvy)
        u_prev = (pvx / pl, pvy / pl) if pl > 1e-9 else d0
        n_prev = (-u_prev[1], u_prev[0])

        if self._last_mouse_pt is not None:
            dx = self._last_mouse_pt[0] - anchor[0]
            dy = self._last_mouse_pt[1] - anchor[1]
            proj_f = dx * u_prev[0] + dy * u_prev[1]
            proj_n = dx * n_prev[0] + dy * n_prev[1]
            if abs(proj_n) >= abs(proj_f) and abs(proj_n) > 1e-4:
                return n_prev if proj_n >= 0.0 else (-n_prev[0], -n_prev[1])
            if proj_f > 1e-4:
                return u_prev
        return u_prev

    def _dir_label(self, dvec: tuple[float, float]) -> str:
        ang = math.degrees(math.atan2(dvec[1], dvec[0])) % 360.0
        if abs(ang) < 1.0 or abs(ang - 360.0) < 1.0:
            return "+X"
        if abs(ang - 90.0) < 1.0:
            return "+Y"
        if abs(ang - 180.0) < 1.0:
            return "-X"
        if abs(ang - 270.0) < 1.0:
            return "-Y"
        return f"{ang:.1f}deg"

    def _start_taper_info(self, settings: dict[str, Any]) -> tuple[float | None, float]:
        if self._start_port is None:
            return (None, 0.0)
        cs: CrossSection = settings["cross_section"]
        return _compute_port_taper(self._start_port.width, cs.core_width, settings)

    def _split_live_taper(self, live: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        sw, lt1 = self._start_taper_info(settings)
        if sw is None or lt1 <= 1e-4 or not live.get("segments"):
            return live
        cs: CrossSection = settings["cross_section"]
        segs = split_taper_segments_list(
            live["segments"],
            core_width=cs.core_width,
            start_width=sw,
            end_width=None,
            taper_length=lt1,
        )
        out = dict(live)
        out["segments"] = segs
        out["summary"] = format_segments_summary(
            segs,
            live["total_length"],
            prefix=("Total" if live.get("feasible_geom", True) else "Raw Total"),
        )
        return out

    def _render_committed_preview(self, prefix_msg: str = "") -> None:
        """Render current committed manual route segments on canvas & status bar."""
        self._preview_bag.clear()
        if self._start_port is None or not self._waypoints:
            return
        settings = _get_route_settings()
        cs: CrossSection = settings["cross_section"]
        btype = str(settings.get("bend_type", "topic") or "topic")
        label_offset = max(4.0, 0.5 * _max_cs_width(cs) + 2.0)
        pts = [(self._start_port.x, self._start_port.y)] + list(self._waypoints)
        live = analyze_live_guide_segments(
            pts,
            radius=cs.radius,
            euler_p=cs.euler_p,
            min_straight=DEFAULT_SHIFT_STEP_UM,
            use_eff_radius=settings["use_eff_radius"],
            bend_type=btype,
        )
        live = self._split_live_taper(live, settings)
        line_w = cs.core_width if live["has_euler"] else 0.0
        self._preview_bag.add_polyline(live["centerline"], _COLOR_PREVIEW_OK, width=line_w)
        _draw_live_segment_labels(
            self._preview_bag,
            live["segments"],
            live["total_length"],
            label_offset=label_offset,
            color=_COLOR_LABEL_DARK,
            total_prefix=("Total" if live["feasible_geom"] else "Raw"),
        )
        if prefix_msg:
            _set_status(f"{prefix_msg} | {live['summary']}")
        else:
            _set_status(live["summary"])

    def _render_typed_preview(self) -> None:
        """Render real-time preview while the user is typing a manual straight length."""
        if self._start_port is None:
            return
        dvec = self._resolve_manual_direction("Cursor")
        dtag = self._dir_label(dvec)
        if not self._typed_buf:
            self._render_committed_preview(f"Dir={dtag} (type length + Enter, Shift-click, or click End Port)")
            return

        try:
            val = float(self._typed_buf)
        except ValueError:
            _set_status(f"[INPUT ({dtag}): {self._typed_buf} um — Enter to place, Backspace edit, Esc clear]")
            return

        if val < DEFAULT_SHIFT_STEP_UM:
            _set_status(
                f"[INPUT ({dtag}): {self._typed_buf} um — min {DEFAULT_SHIFT_STEP_UM:.0f} um; Enter to place, Esc clear]"
            )
            return

        cv = self._view.active_cellview()
        dbu = float(cv.layout().dbu) if (cv is not None and cv.is_valid()) else 0.001
        settings = _get_route_settings()
        cs: CrossSection = settings["cross_section"]
        btype = str(settings.get("bend_type", "topic") or "topic")
        _, lt1 = self._start_taper_info(settings)
        adv = advance_manual_straight_waypoints(
            (self._start_port.x, self._start_port.y),
            self._start_port.angle_deg,
            self._waypoints,
            self._wp_meta,
            dvec,
            val,
            radius=cs.radius,
            euler_p=cs.euler_p,
            use_eff_radius=settings["use_eff_radius"],
            dbu=dbu,
            min_len_um=DEFAULT_SHIFT_STEP_UM,
            start_taper_len=lt1,
            bend_type=btype,
        )
        self._preview_bag.clear()
        if not adv["ok"]:
            _set_status(f"[INPUT ({dtag}): {self._typed_buf} um] {adv['error']}", is_error=True)
            return

        pts = [(self._start_port.x, self._start_port.y)] + list(adv["waypoints"])
        live = analyze_live_guide_segments(
            pts,
            radius=cs.radius,
            euler_p=cs.euler_p,
            min_straight=DEFAULT_SHIFT_STEP_UM,
            use_eff_radius=settings["use_eff_radius"],
            bend_type=btype,
        )
        live = self._split_live_taper(live, settings)
        label_offset = max(4.0, 0.5 * _max_cs_width(cs) + 2.0)
        line_w = cs.core_width if live["has_euler"] else 0.0
        self._preview_bag.add_polyline(live["centerline"], _COLOR_HOVER, width=line_w)
        _draw_live_segment_labels(
            self._preview_bag,
            live["segments"],
            live["total_length"],
            label_offset=label_offset,
            color=_COLOR_LABEL_DARK,
            total_prefix=("Total" if live["feasible_geom"] else "Raw"),
        )
        tip = adv["added_point"]
        self._preview_bag.add_label(
            tip[0], tip[1] + label_offset, f"[INPUT {dtag}: {self._typed_buf} um -> Enter]", color=_COLOR_LABEL_DARK
        )
        _set_status(f"[INPUT {dtag}: {self._typed_buf} um -> Enter] {live['summary']}")

    def commit_manual_straight(self, length_um: float, dir_name: str = "Cursor") -> bool:
        """Commit an exact-length straight segment along ``dir_name`` (or cursor/arrow direction)."""
        if self._start_port is None:
            _set_status("Route Mode: click a Start Port first before entering a straight length.", is_error=True)
            return False
        cv = self._view.active_cellview()
        dbu = float(cv.layout().dbu) if (cv is not None and cv.is_valid()) else 0.001
        settings = _get_route_settings()
        cs: CrossSection = settings["cross_section"]
        btype = str(settings.get("bend_type", "topic") or "topic")
        _, lt1 = self._start_taper_info(settings)
        dvec = self._resolve_manual_direction(dir_name)
        adv = advance_manual_straight_waypoints(
            (self._start_port.x, self._start_port.y),
            self._start_port.angle_deg,
            self._waypoints,
            self._wp_meta,
            dvec,
            float(length_um),
            radius=cs.radius,
            euler_p=cs.euler_p,
            use_eff_radius=settings["use_eff_radius"],
            dbu=dbu,
            min_len_um=DEFAULT_SHIFT_STEP_UM,
            start_taper_len=lt1,
            bend_type=btype,
        )
        if not adv["ok"]:
            _set_status(adv["error"], is_error=True)
            return False

        self._waypoints = adv["waypoints"]
        self._wp_meta = adv["wp_meta"]
        self._typed_buf = ""
        self._forced_dir = None
        self._redraw_selected()
        dtag = self._dir_label(dvec)
        self._render_committed_preview(f"Added {dtag} straight {float(length_um):.3f} um")
        return True

    def activated(self) -> None:
        self._reset()
        self._reload_ports()
        _GUI_STATE["active_route_plugin"] = self
        if not self._world_ports:
            _set_status(
                "Route Mode: No PinRec ports found. Run Auto-Detect Ports (Ctrl+Shift+D) or Add Port (Ctrl+Shift+P).",
                is_error=True,
            )
        else:
            _set_status(
                f"Route Mode ({len(self._world_ports)} ports): Click Start Port "
                "(Shift=1um H/V lock, type digits+Enter or Tab for exact straight length, Esc cancel)."
            )

    def deactivated(self) -> None:
        if _GUI_STATE.get("active_route_plugin") is self:
            _GUI_STATE["active_route_plugin"] = None
        self._reset()

    def drag_cancel(self) -> None:
        self._reset()

    def mouse_moved_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            self._last_mouse_pt = (float(p.x), float(p.y))
            self._hover_bag.clear()
            self._preview_bag.clear()
            pick_r, arrow_len = _dynamic_scale(self._view)
            exclude = self._start_port.full_name if self._start_port is not None else None
            port, _ = _find_nearest_port(
                self._world_ports, p.x, p.y, max_dist=pick_r, exclude_full_name=exclude
            )
            if port is not None:
                color = _COLOR_HOVER if self._start_port is None else _COLOR_TARGET
                self._hover_bag.add_port(port, color, arrow_len=arrow_len, show_label=True)

            if self._start_port is not None:
                if self._typed_buf and port is None:
                    self._render_typed_preview()
                    return False

                settings = _get_route_settings()
                cs: CrossSection = settings["cross_section"]
                btype = str(settings.get("bend_type", "topic") or "topic")
                label_offset = max(4.0, 0.5 * _max_cs_width(cs) + 2.0)
                sw, lt1 = self._start_taper_info(settings)
                if port is not None:
                    ew, lt2 = _compute_port_taper(port.width, cs.core_width, settings)
                    taper_len = max(0.0, float(settings.get("taper_length", 10.0)))
                    cache_key = (
                        self._start_port.full_name,
                        round(self._start_port.x, 4),
                        round(self._start_port.y, 4),
                        port.full_name,
                        round(port.x, 4),
                        round(port.y, 4),
                        tuple(self._waypoints),
                        round(cs.radius, 4),
                        round(cs.euler_p, 4),
                        round(cs.min_straight, 4),
                        round(cs.core_width, 4),
                        str(settings["route_mode"]),
                        btype,
                        bool(settings.get("integer_length", True)),
                        bool(settings.get("use_eff_radius", True)),
                        round(lt1, 4),
                        round(lt2, 4),
                    )
                    if self._port_preview_cache is not None and self._port_preview_cache[0] == cache_key:
                        res, res_disp = self._port_preview_cache[1]
                    else:
                        res = plan_route_ports(
                            self._start_port,
                            port,
                            radius=cs.radius,
                            euler_p=cs.euler_p,
                            min_straight=cs.min_straight,
                            route_mode=settings["route_mode"],
                            waypoints=list(self._waypoints) if self._waypoints else None,
                            use_eff_radius=settings["use_eff_radius"],
                            start_taper_len=lt1,
                            end_taper_len=lt2,
                            bend_type=btype,
                        )
                        if not res.ok and self._waypoints and cs.min_straight > DEFAULT_SHIFT_STEP_UM:
                            res_rel = plan_route_ports(
                                self._start_port,
                                port,
                                radius=cs.radius,
                                euler_p=cs.euler_p,
                                min_straight=DEFAULT_SHIFT_STEP_UM,
                                route_mode=settings["route_mode"],
                                waypoints=list(self._waypoints),
                                use_eff_radius=settings["use_eff_radius"],
                                start_taper_len=lt1,
                                end_taper_len=lt2,
                                bend_type=btype,
                            )
                            if res_rel.ok:
                                res = res_rel
                        if res.ok and len(res.centerline) >= 2:
                            if settings.get("integer_length", True) and not self._waypoints and res.bends:
                                tuned_res, _, was_tuned = tune_route_for_integer_length(
                                    res,
                                    radius=cs.radius,
                                    euler_p=cs.euler_p,
                                    min_straight=cs.min_straight,
                                    use_eff_radius=settings["use_eff_radius"],
                                    step_um=1.0,
                                    dbu=0.001,
                                    min_stub_start=lt1,
                                    min_stub_end=lt2,
                                    bend_type=btype,
                                )
                                if was_tuned:
                                    res = tuned_res
                            res_disp = apply_taper_segments(
                                res,
                                core_width=cs.core_width,
                                start_width=sw,
                                end_width=ew,
                                taper_length=taper_len,
                            )
                        else:
                            res_disp = res
                        self._port_preview_cache = (cache_key, (res, res_disp))

                    if res.ok and len(res.centerline) >= 2:
                        prev_col = _COLOR_PREVIEW_ERR if res.warnings else _COLOR_PREVIEW_OK
                        self._preview_bag.add_polyline(
                            res.centerline, prev_col, width=cs.core_width
                        )
                        _draw_live_segment_labels(
                            self._preview_bag,
                            res_disp.segments,
                            res_disp.total_length,
                            label_offset=label_offset,
                            color=_COLOR_LABEL_DARK,
                            total_prefix="Total",
                        )
                        warn_txt = f" | WARN: {'; '.join(res.warnings)}" if res.warnings else ""
                        summary = format_segments_summary(res_disp.segments, res_disp.total_length, prefix="Preview")
                        _set_status(
                            f"{summary} ({self._start_port.full_name} -> {port.full_name}){warn_txt}",
                            is_error=bool(res.warnings),
                        )
                    else:
                        fallback = res.guide_points if len(res.guide_points) >= 2 else [
                            (self._start_port.x, self._start_port.y),
                            (port.x, port.y),
                        ]
                        self._preview_bag.add_polyline(fallback, _COLOR_PREVIEW_ERR, width=0.0)
                        _set_status(f"Preview infeasible: {res.error}", is_error=True)
                else:
                    cv = self._view.active_cellview()
                    dbu = float(cv.layout().dbu) if (cv is not None and cv.is_valid()) else 0.001
                    drag = compute_manual_drag_waypoints(
                        (self._start_port.x, self._start_port.y),
                        self._start_port.angle_deg,
                        self._waypoints,
                        self._wp_meta,
                        (float(p.x), float(p.y)),
                        radius=cs.radius,
                        euler_p=cs.euler_p,
                        use_eff_radius=settings["use_eff_radius"],
                        step_um=DEFAULT_SHIFT_STEP_UM,
                        dbu=dbu,
                        start_taper_len=lt1,
                        bend_type=btype,
                    )
                    if drag["ok"]:
                        pts = [(self._start_port.x, self._start_port.y)] + list(drag["waypoints"])
                        live = analyze_live_guide_segments(
                            pts,
                            radius=cs.radius,
                            euler_p=cs.euler_p,
                            min_straight=DEFAULT_SHIFT_STEP_UM,
                            use_eff_radius=settings["use_eff_radius"],
                            bend_type=btype,
                        )
                        live = self._split_live_taper(live, settings)
                        line_col = _COLOR_HOVER if drag.get("action") in ("straight", "turn90", "extend") else _COLOR_PREVIEW_OK
                        line_w = cs.core_width if live["has_euler"] else 0.0
                        self._preview_bag.add_polyline(live["centerline"], line_col, width=line_w)
                        _draw_live_segment_labels(
                            self._preview_bag,
                            live["segments"],
                            live["total_length"],
                            label_offset=label_offset,
                            color=_COLOR_LABEL_DARK,
                            total_prefix="Total",
                        )
                        _set_status(f"[PURE-STRAIGHT 1um] {live['summary']}")
                    elif drag.get("pending_taper"):
                        gtip = drag["guide_tip"]
                        self._preview_bag.add_polyline(
                            [(self._start_port.x, self._start_port.y), gtip], _COLOR_PREVIEW_ERR, width=0.0
                        )
                        self._preview_bag.add_label(
                            gtip[0],
                            gtip[1] + label_offset,
                            f"Taper > {drag['needed_taper']:.1f} um ({drag['dist_proj']:.1f}/{drag['needed_taper']:.1f})",
                            color=_COLOR_PREVIEW_ERR,
                        )
                        _set_status(drag["error"])
                    elif drag.get("pending_bend"):
                        # Keep committed route intact (S1 untouched!) and show bend clearance needed
                        if self._waypoints:
                            pts_cur = [(self._start_port.x, self._start_port.y)] + list(self._waypoints)
                            live_cur = analyze_live_guide_segments(
                                pts_cur,
                                radius=cs.radius,
                                euler_p=cs.euler_p,
                                min_straight=DEFAULT_SHIFT_STEP_UM,
                                use_eff_radius=settings["use_eff_radius"],
                                bend_type=btype,
                            )
                            live_cur = self._split_live_taper(live_cur, settings)
                            self._preview_bag.add_polyline(
                                live_cur["centerline"],
                                _COLOR_PREVIEW_OK,
                                width=(cs.core_width if live_cur["has_euler"] else 0.0),
                            )
                            _draw_live_segment_labels(
                                self._preview_bag,
                                live_cur["segments"],
                                live_cur["total_length"],
                                label_offset=label_offset,
                                color=_COLOR_LABEL_DARK,
                                total_prefix="Total",
                            )
                            tip = self._waypoints[-1]
                            gtip = drag["guide_tip"]
                            self._preview_bag.add_polyline([tip, gtip], _COLOR_PREVIEW_ERR, width=0.0)
                            self._preview_bag.add_label(
                                gtip[0],
                                gtip[1] + label_offset,
                                f"90° bend > {drag['needed_t90']:.1f} um ({drag['dist_n']:.1f}/{drag['needed_t90']:.1f})",
                                color=_COLOR_PREVIEW_ERR,
                            )
                            _set_status(f"{live_cur['summary']} | {drag['error']}")
                        else:
                            _set_status(drag["error"])
                    else:
                        if self._waypoints:
                            self._render_committed_preview(drag.get("error", ""))
                        else:
                            self._preview_bag.add_polyline(
                                [(self._start_port.x, self._start_port.y), (float(p.x), float(p.y))],
                                _COLOR_PREVIEW_ERR,
                                width=0.0,
                            )
                            _set_status(drag.get("error", "Drag forward along start port angle."), is_error=True)
        except Exception:
            traceback.print_exc()
        return False

    def mouse_click_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            self._last_mouse_pt = (float(p.x), float(p.y))
            if buttons & pya.ButtonState.RightButton:
                if self._typed_buf:
                    self._typed_buf = ""
                    self._forced_dir = None
                    self._render_committed_preview("Cleared typed length")
                    return True
                if self._waypoints:
                    undo_last_waypoint(self._waypoints, self._wp_meta)
                    self._redraw_selected()
                    if self._waypoints:
                        self._render_committed_preview(f"Undid last step ({len(self._waypoints)} left)")
                    else:
                        self._preview_bag.clear()
                        _set_status("Route Mode: removed last waypoint (0 left).")
                else:
                    self._reset()
                    _set_status("Route Mode: cleared. Click Start Port.")
                return True

            if not (buttons & pya.ButtonState.LeftButton):
                return False

            cv = self._view.active_cellview()
            if cv is None or not cv.is_valid() or cv.cell is None:
                return False
            layout = cv.layout()
            top_cell = cv.cell
            pick_r, _ = _dynamic_scale(self._view)

            if self._start_port is None:
                self._reload_ports()
                port, _ = _find_nearest_port(self._world_ports, p.x, p.y, max_dist=pick_r)
                if port is None:
                    _set_status("Route Mode: click closer to a Start Port.", is_error=True)
                    return True
                self._start_port = port
                self._waypoints.clear()
                self._wp_meta.clear()
                self._typed_buf = ""
                self._forced_dir = None
                self._redraw_selected()
                _set_status(
                    f"Route Start: {port.full_name}. Click End Port, Shift-click waypoints, "
                    "or type straight length + Enter (Tab dialog, Arrows dir, Right-click undo, Esc cancel)."
                )
                return True

            end_port, _ = _find_nearest_port(
                self._world_ports,
                p.x,
                p.y,
                max_dist=pick_r,
                exclude_full_name=self._start_port.full_name,
            )
            if end_port is not None:
                ok = _execute_port_route(
                    self._view,
                    layout,
                    top_cell,
                    self._start_port,
                    end_port,
                    waypoints=self._waypoints,
                    tx_title="PIC Euler Route",
                )
                if ok:
                    self._reset()
                    self._reload_ports()
                return True

            settings = _get_route_settings()
            cs: CrossSection = settings["cross_section"]
            btype = str(settings.get("bend_type", "topic") or "topic")
            _, lt1 = self._start_taper_info(settings)
            drag = compute_manual_drag_waypoints(
                (self._start_port.x, self._start_port.y),
                self._start_port.angle_deg,
                self._waypoints,
                self._wp_meta,
                (float(p.x), float(p.y)),
                radius=cs.radius,
                euler_p=cs.euler_p,
                use_eff_radius=settings["use_eff_radius"],
                step_um=DEFAULT_SHIFT_STEP_UM,
                dbu=float(layout.dbu),
                start_taper_len=lt1,
                bend_type=btype,
            )
            if not drag["ok"]:
                _set_status(drag.get("error", "Cannot place straight segment here."), is_error=True)
                return True

            self._waypoints = drag["waypoints"]
            self._wp_meta = drag["wp_meta"]
            self._typed_buf = ""
            self._forced_dir = None
            self._redraw_selected()
            self._render_committed_preview(
                f"Placed straight {float(drag['straight_len']):.1f} um (#{len(self._waypoints)})"
            )
            return True
        except Exception as exc:
            _set_status(f"Route Mode error: {exc}", is_error=True)
            traceback.print_exc()
            self._reset()
            return True

    def key_event(self, key: int, buttons: int) -> bool:
        try:
            ctrl_mask = int(getattr(pya.ButtonState, "ControlKey", 2)) if pya is not None else 2
            alt_mask = int(getattr(pya.ButtonState, "AltKey", 4)) if pya is not None else 4
            if buttons & (ctrl_mask | alt_mask):
                return False

            if key == pya.KeyCode.Escape:
                if self._typed_buf:
                    self._typed_buf = ""
                    self._forced_dir = None
                    self._render_committed_preview("Cleared typed length")
                    return True
                if self._start_port is not None:
                    self._reset()
                    _set_status("Route Mode: cancelled current route.")
                else:
                    self._view.switch_mode("select")
                    _set_status("Returned to Select mode.")
                return True

            if key == pya.KeyCode.Backspace:
                if self._typed_buf:
                    self._typed_buf = self._typed_buf[:-1]
                    self._render_typed_preview()
                    return True
                if self._waypoints:
                    undo_last_waypoint(self._waypoints, self._wp_meta)
                    self._redraw_selected()
                    if self._waypoints:
                        self._render_committed_preview(f"Removed last waypoint ({len(self._waypoints)} remaining)")
                    else:
                        self._preview_bag.clear()
                        _set_status("Removed last waypoint (0 remaining).")
                    return True
                return False

            if self._start_port is not None:
                # Direct numeric input: '0'..'9' and '.'
                if 48 <= int(key) <= 57:
                    if len(self._typed_buf) < 10:
                        self._typed_buf += chr(int(key))
                    self._render_typed_preview()
                    return True
                if int(key) == 46 and "." not in self._typed_buf:
                    self._typed_buf = (self._typed_buf or "0") + "."
                    self._render_typed_preview()
                    return True

                # Arrow keys: lock cardinal direction for typed straight length
                arrow_dirs = {
                    int(pya.KeyCode.Right): (1.0, 0.0),
                    int(pya.KeyCode.Up): (0.0, 1.0),
                    int(pya.KeyCode.Left): (-1.0, 0.0),
                    int(pya.KeyCode.Down): (0.0, -1.0),
                }
                if int(key) in arrow_dirs:
                    self._forced_dir = arrow_dirs[int(key)]
                    self._render_typed_preview()
                    return True

                # Tab: prompt dialog for exact straight waveguide length
                if key == pya.KeyCode.Tab:
                    dvec = self._resolve_manual_direction("Cursor")
                    dtag = self._dir_label(dvec)
                    default_v = float(self._typed_buf) if self._typed_buf else 50.0
                    val = pya.InputDialog.ask_double(
                        "Manual Straight Length",
                        f"Straight waveguide length along {dtag} (um, >= {DEFAULT_SHIFT_STEP_UM:.0f}):",
                        max(DEFAULT_SHIFT_STEP_UM, default_v),
                        DEFAULT_SHIFT_STEP_UM,
                        100000.0,
                        3,
                    )
                    if val is not None:
                        self.commit_manual_straight(float(val), dir_name="Cursor")
                    return True

                # Space commits typed buffer when non-empty
                if int(key) == 32 and self._typed_buf:
                    try:
                        val = float(self._typed_buf)
                    except ValueError:
                        _set_status(f"Invalid straight length: {self._typed_buf!r}", is_error=True)
                        return True
                    self.commit_manual_straight(val, dir_name="Cursor")
                    return True

            if key in (pya.KeyCode.Return, pya.KeyCode.Enter) and self._start_port is not None:
                if self._typed_buf:
                    try:
                        val = float(self._typed_buf)
                    except ValueError:
                        _set_status(f"Invalid straight length: {self._typed_buf!r}", is_error=True)
                        return True
                    self.commit_manual_straight(val, dir_name="Cursor")
                    return True

                if self._waypoints:
                    cv = self._view.active_cellview()
                    if cv is not None and cv.is_valid() and cv.cell is not None:
                        settings = _get_route_settings()
                        cs: CrossSection = settings["cross_section"]
                        btype = str(settings.get("bend_type", "topic") or "topic")
                        pts = [(self._start_port.x, self._start_port.y)] + list(self._waypoints)
                        cs_use = cs
                        res_open = route_from_guide_points(
                            pts,
                            radius=cs.radius,
                            euler_p=cs.euler_p,
                            min_straight=cs.min_straight,
                            use_eff_radius=settings["use_eff_radius"],
                            min_straight_ends=0.0,
                            bend_type=btype,
                        )
                        if not res_open.ok and cs.min_straight > DEFAULT_SHIFT_STEP_UM:
                            res_rel = route_from_guide_points(
                                pts,
                                radius=cs.radius,
                                euler_p=cs.euler_p,
                                min_straight=DEFAULT_SHIFT_STEP_UM,
                                use_eff_radius=settings["use_eff_radius"],
                                min_straight_ends=0.0,
                                bend_type=btype,
                            )
                            if res_rel.ok:
                                res_open = res_rel
                                cs_use = cs.with_overrides(min_straight=DEFAULT_SHIFT_STEP_UM)
                        # Keep exact R_eff and pure straight lengths S1/S2/... untouched on manual routes!
                        sw, lt1 = _compute_port_taper(self._start_port.width, cs_use.core_width, settings)
                        taper_len = max(0.0, float(settings.get("taper_length", 10.0)))
                        res_open_ann = (
                            apply_taper_segments(
                                res_open,
                                core_width=cs_use.core_width,
                                start_width=sw,
                                end_width=None,
                                taper_length=taper_len,
                            )
                            if res_open.ok
                            else res_open
                        )
                        with _view_transaction(self._view, "PIC Open Waveguide"):
                            instantiate_euler_waveguide(
                                cv.layout(),
                                cv.cell,
                                res_open.guide_points if res_open.ok else pts,
                                cs_use,
                                start_width=sw,
                                taper_length=taper_len,
                                use_eff_radius=settings["use_eff_radius"],
                                min_straight=cs_use.min_straight,
                                bend_type=btype,
                            )
                            if res_open_ann.ok and settings.get("annotate_lengths", True):
                                label_offset = max(4.0, 0.5 * _max_cs_width(cs_use) + 2.0)
                                existing_r = _collect_euler_waveguide_routes(cv.layout(), cv.cell)
                                obs_open: list[tuple[float, float]] = []
                                for rec in existing_r:
                                    obs_open.extend(_sample_polyline_points(rec["result"].centerline, step_um=20.0))
                                draw_length_annotations_to_cell(
                                    cv.layout(),
                                    cv.cell,
                                    res_open_ann,
                                    length_layer=DEFAULT_LENGTH_LAYER,
                                    label_offset=label_offset,
                                    label_style=str(settings.get("label_style", "card")),
                                    route_index=max(1, len(existing_r)),
                                    obstacle_points=obs_open,
                                )
                        _refresh_view_layers(self._view)
                        _refresh_routes_table()
                        if res_open_ann.ok:
                            summary = format_segments_summary(
                                res_open_ann.segments, res_open_ann.total_length, prefix="Total"
                            )
                            _set_status(f"Created open EulerWaveguide: {summary}")
                        else:
                            _set_status(f"Created open EulerWaveguide from {self._start_port.full_name} ({len(pts)} vertices).")
                        self._reset()
                    return True
        except Exception as exc:
            _set_status(f"Route key handler error: {exc}", is_error=True)
            traceback.print_exc()
        return False


class PICFreeDrawWaveguidePlugin(_PluginBase):
    """Draw multi-layer TOPIC/Euler waveguides freely on blank canvas (zero ports required).

    Workflow:
      1. Click anywhere on canvas to place Start Point (quantized to 1 um; snaps if near a port).
      2. Drag along ±X/±Y (or port angle) + click, OR Arrow key + type digits + Enter/Tab/Dock Add Straight.
      3. Subsequent legs automatically append 90° TOPIC/Euler bends while preserving pure straight lengths.
      4. Right-click or Backspace losslessly undoes the last segment (or clears Start Point).
      5. Press Enter (or click an existing End Port) to commit the multi-layer EulerWaveguide PCell
         (with flush o1/o2 PinRec ports when drawn freely without ports).
    """

    def __init__(self, view: Any) -> None:
        super().__init__()
        self._view = view
        self._world_ports: list[Port] = []
        self._start_pt: tuple[float, float] | None = None
        self._start_port: Port | None = None
        self._start_angle_deg: float | None = None
        self._waypoints: list[tuple[float, float]] = []
        self._wp_meta: list[dict[str, Any]] = []
        self._typed_buf: str = ""
        self._last_mouse_pt: tuple[float, float] | None = None
        self._forced_dir: tuple[float, float] | None = None
        self._hover_bag = _MarkerBag(view)
        self._sel_bag = _MarkerBag(view)
        self._preview_bag = _MarkerBag(view)

    def _reload_ports(self) -> None:
        cv = self._view.active_cellview()
        if cv is None or not cv.is_valid() or cv.cell is None:
            self._world_ports = []
            return
        self._world_ports = collect_world_ports(cv.layout(), cv.cell, pin_layer=DEFAULT_PIN_LAYER)

    def _reset(self) -> None:
        self._start_pt = None
        self._start_port = None
        self._start_angle_deg = None
        self._waypoints.clear()
        self._wp_meta.clear()
        self._typed_buf = ""
        self._forced_dir = None
        self._hover_bag.clear()
        self._sel_bag.clear()
        self._preview_bag.clear()

    def _redraw_selected(self) -> None:
        self._sel_bag.clear()
        if self._start_pt is None:
            return
        _, arrow_len = _dynamic_scale(self._view)
        if self._start_port is not None:
            self._sel_bag.add_port(self._start_port, _COLOR_SOURCE, arrow_len=arrow_len, show_label=True)
        else:
            self._sel_bag.add_point_marker(
                self._start_pt[0],
                self._start_pt[1],
                _COLOR_SOURCE,
                size=arrow_len,
                label=f"START ({self._start_pt[0]:.0f}, {self._start_pt[1]:.0f})",
            )
        if self._waypoints:
            pts = [self._start_pt] + list(self._waypoints)
            self._sel_bag.add_polyline(pts, _COLOR_TARGET, width=0.0)

    @staticmethod
    def _cardinal_dir_and_angle(dx: float, dy: float) -> tuple[tuple[float, float], float]:
        if abs(dx) >= abs(dy):
            return ((1.0, 0.0), 0.0) if dx >= 0.0 else ((-1.0, 0.0), 180.0)
        return ((0.0, 1.0), 90.0) if dy >= 0.0 else ((0.0, -1.0), 270.0)

    def _resolve_leg1_dir_and_angle(
        self,
        dir_name: str = "Cursor",
        cursor_pt: tuple[float, float] | None = None,
        ignore_forced: bool = False,
    ) -> tuple[tuple[float, float], float]:
        if self._start_port is not None:
            ang = float(self._start_port.angle_deg) % 360.0
            rad = math.radians(ang)
            return ((math.cos(rad), math.sin(rad)), ang)

        dn = (dir_name or "Cursor").strip()
        if dn.startswith("+X"):
            return ((1.0, 0.0), 0.0)
        if dn.startswith("+Y"):
            return ((0.0, 1.0), 90.0)
        if dn.startswith("-X"):
            return ((-1.0, 0.0), 180.0)
        if dn.startswith("-Y"):
            return ((0.0, -1.0), 270.0)

        if not ignore_forced and self._forced_dir is not None:
            fx, fy = self._forced_dir
            return (self._forced_dir, math.degrees(math.atan2(fy, fx)) % 360.0)

        pt = cursor_pt if cursor_pt is not None else self._last_mouse_pt
        if self._start_pt is not None and pt is not None:
            dx = float(pt[0]) - float(self._start_pt[0])
            dy = float(pt[1]) - float(self._start_pt[1])
            if math.hypot(dx, dy) > 1e-4:
                return self._cardinal_dir_and_angle(dx, dy)

        return ((1.0, 0.0), 0.0)

    def _resolve_manual_direction(self, dir_name: str = "Cursor") -> tuple[float, float]:
        """Resolve unit direction vector for manual straight entry."""
        if self._start_pt is None:
            return (1.0, 0.0)
        if not self._waypoints:
            return self._resolve_leg1_dir_and_angle(dir_name)[0]

        dn = (dir_name or "Cursor").strip()
        if dn.startswith("+X"):
            return (1.0, 0.0)
        if dn.startswith("+Y"):
            return (0.0, 1.0)
        if dn.startswith("-X"):
            return (-1.0, 0.0)
        if dn.startswith("-Y"):
            return (0.0, -1.0)
        if self._forced_dir is not None:
            return self._forced_dir

        th0 = math.radians(float(self._start_angle_deg or 0.0))
        d0 = (math.cos(th0), math.sin(th0))
        prev_pt = self._waypoints[-2] if len(self._waypoints) >= 2 else self._start_pt
        anchor = self._waypoints[-1]
        pvx, pvy = anchor[0] - prev_pt[0], anchor[1] - prev_pt[1]
        pl = math.hypot(pvx, pvy)
        u_prev = (pvx / pl, pvy / pl) if pl > 1e-9 else d0
        n_prev = (-u_prev[1], u_prev[0])

        if self._last_mouse_pt is not None:
            dx = self._last_mouse_pt[0] - anchor[0]
            dy = self._last_mouse_pt[1] - anchor[1]
            proj_f = dx * u_prev[0] + dy * u_prev[1]
            proj_n = dx * n_prev[0] + dy * n_prev[1]
            if abs(proj_n) >= abs(proj_f) and abs(proj_n) > 1e-4:
                return n_prev if proj_n >= 0.0 else (-n_prev[0], -n_prev[1])
            if proj_f > 1e-4:
                return u_prev
        return u_prev

    def _dir_label(self, dvec: tuple[float, float]) -> str:
        ang = math.degrees(math.atan2(dvec[1], dvec[0])) % 360.0
        if abs(ang) < 1.0 or abs(ang - 360.0) < 1.0:
            return "+X"
        if abs(ang - 90.0) < 1.0:
            return "+Y"
        if abs(ang - 180.0) < 1.0:
            return "-X"
        if abs(ang - 270.0) < 1.0:
            return "-Y"
        return f"{ang:.1f}deg"

    def _start_taper_info(self, settings: dict[str, Any]) -> tuple[float | None, float]:
        if self._start_port is None:
            return (None, 0.0)
        cs: CrossSection = settings["cross_section"]
        return _compute_port_taper(self._start_port.width, cs.core_width, settings)

    def _split_live_taper(self, live: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        sw, lt1 = self._start_taper_info(settings)
        if sw is None or lt1 <= 1e-4 or not live.get("segments"):
            return live
        cs: CrossSection = settings["cross_section"]
        segs = split_taper_segments_list(
            live["segments"],
            core_width=cs.core_width,
            start_width=sw,
            end_width=None,
            taper_length=lt1,
        )
        out = dict(live)
        out["segments"] = segs
        out["summary"] = format_segments_summary(
            segs,
            live["total_length"],
            prefix=("Total" if live.get("feasible_geom", True) else "Raw Total"),
        )
        return out

    def _virtual_start_port(self, end_port: Port, cs: CrossSection) -> Port:
        if self._start_port is not None:
            return self._start_port
        assert self._start_pt is not None
        if self._start_angle_deg is not None:
            ang = float(self._start_angle_deg)
        else:
            ang = (float(end_port.angle_deg) + 180.0) % 360.0
        return Port(
            name="start",
            x=float(self._start_pt[0]),
            y=float(self._start_pt[1]),
            angle_deg=ang,
            width=float(cs.core_width),
            core_layer=cs.core_layer,
        )

    def _render_committed_preview(self, prefix_msg: str = "") -> None:
        self._preview_bag.clear()
        if self._start_pt is None or not self._waypoints:
            return
        settings = _get_route_settings()
        cs: CrossSection = settings["cross_section"]
        btype = str(settings.get("bend_type", "topic") or "topic")
        label_offset = max(4.0, 0.5 * _max_cs_width(cs) + 2.0)
        pts = [self._start_pt] + list(self._waypoints)
        live = analyze_live_guide_segments(
            pts,
            radius=cs.radius,
            euler_p=cs.euler_p,
            min_straight=DEFAULT_SHIFT_STEP_UM,
            use_eff_radius=settings["use_eff_radius"],
            bend_type=btype,
        )
        live = self._split_live_taper(live, settings)
        line_w = cs.core_width if live["has_euler"] else 0.0
        self._preview_bag.add_polyline(live["centerline"], _COLOR_PREVIEW_OK, width=line_w)
        _draw_live_segment_labels(
            self._preview_bag,
            live["segments"],
            live["total_length"],
            label_offset=label_offset,
            color=_COLOR_LABEL_DARK,
            total_prefix=("Total" if live["feasible_geom"] else "Raw"),
        )
        if prefix_msg:
            _set_status(f"{prefix_msg} | {live['summary']} (Enter to finish)")
        else:
            _set_status(f"{live['summary']} (Enter to finish)")

    def _render_typed_preview(self) -> None:
        if self._start_pt is None:
            return
        if not self._waypoints:
            dvec, eff_ang = self._resolve_leg1_dir_and_angle("Cursor")
        else:
            dvec = self._resolve_manual_direction("Cursor")
            eff_ang = float(self._start_angle_deg or 0.0)
        dtag = self._dir_label(dvec)
        if not self._typed_buf:
            if self._waypoints:
                self._render_committed_preview(f"Dir={dtag} (type length + Enter, click, or Enter to finish)")
            else:
                self._preview_bag.clear()
                _set_status(f"Free Draw Dir={dtag}: drag + click or type straight length + Enter (Arrows set dir)")
            return

        try:
            val = float(self._typed_buf)
        except ValueError:
            _set_status(f"[INPUT ({dtag}): {self._typed_buf} um — Enter to place, Backspace edit, Esc clear]")
            return

        if val < DEFAULT_SHIFT_STEP_UM:
            _set_status(
                f"[INPUT ({dtag}): {self._typed_buf} um — min {DEFAULT_SHIFT_STEP_UM:.0f} um; Enter to place, Esc clear]"
            )
            return

        cv = self._view.active_cellview()
        dbu = float(cv.layout().dbu) if (cv is not None and cv.is_valid()) else 0.001
        settings = _get_route_settings()
        cs: CrossSection = settings["cross_section"]
        btype = str(settings.get("bend_type", "topic") or "topic")
        _, lt1 = self._start_taper_info(settings)
        adv = advance_manual_straight_waypoints(
            self._start_pt,
            eff_ang,
            self._waypoints,
            self._wp_meta,
            dvec,
            val,
            radius=cs.radius,
            euler_p=cs.euler_p,
            use_eff_radius=settings["use_eff_radius"],
            dbu=dbu,
            min_len_um=DEFAULT_SHIFT_STEP_UM,
            start_taper_len=lt1,
            bend_type=btype,
        )
        self._preview_bag.clear()
        if not adv["ok"]:
            _set_status(f"[INPUT ({dtag}): {self._typed_buf} um] {adv['error']}", is_error=True)
            return

        pts = [self._start_pt] + list(adv["waypoints"])
        live = analyze_live_guide_segments(
            pts,
            radius=cs.radius,
            euler_p=cs.euler_p,
            min_straight=DEFAULT_SHIFT_STEP_UM,
            use_eff_radius=settings["use_eff_radius"],
            bend_type=btype,
        )
        live = self._split_live_taper(live, settings)
        label_offset = max(4.0, 0.5 * _max_cs_width(cs) + 2.0)
        line_w = cs.core_width if live["has_euler"] else 0.0
        self._preview_bag.add_polyline(live["centerline"], _COLOR_HOVER, width=line_w)
        _draw_live_segment_labels(
            self._preview_bag,
            live["segments"],
            live["total_length"],
            label_offset=label_offset,
            color=_COLOR_LABEL_DARK,
            total_prefix=("Total" if live["feasible_geom"] else "Raw"),
        )
        tip = adv["added_point"]
        self._preview_bag.add_label(
            tip[0], tip[1] + label_offset, f"[INPUT {dtag}: {self._typed_buf} um -> Enter]", color=_COLOR_LABEL_DARK
        )
        _set_status(f"[INPUT {dtag}: {self._typed_buf} um -> Enter] {live['summary']}")

    def commit_manual_straight(self, length_um: float, dir_name: str = "Cursor") -> bool:
        """Commit an exact-length straight segment along ``dir_name`` (or cursor/arrow direction)."""
        if self._start_pt is None:
            _set_status(
                "Free Draw Mode: click anywhere on canvas (or a port) to place Start Point first.",
                is_error=True,
            )
            return False
        cv = self._view.active_cellview()
        dbu = float(cv.layout().dbu) if (cv is not None and cv.is_valid()) else 0.001
        settings = _get_route_settings()
        cs: CrossSection = settings["cross_section"]
        btype = str(settings.get("bend_type", "topic") or "topic")
        _, lt1 = self._start_taper_info(settings)
        if not self._waypoints:
            dvec, eff_ang = self._resolve_leg1_dir_and_angle(dir_name)
        else:
            dvec = self._resolve_manual_direction(dir_name)
            eff_ang = float(self._start_angle_deg or 0.0)
        adv = advance_manual_straight_waypoints(
            self._start_pt,
            eff_ang,
            self._waypoints,
            self._wp_meta,
            dvec,
            float(length_um),
            radius=cs.radius,
            euler_p=cs.euler_p,
            use_eff_radius=settings["use_eff_radius"],
            dbu=dbu,
            min_len_um=DEFAULT_SHIFT_STEP_UM,
            start_taper_len=lt1,
            bend_type=btype,
        )
        if not adv["ok"]:
            _set_status(adv["error"], is_error=True)
            return False

        if not self._waypoints and self._start_angle_deg is None:
            self._start_angle_deg = eff_ang
        self._waypoints = adv["waypoints"]
        self._wp_meta = adv["wp_meta"]
        self._typed_buf = ""
        self._forced_dir = None
        self._redraw_selected()
        dtag = self._dir_label(dvec)
        self._render_committed_preview(f"Added {dtag} straight {float(length_um):.3f} um")
        return True

    def _commit_free_waveguide(self) -> bool:
        """Commit the accumulated free-drawn segments into a multi-layer EulerWaveguide PCell."""
        if self._start_pt is None or not self._waypoints:
            _set_status(
                "Free Draw: place at least one straight segment first before pressing Enter.",
                is_error=True,
            )
            return False
        cv = self._view.active_cellview()
        if cv is None or not cv.is_valid() or cv.cell is None:
            return False
        layout = cv.layout()
        top_cell = cv.cell
        settings = _get_route_settings()
        cs: CrossSection = settings["cross_section"]
        btype = str(settings.get("bend_type", "topic") or "topic")
        pts = [self._start_pt] + list(self._waypoints)
        cs_use = cs
        res_open = route_from_guide_points(
            pts,
            radius=cs.radius,
            euler_p=cs.euler_p,
            min_straight=cs.min_straight,
            use_eff_radius=settings["use_eff_radius"],
            min_straight_ends=0.0,
            bend_type=btype,
        )
        if not res_open.ok and cs.min_straight > DEFAULT_SHIFT_STEP_UM:
            res_rel = route_from_guide_points(
                pts,
                radius=cs.radius,
                euler_p=cs.euler_p,
                min_straight=DEFAULT_SHIFT_STEP_UM,
                use_eff_radius=settings["use_eff_radius"],
                min_straight_ends=0.0,
                bend_type=btype,
            )
            if res_rel.ok:
                res_open = res_rel
                cs_use = cs.with_overrides(min_straight=DEFAULT_SHIFT_STEP_UM)

        sw, _ = self._start_taper_info(settings)
        taper_len = max(0.0, float(settings.get("taper_length", 10.0)))
        res_open_ann = (
            apply_taper_segments(
                res_open,
                core_width=cs_use.core_width,
                start_width=sw,
                end_width=None,
                taper_length=taper_len,
            )
            if res_open.ok
            else res_open
        )
        stamp_ports = self._start_port is None
        with _view_transaction(self._view, "PIC Free Draw Waveguide"):
            instantiate_euler_waveguide(
                layout,
                top_cell,
                res_open.guide_points if res_open.ok else pts,
                cs_use,
                start_width=sw,
                taper_length=taper_len,
                use_eff_radius=settings["use_eff_radius"],
                min_straight=cs_use.min_straight,
                bend_type=btype,
                add_ports=stamp_ports,
            )
            if res_open_ann.ok and settings.get("annotate_lengths", True):
                label_offset = max(4.0, 0.5 * _max_cs_width(cs_use) + 2.0)
                existing_r = _collect_euler_waveguide_routes(layout, top_cell)
                obs_pts: list[tuple[float, float]] = []
                for rec in existing_r:
                    obs_pts.extend(_sample_polyline_points(rec["result"].centerline, step_um=20.0))
                draw_length_annotations_to_cell(
                    layout,
                    top_cell,
                    res_open_ann,
                    length_layer=DEFAULT_LENGTH_LAYER,
                    label_offset=label_offset,
                    label_style=str(settings.get("label_style", "card")),
                    route_index=max(1, len(existing_r)),
                    obstacle_points=obs_pts,
                )

        _refresh_view_layers(self._view)
        _refresh_ports_table()
        _refresh_routes_table()
        ports_note = " (o1/o2 PinRec ports stamped)" if stamp_ports else ""
        if res_open_ann.ok:
            summary = format_segments_summary(
                res_open_ann.segments, res_open_ann.total_length, prefix="Total"
            )
            _set_status(f"Created Free-Draw Waveguide{ports_note}: {summary}")
        else:
            _set_status(f"Created Free-Draw Waveguide{ports_note} ({len(pts)} vertices).")
        self._reset()
        self._reload_ports()
        return True

    def activated(self) -> None:
        self._reset()
        self._reload_ports()
        _GUI_STATE["active_free_draw_plugin"] = self
        _set_status(
            "Free Draw Waveguide: Click anywhere on canvas (or an existing port) to place Start Point "
            "(drag/type length+Enter, Right-click undo, Enter to finish, Esc cancel)."
        )

    def deactivated(self) -> None:
        if _GUI_STATE.get("active_free_draw_plugin") is self:
            _GUI_STATE["active_free_draw_plugin"] = None
        self._reset()

    def drag_cancel(self) -> None:
        self._reset()

    def mouse_moved_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            self._last_mouse_pt = (float(p.x), float(p.y))
            self._hover_bag.clear()
            self._preview_bag.clear()
            pick_r, arrow_len = _dynamic_scale(self._view)
            exclude = self._start_port.full_name if self._start_port is not None else None
            port, _ = _find_nearest_port(
                self._world_ports, p.x, p.y, max_dist=pick_r, exclude_full_name=exclude
            )

            if self._start_pt is None:
                if port is not None:
                    self._hover_bag.add_port(port, _COLOR_HOVER, arrow_len=arrow_len, show_label=True)
                    _set_status(f"Free Draw: Click to start from port {port.full_name} (or click blank canvas).")
                else:
                    step = DEFAULT_SHIFT_STEP_UM
                    qx = round(float(p.x) / step) * step
                    qy = round(float(p.y) / step) * step
                    self._hover_bag.add_point_marker(qx, qy, _COLOR_HOVER, size=arrow_len)
                    _set_status(f"Free Draw: Click to place Start Point at ({qx:.0f}, {qy:.0f}) um.")
                return False

            if port is not None:
                self._hover_bag.add_port(port, _COLOR_TARGET, arrow_len=arrow_len, show_label=True)

            if self._typed_buf and port is None:
                self._render_typed_preview()
                return False

            settings = _get_route_settings()
            cs: CrossSection = settings["cross_section"]
            btype = str(settings.get("bend_type", "topic") or "topic")
            label_offset = max(4.0, 0.5 * _max_cs_width(cs) + 2.0)
            sw, lt1 = self._start_taper_info(settings)

            if port is not None:
                start_p = self._virtual_start_port(port, cs)
                ew, lt2 = _compute_port_taper(port.width, cs.core_width, settings)
                taper_len = max(0.0, float(settings.get("taper_length", 10.0)))
                res = plan_route_ports(
                    start_p,
                    port,
                    radius=cs.radius,
                    euler_p=cs.euler_p,
                    min_straight=cs.min_straight,
                    route_mode=settings["route_mode"],
                    waypoints=list(self._waypoints) if self._waypoints else None,
                    use_eff_radius=settings["use_eff_radius"],
                    start_taper_len=lt1,
                    end_taper_len=lt2,
                    bend_type=btype,
                )
                if not res.ok and self._waypoints and cs.min_straight > DEFAULT_SHIFT_STEP_UM:
                    res_rel = plan_route_ports(
                        start_p,
                        port,
                        radius=cs.radius,
                        euler_p=cs.euler_p,
                        min_straight=DEFAULT_SHIFT_STEP_UM,
                        route_mode=settings["route_mode"],
                        waypoints=list(self._waypoints),
                        use_eff_radius=settings["use_eff_radius"],
                        start_taper_len=lt1,
                        end_taper_len=lt2,
                        bend_type=btype,
                    )
                    if res_rel.ok:
                        res = res_rel
                if res.ok and len(res.centerline) >= 2:
                    res_disp = apply_taper_segments(
                        res,
                        core_width=cs.core_width,
                        start_width=sw,
                        end_width=ew,
                        taper_length=taper_len,
                    )
                    prev_col = _COLOR_PREVIEW_ERR if res.warnings else _COLOR_PREVIEW_OK
                    self._preview_bag.add_polyline(res.centerline, prev_col, width=cs.core_width)
                    _draw_live_segment_labels(
                        self._preview_bag,
                        res_disp.segments,
                        res_disp.total_length,
                        label_offset=label_offset,
                        color=_COLOR_LABEL_DARK,
                        total_prefix="Total",
                    )
                    summary = format_segments_summary(res_disp.segments, res_disp.total_length, prefix="Preview")
                    _set_status(f"{summary} (-> {port.full_name}; click to connect)")
                else:
                    fallback = res.guide_points if len(res.guide_points) >= 2 else [self._start_pt, (port.x, port.y)]
                    self._preview_bag.add_polyline(fallback, _COLOR_PREVIEW_ERR, width=0.0)
                    _set_status(f"Preview infeasible: {res.error}", is_error=True)
                return False

            cv = self._view.active_cellview()
            dbu = float(cv.layout().dbu) if (cv is not None and cv.is_valid()) else 0.001
            if not self._waypoints:
                _, eff_ang = self._resolve_leg1_dir_and_angle(
                    "Cursor", cursor_pt=(float(p.x), float(p.y)), ignore_forced=True
                )
            else:
                eff_ang = float(self._start_angle_deg or 0.0)

            drag = compute_manual_drag_waypoints(
                self._start_pt,
                eff_ang,
                self._waypoints,
                self._wp_meta,
                (float(p.x), float(p.y)),
                radius=cs.radius,
                euler_p=cs.euler_p,
                use_eff_radius=settings["use_eff_radius"],
                step_um=DEFAULT_SHIFT_STEP_UM,
                dbu=dbu,
                start_taper_len=lt1,
                bend_type=btype,
            )
            if drag["ok"]:
                pts = [self._start_pt] + list(drag["waypoints"])
                live = analyze_live_guide_segments(
                    pts,
                    radius=cs.radius,
                    euler_p=cs.euler_p,
                    min_straight=DEFAULT_SHIFT_STEP_UM,
                    use_eff_radius=settings["use_eff_radius"],
                    bend_type=btype,
                )
                live = self._split_live_taper(live, settings)
                line_col = _COLOR_HOVER if drag.get("action") in ("straight", "turn90", "extend") else _COLOR_PREVIEW_OK
                line_w = cs.core_width if live["has_euler"] else 0.0
                self._preview_bag.add_polyline(live["centerline"], line_col, width=line_w)
                _draw_live_segment_labels(
                    self._preview_bag,
                    live["segments"],
                    live["total_length"],
                    label_offset=label_offset,
                    color=_COLOR_LABEL_DARK,
                    total_prefix="Total",
                )
                _set_status(f"[FREE DRAW 1um] {live['summary']} (Click add, Enter finish)")
            elif drag.get("pending_taper"):
                gtip = drag["guide_tip"]
                self._preview_bag.add_polyline([self._start_pt, gtip], _COLOR_PREVIEW_ERR, width=0.0)
                self._preview_bag.add_label(
                    gtip[0],
                    gtip[1] + label_offset,
                    f"Taper > {drag['needed_taper']:.1f} um ({drag['dist_proj']:.1f}/{drag['needed_taper']:.1f})",
                    color=_COLOR_PREVIEW_ERR,
                )
                _set_status(drag["error"])
            elif drag.get("pending_bend"):
                if self._waypoints:
                    pts_cur = [self._start_pt] + list(self._waypoints)
                    live_cur = analyze_live_guide_segments(
                        pts_cur,
                        radius=cs.radius,
                        euler_p=cs.euler_p,
                        min_straight=DEFAULT_SHIFT_STEP_UM,
                        use_eff_radius=settings["use_eff_radius"],
                        bend_type=btype,
                    )
                    live_cur = self._split_live_taper(live_cur, settings)
                    self._preview_bag.add_polyline(
                        live_cur["centerline"],
                        _COLOR_PREVIEW_OK,
                        width=(cs.core_width if live_cur["has_euler"] else 0.0),
                    )
                    _draw_live_segment_labels(
                        self._preview_bag,
                        live_cur["segments"],
                        live_cur["total_length"],
                        label_offset=label_offset,
                        color=_COLOR_LABEL_DARK,
                        total_prefix="Total",
                    )
                    tip = self._waypoints[-1]
                    gtip = drag["guide_tip"]
                    self._preview_bag.add_polyline([tip, gtip], _COLOR_PREVIEW_ERR, width=0.0)
                    self._preview_bag.add_label(
                        gtip[0],
                        gtip[1] + label_offset,
                        f"90° bend > {drag['needed_t90']:.1f} um ({drag['dist_n']:.1f}/{drag['needed_t90']:.1f})",
                        color=_COLOR_PREVIEW_ERR,
                    )
                    _set_status(f"{live_cur['summary']} | {drag['error']}")
                else:
                    _set_status(drag["error"])
            else:
                if self._waypoints:
                    self._render_committed_preview(drag.get("error", ""))
                else:
                    _set_status(
                        "Free Draw: drag along ±X/±Y (>= 1 um), press Arrow+digits+Enter, or click an End Port."
                    )
        except Exception:
            traceback.print_exc()
        return False

    def mouse_click_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            self._last_mouse_pt = (float(p.x), float(p.y))
            if buttons & pya.ButtonState.RightButton:
                if self._typed_buf:
                    self._typed_buf = ""
                    self._forced_dir = None
                    self._render_committed_preview("Cleared typed length")
                    return True
                if self._waypoints:
                    undo_last_waypoint(self._waypoints, self._wp_meta)
                    if not self._waypoints and self._start_port is None:
                        self._start_angle_deg = None
                    self._redraw_selected()
                    if self._waypoints:
                        self._render_committed_preview(f"Undid last step ({len(self._waypoints)} left)")
                    else:
                        self._preview_bag.clear()
                        _set_status("Free Draw: removed last segment (0 left). Drag, type length, or Right-click to clear Start.")
                else:
                    self._reset()
                    _set_status("Free Draw: cleared Start Point. Click anywhere to start a new waveguide.")
                return True

            if not (buttons & pya.ButtonState.LeftButton):
                return False

            cv = self._view.active_cellview()
            if cv is None or not cv.is_valid() or cv.cell is None:
                return False
            layout = cv.layout()
            top_cell = cv.cell
            pick_r, _ = _dynamic_scale(self._view)

            if self._start_pt is None:
                self._reload_ports()
                port, _ = _find_nearest_port(self._world_ports, p.x, p.y, max_dist=pick_r)
                if port is not None:
                    self._start_port = port
                    self._start_pt = (float(port.x), float(port.y))
                    self._start_angle_deg = float(port.angle_deg)
                    msg = (
                        f"Free Draw Start snapped to {port.full_name}. "
                        "Drag+click, type straight length+Enter, click End Port, or press Enter to finish."
                    )
                else:
                    step = DEFAULT_SHIFT_STEP_UM
                    qx = round(float(p.x) / step) * step
                    qy = round(float(p.y) / step) * step
                    self._start_port = None
                    self._start_pt = (qx, qy)
                    self._start_angle_deg = None
                    msg = (
                        f"Free Draw Start at ({qx:.0f}, {qy:.0f}) um. "
                        "Drag along ±X/±Y + click, or Arrow+digits+Enter (Right-click undo, Enter finish)."
                    )
                self._waypoints.clear()
                self._wp_meta.clear()
                self._typed_buf = ""
                self._forced_dir = None
                self._redraw_selected()
                _set_status(msg)
                return True

            exclude = self._start_port.full_name if self._start_port is not None else None
            end_port, _ = _find_nearest_port(
                self._world_ports,
                p.x,
                p.y,
                max_dist=pick_r,
                exclude_full_name=exclude,
            )
            if end_port is not None:
                settings = _get_route_settings()
                cs: CrossSection = settings["cross_section"]
                start_p = self._virtual_start_port(end_port, cs)
                ok = _execute_port_route(
                    self._view,
                    layout,
                    top_cell,
                    start_p,
                    end_port,
                    waypoints=self._waypoints,
                    tx_title="PIC Free Draw Route",
                )
                if ok:
                    self._reset()
                    self._reload_ports()
                return True

            settings = _get_route_settings()
            cs = settings["cross_section"]
            btype = str(settings.get("bend_type", "topic") or "topic")
            _, lt1 = self._start_taper_info(settings)
            if not self._waypoints:
                _, eff_ang = self._resolve_leg1_dir_and_angle(
                    "Cursor", cursor_pt=(float(p.x), float(p.y)), ignore_forced=True
                )
            else:
                eff_ang = float(self._start_angle_deg or 0.0)

            drag = compute_manual_drag_waypoints(
                self._start_pt,
                eff_ang,
                self._waypoints,
                self._wp_meta,
                (float(p.x), float(p.y)),
                radius=cs.radius,
                euler_p=cs.euler_p,
                use_eff_radius=settings["use_eff_radius"],
                step_um=DEFAULT_SHIFT_STEP_UM,
                dbu=float(layout.dbu),
                start_taper_len=lt1,
                bend_type=btype,
            )
            if not drag["ok"]:
                _set_status(drag.get("error", "Cannot place straight segment here."), is_error=True)
                return True

            if not self._waypoints and self._start_angle_deg is None:
                self._start_angle_deg = eff_ang
            self._waypoints = drag["waypoints"]
            self._wp_meta = drag["wp_meta"]
            self._typed_buf = ""
            self._forced_dir = None
            self._redraw_selected()
            self._render_committed_preview(
                f"Placed straight {float(drag['straight_len']):.1f} um (#{len(self._waypoints)})"
            )
            return True
        except Exception as exc:
            _set_status(f"Free Draw error: {exc}", is_error=True)
            traceback.print_exc()
            self._reset()
            return True

    def key_event(self, key: int, buttons: int) -> bool:
        try:
            ctrl_mask = int(getattr(pya.ButtonState, "ControlKey", 2)) if pya is not None else 2
            alt_mask = int(getattr(pya.ButtonState, "AltKey", 4)) if pya is not None else 4
            if buttons & (ctrl_mask | alt_mask):
                return False

            if key == pya.KeyCode.Escape:
                if self._typed_buf:
                    self._typed_buf = ""
                    self._forced_dir = None
                    self._render_committed_preview("Cleared typed length")
                    return True
                if self._start_pt is not None:
                    self._reset()
                    _set_status("Free Draw Mode: cancelled current waveguide.")
                else:
                    self._view.switch_mode("select")
                    _set_status("Returned to Select mode.")
                return True

            if key == pya.KeyCode.Backspace:
                if self._typed_buf:
                    self._typed_buf = self._typed_buf[:-1]
                    self._render_typed_preview()
                    return True
                if self._waypoints:
                    undo_last_waypoint(self._waypoints, self._wp_meta)
                    if not self._waypoints and self._start_port is None:
                        self._start_angle_deg = None
                    self._redraw_selected()
                    if self._waypoints:
                        self._render_committed_preview(f"Removed last segment ({len(self._waypoints)} remaining)")
                    else:
                        self._preview_bag.clear()
                        _set_status("Removed last segment (0 remaining).")
                    return True
                if self._start_pt is not None:
                    self._reset()
                    _set_status("Free Draw Mode: cleared Start Point.")
                    return True
                return False

            if self._start_pt is not None:
                if 48 <= int(key) <= 57:
                    if len(self._typed_buf) < 10:
                        self._typed_buf += chr(int(key))
                    self._render_typed_preview()
                    return True
                if int(key) == 46 and "." not in self._typed_buf:
                    self._typed_buf = (self._typed_buf or "0") + "."
                    self._render_typed_preview()
                    return True

                arrow_dirs = {
                    int(pya.KeyCode.Right): (1.0, 0.0),
                    int(pya.KeyCode.Up): (0.0, 1.0),
                    int(pya.KeyCode.Left): (-1.0, 0.0),
                    int(pya.KeyCode.Down): (0.0, -1.0),
                }
                if int(key) in arrow_dirs:
                    self._forced_dir = arrow_dirs[int(key)]
                    self._render_typed_preview()
                    return True

                if key == pya.KeyCode.Tab:
                    dvec = self._resolve_manual_direction("Cursor")
                    dtag = self._dir_label(dvec)
                    default_v = float(self._typed_buf) if self._typed_buf else 50.0
                    val = pya.InputDialog.ask_double(
                        "Free Draw Straight Length",
                        f"Straight waveguide length along {dtag} (um, >= {DEFAULT_SHIFT_STEP_UM:.0f}):",
                        max(DEFAULT_SHIFT_STEP_UM, default_v),
                        DEFAULT_SHIFT_STEP_UM,
                        100000.0,
                        3,
                    )
                    if val is not None:
                        self.commit_manual_straight(float(val), dir_name="Cursor")
                    return True

                if int(key) == 32 and self._typed_buf:
                    try:
                        val = float(self._typed_buf)
                    except ValueError:
                        _set_status(f"Invalid straight length: {self._typed_buf!r}", is_error=True)
                        return True
                    self.commit_manual_straight(val, dir_name="Cursor")
                    return True

            if key in (pya.KeyCode.Return, pya.KeyCode.Enter) and self._start_pt is not None:
                if self._typed_buf:
                    try:
                        val = float(self._typed_buf)
                    except ValueError:
                        _set_status(f"Invalid straight length: {self._typed_buf!r}", is_error=True)
                        return True
                    self.commit_manual_straight(val, dir_name="Cursor")
                    return True
                self._commit_free_waveguide()
                return True
        except Exception as exc:
            _set_status(f"Free Draw key handler error: {exc}", is_error=True)
            traceback.print_exc()
        return False


class PICAddPortPlugin(_PluginBase):
    """Click near a waveguide edge to detect and stamp a PinRec port into that cell."""

    def __init__(self, view: Any) -> None:
        super().__init__()
        self._view = view

    def activated(self) -> None:
        _set_status("Add Port Mode: Click near a waveguide end-face edge to stamp a PinRec port (Esc to exit).")

    def deactivated(self) -> None:
        pass

    def _pick_target_cell(self, top_cell: Any, x: float, y: float, pad: float) -> tuple[Any, Any]:
        """Return (target_cell, world_to_local_dcplx_trans)."""
        # Prefer a selected instance if the click is inside its padded bbox.
        candidates = []
        for obj in self._view.each_object_selected():
            if obj.is_cell_inst():
                inst = obj.inst()
                if inst.cell is not None:
                    box = inst.dbbox()
                    if (
                        box.left - pad <= x <= box.right + pad
                        and box.bottom - pad <= y <= box.top + pad
                    ):
                        return inst.cell, inst.dcplx_trans.inverted()

        for inst in top_cell.each_inst():
            c = inst.cell
            if c is None:
                continue
            if c.is_pcell_variant() and c.pcell_name() == "EulerWaveguide":
                continue
            box = inst.dbbox()
            if (
                box.left - pad <= x <= box.right + pad
                and box.bottom - pad <= y <= box.top + pad
            ):
                area = max(0.0, float(box.width()) * float(box.height()))
                candidates.append((area, c, inst.dcplx_trans.inverted()))

        if candidates:
            candidates.sort(key=lambda item: item[0])
            _, cell, inv_t = candidates[0]
            return cell, inv_t

        return top_cell, pya.DCplxTrans()

    def mouse_click_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            if buttons & pya.ButtonState.RightButton:
                self._view.switch_mode("select")
                _set_status("Exited Add Port mode.")
                return True
            if not (buttons & pya.ButtonState.LeftButton):
                return False

            cv = self._view.active_cellview()
            if cv is None or not cv.is_valid() or cv.cell is None:
                return False
            layout = cv.layout()
            top_cell = cv.cell

            pick_r, _ = _dynamic_scale(self._view)
            search_r = max(5.0, min(50.0, pick_r))
            target_cell, inv_t = self._pick_target_cell(top_cell, p.x, p.y, search_r)
            local_pt = inv_t * pya.DPoint(p.x, p.y)

            settings = _get_route_settings()
            core_layers = [(1, 0), (2, 0)]
            cl = settings["core_layer"]
            if cl not in core_layers:
                core_layers.insert(0, cl)

            det = detect_port_near_point(
                layout,
                target_cell,
                float(local_pt.x),
                float(local_pt.y),
                search_radius=search_r,
                core_layers=tuple(core_layers),
            )
            if det is None:
                _set_status(
                    f"No waveguide end edge detected within {search_r:.1f} um in cell '{target_cell.name}'.",
                    is_error=True,
                )
                return True

            existing_names = {ep.name for ep in read_cell_ports(layout, target_cell, pin_layer=DEFAULT_PIN_LAYER)}
            idx = 1
            while f"o{idx}" in existing_names:
                idx += 1
            default_name = f"o{idx}"

            prompt = (
                f"Cell: {target_cell.name}\n"
                f"Local pos: ({det.x:.3f}, {det.y:.3f}) um | Angle: {det.angle_deg:.2f} deg\n"
                f"Width: {det.width:.3f} um | Core layer: {det.core_layer[0]}/{det.core_layer[1]}\n\n"
                f"Port name:"
            )
            port_name = pya.InputDialog.ask_string("Add PinRec Port", prompt, default_name)
            if port_name is None:
                _set_status("Add Port cancelled.")
                return True
            port_name = port_name.strip() or default_name

            with _view_transaction(self._view, f"PIC Add Port {port_name}"):
                add_port_to_cell(
                    layout,
                    target_cell,
                    name=port_name,
                    x=det.x,
                    y=det.y,
                    angle_deg=det.angle_deg,
                    width=det.width,
                    core_layer=det.core_layer,
                    pin_layer=DEFAULT_PIN_LAYER,
                )
            _refresh_view_layers(self._view)
            _refresh_ports_table()
            _set_status(
                f"Added port '{port_name}' to cell '{target_cell.name}' "
                f"(w={det.width:.3f} um, angle={det.angle_deg:.2f} deg)"
            )
            return True
        except Exception as exc:
            _set_status(f"Add Port error: {exc}", is_error=True)
            traceback.print_exc()
            return True

    def key_event(self, key: int, buttons: int) -> bool:
        if key == pya.KeyCode.Escape:
            self._view.switch_mode("select")
            _set_status("Exited Add Port mode.")
            return True
        return False


class PICPortAlignAxisPlugin(_PluginBase):
    """Click Port A (movable cell) -> Port B (target) to align X or Y coordinate exactly."""

    def __init__(self, view: Any, axis: str = "y") -> None:
        super().__init__()
        self._view = view
        self._axis = str(axis).strip().lower()
        self._world_ports: list[Port] = []
        self._src_port: Port | None = None
        self._hover_bag = _MarkerBag(view)
        self._sel_bag = _MarkerBag(view)
        self._guide_bag = _MarkerBag(view)

    def _reload_ports(self) -> None:
        cv = self._view.active_cellview()
        if cv is None or not cv.is_valid() or cv.cell is None:
            self._world_ports = []
            return
        self._world_ports = collect_world_ports(cv.layout(), cv.cell, pin_layer=DEFAULT_PIN_LAYER)

    def _reset(self) -> None:
        self._src_port = None
        self._hover_bag.clear()
        self._sel_bag.clear()
        self._guide_bag.clear()

    def activated(self) -> None:
        self._reset()
        self._reload_ports()
        ax = self._axis.upper()
        desc = "vertical straight route (same X)" if self._axis == "x" else "horizontal straight route (same Y)"
        if not self._world_ports:
            _set_status(
                f"Align {ax} Mode: No PinRec ports found. Run Auto-Detect Ports (Ctrl+Shift+D) first.",
                is_error=True,
            )
        else:
            _set_status(
                f"Align {ax} Mode ({desc}): Click Source Port on movable cell (Esc to cancel)."
            )

    def deactivated(self) -> None:
        self._reset()

    def drag_cancel(self) -> None:
        self._reset()

    def mouse_moved_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            self._hover_bag.clear()
            self._guide_bag.clear()
            pick_r, arrow_len = _dynamic_scale(self._view)
            if self._src_port is None:
                port, _ = _find_nearest_port(
                    self._world_ports, p.x, p.y, max_dist=pick_r, require_movable=True
                )
            else:
                port, _ = _find_nearest_port(
                    self._world_ports,
                    p.x,
                    p.y,
                    max_dist=pick_r,
                    exclude_full_name=self._src_port.full_name,
                    exclude_inst=self._src_port.inst_obj,
                    exclude_inst_id=self._src_port.inst_id,
                )
            if port is not None:
                color = _COLOR_HOVER if self._src_port is None else _COLOR_TARGET
                self._hover_bag.add_port(port, color, arrow_len=arrow_len, show_label=True)
                if self._src_port is not None:
                    # Preview the aligned axis line between moved source position and target port
                    moved_pt = (
                        (port.x, self._src_port.y)
                        if self._axis == "x"
                        else (self._src_port.x, port.y)
                    )
                    self._guide_bag.add_polyline(
                        [(self._src_port.x, self._src_port.y), moved_pt, (port.x, port.y)],
                        _COLOR_PREVIEW_OK,
                        width=0.0,
                    )
        except Exception:
            traceback.print_exc()
        return False

    def mouse_click_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            ax = self._axis.upper()
            if buttons & pya.ButtonState.RightButton:
                self._reset()
                _set_status(f"Align {ax} Mode: selection cleared. Click Source Port.")
                return True
            if not (buttons & pya.ButtonState.LeftButton):
                return False

            pick_r, arrow_len = _dynamic_scale(self._view)
            cv = self._view.active_cellview()
            if cv is None or not cv.is_valid():
                return False
            layout = cv.layout()

            if self._src_port is None:
                self._reload_ports()
                port, _ = _find_nearest_port(
                    self._world_ports, p.x, p.y, max_dist=pick_r, require_movable=True
                )
                if port is None:
                    _set_status(f"Align {ax} Mode: click closer to a movable cell's port.", is_error=True)
                    return True
                self._src_port = port
                self._sel_bag.clear()
                self._sel_bag.add_port(port, _COLOR_SOURCE, arrow_len=arrow_len, show_label=True)
                _set_status(
                    f"Align {ax} Source: {port.full_name}. Now click Target Port (Right-click / Esc to cancel)."
                )
                return True

            tgt_port, _ = _find_nearest_port(
                self._world_ports,
                p.x,
                p.y,
                max_dist=pick_r,
                exclude_full_name=self._src_port.full_name,
                exclude_inst=self._src_port.inst_obj,
                exclude_inst_id=self._src_port.inst_id,
            )
            if tgt_port is None:
                _set_status(f"Align {ax} Mode: click closer to a Target Port on another cell.", is_error=True)
                return True

            _execute_port_align_axis(self._view, layout, self._src_port, tgt_port, axis=self._axis)
            self._reset()
            self._reload_ports()
            return True
        except Exception as exc:
            _set_status(f"Align {self._axis.upper()} error: {exc}", is_error=True)
            traceback.print_exc()
            self._reset()
            return True

    def key_event(self, key: int, buttons: int) -> bool:
        if key == pya.KeyCode.Escape:
            if self._src_port is not None:
                self._reset()
                _set_status(f"Align {self._axis.upper()} Mode: selection cleared.")
            else:
                self._view.switch_mode("select")
                _set_status("Returned to Select mode.")
            return True
        return False


class PICCenterPairPlugin(_PluginBase):
    """4-click symmetric pair centering: (Src P1, Src P2) centered between (Tgt P1, Tgt P2)."""

    def __init__(self, view: Any) -> None:
        super().__init__()
        self._view = view
        self._world_ports: list[Port] = []
        self._picks: list[Port] = []
        self._hover_bag = _MarkerBag(view)
        self._sel_bag = _MarkerBag(view)
        self._preview_bag = _MarkerBag(view)

    def _reload_ports(self) -> None:
        cv = self._view.active_cellview()
        if cv is None or not cv.is_valid() or cv.cell is None:
            self._world_ports = []
            return
        self._world_ports = collect_world_ports(cv.layout(), cv.cell, pin_layer=DEFAULT_PIN_LAYER)

    def _reset(self) -> None:
        self._picks.clear()
        self._hover_bag.clear()
        self._sel_bag.clear()
        self._preview_bag.clear()

    def _redraw_picks(self) -> None:
        self._sel_bag.clear()
        _, arrow_len = _dynamic_scale(self._view)
        for idx, pt in enumerate(self._picks):
            color = _COLOR_SOURCE if idx < 2 else _COLOR_TARGET
            self._sel_bag.add_port(pt, color, arrow_len=arrow_len, show_label=True)
        if len(self._picks) >= 2:
            a, b = self._picks[0], self._picks[1]
            self._sel_bag.add_polyline([(a.x, a.y), (b.x, b.y)], _COLOR_SOURCE, width=0.0)

    def _pick_candidate(self, x: float, y: float, pick_r: float) -> Port | None:
        stage = len(self._picks)
        if stage == 0:
            port, _ = _find_nearest_port(self._world_ports, x, y, max_dist=pick_r, require_movable=True)
            return port
        if stage == 1:
            p0 = self._picks[0]
            same_inst_ports = [
                pt
                for pt in self._world_ports
                if pt.full_name != p0.full_name and _same_inst_ref(pt.inst_obj, p0.inst_obj)
            ]
            port, _ = _find_nearest_port(same_inst_ports, x, y, max_dist=pick_r)
            return port
        p0 = self._picks[0]
        excl_name = self._picks[2].full_name if stage >= 3 else None
        port, _ = _find_nearest_port(
            self._world_ports,
            x,
            y,
            max_dist=pick_r,
            exclude_full_name=excl_name,
            exclude_inst=p0.inst_obj,
            exclude_inst_id=p0.inst_id,
        )
        return port

    def _prompt_stage(self) -> None:
        stage = len(self._picks)
        if stage == 0:
            _set_status(
                f"Center Pair Mode ({len(self._world_ports)} ports): "
                "[1/4] Click Port 1 of the movable (narrow) pair."
            )
        elif stage == 1:
            _set_status(
                f"Center Pair [2/4]: Src P1={self._picks[0].full_name}. "
                "Click Port 2 on the SAME movable cell (Right-click undo)."
            )
        elif stage == 2:
            _set_status(
                f"Center Pair [3/4]: Movable pair=({self._picks[0].full_name}, {self._picks[1].name}). "
                "Click Target Port 1 on the reference (wide) cell."
            )
        elif stage == 3:
            _set_status(
                f"Center Pair [4/4]: Tgt P1={self._picks[2].full_name}. "
                "Click Target Port 2 to center symmetrically."
            )

    def activated(self) -> None:
        self._reset()
        self._reload_ports()
        if not self._world_ports:
            _set_status(
                "Center Pair Mode: No PinRec ports found. Run Auto-Detect Ports (Ctrl+Shift+D) first.",
                is_error=True,
            )
        else:
            self._prompt_stage()

    def deactivated(self) -> None:
        self._reset()

    def drag_cancel(self) -> None:
        self._reset()

    def mouse_moved_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            self._hover_bag.clear()
            self._preview_bag.clear()
            pick_r, arrow_len = _dynamic_scale(self._view)
            cand = self._pick_candidate(p.x, p.y, pick_r)
            if cand is not None:
                color = _COLOR_HOVER if len(self._picks) < 2 else _COLOR_TARGET
                self._hover_bag.add_port(cand, color, arrow_len=arrow_len, show_label=True)
                if len(self._picks) == 3:
                    sa, sb, ta = self._picks[0], self._picks[1], self._picks[2]
                    tb = cand
                    self._preview_bag.add_polyline([(ta.x, ta.y), (tb.x, tb.y)], _COLOR_TARGET, width=0.0)
                    smx, smy = 0.5 * (sa.x + sb.x), 0.5 * (sa.y + sb.y)
                    tmx, tmy = 0.5 * (ta.x + tb.x), 0.5 * (ta.y + tb.y)
                    self._preview_bag.add_polyline([(smx, smy), (tmx, tmy)], _COLOR_PREVIEW_OK, width=0.0)
        except Exception:
            traceback.print_exc()
        return False

    def mouse_click_event(self, p: Any, buttons: int, prio: bool) -> bool:
        if not prio:
            return False
        try:
            if buttons & pya.ButtonState.RightButton:
                if self._picks:
                    self._picks.pop()
                    self._redraw_picks()
                    self._prompt_stage()
                else:
                    self._reset()
                    self._prompt_stage()
                return True
            if not (buttons & pya.ButtonState.LeftButton):
                return False

            cv = self._view.active_cellview()
            if cv is None or not cv.is_valid():
                return False
            layout = cv.layout()
            pick_r, _ = _dynamic_scale(self._view)

            if not self._picks:
                self._reload_ports()

            cand = self._pick_candidate(p.x, p.y, pick_r)
            if cand is None:
                _set_status("Center Pair Mode: click closer to a valid port for this step.", is_error=True)
                return True

            self._picks.append(cand)
            self._redraw_picks()

            if len(self._picks) < 4:
                self._prompt_stage()
                return True

            sa, sb, ta, tb = self._picks
            _execute_center_pair_ports(
                self._view,
                layout,
                sa,
                sb,
                ta,
                tb,
                axis="auto",
                tx_title="PIC Center Pair Ports",
            )
            self._reset()
            self._reload_ports()
            return True
        except Exception as exc:
            _set_status(f"Center Pair Mode error: {exc}", is_error=True)
            traceback.print_exc()
            self._reset()
            return True

    def key_event(self, key: int, buttons: int) -> bool:
        if key == pya.KeyCode.Escape:
            if self._picks:
                self._reset()
                self._prompt_stage()
            else:
                self._view.switch_mode("select")
                _set_status("Returned to Select mode.")
            return True
        return False


class PICPortSnapFactory(_PluginFactoryBase):
    def __init__(self) -> None:
        super().__init__()
        self.register(21000, MODE_SNAP, "PIC Snap 2 Ports")

    def create_plugin(self, manager: Any, root: Any, view: Any) -> Any:
        return PICPortSnapPlugin(view)


class PICPortRouteFactory(_PluginFactoryBase):
    def __init__(self) -> None:
        super().__init__()
        self.register(21001, MODE_ROUTE, "PIC Euler Route 2 Ports")

    def create_plugin(self, manager: Any, root: Any, view: Any) -> Any:
        return PICPortRoutePlugin(view)


class PICAddPortFactory(_PluginFactoryBase):
    def __init__(self) -> None:
        super().__init__()
        self.register(21002, MODE_ADD_PORT, "PIC Add Port (Click Edge)")

    def create_plugin(self, manager: Any, root: Any, view: Any) -> Any:
        return PICAddPortPlugin(view)


class PICPortAlignXFactory(_PluginFactoryBase):
    def __init__(self) -> None:
        super().__init__()
        self.register(21003, MODE_ALIGN_X, "PIC Align X (2 Ports)")

    def create_plugin(self, manager: Any, root: Any, view: Any) -> Any:
        return PICPortAlignAxisPlugin(view, axis="x")


class PICPortAlignYFactory(_PluginFactoryBase):
    def __init__(self) -> None:
        super().__init__()
        self.register(21004, MODE_ALIGN_Y, "PIC Align Y (2 Ports)")

    def create_plugin(self, manager: Any, root: Any, view: Any) -> Any:
        return PICPortAlignAxisPlugin(view, axis="y")


class PICCenterPairFactory(_PluginFactoryBase):
    def __init__(self) -> None:
        super().__init__()
        self.register(21005, MODE_CENTER_PAIR, "PIC Center Pair Ports (2->2)")

    def create_plugin(self, manager: Any, root: Any, view: Any) -> Any:
        return PICCenterPairPlugin(view)


class PICFreeDrawWaveguideFactory(_PluginFactoryBase):
    def __init__(self) -> None:
        super().__init__()
        self.register(21006, MODE_FREE_DRAW, "PIC Free Draw Waveguide")

    def create_plugin(self, manager: Any, root: Any, view: Any) -> Any:
        return PICFreeDrawWaveguidePlugin(view)


# ---------------------------------------------------------------------------
# Dockable Side Panel (Qt5 / Qt6 compatible)
# ---------------------------------------------------------------------------

class PICRouterDock(_QDockWidgetBase):
    """Dockable right-hand side panel for PIC Snap & Router."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__("PIC Snap & Router", parent)
        self.setObjectName("PICRouterDock")
        self.setAllowedAreas(pya.Qt.LeftDockWidgetArea | pya.Qt.RightDockWidgetArea)
        self.setFeatures(
            pya.QDockWidget.DockWidgetClosable
            | pya.QDockWidget.DockWidgetMovable
            | pya.QDockWidget.DockWidgetFloatable
        )

        self._world_ports: list[Port] = []
        self._filtered_ports: list[Port] = []
        self._selection_order: list[int] = []
        self._routes_cache: list[dict[str, Any]] = []
        self._route_selection_order: list[int] = []
        self._updating_preset = False
        self._last_status_text: str | None = None
        self._last_status_is_error: bool | None = None
        # Keep strong Python references to all Qt widgets/layouts created in _build_ui
        # so KLayout's Python/Qt bridge never destroys child widgets on local-var GC.
        self._ui_refs: list[Any] = []

        self._build_ui()
        self._apply_preset(DEFAULT_PRESET_NAME)

    def _keep(self, obj: Any) -> Any:
        self._ui_refs.append(obj)
        return obj

    def _build_ui(self) -> None:
        scroll = self._keep(pya.QScrollArea(self))
        scroll.setWidgetResizable(True)

        container = self._keep(pya.QWidget(scroll))
        root = self._keep(pya.QVBoxLayout(container))
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # 1. Cross-Section & Route Parameters
        param_group = self._keep(pya.QGroupBox("Cross-Section & Route Params", container))
        grid = self._keep(pya.QGridLayout(param_group))
        grid.setSpacing(4)

        grid.addWidget(self._keep(pya.QLabel("Preset:", param_group)), 0, 0)
        preset_row = self._keep(pya.QHBoxLayout())
        preset_row.setSpacing(3)
        self.preset_combo = self._keep(pya.QComboBox(param_group))
        self._reload_preset_combo(select_name=DEFAULT_PRESET_NAME)
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        preset_row.addWidget(self.preset_combo, 1)

        btn_save_preset = self._keep(pya.QPushButton("Save", param_group))
        btn_save_preset.setMaximumWidth(44)
        btn_save_preset.setToolTip(
            "Save current Core/Trench layers, widths, radius, p & min straight as a persistent JSON user preset"
        )
        btn_save_preset.clicked.connect(lambda *_a: self._on_save_user_preset())
        preset_row.addWidget(btn_save_preset)

        btn_del_preset = self._keep(pya.QPushButton("Del", param_group))
        btn_del_preset.setMaximumWidth(38)
        btn_del_preset.setToolTip("Delete the selected custom user preset (built-in templates are protected)")
        btn_del_preset.clicked.connect(lambda *_a: self._on_delete_user_preset())
        preset_row.addWidget(btn_del_preset)
        grid.addLayout(preset_row, 0, 1, 1, 3)

        grid.addWidget(self._keep(pya.QLabel("Core W (um):", param_group)), 1, 0)
        self.core_w_spin = self._keep(pya.QDoubleSpinBox(param_group))
        self.core_w_spin.setRange(0.05, 100.0)
        self.core_w_spin.setDecimals(3)
        self.core_w_spin.setSingleStep(0.01)
        self.core_w_spin.setValue(0.80)
        grid.addWidget(self.core_w_spin, 1, 1)

        grid.addWidget(self._keep(pya.QLabel("Trench W:", param_group)), 1, 2)
        self.trench_w_spin = self._keep(pya.QDoubleSpinBox(param_group))
        self.trench_w_spin.setRange(0.0, 200.0)
        self.trench_w_spin.setDecimals(2)
        self.trench_w_spin.setSingleStep(1.0)
        self.trench_w_spin.setValue(10.0)
        grid.addWidget(self.trench_w_spin, 1, 3)

        grid.addWidget(self._keep(pya.QLabel("Core L/D:", param_group)), 2, 0)
        self.core_layer_edit = self._keep(pya.QLineEdit("1/0", param_group))
        self.core_layer_edit.setToolTip(
            "Single or ';'-separated core layers: e.g. '1/0' or '1/0; 2/0'\n"
            "Optional per-layer width: '1/0; 2/0=4.0'"
        )
        grid.addWidget(self.core_layer_edit, 2, 1)

        grid.addWidget(self._keep(pya.QLabel("Trench L/D:", param_group)), 2, 2)
        self.trench_layer_edit = self._keep(pya.QLineEdit("1/1", param_group))
        self.trench_layer_edit.setToolTip(
            "Single or ';'-separated trench layers (e.g. double-etch): '1/1; 2/1'\n"
            "Optional per-layer width: '1/1=10; 2/1=25'"
        )
        grid.addWidget(self.trench_layer_edit, 2, 3)

        grid.addWidget(self._keep(pya.QLabel("Radius (um):", param_group)), 3, 0)
        self.radius_spin = self._keep(pya.QDoubleSpinBox(param_group))
        self.radius_spin.setRange(1.0, 5000.0)
        self.radius_spin.setDecimals(1)
        self.radius_spin.setSingleStep(5.0)
        self.radius_spin.setValue(50.0)
        grid.addWidget(self.radius_spin, 3, 1)

        grid.addWidget(self._keep(pya.QLabel("Bend p:", param_group)), 3, 2)
        self.euler_p_spin = self._keep(pya.QDoubleSpinBox(param_group))
        self.euler_p_spin.setRange(0.0, 1.0)
        self.euler_p_spin.setDecimals(2)
        self.euler_p_spin.setSingleStep(0.05)
        self.euler_p_spin.setValue(0.50)
        self.euler_p_spin.setToolTip(
            "Transition fraction p = 2·θ_p / |θ_t| for TOPIC & Euler bends\n"
            "(0 = circular arc, 0.50 => θ_p = 22.5° for 90° bend, 1.0 = full transition)"
        )
        grid.addWidget(self.euler_p_spin, 3, 3)

        grid.addWidget(self._keep(pya.QLabel("Bend Curve:", param_group)), 4, 0)
        self.bend_type_combo = self._keep(pya.QComboBox(param_group))
        for lbl, _code in BEND_TYPE_ITEMS:
            self.bend_type_combo.addItem(lbl)
        self.bend_type_combo.setToolTip(
            "Bend curvature law for auto-routing, manual routing, and Path -> Waveguide:\n"
            "• TOPIC (C3) [default]: cubic Hermite κ(u)=(3u²-2u³)/Rc, κ'=0 at all 4 junctions (lower loss, slightly larger Rmin)\n"
            "• Euler (C2): linear clothoid κ(u)=u/Rc, κ' jumps at straight/circular junctions"
        )
        grid.addWidget(self.bend_type_combo, 4, 1)

        grid.addWidget(self._keep(pya.QLabel("Auto Route:", param_group)), 4, 2)
        self.route_mode_combo = self._keep(pya.QComboBox(param_group))
        for lbl, _code in ROUTE_MODE_ITEMS:
            self.route_mode_combo.addItem(lbl)
        self.route_mode_combo.setToolTip(
            "Auto-route style for offset parallel ports:\n"
            "• S-Bend (C2 Bezier): zero-curvature Quintic Bezier S-bend (largest Rmin)\n"
            "• S-Bend (C2 Sine): zero-curvature Raised-Sine S-bend\n"
            "• 90° Bend + Straight: strictly 90° TOPIC/Euler bends + straight segments\n"
            "(U-turns and 90° L-turns ALWAYS use 90° TOPIC/Euler bends + Straight)"
        )
        grid.addWidget(self.route_mode_combo, 4, 3)

        grid.addWidget(self._keep(pya.QLabel("Min Straight:", param_group)), 5, 0)
        self.min_straight_spin = self._keep(pya.QDoubleSpinBox(param_group))
        self.min_straight_spin.setRange(1.0, 1000.0)
        self.min_straight_spin.setDecimals(1)
        self.min_straight_spin.setSingleStep(1.0)
        self.min_straight_spin.setValue(5.0)
        grid.addWidget(self.min_straight_spin, 5, 1)

        grid.addWidget(self._keep(pya.QLabel("Taper L (um):", param_group)), 5, 2)
        self.taper_len_spin = self._keep(pya.QDoubleSpinBox(param_group))
        self.taper_len_spin.setRange(0.0, 1000.0)
        self.taper_len_spin.setDecimals(1)
        self.taper_len_spin.setSingleStep(5.0)
        self.taper_len_spin.setValue(10.0)
        self.taper_len_spin.setToolTip(
            "Linear taper length (um) when port width != Core W (does NOT consume straight S1/S2 length)"
        )
        grid.addWidget(self.taper_len_spin, 5, 3)

        self.auto_taper_cb = self._keep(pya.QCheckBox("Auto Taper Port Widths", param_group))
        self.auto_taper_cb.setToolTip(
            "Automatically insert linear width taper(s) when port width differs from Core W.\n"
            "Linear taper length is reserved outside straight waveguides & bends."
        )
        self.auto_taper_cb.setChecked(True)
        grid.addWidget(self.auto_taper_cb, 6, 0, 1, 2)

        self.integer_length_cb = self._keep(pya.QCheckBox("Integer um Length", param_group))
        self.integer_length_cb.setToolTip(
            "Auto-route exact integer um total length (tunes R_eff / S-bend stub, or snaps Port 2).\n"
            "Manual routes preserve your exact pure straight lengths S1/S2 and identical fixed bend radius."
        )
        self.integer_length_cb.setChecked(True)
        grid.addWidget(self.integer_length_cb, 6, 2, 1, 2)

        self.annotate_lengths_cb = self._keep(pya.QCheckBox("Annotate Lengths (90/2)", param_group))
        self.annotate_lengths_cb.setToolTip(
            "Write total & per-segment lengths + Rmin/Reff onto dark layer RouteLength (90/2)"
        )
        self.annotate_lengths_cb.setChecked(True)
        grid.addWidget(self.annotate_lengths_cb, 7, 0, 1, 2)

        self.allow_rotate_cb = self._keep(pya.QCheckBox("Allow Rotate on Snap", param_group))
        self.allow_rotate_cb.setChecked(True)
        grid.addWidget(self.allow_rotate_cb, 7, 2, 1, 2)

        grid.addWidget(self._keep(pya.QLabel("Labels (90/2):", param_group)), 8, 0)
        self.label_style_combo = self._keep(pya.QComboBox(param_group))
        for lbl, _code in LABEL_STYLE_ITEMS:
            self.label_style_combo.addItem(lbl)
        self.label_style_combo.setToolTip(
            "Canvas RouteLength (90/2) layout style:\n"
            "• Compact Card (default): 1 multi-line info card + leader per route (zero overlap!)\n"
            "• Per-Segment: clean collision-avoiding label per segment + concise Total header\n"
            "• Total Only: minimal 1-line Total + Rmin/Reff tag (see full breakdown in Routes tab)"
        )
        self.label_style_combo.currentTextChanged.connect(lambda *_a: self._on_label_style_changed())
        grid.addWidget(self.label_style_combo, 8, 1, 1, 3)

        root.addWidget(param_group)

        # 2. Interactive Canvas Tools
        tools_group = self._keep(pya.QGroupBox("Interactive Canvas Tools", container))
        tools_layout = self._keep(pya.QGridLayout(tools_group))
        tools_layout.setSpacing(4)

        btn_snap = self._keep(pya.QPushButton("Snap 2 Ports", tools_group))
        btn_snap.setToolTip(f"Click Port A (movable) -> Port B (target) [{SHORTCUTS['snap_mode']}]")
        btn_snap.clicked.connect(lambda *_a: action_activate_mode(MODE_SNAP, "Snap 2 Ports"))
        tools_layout.addWidget(btn_snap, 0, 0)

        btn_route = self._keep(pya.QPushButton("Route 2 Ports (Shift=H/V)", tools_group))
        btn_route.setToolTip(
            f"Click Port A -> optional waypoints (hold Shift for H/V ortho) -> Port B [{SHORTCUTS['route_mode']}]"
        )
        btn_route.clicked.connect(lambda *_a: action_activate_mode(MODE_ROUTE, "Route 2 Ports"))
        tools_layout.addWidget(btn_route, 0, 1)

        btn_align_x = self._keep(pya.QPushButton("Align X (2 Ports)", tools_group))
        btn_align_x.setToolTip(
            f"Align X coordinate (vertical straight route): Port A (movable) -> Port B [{SHORTCUTS['align_x_mode']}]"
        )
        btn_align_x.clicked.connect(lambda *_a: action_activate_mode(MODE_ALIGN_X, "Align X (2 Ports)"))
        tools_layout.addWidget(btn_align_x, 1, 0)

        btn_align_y = self._keep(pya.QPushButton("Align Y (2 Ports)", tools_group))
        btn_align_y.setToolTip(
            f"Align Y coordinate (horizontal straight route): Port A (movable) -> Port B [{SHORTCUTS['align_y_mode']}]"
        )
        btn_align_y.clicked.connect(lambda *_a: action_activate_mode(MODE_ALIGN_Y, "Align Y (2 Ports)"))
        tools_layout.addWidget(btn_align_y, 1, 1)

        btn_center_pair = self._keep(pya.QPushButton("Center Pair (2->2)", tools_group))
        btn_center_pair.setToolTip(
            f"Center movable port pair symmetrically between target pair [{SHORTCUTS['center_pair_mode']}]"
        )
        btn_center_pair.clicked.connect(
            lambda *_a: action_activate_mode(MODE_CENTER_PAIR, "Center Pair Ports")
        )
        tools_layout.addWidget(btn_center_pair, 2, 0)

        btn_add_port = self._keep(pya.QPushButton("Add Port (Click Edge)", tools_group))
        btn_add_port.setToolTip(f"Click near waveguide end face to stamp PinRec [{SHORTCUTS['add_port_mode']}]")
        btn_add_port.clicked.connect(lambda *_a: action_activate_mode(MODE_ADD_PORT, "Add Port"))
        tools_layout.addWidget(btn_add_port, 2, 1)

        btn_free_draw = self._keep(pya.QPushButton("Free Draw Waveguide", tools_group))
        btn_free_draw.setToolTip(
            f"Draw multi-layer TOPIC/Euler waveguide freely on blank canvas (zero ports required; "
            f"auto-stamps o1/o2 PinRec ports on Enter) [{SHORTCUTS['free_draw_mode']}]"
        )
        btn_free_draw.clicked.connect(
            lambda *_a: action_activate_mode(MODE_FREE_DRAW, "Free Draw Waveguide")
        )
        tools_layout.addWidget(btn_free_draw, 3, 0, 1, 2)

        # Manual straight length step input (for active Route 2 Ports / Free Draw session)
        manual_row = self._keep(pya.QHBoxLayout())
        manual_row.setSpacing(4)
        manual_row.addWidget(self._keep(pya.QLabel("Straight L (um):", tools_group)))
        self.manual_len_spin = self._keep(pya.QDoubleSpinBox(tools_group))
        self.manual_len_spin.setRange(DEFAULT_SHIFT_STEP_UM, 100000.0)
        self.manual_len_spin.setDecimals(3)
        self.manual_len_spin.setSingleStep(1.0)
        self.manual_len_spin.setValue(50.0)
        self.manual_len_spin.setToolTip(
            "Exact straight waveguide length (um) to add in Route Mode or Free Draw Mode.\n"
            "Tip: You can also type digits directly on canvas + Enter (or press Tab)!"
        )
        manual_row.addWidget(self.manual_len_spin)

        self.manual_dir_combo = self._keep(pya.QComboBox(tools_group))
        for d_lbl in ("Cursor", "+X (→)", "+Y (↑)", "-X (←)", "-Y (↓)"):
            self.manual_dir_combo.addItem(d_lbl)
        self.manual_dir_combo.setToolTip("Direction for the manual straight segment (Cursor follows mouse / port tangent)")
        manual_row.addWidget(self.manual_dir_combo)

        btn_add_straight = self._keep(pya.QPushButton("Add Straight", tools_group))
        btn_add_straight.setToolTip(
            "Append exact straight segment in active Route 2 Ports or Free Draw session (auto-adds 90° bend on turns)"
        )
        btn_add_straight.clicked.connect(
            lambda *_a: action_add_manual_straight(
                float(self.manual_len_spin.value), str(self.manual_dir_combo.currentText)
            )
        )
        manual_row.addWidget(btn_add_straight)
        tools_layout.addLayout(manual_row, 4, 0, 1, 2)

        root.addWidget(tools_group)

        # 3. Quick Actions
        quick_group = self._keep(pya.QGroupBox("Quick Actions", container))
        quick_layout = self._keep(pya.QGridLayout(quick_group))
        quick_layout.setSpacing(4)

        btn_qsnap = self._keep(pya.QPushButton("Quick Snap Selected Cell", quick_group))
        btn_qsnap.setToolTip(f"1-click snap selected instance to nearest neighbor port [{SHORTCUTS['quick_snap']}]")
        btn_qsnap.clicked.connect(lambda *_a: action_quick_snap_selected())
        quick_layout.addWidget(btn_qsnap, 0, 0)

        btn_autodetect = self._keep(pya.QPushButton("Auto-Detect / Refresh Ports", quick_group))
        btn_autodetect.setToolTip(
            f"Clear old PinRec (90/0) and re-detect fresh ports from current geometry "
            f"(updates widths after CD Bias or edits) [{SHORTCUTS['auto_detect_ports']}]"
        )
        btn_autodetect.clicked.connect(lambda *_a: action_auto_detect_ports())
        quick_layout.addWidget(btn_autodetect, 0, 1)

        btn_path2wg = self._keep(pya.QPushButton("Path -> Euler Waveguide", quick_group))
        btn_path2wg.setToolTip(f"Convert selected Path(s) to EulerWaveguide PCell [{SHORTCUTS['path_to_waveguide']}]")
        btn_path2wg.clicked.connect(lambda *_a: action_path_to_euler_waveguide())
        quick_layout.addWidget(btn_path2wg, 1, 0)

        btn_revert = self._keep(pya.QPushButton("Revert WG -> Path", quick_group))
        btn_revert.setToolTip(f"Revert selected EulerWaveguide PCell back to DPath [{SHORTCUTS['revert_waveguide']}]")
        btn_revert.clicked.connect(lambda *_a: action_revert_waveguide_to_path())
        quick_layout.addWidget(btn_revert, 1, 1)

        btn_ref_len = self._keep(pya.QPushButton("Refresh Lengths (90/2)", quick_group))
        btn_ref_len.setToolTip("Recompute dark RouteLength (90/2) labels for all EulerWaveguide PCells")
        btn_ref_len.clicked.connect(lambda *_a: action_refresh_length_layer())
        quick_layout.addWidget(btn_ref_len, 2, 0)

        btn_del_len = self._keep(pya.QPushButton("Delete Lengths (90/2)", quick_group))
        btn_del_len.setToolTip("Delete RouteLength (90/2) annotation layer from active layout")
        btn_del_len.clicked.connect(lambda *_a: action_delete_length_layer())
        quick_layout.addWidget(btn_del_len, 2, 1)

        btn_add_trench = self._keep(pya.QPushButton("Add Trench (Multi)...", quick_group))
        btn_add_trench.setToolTip(
            "Generate single- or multi-layer trench(es) around existing core-only PIC structures\n"
            "(supports ';' e.g. '1/1=10; 2/1=25', trimmed flush at ports)"
        )
        btn_add_trench.clicked.connect(lambda *_a: action_add_trench_dialog())
        quick_layout.addWidget(btn_add_trench, 3, 0)

        btn_cd_bias = self._keep(pya.QPushButton("CD Bias (ΔW nm)...", quick_group))
        btn_cd_bias.setToolTip(
            "Linewidth compensation (e.g. +120 nm / +70 nm) on straights, bends, MMIs & PCells\n"
            "while keeping total centerline length & port faces strictly unchanged"
        )
        btn_cd_bias.clicked.connect(lambda *_a: action_cd_bias_dialog())
        quick_layout.addWidget(btn_cd_bias, 3, 1)

        btn_topic_bend = self._keep(pya.QPushButton("Create TOPIC Bend (90° / Custom)...", quick_group))
        btn_topic_bend.setToolTip(
            "Generate a constant-width multi-layer TOPIC (C3 Hermite) bend PCell with specified R_eff & p/θ_p\n"
            "and automatic flush PinRec ports (o1, o2)"
        )
        btn_topic_bend.clicked.connect(lambda *_a: action_create_topic_bend_dialog())
        quick_layout.addWidget(btn_topic_bend, 4, 0, 1, 2)

        root.addWidget(quick_group)

        # 4. Tabbed Inspector: World Ports | Routes (Lengths & ΔL)
        self.inspector_tabs = self._keep(pya.QTabWidget(container))

        # --- Tab 1: World Ports ---
        ports_tab = self._keep(pya.QWidget(self.inspector_tabs))
        ports_layout = self._keep(pya.QVBoxLayout(ports_tab))
        ports_layout.setContentsMargins(4, 4, 4, 4)
        ports_layout.setSpacing(4)

        filter_row = self._keep(pya.QHBoxLayout())
        self.filter_edit = self._keep(pya.QLineEdit(ports_tab))
        self.filter_edit.setPlaceholderText("Filter by ID, cell, layer...")
        self.filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_edit)

        btn_refresh = self._keep(pya.QPushButton("Refresh", ports_tab))
        btn_refresh.clicked.connect(lambda *_a: self.refresh_ports())
        filter_row.addWidget(btn_refresh)

        btn_alias = self._keep(pya.QPushButton("Alias", ports_tab))
        btn_alias.setToolTip("Set persistent GDSII alias on the selected port's instance")
        btn_alias.clicked.connect(lambda *_a: self._on_rename_alias())
        filter_row.addWidget(btn_alias)
        ports_layout.addLayout(filter_row)

        self.ports_table = self._keep(pya.QTableWidget(ports_tab))
        self.ports_table.setColumnCount(6)
        self.ports_table.setHorizontalHeaderLabels(["Full ID", "Cell", "Layer", "Width", "Angle", "(X, Y)"])
        self.ports_table.setSelectionBehavior(pya.QAbstractItemView.SelectRows)
        self.ports_table.setSelectionMode(pya.QAbstractItemView.ExtendedSelection)
        self.ports_table.setEditTriggers(pya.QAbstractItemView.NoEditTriggers)
        self.ports_table.setMinimumHeight(170)
        hdr = self.ports_table.horizontalHeader
        if callable(hdr):
            hdr = hdr()
        if hdr is not None:
            hdr.setSectionResizeMode(0, pya.QHeaderView.Stretch)
            for col in range(1, 6):
                hdr.setSectionResizeMode(col, pya.QHeaderView.Interactive)
        for col, col_w in ((1, 125), (2, 50), (3, 56), (4, 52), (5, 118)):
            self.ports_table.setColumnWidth(col, col_w)
        self.ports_table.cellDoubleClicked.connect(self._on_table_double_click)
        self.ports_table.itemSelectionChanged.connect(lambda *_a: self._on_table_selection_changed())
        ports_layout.addWidget(self.ports_table)

        table_btn_row = self._keep(pya.QHBoxLayout())
        btn_tbl_route = self._keep(pya.QPushButton("Route Selected 2 Ports", ports_tab))
        btn_tbl_route.clicked.connect(lambda *_a: self._on_route_selected_table_ports())
        table_btn_row.addWidget(btn_tbl_route)

        btn_tbl_snap = self._keep(pya.QPushButton("Snap Row1 -> Row2", ports_tab))
        btn_tbl_snap.clicked.connect(lambda *_a: self._on_snap_selected_table_ports())
        table_btn_row.addWidget(btn_tbl_snap)
        ports_layout.addLayout(table_btn_row)

        table_align_row = self._keep(pya.QHBoxLayout())
        btn_tbl_ax = self._keep(pya.QPushButton("Align X (R1->R2)", ports_tab))
        btn_tbl_ax.setToolTip("Move Row1 instance in X only so Row1.x == Row2.x")
        btn_tbl_ax.clicked.connect(lambda *_a: self._on_align_selected_table_ports("x"))
        table_align_row.addWidget(btn_tbl_ax)

        btn_tbl_ay = self._keep(pya.QPushButton("Align Y (R1->R2)", ports_tab))
        btn_tbl_ay.setToolTip("Move Row1 instance in Y only so Row1.y == Row2.y")
        btn_tbl_ay.clicked.connect(lambda *_a: self._on_align_selected_table_ports("y"))
        table_align_row.addWidget(btn_tbl_ay)

        btn_tbl_ctr = self._keep(pya.QPushButton("Center Pair (4 Rows)", ports_tab))
        btn_tbl_ctr.setToolTip("Select 2 ports on movable cell + 2 ports on target cell to center symmetrically")
        btn_tbl_ctr.clicked.connect(lambda *_a: self._on_center_pair_table_ports())
        table_align_row.addWidget(btn_tbl_ctr)
        ports_layout.addLayout(table_align_row)
        self.inspector_tabs.addTab(ports_tab, "World Ports")

        # --- Tab 2: Routes (Clean Length Summary & ΔL Comparator) ---
        routes_tab = self._keep(pya.QWidget(self.inspector_tabs))
        routes_layout = self._keep(pya.QVBoxLayout(routes_tab))
        routes_layout.setContentsMargins(4, 4, 4, 4)
        routes_layout.setSpacing(4)

        self.routes_table = self._keep(pya.QTableWidget(routes_tab))
        self.routes_table.setColumnCount(4)
        self.routes_table.setHorizontalHeaderLabels(["ID", "Total (um)", "Rmin/Reff", "Segments"])
        self.routes_table.setSelectionBehavior(pya.QAbstractItemView.SelectRows)
        self.routes_table.setSelectionMode(pya.QAbstractItemView.ExtendedSelection)
        self.routes_table.setEditTriggers(pya.QAbstractItemView.NoEditTriggers)
        self.routes_table.setMinimumHeight(170)
        rhdr = self.routes_table.horizontalHeader
        if callable(rhdr):
            rhdr = rhdr()
        if rhdr is not None:
            for col in range(3):
                rhdr.setSectionResizeMode(col, pya.QHeaderView.Interactive)
            rhdr.setSectionResizeMode(3, pya.QHeaderView.Stretch)
        for col, col_w in ((0, 42), (1, 78), (2, 86)):
            self.routes_table.setColumnWidth(col, col_w)
        self.routes_table.cellDoubleClicked.connect(self._on_route_double_click)
        self.routes_table.itemSelectionChanged.connect(lambda *_a: self._on_route_selection_changed())
        routes_layout.addWidget(self.routes_table)

        self.route_delta_label = self._keep(
            pya.QLabel("Double-click row to zoom; Ctrl-click 2 routes to measure ΔL.", routes_tab)
        )
        self.route_delta_label.setWordWrap(True)
        self.route_delta_label.setStyleSheet(
            "QLabel { background: #182338; color: #dbe9ff; padding: 4px 6px; "
            "border: 1px solid #304970; border-radius: 3px; font-size: 11px; }"
        )
        routes_layout.addWidget(self.route_delta_label)

        routes_btn_row = self._keep(pya.QHBoxLayout())
        btn_r_ref = self._keep(pya.QPushButton("Refresh", routes_tab))
        btn_r_ref.clicked.connect(lambda *_a: self.refresh_routes())
        routes_btn_row.addWidget(btn_r_ref)

        btn_r_copy = self._keep(pya.QPushButton("Copy Lengths", routes_tab))
        btn_r_copy.setToolTip("Copy selected (or all) route lengths and segment breakdowns to clipboard")
        btn_r_copy.clicked.connect(lambda *_a: self._on_copy_routes())
        routes_btn_row.addWidget(btn_r_copy)

        btn_r_902 = self._keep(pya.QPushButton("Relayout 90/2", routes_tab))
        btn_r_902.setToolTip("Redraw RouteLength (90/2) with global collision avoidance")
        btn_r_902.clicked.connect(lambda *_a: action_refresh_length_layer())
        routes_btn_row.addWidget(btn_r_902)
        routes_layout.addLayout(routes_btn_row)
        self.inspector_tabs.addTab(routes_tab, "Routes (ΔL)")

        root.addWidget(self.inspector_tabs)

        # 5. Status / Log Line
        self.status_label = self._keep(pya.QLabel("Ready. Use PIC Tools or buttons above.", container))
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            "QLabel { background: #1e1e1e; color: #dcdcdc; padding: 6px; "
            "border: 1px solid #444; border-radius: 3px; font-size: 11px; }"
        )
        root.addWidget(self.status_label)

        scroll.setWidget(container)
        self.setWidget(scroll)

    def _reload_preset_combo(self, select_name: str | None = None) -> None:
        self._updating_preset = True
        try:
            self.preset_combo.clear()
            all_presets = get_all_presets()
            for name in all_presets.keys():
                self.preset_combo.addItem(name)
            self.preset_combo.addItem("Custom")
            if select_name:
                self.preset_combo.setCurrentText(select_name)
        finally:
            self._updating_preset = False

    def _on_preset_changed(self, preset_name: str) -> None:
        if self._updating_preset:
            return
        if preset_name in get_all_presets():
            self._apply_preset(preset_name)

    def _apply_preset(self, preset_name: str) -> None:
        ps = get_all_presets().get(preset_name)
        if ps is None:
            return
        self._updating_preset = True
        try:
            self.preset_combo.setCurrentText(preset_name)
            core_specs = [ls for ls in ps.layers if ls.role in ("core", "cladding")]
            if core_specs:
                primary_cw = float(core_specs[0].width)
                self.core_w_spin.setValue(primary_cw)
                if len(core_specs) == 1:
                    self.core_layer_edit.setText(f"{core_specs[0].layer}/{core_specs[0].datatype}")
                else:
                    parts = []
                    for idx, cs_ls in enumerate(core_specs):
                        if idx == 0 or abs(float(cs_ls.width) - primary_cw) <= 1e-6:
                            parts.append(f"{cs_ls.layer}/{cs_ls.datatype}")
                        else:
                            parts.append(f"{cs_ls.layer}/{cs_ls.datatype}={float(cs_ls.width):g}")
                    self.core_layer_edit.setText("; ".join(parts))
            else:
                self.core_w_spin.setValue(float(ps.core_width))
                if ps.core_layer is not None:
                    self.core_layer_edit.setText(f"{ps.core_layer[0]}/{ps.core_layer[1]}")

            trench_specs = [ls for ls in ps.layers if ls.role == "trench" and ls.width > 0.0]
            if not trench_specs:
                self.trench_w_spin.setValue(0.0)
                self.trench_layer_edit.setText("1/1")
            elif len(trench_specs) == 1:
                self.trench_w_spin.setValue(float(trench_specs[0].width))
                self.trench_layer_edit.setText(f"{trench_specs[0].layer}/{trench_specs[0].datatype}")
            else:
                self.trench_w_spin.setValue(float(trench_specs[0].width))
                trench_txt = "; ".join(
                    f"{ts.layer}/{ts.datatype}={float(ts.width):g}" for ts in trench_specs
                )
                self.trench_layer_edit.setText(trench_txt)

            self.radius_spin.setValue(float(ps.radius))
            self.euler_p_spin.setValue(float(ps.euler_p))
            self.min_straight_spin.setValue(float(ps.min_straight))
        finally:
            self._updating_preset = False

    def _on_save_user_preset(self) -> None:
        try:
            current = str(self.preset_combo.currentText)
            default_name = (
                current
                if (current and current != "Custom" and current not in CROSS_SECTION_PRESETS)
                else "My_Process_WG"
            )
            name = pya.InputDialog.ask_string(
                "Save Custom Cross-Section Preset",
                "Enter a preset name (saved to pic_router_presets.json):",
                default_name,
            )
            if name is None:
                return
            clean_name = str(name).strip()
            if not clean_name:
                _set_status("Preset name cannot be empty.", is_error=True)
                return
            if clean_name == "Custom" or clean_name in CROSS_SECTION_PRESETS:
                _set_status(
                    f"Cannot overwrite built-in template '{clean_name}'. Choose a different name.",
                    is_error=True,
                )
                return
            cs = self.get_settings()["cross_section"].copy()
            cs.name = clean_name
            saved_path = save_user_preset(clean_name, cs)
            self._reload_preset_combo(select_name=clean_name)
            _set_status(f"Saved custom preset '{clean_name}' to {saved_path.name}.")
        except Exception as exc:
            _set_status(f"Save preset failed: {exc}", is_error=True)
            traceback.print_exc()

    def _on_delete_user_preset(self) -> None:
        try:
            name = str(self.preset_combo.currentText)
            if name == "Custom" or name in CROSS_SECTION_PRESETS:
                _set_status(f"Built-in template '{name}' cannot be deleted.", is_error=True)
                return
            if delete_user_preset(name):
                self._reload_preset_combo(select_name=DEFAULT_PRESET_NAME)
                self._apply_preset(DEFAULT_PRESET_NAME)
                _set_status(f"Deleted custom preset '{name}'.")
            else:
                _set_status(f"Custom preset '{name}' was not found.", is_error=True)
        except Exception as exc:
            _set_status(f"Delete preset failed: {exc}", is_error=True)
            traceback.print_exc()

    def get_settings(self) -> dict[str, Any]:
        preset = str(self.preset_combo.currentText)
        core_w = float(self.core_w_spin.value)
        trench_w = float(self.trench_w_spin.value)
        core_entries = parse_layer_list_text(str(self.core_layer_edit.text)) or [(1, 0, None)]
        trench_entries = parse_layer_list_text(str(self.trench_layer_edit.text))
        core_l = (core_entries[0][0], core_entries[0][1])
        trench_l = (trench_entries[0][0], trench_entries[0][1]) if trench_entries else (1, 1)
        radius = float(self.radius_spin.value)
        euler_p = float(self.euler_p_spin.value)
        raw_btype = str(self.bend_type_combo.currentText)
        bend_type = _BEND_TYPE_LABEL_TO_CODE.get(raw_btype, raw_btype.lower())
        if bend_type not in VALID_BEND_TYPES:
            bend_type = "topic"
        min_straight = float(self.min_straight_spin.value)
        raw_mode = str(self.route_mode_combo.currentText)
        route_mode = _ROUTE_MODE_LABEL_TO_CODE.get(raw_mode, raw_mode)

        layers: list[LayerSpec] = []
        for idx, (cl_num, cl_dt, w_ovr) in enumerate(core_entries):
            w_val = float(w_ovr) if (w_ovr is not None and w_ovr > 0.0) else core_w
            if w_val > 0.0:
                # Primary core & equal-width multi-cores get role="core" (auto-tapered together);
                # secondary core layers with an explicit custom width keep fixed width via role="cladding".
                role = "core" if (idx == 0 or w_ovr is None or abs(w_val - core_w) <= 1e-6) else "cladding"
                layers.append(LayerSpec(cl_num, cl_dt, w_val, 0.0, role))

        for tl_num, tl_dt, w_ovr in trench_entries:
            w_val = float(w_ovr) if (w_ovr is not None and w_ovr > 0.0) else trench_w
            if w_val > 0.0:
                layers.append(LayerSpec(tl_num, tl_dt, w_val, 0.0, "trench"))

        if not layers:
            layers.append(LayerSpec(1, 0, max(0.05, core_w), 0.0, "core"))

        cs = CrossSection(
            name=preset,
            layers=layers,
            radius=radius,
            euler_p=euler_p,
            min_straight=min_straight,
        )
        eff_preset = _effective_preset_name(cs, check_min_straight=True)
        cs.name = eff_preset
        return {
            "preset": eff_preset,
            "core_width": core_w,
            "trench_width": trench_w,
            "core_layer": core_l,
            "trench_layer": trench_l,
            "core_layers": [(l, d) for l, d, _ in core_entries],
            "trench_layers": [(l, d) for l, d, _ in trench_entries],
            "radius": radius,
            "euler_p": euler_p,
            "bend_type": bend_type,
            "min_straight": min_straight,
            "route_mode": route_mode,
            "auto_taper": bool(self.auto_taper_cb.isChecked()),
            "allow_rotate": bool(self.allow_rotate_cb.isChecked()),
            "integer_length": bool(self.integer_length_cb.isChecked()),
            "annotate_lengths": bool(self.annotate_lengths_cb.isChecked()),
            "label_style": _LABEL_STYLE_LABEL_TO_CODE.get(str(self.label_style_combo.currentText), "card"),
            "use_eff_radius": True,
            "taper_length": float(self.taper_len_spin.value),
            "cross_section": cs,
        }

    def _on_label_style_changed(self) -> None:
        try:
            ctx = _get_active_context()
            if ctx is None:
                return
            _, _, layout, top_cell = ctx
            li = layout.find_layer(DEFAULT_LENGTH_LAYER[0], DEFAULT_LENGTH_LAYER[1])
            if li is not None and li >= 0 and top_cell.shapes(li).size() > 0:
                action_refresh_length_layer()
        except Exception:
            pass

    def refresh_routes(self) -> None:
        try:
            ctx = _get_active_context()
            if ctx is None:
                self._routes_cache = []
                self._route_selection_order.clear()
                self.routes_table.setRowCount(0)
                self.route_delta_label.setText("No active layout.")
                return
            _, _, layout, top_cell = ctx
            self._routes_cache = _collect_euler_waveguide_routes(layout, top_cell)
            self._route_selection_order.clear()
            self.routes_table.setUpdatesEnabled(False)
            self.routes_table.blockSignals(True)
            try:
                self.routes_table.setRowCount(len(self._routes_cache))
                for row, rec in enumerate(self._routes_cache):
                    vals = [
                        f"#{rec['index']}",
                        f"{rec['total_length']:.3f}",
                        str(rec["radius_str"]),
                        str(rec["segments_str"]),
                    ]
                    tip = f"#{rec['index']} Total: {rec['total_length']:.3f} um | {rec['segments_str']}"
                    for col, val in enumerate(vals):
                        item = pya.QTableWidgetItem(val)
                        item.setToolTip(tip)
                        self.routes_table.setItem(row, col, item)
            finally:
                self.routes_table.blockSignals(False)
                self.routes_table.setUpdatesEnabled(True)
            n = len(self._routes_cache)
            self.inspector_tabs.setTabText(1, f"Routes ({n})")
            if n == 0:
                self.route_delta_label.setText("No EulerWaveguide routes yet.")
            else:
                self.route_delta_label.setText(
                    f"{n} route(s). Double-click to zoom; Ctrl-click 2 rows to compare ΔL."
                )
        except Exception as exc:
            self.set_status(f"Refresh routes failed: {exc}", is_error=True)
            traceback.print_exc()

    def _on_route_selection_changed(self) -> None:
        current_rows = sorted({item.row() for item in self.routes_table.selectedItems()})
        current_set = set(current_rows)
        self._route_selection_order = [r for r in self._route_selection_order if r in current_set]
        for r in current_rows:
            if r not in self._route_selection_order:
                self._route_selection_order.append(r)
        sel = [self._routes_cache[r] for r in self._route_selection_order if 0 <= r < len(self._routes_cache)]
        if len(sel) >= 2:
            a, b = sel[0], sel[1]
            dl = b["total_length"] - a["total_length"]
            self.route_delta_label.setText(
                f"ΔL (#{b['index']} - #{a['index']}) = {dl:+.3f} um   (|ΔL| = {abs(dl):.3f} um)\n"
                f"#{a['index']}: {a['total_length']:.3f} um  |  #{b['index']}: {b['total_length']:.3f} um"
            )
        elif len(sel) == 1:
            a = sel[0]
            self.route_delta_label.setText(
                f"#{a['index']} Total: {a['total_length']:.3f} um (R={a['radius_str']})\n{a['segments_str']}"
            )
        else:
            n = len(self._routes_cache)
            self.route_delta_label.setText(
                f"{n} route(s). Double-click to zoom; Ctrl-click 2 rows to compare ΔL."
            )

    def _on_route_double_click(self, row: int, col: int) -> None:
        try:
            if not (0 <= row < len(self._routes_cache)):
                return
            ctx = _get_active_context()
            if ctx is None:
                return
            _, view, _, _ = ctx
            rec = self._routes_cache[row]
            bb = rec["bbox"]
            pad = max(25.0, 0.15 * max(float(bb.width()), float(bb.height())))
            view.zoom_box(
                pya.DBox(
                    float(bb.left) - pad,
                    float(bb.bottom) - pad,
                    float(bb.right) + pad,
                    float(bb.top) + pad,
                )
            )
            _clear_global_highlights()
            bag = _MarkerBag(view)
            bag.add_polyline(rec["result"].centerline, _COLOR_HIGHLIGHT, width=max(1.5, float(rec["max_width"]) * 0.2))
            _GUI_STATE["highlight_markers"].extend(bag._markers)
            self.set_status(
                f"Zoomed to Route #{rec['index']}: Total={rec['total_length']:.3f} um "
                f"(R={rec['radius_str']}) | {rec['segments_str']}"
            )
        except Exception as exc:
            self.set_status(f"Route zoom failed: {exc}", is_error=True)
            traceback.print_exc()

    def _on_copy_routes(self) -> None:
        try:
            sel = [self._routes_cache[r] for r in self._route_selection_order if 0 <= r < len(self._routes_cache)]
            rows = sel if sel else self._routes_cache
            if not rows:
                self.set_status("No routes to copy.", is_error=True)
                return
            lines = ["ID\tTotal_um\tRmin_Reff\tSegments"]
            for rec in rows:
                lines.append(
                    f"#{rec['index']}\t{rec['total_length']:.3f}\t{rec['radius_str']}\t{rec['segments_str']}"
                )
            if len(rows) == 2:
                dl = rows[1]["total_length"] - rows[0]["total_length"]
                lines.append(f"DeltaL(#{rows[1]['index']}-#{rows[0]['index']})\t{dl:+.3f}\t\t")
            txt = "\n".join(lines)
            cb = pya.QApplication.clipboard() if hasattr(pya, "QApplication") else None
            if cb is not None:
                cb.setText(txt)
            self.set_status(f"Copied {len(rows)} route length summary row(s) to clipboard.")
        except Exception as exc:
            self.set_status(f"Copy routes failed: {exc}", is_error=True)
            traceback.print_exc()

    def set_status(self, msg: str, is_error: bool = False) -> None:
        txt = str(msg)
        err = bool(is_error)
        if txt != self._last_status_text:
            self.status_label.setText(txt)
            self._last_status_text = txt
        if err != self._last_status_is_error:
            self._last_status_is_error = err
            if err:
                self.status_label.setStyleSheet(
                    "QLabel { background: #3b1818; color: #ffd0d0; padding: 6px; "
                    "border: 1px solid #a03030; border-radius: 3px; font-size: 11px; }"
                )
            else:
                self.status_label.setStyleSheet(
                    "QLabel { background: #182b1e; color: #d8f5de; padding: 6px; "
                    "border: 1px solid #357048; border-radius: 3px; font-size: 11px; }"
                )

    def refresh_ports(self) -> None:
        try:
            ctx = _get_active_context()
            if ctx is None:
                self._world_ports = []
                self._apply_filter("")
                return
            _, _, layout, top_cell = ctx
            self._world_ports = collect_world_ports(layout, top_cell, pin_layer=DEFAULT_PIN_LAYER)
            self._apply_filter(str(self.filter_edit.text))
        except Exception as exc:
            self.set_status(f"Refresh ports failed: {exc}", is_error=True)
            traceback.print_exc()

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        if not needle:
            self._filtered_ports = list(self._world_ports)
        else:
            out = []
            for p in self._world_ports:
                layer_str = f"{p.core_layer[0]}/{p.core_layer[1]}"
                hay = f"{p.full_name} {p.cell_name} {layer_str}".lower()
                if needle in hay:
                    out.append(p)
            self._filtered_ports = out

        self._selection_order.clear()
        self.ports_table.setUpdatesEnabled(False)
        self.ports_table.blockSignals(True)
        try:
            self.ports_table.setRowCount(len(self._filtered_ports))
            for row, p in enumerate(self._filtered_ports):
                vals = [
                    p.full_name,
                    p.cell_name,
                    f"{p.core_layer[0]}/{p.core_layer[1]}",
                    f"{p.width:.3f}",
                    f"{p.angle_deg:.1f}",
                    f"({p.x:.2f}, {p.y:.2f})",
                ]
                for col, val in enumerate(vals):
                    self.ports_table.setItem(row, col, pya.QTableWidgetItem(val))
        finally:
            self.ports_table.blockSignals(False)
            self.ports_table.setUpdatesEnabled(True)

    def _on_table_selection_changed(self) -> None:
        current_rows = sorted({item.row() for item in self.ports_table.selectedItems()})
        current_set = set(current_rows)
        self._selection_order = [r for r in self._selection_order if r in current_set]
        for r in current_rows:
            if r not in self._selection_order:
                self._selection_order.append(r)

    def _selected_table_ports(self) -> list[Port]:
        ports = []
        for r in self._selection_order:
            if 0 <= r < len(self._filtered_ports):
                ports.append(self._filtered_ports[r])
        return ports

    def _on_table_double_click(self, row: int, col: int) -> None:
        try:
            if not (0 <= row < len(self._filtered_ports)):
                return
            ctx = _get_active_context()
            if ctx is None:
                return
            _, view, _, _ = ctx
            port = self._filtered_ports[row]
            half_span = 25.0
            view.zoom_box(
                pya.DBox(
                    port.x - half_span,
                    port.y - half_span,
                    port.x + half_span,
                    port.y + half_span,
                )
            )
            _clear_global_highlights()
            bag = _MarkerBag(view)
            bag.add_port(port, _COLOR_HIGHLIGHT, arrow_len=4.0, show_label=True)
            _GUI_STATE["highlight_markers"].extend(bag._markers)
            self.set_status(
                f"Zoomed to {port.full_name} at ({port.x:.3f}, {port.y:.3f}), "
                f"angle={port.angle_deg:.2f} deg, w={port.width:.3f} um"
            )
        except Exception as exc:
            self.set_status(f"Zoom failed: {exc}", is_error=True)
            traceback.print_exc()

    def _on_rename_alias(self) -> None:
        try:
            ports = self._selected_table_ports()
            if not ports:
                self.set_status("Select a port row first to rename its instance alias.", is_error=True)
                return
            port = ports[0]
            if port.inst_obj is None:
                self.set_status("Top-level ports do not belong to a child instance.", is_error=True)
                return
            ctx = _get_active_context()
            if ctx is None:
                return
            _, view, _, _ = ctx
            new_alias = pya.InputDialog.ask_string(
                "Rename Instance Alias",
                f"Persistent GDSII alias for instance '{port.inst_id}' (cell '{port.cell_name}'):",
                port.inst_id,
            )
            if new_alias is None:
                return
            new_alias = new_alias.strip()
            if not new_alias:
                return
            with _view_transaction(view, f"PIC Rename Alias -> {new_alias}"):
                set_instance_alias(port.inst_obj, new_alias)
            self.refresh_ports()
            self.set_status(f"Renamed instance alias to '{new_alias}'.")
        except Exception as exc:
            self.set_status(f"Rename alias failed: {exc}", is_error=True)
            traceback.print_exc()

    def _on_route_selected_table_ports(self) -> None:
        try:
            ports = self._selected_table_ports()
            if len(ports) != 2:
                self.set_status("Select exactly 2 rows in the World Ports table to route.", is_error=True)
                return
            ctx = _get_active_context()
            if ctx is None:
                return
            _, view, layout, top_cell = ctx
            live_ports = _resolve_live_ports(layout, top_cell, ports)
            _execute_port_route(view, layout, top_cell, live_ports[0], live_ports[1], tx_title="PIC Euler Route (Table)")
        except Exception as exc:
            self.set_status(f"Table route failed: {exc}", is_error=True)
            traceback.print_exc()

    def _on_snap_selected_table_ports(self) -> None:
        try:
            ports = self._selected_table_ports()
            if len(ports) != 2:
                self.set_status("Select exactly 2 rows in the World Ports table (Row1 movable -> Row2 target).", is_error=True)
                return
            ctx = _get_active_context()
            if ctx is None:
                return
            _, view, layout, top_cell = ctx
            live_ports = _resolve_live_ports(layout, top_cell, ports)
            _execute_port_snap(view, layout, live_ports[0], live_ports[1], tx_title="PIC Port Snap (Table)")
        except Exception as exc:
            self.set_status(f"Table snap failed: {exc}", is_error=True)
            traceback.print_exc()

    def _on_align_selected_table_ports(self, axis: str) -> None:
        try:
            ax = str(axis).strip().lower()
            ports = self._selected_table_ports()
            if len(ports) != 2:
                self.set_status(
                    f"Select exactly 2 rows in the World Ports table (Row1 movable -> Row2 target) to align {ax.upper()}.",
                    is_error=True,
                )
                return
            ctx = _get_active_context()
            if ctx is None:
                return
            _, view, layout, top_cell = ctx
            live_ports = _resolve_live_ports(layout, top_cell, ports)
            _execute_port_align_axis(
                view,
                layout,
                live_ports[0],
                live_ports[1],
                axis=ax,
                tx_title=f"PIC Align {ax.upper()} (Table)",
            )
        except Exception as exc:
            self.set_status(f"Table align {axis.upper()} failed: {exc}", is_error=True)
            traceback.print_exc()

    def _on_center_pair_table_ports(self) -> None:
        try:
            ports = self._selected_table_ports()
            if len(ports) != 4:
                self.set_status(
                    "Select exactly 4 rows (2 ports on movable cell + 2 ports on target cell) to center pair.",
                    is_error=True,
                )
                return
            ctx = _get_active_context()
            if ctx is None:
                return
            _, view, layout, top_cell = ctx
            live = _resolve_live_ports(layout, top_cell, ports)
            # Group cleanly: prefer [0,1] -> [2,3] when [0,1] share the movable instance,
            # otherwise group by first clicked movable instance vs remaining 2 ports.
            if live[0].inst_obj is not None and _same_inst_ref(live[0].inst_obj, live[1].inst_obj):
                src_pair = (live[0], live[1])
                tgt_pair = (live[2], live[3])
            else:
                first_mov = next((p for p in live if p.inst_obj is not None), None)
                if first_mov is None:
                    self.set_status("At least 2 selected ports must belong to a movable cell instance.", is_error=True)
                    return
                src_list = [p for p in live if _same_inst_ref(p.inst_obj, first_mov.inst_obj)]
                tgt_list = [p for p in live if not _same_inst_ref(p.inst_obj, first_mov.inst_obj)]
                if len(src_list) != 2 or len(tgt_list) != 2:
                    self.set_status(
                        "Select 2 ports on ONE movable cell and 2 target ports on other cell(s).",
                        is_error=True,
                    )
                    return
                src_pair = (src_list[0], src_list[1])
                tgt_pair = (tgt_list[0], tgt_list[1])

            _execute_center_pair_ports(
                view,
                layout,
                src_pair[0],
                src_pair[1],
                tgt_pair[0],
                tgt_pair[1],
                axis="auto",
                tx_title="PIC Center Pair Ports (Table)",
            )
        except Exception as exc:
            self.set_status(f"Table center pair failed: {exc}", is_error=True)
            traceback.print_exc()


# ---------------------------------------------------------------------------
# Menu & Plugin Bootstrap
# ---------------------------------------------------------------------------

def _make_action(title: str, callback: Any, shortcut: str = "", tooltip: str = "") -> Any:
    action = pya.Action()
    action.title = title
    if shortcut:
        action.shortcut = shortcut
    if tooltip:
        action.tool_tip = tooltip
    action.on_triggered += lambda *_a, _cb=callback: _cb()
    _GUI_STATE["actions"].append(action)
    return action


def _register_menu(mw: Any) -> None:
    menu = mw.menu()
    menu_id = "pic_tools_menu"
    if not menu.is_valid(menu_id):
        menu.insert_menu("end", menu_id, "PIC Tools")

    def _add_item(item_id: str, title: str, cb: Any, shortcut: str = "", tip: str = "") -> None:
        full_path = f"{menu_id}.{item_id}"
        if menu.is_valid(full_path):
            return
        act = _make_action(title, cb, shortcut=shortcut, tooltip=tip)
        menu.insert_item(f"{menu_id}.end", item_id, act)

    def _add_sep(sep_id: str) -> None:
        full_path = f"{menu_id}.{sep_id}"
        if not menu.is_valid(full_path):
            menu.insert_separator(f"{menu_id}.end", sep_id)

    _add_item(
        "toggle_panel",
        "Toggle PIC Router Panel",
        action_toggle_panel,
        shortcut=SHORTCUTS["toggle_panel"],
        tip="Show or hide the PIC Snap & Router dock panel",
    )
    _add_sep("sep1")
    _add_item(
        "snap_mode",
        "Snap 2 Ports (Interactive)",
        lambda: action_activate_mode(MODE_SNAP, "Snap 2 Ports"),
        shortcut=SHORTCUTS["snap_mode"],
        tip="Click source port on movable cell, then target port",
    )
    _add_item(
        "route_mode",
        "Route 2 Ports with TOPIC/Euler Bend (Interactive, Shift=H/V)",
        lambda: action_activate_mode(MODE_ROUTE, "Route 2 Ports"),
        shortcut=SHORTCUTS["route_mode"],
        tip="Click start port, optional waypoints (hold Shift for H/V ortho), then end port",
    )
    _add_item(
        "free_draw_mode",
        "Free Draw Waveguide (Blank Canvas)",
        lambda: action_activate_mode(MODE_FREE_DRAW, "Free Draw Waveguide"),
        shortcut=SHORTCUTS["free_draw_mode"],
        tip="Draw multi-layer TOPIC/Euler waveguides freely on blank canvas (zero ports required; Enter finishes with o1/o2 PinRec ports)",
    )
    _add_item(
        "align_x_mode",
        "Align X of 2 Ports (Vertical Straight Prep)",
        lambda: action_activate_mode(MODE_ALIGN_X, "Align X (2 Ports)"),
        shortcut=SHORTCUTS["align_x_mode"],
        tip="Move source cell in X only so source port X matches target port X",
    )
    _add_item(
        "align_y_mode",
        "Align Y of 2 Ports (Horizontal Straight Prep)",
        lambda: action_activate_mode(MODE_ALIGN_Y, "Align Y (2 Ports)"),
        shortcut=SHORTCUTS["align_y_mode"],
        tip="Move source cell in Y only so source port Y matches target port Y",
    )
    _add_item(
        "center_pair_mode",
        "Center Port Pair Between Target Pair (2->2)",
        lambda: action_activate_mode(MODE_CENTER_PAIR, "Center Pair Ports"),
        shortcut=SHORTCUTS["center_pair_mode"],
        tip="Center 2 ports of a movable cell symmetrically between 2 target ports",
    )
    _add_item(
        "add_port_mode",
        "Add Port by Clicking Edge (Interactive)",
        lambda: action_activate_mode(MODE_ADD_PORT, "Add Port"),
        shortcut=SHORTCUTS["add_port_mode"],
        tip="Click near a waveguide end edge to detect and stamp PinRec",
    )
    _add_sep("sep2")
    _add_item(
        "quick_snap",
        "Quick Snap Selected Cell",
        action_quick_snap_selected,
        shortcut=SHORTCUTS["quick_snap"],
        tip="Snap selected instance to nearest compatible neighbor port",
    )
    _add_item(
        "auto_detect_ports",
        "Auto-Detect Ports on Selected Cell",
        action_auto_detect_ports,
        shortcut=SHORTCUTS["auto_detect_ports"],
        tip="Automatically detect and stamp PinRec ports on selected cell(s)",
    )
    _add_item(
        "path_to_waveguide",
        "Path -> Euler Waveguide PCell",
        action_path_to_euler_waveguide,
        shortcut=SHORTCUTS["path_to_waveguide"],
        tip="Convert selected KLayout Path(s) into multi-layer EulerWaveguide PCells",
    )
    _add_item(
        "revert_waveguide",
        "Revert Euler Waveguide -> Path",
        action_revert_waveguide_to_path,
        shortcut=SHORTCUTS["revert_waveguide"],
        tip="Convert selected EulerWaveguide PCell(s) back to raw DPath",
    )
    _add_item(
        "create_topic_bend",
        "Create TOPIC Bend (Constant Width, R_eff & p)...",
        action_create_topic_bend_dialog,
        tip="Generate a constant-width multi-layer TOPIC (C3 Hermite) bend PCell with flush PinRec ports",
    )
    _add_sep("sep_mask")
    _add_item(
        "add_trench",
        "Add Trench to Existing Structure (Multi-Layer)...",
        action_add_trench_dialog,
        tip="Generate single- or multi-layer trenches around existing core-only PIC structures",
    )
    _add_item(
        "cd_bias",
        "Linewidth Compensation / CD Bias (ΔW nm)...",
        action_cd_bias_dialog,
        tip="Apply length-preserving CD linewidth bias (+120 nm, +70 nm, etc.) across straights, bends & devices",
    )
    _add_sep("sep3")
    _add_item(
        "refresh_lengths",
        "Refresh RouteLength Annotations (90/2)",
        action_refresh_length_layer,
        tip="Regenerate dark RouteLength (90/2) segment and total length labels for all EulerWaveguide PCells",
    )
    _add_item(
        "delete_lengths",
        "Delete RouteLength Layer (90/2)",
        action_delete_length_layer,
        tip="Delete the RouteLength (90/2) annotation layer from active layout",
    )
    _add_item(
        "refresh_ports",
        "Refresh World Ports Table",
        _refresh_ports_table,
        tip="Rescan all PinRec ports in active top cell",
    )


def init_gui() -> bool:
    """Initialize PIC_Router GUI (menu, dock panel, canvas PluginFactories).

    Returns False cleanly in headless mode (when main_window() is None).
    Idempotent across repeated calls.
    """
    register_pcell_library()
    if pya is None or not hasattr(pya, "Application"):
        return False
    app = pya.Application.instance()
    if app is None:
        return False
    mw = app.main_window()
    if mw is None:
        return False

    if _GUI_STATE["initialized"]:
        return True

    # 1. Register interactive canvas PluginFactories
    if not _GUI_STATE["factories"]:
        _GUI_STATE["factories"] = [
            PICPortSnapFactory(),
            PICPortRouteFactory(),
            PICAddPortFactory(),
            PICPortAlignXFactory(),
            PICPortAlignYFactory(),
            PICCenterPairFactory(),
            PICFreeDrawWaveguideFactory(),
        ]

    # 2. Create and dock right-hand side panel
    dock = _GUI_STATE.get("dock")
    if dock is None:
        dock = PICRouterDock(mw)
        mw.addDockWidget(pya.Qt.RightDockWidgetArea, dock)
        _GUI_STATE["dock"] = dock
        try:
            mw.on_current_view_changed += lambda *_a: (dock.refresh_ports(), dock.refresh_routes())
        except Exception:
            pass

    # 3. Register top-level 'PIC Tools' menu with safe shortcuts
    _register_menu(mw)
    _GUI_STATE["initialized"] = True
    return True
