"""Parametric KLayout PCells for PIC_Router (EulerWaveguide, EulerBend, and TOPICBend).

100% pure Python 3.10+ + pya / klayout.db (zero numpy/scipy dependencies).
Safe to import and register in both KLayout GUI and headless ``klayout -b`` mode.
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable, Sequence

from .config import (
    CROSS_SECTION_PRESETS,
    DEFAULT_ERROR_LAYER,
    DEFAULT_PIN_LAYER,
    DEFAULT_PRESET_NAME,
    CrossSection,
    LayerSpec,
    get_kdb,
)
from .euler import (
    VALID_BEND_TYPES,
    VALID_SBEND_CURVES,
    build_multilayer_waveguide_regions,
    euler_bend_metrics,
    euler_bend_points,
    topic_bend_metrics,
    topic_bend_points,
)
from .ports import add_port_to_cell
from .router import draw_route_to_cell, route_from_guide_points, route_sbend_from_guide_points

kdb = get_kdb()

LIBRARY_NAME = "PIC_Router"
LIBRARY_DESCRIPTION = "Generic Photonic Multi-Layer TOPIC / Euler Waveguide & Bend PCells"

_DEFAULT_PRESET = DEFAULT_PRESET_NAME
_DEFAULT_CORE_WIDTH = 0.80
_DEFAULT_TRENCH_WIDTH = 10.0
_DEFAULT_RADIUS = 50.0
_DEFAULT_EULER_P = 0.50
_DEFAULT_MIN_STRAIGHT = 5.0
_DEFAULT_CORE_LAYER = (1, 0)
_DEFAULT_TRENCH_LAYER = (1, 1)

# Keep a strong module-level reference so KLayout never loses the Python library object.
_REGISTERED_LIBRARY = None


def _preset_choices() -> list[tuple[str, str]]:
    choices = [(name, name) for name in CROSS_SECTION_PRESETS.keys()]
    choices.append(("Custom", "Custom"))
    return choices


def _layer_tuple(layer_info: Any) -> tuple[int, int] | None:
    if layer_info is None:
        return None
    layer = getattr(layer_info, "layer", -1)
    datatype = getattr(layer_info, "datatype", 0)
    if layer is None or int(layer) < 0:
        return None
    return (int(layer), int(datatype))


def _parse_extra_layers(extra_layers_json: str) -> tuple[list[LayerSpec], str]:
    """Parse optional extra_layers_json into a list of LayerSpec objects."""
    raw = (extra_layers_json or "").strip()
    if not raw:
        return [], ""
    try:
        data = json.loads(raw)
    except Exception as exc:
        return [], f"Invalid extra_layers_json: {exc}"
    if not isinstance(data, list):
        return [], "extra_layers_json must be a JSON array"

    specs: list[LayerSpec] = []
    for idx, entry in enumerate(data):
        if isinstance(entry, dict):
            try:
                layer = int(entry["layer"])
                dt = int(entry.get("datatype", entry.get("dt", 0)))
                width = float(entry["width"])
                offset = float(entry.get("offset", 0.0))
                role = str(entry.get("role", "cladding"))
            except Exception as exc:
                return [], f"Invalid extra_layers_json[{idx}]: {exc}"
        elif isinstance(entry, (list, tuple)) and len(entry) >= 3:
            try:
                layer = int(entry[0])
                dt = int(entry[1])
                width = float(entry[2])
                offset = float(entry[3]) if len(entry) >= 4 else 0.0
                role = str(entry[4]) if len(entry) >= 5 else "cladding"
            except Exception as exc:
                return [], f"Invalid extra_layers_json[{idx}]: {exc}"
        else:
            return [], f"Invalid extra_layers_json[{idx}]: expected [layer, dt, width, offset]"
        if width > 0.0 and layer >= 0:
            specs.append(
                LayerSpec(
                    layer=layer,
                    datatype=dt,
                    width=width,
                    offset=offset,
                    role=role,
                )
            )
    return specs, ""


def _build_cross_section_from_params(
    preset: str,
    core_layer_info: Any,
    core_width: float,
    trench_layer_info: Any,
    trench_width: float,
    radius: float,
    euler_p: float,
    min_straight: float,
    extra_layers_json: str = "",
) -> tuple[CrossSection, str]:
    layers: list[LayerSpec] = []
    cl = _layer_tuple(core_layer_info) or _DEFAULT_CORE_LAYER
    layers.append(
        LayerSpec(
            layer=cl[0],
            datatype=cl[1],
            width=max(0.01, float(core_width)),
            offset=0.0,
            role="core",
        )
    )
    tl = _layer_tuple(trench_layer_info)
    if float(trench_width) > 0.0 and tl is not None:
        layers.append(
            LayerSpec(
                layer=tl[0],
                datatype=tl[1],
                width=float(trench_width),
                offset=0.0,
                role="trench",
            )
        )
    extra_specs, extra_err = _parse_extra_layers(extra_layers_json)
    layers.extend(extra_specs)
    cs = CrossSection(
        name=preset or "Custom",
        layers=layers,
        radius=max(0.5, float(radius)),
        euler_p=max(0.0, min(1.0, float(euler_p))),
        min_straight=max(0.0, float(min_straight)),
    )
    return cs, extra_err


def _extra_layers_to_json(cross_section: CrossSection) -> str:
    extras = []
    seen_primary_core = False
    seen_primary_trench = False
    for spec in cross_section.layers:
        if spec.role == "core" and not seen_primary_core:
            seen_primary_core = True
            continue
        if spec.role == "trench" and not seen_primary_trench:
            seen_primary_trench = True
            continue
        extras.append([spec.layer, spec.datatype, float(spec.width), float(spec.offset), spec.role])
    if not extras:
        return ""
    return json.dumps(extras)


def _effective_preset_name(cross_section: CrossSection, check_min_straight: bool = True) -> str:
    """Return cross_section.name if it matches a built-in preset exactly, else 'Custom'."""
    name = cross_section.name
    if not name or name not in CROSS_SECTION_PRESETS:
        return "Custom"
    ps = CROSS_SECTION_PRESETS[name]
    if len(cross_section.layers) != len(ps.layers):
        return "Custom"
    for cs_ls, ps_ls in zip(cross_section.layers, ps.layers):
        if (
            cs_ls.layer != ps_ls.layer
            or cs_ls.datatype != ps_ls.datatype
            or cs_ls.role != ps_ls.role
            or abs(float(cs_ls.width) - float(ps_ls.width)) > 1e-9
            or abs(float(cs_ls.offset) - float(ps_ls.offset)) > 1e-9
        ):
            return "Custom"
    if abs(float(cross_section.radius) - float(ps.radius)) > 1e-9:
        return "Custom"
    if abs(float(cross_section.euler_p) - float(ps.euler_p)) > 1e-9:
        return "Custom"
    if check_min_straight and abs(float(cross_section.min_straight) - float(ps.min_straight)) > 1e-9:
        return "Custom"
    return name


def _sync_preset_on_coerce(pcell: Any, has_min_straight: bool = True) -> None:
    """Sensibly sync preset layers/defaults without clobbering explicit user tweaks."""
    preset = getattr(pcell, "preset", "Custom")
    if not preset or preset == "Custom" or preset not in CROSS_SECTION_PRESETS:
        return

    ps = CROSS_SECTION_PRESETS[preset]
    cl = _layer_tuple(pcell.core_layer)
    tl = _layer_tuple(pcell.trench_layer)
    extra_str = str(getattr(pcell, "extra_layers_json", "") or "").strip()

    if preset == _DEFAULT_PRESET:
        # If caller explicitly passed a different layer while leaving preset at default,
        # treat it as Custom instead of overwriting their layer choice.
        if cl is not None and cl != _DEFAULT_CORE_LAYER:
            pcell.preset = "Custom"
            return
        if float(pcell.trench_width) > 0.0 and tl is not None and tl != _DEFAULT_TRENCH_LAYER:
            pcell.preset = "Custom"
            return
        if extra_str:
            pcell.preset = "Custom"
            return
        return

    # Non-default preset chosen.
    # Determine whether this looks like a fresh preset-only instantiation (layers or all
    # numeric fields still at PCell defaults) vs an already-populated preset that the user
    # tweaked manually.
    ps_extra_json = _extra_layers_to_json(ps)
    layers_need_sync = (
        (cl == _DEFAULT_CORE_LAYER and ps.core_layer != _DEFAULT_CORE_LAYER)
        or (bool(ps_extra_json) and not extra_str)
    )
    all_numeric_default = (
        abs(float(pcell.core_width) - _DEFAULT_CORE_WIDTH) < 1e-9
        and abs(float(pcell.trench_width) - _DEFAULT_TRENCH_WIDTH) < 1e-9
        and abs(float(pcell.radius) - _DEFAULT_RADIUS) < 1e-9
        and (not has_min_straight or abs(float(pcell.min_straight) - _DEFAULT_MIN_STRAIGHT) < 1e-9)
    )

    if not layers_need_sync and not all_numeric_default:
        # Caller already provided explicit layers/tweaks; never clobber values that happen
        # to coincide with _DEFAULT_*.
        return

    if ps.core_layer is not None and cl == _DEFAULT_CORE_LAYER and ps.core_layer != _DEFAULT_CORE_LAYER:
        pcell.core_layer = kdb.LayerInfo(ps.core_layer[0], ps.core_layer[1])

    if ps.trench_layer is not None:
        if tl == _DEFAULT_TRENCH_LAYER and ps.trench_layer != _DEFAULT_TRENCH_LAYER:
            pcell.trench_layer = kdb.LayerInfo(ps.trench_layer[0], ps.trench_layer[1])
        if (
            ps.trench_width is not None
            and abs(float(pcell.trench_width) - _DEFAULT_TRENCH_WIDTH) < 1e-9
            and abs(ps.trench_width - _DEFAULT_TRENCH_WIDTH) > 1e-9
        ):
            pcell.trench_width = float(ps.trench_width)
    else:
        if abs(float(pcell.trench_width) - _DEFAULT_TRENCH_WIDTH) < 1e-9:
            pcell.trench_width = 0.0

    if ps_extra_json and not extra_str:
        pcell.extra_layers_json = ps_extra_json

    if (
        abs(float(pcell.core_width) - _DEFAULT_CORE_WIDTH) < 1e-9
        and abs(ps.core_width - _DEFAULT_CORE_WIDTH) > 1e-9
    ):
        pcell.core_width = float(ps.core_width)

    if (
        abs(float(pcell.radius) - _DEFAULT_RADIUS) < 1e-9
        and abs(ps.radius - _DEFAULT_RADIUS) > 1e-9
    ):
        pcell.radius = float(ps.radius)

    if has_min_straight:
        if (
            abs(float(pcell.min_straight) - _DEFAULT_MIN_STRAIGHT) < 1e-9
            and abs(ps.min_straight - _DEFAULT_MIN_STRAIGHT) > 1e-9
        ):
            pcell.min_straight = float(ps.min_straight)


def _apply_preset_callback(pcell: Any, param_name: str, has_min_straight: bool = True) -> None:
    """Apply preset defaults live in KLayout's PCell Properties dialog."""
    if param_name == "preset":
        preset_val = pcell.preset.value
        if preset_val not in CROSS_SECTION_PRESETS:
            return
        ps = CROSS_SECTION_PRESETS[preset_val]
        pcell.core_width.value = float(ps.core_width)
        if ps.core_layer is not None:
            pcell.core_layer.value = kdb.LayerInfo(ps.core_layer[0], ps.core_layer[1])
        if ps.trench_layer is not None and ps.trench_width is not None:
            pcell.trench_layer.value = kdb.LayerInfo(ps.trench_layer[0], ps.trench_layer[1])
            pcell.trench_width.value = float(ps.trench_width)
        else:
            pcell.trench_width.value = 0.0
        if hasattr(pcell, "extra_layers_json"):
            pcell.extra_layers_json.value = _extra_layers_to_json(ps)
        pcell.radius.value = float(ps.radius)
        pcell.euler_p.value = float(ps.euler_p)
        if has_min_straight:
            pcell.min_straight.value = float(ps.min_straight)
        return

    # If user edits any geometry/layer parameter away from the active preset, switch to Custom
    # so subsequent coerce never fights their explicit override.
    watched = {
        "core_width",
        "trench_width",
        "radius",
        "euler_p",
        "core_layer",
        "trench_layer",
        "extra_layers_json",
    }
    if has_min_straight:
        watched.add("min_straight")
    if param_name in watched and pcell.preset.value in CROSS_SECTION_PRESETS:
        pcell.preset.value = "Custom"


def _extract_guide_points(shape_val: Any, dbu: float) -> list[tuple[float, float]]:
    """Extract (x, y) micron points from DPath / Path / DPolygon / Polygon / DEdge / Edge."""
    if shape_val is None:
        return []
    if isinstance(shape_val, kdb.DPath):
        return [(float(p.x), float(p.y)) for p in shape_val.each_point()]
    if isinstance(shape_val, kdb.Path):
        return [(float(p.x) * dbu, float(p.y) * dbu) for p in shape_val.each_point()]
    if isinstance(shape_val, kdb.DPolygon):
        return [(float(p.x), float(p.y)) for p in shape_val.each_point_hull()]
    if isinstance(shape_val, kdb.Polygon):
        return [(float(p.x) * dbu, float(p.y) * dbu) for p in shape_val.each_point_hull()]
    if isinstance(shape_val, kdb.DEdge):
        return [
            (float(shape_val.p1.x), float(shape_val.p1.y)),
            (float(shape_val.p2.x), float(shape_val.p2.y)),
        ]
    if isinstance(shape_val, kdb.Edge):
        return [
            (float(shape_val.p1.x) * dbu, float(shape_val.p1.y) * dbu),
            (float(shape_val.p2.x) * dbu, float(shape_val.p2.y) * dbu),
        ]
    # Fallback duck typing for any shape-like object with each_point
    if hasattr(shape_val, "each_point"):
        pts = list(shape_val.each_point())
        if pts and isinstance(pts[0], kdb.Point):
            return [(float(p.x) * dbu, float(p.y) * dbu) for p in pts]
        return [(float(p.x), float(p.y)) for p in pts]
    return []


def _draw_error_indicator(layout: Any, cell: Any, points: Sequence[tuple[float, float]], msg: str) -> None:
    err_idx = layout.layer(DEFAULT_ERROR_LAYER[0], DEFAULT_ERROR_LAYER[1])
    if len(points) >= 2:
        dpts = [kdb.DPoint(float(x), float(y)) for x, y in points]
        cell.shapes(err_idx).insert(kdb.DPath(dpts, 0.5))
        anchor = dpts[0]
    elif len(points) == 1:
        anchor = kdb.DPoint(float(points[0][0]), float(points[0][1]))
        cell.shapes(err_idx).insert(kdb.DBox(anchor.x - 1.0, anchor.y - 1.0, anchor.x + 1.0, anchor.y + 1.0))
    else:
        anchor = kdb.DPoint(0.0, 0.0)
        cell.shapes(err_idx).insert(kdb.DBox(-1.0, -1.0, 1.0, 1.0))
    cell.shapes(err_idx).insert(kdb.DText(str(msg), kdb.DTrans(anchor.x, anchor.y)))


class EulerWaveguide(kdb.PCellDeclarationHelper):
    """Multi-layer Euler-bend waveguide PCell with live-editable guiding DPath."""

    def __init__(self) -> None:
        super().__init__()
        default_path = kdb.DPath(
            [
                kdb.DPoint(0.0, 0.0),
                kdb.DPoint(100.0, 0.0),
                kdb.DPoint(100.0, 100.0),
            ],
            _DEFAULT_CORE_WIDTH,
        )
        self.param("path", self.TypeShape, "Guiding Path", default=default_path)
        self.param(
            "preset",
            self.TypeString,
            "Cross-Section Preset",
            default=_DEFAULT_PRESET,
            choices=_preset_choices(),
        )
        self.param("core_width", self.TypeDouble, "Core Width", default=_DEFAULT_CORE_WIDTH, unit="um")
        self.param(
            "trench_width",
            self.TypeDouble,
            "Trench Width (<=0 disables)",
            default=_DEFAULT_TRENCH_WIDTH,
            unit="um",
        )
        self.param("radius", self.TypeDouble, "Bend Radius", default=_DEFAULT_RADIUS, unit="um")
        self.param("euler_p", self.TypeDouble, "Euler Fraction p (0..1)", default=_DEFAULT_EULER_P)
        self.param("min_straight", self.TypeDouble, "Min Straight", default=_DEFAULT_MIN_STRAIGHT, unit="um")
        self.param("use_eff_radius", self.TypeBoolean, "Use Effective Radius (R_eff)", default=True)
        self.param(
            "start_width",
            self.TypeDouble,
            "Start Width (<=0 uses core)",
            default=0.0,
            unit="um",
        )
        self.param(
            "end_width",
            self.TypeDouble,
            "End Width (<=0 uses core)",
            default=0.0,
            unit="um",
        )
        self.param("taper_length", self.TypeDouble, "End Taper Length", default=10.0, unit="um")
        self.param(
            "core_layer",
            self.TypeLayer,
            "Core Layer",
            default=kdb.LayerInfo(_DEFAULT_CORE_LAYER[0], _DEFAULT_CORE_LAYER[1]),
        )
        self.param(
            "trench_layer",
            self.TypeLayer,
            "Trench Layer",
            default=kdb.LayerInfo(_DEFAULT_TRENCH_LAYER[0], _DEFAULT_TRENCH_LAYER[1]),
        )
        self.param(
            "extra_layers_json",
            self.TypeString,
            "Extra Layers JSON",
            default="",
            tooltip="Optional JSON array [[layer, dt, width, offset], ...] for 3+ layer waveguides",
        )
        self.param(
            "curve_type",
            self.TypeString,
            "Curve Type",
            default="euler",
            choices=[
                ("Euler Bend (Polyline)", "euler"),
                ("S-Bend (C2 Quintic Bezier)", "sbend_bezier"),
                ("S-Bend (C2 Raised-Sine)", "sbend_sine"),
            ],
        )
        self.param(
            "sbend_angle_deg",
            self.TypeDouble,
            "S-Bend Start Angle",
            default=0.0,
            unit="deg",
        )
        self.param(
            "bend_type",
            self.TypeString,
            "Bend Type",
            default="euler",
            choices=[
                ("TOPIC (C3 Hermite)", "topic"),
                ("Euler (C2 Clothoid)", "euler"),
            ],
        )
        self.param("add_ports", self.TypeBoolean, "Stamp PinRec Ports (o1, o2)", default=False)

    def display_text_impl(self) -> str:
        ctype = str(getattr(self, "curve_type", "euler") or "euler")
        if ctype == "sbend_bezier":
            return f"SBendBez({self.preset}, W={float(self.core_width):.2f})"
        if ctype == "sbend_sine":
            return f"SBendSin({self.preset}, W={float(self.core_width):.2f})"
        btype = str(getattr(self, "bend_type", "euler") or "euler").lower()
        prefix = "TopicWG" if btype == "topic" else "EulerWG"
        return (
            f"{prefix}({self.preset}, W={float(self.core_width):.2f}, "
            f"R={float(self.radius):.1f}, p={float(self.euler_p):.2f})"
        )

    def callback_impl(self, name: str) -> None:
        _apply_preset_callback(self, name, has_min_straight=True)

    def coerce_parameters_impl(self) -> None:
        self.euler_p = max(0.0, min(1.0, float(self.euler_p)))
        self.radius = max(0.5, float(self.radius))
        self.min_straight = max(0.0, float(self.min_straight))
        self.core_width = max(0.01, float(self.core_width))
        self.taper_length = max(0.0, float(self.taper_length))
        self.start_width = max(0.0, float(self.start_width))
        self.end_width = max(0.0, float(self.end_width))
        ctype = str(getattr(self, "curve_type", "euler") or "euler")
        if ctype not in ("euler", "sbend_bezier", "sbend_sine"):
            self.curve_type = "euler"
        btype = str(getattr(self, "bend_type", "euler") or "euler").lower()
        if btype not in VALID_BEND_TYPES:
            self.bend_type = "euler"
        else:
            self.bend_type = btype
        self.sbend_angle_deg = float(getattr(self, "sbend_angle_deg", 0.0)) % 360.0
        _sync_preset_on_coerce(self, has_min_straight=True)

    def can_create_from_shape_impl(self) -> bool:
        return bool(self.shape is not None and self.shape.is_path())

    def parameters_from_shape_impl(self) -> None:
        dpath = self.shape.dpath
        self.path = dpath
        if dpath.width > 0.0:
            self.core_width = float(dpath.width)
        li = self.layout.get_info(self.layer)
        self.core_layer = li
        self.preset = "Custom"
        self.trench_width = 0.0

    def transformation_from_shape_impl(self) -> Any:
        return kdb.Trans()

    def produce_impl(self) -> None:
        dbu = float(self.layout.dbu)
        shape_val = getattr(self, "path", None)
        if shape_val is None:
            shape_val = getattr(self, "path_shape", None)

        pts = _extract_guide_points(shape_val, dbu)
        if len(pts) < 2:
            _draw_error_indicator(
                self.layout,
                self.cell,
                pts,
                "EulerWaveguide error: guiding path needs at least 2 points",
            )
            return

        cs, extra_err = _build_cross_section_from_params(
            preset=str(self.preset),
            core_layer_info=self.core_layer,
            core_width=float(self.core_width),
            trench_layer_info=self.trench_layer,
            trench_width=float(self.trench_width),
            radius=float(self.radius),
            euler_p=float(self.euler_p),
            min_straight=float(self.min_straight),
            extra_layers_json=str(self.extra_layers_json or ""),
        )
        if extra_err:
            _draw_error_indicator(self.layout, self.cell, pts, extra_err)

        ctype = str(getattr(self, "curve_type", "euler") or "euler")
        btype = str(getattr(self, "bend_type", "euler") or "euler").lower()
        if ctype in VALID_SBEND_CURVES:
            route_res = route_sbend_from_guide_points(
                pts,
                curve_type=ctype,
                start_angle_deg=float(getattr(self, "sbend_angle_deg", 0.0)),
            )
        else:
            route_res = route_from_guide_points(
                pts,
                radius=cs.radius,
                euler_p=cs.euler_p,
                min_straight=cs.min_straight,
                use_eff_radius=bool(self.use_eff_radius),
                bend_type=btype,
            )
        sw = float(self.start_width) if float(self.start_width) > 0.0 else None
        ew = float(self.end_width) if float(self.end_width) > 0.0 else None
        draw_route_to_cell(
            self.layout,
            self.cell,
            route_res,
            cs,
            start_width=sw,
            end_width=ew,
            taper_length=float(self.taper_length),
            draw_error_layer=DEFAULT_ERROR_LAYER,
        )

        if bool(getattr(self, "add_ports", False)) and route_res.ok and len(route_res.centerline) >= 2:
            cl = cs.core_layer
            x0, y0 = route_res.centerline[0]
            x1, y1 = route_res.centerline[1]
            for px, py in route_res.centerline[1:]:
                if math.hypot(px - x0, py - y0) > 1e-9:
                    x1, y1 = px, py
                    break
            xe, ye = route_res.centerline[-1]
            xp, yp = route_res.centerline[-2]
            for px, py in reversed(route_res.centerline[:-1]):
                if math.hypot(xe - px, ye - py) > 1e-9:
                    xp, yp = px, py
                    break
            start_ang = math.degrees(math.atan2(y0 - y1, x0 - x1)) % 360.0
            end_ang = math.degrees(math.atan2(ye - yp, xe - xp)) % 360.0
            w_start = sw if (sw is not None and sw > 0.0) else cs.core_width
            w_end = ew if (ew is not None and ew > 0.0) else cs.core_width
            add_port_to_cell(
                self.layout,
                self.cell,
                name="o1",
                x=float(x0),
                y=float(y0),
                angle_deg=start_ang,
                width=float(w_start),
                core_layer=cl,
                pin_layer=DEFAULT_PIN_LAYER,
            )
            add_port_to_cell(
                self.layout,
                self.cell,
                name="o2",
                x=float(xe),
                y=float(ye),
                angle_deg=end_ang,
                width=float(w_end),
                core_layer=cl,
                pin_layer=DEFAULT_PIN_LAYER,
            )


class EulerBend(kdb.PCellDeclarationHelper):
    """Standalone parametric multi-layer Euler bend PCell with PinRec ports o1/o2."""

    def __init__(self) -> None:
        super().__init__()
        self.param("angle_deg", self.TypeDouble, "Bend Angle", default=90.0, unit="deg")
        self.param("radius", self.TypeDouble, "Bend Radius", default=_DEFAULT_RADIUS, unit="um")
        self.param("euler_p", self.TypeDouble, "Euler Fraction p (0..1)", default=_DEFAULT_EULER_P)
        self.param("use_eff_radius", self.TypeBoolean, "Use Effective Radius (R_eff)", default=True)
        self.param(
            "preset",
            self.TypeString,
            "Cross-Section Preset",
            default=_DEFAULT_PRESET,
            choices=_preset_choices(),
        )
        self.param("core_width", self.TypeDouble, "Core Width", default=_DEFAULT_CORE_WIDTH, unit="um")
        self.param(
            "trench_width",
            self.TypeDouble,
            "Trench Width (<=0 disables)",
            default=_DEFAULT_TRENCH_WIDTH,
            unit="um",
        )
        self.param(
            "core_layer",
            self.TypeLayer,
            "Core Layer",
            default=kdb.LayerInfo(_DEFAULT_CORE_LAYER[0], _DEFAULT_CORE_LAYER[1]),
        )
        self.param(
            "trench_layer",
            self.TypeLayer,
            "Trench Layer",
            default=kdb.LayerInfo(_DEFAULT_TRENCH_LAYER[0], _DEFAULT_TRENCH_LAYER[1]),
        )
        self.param(
            "extra_layers_json",
            self.TypeString,
            "Extra Layers JSON",
            default="",
            tooltip="Optional JSON array [[layer, dt, width, offset], ...] for 3+ layer bends",
        )
        self.param("add_ports", self.TypeBoolean, "Stamp PinRec Ports (o1, o2)", default=True)

    def display_text_impl(self) -> str:
        return (
            f"EulerBend({float(self.angle_deg):.1f}deg, R={float(self.radius):.1f}, "
            f"p={float(self.euler_p):.2f}, W={float(self.core_width):.2f})"
        )

    def callback_impl(self, name: str) -> None:
        _apply_preset_callback(self, name, has_min_straight=False)

    def coerce_parameters_impl(self) -> None:
        self.euler_p = max(0.0, min(1.0, float(self.euler_p)))
        self.radius = max(0.5, float(self.radius))
        self.core_width = max(0.01, float(self.core_width))
        ang = float(self.angle_deg)
        if abs(ang) < 0.1:
            ang = 90.0 if ang >= 0.0 else -90.0
        if ang > 359.0:
            ang = 359.0
        elif ang < -359.0:
            ang = -359.0
        self.angle_deg = ang
        _sync_preset_on_coerce(self, has_min_straight=False)

    def produce_impl(self) -> None:
        dbu = float(self.layout.dbu)
        cs, extra_err = _build_cross_section_from_params(
            preset=str(self.preset),
            core_layer_info=self.core_layer,
            core_width=float(self.core_width),
            trench_layer_info=self.trench_layer,
            trench_width=float(self.trench_width),
            radius=float(self.radius),
            euler_p=float(self.euler_p),
            min_straight=0.0,
            extra_layers_json=str(self.extra_layers_json or ""),
        )
        if extra_err:
            _draw_error_indicator(self.layout, self.cell, [(0.0, 0.0)], extra_err)

        try:
            pts = euler_bend_points(
                float(self.angle_deg),
                cs.radius,
                p=cs.euler_p,
                use_eff_radius=bool(self.use_eff_radius),
            )
            metrics = euler_bend_metrics(
                float(self.angle_deg),
                cs.radius,
                p=cs.euler_p,
                use_eff_radius=bool(self.use_eff_radius),
            )
        except Exception as exc:
            _draw_error_indicator(self.layout, self.cell, [(0.0, 0.0)], f"EulerBend error: {exc}")
            return

        regions = build_multilayer_waveguide_regions(pts, cs, dbu=dbu)
        for (layer_num, dt_num), region in regions.items():
            lidx = self.layout.layer(layer_num, dt_num)
            self.cell.shapes(lidx).insert(region)

        if bool(self.add_ports):
            cl = cs.core_layer
            dx = float(metrics["dx"])
            dy = float(metrics["dy"])
            end_angle = float(self.angle_deg) % 360.0
            add_port_to_cell(
                self.layout,
                self.cell,
                name="o1",
                x=0.0,
                y=0.0,
                angle_deg=180.0,
                width=cs.core_width,
                core_layer=cl,
                pin_layer=DEFAULT_PIN_LAYER,
            )
            add_port_to_cell(
                self.layout,
                self.cell,
                name="o2",
                x=dx,
                y=dy,
                angle_deg=end_angle,
                width=cs.core_width,
                core_layer=cl,
                pin_layer=DEFAULT_PIN_LAYER,
            )


class TOPICBend(kdb.PCellDeclarationHelper):
    """Standalone constant-width multi-layer TOPIC (C3 Hermite-curvature) bend PCell with PinRec ports."""

    def __init__(self) -> None:
        super().__init__()
        self.param("angle_deg", self.TypeDouble, "Bend Angle", default=90.0, unit="deg")
        self.param("radius", self.TypeDouble, "Effective Radius (R_eff)", default=_DEFAULT_RADIUS, unit="um")
        self.param(
            "euler_p",
            self.TypeDouble,
            "TOPIC Fraction p (0..1)",
            default=_DEFAULT_EULER_P,
            tooltip="p = 2*theta_p / |angle_deg| (0=circular, 0.50=>22.5 deg for 90 deg, 1=pure cubic TOP)",
        )
        self.param("use_eff_radius", self.TypeBoolean, "Use Effective Radius (R_eff)", default=True)
        self.param("straight_in", self.TypeDouble, "Input Straight Lead", default=0.0, unit="um")
        self.param("straight_out", self.TypeDouble, "Output Straight Lead", default=0.0, unit="um")
        self.param(
            "preset",
            self.TypeString,
            "Cross-Section Preset",
            default=_DEFAULT_PRESET,
            choices=_preset_choices(),
        )
        self.param("core_width", self.TypeDouble, "Core Width", default=_DEFAULT_CORE_WIDTH, unit="um")
        self.param(
            "trench_width",
            self.TypeDouble,
            "Trench Width (<=0 disables)",
            default=_DEFAULT_TRENCH_WIDTH,
            unit="um",
        )
        self.param(
            "core_layer",
            self.TypeLayer,
            "Core Layer",
            default=kdb.LayerInfo(_DEFAULT_CORE_LAYER[0], _DEFAULT_CORE_LAYER[1]),
        )
        self.param(
            "trench_layer",
            self.TypeLayer,
            "Trench Layer",
            default=kdb.LayerInfo(_DEFAULT_TRENCH_LAYER[0], _DEFAULT_TRENCH_LAYER[1]),
        )
        self.param(
            "extra_layers_json",
            self.TypeString,
            "Extra Layers JSON",
            default="",
            tooltip="Optional JSON array [[layer, dt, width, offset, role], ...] for multi-layer bends",
        )
        self.param("add_ports", self.TypeBoolean, "Stamp PinRec Ports (o1, o2)", default=True)

    def display_text_impl(self) -> str:
        rlbl = "Reff" if bool(self.use_eff_radius) else "Rc"
        return (
            f"TOPICBend({float(self.angle_deg):.1f}deg, {rlbl}={float(self.radius):.1f}, "
            f"p={float(self.euler_p):.2f}, W={float(self.core_width):.2f})"
        )

    def callback_impl(self, name: str) -> None:
        _apply_preset_callback(self, name, has_min_straight=False)

    def coerce_parameters_impl(self) -> None:
        self.euler_p = max(0.0, min(1.0, float(self.euler_p)))
        self.radius = max(0.5, float(self.radius))
        self.core_width = max(0.01, float(self.core_width))
        self.straight_in = max(0.0, float(self.straight_in))
        self.straight_out = max(0.0, float(self.straight_out))
        ang = float(self.angle_deg)
        if abs(ang) < 0.1:
            ang = 90.0 if ang >= 0.0 else -90.0
        if ang > 359.0:
            ang = 359.0
        elif ang < -359.0:
            ang = -359.0
        self.angle_deg = ang
        _sync_preset_on_coerce(self, has_min_straight=False)

    def produce_impl(self) -> None:
        dbu = float(self.layout.dbu)
        cs, extra_err = _build_cross_section_from_params(
            preset=str(self.preset),
            core_layer_info=self.core_layer,
            core_width=float(self.core_width),
            trench_layer_info=self.trench_layer,
            trench_width=float(self.trench_width),
            radius=float(self.radius),
            euler_p=float(self.euler_p),
            min_straight=0.0,
            extra_layers_json=str(self.extra_layers_json or ""),
        )
        if extra_err:
            _draw_error_indicator(self.layout, self.cell, [(0.0, 0.0)], extra_err)

        s_in = max(0.0, float(self.straight_in))
        s_out = max(0.0, float(self.straight_out))
        ang_deg = float(self.angle_deg)
        try:
            bend_pts = topic_bend_points(
                ang_deg,
                cs.radius,
                p=cs.euler_p,
                use_eff_radius=bool(self.use_eff_radius),
            )
            metrics = topic_bend_metrics(
                ang_deg,
                cs.radius,
                p=cs.euler_p,
                use_eff_radius=bool(self.use_eff_radius),
            )
        except Exception as exc:
            _draw_error_indicator(self.layout, self.cell, [(0.0, 0.0)], f"TOPICBend error: {exc}")
            return

        # Assemble centerline starting flush at (0, 0) along +X
        pts: list[tuple[float, float]] = []
        if s_in > 1e-6:
            pts.append((0.0, 0.0))
        for bx, by in bend_pts:
            pts.append((bx + s_in, by))
        ang_rad = math.radians(ang_deg)
        x_end = s_in + float(metrics["dx"])
        y_end = float(metrics["dy"])
        if s_out > 1e-6:
            x_end = x_end + s_out * math.cos(ang_rad)
            y_end = y_end + s_out * math.sin(ang_rad)
            pts.append((x_end, y_end))

        regions = build_multilayer_waveguide_regions(pts, cs, dbu=dbu)
        for (layer_num, dt_num), region in regions.items():
            lidx = self.layout.layer(layer_num, dt_num)
            self.cell.shapes(lidx).insert(region)

        if bool(self.add_ports):
            cl = cs.core_layer
            end_angle = ang_deg % 360.0
            add_port_to_cell(
                self.layout,
                self.cell,
                name="o1",
                x=0.0,
                y=0.0,
                angle_deg=180.0,
                width=cs.core_width,
                core_layer=cl,
                pin_layer=DEFAULT_PIN_LAYER,
            )
            add_port_to_cell(
                self.layout,
                self.cell,
                name="o2",
                x=x_end,
                y=y_end,
                angle_deg=end_angle,
                width=cs.core_width,
                core_layer=cl,
                pin_layer=DEFAULT_PIN_LAYER,
            )


def register_pcell_library(force: bool = False) -> Any:
    """Register the PIC_Router PCell library idempotently."""
    global _REGISTERED_LIBRARY
    existing = kdb.Library.library_by_name(LIBRARY_NAME)
    if existing is not None and not force:
        try:
            names = set(existing.layout().pcell_names())
            if {"EulerWaveguide", "EulerBend", "TOPICBend"}.issubset(names):
                _REGISTERED_LIBRARY = existing
                return existing
        except Exception:
            pass

    lib = kdb.Library()
    lib.description = LIBRARY_DESCRIPTION
    lib.layout().register_pcell("EulerWaveguide", EulerWaveguide())
    lib.layout().register_pcell("EulerBend", EulerBend())
    lib.layout().register_pcell("TOPICBend", TOPICBend())
    lib.register(LIBRARY_NAME)
    _REGISTERED_LIBRARY = lib
    return lib


def instantiate_euler_waveguide(
    layout: Any,
    top_cell: Any,
    guide_points: Sequence[tuple[float, float]],
    cross_section: CrossSection,
    start_width: float | None = None,
    end_width: float | None = None,
    taper_length: float = 10.0,
    use_eff_radius: bool = True,
    min_straight: float | None = None,
    trans: Any = None,
    curve_type: str = "euler",
    sbend_angle_deg: float = 0.0,
    bend_type: str = "euler",
    add_ports: bool = False,
) -> Any:
    """Create and insert an EulerWaveguide PCell instance into ``top_cell``."""
    register_pcell_library()
    dpts = [kdb.DPoint(float(x), float(y)) for x, y in guide_points]
    dpath = kdb.DPath(dpts, float(cross_section.core_width))

    core_l = cross_section.core_layer or _DEFAULT_CORE_LAYER
    trench_l = cross_section.trench_layer
    trench_w = float(cross_section.trench_width) if (trench_l is not None and cross_section.trench_width is not None) else 0.0
    eff_min_straight = float(cross_section.min_straight if min_straight is None else min_straight)
    preset_name = _effective_preset_name(cross_section, check_min_straight=True)
    if preset_name != "Custom" and abs(eff_min_straight - float(CROSS_SECTION_PRESETS[preset_name].min_straight)) > 1e-9:
        preset_name = "Custom"

    btype = str(bend_type or "euler").lower()
    if btype not in VALID_BEND_TYPES:
        btype = "euler"

    params = {
        "path": dpath,
        "preset": preset_name,
        "core_width": float(cross_section.core_width),
        "trench_width": trench_w,
        "radius": float(cross_section.radius),
        "euler_p": float(cross_section.euler_p),
        "min_straight": eff_min_straight,
        "use_eff_radius": bool(use_eff_radius),
        "start_width": float(start_width) if (start_width is not None and start_width > 0.0) else 0.0,
        "end_width": float(end_width) if (end_width is not None and end_width > 0.0) else 0.0,
        "taper_length": float(taper_length),
        "core_layer": kdb.LayerInfo(core_l[0], core_l[1]),
        "trench_layer": kdb.LayerInfo(trench_l[0], trench_l[1]) if trench_l is not None else kdb.LayerInfo(1, 1),
        "extra_layers_json": _extra_layers_to_json(cross_section),
        "curve_type": str(curve_type or "euler"),
        "sbend_angle_deg": float(sbend_angle_deg) % 360.0,
        "bend_type": btype,
        "add_ports": bool(add_ports),
    }

    pcell = layout.create_cell("EulerWaveguide", LIBRARY_NAME, params)
    if pcell is None:
        raise RuntimeError(f"Failed to create PCell 'EulerWaveguide' from library '{LIBRARY_NAME}'")

    dtrans = trans if trans is not None else kdb.DTrans()
    if isinstance(dtrans, kdb.DCplxTrans):
        inst_arr = kdb.DCellInstArray(pcell.cell_index(), dtrans)
    else:
        inst_arr = kdb.DCellInstArray(pcell.cell_index(), dtrans)
    return top_cell.insert(inst_arr)


def instantiate_euler_bend(
    layout: Any,
    top_cell: Any,
    angle_deg: float = 90.0,
    cross_section: CrossSection | None = None,
    trans: Any = None,
    use_eff_radius: bool = True,
    add_ports: bool = True,
) -> Any:
    """Create and insert an EulerBend PCell instance into ``top_cell``."""
    register_pcell_library()
    cs = cross_section or CROSS_SECTION_PRESETS[_DEFAULT_PRESET]
    core_l = cs.core_layer
    trench_l = cs.trench_layer
    trench_w = float(cs.trench_width) if (trench_l is not None and cs.trench_width is not None) else 0.0
    preset_name = _effective_preset_name(cs, check_min_straight=False)

    params = {
        "angle_deg": float(angle_deg),
        "radius": float(cs.radius),
        "euler_p": float(cs.euler_p),
        "use_eff_radius": bool(use_eff_radius),
        "preset": preset_name,
        "core_width": float(cs.core_width),
        "trench_width": trench_w,
        "core_layer": kdb.LayerInfo(core_l[0], core_l[1]),
        "trench_layer": kdb.LayerInfo(trench_l[0], trench_l[1]) if trench_l is not None else kdb.LayerInfo(1, 1),
        "extra_layers_json": _extra_layers_to_json(cs),
        "add_ports": bool(add_ports),
    }

    pcell = layout.create_cell("EulerBend", LIBRARY_NAME, params)
    if pcell is None:
        raise RuntimeError(f"Failed to create PCell 'EulerBend' from library '{LIBRARY_NAME}'")

    dtrans = trans if trans is not None else kdb.DTrans()
    return top_cell.insert(kdb.DCellInstArray(pcell.cell_index(), dtrans))


def instantiate_topic_bend(
    layout: Any,
    top_cell: Any,
    angle_deg: float = 90.0,
    cross_section: CrossSection | None = None,
    r_eff: float | None = None,
    p: float | None = None,
    straight_in: float = 0.0,
    straight_out: float = 0.0,
    trans: Any = None,
    use_eff_radius: bool = True,
    add_ports: bool = True,
) -> Any:
    """Create and insert a constant-width multi-layer ``TOPICBend`` PCell instance into ``top_cell``."""
    register_pcell_library()
    cs = cross_section or CROSS_SECTION_PRESETS[_DEFAULT_PRESET]
    if r_eff is not None or p is not None:
        cs = cs.with_overrides(
            radius=(float(r_eff) if r_eff is not None else cs.radius),
            euler_p=(float(p) if p is not None else cs.euler_p),
        )
    core_l = cs.core_layer
    trench_l = cs.trench_layer
    trench_w = float(cs.trench_width) if (trench_l is not None and cs.trench_width is not None) else 0.0
    preset_name = _effective_preset_name(cs, check_min_straight=False)

    params = {
        "angle_deg": float(angle_deg),
        "radius": float(cs.radius),
        "euler_p": float(cs.euler_p),
        "use_eff_radius": bool(use_eff_radius),
        "straight_in": max(0.0, float(straight_in)),
        "straight_out": max(0.0, float(straight_out)),
        "preset": preset_name,
        "core_width": float(cs.core_width),
        "trench_width": trench_w,
        "core_layer": kdb.LayerInfo(core_l[0], core_l[1]),
        "trench_layer": kdb.LayerInfo(trench_l[0], trench_l[1]) if trench_l is not None else kdb.LayerInfo(1, 1),
        "extra_layers_json": _extra_layers_to_json(cs),
        "add_ports": bool(add_ports),
    }

    pcell = layout.create_cell("TOPICBend", LIBRARY_NAME, params)
    if pcell is None:
        raise RuntimeError(f"Failed to create PCell 'TOPICBend' from library '{LIBRARY_NAME}'")

    dtrans = trans if trans is not None else kdb.DTrans()
    return top_cell.insert(kdb.DCellInstArray(pcell.cell_index(), dtrans))
