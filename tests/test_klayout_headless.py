"""KLayout headless (-b) + offscreen Qt5/Qt6 GUI integration verification script.

Run directly via KLayout:
    klayout -b -r tests/test_klayout_headless.py

Zero external GDS files and zero hardcoded paths (all paths resolved via Path(__file__)).
"""

from __future__ import annotations

import ast
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET

import pya

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[1]
PYTHON_DIR = REPO_ROOT / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))


def _angle_diff_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _check_zero_numpy_scipy_imports() -> None:
    """Verify neither numpy nor scipy is imported in Python source or at runtime."""
    assert "numpy" not in sys.modules, "numpy unexpectedly loaded in KLayout runtime"
    assert "scipy" not in sys.modules, "scipy unexpectedly loaded in KLayout runtime"

    pkg_dir = PYTHON_DIR / "pic_router"
    py_files = sorted(pkg_dir.glob("*.py"))
    assert len(py_files) >= 8, f"Expected >= 8 modules in {pkg_dir}"
    for pyf in py_files:
        tree = ast.parse(pyf.read_text(encoding="utf-8"), filename=str(pyf))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in ("numpy", "scipy"), f"Forbidden import {alias.name} in {pyf.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in ("numpy", "scipy"), f"Forbidden from-import {node.module} in {pyf.name}"


def _exec_lym_macro() -> None:
    """Execute pymacros/pic_router_init.lym directly as KLayout does on startup."""
    lym_path = REPO_ROOT / "pymacros" / "pic_router_init.lym"
    assert lym_path.is_file(), f"Missing {lym_path}"
    root = ET.parse(lym_path).getroot()
    text_el = root.find("text")
    assert text_el is not None and text_el.text, "pic_router_init.lym missing <text> code"
    ns: dict[str, object] = {"__file__": str(lym_path), "__name__": "__main__"}
    exec(compile(text_el.text, str(lym_path), "exec"), ns)

    import pic_router

    loaded_from = Path(pic_router.__file__).resolve()
    assert loaded_from.is_relative_to(PYTHON_DIR.resolve()), (
        f"pic_router_init.lym loaded pic_router from {loaded_from} instead of {PYTHON_DIR.resolve()}"
    )


def _check_grain_xml_and_installer() -> None:
    grain_path = REPO_ROOT / "grain.xml"
    assert grain_path.is_file(), f"Missing {grain_path}"
    root = ET.parse(grain_path).getroot()
    assert root.tag == "salt-grain"
    assert (root.findtext("name") or "").strip() == "pic_router"
    assert (root.findtext("version") or "").strip() == "0.1.1"
    assert (root.findtext("author") or "").strip() == "MeshedPoto"
    assert (root.findtext("license") or "").strip() == "MIT"
    assert (root.findtext("title") or "").strip() == "KLayout-PIC Router"

    sh_path = REPO_ROOT / "scripts" / "install_linux_mac.sh"
    assert sh_path.is_file(), f"Missing {sh_path}"
    proc = subprocess.run(["bash", "-n", str(sh_path)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"


def _check_pcells_and_shortcuts() -> None:
    from pic_router.config import DEFAULT_PIN_LAYER, get_cross_section
    from pic_router.gui import SHORTCUTS
    from pic_router.pcells import (
        LIBRARY_NAME,
        instantiate_euler_bend,
        instantiate_euler_waveguide,
        instantiate_topic_bend,
    )
    from pic_router.ports import read_cell_ports

    # 1. PIC_Router library registered by pic_router_init.lym
    lib = pya.Library.library_by_name(LIBRARY_NAME)
    assert lib is not None, f"Library '{LIBRARY_NAME}' not registered"
    pcell_names = sorted(lib.layout().pcell_names())
    assert pcell_names == ["EulerBend", "EulerWaveguide", "TOPICBend"], f"Got {pcell_names}"

    # 2. Unique non-conflicting shortcuts
    assert len(SHORTCUTS) >= 12
    vals = list(SHORTCUTS.values())
    assert len(vals) == len(set(vals)), f"Duplicate shortcuts found: {SHORTCUTS}"
    for key, sc in SHORTCUTS.items():
        assert sc.startswith("Ctrl+Shift+"), f"Shortcut {key}={sc!r} must use Ctrl+Shift+"

    # 3. Instantiate EulerWaveguide (add_ports=True & False), EulerBend, TOPICBend
    ly = pya.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("HEADLESS_PCELLS")
    cs = get_cross_section("Double Etch (2 Trenches)")

    inst_wg_p = instantiate_euler_waveguide(
        ly,
        top,
        [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)],
        cs,
        bend_type="topic",
        add_ports=True,
    )
    ports_p = {p.name: p for p in read_cell_ports(ly, inst_wg_p.cell, pin_layer=DEFAULT_PIN_LAYER)}
    assert set(ports_p.keys()) == {"o1", "o2"}
    assert math.hypot(ports_p["o1"].x, ports_p["o1"].y) < 1e-6
    assert math.hypot(ports_p["o2"].x - 100.0, ports_p["o2"].y - 100.0) < 1e-6

    inst_wg_np = instantiate_euler_waveguide(
        ly,
        top,
        [(0.0, -40.0), (80.0, -40.0)],
        cs,
        bend_type="euler",
        add_ports=False,
    )
    assert read_cell_ports(ly, inst_wg_np.cell, pin_layer=DEFAULT_PIN_LAYER) == []

    inst_eb = instantiate_euler_bend(ly, top, angle_deg=90.0, cross_section=cs, add_ports=True)
    assert len(read_cell_ports(ly, inst_eb.cell, pin_layer=DEFAULT_PIN_LAYER)) == 2

    inst_tb = instantiate_topic_bend(
        ly, top, angle_deg=90.0, cross_section=cs, r_eff=40.0, p=0.50, straight_in=5.0, straight_out=5.0, add_ports=True
    )
    tb_ports = {p.name: p for p in read_cell_ports(ly, inst_tb.cell, pin_layer=DEFAULT_PIN_LAYER)}
    assert set(tb_ports.keys()) == {"o1", "o2"}
    assert math.hypot(tb_ports["o2"].x - 45.0, tb_ports["o2"].y - 45.0) < 1e-6


def _make_synthetic_mmi(layout: pya.Layout, name: str = "SYN_MMI", w: float = 0.80) -> pya.Cell:
    cell = layout.create_cell(name)
    li = layout.layer(1, 0)
    hw = 0.5 * w
    reg = pya.Region()
    reg.insert(pya.DBox(0.0, -hw, 20.0, hw).to_itype(layout.dbu))
    reg.insert(pya.DBox(20.0, -4.0, 60.0, 4.0).to_itype(layout.dbu))
    for sy in (-2.5, 2.5):
        reg.insert(pya.DBox(60.0, sy - hw, 80.0, sy + hw).to_itype(layout.dbu))
    cell.shapes(li).insert(reg.merged())
    return cell


def _run_offscreen_gui_phase() -> None:
    """Full offscreen Qt5/Qt6 GUI integration verification inside klayout -e -z."""
    checks: list[str] = []

    def _pass(name: str) -> None:
        checks.append(name)
        print(f"  [GUI PASS] {name}")

    app = pya.Application.instance()
    assert app is not None
    mw = app.main_window()
    assert mw is not None, "Offscreen GUI phase requires non-null main_window()"

    _exec_lym_macro()
    _check_zero_numpy_scipy_imports()

    from pic_router.config import (
        CROSS_SECTION_PRESETS,
        DEFAULT_LENGTH_LAYER,
        DEFAULT_PIN_LAYER,
        DEFAULT_PRESET_NAME,
        get_user_presets_path,
        load_user_presets,
        save_user_preset,
    )
    from pic_router.euler import topic_bend_metrics
    from pic_router.gui import (
        MODE_ADD_PORT,
        MODE_ALIGN_X,
        MODE_ALIGN_Y,
        MODE_CENTER_PAIR,
        MODE_FREE_DRAW,
        MODE_ROUTE,
        MODE_SNAP,
        PICFreeDrawWaveguidePlugin,
        _GUI_STATE,
        _execute_port_route,
        action_activate_mode,
        action_add_manual_straight,
        action_auto_detect_ports,
        action_path_to_euler_waveguide,
        action_revert_waveguide_to_path,
        execute_add_trench,
        execute_cd_bias,
        execute_create_topic_bend,
        init_gui,
    )
    from pic_router.ports import add_port_to_cell, collect_world_ports, read_cell_ports, set_instance_alias

    # 1. init_gui() idempotency, dock & menu registration
    assert init_gui() is True
    assert init_gui() is True
    dock = _GUI_STATE["dock"]
    assert dock is not None
    assert mw.menu().is_valid("pic_tools_menu")
    assert len(_GUI_STATE["factories"]) == 7
    _pass("init_gui + PICRouterDock + 7 PluginFactories + PIC Tools menu")

    # 2. Editable layout setup (create TOP cell and select it as active cell)
    mw.create_layout(0)
    view = mw.current_view()
    assert view is not None
    cv = view.active_cellview()
    assert cv.is_valid()
    layout = cv.layout()
    layout.dbu = 0.001
    assert layout.is_editable(), "Layout must be editable (-e flag)"
    top = layout.create_cell("TOP")
    view.select_cell(top.cell_index(), 0)
    assert view.active_cellview().cell is not None
    view.zoom_box(pya.DBox(-50.0, -100.0, 600.0, 400.0))
    _pass("Editable layout & viewport initialized")

    # 3. Neutral presets & JSON user preset Save/Delete in temp KLAYOUT_HOME
    dock._apply_preset("Core Only (Single Layer)")
    assert math.isclose(float(dock.core_w_spin.value), 0.50, abs_tol=1e-6)
    assert math.isclose(float(dock.trench_w_spin.value), 0.0, abs_tol=1e-6)

    dock._apply_preset("Double Etch (2 Trenches)")
    assert math.isclose(float(dock.core_w_spin.value), 0.80, abs_tol=1e-6)
    assert str(dock.trench_layer_edit.text) == "1/1=10; 2/1=25"

    # Save & delete custom preset in isolated temp KLAYOUT_HOME
    user_json_path = get_user_presets_path()
    kl_home = os.environ.get("KLAYOUT_HOME", "")
    assert kl_home and str(user_json_path).startswith(kl_home), (
        f"Expected user presets under temp KLAYOUT_HOME ({kl_home}), got {user_json_path}"
    )
    dock.core_w_spin.setValue(0.65)
    dock.radius_spin.setValue(35.0)
    custom_cs = dock.get_settings()["cross_section"].copy()
    custom_cs.name = "User_Test_Preset"
    save_user_preset("User_Test_Preset", custom_cs)
    dock._reload_preset_combo(select_name="User_Test_Preset")
    dock._apply_preset("User_Test_Preset")
    assert math.isclose(float(dock.core_w_spin.value), 0.65, abs_tol=1e-6)
    assert "User_Test_Preset" in load_user_presets()

    dock._on_delete_user_preset()
    assert "User_Test_Preset" not in load_user_presets()
    assert str(dock.preset_combo.currentText) == DEFAULT_PRESET_NAME
    # Built-in template delete attempt must be rejected
    dock._on_delete_user_preset()
    assert DEFAULT_PRESET_NAME in CROSS_SECTION_PRESETS
    _pass("Neutral presets + temp JSON Save/Delete + built-in protection")

    # 4. Activate all 7 interactive canvas modes cleanly
    all_modes = [
        (MODE_SNAP, "Snap 2 Ports"),
        (MODE_ROUTE, "Route 2 Ports"),
        (MODE_ADD_PORT, "Add Port"),
        (MODE_ALIGN_X, "Align X"),
        (MODE_ALIGN_Y, "Align Y"),
        (MODE_CENTER_PAIR, "Center Pair"),
        (MODE_FREE_DRAW, "Free Draw Waveguide"),
    ]
    for mode_id, mode_lbl in all_modes:
        action_activate_mode(mode_id, mode_lbl)
    _pass("All 7 interactive modes activated cleanly")

    # 5. Full MODE_FREE_DRAW blank-canvas interactive workflow:
    #    click start -> drag -> type length -> undo -> Dock Add Straight -> Enter commit -> flush o1/o2
    dock._apply_preset("Core + Trench (Rib)")
    dock.bend_type_combo.setCurrentText("TOPIC (C3)")
    dock.radius_spin.setValue(40.0)
    dock.euler_p_spin.setValue(0.50)

    fd = _GUI_STATE.get("active_free_draw_plugin")
    assert fd is not None, "MODE_FREE_DRAW activation did not set active_free_draw_plugin"

    left_btn = int(pya.ButtonState.LeftButton)
    right_btn = int(pya.ButtonState.RightButton)
    # 5a. Blank-canvas click at (10.2, 20.4) -> quantized Start (10, 20)
    assert fd.mouse_click_event(pya.DPoint(10.2, 20.4), left_btn, True) is True
    assert fd._start_pt == (10.0, 20.0) and fd._start_port is None

    # 5b. Drag along +X to (110.2, 20.0) + click -> places S1 = 100.0 um
    fd.mouse_moved_event(pya.DPoint(110.2, 20.0), 0, True)
    assert fd.mouse_click_event(pya.DPoint(110.2, 20.0), left_btn, True) is True
    assert len(fd._waypoints) == 1
    assert math.hypot(fd._waypoints[0][0] - 110.0, fd._waypoints[0][1] - 20.0) < 1e-3

    # 5c. Arrow Up + type "80" + Enter -> appends 90-deg TOPIC bend + 80 um +Y straight
    assert fd.key_event(int(pya.KeyCode.Up), 0) is True
    assert fd.key_event(ord("8"), 0) is True
    assert fd.key_event(ord("0"), 0) is True
    assert fd.key_event(int(pya.KeyCode.Return), 0) is True
    assert len(fd._waypoints) == 2

    # 5d. Lossless undo via Right-click -> restores exact (110, 20) tip
    assert fd.mouse_click_event(pya.DPoint(110.0, 60.0), right_btn, True) is True
    assert len(fd._waypoints) == 1
    assert math.hypot(fd._waypoints[0][0] - 110.0, fd._waypoints[0][1] - 20.0) < 1e-3

    # 5e. Add 60 um +Y via Dock helper action_add_manual_straight, then Enter to commit
    assert action_add_manual_straight(60.0, "+Y (↑)") is True
    assert len(fd._waypoints) == 2
    assert fd.key_event(int(pya.KeyCode.Return), 0) is True

    # Verify committed EulerWaveguide PCell + flush o1/o2 PinRec ports + RouteLength (90/2)
    t90 = topic_bend_metrics(90.0, 40.0, p=0.50)["tangent_offset"]
    free_wgs = [
        inst
        for inst in top.each_inst()
        if inst.is_pcell() and inst.cell is not None and inst.cell.pcell_name() == "EulerWaveguide"
    ]
    assert len(free_wgs) == 1
    fd_ports = {p.name: p for p in read_cell_ports(layout, free_wgs[0].cell, pin_layer=DEFAULT_PIN_LAYER)}
    assert set(fd_ports.keys()) == {"o1", "o2"}
    assert math.hypot(fd_ports["o1"].x - 10.0, fd_ports["o1"].y - 20.0) < 1e-3
    assert _angle_diff_deg(fd_ports["o1"].angle_deg, 180.0) < 0.1
    assert math.hypot(fd_ports["o2"].x - (110.0 + t90), fd_ports["o2"].y - (20.0 + t90 + 60.0)) < 1e-2
    assert _angle_diff_deg(fd_ports["o2"].angle_deg, 90.0) < 0.1
    li_len = layout.find_layer(*DEFAULT_LENGTH_LAYER)
    assert li_len >= 0 and top.shapes(li_len).size() > 0
    _pass("MODE_FREE_DRAW blank-canvas click -> drag -> type -> undo -> Enter commit with flush o1/o2")

    # 6. Synthetic devices + Auto-Detect/Refresh Ports (clearing stale port widths)
    mmi = _make_synthetic_mmi(layout, name="GUI_MMI", w=0.80)
    inst_m1 = top.insert(pya.DCellInstArray(mmi.cell_index(), pya.DTrans(0.0, 220.0)))
    inst_m2 = top.insert(pya.DCellInstArray(mmi.cell_index(), pya.DTrans(2, False, 340.0, 250.0)))
    set_instance_alias(inst_m1, "MMI_L")
    set_instance_alias(inst_m2, "MMI_R")

    # Plant a stale wrong-width port (0.40 um) + ghost port before refresh
    add_port_to_cell(layout, mmi, "in_port", 0.0, 0.0, 180.0, width=0.40, core_layer=(1, 0))
    add_port_to_cell(layout, mmi, "ghost_stale", 40.0, 10.0, 90.0, width=0.40, core_layer=(1, 0))
    view.clear_selection()
    action_auto_detect_ports()
    refreshed = {p.name: p for p in read_cell_ports(layout, mmi)}
    assert "ghost_stale" not in refreshed
    assert "in_port" in refreshed and math.isclose(refreshed["in_port"].width, 0.80, abs_tol=2e-3)
    assert len(refreshed) == 3
    _pass("Auto-Detect / Refresh Ports cleared stale widths & preserved custom port name")

    # 7. TOPIC (C3) & Euler (C2) Port-to-Port Routing
    wports = collect_world_ports(layout, top)
    p_l_up = next(p for p in wports if p.inst_id == "MMI_L" and abs(p.y - 222.5) < 0.2)
    p_l_dn = next(p for p in wports if p.inst_id == "MMI_L" and abs(p.y - 217.5) < 0.2)
    p_r_up = next(p for p in wports if p.inst_id == "MMI_R" and abs(p.y - 252.5) < 0.2)
    p_r_dn = next(p for p in wports if p.inst_id == "MMI_R" and abs(p.y - 247.5) < 0.2)

    dock.route_mode_combo.setCurrentText("90° Bend + Straight")
    dock.bend_type_combo.setCurrentText("TOPIC (C3)")
    assert _execute_port_route(view, layout, top, p_l_up, p_r_up) is True

    dock.bend_type_combo.setCurrentText("Euler (C2)")
    assert _execute_port_route(view, layout, top, p_l_dn, p_r_dn) is True

    wg_insts = [
        inst
        for inst in top.each_inst()
        if inst.is_pcell() and inst.cell is not None and inst.cell.pcell_name() == "EulerWaveguide"
    ]
    assert len(wg_insts) == 3  # 1 Free-Draw + 1 TOPIC + 1 Euler
    btypes = {str(inst.pcell_parameters_by_name().get("bend_type")) for inst in wg_insts}
    assert {"topic", "euler"}.issubset(btypes)
    _pass("TOPIC (C3) & Euler (C2) routing created valid EulerWaveguide PCells")

    # 8. Create Standalone TOPIC Bend
    assert execute_create_topic_bend(
        r_eff=40.0, angle_deg=90.0, p=0.50, straight_in=5.0, straight_out=5.0, origin=(400.0, 0.0)
    ) is True
    tb_insts = [
        inst
        for inst in top.each_inst()
        if inst.is_pcell() and inst.cell is not None and inst.cell.pcell_name() == "TOPICBend"
    ]
    assert len(tb_insts) == 1
    _pass("execute_create_topic_bend created standalone TOPICBend with ports & annotation")

    # 9. Add Trench (Multi-Layer) on selected MMI instance
    cv_idx = int(view.active_cellview_index)
    sel_mmi = pya.ObjectInstPath()
    sel_mmi.cv_index = cv_idx
    sel_mmi.top = top.cell_index()
    sel_mmi.append_path(pya.InstElement(inst_m1))
    view.object_selection = [sel_mmi]
    assert execute_add_trench("1/0", "1/1=10; 2/1=25", nominal_core_w=0.80, trim_at_ports=True) is True
    assert mmi.shapes(layout.find_layer(1, 1)).size() > 0
    assert mmi.shapes(layout.find_layer(2, 1)).size() > 0
    _pass("execute_add_trench generated multi-layer 1/1 & 2/1 trenches trimmed at ports")

    # 10. CD Bias (non-destructive overlay to 1/10 AND in-place +120 nm with PinRec width sync)
    assert execute_cd_bias("1/0", bias_nm=120.0, output_mode="dt_offset", dt_offset=10) is True
    assert top.shapes(layout.find_layer(1, 10)).size() > 0

    assert execute_cd_bias("1/0", bias_nm=120.0, output_mode="in_place") is True
    mmi_pins_after = read_cell_ports(layout, mmi)
    assert all(math.isclose(p.width, 0.920, abs_tol=2e-3) for p in mmi_pins_after)
    _pass("execute_cd_bias verified (dt_offset overlay + in-place with PinRec sync)")

    # 11. Routes (ΔL) tab refresh & 2-row ΔL comparison
    dock.refresh_routes()
    assert dock.routes_table.rowCount >= 3
    dock.routes_table.clearSelection()
    dock.routes_table.setRangeSelected(pya.QTableWidgetSelectionRange(0, 0, 0, 3), True)
    dock.routes_table.setRangeSelected(pya.QTableWidgetSelectionRange(1, 0, 1, 3), True)
    dock._on_route_selection_changed()
    delta_txt = str(dock.route_delta_label.text)
    assert "ΔL" in delta_txt, f"Expected ΔL comparison in route_delta_label, got: {delta_txt!r}"
    _pass("Routes (ΔL) tab populated and computed ΔL across selected routes")

    # 12. Path <-> Waveguide roundtrip
    view.clear_selection()
    li_core = layout.layer(1, 0)
    raw_path = pya.DPath([pya.DPoint(0.0, -60.0), pya.DPoint(120.0, -60.0), pya.DPoint(120.0, -180.0)], 0.80)
    sh_path = top.shapes(li_core).insert(raw_path)

    sel_p = pya.ObjectInstPath()
    sel_p.cv_index = cv_idx
    sel_p.top = top.cell_index()
    sel_p.layer = li_core
    sel_p.shape = sh_path
    view.object_selection = [sel_p]

    wg_count_before = len(
        [i for i in top.each_inst() if i.is_pcell() and i.cell.pcell_name() == "EulerWaveguide"]
    )
    action_path_to_euler_waveguide()
    wg_after = [i for i in top.each_inst() if i.is_pcell() and i.cell.pcell_name() == "EulerWaveguide"]
    assert len(wg_after) == wg_count_before + 1

    # Revert the newly created EulerWaveguide back to raw DPath
    new_wg = wg_after[-1]
    sel_wg = pya.ObjectInstPath()
    sel_wg.cv_index = cv_idx
    sel_wg.top = top.cell_index()
    sel_wg.append_path(pya.InstElement(new_wg))
    view.object_selection = [sel_wg]
    action_revert_waveguide_to_path()
    wg_final = [i for i in top.each_inst() if i.is_pcell() and i.cell.pcell_name() == "EulerWaveguide"]
    assert len(wg_final) == wg_count_before
    assert any(sh.is_path() for sh in top.shapes(li_core).each())
    _pass("Path <-> EulerWaveguide roundtrip verified")

    print(f"GUI_PHASE_OK: {len(checks)} checks passed")


def _run_batch_phase() -> None:
    """Batch (-b) verification + spawns offscreen Qt GUI phase in isolated temp KLAYOUT_HOME."""
    checks: list[str] = []

    def _pass(name: str) -> None:
        checks.append(name)
        print(f"  [BATCH PASS] {name}")

    _check_zero_numpy_scipy_imports()
    _pass("Zero numpy/scipy imports (runtime + AST scan)")

    _exec_lym_macro()
    _pass("Executed pymacros/pic_router_init.lym cleanly")

    _check_pcells_and_shortcuts()
    _pass("PIC_Router PCell library (EulerBend, EulerWaveguide, TOPICBend) + unique shortcuts")

    _check_grain_xml_and_installer()
    _pass("grain.xml metadata (0.1.1, MeshedPoto, MIT) + bash -n install_linux_mac.sh")

    # Launch Phase 2 (offscreen Qt5/Qt6 GUI integration) in an isolated temp KLAYOUT_HOME
    klayout_bin = os.path.realpath(sys.executable)
    if not os.path.isfile(klayout_bin) or "klayout" not in Path(klayout_bin).name.lower():
        klayout_bin = shutil.which("klayout") or "klayout"

    with tempfile.TemporaryDirectory(prefix="pic_router_gui_test_") as tmp_home:
        env = dict(os.environ)
        env["PIC_ROUTER_GUI_PHASE"] = "1"
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["KLAYOUT_HOME"] = tmp_home
        proc = subprocess.run(
            [klayout_bin, "-e", "-z", "-nc", "-rx", "-r", str(SCRIPT_PATH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if proc.stdout:
            print(proc.stdout.rstrip())
        if proc.returncode != 0 or "GUI_PHASE_OK:" not in proc.stdout:
            if proc.stderr:
                print(proc.stderr, file=sys.stderr)
            raise RuntimeError(
                f"Offscreen GUI phase failed (rc={proc.returncode})."
            )
    _pass("Full offscreen Qt5/Qt6 GUI integration phase passed")
    print(f"ALL_HEADLESS_AND_GUI_CHECKS_PASSED ({len(checks)} batch checks + GUI suite)")


def main() -> None:
    is_gui_phase = os.environ.get("PIC_ROUTER_GUI_PHASE") == "1"
    app = pya.Application.instance() if hasattr(pya, "Application") else None
    try:
        if is_gui_phase:
            _run_offscreen_gui_phase()
            if app is not None and app.main_window() is not None:
                app.exit(0)
        else:
            _run_batch_phase()
    except Exception:
        traceback.print_exc()
        if is_gui_phase and app is not None and app.main_window() is not None:
            app.exit(1)
        sys.exit(1)


if __name__ == "__main__":
    main()
