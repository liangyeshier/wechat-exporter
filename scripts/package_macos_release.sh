#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/dist/微信聊天记录导出.app"
OUT="$ROOT/dist/WeChatExporter-macOS-arm64.zip"

if [ ! -d "$APP" ]; then
  "$ROOT/scripts/build_macos_app.sh"
fi
rm -f "$OUT"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT"
shasum -a 256 "$OUT" > "$OUT.sha256"
echo "$OUT"
