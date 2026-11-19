#!/usr/bin/env python
# coding: utf-8
"""股票底部预测程序 —— Tushare 版

把「股票底部预测」的数据层从腾讯/新浪免费接口换成 Tushare Pro。
指标公式、输出列、取数口径与腾讯版（bottom_forecast_tencent.py）逐字一致，
只替换数据层，所以两版结果可以直接逐列比对（见 --merge / 比对脚本）。

================================================================
一、接口对照
================================================================
  腾讯/新浪版                        Tushare 版
  ---------------------------------------------------------------
  新浪 CN_MarketData.getKLineData    daily(trade_date=...)      股票日线
                                     fund_daily(trade_date=...) ETF/基金日线
  腾讯 mkline（1 分钟）              stk_mins(freq='1min')      股票+ETF 分钟线
  用 sz000001 日线反推交易日          trade_cal(exchange='SSE')  交易日历
  本地 equity_list.txt               本地 equity_list.txt（自动去重）

================================================================
二、口径差异（只影响数值细节，不影响公式）
================================================================
  1) daily / fund_daily / stk_mins 都是**不复权**价，与腾讯版一致。
  2) daily.vol 与 fund_daily.vol 单位是「手」，×100 折成「股」后与分钟线
     （stk_mins.vol 单位是股）比较；腾讯版用新浪日线（本来就是股），
     两边最终都是「股」，放量判据量纲一致。
  3) stk_mins 返回 241 根（含 09:30 集合竞价根）。实测 241 根与裁掉后的
     240 根极值完全相同，本版**全部保留不裁剪**——集合竞价是真实成交，
     日线的高低点也包含它，保留才和 daily 自洽。
  4) Tushare 价格带浮点噪声（如 5.470000000000003），所有输出统一 round(2)，
     与腾讯版一致。
  5) 最高/最低点位置仍取「最后一次出现」（等价原版 tail(1)）。

================================================================
三、缓存分层（这是每天跑的时候真正决定耗时的地方）
================================================================
  ┌ 数据 ──────────────┬ 缓存键 ───────────────┬ TTL ──┬ 为什么
  │ 历史日线/ETF 日线   │ (接口, trade_date)     │ 永久   │ 收盘后即终态，永不变化
  │ 历史分钟线          │ (ts_code, 当日区间)    │ 永久   │ 同上
  │ 当日日线/当日分钟   │ (接口/代码, 当日)      │ 6 小时 │ 防盘中半日数据被永久固化
  │ 交易日历            │ (自然年)              │ 30 天  │ 年内 key 稳定，节假日调整靠 TTL
  └────────────────────┴───────────────────────┴───────┴──────────────
  结论：
    * 每天**必须新取**的只有「目标交易日的日线（1 次）」+
      「1 次 ETF 批量日线」+「该日 1 分钟线（668 只按 30 只一批 = 23 次）」
      ≈ **25 次调用**。
    * ma20、5 日均量、前收 需要的历史 20 个交易日**全部命中缓存**，
      不会因为每天跑而重复请求。
    * 同一天重跑 = **0 次网络请求**（实测 695 次缓存命中、0.9 秒）；
      隔日重跑 ≈ 25 次。
    * 停牌股在日线阶段就被识别并跳过，不会浪费一次分钟线请求。

================================================================
四、运行
================================================================
  源码目录（4tushare/）：
    .venv/bin/python scripts/bottom_forecast_tushare.py -v              # 全量
    .venv/bin/python scripts/bottom_forecast_tushare.py --codes 600795,002060 -v
    .venv/bin/python scripts/bottom_forecast_tushare.py --date 2026-11-19 --limit 50
    .venv/bin/python scripts/bottom_forecast_tushare.py --refresh        # 忽略缓存

  装成 skill 之后（dividend-screen skill 的第二功能）：
    ~/.agents/skills/dividend-screen/scripts/bottom.sh                  # 全量
    ~/.agents/skills/dividend-screen/scripts/bottom.sh --background     # 后台
    ~/.agents/skills/dividend-screen/scripts/bottom.sh --codes 600795,002060 -v

  数据/缓存/结果都落在 $DIVIDEND_SCREEN_HOME（默认 ~/.local/share/dividend-screen），
  股票池默认取 <skill>/assets/equity_list.txt。

  token 取法（优先级从高到低）：--token / 环境变量 TUSHARE_TOKEN / config.json
  api_url 同理：--api-url / TUSHARE_API_URL / config.json 的 tushare_api_url
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd

try:
    import tushare as ts
except ImportError:  # 允许在没有 tushare 的环境里做纯函数单测
    ts = None


BASE_DIR = Path(__file__).resolve().parent           # scripts/
ROOT_DIR = BASE_DIR.parent                           # 4tushare/ 或 skill 根目录
# 运行时目录：
#   * 装成 skill 后，install.sh 会写入 runtime.env 并把 DIVIDEND_SCREEN_HOME
#     指向 ~/.local/share/dividend-screen（数据/缓存/config.json 都在那）；
#   * 在 4tushare 源码目录直接运行时，退回脚本上一级（= 4tushare/）。
RUNTIME_HOME = Path(os.environ.get("DIVIDEND_SCREEN_HOME", str(ROOT_DIR))).expanduser()
DATA_DIR = RUNTIME_HOME / "data"
CACHE_DIR = DATA_DIR / "cache"

# ── 策略参数（与腾讯版 / 聚宽原版严格一致，勿改） ──────────────
MIN_VOLUME_MULTIPLIER    = 1.5
VOLUME_LOOKBACK_DAYS     = 5
MA20_DEVIATION_THRESHOLD = 0.85
MA_WINDOW                = 20

# 需要多少根历史日线：ma20 需 20 根 + 前收需 1 根 = 21 根；
# 多取一些是为了覆盖停牌造成的缺口，避免逐只补拉。
DEFAULT_HISTORY_DAYS = 40

OUTPUT_COLS = [
    "p_close", "open", "close", "high", "low",
    "low_after_high", "high_after_low",
    "hc", "hd", "hx", "ha",
    "lc", "ld", "lx", "la", "no", "ma20",
]

# 需要显式 fields 的接口（自建网关不传会报 invalid_params）
DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol"
MINUTE_FIELDS = "ts_code,trade_time,open,high,low,close,vol"

# 错误分类：限流类 → 等待重试；权限/参数类 → 直接失败不重试
RATE_LIMIT_HINTS = (
    "您请求速度过快", "请求速度过快", "rate_limited", "每分钟", "频率", "最多访问",
    "upstream_pool_exhausted", "当前不可用", "application_capacity", "edge_conn_limit",
    "额度已达到限制",
)
FATAL_HINTS = (
    "权限", "invalid_params", "unknown_api", "date_range_too_large",
    "api_not_enabled", "api_scope_denied",
)


# ============================================================
# 日志
# ============================================================
class Log:
    def __init__(self, verbose=True):
        self.verbose = verbose

    def __call__(self, *a):
        if self.verbose:
            print(*a, flush=True)

    @staticmethod
    def always(*a):
        print(*a, flush=True)


# ============================================================
# Tushare 客户端：限速 + 重试 + 分层磁盘缓存
# ============================================================
class TushareClient:
    """对 pro_api 的薄封装。

    - 限速：两次请求至少间隔 min_interval 秒（自建网关对连发非常敏感）；
    - 重试：限流等待 20~60s 再试，其余错误指数退避；权限/参数错误立刻抛；
    - 缓存：按 (接口, 参数) 落到 data/cache/<接口>/<hash>.pkl，
            支持 cache_key（稳定键）与 ttl_days（有效期）两张策略；
    - 网关：api_url 覆盖 SDK 默认地址，适配自建/代理网关。
    """

    def __init__(self, token, api_url=None, cache_dir=CACHE_DIR,
                 min_interval=0.35, use_cache=True, refresh=False, retries=5):
        if ts is None:
            raise RuntimeError("未安装 tushare，请先执行：pip install tushare")
        try:
            self.pro = ts.pro_api(token)
        except Exception as e:                      # tushare 抛普通 Exception
            raise RuntimeError(f"Tushare 初始化失败：{e}") from e
        if api_url:
            setattr(self.pro, "_DataApi__http_url", str(api_url))
        self.api_url = api_url

        self.cache_dir = Path(cache_dir)
        self.min_interval = max(float(min_interval), 0.0)
        self.use_cache = bool(use_cache)
        self.refresh = bool(refresh)
        self.retries = int(retries)
        self._last_call = 0.0
        self.n_calls = 0
        self.n_cache_hits = 0

    # ---------- 缓存 ----------
    def _cache_path(self, api, params, cache_key=None):
        if cache_key:
            raw = {"key": str(cache_key), "fields": params.get("fields")}
        else:
            raw = params
        digest = hashlib.md5(
            json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:16]
        return self.cache_dir / api / f"{digest}.pkl"

    @staticmethod
    def _fresh(path, ttl_days):
        """ttl_days=None 表示永久有效。"""
        if ttl_days is None:
            return True
        try:
            age_days = (time.time() - path.stat().st_mtime) / 86400.0
        except OSError:
            return True
        return age_days <= float(ttl_days)

    def _read_cache(self, path):
        if not (self.use_cache and path.exists()):
            return None
        try:
            return pd.read_pickle(path)
        except Exception:
            return None

    def _write_cache(self, path, df):
        if not self.use_cache or df is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
            df.to_pickle(tmp)
            os.replace(tmp, path)
        except Exception:
            pass

    # ---------- 限速 ----------
    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    # ---------- 缓存读写（分开暴露：批量取数后可按「单只」粒度回填）----------
    def cache_get(self, api, params, ttl_days=None, cache_key=None):
        if not (self.use_cache and not self.refresh):
            return None
        path = self._cache_path(api, params, cache_key)
        df = self._read_cache(path)
        if df is None or not self._fresh(path, ttl_days):
            return None
        self.n_cache_hits += 1
        return df

    def cache_put(self, api, params, df, cache_key=None):
        self._write_cache(self._cache_path(api, params, cache_key), df)

    # ---------- 真实调用（不读不写缓存）：限速 + 重试 ----------
    def fetch(self, api, **params):
        params = {k: v for k, v in params.items() if v is not None}
        last_err = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                df = getattr(self.pro, api)(**params)
                self.n_calls += 1
                return df if df is not None else pd.DataFrame()
            except Exception as e:
                last_err = e
                msg = str(e)
                low = msg.lower()

                if "token" in low and ("不对" in msg or "无效" in msg or "invalid" in low):
                    raise RuntimeError(f"Tushare token 无效：{msg}") from e
                if any(k in msg for k in FATAL_HINTS):
                    raise RuntimeError(f"接口 {api} 调用被拒绝：{msg}") from e

                if any(k in msg for k in RATE_LIMIT_HINTS):
                    wait = 20 * (attempt + 1)      # 20 / 40 / 60 / 80 / 100
                else:
                    wait = 2 ** attempt            # 1 / 2 / 4 / 8 / 16

                if attempt == self.retries - 1:
                    raise RuntimeError(f"Tushare 接口 {api} 调用失败（{msg[:100]}）") from e
                print(f"  ⚠ {api} 调用失败（{msg[:60]}），{wait}s 后重试 "
                      f"({attempt + 1}/{self.retries}) ...", flush=True)
                time.sleep(wait)

        raise RuntimeError(f"Tushare 接口 {api} 调用失败：{last_err}")

    # ---------- 带缓存的调用 ----------
    def call(self, api, cache_key=None, ttl_days=None, **params):
        params = {k: v for k, v in params.items() if v is not None}
        cached = self.cache_get(api, params, ttl_days, cache_key)
        if cached is not None:
            return cached
        df = self.fetch(api, **params)
        self.cache_put(api, params, df, cache_key)
        return df


# ============================================================
# token / api_url
# ============================================================
def _read_config(name):
    """依次在运行时目录、源码/skill 根目录找配置文件。

    装成 skill 后 config.json 在 ~/.local/share/dividend-screen/（600 权限）；
    源码目录直接运行时在 4tushare/ 下。
    """
    for base in (RUNTIME_HOME, ROOT_DIR):
        p = base / name
        if not p.exists():
            continue
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def resolve_token(arg=None):
    if arg:
        return arg.strip()
    env = os.environ.get("TUSHARE_TOKEN")
    if env:
        return env.strip()
    for name in ("config.json", "config.example.json"):
        token = _read_config(name).get("tushare_token")
        if token and "你的" not in token and "xxx" not in token.lower():
            return token.strip()
    raise SystemExit(
        "找不到 Tushare token。三种放法：\n"
        "  1) --token 你的token\n"
        "  2) export TUSHARE_TOKEN=你的token\n"
        "  3) cp config.example.json config.json 后填入 tushare_token"
    )


def resolve_api_url(arg=None):
    if arg:
        return arg.strip()
    env = os.environ.get("TUSHARE_API_URL")
    if env:
        return env.strip()
    for name in ("config.json", "config.example.json"):
        url = _read_config(name).get("tushare_api_url")
        if url and "http" in url:
            return url.strip()
    return None                                   # 走官方源


# ============================================================
# 代码归一化：6 位数字 -> Tushare ts_code
# ============================================================
def to_ts_code(raw) -> str | None:
    """600795 -> 600795.SH；002060 -> 002060.SZ；430047 -> 430047.BJ；159790 -> 159790.SZ"""
    s = str(raw).strip().upper().replace(" ", "")
    if not s or s in ("NAN", "NONE"):
        return None
    if "." in s:                                   # 已经是 ts_code
        body, suf = s.split(".", 1)
        body = "".join(c for c in body if c.isdigit()).zfill(6)
        return f"{body}.{suf.replace('XSHG', 'SH').replace('XSHE', 'SZ')}"
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return None
    digits = digits.zfill(6)
    if digits[0] in ("6", "9"):
        return f"{digits}.SH"
    if digits[:2] in ("00", "30", "15", "16", "12", "13"):
        return f"{digits}.SZ"
    if digits[0] in ("4", "8") or digits[:2] in ("43", "83", "87", "92"):
        return f"{digits}.BJ"
    return f"{digits}.SH"


def is_fund(ts_code: str) -> bool:
    """ETF/LOF/基金：走 fund_daily 而不是 daily。"""
    body, _, mkt = ts_code.partition(".")
    if mkt == "SH":
        return body.startswith(("5", "58"))       # 51/52/56/58 开头
    if mkt == "SZ":
        return body.startswith(("15", "16"))      # 15/16 开头
    return False


def default_equity_list() -> Path:
    """股票池默认位置。

    依次找：运行时目录 → 源码/skill 根目录 → skill 的 assets/。
    返回第一个存在的；都不存在时返回源码根下那个，好让报错信息里出现具体路径。
    """
    for p in (RUNTIME_HOME / "equity_list.txt",
              ROOT_DIR / "equity_list.txt",
              ROOT_DIR / "assets" / "equity_list.txt"):
        if p.exists():
            return p
    return ROOT_DIR / "equity_list.txt"


def load_equity_list(path: Path) -> tuple[list[str], list[str]]:
    """返回 (原始列, ts_code 列)。股票池里的重复代码会去重（保留首次出现）。"""
    if not path.exists():
        raise RuntimeError(f"找不到股票池文件：{path}")
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    col = "security" if "security" in df.columns else df.columns[0]

    raws, codes, seen, dup = [], [], set(), []
    for v in df[col].tolist():
        code = to_ts_code(v)
        if not code:
            continue
        if code in seen:
            dup.append(code)
            continue
        seen.add(code)
        raws.append(str(v).strip())
        codes.append(code)
    if not codes:
        raise RuntimeError(f"股票池为空或格式错误：{path}")
    if dup:
        print(f"  ℹ 股票池内有重复代码 {len(dup)} 个，已去重：{', '.join(sorted(set(dup)))}")
    return raws, codes


# ============================================================
# 交易日历（按自然年缓存，年内 key 稳定）
# ============================================================
def _year_calendar(client: TushareClient, year: int) -> list[pd.Timestamp]:
    df = client.call(
        "trade_cal",
        cache_key=f"trade_cal_{year}",
        ttl_days=30,
        exchange="SSE",
        start_date=f"{year}0101",
        end_date=f"{year}1231",
        is_open="1",
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    if df is None or df.empty or "cal_date" not in df.columns:
        raise RuntimeError(f"trade_cal {year} 返回为空，请检查 token/网络。")
    days = pd.to_datetime(df["cal_date"].astype(str), format="%Y%m%d", errors="coerce")
    return sorted(d for d in days.dropna().unique().tolist())


def trade_days_between(client, start, end) -> list[pd.Timestamp]:
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    for y in range(start.year, end.year + 1):
        out += [d for d in _year_calendar(client, y) if start <= d <= end]
    return sorted(set(out))


def last_trade_days(client, end_date, n: int) -> list[pd.Timestamp]:
    """<= end_date 的最近 n 个交易日，升序。"""
    end = pd.Timestamp(end_date)
    days = trade_days_between(client, end - pd.Timedelta(days=400), end)
    if not days:
        raise RuntimeError(f"{end_date} 之前没有交易日。")
    return days[-n:]


def resolve_end_date(client, requested=None, now=None) -> str:
    """复刻原版：盘中(<15:00)且当天是交易日，则回滚到前一交易日。"""
    now = now or dt.datetime.now()
    if requested:
        req = pd.Timestamp(requested).normalize()
        days = trade_days_between(client, req - pd.Timedelta(days=30), req)
        if not days:
            return req.strftime("%Y-%m-%d")
        return days[-1].strftime("%Y-%m-%d")

    today = pd.Timestamp(now.date())
    days = trade_days_between(client, today - pd.Timedelta(days=30), today)
    if not days:
        return today.strftime("%Y-%m-%d")
    if days[-1] == today and (now.hour, now.minute) < (15, 0):
        days = [d for d in days if d < today]
        if days:
            return days[-1].strftime("%Y-%m-%d")
    return days[-1].strftime("%Y-%m-%d")


# ============================================================
# 缓存 TTL 策略
# ============================================================
def ttl_for_trade_date(day) -> float | None:
    """已收盘的历史交易日 → 永久缓存；当天 → 6 小时内有效。

    当天给个短 TTL，是为了防止盘中（数据不全）跑出来的结果被当成终态永久固化。
    """
    d = pd.Timestamp(day).date()
    return None if d < dt.date.today() else 0.25


# ============================================================
# 数据层 A：按交易日批量取日线（股票 + ETF）
# ============================================================
def load_daily_panel(client: TushareClient, dates, codes, log) -> dict[str, pd.DataFrame]:
    """取日线面板，最后按股票切分。

    两条取数路径（都靠缓存，第二天只补一天）：
      * 股票：`daily(trade_date=...)` **整市场一天一次**——约 5600 行/次，
        21 根历史日线只需 21 次调用；逐只则要 669 次。
      * ETF/基金：`fund_daily(ts_code="A,B,...", start_date, end_date)`
        按代码批量取一段区间——26 只 ETF 只要 1 次调用，而不是每个交易日
        都去拉一份整市场 ETF 日线（那要 40 次、且几乎全是无用行）。
    """
    code_set = set(codes)
    fund_codes = [c for c in codes if is_fund(c)]
    frames = []

    for i, d in enumerate(dates, 1):
        d8 = pd.Timestamp(d).strftime("%Y%m%d")
        try:
            df = client.call("daily", trade_date=d8, ttl_days=ttl_for_trade_date(d),
                             fields=DAILY_FIELDS)
            if df is not None and not df.empty:
                frames.append(df)
        except Exception as e:
            log(f"  ⚠ daily {d8} 拉取失败：{str(e)[:80]}")
        if i % 10 == 0 or i == len(dates):
            log(f"  日线进度 {i}/{len(dates)}（{d8}）")

    if fund_codes:
        start = (pd.Timestamp(dates[0]) - pd.Timedelta(days=15)).strftime("%Y%m%d")
        end8 = pd.Timestamp(dates[-1]).strftime("%Y%m%d")
        try:
            # 注意：不要给 cache_key —— 这里的参数里含 ts_code 列表，
            # 让 key 由完整参数决定，才不会出现「小样本跑一次把全量缓存写残」的问题。
            fdf = client.call(
                "fund_daily", ttl_days=ttl_for_trade_date(dates[-1]),
                ts_code=",".join(fund_codes), start_date=start, end_date=end8,
                fields=DAILY_FIELDS,
            )
            if fdf is not None and not fdf.empty:
                frames.append(fdf)
                got = set(fdf["ts_code"].astype(str))
                missing = [c for c in fund_codes if c not in got]
                log(f"  ETF/基金日线：{len(fund_codes)} 只、{len(fdf)} 行（1 次调用）")
                if missing:
                    log(f"  ⚠ 其中 {len(missing)} 只在区间内无数据："
                        f"{', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}")
        except Exception as e:
            log(f"  ⚠ fund_daily 拉取失败（{len(fund_codes)} 只 ETF 将被跳过）：{str(e)[:80]}")

    if not frames:
        raise RuntimeError("日线数据全为空，请检查 token/网络/接口权限。")

    panel = pd.concat(frames, ignore_index=True)
    panel["ts_code"] = panel["ts_code"].astype(str)
    panel["trade_date"] = pd.to_datetime(
        panel["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    panel = panel[panel["ts_code"].isin(code_set)]
    # 同一 (股票, 日期) 若 daily 与 fund_daily 都给，保留后出现的 fund_daily
    panel = panel.dropna(subset=["trade_date"]).drop_duplicates(
        subset=["ts_code", "trade_date"], keep="last"
    )
    for col in ("open", "high", "low", "close", "vol"):
        panel[col] = pd.to_numeric(panel[col], errors="coerce")

    return {code: sub.sort_values("trade_date").reset_index(drop=True)
            for code, sub in panel.groupby("ts_code", sort=False)}


# ============================================================
# 数据层 B：1 分钟线（股票 + ETF 都支持，支持多代码批量）
# ============================================================
# 关键实测结论：
#   * stk_mins 的 ts_code 可以逗号分隔批量取，但网关有 8000 行硬上限；
#     241 根/只 × N 只，N=30 时 7230 行安全，N=40 会被截断成 216 根/只。
#   * 因此按 30 只一批请求（669 只 => 23 次调用，而不是 669 次）。
#   * 缓存仍以「单只股票」为单元：批量拿回来再按股票拆开回填，
#     这样以后无论批量还是单只调用都能复用同一份缓存。
MINUTE_BATCH = 30
MINUTE_ROW_CAP = 8000


def _minute_params(code: str, day) -> dict:
    d = pd.Timestamp(day).strftime("%Y-%m-%d")
    return dict(ts_code=code, freq="1min",
                start_date=f"{d} 09:00:00", end_date=f"{d} 15:00:00",
                fields=MINUTE_FIELDS)


def _normalize_minute(df) -> pd.DataFrame:
    """统一成 trade_time 升序、数值列可计算。空/异常返回空表。"""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    if "high" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
    df = df.dropna(subset=["trade_time"]).sort_values("trade_time").reset_index(drop=True)
    for col in ("high", "low", "close", "vol"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def fetch_minutes(client: TushareClient, codes, day, log) -> dict[str, pd.DataFrame]:
    """取指定交易日的 1 分钟线，返回 {ts_code: DataFrame}（取不到的给空表）。

    流程：先查单只缓存 → 剩下的按 30 只一批批量请求 → 按股票拆开回填缓存
    → 漏码的再单独补一次。
    """
    ttl = ttl_for_trade_date(day)
    out, pending = {}, []

    for code in codes:
        cached = client.cache_get("stk_mins", _minute_params(code, day), ttl)
        if cached is None:
            pending.append(code)
        else:
            out[code] = _normalize_minute(cached)

    if pending:
        log(f"  分钟线待取 {len(pending)} 只（缓存命中 {len(out)} 只），"
            f"按 {MINUTE_BATCH} 只/批发送")
    singles = 0
    for start in range(0, len(pending), MINUTE_BATCH):
        batch = pending[start:start + MINUTE_BATCH]
        df = None
        try:
            df = client.fetch("stk_mins", **_minute_params(",".join(batch), day))
        except Exception as e:
            log(f"  ⚠ 批量分钟线失败（{len(batch)} 只）：{str(e)[:70]}，改为逐只取")

        truncated = df is not None and len(df) >= MINUTE_ROW_CAP
        if truncated:
            log(f"  ⚠ 批量返回触达 {MINUTE_ROW_CAP} 行上限，本批改为逐只重取")

        groups = {}
        if df is not None and not df.empty and not truncated and "ts_code" in df.columns:
            groups = {str(c): sub for c, sub in df.groupby("ts_code", sort=False)}

        for code in batch:
            sub = groups.get(code)
            if sub is None or sub.empty:          # 漏码/被截断 → 单只补取
                singles += 1
                try:
                    sub = client.fetch("stk_mins", **_minute_params(code, day))
                except Exception:
                    sub = None
            if sub is not None and isinstance(sub, pd.DataFrame):
                client.cache_put("stk_mins", _minute_params(code, day), sub)
                out[code] = _normalize_minute(sub)
            else:
                out[code] = pd.DataFrame()

        done = min(start + MINUTE_BATCH, len(pending))
        log(f"  分钟线进度 {done}/{len(pending)}")

    if singles:
        log(f"  其中 {singles} 只走了单只补取")
    return out


# ============================================================
# 指标层：纯计算，无 I/O，可离线单测
# ============================================================
class SkipStock(Exception):
    """该股本次无法算出结果（停牌/无分钟线/价格异常）。"""


def compute_forecast(daily: pd.DataFrame, minute: pd.DataFrame) -> dict:
    """日线 + 分钟线 → 预测指标。公式与腾讯版/聚宽原版逐字一致。

    c_high / c_low 取【日线】；high / low / low_after_high / high_after_low
    取【分钟线】——这正是原版循环内的取值方式。
    """
    if daily is None or len(daily) < 2:
        raise SkipStock("日线历史不足2根")

    last, prev = daily.iloc[-1], daily.iloc[-2]
    p_close    = float(prev["close"])      # 前收
    open_val   = float(last["open"])       # 今开（日线）
    close_val  = float(last["close"])      # 今收（日线）
    c_high     = float(last["high"])       # 当日最高（日线，进公式）
    c_low      = float(last["low"])        # 当日最低（日线，进公式）
    if min(p_close, open_val, close_val, c_high, c_low) <= 0:
        raise SkipStock("日线价格异常(0值)")

    if minute is None or minute.empty:
        raise SkipStock("无当日分钟线")

    high = float(minute["high"].max())     # 分钟口径最高（写入 high 列）
    low = float(minute["low"].min())       # 分钟口径最低（写入 low 列）

    # 最高/最低价位置：取最后一次出现（与原版 tail(1) 一致）
    high_pos = minute.index[minute["high"] == high][-1]
    low_pos = minute.index[minute["low"] == low][-1]
    after_high = minute.loc[high_pos:]
    after_low = minute.loc[low_pos:]

    # 5 日均量：日线 vol 单位「手」→ ×100 折「股」，与分钟线量纲统一
    vol5 = daily["vol"].tail(VOLUME_LOOKBACK_DAYS)
    if len(vol5) >= VOLUME_LOOKBACK_DAYS and vol5.notna().all():
        avg_volume_5d = float(vol5.mean()) * 100.0
    else:
        avg_volume_5d = float(minute["vol"].mean())
    threshold = avg_volume_5d * MIN_VOLUME_MULTIPLIER

    surge_lows = after_high.loc[after_high["vol"] > threshold, "low"]
    surge_highs = after_low.loc[after_low["vol"] > threshold, "high"]
    low_after_high = float(surge_lows.min()) if len(surge_lows) else float(after_high["low"].min())
    high_after_low = float(surge_highs.max()) if len(surge_highs) else float(after_low["high"].max())

    # ── 预测公式（与原版逐字一致；c.high / c.low 用日线）──
    hc = (c_high / p_close) * close_val
    hd = (c_high / open_val) * close_val
    hx = (high_after_low / c_low) * close_val
    ha = (hc + hd + hx) / 3
    lc = (c_low / open_val) * close_val
    ld = (c_low / p_close) * close_val
    lx = (low_after_high / c_high) * close_val
    la = (lc + ld + lx) / 3
    no = round((open_val / p_close) * close_val, 2)
    ma20 = float(daily["close"].tail(MA_WINDOW).mean()) if len(daily) >= MA_WINDOW else close_val
    signal = "weak" if lx < ma20 * MA20_DEVIATION_THRESHOLD else "normal"

    return {
        "p_close": p_close, "open": open_val, "close": close_val,
        "high": round(high, 2), "low": round(low, 2),
        "low_after_high": round(low_after_high, 2),
        "high_after_low": round(high_after_low, 2),
        "hc": round(hc, 2), "hd": round(hd, 2), "hx": round(hx, 2), "ha": round(ha, 2),
        "lc": round(lc, 2), "ld": round(ld, 2), "lx": round(lx, 2), "la": round(la, 2),
        "no": no, "ma20": round(ma20, 2),
        "signal_strength": signal,
        "_minute_bars": len(minute),
    }


# ============================================================
# 主流程
# ============================================================
def run(opts):
    log = Log(verbose=not opts.quiet)
    client = TushareClient(
        token=resolve_token(opts.token),
        api_url=resolve_api_url(opts.api_url),
        min_interval=opts.interval,
        use_cache=not opts.no_cache,
        refresh=opts.refresh,
        retries=opts.retries,
    )
    if client.api_url:
        log(f"  网关: {client.api_url}")
    t_start = time.time()

    log(">>> 步骤1: 读取股票池 ...")
    eq_path = Path(opts.equity_list) if opts.equity_list else default_equity_list()
    raws, codes = load_equity_list(eq_path)
    if opts.codes:
        want = {to_ts_code(x) for x in opts.codes.split(",") if x.strip()}
        keep = [i for i, c in enumerate(codes) if c in want]
        raws = [raws[i] for i in keep]
        codes = [codes[i] for i in keep]
    if opts.limit > 0:
        raws, codes = raws[:opts.limit], codes[:opts.limit]
    if opts.offset > 0:
        raws, codes = raws[opts.offset:], codes[opts.offset:]
    if not codes:
        raise RuntimeError("股票池筛选后为空。")
    log(f"  股票池 {eq_path.name}，本次 {len(codes)} 只")

    log("\n>>> 步骤2: 确定交易截止日 ...")
    end_date = resolve_end_date(client, opts.date)
    log(f"  end_date = {end_date}")

    log(f"\n>>> 步骤3: 批量拉日线（最近 {opts.history_days} 个交易日）...")
    dates = last_trade_days(client, end_date, opts.history_days)
    panel = load_daily_panel(client, dates, codes, log)
    log(f"  命中 {len(panel)} 只股票的日线")

    out_dir = Path(opts.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{end_date.replace('-', '')}_forecast_price.csv"

    # 断点续跑：读已有结果，只补算 0 值行
    prev = {}
    if opts.merge and out_path.exists():
        try:
            pdf = pd.read_csv(out_path, index_col=0, dtype={"security": str}, encoding="utf-8-sig")
            for idx, row in pdf.iterrows():
                if float(row.get("close", 0) or 0) > 0:
                    prev[str(idx).zfill(6)] = row
            log(f"  续跑：已有 {len(prev)} 只有效结果")
        except Exception:
            pass

    def build_frame(rows):
        frame = pd.DataFrame(0.0, index=pd.Index(raws, name="security"), columns=OUTPUT_COLS)
        frame["signal_strength"] = "normal"
        for code, r in rows.items():
            raw = code2raw[code]
            for col in OUTPUT_COLS:
                frame.at[raw, col] = r[col]
            frame.at[raw, "signal_strength"] = r["signal_strength"]
        for raw in frame.index:
            key = str(raw).zfill(6)
            if float(frame.at[raw, "close"]) == 0 and key in prev:
                row = prev[key]
                for col in OUTPUT_COLS:
                    if col in row.index:
                        frame.at[raw, col] = row[col]
                frame.at[raw, "signal_strength"] = row.get("signal_strength", "normal")
        return frame

    code2raw = dict(zip(codes, raws))

    # ── 先用日线筛掉停牌/退市，再只为能交易的股票取分钟线 ──
    log("\n>>> 步骤4: 筛出当日可计算的股票 ...")
    skip = Counter()
    tradable = []
    for code in codes:
        day = panel.get(code)
        if day is None or day.empty:
            skip["无日线（停牌/退市/不在 daily 覆盖）"] += 1
            continue
        if pd.Timestamp(day.iloc[-1]["trade_date"]) != pd.Timestamp(end_date):
            skip["当日无日线（停牌）"] += 1
            continue
        tradable.append(code)
    log(f"  可计算 {len(tradable)} 只，日线阶段已排除 {len(codes) - len(tradable)} 只")

    log(f"\n>>> 步骤5: 取 {end_date} 的 1 分钟线 ...")
    minutes = fetch_minutes(client, tradable, end_date, log)
    have_minute = sum(1 for c in tradable if not minutes.get(c, pd.DataFrame()).empty)
    if tradable and have_minute == 0:
        raise RuntimeError(
            "所有股票都取不到分钟线——大概率是网关未开通「A股历史分钟」"
            "(stk_mins) 权限，或该日不是交易日。"
        )
    log(f"  分钟线可用 {have_minute}/{len(tradable)} 只")

    log(f"\n>>> 步骤6: 逐股计算（共 {len(tradable)} 只）...")
    rows = {}
    for i, code in enumerate(tradable, 1):
        raw = code2raw[code]
        try:
            minute = minutes.get(code, pd.DataFrame())
            if minute.empty:
                raise SkipStock("无当日分钟线")
            r = compute_forecast(panel[code], minute)
            rows[code] = r
            if opts.verbose:
                log(f"  [{i}/{len(tradable)}] {raw:>6} ha={r['ha']:.2f} la={r['la']:.2f} "
                    f"lx={r['lx']:.2f} ma20={r['ma20']:.2f} {r['signal_strength']} "
                    f"(分钟{r['_minute_bars']}根)")
            elif i % 100 == 0 or i == len(tradable):
                log(f"  进度 {i}/{len(tradable)}，成功 {len(rows)} ...")
        except SkipStock as e:
            skip[str(e)] += 1
        except Exception as e:
            skip[f"异常:{str(e)[:40]}"] += 1
            if opts.verbose:
                log(f"  [{i}] {raw} ERR {e}")

    log("\n>>> 步骤7: 汇总保存 ...")
    rs = build_frame(rows)
    rs.to_csv(out_path, encoding="utf-8-sig")
    weak = int((rs["signal_strength"] == "weak").sum())

    Log.always("\n==================== 统计摘要 ====================")
    Log.always(f"  股票池: {len(codes)} 只 | 成功: {len(rows)} 只 | 弱势信号: {weak} 只")
    Log.always(f"  输出: {out_path}")
    if skip:
        Log.always("  跳过明细:")
        for reason, n in skip.most_common():
            Log.always(f"    - {reason}: {n}")
    Log.always(f"  HTTP 请求 {client.n_calls} 次，缓存命中 {client.n_cache_hits} 次，"
               f"耗时 {time.time() - t_start:.1f}s")
    Log.always("=================================================")
    return out_path


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="股票底部预测（Tushare 版）")
    p.add_argument("--date", help="截止交易日 YYYY-MM-DD，默认按盘中规则自动判定")
    p.add_argument("--equity-list", help="股票池 txt 路径，默认取 <运行时目录>/equity_list.txt "
                                         "或 <skill 根>/assets/equity_list.txt")
    p.add_argument("--out-dir", default=str(DATA_DIR), help="输出目录")
    p.add_argument("--codes", help="只跑指定代码，逗号分隔（6 位代码或 ts_code）")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 只")
    p.add_argument("--offset", type=int, default=0, help="跳过前 N 只")
    p.add_argument("--merge", action="store_true", help="与已有 CSV 合并（分批续跑）")
    p.add_argument("--history-days", type=int, default=DEFAULT_HISTORY_DAYS,
                   help=f"往前取多少个交易日算 ma20（默认 {DEFAULT_HISTORY_DAYS}）")
    p.add_argument("--interval", type=float, default=0.35, help="请求最小间隔秒（默认 0.35）")
    p.add_argument("--retries", type=int, default=5, help="单请求重试次数")
    p.add_argument("--token", help="Tushare token（也可用 TUSHARE_TOKEN 或 config.json）")
    p.add_argument("--api-url", help="Tushare 网关地址（覆盖官方源）")
    p.add_argument("--no-cache", action="store_true", help="禁用磁盘缓存")
    p.add_argument("--refresh", action="store_true", help="忽略缓存重新取数")
    p.add_argument("--verbose", "-v", action="store_true", help="逐股明细")
    p.add_argument("--quiet", "-q", action="store_true", help="静默进度")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        run(args)
    except Exception as e:
        Log.always(f"✗ 失败：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
