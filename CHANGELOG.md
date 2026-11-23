# Changelog

All notable changes to **KLayout-PIC Router** (`pic_router`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-11-23

### Fixed
- **Eliminated Multi-Second UI Freezes on Chip-Scale Layouts (`PICRouterDock`)**:
  - Replaced `QHeaderView.ResizeToContents` on `ports_table` and `routes_table` with `QHeaderView.Interactive` + explicit initial column widths, wrapped table repopulation in `setUpdatesEnabled(False)` / `blockSignals(True)`, and deduplicated `set_status()` text/stylesheet updates—eliminating $O(N^2)$ synchronous Qt font/layout recalculations during `Align X`, `Align Y`, `Center Pair (2->2)`, `Snap`, and port refreshes on multi-hundred-port chips (~600× faster dock refresh).
- **Prevented Steep Cross-Chip S-Bend Vertex Explosion & Extrusion Stalls (`router.py`, `euler.py`, `gui.py`)**:
  - Added aspect-ratio and minimum-radius guards in `_plan_c2_sbend` (`|ly| > 4 * lx` or `R_min < max(2.0, 0.12 * R_min_req)`) so degenerate cross-chip facing port pairs cleanly fall back to `90° Bend + Straight` routing instead of sampling hundreds of thousands of cliff S-bend vertices.
  - Replaced repeated full-polyline S-bend generation inside `tune_route_for_integer_length` bisection with analytical Gauss–Legendre quadrature (`sbend_metrics`), sampling the centerline only once at convergence.
  - Added a `max_points=2049` safety cap in `sbend_points`, relaxed the smooth single-ribbon fast path in `build_multilayer_waveguide_regions` to `max_extent < 0.80 * R_min` (avoiding thousands of per-segment `Region` unions on wide trenches), decimated canvas preview `DPath` markers to $\le 320$ points, and cached hovered target-port route previews in `PICPortRoutePlugin`.
- **Accelerated `Auto-Detect / Refresh Ports` on Dense Multi-Turn Spiral Cells (`ports.py`)**:
  - Added a 4-multiply integer dot-product corner-angle gate before floating-point square-root and collinear walks in `_detect_endcaps_in_regions` to immediately reject smooth curved/spiral chords, replaced `region.interacting(probe)` with clipped boolean `(region & probe)` to avoid copying 100k+ vertex polygons, and clipped heavy polygons (`n > 512`) to a local facet bounding box prior to inward strip verification (~9× faster on dense spiral subcells).

## [0.1.0] - 2026-11-21

Initial public open-source release of **KLayout-PIC Router** for KLayout 0.28–0.30+.

### Added
- **Pure-Python Photonic Geometry & Routing Engine (Zero External Dependencies)**:
  - Constant-width **TOPIC ($C^3$ Hermite-curvature)** 90° and arbitrary-angle bends with zero curvature derivative ($\kappa' = 0$) at all four straight/transition/circular junctions.
  - Symmetric **Partial Euler ($C^2$ clothoid)** bends via built-in 16-point Gauss–Legendre Fresnel integration (`fresnel`), matching `scipy.special.fresnel` to $< 10^{-13}$ with zero `numpy`/`scipy` imports.
  - Smooth zero-endpoint-curvature **$C^2$ Quintic Bezier** and **$C^2$ Raised-Sine** S-bends for offset parallel ports.
  - Analytical integer-micron optical path-length tuning (`Integer um Length`) via exact $R_{\mathrm{eff}}$ / S-bend stub adjustment with zero device displacement.
  - Non-consuming **Auto Linear Tapers** whenever port widths differ from the active routing `Core W`, preserving pure straight waveguide lengths.
- **Interactive KLayout GUI (`PIC Tools` Menu & `PIC Snap & Router` Dock Panel)**:
  - **Blank-Canvas Free-Draw Waveguide** (`Ctrl+Shift+F`): draw multi-layer TOPIC/Euler waveguides anywhere without pre-existing ports, with click/drag, arrow-key + typed-digit exact straight lengths, lossless right-click/Backspace undo, and automatic flush `o1`/`o2` `PinRec (90/0)` port stamping.
  - **Port Management**: flush-inside `PinRec (90/0)` port markers with persistent GDSII `PROPATTR` metadata, one-click `Auto-Detect / Refresh Ports` (`Ctrl+Shift+D`, cleanly updating port widths after CD bias or geometry edits), interactive edge click (`Ctrl+Shift+P`), and instance aliasing (`World Ports` tab).
  - **Precision Cell Alignment & Snapping**: `Snap 2 Ports` (`Ctrl+Shift+S`), `Quick Snap Selected Cell` (`Ctrl+Shift+K`), `Align X` (`Ctrl+Shift+X`), `Align Y` (`Ctrl+Shift+Y`), and symmetric `Center Pair (2->2)` (`Ctrl+Shift+M`), with full support for rotated, mirrored (`r0*`), and arbitrary-angle facets.
  - **Pure-Straight Manual & Auto Routing** (`Ctrl+Shift+R`): Shift-locked H/V ortho dragging, typed numeric lengths + `Enter`/`Tab` or Dock `Add Straight`, lossless waypoint undo, `Path -> Euler Waveguide` (`Ctrl+Shift+W`), and `Revert WG -> Path` (`Ctrl+Shift+E`).
  - **Process-Neutral Presets & Persistent User JSON Presets**: built-in starter templates (`Core Only (Single Layer)`, `Core + Trench (Rib)`, `Double Etch (2 Trenches)`) plus Dock `Save` / `Del` persistence to `pic_router_presets.json` with `;`-separated multi-layer core/trench syntax (e.g., `1/1=10; 2/1=25`).
  - **Collision-Free `RouteLength (90/2)` Annotations & `Routes (ΔL)` Inspector**: `Compact Card`, `Per-Segment`, and `Total Only` annotation styles, interactive two-route $\Delta L$ comparator, and clipboard export.
  - **Post-Layout Mask Prep & Lithography Tools**:
    - `Add Trench (Multi)...`: generate single- or multi-layer trenches around existing core-only PIC structures, trimmed flush at port faces with zero protrusion.
    - `CD Bias (ΔW nm)...`: isotropic or anisotropic X/Y linewidth compensation (`Extend -> Size -> Flush Trim`) on straight, curved, bend, MMI, and PCell geometries with `0.000 nm` port-face and centerline length shift.
  - **Standalone PCell Library (`PIC_Router`)**: live-editable `EulerWaveguide`, `TOPICBend`, and `EulerBend` PCells, plus the interactive `Create TOPIC Bend (90° / Custom)...` dialog with live $R_c$ / $\theta_p$ / footprint readout.
- **Cross-Platform Installers & Packaging**:
  - One-click Windows installer (`scripts/install_windows.bat`), Linux/macOS installer with `--install` / `--symlink` / `--uninstall` / `--status` (`scripts/install_linux_mac.sh`), and release archive builder (`scripts/build_release_zip.py`).
