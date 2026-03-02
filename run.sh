#!/bin/bash
# SwitchWatch launcher
# Run this from Terminal: bash run.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(which python3)"

# Verify dependencies
if ! "$PYTHON" -c "import rumps, AppKit, Foundation" 2>/dev/null; then
    echo "Installing dependencies…"
    "$PYTHON" -m pip install rumps pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices
fi

echo "Starting SwitchWatch…"
exec "$PYTHON" "$SCRIPT_DIR/switchwatch.py"
