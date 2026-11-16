#!/usr/bin/env bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${DIVIDEND_SCREEN_HOME:-}" ] && [ -f "$SKILL_DIR/runtime.env" ]; then
  # shellcheck disable=SC1091
  . "$SKILL_DIR/runtime.env"
fi
DATA_HOME="${DIVIDEND_SCREEN_HOME:-$HOME/.local/share/dividend-screen}"
PY="$DATA_HOME/venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "✗ 未找到运行环境：$PY（先运行 install.sh）" >&2
  exit 1
fi

CSV="${1:-}"
if [ -z "$CSV" ]; then
  CSV="$(ls -t "$DATA_HOME"/data/*_dividend_screen.csv 2>/dev/null | head -1 || true)"
fi
if [ -z "$CSV" ] || [ ! -f "$CSV" ]; then
  echo "✗ 没有找到选股结果 CSV（$DATA_HOME/data/*_dividend_screen.csv）" >&2
  exit 1
fi

LOG="$(ls -t "$DATA_HOME"/data/run_*.log 2>/dev/null | head -1 || true)"
OUT="${CSV%_dividend_screen.csv}_summary.md"
if [ -n "$LOG" ] && [ -f "$LOG" ]; then
  "$PY" "$SKILL_DIR/scripts/make_summary.py" --csv "$CSV" --log "$LOG" --out "$OUT"
else
  "$PY" "$SKILL_DIR/scripts/make_summary.py" --csv "$CSV" --out "$OUT"
fi
echo "summary：$OUT"
