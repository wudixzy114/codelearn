#!/usr/bin/env bash
# CodeLearn 一键启动脚本
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# 初始工作区（可选）：第一个参数或 CODELEARN_TARGET 指定；
# 都不给则启动为空，进浏览器后再选。上次打开的工作区会自动恢复。
if [ "${1:-}" != "" ]; then
  export CODELEARN_TARGET="$1"
fi
export CODELEARN_HOST="${CODELEARN_HOST:-127.0.0.1}"
export CODELEARN_PORT="${CODELEARN_PORT:-43187}"

echo "[codelearn] initial target : ${CODELEARN_TARGET:-（无，启动后在 UI 里打开工作区）}"
echo "[codelearn] serving on  : http://$CODELEARN_HOST:$CODELEARN_PORT"

exec python3 -m uvicorn backend.main:app \
  --host "$CODELEARN_HOST" \
  --port "$CODELEARN_PORT" \
  --app-dir "$HERE"
