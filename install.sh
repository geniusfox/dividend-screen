#!/usr/bin/env bash
# 高股息选股 skill 一键安装
# 用法（本地）:   bash install.sh
# 用法（远程）:   curl -fsSL <tar.gz 地址> ... 或 bash install.sh --pkg-url <tar.gz>
# 可选参数:
#   --token <token>          Tushare token（不传则交互输入）
#   --api-url <url>          Tushare 网关地址（官方源留空）
#   --backup-token <token>   备用通道 token（可选）
#   --skip-check             安装后不做接口自检
#   --skill-dir <dir>        默认 ~/.agents/skills/dividend-screen
#   --data-dir <dir>         默认 ~/.local/share/dividend-screen
#   --pkg-url <tar.gz>       本地没有源码时从该地址下载
set -euo pipefail

SKILL_NAME="dividend-screen"
SKILL_DIR="${DSH_SKILLS_DIR:-$HOME/.agents/skills}/$SKILL_NAME"
DATA_HOME="${DIVIDEND_SCREEN_HOME:-$HOME/.local/share/dividend-screen}"
PKG_URL="${DIVIDEND_SCREEN_PKG_URL:-https://github.com/geniusfox/dividend-screen/archive/refs/heads/main.tar.gz}"
TOKEN=""
API_URL=""
BACKUP=""
SKIP_CHECK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:?}"; shift 2 ;;
    --api-url) API_URL="${2:?}"; shift 2 ;;
    --backup-token) BACKUP="${2:?}"; shift 2 ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    --skill-dir) SKILL_DIR="${2:?}"; shift 2 ;;
    --data-dir) DATA_HOME="${2:?}"; shift 2 ;;
    --pkg-url) PKG_URL="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
done

# ---------- 定位源码 ----------
SRC_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  CANDIDATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "$CANDIDATE/scripts/dividend_screen.py" ]; then
    SRC_DIR="$CANDIDATE"
  fi
fi
if [ -z "$SRC_DIR" ] && [ -f "scripts/dividend_screen.py" ]; then
  SRC_DIR="$(pwd)"
fi
if [ -z "$SRC_DIR" ]; then
  if [ -z "$PKG_URL" ]; then
    echo "✗ 没有找到 skill 源码（scripts/dividend_screen.py）。" >&2
    echo "  请在解压后的目录里运行 install.sh，或使用 --pkg-url <tar.gz 地址>。" >&2
    exit 1
  fi
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT
  echo "正在下载安装包 ..."
  curl -fsSL "$PKG_URL" -o "$TMP_DIR/pkg.tar.gz"
  tar -xzf "$TMP_DIR/pkg.tar.gz" -C "$TMP_DIR"
  FOUND="$(find "$TMP_DIR" -maxdepth 4 -name SKILL.md | head -1 || true)"
  if [ -z "$FOUND" ]; then
    echo "✗ 安装包内容不完整（找不到 SKILL.md）" >&2
    exit 1
  fi
  SRC_DIR="$(dirname "$FOUND")"
fi

# ---------- Python ----------
PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "✗ 未找到 python3，请先安装 Python 3.9+。" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "✗ Python 版本过低，需要 3.9+。" >&2
  exit 1
fi

echo "安装位置：$SKILL_DIR"
echo "运行数据：$DATA_HOME"
mkdir -p "$SKILL_DIR" "$DATA_HOME/data" "$DATA_HOME/bin"

# ---------- 复制 skill 文件 ----------
if [ "$SRC_DIR" = "$SKILL_DIR" ]; then
  echo "检测到源码与安装目录相同，跳过复制。"
else
for item in SKILL.md MEMO.md manifest.json requirements.txt scripts references assets install.sh README.md; do
  if [ -e "$SRC_DIR/$item" ]; then
    rm -rf "$SKILL_DIR/$item"
    cp -R "$SRC_DIR/$item" "$SKILL_DIR/$item"
  fi
done
fi
chmod 755 "$SKILL_DIR/scripts/run.sh" "$SKILL_DIR/scripts/report.sh" \
          "$SKILL_DIR/scripts/bottom.sh" "$SKILL_DIR/scripts/bottom_forecast_tushare.py" \
          "$SKILL_DIR/install.sh" 2>/dev/null || true
printf 'DIVIDEND_SCREEN_HOME=%s\n' "$DATA_HOME" > "$SKILL_DIR/runtime.env"

# ---------- 虚拟环境 ----------
if [ ! -x "$DATA_HOME/venv/bin/python" ]; then
  echo "创建虚拟环境 ..."
  "$PYTHON_BIN" -m venv "$DATA_HOME/venv"
fi
PY="$DATA_HOME/venv/bin/python"
"$PY" -m pip install -q --upgrade pip 2>/dev/null || true
echo "安装依赖（tushare/pandas/numpy）..."
"$PY" -m pip install -q -r "$SKILL_DIR/requirements.txt"

# ---------- token 配置 ----------
CFG="$DATA_HOME/config.json"
EXIST_TOKEN=""
EXIST_API=""
EXIST_BACKUP=""
if [ -f "$CFG" ]; then
  EXIST_TOKEN="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tushare_token",""))' "$CFG" 2>/dev/null || true)"
  EXIST_API="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tushare_api_url",""))' "$CFG" 2>/dev/null || true)"
  EXIST_BACKUP="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tushare_token_backup",""))' "$CFG" 2>/dev/null || true)"
fi

read_secret() {
  local label="$1" value=""
  if [ -t 0 ]; then
    read -r -s -p "$label" value || true
    echo
  elif [ -r /dev/tty ]; then
    read -r -s -p "$label" value </dev/tty || true
    echo
  fi
  printf '%s' "$value"
}

TOKEN="${TOKEN:-${TUSHARE_TOKEN:-${EXIST_TOKEN:-}}}"
if [ -z "$TOKEN" ]; then
  TOKEN="$(read_secret 'Tushare token: ')"
fi
if [ -z "$TOKEN" ]; then
  echo "✗ 没有 token。请到 https://tushare.pro/user/token 复制后重跑，或用 --token 传入。" >&2
  exit 1
fi

API_URL="${API_URL:-${TUSHARE_API_URL:-${EXIST_API:-}}}"
if [ -z "$API_URL" ] && [ ! -f "$CFG" ]; then
  API_URL="$(read_secret 'Tushare 网关地址（官方源直接回车）: ')"
fi
BACKUP="${BACKUP:-${TUSHARE_TOKEN_BACKUP:-${EXIST_BACKUP:-}}}"
if [ -z "$BACKUP" ] && [ ! -f "$CFG" ]; then
  BACKUP="$(read_secret '备用通道 token（没有直接回车）: ')"
fi

TOKEN="$TOKEN" API_URL="$API_URL" BACKUP="$BACKUP" CFG="$CFG" "$PY" - <<'PY'
import json
import os

cfg = {"tushare_token": os.environ["TOKEN"]}
if os.environ.get("API_URL"):
    cfg["tushare_api_url"] = os.environ["API_URL"]
if os.environ.get("BACKUP"):
    cfg["tushare_token_backup"] = os.environ["BACKUP"]
with open(os.environ["CFG"], "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
chmod 600 "$CFG"

# ---------- 命令行包装 ----------
cat >"$DATA_HOME/bin/dividend-screen" <<EOF
#!/usr/bin/env bash
export DIVIDEND_SCREEN_HOME="$DATA_HOME"
exec "$DATA_HOME/venv/bin/python" "$SKILL_DIR/scripts/dividend_screen.py" "\$@"
EOF
cat >"$DATA_HOME/bin/dividend-screen-report" <<EOF
#!/usr/bin/env bash
exec "$SKILL_DIR/scripts/report.sh" "\$@"
EOF
cat >"$DATA_HOME/bin/dividend-screen-bottom" <<EOF
#!/usr/bin/env bash
exec "$SKILL_DIR/scripts/bottom.sh" "\$@"
EOF
chmod +x "$DATA_HOME/bin/dividend-screen" "$DATA_HOME/bin/dividend-screen-report" \
         "$DATA_HOME/bin/dividend-screen-bottom"

# ---------- 自检 ----------
if [ "$SKIP_CHECK" -eq 0 ]; then
  echo
  if DIVIDEND_SCREEN_HOME="$DATA_HOME" "$PY" "$SKILL_DIR/scripts/dividend_screen.py" --check; then
    :
  else
    echo
    echo "⚠ 接口自检没有全部通过。常见原因：token 无效、账号积分/权限不足、网关地址错误。"
    echo "  修正 config.json 后重跑：$SKILL_DIR/scripts/run.sh --check"
  fi
fi

cat <<EOF

✓ 安装完成
  skill：$SKILL_DIR
  数据：$DATA_HOME/data
  配置：$CFG

在 DSH 里说一句：
  用高股息策略帮我筛选一下 A 股
  或：用底部预测算一下这些股票的买点

命令行小样本验证：
  $DATA_HOME/bin/dividend-screen --limit 30 --skip-minute
  $DATA_HOME/bin/dividend-screen-bottom --codes 600795,002060 -v

后台全量：
  $SKILL_DIR/scripts/run.sh --background        # 高股息选股
  $SKILL_DIR/scripts/bottom.sh --background     # 股票底部预测

出报告（仅高股息选股）：
  $SKILL_DIR/scripts/report.sh
EOF
