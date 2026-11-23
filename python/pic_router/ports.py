"""Cell-local PinRec port management, instance-unique world ports, and auto-detection.

100% pure Python 3.10+ stdlib + pya / klayout.db (zero numpy/scipy dependencies).
All port definitions survive GDSII save/reload via explicit PinRec geometry
(2-point DPath + DText) and GDSII integer PROPATTR metadata (keys 10..13).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
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


# GDSII integer PROPATTR keys (0..127 survive GDSII write/read; values are strings on read)
PROP_CORE_LAYER: int = 10
PROP_CORE_DATATYPE: int = 11
PROP_PORT_NAME: int = 12
PROP_INST_ALIAS: int = 13
PROP_PIN_ANCHOR: int = 14

# Flush-inside PinRec marker: DPath([p_in, p_face], w) lies strictly inside the
# waveguide end and flush with the facet at p1 (p_face), so outward direction is
# p1 - p0 and cell bounding boxes never grow by a single DBU.
PIN_ANCHOR_END: str = "end"

DEFAULT_MARKER_LEN: float = 0.5
TEXT_PAIR_TOL_UM: float = 0.05

_NAT_SPLIT_RE = re.compile(r"(\d+)")


def _normalize_angle_deg(angle_deg: float) -> float:
    ang = float(angle_deg) % 360.0
    if abs(ang) < 1e-9 or abs(ang - 360.0) < 1e-9:
        return 0.0
    return ang


def _angle_diff_deg(a_deg: float, b_deg: float) -> float:
    """Smallest absolute angular difference in [0, 180] degrees."""
    diff = (float(a_deg) - float(b_deg) + 180.0) % 360.0 - 180.0
    return abs(diff)


def _natural_sort_key(text: str) -> tuple[Any, ...]:
    parts = _NAT_SPLIT_RE.split(text)
    key: list[Any] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        elif part:
            key.append((1, part))
    return tuple(key)


def _parse_int_prop(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def _parse_str_prop(val: Any) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    return text if text else None


@dataclass
class Port:
    """Photonic port in cell-local or top-cell world coordinates."""

    name: str
    x: float
    y: float
    angle_deg: float
    width: float
    core_layer: tuple[int, int] = (1, 0)
    cell_name: str = ""
    inst_id: str = ""
    inst_path: tuple[int, ...] = ()
    inst_obj: Any = None

    def __post_init__(self) -> None:
        self.name = str(self.name)
        self.x = float(self.x)
        self.y = float(self.y)
        self.angle_deg = _normalize_angle_deg(self.angle_deg)
        self.width = float(self.width)
        self.core_layer = (int(self.core_layer[0]), int(self.core_layer[1]))
        self.cell_name = str(self.cell_name)
        self.inst_id = str(self.inst_id)
        self.inst_path = tuple(int(i) for i in self.inst_path)

    @property
    def full_name(self) -> str:
        return f"{self.inst_id}:{self.name}" if self.inst_id else self.name

    @property
    def direction_vector(self) -> tuple[float, float]:
        rad = math.radians(self.angle_deg)
        return (math.cos(rad), math.sin(rad))

    def __str__(self) -> str:
        return self.full_name


def _extract_path_endpoints(dpath: Any) -> tuple[Any, Any] | None:
    pts = list(dpath.each_point())
    if len(pts) < 2:
        return None
    p0 = pts[0]
    p1 = pts[-1]
    if math.hypot(p1.x - p0.x, p1.y - p0.y) < 1e-12:
        return None
    return (p0, p1)


def _read_cell_pin_shapes(layout: Any, cell: Any, pin_layer: tuple[int, int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read raw DPath and DText pin shapes from a cell's PinRec layer."""
    pin_li = layout.find_layer(int(pin_layer[0]), int(pin_layer[1]))
    if pin_li is None or pin_li < 0:
        return ([], [])

    paths: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []

    for sh in cell.shapes(pin_li).each():
        if sh.is_path():
            dp = sh.dpath
            ep = _extract_path_endpoints(dp)
            if ep is None:
                continue
            p0, p1 = ep
            l_prop = _parse_int_prop(sh.property(PROP_CORE_LAYER))
            d_prop = _parse_int_prop(sh.property(PROP_CORE_DATATYPE))
            core_layer = (l_prop, d_prop) if (l_prop is not None and d_prop is not None) else (1, 0)
            prop_name = _parse_str_prop(sh.property(PROP_PORT_NAME))
            anchor_prop = _parse_str_prop(sh.property(PROP_PIN_ANCHOR))
            anchor_at_end = anchor_prop == PIN_ANCHOR_END
            anchor = p1 if anchor_at_end else p0
            paths.append(
                {
                    "shape": sh,
                    "p0": p0,
                    "p1": p1,
                    "anchor": anchor,
                    "anchor_at_end": anchor_at_end,
                    "width": float(dp.width),
                    "core_layer": core_layer,
                    "prop_name": prop_name,
                }
            )
        elif sh.is_text():
            dt = sh.dtext
            texts.append(
                {
                    "shape": sh,
                    "x": float(dt.x),
                    "y": float(dt.y),
                    "text": str(dt.string).strip(),
                }
            )

    return (paths, texts)


def _pair_pin_records(
    cell_name: str,
    paths: list[dict[str, Any]],
    texts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pair PinRec DPath anchors with nearby DText labels deterministically."""
    used_text_indices: set[int] = set()
    assigned_names: set[str] = set()
    prelim: list[dict[str, Any]] = []

    for rec in paths:
        anchor = rec["anchor"]
        prop_name = rec["prop_name"]
        best_idx: int | None = None
        best_key: tuple[int, float] | None = None

        for tidx, trec in enumerate(texts):
            if tidx in used_text_indices:
                continue
            dist = math.hypot(trec["x"] - anchor.x, trec["y"] - anchor.y)
            if dist <= TEXT_PAIR_TOL_UM + 1e-9:
                name_mismatch = 0 if (prop_name is not None and trec["text"] == prop_name) else 1
                key = (name_mismatch, dist)
                if best_key is None or key < best_key:
                    best_key = key
                    best_idx = tidx

        matched_text_rec = None
        name: str | None = None
        if best_idx is not None:
            used_text_indices.add(best_idx)
            matched_text_rec = texts[best_idx]
            if matched_text_rec["text"]:
                name = matched_text_rec["text"]

        if not name and prop_name:
            name = prop_name

        if name:
            assigned_names.add(name)

        prelim.append(
            {
                "name": name,
                "path_rec": rec,
                "text_rec": matched_text_rec,
            }
        )

    # Assign fallback names p1, p2, ... to any unlabeled DPaths
    fallback_idx = 1
    results: list[dict[str, Any]] = []
    for item in prelim:
        name = item["name"]
        if not name:
            while f"p{fallback_idx}" in assigned_names:
                fallback_idx += 1
            name = f"p{fallback_idx}"
            assigned_names.add(name)
            fallback_idx += 1

        rec = item["path_rec"]
        p0 = rec["p0"]
        p1 = rec["p1"]
        anchor = rec["anchor"]
        angle_deg = _normalize_angle_deg(math.degrees(math.atan2(p1.y - p0.y, p1.x - p0.x)))
        port = Port(
            name=name,
            x=float(anchor.x),
            y=float(anchor.y),
            angle_deg=angle_deg,
            width=rec["width"],
            core_layer=rec["core_layer"],
            cell_name=cell_name,
        )
        results.append(
            {
                "port": port,
                "p0": p0,
                "p1": p1,
                "anchor": anchor,
                "anchor_at_end": bool(rec.get("anchor_at_end", False)),
                "path_shape": rec["shape"],
                "text_shape": item["text_rec"]["shape"] if item["text_rec"] is not None else None,
            }
        )

    results.sort(
        key=lambda r: (
            _natural_sort_key(r["port"].name),
            round(r["port"].x, 6),
            round(r["port"].y, 6),
        )
    )
    return results


def _best_dbu_marker_endpoint(
    x: float,
    y: float,
    angle_deg: float,
    marker_len: float,
    dbu: float,
) -> tuple[float, float]:
    """Choose a DBU-aligned endpoint near ``marker_len`` minimizing angle quantization."""
    rad = math.radians(angle_deg)
    ca = math.cos(rad)
    sa = math.sin(rad)
    if dbu <= 0.0:
        return (x + marker_len * ca, y + marker_len * sa)

    x0_i = round(x / dbu)
    y0_i = round(y / dbu)
    r0 = max(1, round(marker_len / dbu))

    # Fast exact path for cardinal / 45-deg angles or exact grid hits
    dx0 = round(r0 * ca)
    dy0 = round(r0 * sa)
    if dx0 != 0 or dy0 != 0:
        err0 = _angle_diff_deg(math.degrees(math.atan2(dy0, dx0)), angle_deg)
        if err0 < 1e-6:
            return ((x0_i + dx0) * dbu, (y0_i + dy0) * dbu)
    else:
        err0 = 360.0

    best_dx, best_dy = dx0, dy0
    best_err = err0
    span = max(2, int(round(0.05 * r0)))
    for r in range(max(1, r0 - span), r0 + span + 1):
        dx = round(r * ca)
        dy = round(r * sa)
        if dx == 0 and dy == 0:
            continue
        err = _angle_diff_deg(math.degrees(math.atan2(dy, dx)), angle_deg)
        if err < best_err - 1e-12:
            best_err = err
            best_dx, best_dy = dx, dy
            if best_err < 1e-4:
                break

    return ((x0_i + best_dx) * dbu, (y0_i + best_dy) * dbu)


def add_port_to_cell(
    layout: Any,
    cell: Any,
    name: str,
    x: float,
    y: float,
    angle_deg: float,
    width: float,
    core_layer: tuple[int, int] = (1, 0),
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
    marker_len: float = DEFAULT_MARKER_LEN,
    replace_existing: bool = True,
) -> Port:
    """Stamp a cell-local PinRec port (2-point DPath + DText) into ``cell``."""
    kdb = get_kdb()
    name_str = str(name).strip()
    if not name_str:
        raise ValueError("Port name must be a non-empty string")

    cl = (int(core_layer[0]), int(core_layer[1])) if core_layer is not None else (1, 0)
    ang = _normalize_angle_deg(angle_deg)
    dbu = float(layout.dbu)
    w = max(float(width), dbu)
    mlen = float(marker_len) if float(marker_len) > 0.0 else DEFAULT_MARKER_LEN

    if replace_existing:
        remove_port_from_cell(layout, cell, name_str, pin_layer=pin_layer)

    pin_li = layout.layer(int(pin_layer[0]), int(pin_layer[1]))
    # Draw PinRec strictly INSIDE the waveguide end and flush with the facet at
    # p_face = (x, y) so cell bounding boxes never grow past the waveguide face
    # and the vector p_in -> p_face still points strictly outward along `ang`.
    p_face = kdb.DPoint(float(x), float(y))
    inward_ang = _normalize_angle_deg(ang + 180.0)
    p_in_x, p_in_y = _best_dbu_marker_endpoint(float(x), float(y), inward_ang, mlen, dbu)
    p_in = kdb.DPoint(p_in_x, p_in_y)

    dpath = kdb.DPath([p_in, p_face], w)
    path_shape = cell.shapes(pin_li).insert(dpath)
    path_shape.set_property(PROP_CORE_LAYER, cl[0])
    path_shape.set_property(PROP_CORE_DATATYPE, cl[1])
    path_shape.set_property(PROP_PORT_NAME, name_str)
    path_shape.set_property(PROP_PIN_ANCHOR, PIN_ANCHOR_END)

    dtext = kdb.DText(name_str, kdb.DTrans(float(x), float(y)))
    cell.shapes(pin_li).insert(dtext)

    return Port(
        name=name_str,
        x=float(x),
        y=float(y),
        angle_deg=ang,
        width=w,
        core_layer=cl,
        cell_name=cell.name,
    )


def remove_port_from_cell(
    layout: Any,
    cell: Any,
    name: str,
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
) -> bool:
    """Remove all PinRec shapes belonging to port ``name`` in ``cell``."""
    pin_li = layout.find_layer(int(pin_layer[0]), int(pin_layer[1]))
    if pin_li is None or pin_li < 0:
        return False

    target_name = str(name).strip()
    paths, texts = _read_cell_pin_shapes(layout, cell, pin_layer)
    paired = _pair_pin_records(cell.name, paths, texts)

    shapes_to_erase: list[Any] = []
    seen_ids: set[int] = set()

    for rec in paired:
        if rec["port"].name == target_name:
            ps = rec["path_shape"]
            shapes_to_erase.append(ps)
            seen_ids.add(id(ps))
            ts = rec["text_shape"]
            if ts is not None:
                shapes_to_erase.append(ts)
                seen_ids.add(id(ts))

    # Also clean up any orphan DText on pin_layer with exact matching name
    for trec in texts:
        if trec["text"] == target_name and id(trec["shape"]) not in seen_ids:
            shapes_to_erase.append(trec["shape"])
            seen_ids.add(id(trec["shape"]))

    if not shapes_to_erase:
        return False

    layer_shapes = cell.shapes(pin_li)
    for sh in shapes_to_erase:
        layer_shapes.erase(sh)
    return True


def read_cell_ports(
    layout: Any,
    cell: Any,
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
) -> list[Port]:
    """Read all cell-local PinRec ports from ``cell``."""
    paths, texts = _read_cell_pin_shapes(layout, cell, pin_layer)
    paired = _pair_pin_records(cell.name, paths, texts)
    return [rec["port"] for rec in paired]


def _read_cell_port_records(
    layout: Any,
    cell: Any,
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
) -> list[dict[str, Any]]:
    paths, texts = _read_cell_pin_shapes(layout, cell, pin_layer)
    return _pair_pin_records(cell.name, paths, texts)


def set_instance_alias(inst: Any, alias: str | None) -> None:
    """Attach a persistent GDSII integer-property alias (key 13) to an Instance."""
    text = str(alias).strip() if alias is not None else ""
    if text:
        inst.set_property(PROP_INST_ALIAS, text)
    elif hasattr(inst, "delete_property"):
        inst.delete_property(PROP_INST_ALIAS)
    else:
        inst.set_property(PROP_INST_ALIAS, "")


def get_instance_alias(inst: Any) -> str | None:
    """Read the persistent GDSII instance alias (property key 13), if set."""
    return _parse_str_prop(inst.property(PROP_INST_ALIAS))


def _expand_cell_instances(cell: Any) -> list[dict[str, Any]]:
    """Expand direct child instances (including regular arrays) with deterministic IDs."""
    kdb = get_kdb()
    entries: list[dict[str, Any]] = []

    for inst in cell.each_inst():
        child_cell = inst.cell
        if child_cell is None:
            continue
        alias = get_instance_alias(inst)
        base_trans = inst.dcplx_trans

        if inst.is_regular_array() and (int(inst.na) > 1 or int(inst.nb) > 1):
            na = max(1, int(inst.na))
            nb = max(1, int(inst.nb))
            da = inst.da
            db = inst.db
            for ia in range(na):
                for ib in range(nb):
                    arr_vec = kdb.DVector(ia * da.x + ib * db.x, ia * da.y + ib * db.y)
                    elem_trans = kdb.DCplxTrans(arr_vec) * base_trans
                    entries.append(
                        {
                            "inst": inst,
                            "cell": child_cell,
                            "cell_name": child_cell.name,
                            "trans": elem_trans,
                            "alias": alias,
                            "array_idx": (ia, ib),
                            "is_array": True,
                        }
                    )
        else:
            entries.append(
                {
                    "inst": inst,
                    "cell": child_cell,
                    "cell_name": child_cell.name,
                    "trans": base_trans,
                    "alias": alias,
                    "array_idx": (0, 0),
                    "is_array": False,
                }
            )

    def _entry_sort_key(e: dict[str, Any]) -> tuple[Any, ...]:
        t = e["trans"]
        return (
            e["cell_name"],
            round(float(t.disp.x), 6),
            round(float(t.disp.y), 6),
            round(_normalize_angle_deg(t.angle), 6),
            1 if t.is_mirror() else 0,
            e["array_idx"],
        )

    entries.sort(key=_entry_sort_key)

    # Assign unique deterministic inst_id: custom alias (prop 13) if set, else "<cell_name>#<idx>"
    used_ids: set[str] = set()
    cell_counters: dict[str, int] = {}

    for det_idx, entry in enumerate(entries):
        entry["det_idx"] = det_idx
        alias = entry["alias"]
        if alias:
            base_id = (
                f"{alias}[{entry['array_idx'][0]},{entry['array_idx'][1]}]"
                if entry["is_array"]
                else alias
            )
            cand_id = base_id
            suffix = 2
            while cand_id in used_ids:
                cand_id = f"{base_id}#{suffix}"
                suffix += 1
        else:
            cname = entry["cell_name"]
            cnt = cell_counters.get(cname, 0) + 1
            cand_id = f"{cname}#{cnt}"
            while cand_id in used_ids:
                cnt += 1
                cand_id = f"{cname}#{cnt}"
            cell_counters[cname] = cnt

        used_ids.add(cand_id)
        entry["local_id"] = cand_id

    return entries


def _transform_port_record(
    rec: dict[str, Any],
    acc_trans: Any,
    inst_id: str,
    inst_path: tuple[int, ...],
    top_inst_obj: Any,
) -> Port:
    """Transform a cell-local PinRec record via full DCplxTrans (exact under rot + mirx + mag)."""
    local_port: Port = rec["port"]
    wp0 = acc_trans * rec["p0"]
    wp1 = acc_trans * rec["p1"]
    w_anchor = wp1 if rec.get("anchor_at_end", False) else wp0
    world_angle = _normalize_angle_deg(math.degrees(math.atan2(wp1.y - wp0.y, wp1.x - wp0.x)))
    world_width = local_port.width * float(acc_trans.mag)
    return Port(
        name=local_port.name,
        x=float(w_anchor.x),
        y=float(w_anchor.y),
        angle_deg=world_angle,
        width=world_width,
        core_layer=local_port.core_layer,
        cell_name=local_port.cell_name,
        inst_id=inst_id,
        inst_path=inst_path,
        inst_obj=top_inst_obj,
    )


def collect_world_ports(
    layout: Any,
    top_cell: Any,
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
    max_depth: int = 8,
) -> list[Port]:
    """Collect all PinRec ports in ``top_cell`` and its instance hierarchy in world coordinates."""
    kdb = get_kdb()
    world_ports: list[Port] = []
    local_cache: dict[int, list[dict[str, Any]]] = {}

    def _get_local_records(c: Any) -> list[dict[str, Any]]:
        ci = c.cell_index()
        if ci not in local_cache:
            local_cache[ci] = _read_cell_port_records(layout, c, pin_layer=pin_layer)
        return local_cache[ci]

    # 1. Direct PinRec ports placed on top_cell itself
    for rec in _get_local_records(top_cell):
        p = rec["port"]
        world_ports.append(
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

    # 2. Recursive traversal of instances
    def _walk(
        curr_cell: Any,
        acc_trans: Any,
        parent_id: str,
        parent_path: tuple[int, ...],
        top_inst_obj: Any,
        depth: int,
    ) -> None:
        if depth > max_depth:
            return
        for entry in _expand_cell_instances(curr_cell):
            child_cell = entry["cell"]
            child_trans = acc_trans * entry["trans"]
            local_id = entry["local_id"]
            full_id = f"{parent_id}/{local_id}" if parent_id else local_id
            curr_path = parent_path + (entry["det_idx"],)
            curr_top_inst = entry["inst"] if depth == 1 else top_inst_obj

            for rec in _get_local_records(child_cell):
                world_ports.append(
                    _transform_port_record(
                        rec,
                        child_trans,
                        inst_id=full_id,
                        inst_path=curr_path,
                        top_inst_obj=curr_top_inst,
                    )
                )

            if depth < max_depth and child_cell.child_instances() > 0:
                _walk(
                    child_cell,
                    child_trans,
                    full_id,
                    curr_path,
                    curr_top_inst,
                    depth + 1,
                )

    if max_depth >= 1:
        _walk(top_cell, kdb.DCplxTrans(), "", (), None, 1)

    return world_ports


# ---------------------------------------------------------------------------
# Smart Auto-Port Detector
# ---------------------------------------------------------------------------

def _region_contains_point_um(region: Any, x_um: float, y_um: float, dbu: float, kdb: Any) -> bool:
    """Fast point-in-Region check using C++ clipped box intersection (avoids copying large polygons)."""
    px = round(x_um / dbu)
    py = round(y_um / dbu)
    probe = kdb.Region(kdb.Box(px - 1, py - 1, px + 1, py + 1))
    return not (region & probe).is_empty()


def _poly_contains_point_um(poly: Any, x_um: float, y_um: float, dbu: float, kdb: Any) -> bool:
    return bool(poly.inside(kdb.Point(round(x_um / dbu), round(y_um / dbu))))


def _local_poly_contains_point_um(local_polys: Sequence[Any], x_um: float, y_um: float, dbu: float, kdb: Any) -> bool:
    pt = kdb.Point(round(x_um / dbu), round(y_um / dbu))
    for lp in local_polys:
        if lp.inside(pt):
            return True
    return False


def _collect_collinear_side(
    pts: list[tuple[int, int]],
    start_idx: int,
    step: int,
    dbu: float,
    cos_collinear: float = 0.9998477,  # cos(1.0 deg)
    max_steps: int = 32,
) -> tuple[float, float, float]:
    """Walk from ``start_idx`` along ``step`` (+1 or -1) merging collinear segments.

    Returns ``(vx_um, vy_um, length_um)`` directed away from ``pts[start_idx]``.
    """
    n = len(pts)
    sx, sy = pts[start_idx]
    curr_idx = start_idx
    ref_ux = 0.0
    ref_uy = 0.0
    end_x, end_y = sx, sy

    for _ in range(min(max_steps, n - 1)):
        nxt_idx = (curr_idx + step) % n
        nx, ny = pts[nxt_idx]
        dx = nx - pts[curr_idx][0]
        dy = ny - pts[curr_idx][1]
        seg_len = math.hypot(dx, dy)
        if seg_len < 1e-9:
            curr_idx = nxt_idx
            continue
        ux = dx / seg_len
        uy = dy / seg_len
        if ref_ux == 0.0 and ref_uy == 0.0:
            ref_ux, ref_uy = ux, uy
            end_x, end_y = nx, ny
            curr_idx = nxt_idx
        else:
            if ux * ref_ux + uy * ref_uy >= cos_collinear:
                end_x, end_y = nx, ny
                curr_idx = nxt_idx
            else:
                break

    vx = (end_x - sx) * dbu
    vy = (end_y - sy) * dbu
    return (vx, vy, math.hypot(vx, vy))


def _detect_endcaps_in_regions(
    merged_regions: dict[tuple[int, int], Any],
    core_layers: Sequence[tuple[int, int]],
    cell_name: str,
    dbu: float,
    min_width: float,
    max_width: float,
    min_adj_len: float = 0.05,
    max_perp_err_deg: float = 12.0,
    max_side_div_deg: float = 15.0,
) -> list[dict[str, Any]]:
    """Detect candidate waveguide end-face ports across merged core Regions."""
    kdb = get_kdb()
    cos_perp = math.cos(math.radians(max_perp_err_deg))
    cos_side_div = math.cos(math.radians(max_side_div_deg))
    tan_side_tol = math.tan(math.radians(max_perp_err_deg + 1.0))
    # Fast corner angle gate: smooth curve chords turn by ~0..15 deg (cos^2 > 0.9), whereas
    # waveguide endcap corners turn by 90 +- max_perp_err_deg (cos^2 <= sin^2(max_perp_err_deg)).
    cos_corner_gate2 = math.sin(math.radians(min(45.0, max_perp_err_deg + 8.0))) ** 2

    min_w_dbu = max(1, int(math.floor((min_width - 1e-6) / dbu)))
    max_w_dbu = int(math.ceil((max_width + 1e-6) / dbu))
    min_w_dbu2 = min_w_dbu * min_w_dbu
    max_w_dbu2 = max_w_dbu * max_w_dbu

    candidates: list[dict[str, Any]] = []
    layer_order = {lp: idx for idx, lp in enumerate(core_layers)}

    for layer_pair in core_layers:
        reg = merged_regions.get(layer_pair)
        if reg is None or reg.is_empty():
            continue

        for poly in reg.each():
            pts = [(p.x, p.y) for p in poly.each_point_hull()]
            n = len(pts)
            if n < 3:
                continue

            # Signed doubled area: > 0 => CCW, < 0 => CW
            area2 = 0
            x_prev, y_prev = pts[-1]
            for x_curr, y_curr in pts:
                area2 += x_prev * y_curr - x_curr * y_prev
                x_prev, y_prev = x_curr, y_curr
            if area2 == 0:
                continue
            orient = 1.0 if area2 > 0 else -1.0
            is_heavy_poly = n > 512
            poly_reg = kdb.Region(poly) if is_heavy_poly else None

            for i in range(n):
                x0, y0 = pts[i]
                j = i + 1 if i + 1 < n else 0
                x1, y1 = pts[j]
                dx = x1 - x0
                dy = y1 - y0

                # Fast integer DBU rejection before any floating-point math
                if dx > max_w_dbu or dx < -max_w_dbu or dy > max_w_dbu or dy < -max_w_dbu:
                    continue
                d2 = dx * dx + dy * dy
                if d2 < min_w_dbu2 or d2 > max_w_dbu2:
                    continue

                # Reject smooth curve/spiral chords in 4 integer ops before sqrt/collinear walk:
                # any adjacent segment >= 8 DBU must turn by ~90 deg, never nearly collinear.
                xp0, yp0 = pts[i - 1]
                dx_p = x0 - xp0
                dy_p = y0 - yp0
                d2_p = dx_p * dx_p + dy_p * dy_p
                if d2_p >= 64:
                    dot_p = dx_p * dx + dy_p * dy
                    if dot_p * dot_p > cos_corner_gate2 * d2_p * d2:
                        continue
                xn1, yn1 = pts[j + 1 if j + 1 < n else 0]
                dx_n = xn1 - x1
                dy_n = yn1 - y1
                d2_n = dx_n * dx_n + dy_n * dy_n
                if d2_n >= 64:
                    dot_n = dx_n * dx + dy_n * dy
                    if dot_n * dot_n > cos_corner_gate2 * d2_n * d2:
                        continue

                L = math.sqrt(d2) * dbu
                if L < min_width - 1e-6 or L > max_width + 1e-6:
                    continue

                # Candidate outward normal: right normal for CCW, left normal for CW
                tx = (dx * dbu) / L
                ty = (dy * dbu) / L
                nx = orient * ty
                ny = -orient * tx
                inx = -nx
                iny = -ny

                # Check adjacent side-wall edges pointing inward
                v1x, v1y, L1 = _collect_collinear_side(pts, i, -1, dbu)
                if L1 < min_adj_len - 1e-9:
                    continue
                v2x, v2y, L2 = _collect_collinear_side(pts, j, +1, dbu)
                if L2 < min_adj_len - 1e-9:
                    continue

                dot1 = (v1x * inx + v1y * iny) / L1
                if dot1 < cos_perp:
                    continue
                dot2 = (v2x * inx + v2y * iny) / L2
                if dot2 < cos_perp:
                    continue
                dot12 = (v1x * v2x + v1y * v2y) / (L1 * L2)
                if dot12 < cos_side_div:
                    continue

                mx = 0.5 * (x0 + x1) * dbu
                my = 0.5 * (y0 + y1) * dbu
                eps = max(min(0.05, 0.25 * L, 0.5 * min(L1, L2)), max(3.0 * dbu, 0.005))

                # Verify outward is empty and inward is material
                p_out_x = mx + eps * nx
                p_out_y = my + eps * ny
                p_in_x = mx - eps * nx
                p_in_y = my - eps * ny

                if _region_contains_point_um(reg, p_out_x, p_out_y, dbu, kdb):
                    continue

                if is_heavy_poly:
                    # Clip 100k+ vertex polygon once to a tiny local box around candidate facet
                    pad_dbu = int(math.ceil((L + 1.0) / dbu))
                    mx_i = round(mx / dbu)
                    my_i = round(my / dbu)
                    loc_clip = poly_reg & kdb.Region(
                        kdb.Box(mx_i - pad_dbu, my_i - pad_dbu, mx_i + pad_dbu, my_i + pad_dbu)
                    )
                    loc_polys = list(loc_clip.each())
                    if not _local_poly_contains_point_um(loc_polys, p_in_x, p_in_y, dbu, kdb):
                        continue
                    d_in = max(eps, min(0.10, 0.8 * min(L1, L2)))
                    cx = mx - d_in * nx
                    cy = my - d_in * ny
                    if not _local_poly_contains_point_um(loc_polys, cx, cy, dbu, kdb):
                        continue
                    inner_half = 0.30 * L
                    if not (
                        _local_poly_contains_point_um(loc_polys, cx + inner_half * tx, cy + inner_half * ty, dbu, kdb)
                        and _local_poly_contains_point_um(loc_polys, cx - inner_half * tx, cy - inner_half * ty, dbu, kdb)
                    ):
                        continue
                    side_off = 0.5 * L + max(0.03, 0.08 * L + d_in * tan_side_tol)
                    if (
                        _local_poly_contains_point_um(loc_polys, cx + side_off * tx, cy + side_off * ty, dbu, kdb)
                        or _local_poly_contains_point_um(loc_polys, cx - side_off * tx, cy - side_off * ty, dbu, kdb)
                    ):
                        continue
                else:
                    if not _poly_contains_point_um(poly, p_in_x, p_in_y, dbu, kdb):
                        continue
                    # Verify local waveguide strip width inward (rejects notches/staircases)
                    d_in = max(eps, min(0.10, 0.8 * min(L1, L2)))
                    cx = mx - d_in * nx
                    cy = my - d_in * ny
                    if not _poly_contains_point_um(poly, cx, cy, dbu, kdb):
                        continue
                    inner_half = 0.30 * L
                    if not (
                        _poly_contains_point_um(poly, cx + inner_half * tx, cy + inner_half * ty, dbu, kdb)
                        and _poly_contains_point_um(poly, cx - inner_half * tx, cy - inner_half * ty, dbu, kdb)
                    ):
                        continue
                    side_off = 0.5 * L + max(0.03, 0.08 * L + d_in * tan_side_tol)
                    if (
                        _poly_contains_point_um(poly, cx + side_off * tx, cy + side_off * ty, dbu, kdb)
                        or _poly_contains_point_um(poly, cx - side_off * tx, cy - side_off * ty, dbu, kdb)
                    ):
                        continue

                angle_deg = _normalize_angle_deg(math.degrees(math.atan2(ny, nx)))
                candidates.append(
                    {
                        "x": mx,
                        "y": my,
                        "angle_deg": angle_deg,
                        "width": L,
                        "core_layer": layer_pair,
                        "layer_rank": layer_order.get(layer_pair, 99),
                        "nx": nx,
                        "ny": ny,
                        "eps": eps,
                        "p0": (x0 * dbu, y0 * dbu),
                        "p1": (x1 * dbu, y1 * dbu),
                        "cell_name": cell_name,
                    }
                )

    # Cross-layer filtering when multiple core layers exist
    if len(merged_regions) > 1 and candidates:
        # 1. Suppress pedestal ends where another core layer crosses continuously across the face
        filtered: list[dict[str, Any]] = []
        for cand in candidates:
            crossed = False
            mx, my = cand["x"], cand["y"]
            nx, ny, eps = cand["nx"], cand["ny"], cand["eps"]
            for other_lp, other_reg in merged_regions.items():
                if other_lp == cand["core_layer"]:
                    continue
                if _region_contains_point_um(other_reg, mx + eps * nx, my + eps * ny, dbu, kdb) and (
                    _region_contains_point_um(other_reg, mx - eps * nx, my - eps * ny, dbu, kdb)
                ):
                    crossed = True
                    break
            if not crossed:
                filtered.append(cand)

        # 2. Deduplicate co-located multi-layer ports (e.g. a narrow rib core over a wider slab pedestal)
        filtered.sort(key=lambda c: (round(c["width"], 4), c["layer_rank"]))
        deduped: list[dict[str, Any]] = []
        for cand in filtered:
            duplicate = False
            for kept in deduped:
                if (
                    math.hypot(cand["x"] - kept["x"], cand["y"] - kept["y"]) <= 0.10
                    and _angle_diff_deg(cand["angle_deg"], kept["angle_deg"]) <= 10.0
                ):
                    duplicate = True
                    break
            if not duplicate:
                deduped.append(cand)
        candidates = deduped

    return candidates


def auto_detect_ports(
    layout: Any,
    cell: Any,
    core_layers: Sequence[tuple[int, int]] = ((1, 0), (2, 0)),
    min_width: float = 0.15,
    max_width: float = 5.0,
    include_hierarchy: bool = True,
) -> list[Port]:
    """Automatically detect waveguide end-face ports on ``core_layers`` in ``cell``."""
    kdb = get_kdb()
    dbu = float(layout.dbu)
    norm_layers = [(int(lp[0]), int(lp[1])) for lp in core_layers]
    merged_regions: dict[tuple[int, int], Any] = {}

    for lp in norm_layers:
        li = layout.find_layer(lp[0], lp[1])
        if li is None or li < 0:
            continue
        reg = (
            kdb.Region(cell.begin_shapes_rec(li))
            if include_hierarchy
            else kdb.Region(cell.shapes(li))
        )
        if reg.is_empty():
            continue
        reg.merge()
        merged_regions[lp] = reg

    if not merged_regions:
        return []

    candidates = _detect_endcaps_in_regions(
        merged_regions=merged_regions,
        core_layers=norm_layers,
        cell_name=cell.name,
        dbu=dbu,
        min_width=float(min_width),
        max_width=float(max_width),
    )

    # Deterministic ordering: West/leftmost first, then by y, then angle & core layer
    candidates.sort(
        key=lambda c: (
            round(c["x"], 3),
            round(c["y"], 3),
            round(c["angle_deg"], 2),
            c["core_layer"],
        )
    )

    ports: list[Port] = []
    for idx, cand in enumerate(candidates, start=1):
        ports.append(
            Port(
                name=f"o{idx}",
                x=cand["x"],
                y=cand["y"],
                angle_deg=cand["angle_deg"],
                width=cand["width"],
                core_layer=cand["core_layer"],
                cell_name=cell.name,
            )
        )
    return ports


def tag_detected_ports(
    layout: Any,
    cell: Any,
    core_layers: Sequence[tuple[int, int]] = ((1, 0), (2, 0)),
    pin_layer: tuple[int, int] = DEFAULT_PIN_LAYER,
    overwrite: bool = False,
    min_width: float = 0.15,
    max_width: float = 5.0,
    include_hierarchy: bool = True,
    marker_len: float = DEFAULT_MARKER_LEN,
) -> list[Port]:
    """Detect waveguide ports on ``cell`` and stamp persistent PinRec markers."""
    detected = auto_detect_ports(
        layout,
        cell,
        core_layers=core_layers,
        min_width=min_width,
        max_width=max_width,
        include_hierarchy=include_hierarchy,
    )

    if overwrite:
        existing = read_cell_ports(layout, cell, pin_layer=pin_layer)
        # Clear all stale/orphan shapes on pin_layer in this cell before re-stamping fresh ports
        li_pin = layout.find_layer(int(pin_layer[0]), int(pin_layer[1]))
        if li_pin is not None and li_pin >= 0:
            cell.shapes(li_pin).clear()
        # Preserve custom (non-default oN) port names if a refreshed port sits at the same face
        used_names: set[str] = set()
        resolved_names: list[str] = []
        for p in detected:
            matched_custom: str | None = None
            best_d = 0.50
            for ex in existing:
                if ex.name in used_names:
                    continue
                is_default_o = ex.name.startswith("o") and ex.name[1:].isdigit()
                if is_default_o:
                    continue
                d = math.hypot(p.x - ex.x, p.y - ex.y)
                if d <= best_d and _angle_diff_deg(p.angle_deg, ex.angle_deg) <= 10.0:
                    best_d = d
                    matched_custom = ex.name
            if matched_custom is not None:
                used_names.add(matched_custom)
                resolved_names.append(matched_custom)
            else:
                resolved_names.append("")

        next_idx = 1
        for i, p in enumerate(detected):
            if not resolved_names[i]:
                cand_name = p.name
                if cand_name in used_names:
                    while f"o{next_idx}" in used_names:
                        next_idx += 1
                    cand_name = f"o{next_idx}"
                    next_idx += 1
                used_names.add(cand_name)
                resolved_names[i] = cand_name

        stamped: list[Port] = []
        for p, name in zip(detected, resolved_names):
            stamped.append(
                add_port_to_cell(
                    layout,
                    cell,
                    name=name,
                    x=p.x,
                    y=p.y,
                    angle_deg=p.angle_deg,
                    width=p.width,
                    core_layer=p.core_layer,
                    pin_layer=pin_layer,
                    marker_len=marker_len,
                    replace_existing=True,
                )
            )
        return stamped

    existing_recs = _read_cell_port_records(layout, cell, pin_layer=pin_layer)
    # Upgrade any legacy outward-protruding PinRec markers to flush-inside markers
    # while keeping their existing port names and metadata intact.
    upgraded_any = False
    for rec in existing_recs:
        if not rec.get("anchor_at_end", False):
            ex = rec["port"]
            add_port_to_cell(
                layout,
                cell,
                name=ex.name,
                x=ex.x,
                y=ex.y,
                angle_deg=ex.angle_deg,
                width=ex.width,
                core_layer=ex.core_layer,
                pin_layer=pin_layer,
                marker_len=marker_len,
                replace_existing=True,
            )
            upgraded_any = True
    existing = read_cell_ports(layout, cell, pin_layer=pin_layer) if upgraded_any else [r["port"] for r in existing_recs]
    used_names = {p.name for p in existing}
    next_idx = 1

    for cand in detected:
        already = any(
            math.hypot(cand.x - ex.x, cand.y - ex.y) <= 0.10
            and _angle_diff_deg(cand.angle_deg, ex.angle_deg) <= 10.0
            for ex in existing
        )
        if already:
            continue
        name = cand.name
        if name in used_names:
            while f"o{next_idx}" in used_names:
                next_idx += 1
            name = f"o{next_idx}"
            next_idx += 1
        used_names.add(name)
        add_port_to_cell(
            layout,
            cell,
            name=name,
            x=cand.x,
            y=cand.y,
            angle_deg=cand.angle_deg,
            width=cand.width,
            core_layer=cand.core_layer,
            pin_layer=pin_layer,
            marker_len=marker_len,
            replace_existing=False,
        )

    return read_cell_ports(layout, cell, pin_layer=pin_layer)


def _point_to_segment_dist(
    px: float, py: float, x0: float, y0: float, x1: float, y1: float
) -> float:
    vx = x1 - x0
    vy = y1 - y0
    l2 = vx * vx + vy * vy
    if l2 <= 1e-18:
        return math.hypot(px - x0, py - y0)
    t = ((px - x0) * vx + (py - y0) * vy) / l2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return math.hypot(px - (x0 + t * vx), py - (y0 + t * vy))


def detect_port_near_point(
    layout: Any,
    cell: Any,
    x: float,
    y: float,
    search_radius: float = 5.0,
    core_layers: Sequence[tuple[int, int]] = ((1, 0), (2, 0)),
    min_width: float = 0.15,
    max_width: float = 5.0,
    include_hierarchy: bool = True,
) -> Port | None:
    """Find the nearest waveguide end-face port near ``(x, y)`` for interactive GUI tagging."""
    kdb = get_kdb()
    dbu = float(layout.dbu)
    x = float(x)
    y = float(y)
    sr = max(float(search_radius), 0.1)
    norm_layers = [(int(lp[0]), int(lp[1])) for lp in core_layers]

    pad = max(sr * 2.0, float(max_width) * 2.0, 20.0)
    qbox = kdb.Box(
        round((x - pad) / dbu),
        round((y - pad) / dbu),
        round((x + pad) / dbu),
        round((y + pad) / dbu),
    )

    merged_regions: dict[tuple[int, int], Any] = {}
    for lp in norm_layers:
        li = layout.find_layer(lp[0], lp[1])
        if li is None or li < 0:
            continue
        if include_hierarchy:
            it = cell.begin_shapes_rec_overlapping(li, qbox)
            reg = kdb.Region(it)
        else:
            reg = kdb.Region()
            for sh in cell.shapes(li).each_overlapping(qbox):
                if sh.is_polygon() or sh.is_box() or sh.is_path():
                    reg.insert(sh.polygon)
        if reg.is_empty():
            continue
        reg.merge()
        merged_regions[lp] = reg

    if not merged_regions:
        return None

    # 1. Prefer strict waveguide endcap ports within search_radius
    candidates = _detect_endcaps_in_regions(
        merged_regions=merged_regions,
        core_layers=norm_layers,
        cell_name=cell.name,
        dbu=dbu,
        min_width=float(min_width),
        max_width=float(max_width),
    )

    best_cand: dict[str, Any] | None = None
    best_dist = float("inf")
    for cand in candidates:
        p0x, p0y = cand["p0"]
        p1x, p1y = cand["p1"]
        seg_dist = _point_to_segment_dist(x, y, p0x, p0y, p1x, p1y)
        mid_dist = math.hypot(x - cand["x"], y - cand["y"])
        d = min(seg_dist, mid_dist)
        if d <= sr + 1e-9 and d < best_dist:
            best_dist = d
            best_cand = cand

    if best_cand is not None:
        return Port(
            name="o1",
            x=best_cand["x"],
            y=best_cand["y"],
            angle_deg=best_cand["angle_deg"],
            width=best_cand["width"],
            core_layer=best_cand["core_layer"],
            cell_name=cell.name,
        )

    # 2. Fallback: nearest exterior polygon edge of plausible width within search_radius
    max_fb_w = max(float(max_width), 15.0)
    min_fb_w = min(float(min_width), 0.10)
    best_edge: tuple[float, float, float, float, tuple[int, int]] | None = None
    best_edge_dist = float("inf")

    for lp in norm_layers:
        reg = merged_regions.get(lp)
        if reg is None or reg.is_empty():
            continue
        for poly in reg.each():
            pts = [(p.x * dbu, p.y * dbu) for p in poly.each_point_hull()]
            n = len(pts)
            if n < 3:
                continue
            area2 = 0.0
            xp, yp = pts[-1]
            for xc, yc in pts:
                area2 += xp * yc - xc * yp
                xp, yp = xc, yc
            if abs(area2) < 1e-12:
                continue
            orient = 1.0 if area2 > 0 else -1.0

            for i in range(n):
                x0, y0 = pts[i]
                x1, y1 = pts[(i + 1) % n]
                L = math.hypot(x1 - x0, y1 - y0)
                if not (min_fb_w <= L <= max_fb_w):
                    continue
                d = _point_to_segment_dist(x, y, x0, y0, x1, y1)
                if d > sr + 1e-9 or d >= best_edge_dist:
                    continue
                nx = orient * (y1 - y0) / L
                ny = -orient * (x1 - x0) / L
                mx = 0.5 * (x0 + x1)
                my = 0.5 * (y0 + y1)
                eps = max(min(0.05, 0.25 * L), 3.0 * dbu)
                if _region_contains_point_um(reg, mx + eps * nx, my + eps * ny, dbu, kdb):
                    continue
                if not _poly_contains_point_um(poly, mx - eps * nx, my - eps * ny, dbu, kdb):
                    continue
                ang = _normalize_angle_deg(math.degrees(math.atan2(ny, nx)))
                best_edge_dist = d
                best_edge = (mx, my, ang, L, lp)

    if best_edge is None:
        return None

    return Port(
        name="o1",
        x=best_edge[0],
        y=best_edge[1],
        angle_deg=best_edge[2],
        width=best_edge[3],
        core_layer=best_edge[4],
        cell_name=cell.name,
    )
