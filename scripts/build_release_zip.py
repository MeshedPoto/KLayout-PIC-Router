#!/usr/bin/env python3
"""Build a clean, self-contained release ZIP of KLayout-PIC Router (`pic_router`).

Usage:
    python3 scripts/build_release_zip.py
"""

from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DIST_DIR = REPO_ROOT / "dist"


def _read_version() -> str:
    grain = (REPO_ROOT / "grain.xml").read_text(encoding="utf-8")
    m = re.search(r"<version>\s*([^<\s]+)\s*</version>", grain)
    return m.group(1) if m else "0.1.0"


def _to_crlf_ascii(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")


def build_zip() -> Path:
    version = _read_version()
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    root_folder = f"KLayout-PIC-Router_v{version}"
    zip_path = DIST_DIR / f"{root_folder}.zip"

    bat_path = SCRIPT_DIR / "install_windows.bat"
    bat_crlf = _to_crlf_ascii(bat_path.read_text(encoding="utf-8"))
    bat_path.write_bytes(bat_crlf)

    sh_path = SCRIPT_DIR / "install_linux_mac.sh"
    sh_bytes = sh_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Core KLayout Salt package files
        zf.write(REPO_ROOT / "grain.xml", f"{root_folder}/pic_router/grain.xml")
        zf.write(
            REPO_ROOT / "pymacros" / "pic_router_init.lym",
            f"{root_folder}/pic_router/pymacros/pic_router_init.lym",
        )
        py_dir = REPO_ROOT / "python" / "pic_router"
        for py_file in sorted(py_dir.glob("*.py")):
            zf.write(py_file, f"{root_folder}/pic_router/python/pic_router/{py_file.name}")

        # 2. Cross-platform installers
        zf.writestr(f"{root_folder}/install_windows.bat", bat_crlf)
        zi_sh = zipfile.ZipInfo(f"{root_folder}/install_linux_mac.sh")
        zi_sh.external_attr = 0o755 << 16
        zf.writestr(zi_sh, sh_bytes)

        # 3. License and documentation (included when present in repository root)
        for doc_name in ("LICENSE", "README.md", "README_zh.md"):
            doc_path = REPO_ROOT / doc_name
            if doc_path.is_file():
                zf.write(doc_path, f"{root_folder}/{doc_name}")

    return zip_path


if __name__ == "__main__":
    out = build_zip()
    size_kb = os.path.getsize(out) / 1024.0
    print(f"Built: {out} ({size_kb:.1f} KB)")
