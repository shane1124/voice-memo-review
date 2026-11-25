#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

chmod +x "$ROOT/vmr"

if [[ ! -f "$ROOT/config.toml" ]]; then
  cp "$ROOT/config.example.toml" "$ROOT/config.toml"
  echo "✅ 已从模板创建本机 config.toml（请按需修改 import_since / output_dir / fluidaudio_bin）"
else
  echo "ℹ️  已存在 config.toml，跳过覆盖"
fi

echo ""
echo "正在执行环境自检..."
python3 "$ROOT/vmr" doctor || true
