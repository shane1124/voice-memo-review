#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(/opt/homebrew/bin/python3.13 -c "import sys; sys.path.insert(0, '$REPO_ROOT'); import voice_review; print(voice_review.__version__)" 2>/dev/null || python3 -c "import re; print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open('$REPO_ROOT/voice_review/__init__.py').read()).group(1))")"

DIST_DIR="$REPO_ROOT/dist"
STAGE_NAME="voice-memo-review-v${VERSION}"
STAGE_DIR="$DIST_DIR/$STAGE_NAME"
ARCHIVE_NAME="vmr-macos-arm64-v${VERSION}.tar.gz"

FLUIDAUDIO_BIN="${FLUIDAUDIO_BIN:-$(command -v fluidaudiocli 2>/dev/null || true)}"
if [ -z "${FLUIDAUDIO_BIN}" ]; then
  for cand in "$HOME/.local/share/voice-memo-review/bin/fluidaudiocli" "$HOME/.local/bin/fluidaudiocli"; do
    if [ -x "$cand" ]; then
      FLUIDAUDIO_BIN="$cand"
      break
    fi
  done
fi
FLUIDAUDIO_MODELS_DIR="${FLUIDAUDIO_MODELS_DIR:-$HOME/Library/Application Support/FluidAudio/Models/speaker-diarization}"
STATIC_FFMPEG_BIN="${STATIC_FFMPEG_BIN:-}"
STATIC_FFPROBE_BIN="${STATIC_FFPROBE_BIN:-}"

rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/bin" "$STAGE_DIR/models" "$DIST_DIR"

cp -R "$REPO_ROOT/voice_review" "$REPO_ROOT/templates" "$REPO_ROOT/skills" "$REPO_ROOT/prompts" "$STAGE_DIR/"
find "$STAGE_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
cp "$REPO_ROOT/vmr" "$REPO_ROOT/config.example.toml" "$REPO_ROOT/LICENSE" "$REPO_ROOT/README.md" "$REPO_ROOT/AI_SETUP.md" "$STAGE_DIR/"
cp "$REPO_ROOT/scripts/install.sh" "$STAGE_DIR/install.sh"
chmod +x "$STAGE_DIR/vmr" "$STAGE_DIR/install.sh"

# 双击图形入口（非技术同事免开终端直接进可视化控制台）
cat > "$STAGE_DIR/启动控制台.command" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$HOME/.local/bin/vmr" ]; then
  VMR_NO_OPEN_UI=1 "$HERE/install.sh"
fi
exec "$HOME/.local/bin/vmr" ui
EOF
chmod +x "$STAGE_DIR/启动控制台.command"

# 打包 10MB 零外部动态库 fluidaudiocli
if [ -x "$FLUIDAUDIO_BIN" ]; then
  cp "$FLUIDAUDIO_BIN" "$STAGE_DIR/bin/fluidaudiocli"
  chmod +x "$STAGE_DIR/bin/fluidaudiocli"
  echo "✅ 已打入预编译 fluidaudiocli（$(du -sh "$STAGE_DIR/bin/fluidaudiocli" | awk '{print $1}')）"
else
  echo "⚠️ 未找到 fluidaudiocli（${FLUIDAUDIO_BIN}），分发包将不含预编译声纹引擎。"
fi

# 打包 21MB Core ML 离线声纹模型
if [ -d "$FLUIDAUDIO_MODELS_DIR" ]; then
  cp -R "$FLUIDAUDIO_MODELS_DIR" "$STAGE_DIR/models/speaker-diarization"
  echo "✅ 已打入离线 Core ML 声纹模型（$(du -sh "$STAGE_DIR/models/speaker-diarization" | awk '{print $1}')）"
else
  echo "⚠️ 未找到离线声纹模型目录（${FLUIDAUDIO_MODELS_DIR}），首次运行将自动联网下载。"
fi

# 可选打入静态 arm64 ffmpeg / ffprobe（建议对外发布时通过环境变量传入纯静态构建版）
if [ -n "$STATIC_FFMPEG_BIN" ] && [ -x "$STATIC_FFMPEG_BIN" ]; then
  cp "$STATIC_FFMPEG_BIN" "$STAGE_DIR/bin/ffmpeg"
  chmod +x "$STAGE_DIR/bin/ffmpeg"
  echo "✅ 已打入静态 ffmpeg（$(du -sh "$STAGE_DIR/bin/ffmpeg" | awk '{print $1}')）"
else
  echo "⚠️ 未指定 STATIC_FFMPEG_BIN：分发包未内嵌静态 ffmpeg（目标 Mac 需已安装 ffmpeg，对外发布请设置 STATIC_FFMPEG_BIN/STATIC_FFPROBE_BIN）。"
fi
if [ -n "$STATIC_FFPROBE_BIN" ] && [ -x "$STATIC_FFPROBE_BIN" ]; then
  cp "$STATIC_FFPROBE_BIN" "$STAGE_DIR/bin/ffprobe"
  chmod +x "$STAGE_DIR/bin/ffprobe"
  echo "✅ 已打入静态 ffprobe（$(du -sh "$STAGE_DIR/bin/ffprobe" | awk '{print $1}')）"
fi

(cd "$DIST_DIR" && tar -czf "$ARCHIVE_NAME" "$STAGE_NAME" && shasum -a 256 "$ARCHIVE_NAME" > SHA256SUMS)
rm -rf "$STAGE_DIR"

echo ""
echo "🎉 离线分发包构建完成："
echo "   - 归档文件：$DIST_DIR/${ARCHIVE_NAME}（$(du -sh "$DIST_DIR/${ARCHIVE_NAME}" | awk '{print $1}')）"
echo "   - 校验清单：$DIST_DIR/SHA256SUMS"
cat "$DIST_DIR/SHA256SUMS"
