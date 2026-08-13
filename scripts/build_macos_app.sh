#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_ROOT="$ROOT/.build/macos"
VENV="$BUILD_ROOT/venv"
DIST="$ROOT/dist"
APP_NAME="微信聊天记录导出"

mkdir -p "$BUILD_ROOT"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements-build.txt"

rm -rf "$BUILD_ROOT/work" "$DIST/$APP_NAME.app"
mkdir -p "$DIST"

"$VENV/bin/pyinstaller" \
  --noconfirm \
  --clean \
  --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "io.github.wechat-exporter.local" \
  --distpath "$DIST" \
  --workpath "$BUILD_ROOT/work" \
  --specpath "$BUILD_ROOT" \
  --add-data "$ROOT/templates:templates" \
  --add-data "$ROOT/tools:tools" \
  --collect-all sqlcipher3 \
  --collect-all av \
  --collect-all webview \
  --exclude-module frida \
  --exclude-module frida_tools \
  --exclude-module pandas \
  --exclude-module faster_whisper \
  --exclude-module onnxruntime \
  --exclude-module torch \
  --exclude-module transformers \
  "$ROOT/app.py"

# Wheel RECORD files can preserve absolute build-machine cache paths. They are
# not needed at runtime and must not be shipped in a public artifact.
find "$DIST/$APP_NAME.app" -path '*/Resources/*.dist-info/RECORD' -delete

/usr/bin/codesign --force --deep --sign - "$DIST/$APP_NAME.app"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$DIST/$APP_NAME.app"
echo "$DIST/$APP_NAME.app"
