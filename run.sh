#!/usr/bin/env bash
# CodeLearn 一键启动脚本
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# 目标代码库：默认同级的 xllm，可用第一个参数覆盖
export CODELEARN_TARGET="${1:-$HERE/../xllm}"
export CODELEARN_HOST="${CODELEARN_HOST:-127.0.0.1}"
export CODELEARN_PORT="${CODELEARN_PORT:-43187}"

echo "[codelearn] target repo : $CODELEARN_TARGET"
echo "[codelearn] serving on  : http://$CODELEARN_HOST:$CODELEARN_PORT"

exec python3 -m uvicorn backend.main:app \
  --host "$CODELEARN_HOST" \
  --port "$CODELEARN_PORT" \
  --app-dir "$HERE"
