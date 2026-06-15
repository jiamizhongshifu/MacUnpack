#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/dist/MacUnpack.app"

rm -rf "$APP_DIR"
mkdir -p "$ROOT_DIR/dist"

osacompile -o "$APP_DIR" "$ROOT_DIR/app/MacUnpack.applescript"
mkdir -p "$APP_DIR/Contents/Resources"
cp "$ROOT_DIR/src/mac-unpack.py" "$APP_DIR/Contents/Resources/mac-unpack.py"
chmod +x "$APP_DIR/Contents/Resources/mac-unpack.py"

if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$APP_DIR" 2>/dev/null || true
fi

if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "Built $APP_DIR"
