#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve Salt package source directory:
# 1. Release zip layout: <dir>/pic_router/grain.xml
# 2. Repo root layout:   <dir>/grain.xml
# 3. scripts/ layout:    <dir>/../grain.xml
if [[ -f "$SCRIPT_DIR/pic_router/grain.xml" ]]; then
    SRC_DIR="$SCRIPT_DIR/pic_router"
elif [[ -f "$SCRIPT_DIR/grain.xml" ]]; then
    SRC_DIR="$SCRIPT_DIR"
elif [[ -f "$SCRIPT_DIR/../grain.xml" ]]; then
    SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
    echo "Error: Could not locate grain.xml relative to $SCRIPT_DIR" >&2
    exit 1
fi

SALT_DIR="${KLAYOUT_HOME:-$HOME/.klayout}/salt"
TARGET_DIR="$SALT_DIR/pic_router"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--install | --symlink | --uninstall | --status]

Install or manage the KLayout Salt package 'pic_router' (KLayout-PIC Router).
  --install    Copy package files into $TARGET_DIR (default)
  --symlink    Developer mode: symlink $TARGET_DIR -> $SRC_DIR
  --uninstall  Remove $TARGET_DIR
  --status     Show current installation status
  -h, --help   Show this help message

Environment:
  KLAYOUT_HOME  Override KLayout user directory (default: \$HOME/.klayout)
EOF
}

ACTION="install"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install)
            ACTION="install"
            shift
            ;;
        --symlink|--link|-s)
            ACTION="symlink"
            shift
            ;;
        --uninstall|-u)
            ACTION="uninstall"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: Unknown option '$1'" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$ACTION" in
    status)
        if [[ -L "$TARGET_DIR" ]]; then
            CURRENT_TARGET="$(readlink -f "$TARGET_DIR" 2>/dev/null || readlink "$TARGET_DIR")"
            echo "Installed (symlink): $TARGET_DIR -> $CURRENT_TARGET"
        elif [[ -d "$TARGET_DIR" ]]; then
            echo "Installed (copied directory): $TARGET_DIR"
        else
            echo "Not installed ($TARGET_DIR does not exist)"
        fi
        ;;
    symlink)
        mkdir -p "$SALT_DIR"
        rm -rf "$TARGET_DIR"
        ln -s "$SRC_DIR" "$TARGET_DIR"
        echo "[OK] Linked pic_router (dev mode): $TARGET_DIR -> $SRC_DIR"
        echo "Restart KLayout (Editing Mode: klayout -e) to load KLayout-PIC Router."
        ;;
    install)
        mkdir -p "$SALT_DIR"
        rm -rf "$TARGET_DIR"
        mkdir -p "$TARGET_DIR/pymacros" "$TARGET_DIR/python/pic_router"
        cp "$SRC_DIR/grain.xml" "$TARGET_DIR/grain.xml"
        cp "$SRC_DIR/pymacros/pic_router_init.lym" "$TARGET_DIR/pymacros/pic_router_init.lym"
        cp "$SRC_DIR/python/pic_router/"*.py "$TARGET_DIR/python/pic_router/"
        echo "[OK] Installed pic_router to $TARGET_DIR"
        echo "Restart KLayout (Editing Mode: klayout -e) to load KLayout-PIC Router."
        ;;
    uninstall)
        if [[ -L "$TARGET_DIR" || -e "$TARGET_DIR" ]]; then
            rm -rf "$TARGET_DIR"
            echo "[OK] Uninstalled $TARGET_DIR. Restart KLayout to complete removal."
        else
            echo "pic_router is not currently installed at $TARGET_DIR."
        fi
        ;;
esac
