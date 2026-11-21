# Contributing to KLayout-PIC Router

Thank you for your interest in contributing to **KLayout-PIC Router**! Bug reports, feature requests, documentation improvements, and pull requests are all welcome.

## Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/MeshedPoto/KLayout-PIC-Router.git
   cd KLayout-PIC-Router
   ```

2. Install the package into KLayout in **developer symlink mode** so code edits take effect on KLayout restart without recopying files:
   ```bash
   bash scripts/install_linux_mac.sh --symlink
   ```
   Check installation status at any time with:
   ```bash
   bash scripts/install_linux_mac.sh --status
   ```
   On Windows, copy or link the repository folder into `%USERPROFILE%\KLayout\salt\pic_router`, or re-run `scripts\install_windows.bat` after changes.

3. Launch KLayout in **Editing Mode** (`klayout -e`, or enable `File -> Setup -> Application -> Editing Mode` and restart).

## Running Tests

Run both the external Python unit tests and KLayout's native headless batch test suite before opening a pull request:

```bash
# 1. Pure-Python / klayout.db unit test suite
pytest

# 2. Native KLayout headless batch integration tests
klayout -b -r tests/test_klayout_headless.py
```

If you are building a release archive, you can verify it locally with:
```bash
python3 scripts/build_release_zip.py
```

## Code Conventions & Design Constraints

Please preserve the core architectural guarantees that make **KLayout-PIC Router** work reliably out of the box across every KLayout installation:

1. **100% Pure Python Inside `python/pic_router/`**:
   - **Zero `numpy`, `scipy`, or other third-party imports** anywhere under `python/pic_router/`. Only Python 3.10+ standard library modules and `pya` / `klayout.db` are allowed so the plugin installs cleanly into KLayout's embedded Python without pip/conda setup.
2. **Qt5 & Qt6 Compatibility (KLayout 0.28–0.30+)**:
   - Keep strong references to Qt widgets, actions, and plugin factories created in Python so KLayout's Python/Qt bridge never prematurely garbage-collects UI objects.
   - Use defensive property/method helpers where Qt5 and Qt6 differ (e.g., dialog execution, header resize modes, alignment enums).
3. **Headless-Safe Imports**:
   - All modules under `python/pic_router/` (including `gui.py` and `pcells.py`) must remain safe to import when `pya` is absent (external `klayout.db`) or when `pya.Application.instance().main_window()` is `None` (`klayout -b`).
4. **Process Neutrality**:
   - Keep built-in cross-section templates generic and foundry-agnostic; foundry-specific layer stacks belong in user JSON presets (`pic_router_presets.json`).
5. **Non-Conflicting Shortcuts**:
   - Interactive shortcuts must use `Ctrl+Shift+...` combinations so they do not override standard KLayout editing shortcuts.

## Pull Request Checklist

- [ ] Code under `python/pic_router/` uses only Python stdlib + `pya` / `klayout.db` (no `numpy` or `scipy`).
- [ ] `pytest` passes with zero failures.
- [ ] `klayout -b -r tests/test_klayout_headless.py` passes with exit code `0`.
- [ ] New UI buttons, shortcuts, presets, or PCell parameters are documented in both `README.md` and `README_zh.md`.
- [ ] No private GDS files, local absolute paths, or generated `dist/` artifacts are committed.
- [ ] `CHANGELOG.md` is updated if the change is user-facing.
