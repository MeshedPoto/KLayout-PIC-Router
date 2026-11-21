"""Mask-prep & lithography operations for existing PIC structures in KLayout.

Pure Python + ``klayout.db`` / ``pya`` (zero numpy/scipy):
  1. ``add_trench_to_cell`` / ``build_trench_region``:
     Generate single- or multi-layer trenches (e.g. double-etch ``1/1=10; 2/1=25``)
     around existing core-only PIC structures, trimmed cleanly flush at port faces.
  2. ``bias_cell_linewidth`` / ``bias_region_preserve_length``:
     Apply positive or negative CD linewidth bias (e.g. ``+120 nm``, ``+70 nm``, ``-50 nm``)
     to straight, curved, bend, MMI, and angled-facet PIC structures in BOTH horizontal
     and vertical directions simultaneously while keeping total optical path length and
     port coordinates strictly invariant (0.000 nm length error via Extend -> Size -> Trim).
"""

from __future__ import annotations

import json
import math
from typing import Any, Sequence

from .config import DEFAULT_DBU, DEFAULT_PIN_LAYER, get_kdb
from .ports import Port, add_port_to_cell, auto_detect_ports, read_cell_ports


def _region_total_points(region: Any) -> int:
    total = 0
    for poly in region.each():
        total += int(poly.num_points())
    return total


def _rect_at_port(
    kdb: Any,
    port: Port,
    u0: float,
    u1: float,
    half_t: float,
    dbu: float,
) -> Any:
    """Create an oriented integer-DBU rectangle in a port's local (outward u, tangent t) frame."""
    rad = math.radians(float(port.angle_deg))
    ux, uy = math.cos(rad), math.sin(rad)
    tx, ty = -uy, ux
    px, py = float(port.x), float(port.y)
    pts = [
        (px + u0 * ux - half_t * tx, py + u0 * uy - half_t * ty),
        (px + u1 * ux - half_t * tx, py + u1 * uy - half_t * ty),
        (px + u1 * ux + half_t * tx, py + u1 * uy + half_t * ty),
        (px + u0 * ux + half_t * tx, py + u0 * uy + half_t * ty),
    ]
    return kdb.DPolygon([kdb.DPoint(x, y) for x, y in pts]).to_itype(float(dbu))


def _dedupe_ports(ports: Sequence[Port], tol_um: float = 0.5) -> list[Port]:
    out: list[Port] = []
    for p in ports:
        if any(math.hypot(p.x - q.x, p.y - q.y) < tol_um for q in out):
            continue
        out.append(p)
    return out


def detect_open_ports_on_region(
    region: Any,
    core_layer: tuple[int, int] = (1, 0),
    dbu: float = DEFAULT_DBU,
) -> list[Port]:
    """Detect only true open waveguide end faces on an already-merged ``kdb.Region``."""
    reg = region.merged()
    if reg.is_empty():
        return []
    kdb = get_kdb()
    tmp_ly = kdb.Layout()
    tmp_ly.dbu = float(dbu)
    tmp_cell = tmp_ly.create_cell("_TMP_MASK_OP")
    li = tmp_ly.layer(int(core_layer[0]), int(core_layer[1]))
    tmp_cell.shapes(li).insert(reg)
    return auto_detect_ports(tmp_ly, tmp_cell, core_layers=(tuple(core_layer),))


def resolve_cell_ports(
    layout: Any,
    cell: Any,
    core_layers: Sequence[tuple[int, int]],
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
    include_auto_missing: bool = False,
) -> list[Port]:
    """Return local-frame ports of ``cell`` from PinRec (90/0), falling back to auto-detection."""
    pin_ports = read_cell_ports(layout, cell, pin_layer=pin_layer)
    core_set = {(int(l), int(d)) for l, d in core_layers}
    if pin_ports:
        matched = [p for p in pin_ports if not core_set or tuple(p.core_layer) in core_set]
        if not include_auto_missing:
            return matched if matched else list(pin_ports)
        auto_p = auto_detect_ports(layout, cell, core_layers=tuple(core_set) if core_set else ((1, 0), (2, 0)))
        return _dedupe_ports(list(matched) + list(auto_p), tol_um=0.5)
    return auto_detect_ports(layout, cell, core_layers=tuple(core_set) if core_set else ((1, 0), (2, 0)))


def _safe_port_trim_depth(
    kdb: Any,
    port: Port,
    max_depth: float,
    half_t: float,
    core_region: Any,
    dbu: float,
) -> float:
    """Prevent a trench trim box from accidentally chopping another core branch in front of an interior port."""
    probe = _rect_at_port(kdb, port, 0.05, max_depth, min(half_t, max(0.5 * port.width + 0.5, 1.0)), dbu)
    hit = core_region & kdb.Region(probe)
    if hit.is_empty():
        return max_depth
    rad = math.radians(float(port.angle_deg))
    ux, uy = math.cos(rad), math.sin(rad)
    min_u = max_depth
    for poly in hit.each():
        for pt in poly.each_point_hull():
            u = (float(pt.x) * dbu - port.x) * ux + (float(pt.y) * dbu - port.y) * uy
            if u > 0.02 and u < min_u:
                min_u = u
    return max(0.0, min_u - 0.1)


# ---------------------------------------------------------------------------
# Feature 1: Add Single- or Multi-Layer Trench to Existing Core Structures
# ---------------------------------------------------------------------------

def build_trench_region(
    core_region: Any,
    margin_um: float,
    dbu: float = DEFAULT_DBU,
    ports: Sequence[Port] = (),
    trim_at_ports: bool = True,
    hollow_minus_core: bool = False,
    sizing_mode: int = 2,
) -> Any:
    """Expand ``core_region`` laterally by ``margin_um`` and trim cleanly flush at port faces.

    Automatically simplifies >50k-vertex dense curves (20 nm tolerance) before large trench
    sizing so 1M+ vertex GDS components complete in seconds without freezing KLayout.
    """
    kdb = get_kdb()
    dbu_f = float(dbu)
    margin = float(margin_um)
    reg = core_region.merged()
    if reg.is_empty():
        return kdb.Region()
    if margin <= 1e-6:
        return kdb.Region() if hollow_minus_core else reg

    work = reg
    if _region_total_points(work) > 50000 and hasattr(work, "smoothed"):
        tol_dbu = max(1, int(round(0.020 / dbu_f)))
        work = work.smoothed(tol_dbu)

    m_dbu = int(round(margin / dbu_f))
    trench = work.sized(m_dbu, m_dbu, int(sizing_mode)).merged()

    if trim_at_ports and ports:
        trim_reg = kdb.Region()
        for p in ports:
            half_t = 0.5 * max(float(p.width), 0.1) + margin + 1.0
            depth = _safe_port_trim_depth(kdb, p, margin + 1.5, half_t, reg, dbu_f)
            if depth > 1e-3:
                trim_reg.insert(_rect_at_port(kdb, p, 0.0, depth, half_t, dbu_f))
        if not trim_reg.is_empty():
            trench = (trench - trim_reg).merged()

    if hollow_minus_core:
        trench = (trench - reg).merged()

    return trench


def add_trench_to_pcell_inst(
    layout: Any,
    inst: Any,
    trench_specs: Sequence[tuple[int, int, float]],
    nominal_core_width: float = 0.80,
    width_mode: str = "total_width",
) -> bool:
    """Update an ``EulerWaveguide`` or ``EulerBend`` PCell instance with 1+ trench layers parametrically."""
    if not (inst.is_pcell() and inst.cell is not None):
        return False
    pname = inst.cell.pcell_name()
    if pname not in ("EulerWaveguide", "EulerBend", "TOPICBend"):
        return False
    if not trench_specs:
        return False

    kdb = get_kdb()
    params = dict(inst.pcell_parameters_by_name())
    cw = float(params.get("core_width", nominal_core_width))

    def _to_total_w(val: float) -> float:
        if width_mode == "side_margin":
            return max(0.0, cw + 2.0 * float(val))
        return max(0.0, float(val))

    # Preserve non-trench extra layers (e.g. extra cores / cladding), replace extra trenches
    kept_extras: list[list[Any]] = []
    raw_extra = str(params.get("extra_layers_json", "") or "").strip()
    if raw_extra:
        try:
            for item in json.loads(raw_extra):
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    role = str(item[4]) if len(item) >= 5 else "cladding"
                    if role != "trench":
                        kept_extras.append(list(item))
        except Exception:
            pass

    first_l, first_d, first_val = trench_specs[0]
    first_tw = _to_total_w(first_val)
    params["preset"] = "Custom"
    params["trench_layer"] = kdb.LayerInfo(int(first_l), int(first_d))
    params["trench_width"] = float(first_tw)

    for tl, td, tval in trench_specs[1:]:
        tw = _to_total_w(tval)
        if tw > 0.0:
            kept_extras.append([int(tl), int(td), float(tw), 0.0, "trench"])

    params["extra_layers_json"] = json.dumps(kept_extras) if kept_extras else ""
    inst.change_pcell_parameters(params)
    return True


def add_trench_to_cell(
    layout: Any,
    cell: Any,
    core_layers: Sequence[tuple[int, int]],
    trench_specs: Sequence[tuple[int, int, float]],
    nominal_core_width: float = 0.80,
    width_mode: str = "total_width",
    trim_at_ports: bool = True,
    hollow_minus_core: bool = False,
    overwrite_existing: bool = True,
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
    source_region: Any = None,
    ports_override: Sequence[Port] | None = None,
) -> dict[str, Any]:
    """Generate single- or multi-layer trench shapes in ``cell`` from ``core_layers``.

    Args:
        trench_specs: List of ``(layer, datatype, width_or_margin_um)``.
        width_mode:
          - ``"total_width"`` (default): ``margin = 0.5 * (trench_w - nominal_core_width)``,
            matching ``EulerWaveguide`` total trench width on nominal waveguides (e.g. 10 um / 25 um).
          - ``"side_margin"``: direct single-sided lateral expansion distance in um.
    """
    kdb = get_kdb()
    dbu = float(layout.dbu)

    if source_region is not None:
        core_reg = source_region.merged()
    else:
        core_reg = kdb.Region()
        for cl, cd in core_layers:
            li_c = layout.find_layer(int(cl), int(cd))
            if li_c is not None and li_c >= 0:
                core_reg += kdb.Region(cell.begin_shapes_rec(li_c))
        core_reg.merge()

    if core_reg.is_empty():
        return {"ok": False, "error": f"No shapes found on core layer(s) {list(core_layers)} in '{cell.name}'.", "layers_written": []}

    if ports_override is not None:
        ports = list(ports_override)
    elif trim_at_ports:
        ports = resolve_cell_ports(layout, cell, core_layers=core_layers, pin_layer=pin_layer)
    else:
        ports = []

    written: list[tuple[int, int, int]] = []
    for tl, td, val in trench_specs:
        if width_mode == "side_margin":
            margin_um = float(val)
        else:
            margin_um = 0.5 * (float(val) - float(nominal_core_width))
        if margin_um <= 1e-5:
            continue
        trench_reg = build_trench_region(
            core_reg,
            margin_um=margin_um,
            dbu=dbu,
            ports=ports,
            trim_at_ports=trim_at_ports,
            hollow_minus_core=hollow_minus_core,
        )
        if trench_reg.is_empty():
            continue
        li_t = layout.layer(int(tl), int(td))
        if overwrite_existing:
            cell.shapes(li_t).clear()
        cell.shapes(li_t).insert(trench_reg)
        written.append((int(tl), int(td), int(trench_reg.count())))

    if not written:
        return {"ok": False, "error": "No valid trench margin (> 0 um) specified.", "layers_written": []}

    return {
        "ok": True,
        "cell_name": cell.name,
        "ports_trimmed": len(ports) if trim_at_ports else 0,
        "layers_written": written,
    }


# ---------------------------------------------------------------------------
# Feature 2: Length-Preserving PIC Linewidth Compensation (CD Bias)
# ---------------------------------------------------------------------------

def bias_region_preserve_length(
    core_region: Any,
    bias_um: float,
    dbu: float = DEFAULT_DBU,
    ports: Sequence[Port] = (),
    bias_x_um: float | None = None,
    bias_y_um: float | None = None,
    preserve_port_faces: bool = True,
    sizing_mode: int = 2,
) -> Any:
    """Apply positive or negative linewidth CD bias while keeping total centerline length invariant.

    Args:
        bias_um: Total isotropic linewidth change in um (e.g. ``+0.120`` for ``+120 nm``:
            ``700 nm -> 820 nm``; ``-0.080`` for ``-80 nm``: ``700 nm -> 620 nm``).
        bias_x_um: Optional override for X-axis total linewidth bias (vertical waveguide width).
        bias_y_um: Optional override for Y-axis total linewidth bias (horizontal waveguide width).
        ports: Port list whose end planes remain fixed at 0.000 nm shift via Extend -> Size -> Trim.
    """
    kdb = get_kdb()
    dbu_f = float(dbu)
    reg = core_region.merged()
    if reg.is_empty():
        return kdb.Region()

    dx = 0.5 * float(bias_x_um if bias_x_um is not None else bias_um)
    dy = 0.5 * float(bias_y_um if bias_y_um is not None else bias_um)
    dx_dbu = int(round(dx / dbu_f))
    dy_dbu = int(round(dy / dbu_f))
    if dx_dbu == 0 and dy_dbu == 0:
        return reg

    work = reg
    if _region_total_points(work) > 80000 and hasattr(work, "smoothed"):
        tol_dbu = max(1, int(round(0.002 / dbu_f)))  # 2 nm tolerance on >80k-vertex polygons
        work = work.smoothed(tol_dbu)

    max_abs_d = max(abs(dx), abs(dy))
    if preserve_port_faces and ports:
        ext = max_abs_d + 0.5
        caps = kdb.Region()
        trims = kdb.Region()
        for p in ports:
            pw = max(float(p.width), 0.05)
            # Extend outward along port normal by `ext` at exact port width before sizing:
            # guarantees sharp 90-deg port corners (+bias) and zero retraction air-gaps (-bias)!
            caps.insert(_rect_at_port(kdb, p, -0.05, ext, 0.5 * pw, dbu_f))
            trims.insert(
                _rect_at_port(
                    kdb,
                    p,
                    0.0,
                    ext + max_abs_d + 0.5,
                    0.5 * pw + max_abs_d + 0.08,
                    dbu_f,
                )
            )
        work = (work + caps).merged()
        sized = work.sized(dx_dbu, dy_dbu, int(sizing_mode))
        return (sized - trims).merged()

    return work.sized(dx_dbu, dy_dbu, int(sizing_mode)).merged()


def bias_pcell_inst_linewidth(
    inst: Any,
    bias_um: float,
    target_core_layers: Sequence[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Parametrically apply isotropic linewidth bias ``bias_um`` to an ``EulerWaveguide`` / ``EulerBend`` PCell.

    Preserves exact centerline geometry, bend radii, and total optical length (0.000 nm error).
    """
    if not (inst.is_pcell() and inst.cell is not None):
        return {"ok": False, "skipped": True}
    pname = inst.cell.pcell_name()
    if pname not in ("EulerWaveguide", "EulerBend", "TOPICBend"):
        return {"ok": False, "skipped": True}

    params = dict(inst.pcell_parameters_by_name())
    cl_info = params.get("core_layer")
    cl_tuple = (int(cl_info.layer), int(cl_info.datatype)) if cl_info is not None else (1, 0)
    target_set = {(int(l), int(d)) for l, d in target_core_layers} if target_core_layers else None

    changed = False
    if target_set is None or cl_tuple in target_set:
        old_cw = float(params.get("core_width", 0.80))
        new_cw = round(old_cw + float(bias_um), 6)
        if new_cw <= 0.02:
            return {"ok": False, "error": f"Biased core_width ({new_cw:.4f} um) would be <= 0.02 um."}
        params["core_width"] = new_cw
        params["preset"] = "Custom"
        for key in ("start_width", "end_width"):
            if key in params and float(params[key]) > 0.0:
                nw = round(float(params[key]) + float(bias_um), 6)
                if nw <= 0.02:
                    return {"ok": False, "error": f"Biased {key} ({nw:.4f} um) would be <= 0.02 um."}
                params[key] = nw
        changed = True

    # Also bias any extra role=="core" layers in extra_layers_json matching target_set
    raw_extra = str(params.get("extra_layers_json", "") or "").strip()
    if raw_extra:
        try:
            items = json.loads(raw_extra)
            mod_extra = False
            new_items = []
            for item in items:
                if isinstance(item, list) and len(item) >= 3:
                    role = str(item[4]) if len(item) >= 5 else "cladding"
                    key_ld = (int(item[0]), int(item[1]))
                    if role == "core" and (target_set is None or key_ld in target_set):
                        nw = round(float(item[2]) + float(bias_um), 6)
                        if nw <= 0.02:
                            return {"ok": False, "error": f"Biased extra core {key_ld} width ({nw:.4f} um) <= 0.02 um."}
                        item = list(item)
                        item[2] = nw
                        mod_extra = True
                new_items.append(item)
            if mod_extra:
                params["extra_layers_json"] = json.dumps(new_items)
                params["preset"] = "Custom"
                changed = True
        except Exception:
            pass

    if not changed:
        return {"ok": False, "skipped": True}

    inst.change_pcell_parameters(params)
    return {"ok": True, "skipped": False, "pcell": pname, "new_core_width": params.get("core_width")}


def bias_cell_linewidth(
    layout: Any,
    cell: Any,
    core_layers: Sequence[tuple[int, int]],
    bias_um: float,
    bias_x_um: float | None = None,
    bias_y_um: float | None = None,
    preserve_port_faces: bool = True,
    output_layer_map: dict[tuple[int, int], tuple[int, int]] | None = None,
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
    source_region_by_layer: dict[tuple[int, int], Any] | None = None,
    ports_override: Sequence[Port] | None = None,
) -> dict[str, Any]:
    """Apply length-preserving CD linewidth compensation to ``core_layers`` inside ``cell``.

    Args:
        output_layer_map:
          - ``None``: **In-Place** (replaces each layer in ``cell`` directly; if ``cell`` has nested
            sub-cells owning ``core_layers`` and no direct shapes, flattens merged shapes into ``cell``
            only when writing to a new layer, or biases unique owning leaf cells when in-place).
          - ``{(1, 0): (1, 10), ...}``: **Non-Destructive** (reads recursively merged ``core_layers``
            across ``cell``, applies Extend -> Size -> Trim, and writes onto the target output layers
            in ``cell`` without modifying any original shapes or child cells).
    """
    kdb = get_kdb()
    dbu = float(layout.dbu)
    layers_done: list[tuple[tuple[int, int], tuple[int, int], int]] = []
    ports_used = 0

    for cl, cd in core_layers:
        src_ld = (int(cl), int(cd))
        dst_ld = output_layer_map[src_ld] if (output_layer_map and src_ld in output_layer_map) else src_ld
        in_place = (dst_ld == src_ld)

        if source_region_by_layer is not None and src_ld in source_region_by_layer:
            reg = source_region_by_layer[src_ld].merged()
        else:
            li_src = layout.find_layer(*src_ld)
            if li_src is None or li_src < 0:
                continue
            if in_place:
                reg = kdb.Region(cell.shapes(li_src)).merged()
            else:
                reg = kdb.Region(cell.begin_shapes_rec(li_src)).merged()

        if reg.is_empty():
            continue

        if ports_override is not None:
            ports = list(ports_override)
        elif preserve_port_faces:
            ports = resolve_cell_ports(
                layout,
                cell,
                core_layers=(src_ld,),
                pin_layer=pin_layer,
                include_auto_missing=True,
            )
        else:
            ports = []

        biased = bias_region_preserve_length(
            reg,
            bias_um=bias_um,
            dbu=dbu,
            ports=ports,
            bias_x_um=bias_x_um,
            bias_y_um=bias_y_um,
            preserve_port_faces=preserve_port_faces,
        )
        if biased.is_empty():
            continue

        li_dst = layout.layer(*dst_ld)
        cell.shapes(li_dst).clear()
        cell.shapes(li_dst).insert(biased)
        layers_done.append((src_ld, dst_ld, int(biased.count())))
        ports_used = max(ports_used, len(ports))

        # Keep existing PinRec (90/0) port widths automatically synced on in-place CD Bias
        if in_place:
            existing_pins = read_cell_ports(layout, cell, pin_layer=pin_layer)
            bx_val = float(bias_x_um if bias_x_um is not None else bias_um)
            by_val = float(bias_y_um if bias_y_um is not None else bias_um)
            for ep in existing_pins:
                if ep.core_layer != src_ld:
                    continue
                rad = math.radians(float(ep.angle_deg))
                # Transverse waveguide width lies along (-sin θ, cos θ):
                # horizontal WG (0/180 deg) uses by_val; vertical WG (90/270 deg) uses bx_val.
                dw_port = (
                    float(bias_um)
                    if (bias_x_um is None and bias_y_um is None)
                    else (abs(math.sin(rad)) * bx_val + abs(math.cos(rad)) * by_val)
                )
                new_pw = round(float(ep.width) + dw_port, 6)
                if new_pw > 0.02:
                    add_port_to_cell(
                        layout,
                        cell,
                        name=ep.name,
                        x=ep.x,
                        y=ep.y,
                        angle_deg=ep.angle_deg,
                        width=new_pw,
                        core_layer=ep.core_layer,
                        pin_layer=pin_layer,
                        replace_existing=True,
                    )

    if not layers_done:
        return {
            "ok": False,
            "error": f"No shapes found on layer(s) {list(core_layers)} in '{cell.name}'.",
            "layers_done": [],
        }
    return {
        "ok": True,
        "cell_name": cell.name,
        "ports_preserved": ports_used if preserve_port_faces else 0,
        "layers_done": layers_done,
    }


def collect_owning_static_cells(
    layout: Any,
    root_cell: Any,
    core_layers: Sequence[tuple[int, int]],
) -> tuple[list[Any], list[Any]]:
    """Walk hierarchy under ``root_cell`` and return ``(unique_static_cells_with_direct_shapes, pcell_insts)``.

    Guarantees each referenced static cell is returned AT MOST ONCE (preventing double-biasing
    of cells instantiated multiple times like MMI#1 & MMI#2).
    """
    layer_ids = [
        li
        for cl, cd in core_layers
        if (li := layout.find_layer(int(cl), int(cd))) is not None and li >= 0
    ]
    static_cells: list[Any] = []
    pcell_insts: list[Any] = []
    seen_cells: set[int] = set()

    def _visit(cell: Any) -> None:
        cidx = cell.cell_index()
        if cidx in seen_cells:
            return
        seen_cells.add(cidx)
        if any(cell.shapes(li).size() > 0 for li in layer_ids):
            static_cells.append(cell)
        for inst in cell.each_inst():
            if inst.is_pcell() and inst.cell is not None and inst.cell.pcell_name() in ("EulerWaveguide", "EulerBend", "TOPICBend"):
                pcell_insts.append(inst)
            elif inst.cell is not None:
                _visit(inst.cell)

    _visit(root_cell)
    return (static_cells, pcell_insts)
