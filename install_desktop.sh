#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$SCRIPT_DIR/run.sh"

APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"

sed "s|^Exec=.*|Exec=$SCRIPT_DIR/run.sh|" "$SCRIPT_DIR/hdhomerun-desktop.desktop" > "$APPS_DIR/hdhomerun-desktop.desktop"

echo "Installed HDHomeRun Desktop launcher to $APPS_DIR/hdhomerun-desktop.desktop"
