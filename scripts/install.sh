#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_ROOT="$SCRIPT_DIR"
if [ ! -d "$PKG_ROOT/voice_review" ] && [ -d "$SCRIPT_DIR/../voice_review" ]; then
  PKG_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

MANAGED_DIR="${VMR_MANAGED_DIR:-$HOME/.local/share/voice-memo-review}"
BIN_DIR="${VMR_BIN_DIR:-$HOME/.local/bin}"
VMR_HOME="${VMR_HOME:-$HOME/.config/voice-memo-review}"
MODEL_TARGET="$HOME/Library/Application Support/FluidAudio/Models/speaker-diarization"

echo "=== voice-memo-review 免编译一键安装 ==="

# 1. 部署受管程序目录（保留用户数据目录完全不动）
mkdir -p "$(dirname "$MANAGED_DIR")" "$BIN_DIR" "$VMR_HOME"
TMP_STAGE="${MANAGED_DIR}.staging.$$"
rm -rf "$TMP_STAGE"
mkdir -p "$TMP_STAGE"
cp -R "$PKG_ROOT/voice_review" "$PKG_ROOT/templates" "$PKG_ROOT/skills" "$PKG_ROOT/prompts" "$PKG_ROOT/vmr" "$TMP_STAGE/"
[ -d "$PKG_ROOT/bin" ] && cp -R "$PKG_ROOT/bin" "$TMP_STAGE/"
[ -f "$PKG_ROOT/config.example.toml" ] && cp "$PKG_ROOT/config.example.toml" "$TMP_STAGE/"
chmod +x "$TMP_STAGE/vmr"
[ -d "$TMP_STAGE/bin" ] && chmod +x "$TMP_STAGE/bin"/* 2>/dev/null || true

rm -rf "${MANAGED_DIR}.old"
[ -d "$MANAGED_DIR" ] && mv "$MANAGED_DIR" "${MANAGED_DIR}.old"
mv "$TMP_STAGE" "$MANAGED_DIR"
rm -rf "${MANAGED_DIR}.old"

ln -sf "$MANAGED_DIR/vmr" "$BIN_DIR/vmr"
echo "✅ 程序本体已安装至：$MANAGED_DIR"
echo "✅ 命令入口已链接至：$BIN_DIR/vmr"

# 2. 离线预置 21MB Core ML 声纹模型（如果分发包内置且本机尚未存在）
if [ -d "$PKG_ROOT/models/speaker-diarization" ]; then
  if [ ! -d "$MODEL_TARGET" ] || [ -z "$(ls -A "$MODEL_TARGET" 2>/dev/null)" ]; then
    mkdir -p "$(dirname "$MODEL_TARGET")"
    cp -R "$PKG_ROOT/models/speaker-diarization" "$MODEL_TARGET"
    echo "✅ 已离线预置 21MB Core ML 声纹模型至：$MODEL_TARGET"
  else
    echo "ℹ️  本机已存在 FluidAudio 声纹模型缓存，保留未覆盖。"
  fi
fi

# 3. 首次初始化用户数据目录与 4 套明文规则模板
if [ ! -f "$VMR_HOME/config.toml" ] && [ -f "$MANAGED_DIR/config.example.toml" ]; then
  cp "$MANAGED_DIR/config.example.toml" "$VMR_HOME/config.toml"
  echo "✅ 已初始化用户配置：$VMR_HOME/config.toml"
fi

"$BIN_DIR/vmr" --vmr-home "$VMR_HOME" doctor || true

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo ""
  echo "💡 提示：建议把 $BIN_DIR 加入 ~/.zshrc PATH："
  echo "   echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
fi

echo ""
echo "🎉 安装完成！"
echo "   - 打开可视化控制台： $BIN_DIR/vmr ui"
echo "   - 一键接入本机 AI：  $BIN_DIR/vmr install"
echo "   - 后台自动监听：     $BIN_DIR/vmr watch"

if [ "${VMR_NO_OPEN_UI:-0}" != "1" ] && [ -t 1 ]; then
  echo "正在为你打开本地可视化控制台…"
  "$BIN_DIR/vmr" ui &
fi
