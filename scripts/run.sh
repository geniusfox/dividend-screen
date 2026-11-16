#!/usr/bin/env bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${DIVIDEND_SCREEN_HOME:-}" ] && [ -f "$SKILL_DIR/runtime.env" ]; then
  # shellcheck disable=SC1091
  . "$SKILL_DIR/runtime.env"
fi
DATA_HOME="${DIVIDEND_SCREEN_HOME:-$HOME/.local/share/dividend-screen}"
export DIVIDEND_SCREEN_HOME="$DATA_HOME"
PY="$DATA_HOME/venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "✗ 未找到运行环境：$PY" >&2
  echo "  请先运行安装脚本：$SKILL_DIR/install.sh" >&2
  exit 1
fi
mkdir -p "$DATA_HOME/data"

BACKGROUND=0
if [ "${1:-}" = "--background" ]; then
  BACKGROUND=1
  shift
fi

LOG="$DATA_HOME/data/run_$(date +%Y%m%d_%H%M%S).log"
CMD=("$PY" -u "$SKILL_DIR/scripts/dividend_screen.py" --out-dir "$DATA_HOME/data" "$@")

if [ "$BACKGROUND" -eq 1 ]; then
  nohup "${CMD[@]}" >"$LOG" 2>&1 &
  PID=$!
  echo "已启动：PID=$PID"
  echo "日志：$LOG"
  echo "结果目录：$DATA_HOME/data"
  echo "跟踪进度：tail -f \"$LOG\""
else
  "${CMD[@]}" 2>&1 | tee "$LOG"
fi
