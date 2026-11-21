# Changelog

All notable changes to **KLayout-PIC Router** (`pic_router`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
