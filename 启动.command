#!/bin/bash
# ============================================================
#  微信聊天记录导出 —— 一键安装并启动（macOS）
#  源码版启动器：自动装依赖 → 打开图形界面。首次密钥设置在页面内完成。
#  以后再双击就直接打开。全程只读你本机的微信数据，仅在本机运行。
# ============================================================
cd "$(dirname "$0")" || exit 1
APPDIR="$(pwd)"          # 项目目录（可能在 iCloud/文稿里）
# 关键：虚拟环境放在「主目录」下的 ~/.wechat_exporter/venv，
# 避开 iCloud（文稿/桌面）同步对 venv 成千上万个文件的破坏/驱逐。
VENV="$HOME/.wechat_exporter/venv"
mkdir -p "$HOME/.wechat_exporter"
echo "============================================================"
echo "   微信聊天记录导出 · 一键启动"
echo "============================================================"

# ---- 1) 找一个可用的 Python (3.9+) ----
PY=""
for c in python3.12 python3.11 python3.13 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2]>=(3,9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "✗ 没找到 Python 3.9 或更高版本。请先安装其中一个，再重新双击本文件："
  echo "    • brew install python@3.12   （需先装 Homebrew: https://brew.sh ）"
  echo "    • 或从 https://www.python.org/downloads/macos/ 下载安装"
  read -r -p "按回车键关闭…" _; exit 1
fi
echo "✓ Python: $("$PY" --version 2>&1)"

# ---- 找到 venv 里实际存在的 python（有的发行版只建 python3，不建 python）----
find_vp() {
  for p in python python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
    [ -x "$VENV/bin/$p" ] && { echo "$VENV/bin/$p"; return 0; }
  done
  return 1
}

# ---- 2) 准备虚拟环境 + 依赖（含从别的电脑拷来 / 不完整时的自动重建）----
need=0
VP="$(find_vp)" || need=1
if [ "$need" = 0 ] && ! "$VP" -c "import flask, sqlcipher3, PIL, av, jinja2" >/dev/null 2>&1; then
  echo "• 现有虚拟环境不可用（可能从别的电脑拷来 / 不完整），将重建…"
  need=1
fi
if [ "$need" = 1 ]; then
  echo "• 正在创建虚拟环境并安装依赖（首次需要联网，可能几分钟）…"
  echo "  位置: $VENV  （放在主目录，不受 iCloud 影响）"
  rm -rf "$VENV"
  "$PY" -m venv "$VENV" || { echo "✗ 创建虚拟环境失败"; read -r -p "回车关闭…" _; exit 1; }
  VP="$(find_vp)" || { echo "✗ 创建后仍找不到虚拟环境里的 python。"; read -r -p "回车关闭…" _; exit 1; }
  "$VP" -m pip install --quiet --upgrade pip
  if ! "$VP" -m pip install -r "$APPDIR/requirements.txt"; then
    echo "✗ 依赖安装失败，请检查网络后重试。"; read -r -p "回车关闭…" _; exit 1
  fi
  echo "✓ 依赖安装完成"
else
  echo "✓ 依赖已就绪"
fi
VP="$(find_vp)"   # 重新确认

# ---- 3) 打开图形界面 ----
echo "• 正在打开界面（浏览器会自动弹出）…"
echo "  用完想退出：回到本窗口按 Ctrl+C，或直接关闭本窗口。"
echo "============================================================"
if [ -z "$VP" ] || [ ! -x "$VP" ]; then
  echo "✗ 找不到虚拟环境里的 python，无法打开界面。请删除 ~/.wechat_exporter/venv 后重试。"
  read -r -p "回车关闭…" _; exit 1
fi
exec "$VP" "$APPDIR/app.py"
