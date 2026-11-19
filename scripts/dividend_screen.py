#!/usr/bin/env python
# coding: utf-8
# ============================================================
# 高股息 / 自由现金流策略 —— 红利选股 v10（Tushare Pro 迁移版）
#
# 原版：../高股息策略量化选股v10.py（聚宽研究环境）
# 参照：../高股息策略量化选股v10_bigquant.py（BigQuant 迁移版）
#
# 用法：
#   export TUSHARE_TOKEN=你的token        # 或写入 4tushare/config.json
#   python 高股息策略量化选股v10_tushare.py --date 2026-08-13
#
# 主要接口对照（聚宽 -> Tushare Pro）：
#   get_all_securities           -> stock_basic(list_status='L')
#   get_extras('is_st')          -> stock_st(trade_date=...)（失败回退到 name 含 ST）
#   get_industry(申万一级)        -> index_classify(SW2021) + index_member_all(l1_code=...)
#   finance.STK_XR_XD            -> dividend(ts_code=...) 的 cash_div_tax（每股税前，元）
#   indicator.adjusted_profit    -> fina_indicator.profit_dedt（年报，元）
#   cash_flow.net_operate_cash_flow -> cashflow.n_cashflow_act（年报，元）
#   valuation.capitalization     -> daily_basic.total_share（万股，年末）
#   valuation.pb_ratio/pe_ratio  -> daily_basic.pb / pe_ttm
#   balance.total_liability/assets -> fina_indicator.debt_to_assets（%）
#   get_price 日线 / 分钟         -> daily / stk_mins（均为不复权）
#   get_trade_days               -> trade_cal(exchange='SSE', is_open='1')
#
# 口径说明：
#   1) daily / stk_mins 都是不复权价，与原版“真实价格”口径一致（BigQuant 版
#      需要先除以 adjust_factor，Tushare 版不需要）。
#   2) 分红按“分红年度(end_date)”归属，只取 div_proc='实施' 且 ex_date <= 数据截止日，
#      与聚宽 report_date + plan_progress=='实施方案' 等价。
#   3) 分红金额 = 每股分红(元) × 年末总股本(万股) × 10000，单位元，与聚宽版一致。
#   4) 所有 Tushare 调用都带本地缓存（data/cache/），中断后重跑可断点续传。
# ============================================================

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import tushare as ts
except ImportError:  # 允许离线做单元测试时没有安装 tushare
    ts = None


BASE_DIR = Path(__file__).resolve().parent
# 运行时目录：skill 安装后数据/配置放在 ~/.local/share/dividend-screen，
# 源码直接运行时仍用脚本同级的 data/ 和 config.json
RUNTIME_HOME = Path(os.environ.get("DIVIDEND_SCREEN_HOME", str(BASE_DIR))).expanduser()
DATA_DIR = RUNTIME_HOME / "data"
CACHE_DIR = DATA_DIR / "cache"

# ────────────────────────────────────────────────────────────────
# 全局参数（与原版严格一致，可自行调整）
# ────────────────────────────────────────────────────────────────
LOOKBACK_YEARS = 5
MIN_CONTINUOUS_DIV_YEARS = 2
MIN_POSITIVE_PROFIT_YEARS = 3
MIN_CASH_COVER_RATIO = 0.5
MIN_AVG_DIV_YIELD = 4.0
MAX_CURRENT_DIV_YIELD = 8.0
MIN_LISTED_YEARS = 0

MAX_DEBT_RATIO = 70.0
MAX_PB = 1.5
PB_PERCENTILE_MAX = 30.0
PB_HISTORY_YEARS = 5

RISK_FREE_RATE = 2.7
ANCHOR_MULTIPLE_MIN = 1.3
ANCHOR_MULTIPLE_AVG = 1.0

# 次日最低价预测参数
MIN_VOLUME_MULTIPLIER = 1.5
VOLUME_LOOKBACK_DAYS = 5
MA20_DEVIATION_THRESHOLD = 0.85

# 行业白名单 / 规避名单（申万一级，2021 版名称，不带“I”后缀）
CASH_COW_INDUSTRIES = [
    "银行", "非银金融", "公用事业", "交通运输",
    "煤炭", "钢铁", "石油石化", "家用电器", "通信",
    "建筑装饰", "纺织服装", "食品饮料",
]
AVOID_INDUSTRIES = ["房地产", "传媒", "综合", "国防军工", "计算机"]

# 不同申万版本下的行业别名（2014 -> 2021 / 2021 内差异）
INDUSTRY_ALIASES = {
    "纺织服装": ["纺织服饰"],
    "石油石化": ["石油石化", "化工", "采掘"],
    "煤炭": ["煤炭", "采掘"],
}


# ============================================================
# Tushare 客户端：统一限速、重试、本地缓存
# ============================================================
class TushareClient:
    """对 pro_api 的薄封装。

    - 限速：两次请求之间至少间隔 min_interval 秒；
    - 重试：频率限制等待 30~60 秒，其余错误指数退避；权限/参数错误不重试；
    - 缓存：按 (接口, 参数) 存到 data/cache/<接口>/<hash>.pkl，断点续传；
    - 网关：api_url 可指向自建/代理 Tushare 网关（覆盖 SDK 默认地址）。
    """

    # 备用通道（backup15000_ 前缀 token）可用的接口；其余走主通道
    BACKUP_APIS = frozenset({"dividend", "fina_indicator", "cashflow",
                             "stock_st", "trade_cal", "index_classify"})

    def __init__(self, token=None, cache_dir=CACHE_DIR, min_interval=0.35,
                 use_cache=True, refresh=False, retries=5, api_url=None,
                 backup_token=None):
        if ts is None:
            raise RuntimeError("未安装 tushare，请先执行：pip install tushare")
        try:
            if token:
                self.pro = ts.pro_api(token)
            else:
                self.pro = ts.pro_api()  # 读取已通过 ts.set_token 保存的 token
        except Exception as e:  # noqa: BLE001 - tushare 用普通 Exception 抛错
            raise RuntimeError(f"Tushare 初始化失败：{e}") from e
        if api_url:
            # 自建网关：覆盖 tushare SDK 的默认 http://api.tushare.pro
            setattr(self.pro, "_DataApi__http_url", str(api_url))
        self.api_url = api_url

        # 备用通道：backup15000_ 前缀 token，走专用通道，速度更快（按接口白名单使用）
        self.pro_backup = None
        self._backup_disabled = set()
        if backup_token:
            try:
                self.pro_backup = ts.pro_api(backup_token)
                if api_url:
                    setattr(self.pro_backup, "_DataApi__http_url", str(api_url))
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ 备用 token 初始化失败，将只使用主通道：{str(e)[:80]}")
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
            # 稳定 key：忽略随运行变化的日期参数，只保留 key 与 fields
            raw = json.dumps({"key": str(cache_key), "fields": params.get("fields")},
                             sort_keys=True, ensure_ascii=False, default=str)
        else:
            # 保持旧版按完整参数哈希，兼容已有缓存文件
            raw = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / api / f"{digest}.pkl"

    @staticmethod
    def _fresh(path, ttl_days):
        """缓存是否在有效期内；ttl_days=None 表示永久有效。"""
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

    # ---------- 通道选择 ----------
    def _use_backup(self, api, params):
        if self.pro_backup is None or api in self._backup_disabled:
            return False
        if api in self.BACKUP_APIS:
            return True
        # 单只股票的日线/每日指标走备用通道；整市场批量查询必须走主通道（避免行数截断）
        if api in ("daily", "daily_basic") and "ts_code" in params:
            return True
        return False

    def _invoke(self, api, params):
        if self._use_backup(api, params):
            try:
                return getattr(self.pro_backup, api)(**params)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "api_scope_denied" in msg or "api_not_enabled" in msg:
                    self._backup_disabled.add(api)
                    print(f"  ℹ 备用通道不支持 {api}，本次改用主通道。")
                else:
                    raise
        return getattr(self.pro, api)(**params)

    # ---------- 调用 ----------
    def call(self, api, cache_key=None, ttl_days=None, **params):
        """调用 Tushare 接口。

        - cache_key：稳定缓存 key，忽略日期等易变参数（用于财务/分红/PB 等历史数据）；
        - ttl_days：缓存有效期（天），过期的历史数据会自动刷新；
        - 不传 ttl_days 时缓存永久有效（适合按交易日/年度切分的不可变数据）。
        """
        params = {k: v for k, v in params.items() if v is not None}
        cache_path = self._cache_path(api, params, cache_key)

        if self.use_cache and not self.refresh:
            cached = self._read_cache(cache_path)
            if cached is not None and self._fresh(cache_path, ttl_days):
                self.n_cache_hits += 1
                return cached
            # 兼容旧版按完整参数存储的缓存：同一份数据直接迁移到稳定 key
            if cache_key:
                legacy_path = self._cache_path(api, params)
                if legacy_path != cache_path:
                    legacy = self._read_cache(legacy_path)
                    if legacy is not None and self._fresh(legacy_path, ttl_days):
                        self._write_cache(cache_path, legacy)
                        # 保留旧文件的 mtime，避免旧数据被误判为"刚刷新"
                        try:
                            st = legacy_path.stat()
                            os.utime(cache_path, (st.st_atime, st.st_mtime))
                        except OSError:
                            pass
                        self.n_cache_hits += 1
                        return legacy

        last_err = None
        df = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                df = self._invoke(api, params)
                self.n_calls += 1
                break
            except Exception as e:  # noqa: BLE001 - tushare 用普通 Exception 报错
                last_err = e
                msg = str(e)
                if "token" in msg.lower() and ("不对" in msg or "无效" in msg or "invalid" in msg.lower()):
                    raise RuntimeError(f"Tushare token 无效：{msg}") from e
                if any(k in msg for k in ("权限", "date_range_too_large", "invalid_params", "unknown_api")):
                    raise RuntimeError(f"接口 {api} 调用被拒绝：{msg}") from e
                if "rate_limited" in msg or "额度已达到限制" in msg:
                    wait = 35
                elif "upstream_pool_exhausted" in msg or "当前不可用" in msg:
                    wait = 31
                elif "每分钟" in msg or "频率" in msg or "最多访问" in msg:
                    wait = 60
                else:
                    wait = 2 ** attempt
                if attempt == self.retries - 1:
                    raise RuntimeError(f"Tushare 接口 {api} 调用失败（参数 {params}）：{msg}") from e
                print(f"  ⚠ {api} 调用失败（{msg[:80]}），{wait}s 后重试 ...")
                time.sleep(wait)

        if df is None:
            if last_err is not None:
                raise RuntimeError(f"Tushare 接口 {api} 调用失败：{last_err}")
            df = pd.DataFrame()

        self._write_cache(cache_path, df)
        return df


def _empty(df):
    return df is None or not isinstance(df, pd.DataFrame) or df.empty


# ============================================================
# 小工具
# ============================================================
def ymd(value) -> str:
    """任意日期 -> 'YYYYMMDD'。"""
    return pd.Timestamp(value).strftime("%Y%m%d")


def iso(value) -> str:
    """任意日期 -> 'YYYY-MM-DD'。"""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def check_and_report_empty(prev_df, cur_df, step_name, extra_cols=None):
    if len(cur_df) == 0 and len(prev_df) > 0:
        cols = ["code"]
        if extra_cols:
            cols += [c for c in extra_cols if c not in cols]
        cols = [c for c in cols if c in prev_df.columns]
        print(f"  ⚠ 【{step_name}】筛选后为 0 只，以下是上一步骤剩余的 {len(prev_df)} 只个股信息（前20行）：")
        print(prev_df[cols].head(20).to_string(index=False))
        print()


def progress(i, total, label, every=50):
    if total and ((i + 1) % every == 0 or i + 1 == total):
        print(f"  {label}: {i + 1}/{total}")


# ============================================================
# 交易日历 / 股票池 / 行业 / ST
# ============================================================
def get_trade_days(client, start_date, end_date) -> list[pd.Timestamp]:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    # 一次拉到 end 所在自然年年末，年内 key 稳定，翻年才需要重新拉
    fetch_end = max(end, pd.Timestamp(f"{end.year}-12-31"))
    df = client.call(
        "trade_cal",
        exchange="SSE",
        start_date=ymd(start),
        end_date=ymd(fetch_end),
        is_open="1",
        ttl_days=30,
    )
    if _empty(df):
        raise RuntimeError("trade_cal 返回为空，请检查 token/网络。")
    days = pd.to_datetime(df["cal_date"].astype(str), format="%Y%m%d", errors="coerce")
    return sorted(d for d in days.dropna().unique().tolist() if start <= d <= end)


def previous_trading_date(client, date_str):
    target = pd.Timestamp(date_str)
    days = [d for d in get_trade_days(client, f"{target.year}-01-01", f"{target.year}-12-31")
            if d < target]
    if not days:
        days = [d for d in get_trade_days(client, f"{target.year - 1}-01-01",
                                          f"{target.year - 1}-12-31") if d < target]
    if not days:
        raise SystemExit(f"{date_str} 之前没有交易日，请检查日期。")
    return days[-1]


def year_end_trade_days(trade_days, start_year, end_year):
    """每个年度 <= 12-31 的最后一个交易日。"""
    result = {}
    for y in range(start_year, end_year + 1):
        cutoff = pd.Timestamp(f"{y}-12-31")
        sub = [d for d in trade_days if d.year == y and d <= cutoff]
        if sub:
            result[y] = sub[-1]
    return result


def load_universe(client, date_str):
    """返回基础股票池 DataFrame: code / name / listed_years / is_st。"""
    df = client.call(
        "stock_basic",
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,market,list_date,delist_date",
        ttl_days=1,
    )
    if _empty(df):
        raise RuntimeError("stock_basic 返回为空。")
    df = df.copy()
    df["ts_code"] = df["ts_code"].astype(str)
    df["list_date"] = pd.to_datetime(df["list_date"].astype(str), format="%Y%m%d", errors="coerce")
    target = pd.Timestamp(date_str)
    df = df[df["list_date"].notna() & (df["list_date"] <= target)].copy()
    df["listed_years"] = ((target - df["list_date"]).dt.days / 365.0).clip(lower=0)

    st_codes = set()
    st_attempts = [
        ("stock_st", dict(trade_date=ymd(date_str),
                          fields="ts_code,name,trade_date,type,type_name")),
        ("stock_st", dict(trade_date=ymd(date_str))),
        ("st_stock_list", dict(trade_date=ymd(date_str))),
    ]
    for api, kw in st_attempts:
        try:
            st_df = client.call(api, **kw)
        except Exception as e:  # noqa: BLE001
            print(f"  {api} 不可用（{str(e)[:80]}），稍后回退到股票名称判断 ST。")
            continue
        if not _empty(st_df) and "ts_code" in st_df.columns:
            st_codes = set(st_df["ts_code"].astype(str))
            print(f"  {api} 返回 {len(st_codes)} 只 ST/风险警示股票。")
            break

    if st_codes:
        df["is_st"] = df["ts_code"].isin(st_codes)
    else:
        df["is_st"] = df["name"].astype(str).str.contains("ST", regex=False)

    df["code"] = df["ts_code"].str.split(".").str[0]
    return df[["ts_code", "code", "name", "listed_years", "is_st"]].reset_index(drop=True)


def load_industry_catalog(client):
    """申万一级行业目录（SW2021），返回 DataFrame[index_code, industry_name, level]。"""
    cls = client.call("index_classify", level="L1", src="SW2021",
                      fields="index_code,industry_name,level", ttl_days=30)
    if _empty(cls):
        raise RuntimeError("index_classify 返回为空")
    cls = cls.copy()
    cls["industry_name"] = (cls["industry_name"].astype(str).str.strip()
                            .str.rstrip("I").str.rstrip("Ⅰ"))
    return cls


def load_industry_members(client, cls, wanted_names):
    """只为需要的行业拉成分股，返回 {ts_code: 行业名}。"""
    mapping = {}
    fields = "index_code,con_code,in_date,out_date,is_new"
    for _, row in cls.iterrows():
        name = str(row["industry_name"]).strip()
        if wanted_names and name not in wanted_names:
            continue
        code = str(row["index_code"])
        mem = None
        for kwargs in ({"index_code": code, "is_new": "Y", "fields": fields},
                       {"index_code": code, "fields": fields}):
            try:
                mem = client.call("index_member", ttl_days=30, **kwargs)
                break
            except Exception:  # noqa: BLE001
                continue
        if _empty(mem):
            continue
        col = "con_code" if "con_code" in mem.columns else (
            "ts_code" if "ts_code" in mem.columns else None)
        if col is None:
            continue
        if "is_new" in mem.columns:
            mem = mem[mem["is_new"].astype(str).isin(["Y", "1", "True"])]
        for c in mem[col].astype(str).unique():
            mapping[c] = name
        print(f"    {name}({code}): {mem[col].nunique()} 只")
    return mapping


def load_industry_from_stock_basic(client):
    """兜底：stock_basic.industry（Tushare 自有分类，粒度较粗）。"""
    mapping = {}
    try:
        basic = client.call(
            "stock_basic", exchange="", list_status="L",
            fields="ts_code,industry",
        )
        if not _empty(basic) and "industry" in basic.columns:
            mapping = {
                str(r["ts_code"]): str(r["industry"]).strip()
                for _, r in basic.iterrows()
                if pd.notna(r.get("industry"))
            }
            print(f"  已回退到 stock_basic.industry，共 {len(mapping)} 只。")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 行业兜底数据也失败：{str(e)[:100]}")
    return mapping


def resolve_whitelist(available_names):
    """把白名单名称解析为当前分类下真实存在的名称（支持别名）。"""
    chosen = set()
    for name in CASH_COW_INDUSTRIES:
        if name in available_names:
            chosen.add(name)
            continue
        for alt in INDUSTRY_ALIASES.get(name, []):
            if alt in available_names:
                print(f"  行业「{name}」不在当前分类中，已用「{alt}」替代。")
                chosen.add(alt)
                break
        else:
            print(f"  ⚠ 白名单行业「{name}」未找到，已忽略。")
    return chosen


# ============================================================
# 行情 / 估值 / 分红 / 财务
# ============================================================
def fetch_daily_by_dates(client, dates, codes):
    """按交易日批量取日线（不复权），只保留给定代码，返回长表。"""
    frames = []
    for d in dates:
        ttl = 0.5 if pd.Timestamp(d).normalize() >= pd.Timestamp.today().normalize() else None
        df = client.call("daily", trade_date=ymd(d), ttl_days=ttl,
                         fields="ts_code,trade_date,open,high,low,close,vol")
        if _empty(df):
            continue
        df = df[df["ts_code"].astype(str).isin(codes)].copy()
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_daily_basic_by_dates(client, dates, fields="ts_code,trade_date,close,pb,pe_ttm,total_share"):
    """按交易日批量取每日指标，返回长表。"""
    frames = []
    for d in dates:
        ttl = 0.5 if pd.Timestamp(d).normalize() >= pd.Timestamp.today().normalize() else None
        df = client.call("daily_basic", trade_date=ymd(d), ttl_days=ttl, fields=fields)
        if _empty(df):
            continue
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def build_shares_history(db_year_end):
    """{ts_code: {year: 年末总股本(万股)}}"""
    result = {}
    if _empty(db_year_end):
        return result
    for _, r in db_year_end.iterrows():
        v = r.get("total_share")
        if pd.notna(v) and float(v) > 0:
            result.setdefault(str(r["ts_code"]), {})[int(r["trade_date"].year)] = float(v)
    return result


def fetch_dividend_hist(client, code, start_year, end_year, asof_date):
    """{year: 每股税前现金分红(元)}，只含已实施且除权除息日 <= asof_date。"""
    df = client.call("dividend", cache_key=f"div:{code}", ttl_days=7, ts_code=code)
    if _empty(df):
        return {}
    df = df.copy()
    df["div_proc"] = df["div_proc"].astype(str).str.strip()
    df = df[df["div_proc"] == "实施"]
    df["ex_dt"] = pd.to_datetime(df["ex_date"].astype(str), format="%Y%m%d", errors="coerce")
    df = df[df["ex_dt"].notna() & (df["ex_dt"] <= pd.Timestamp(asof_date))]
    df["year"] = pd.to_datetime(df["end_date"].astype(str), format="%Y%m%d", errors="coerce").dt.year
    df = df[df["year"].between(start_year, end_year)]
    if df.empty:
        return {}
    if "cash_div_tax" in df.columns:
        cash = pd.to_numeric(df["cash_div_tax"], errors="coerce")
    else:
        cash = pd.Series(np.nan, index=df.index)
    if "cash_div" in df.columns:
        cash = cash.fillna(pd.to_numeric(df["cash_div"], errors="coerce"))
    df = df.assign(dps=cash.fillna(0.0))
    return {
        int(y): float(v)
        for y, v in df.groupby("year")["dps"].sum().items()
        if v is not None and v > 0
    }


def fetch_financials(client, code, start_year, asof_date):
    """返回 (年度扣非净利润, 年度经营现金流, 最新资产负债率%)。"""
    start = f"{start_year}0101"
    end = ymd(asof_date)
    asof = pd.Timestamp(asof_date)

    profit_hist, ocf_hist, debt_ratio = {}, {}, None

    try:
        fd = client.call(
            "fina_indicator", cache_key=f"fina:{code}:{start_year}", ttl_days=7,
            ts_code=code, start_date=start, end_date=end,
            fields="ts_code,ann_date,end_date,profit_dedt,debt_to_assets",
        )
    except Exception as e:  # noqa: BLE001
        print(f"  {code} fina_indicator 失败：{str(e)[:80]}")
        fd = pd.DataFrame()

    if not _empty(fd):
        fd = fd.copy()
        fd["end_dt"] = pd.to_datetime(fd["end_date"].astype(str), format="%Y%m%d", errors="coerce")
        fd["ann_dt"] = pd.to_datetime(fd["ann_date"].astype(str), format="%Y%m%d", errors="coerce")
        fd = fd[fd["end_dt"].notna() & (fd["end_dt"] <= asof)]
        # 只保留已公告（或公告日缺失）的记录，同一报告期取最新公告版本
        fd = fd[fd["ann_dt"].isna() | (fd["ann_dt"] <= asof)]
        fd["_ann_sort"] = fd["ann_dt"].fillna(pd.Timestamp("1900-01-01"))
        fd = fd.sort_values(["end_dt", "_ann_sort"]).drop_duplicates("end_dt", keep="last")

        annual = fd[fd["end_dt"].dt.strftime("%m%d") == "1231"]
        for _, r in annual.iterrows():
            y = int(r["end_dt"].year)
            if start_year <= y <= asof.year:
                v = r.get("profit_dedt")
                if pd.notna(v):
                    profit_hist[y] = float(v)

        # 最新一期资产负债率（任意报告期）
        fd_any = fd.dropna(subset=["debt_to_assets"])
        if not fd_any.empty:
            debt_ratio = float(fd_any.iloc[-1]["debt_to_assets"])

    # 现金流量表单次查询区间有限（实测最多 1 年），按自然年分段拉取后合并
    cf_frames = []
    for y in range(start_year, asof.year):
        try:
            # 最近一年的年报可能尚未披露：30 天后重试；更早年度基本不变，永久缓存
            ttl = 30 if y >= asof.year - 1 else None
            dfy = client.call(
                "cashflow", ts_code=code, start_date=f"{y}0101", end_date=f"{y}1231",
                fields="ts_code,ann_date,end_date,n_cashflow_act", ttl_days=ttl,
            )
            if not _empty(dfy):
                cf_frames.append(dfy)
        except Exception as e:  # noqa: BLE001
            print(f"  {code} cashflow {y} 失败：{str(e)[:60]}")
    cf = pd.concat(cf_frames, ignore_index=True) if cf_frames else pd.DataFrame()

    if not _empty(cf):
        cf = cf.copy()
        cf["end_dt"] = pd.to_datetime(cf["end_date"].astype(str), format="%Y%m%d", errors="coerce")
        cf["ann_dt"] = pd.to_datetime(cf["ann_date"].astype(str), format="%Y%m%d", errors="coerce")
        cf = cf[cf["end_dt"].notna() & (cf["end_dt"] <= asof)]
        cf = cf[cf["ann_dt"].isna() | (cf["ann_dt"] <= asof)]
        cf["_ann_sort"] = cf["ann_dt"].fillna(pd.Timestamp("1900-01-01"))
        cf = cf.sort_values(["end_dt", "_ann_sort"]).drop_duplicates("end_dt", keep="last")
        cf_annual = cf[cf["end_dt"].dt.strftime("%m%d") == "1231"]
        for _, r in cf_annual.iterrows():
            y = int(r["end_dt"].year)
            if start_year <= y <= asof.year:
                v = r.get("n_cashflow_act")
                if pd.notna(v):
                    ocf_hist[y] = float(v)

    return profit_hist, ocf_hist, debt_ratio


# ============================================================
# 六道关 / 锚定价 / PB 百分位 / 次日最低价
# ============================================================
def calc_six_gates(stock, price, div_hist, profit_hist, ocf_hist,
                   shares_hist, year_end_prices, debug=False):
    try:
        div_years_count = sum(1 for y, v in div_hist.items() if v > 0)
        if debug:
            print(f"   {stock} 分红年数: {div_years_count}, 历史分红: {div_hist}")
        if MIN_CONTINUOUS_DIV_YEARS > 0 and div_years_count < MIN_CONTINUOUS_DIV_YEARS:
            if debug:
                print(f"   ✗ {stock} 分红年数不足: {div_years_count} < {MIN_CONTINUOUS_DIV_YEARS}")
            return None

        if debug:
            print(f"   {stock} 扣非利润数据: {profit_hist}")
        if len(profit_hist) < MIN_POSITIVE_PROFIT_YEARS:
            if debug:
                print(f"   ✗ {stock} 扣非利润数据不足: {len(profit_hist)} < {MIN_POSITIVE_PROFIT_YEARS}")
            return None
        positive_years = sum(1 for v in profit_hist.values() if v > 0)
        if positive_years < MIN_POSITIVE_PROFIT_YEARS:
            if debug:
                print(f"   ✗ {stock} 扣非利润正数年不足: {positive_years} < {MIN_POSITIVE_PROFIT_YEARS}")
            return None

        total_ocf = sum(float(v) for v in ocf_hist.values()) if ocf_hist else 0.0
        total_dividend_amount = 0.0
        for y, dps in div_hist.items():
            shares = shares_hist.get(y)
            if shares is not None and shares > 0:
                # 每股分红(元) × 总股本(万股) × 10000 = 元
                total_dividend_amount += float(dps) * float(shares) * 10000.0
        if debug:
            print(f"   {stock} 总经营现金流: {total_ocf:,.0f}, 总分红金额: {total_dividend_amount:,.0f}")
        if total_dividend_amount > 0:
            cash_cover_ratio = total_ocf / total_dividend_amount
            if cash_cover_ratio < MIN_CASH_COVER_RATIO:
                if debug:
                    print(f"   ✗ {stock} 现金流覆盖不足: {cash_cover_ratio:.2f} < {MIN_CASH_COVER_RATIO}")
                return None
        else:
            cash_cover_ratio = 0.0

        # 历史真实股息率均值（v9 的核心改动）
        hist_yields = []
        for y, dps in div_hist.items():
            p = year_end_prices.get(y)
            if p is not None and p > 0 and dps > 0:
                hist_yields.append(dps / p * 100)
        historical_avg_yield = float(np.mean(hist_yields)) if hist_yields else None

        # 当前股息率：近三年平均每股分红 / 现价
        recent_years = sorted(div_hist.keys())[-3:]
        if recent_years and price and price > 0:
            avg_dps = float(np.mean([div_hist[y] for y in recent_years]))
            avg_div_yield = avg_dps / price * 100
        else:
            avg_div_yield = None

        if debug:
            if avg_div_yield:
                print(f"   ✓ {stock} 通过六道关，当前股息率: {avg_div_yield:.2f}%, "
                      f"历史平均股息率: {historical_avg_yield:.2f}%")
            else:
                print(f"   ✓ {stock} 通过六道关（无股息率）")

        return {
            "code": stock,
            "div_years_count": div_years_count,
            "positive_profit_years": positive_years,
            "cash_cover_ratio": round(cash_cover_ratio, 2) if cash_cover_ratio else 0.0,
            "div_hist": div_hist,
            "avg_div_yield_hist": round(avg_div_yield, 2) if avg_div_yield else None,
            "historical_avg_yield": round(historical_avg_yield, 2) if historical_avg_yield else None,
        }
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ {stock} 六道关计算异常: {e}")
        return None


def calc_anchor_prices(current_price, div_hist, historical_avg_yield,
                       risk_free_rate=RISK_FREE_RATE):
    if not div_hist:
        return None, None, None
    recent_years = sorted(div_hist.keys())[-3:]
    if not recent_years:
        return None, None, None
    est_dps = float(np.mean([div_hist[y] for y in recent_years]))
    if est_dps <= 0:
        return None, None, None

    target_yield_min = risk_free_rate * ANCHOR_MULTIPLE_MIN / 100
    p_min = round(est_dps / target_yield_min, 2) if target_yield_min > 0 else None

    if historical_avg_yield and historical_avg_yield > 0:
        target_yield_avg = historical_avg_yield / 100
        p_avg = round(est_dps / target_yield_avg, 2) if target_yield_avg > 0 else None
    else:
        p_avg = None
    return p_min, p_avg, round(est_dps, 4)


def calc_pb_percentile(client, code, date_str, current_pb=None, years=PB_HISTORY_YEARS):
    """返回 (当前PB, 5年历史百分位)。

    历史序列用稳定 key 缓存（7 天刷新），当日 PB 直接用本次批量行情里的值追加，
    避免每天重拉 5 年序列。
    """
    start = (pd.Timestamp(date_str) - pd.DateOffset(years=years)).strftime("%Y%m%d")
    try:
        df = client.call(
            "daily_basic", cache_key=f"pb:{code}", ttl_days=7,
            ts_code=code, start_date=start, end_date=ymd(date_str),
            fields="ts_code,trade_date,pb",
        )
    except Exception as e:  # noqa: BLE001
        print(f"  {code} PB 历史读取失败：{str(e)[:80]}")
        return None, None
    if _empty(df):
        return None, None
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    pb = pd.to_numeric(df.sort_values("trade_date")["pb"], errors="coerce").dropna()
    values = [float(v) for v in pb.tolist()]
    if current_pb is None or pd.isna(current_pb):
        cur = values[-1] if values else None
    else:
        cur = float(current_pb)
    if cur is None:
        return None, None
    if not values or abs(values[-1] - cur) > 1e-9:
        values.append(cur)
    percentile = float((np.asarray(values) < cur).mean() * 100)
    return round(cur, 2), round(percentile, 1)


def is_volume_surge(volume, avg_volume):
    return volume > avg_volume * MIN_VOLUME_MULTIPLIER


def predict_next_day_low(client, code, prev_trading_date, state):
    """用不复权日线 + 1 分钟线预测次日最低价 lx。数据/权限不足返回 None。"""
    try:
        d_str = iso(prev_trading_date)
        start = (pd.Timestamp(d_str) - pd.Timedelta(days=60)).strftime("%Y%m%d")

        day = client.call(
            "daily", ts_code=code, start_date=start, end_date=ymd(d_str),
            fields="ts_code,trade_date,open,high,low,close,vol",
        )
        if _empty(day):
            return None
        day = day.copy()
        day["trade_date"] = pd.to_datetime(day["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
        day = day.sort_values("trade_date").reset_index(drop=True)
        if len(day) < 2 or day.iloc[-1]["trade_date"] != pd.Timestamp(d_str):
            return None

        p_close = float(day.iloc[-2]["close"])
        c = day.iloc[-1]
        open_val, close_val = float(c["open"]), float(c["close"])
        if open_val == 0 or float(c["low"]) == 0 or float(c["high"]) == 0 or p_close == 0:
            return None

        if not state.get("minute_ok", True):
            return None
        try:
            minute = client.call(
                "stk_mins", ts_code=code, freq="1min",
                start_date=f"{d_str} 09:00:00", end_date=f"{d_str} 15:00:00",
                fields="ts_code,trade_time,open,high,low,close,vol",
            )
        except Exception as e:  # noqa: BLE001
            if not state.get("minute_warned"):
                print(f"  ⚠ 分钟线接口不可用（可能是未开通“A股历史分钟”权限）：{str(e)[:100]}")
                print("    本次运行将跳过 lx 次日最低价预测，其余结果不受影响。")
                state["minute_warned"] = True
            state["minute_ok"] = False
            return None

        if _empty(minute) or "high" not in minute.columns:
            return None
        minute = minute.copy().sort_values("trade_time").reset_index(drop=True)
        if len(minute) > 240:
            minute = minute.tail(240).reset_index(drop=True)
        if len(minute) < 240:
            return None

        # 与原版一致：取“最高价最后一次出现”的位置
        max_high = float(minute["high"].max())
        high_pos = int(minute.index[minute["high"] == max_high][-1])
        after_high = minute.iloc[high_pos:].reset_index(drop=True)

        # 日线 vol 单位是“手”，分钟线 vol 单位是“股”，统一折算成“股”
        vol5 = day["vol"].tail(VOLUME_LOOKBACK_DAYS)
        if len(vol5) >= VOLUME_LOOKBACK_DAYS:
            avg_volume_5d = float(vol5.mean()) * 100.0
        else:
            avg_volume_5d = float(minute["vol"].mean())
        valid_lows = [
            float(r["low"]) for _, r in after_high.iterrows()
            if is_volume_surge(float(r["vol"]), avg_volume_5d)
        ]
        low_after_high = min(valid_lows) if valid_lows else float(after_high["low"].min())

        lc = (float(c["low"]) / open_val) * close_val
        ld = (float(c["low"]) / p_close) * close_val
        lx = (low_after_high / float(c["high"])) * close_val
        if min(lc, ld, lx) <= 0:
            return None

        ma20 = float(day["close"].tail(20).mean()) if len(day) >= 20 else close_val
        signal_strength = "weak" if lx < ma20 * MA20_DEVIATION_THRESHOLD else "normal"

        return {
            "code": code,
            "lx": round(lx, 2),
            "next_P_min": round(min(lc, ld, lx), 2),
            "next_P_avg": round((lc + ld + lx) / 3, 2),
            "ma20": round(ma20, 2),
            "signal_strength": signal_strength,
        }
    except Exception as e:  # noqa: BLE001
        print(f"  股票{code}次日最低价预测异常：{e}")
        return None


# ============================================================
# 主流程
# ============================================================
@dataclass
class ScreenOptions:
    date: str = "2026-08-13"
    out_dir: Path = DATA_DIR
    enable_minute: bool = True
    limit: int = 0  # >0 时只保留行业筛选后的前 N 只（调试用）
    codes: tuple = ()  # 只测试指定 ts_code（调试用）


def run_screen(opts: ScreenOptions, client):
    specified_date = pd.Timestamp(opts.date).strftime("%Y-%m-%d")

    prev_trading_date = previous_trading_date(client, specified_date)
    date_str = iso(prev_trading_date)
    end_year = prev_trading_date.year - 1
    start_year = end_year - LOOKBACK_YEARS + 1
    print(f"目标日 {specified_date}，前一交易日 {date_str}，"
          f"财务/分红窗口 {start_year}~{end_year}")

    # ── 第0步：基础数据 ──
    print("\n[第0步] 基础股票池 / ST / 上市年限 ...")
    basic_df = load_universe(client, date_str)
    print(f"总共有 {len(basic_df)} 只股票参与筛选。")
    print("上市年限统计：\n", basic_df["listed_years"].describe())

    df0 = basic_df[(basic_df["is_st"] == False) &  # noqa: E712
                   (basic_df["listed_years"] >= MIN_LISTED_YEARS)].copy()
    print(f"剔除ST/上市不满{MIN_LISTED_YEARS}年后剩余 {len(df0)} 只。")
    check_and_report_empty(basic_df, df0, "ST/次新股剔除")
    if len(df0) == 0:
        raise SystemExit("无股票通过基础筛选，请检查数据或放宽条件。")

    # ── 第1步：行业筛选 ──
    print("\n[第1步] 申万一级行业筛选 ...")
    industry_map = {}
    cls = None
    try:
        cls = load_industry_catalog(client)
        available_names = set(cls["industry_name"])
        print(f"  申万一级行业共 {len(cls)} 个")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 申万行业目录不可用（{str(e)[:100]}），尝试 stock_basic.industry 兜底。")
        industry_map = load_industry_from_stock_basic(client)
        available_names = set(industry_map.values())

    whitelist = resolve_whitelist(available_names)
    if not whitelist:
        raise SystemExit("白名单行业一个都没匹配上，请检查申万分类是否可用。")
    avoid = set(AVOID_INDUSTRIES) & available_names
    print(f"生效白名单: {sorted(whitelist)}")
    if avoid:
        print(f"规避名单: {sorted(avoid)}")

    if cls is not None:
        print(f"  只拉取白名单行业的成分股（{len(whitelist)} 个行业）...")
        industry_map = load_industry_members(client, cls, whitelist)
        if not industry_map:
            industry_map = load_industry_from_stock_basic(client)

    df0["industry"] = df0["ts_code"].map(industry_map)
    print(f"行业映射成功数量：{df0['industry'].notna().sum()} / {len(df0)}")
    print("行业分布前20：")
    print(df0["industry"].value_counts().head(20))

    df1 = df0[df0["industry"].isin(whitelist) & ~df0["industry"].isin(avoid)].copy()
    print(f"有 {len(df0)} 只股票，符合现金牛行业白名单筛选条件的股票有 {len(df1)} 只。")
    check_and_report_empty(df0, df1, "行业白名单筛选", extra_cols=["industry"])
    if len(df1) == 0:
        raise SystemExit("无股票通过行业筛选，请根据实际行业名称调整白名单。")
    if opts.codes:
        wanted = {c.strip().upper() for c in opts.codes if c.strip()}
        missing = sorted(wanted - set(df1["ts_code"].astype(str)))
        if missing:
            print(f"  ⚠ 以下代码不在当前白名单行业池中，已忽略: {missing}")
        df1 = df1[df1["ts_code"].astype(str).isin(wanted)].copy()
        print(f"（调试模式）只测试指定代码 {len(df1)} 只。")
        if len(df1) == 0:
            raise SystemExit("--codes 指定的股票都不在候选池中。")
    if opts.limit and len(df1) > opts.limit:
        df1 = df1.head(opts.limit).copy()
        print(f"（调试模式）只保留行业筛选后的前 {opts.limit} 只继续后续流程。")

    # ── 第2步：估值和价格 ──
    print("\n[第2步] 估值（PB/PE）与价格 ...")
    val = client.call(
        "daily_basic", trade_date=ymd(date_str),
        fields="ts_code,close,pb,pe_ttm,total_share",
    )
    if _empty(val):
        raise RuntimeError("daily_basic 当日无数据，请检查日期是否为交易日。")
    val = val.copy()
    val["ts_code"] = val["ts_code"].astype(str)
    val = val.rename(columns={"pb": "pb_ratio", "pe_ttm": "pe_ratio"})
    df1 = df1.merge(val[["ts_code", "pb_ratio", "pe_ratio", "close"]], on="ts_code", how="left")
    df1 = df1.rename(columns={"close": "price"})
    df1 = df1[df1["price"].notna() & (df1["price"] > 0)].copy()
    print(f"有 {len(df1)} 只股票具有有效价格。")

    # PB 上限与第6步同条件（AND 关系），提前到分红/财务之前，能省掉大量请求
    before_pb = len(df1)
    df1 = df1[df1["pb_ratio"].notna() & (df1["pb_ratio"] <= MAX_PB)].copy()
    print(f"PB <= {MAX_PB} 预筛：{before_pb} -> {len(df1)} 只")
    if len(df1) == 0:
        raise SystemExit(f"PB <= {MAX_PB} 预筛后无股票。")

    # ── 第3步：批量预取分红数据 ──
    print(f"\n[第3步] 分红数据（逐只 dividend，共 {len(df1)} 只，带本地缓存）...")
    div_cache = {}
    codes = df1["ts_code"].tolist()
    empty_dividend = 0
    for i, code in enumerate(codes):
        dh = fetch_dividend_hist(client, code, start_year, end_year, date_str)
        if not dh:
            empty_dividend += 1
        div_cache[code] = dh
        progress(i, len(codes), "分红预取", every=100)
    print(f"预取完成：{len(div_cache) - empty_dividend} 只有分红记录，"
          f"{empty_dividend} 只无记录。")

    # ── 第4步：六道关 ──
    print("\n[第4步] 六道关 ...")
    trade_days = get_trade_days(client, f"{start_year}-01-01", date_str)
    year_days = year_end_trade_days(trade_days, start_year, end_year)

    # 年末收盘价（不复权）与年末总股本
    daily_px = fetch_daily_by_dates(client, list(year_days.values()) + [date_str], set(codes))
    year_end_prices = {}
    if not daily_px.empty:
        for _, r in daily_px.iterrows():
            code, y, close = str(r["ts_code"]), int(r["trade_date"].year), r.get("close")
            if pd.notna(close):
                year_end_prices.setdefault(code, {})[y] = float(close)

    db_year_end = fetch_daily_basic_by_dates(
        client, list(year_days.values()), fields="ts_code,trade_date,total_share")
    year_end_shares = build_shares_history(db_year_end)

    gate_rows = []
    price_map = dict(zip(df1["ts_code"], df1["price"]))
    candidates = [
        (code, float(price_map[code]))
        for code in codes
        if sum(1 for v in div_cache.get(code, {}).values() if v > 0) >= MIN_CONTINUOUS_DIV_YEARS
    ]
    print(f"分红年数达标的股票 {len(candidates)} 只 ...")
    if not candidates:
        raise SystemExit("分红年数达标股票为 0，请检查分红数据或放宽 MIN_CONTINUOUS_DIV_YEARS。")
    for i, (code, price) in enumerate(candidates):
        profit_hist, ocf_hist, debt_ratio = fetch_financials(
            client, code, start_year, date_str)
        res = calc_six_gates(
            code, price, div_cache.get(code, {}), profit_hist, ocf_hist,
            year_end_shares.get(code, {}), year_end_prices.get(code, {}),
            debug=(i < 10),
        )
        if res is not None:
            res["debt_ratio"] = debt_ratio
            gate_rows.append(res)
        progress(i, len(candidates), "六道关", every=50)

    if not gate_rows:
        print("⚠️ 没有股票通过六道关。")
        print("可能原因：分红数据不足、扣非利润不达标、现金流覆盖过低。")
        print("请检查上述调试输出，或放宽参数（如 MIN_CONTINUOUS_DIV_YEARS=0, MIN_CASH_COVER_RATIO=0.1）")
        raise SystemExit("六道关无通过股票。")

    gate_df = pd.DataFrame(gate_rows).rename(columns={"code": "ts_code"})

    df2 = df1[df1["ts_code"].isin(gate_df["ts_code"].tolist())].copy()
    df2 = df2.merge(gate_df.drop(columns=["div_hist"]), on="ts_code", how="left")

    # 股息率筛选（与原版一致：筛完为 0 则跳过该条件）
    if "avg_div_yield_hist" in df2.columns:
        df2_temp = df2[df2["avg_div_yield_hist"].notna() &
                       (df2["avg_div_yield_hist"] >= MIN_AVG_DIV_YIELD) &
                       (df2["avg_div_yield_hist"] <= MAX_CURRENT_DIV_YIELD)].copy()
        if not df2_temp.empty:
            df2 = df2_temp
            print(f"应用股息率筛选后剩余: {len(df2)} 只。")
        else:
            print("股息率筛选后无股票，跳过股息率条件，使用全部通过六道关的股票。")
    else:
        print("无股息率数据，跳过股息率筛选。")

    # ── 第5步：负债率 ──
    print("\n[第5步] 负债率 ...")
    df3 = df2[df2["debt_ratio"].notna() & (df2["debt_ratio"] <= MAX_DEBT_RATIO)].copy()
    print(f"负债率 <= {MAX_DEBT_RATIO}% 的股票: {len(df3)} 只。")
    check_and_report_empty(df2, df3, "负债率",
                           extra_cols=["industry", "avg_div_yield_hist", "debt_ratio"])
    if len(df3) == 0:
        raise SystemExit("无股票通过负债率筛选。")

    # ── 第6步：PB 和百分位 ──
    print("\n[第6步] PB 与百分位 ...")
    pb_pct = {}
    pb_now = dict(zip(df3["ts_code"], df3["pb_ratio"]))
    for code in df3["ts_code"].tolist():
        pb_pct[code] = calc_pb_percentile(client, code, date_str, current_pb=pb_now.get(code))
    df3["pb_percentile"] = df3["ts_code"].map(
        lambda s: pb_pct.get(s, (None, None))[1])

    df4 = df3[(df3["pb_ratio"] <= MAX_PB) &
              (df3["pb_percentile"].notna()) &
              (df3["pb_percentile"] <= PB_PERCENTILE_MAX)].copy()
    print(f"PB <= {MAX_PB} 且百分位 <= {PB_PERCENTILE_MAX}% 的股票: {len(df4)} 只。")
    check_and_report_empty(df3, df4, "PB筛选",
                           extra_cols=["industry", "debt_ratio", "pb_ratio", "pb_percentile"])
    if len(df4) == 0:
        raise SystemExit("无股票通过PB筛选。")

    # ============================================================
    # 底价预测（使用历史平均股息率）
    # ============================================================
    print("\n最终股票代码：", df4["ts_code"].tolist())

    gate_by_code = {r["code"]: r for r in gate_rows}
    anchor_rows = []
    for _, row in df4.iterrows():
        code = row["ts_code"]
        div_hist = gate_by_code.get(code, {}).get("div_hist")
        if not div_hist:
            continue
        p_min, p_avg, est_dps = calc_anchor_prices(
            float(row["price"]), div_hist, row.get("historical_avg_yield"))
        if p_min is None:
            continue
        anchor_rows.append({
            "ts_code": code,
            "est_annual_dps": est_dps,
            "P_min": p_min,
            "P_avg": p_avg,
            "current_price": float(row["price"]),
            "current_yield_if_buy_now": round(est_dps / float(row["price"]) * 100, 2)
            if row["price"] else None,
        })

    if not anchor_rows:
        print("警告：无底价预测数据（分红数据缺失），只输出筛选结果。")
        final_df = df4.copy()
        final_df["est_annual_dps"] = np.nan
        final_df["P_min"] = np.nan
        final_df["P_avg"] = np.nan
        final_df["current_price"] = final_df["price"]
        final_df["current_yield_if_buy_now"] = np.nan
    else:
        anchor_df = pd.DataFrame(anchor_rows)
        final_df = df4.merge(anchor_df, on="ts_code", how="left")
        final_df = final_df.dropna(subset=["P_min", "P_avg"])

    # ── 次日最低价预测 lx ──
    print("\n>>> 次日最低价预测（lx）...")
    state = {"minute_ok": bool(opts.enable_minute), "minute_warned": False}
    lx_rows = []
    for code in final_df["ts_code"].tolist():
        res = predict_next_day_low(client, code, prev_trading_date, state)
        if res is not None:
            lx_rows.append(res)
            print(f"  {code} lx={res['lx']}  next_P_min={res['next_P_min']}  "
                  f"next_P_avg={res['next_P_avg']}  ma20={res['ma20']}  强弱={res['signal_strength']}")
        else:
            print(f"  {code} 数据不足，未能预测")

    if lx_rows:
        lx_df = pd.DataFrame(lx_rows).rename(columns={"code": "ts_code"})
        final_df = final_df.merge(lx_df, on="ts_code", how="left")
    else:
        for col in ["lx", "next_P_min", "next_P_avg", "ma20", "signal_strength"]:
            final_df[col] = np.nan
        print("警告：所有入选股票均未能预测次日最低价（可能是分钟线数据不足）。")

    # ── 展示 ──
    display_cols = ["code", "industry", "price", "avg_div_yield_hist", "historical_avg_yield",
                    "div_years_count", "cash_cover_ratio", "debt_ratio", "pb_ratio", "pb_percentile"]
    if "est_annual_dps" in final_df.columns:
        display_cols += ["est_annual_dps", "P_avg", "P_min"]
    display_cols += ["lx", "next_P_min", "next_P_avg", "signal_strength"]
    display_cols = [c for c in display_cols if c in final_df.columns]
    print("\n最终结果：")
    print(final_df[display_cols].to_string(index=False))

    if "P_min" in final_df.columns:
        print("\n逐股说明：")
        for _, row in final_df.iterrows():
            if pd.isna(row["P_min"]):
                continue
            if pd.isna(row["P_avg"]):
                print(f"\n  {row['code']} | {row['industry']} | 现价¥{row['price']:.2f} | "
                      f"当前股息率{row['avg_div_yield_hist']:.2f}% | 无历史平均股息率")
                continue
            discount_to_price = (row["price"] - row["P_avg"]) / row["price"] * 100
            print(f"\n  {row['code']} | {row['industry']} | 现价¥{row['price']:.2f} | "
                  f"当前股息率{row['avg_div_yield_hist']:.2f}% | "
                  f"历史平均股息率{row['historical_avg_yield']:.2f}% | "
                  f"连续分红{row['div_years_count']}/{LOOKBACK_YEARS}年 | "
                  f"现金流覆盖{row['cash_cover_ratio']}倍")
            print(f"    → 预估分红¥{row['est_annual_dps']:.4f} | "
                  f"P_avg=¥{row['P_avg']:.2f} | P_min=¥{row['P_min']:.2f}")
            if "lx" in row.index and not pd.isna(row["lx"]):
                print(f"    → 次日最低价预测 lx=¥{row['lx']:.2f} | "
                      f"低吸价 next_P_min=¥{row['next_P_min']:.2f}"
                      f" / next_P_avg=¥{row['next_P_avg']:.2f} | "
                      f"ma20=¥{row['ma20']:.2f} | 强弱={row['signal_strength']}")
            if row["price"] <= row["P_min"]:
                print("    → 深度安全边际，可买入")
            elif row["price"] <= row["P_avg"]:
                print("    → 估值合理，可定投")
            else:
                print(f"    → 溢价{-discount_to_price:.1f}%，等待回调")

    print("\n==================== 统计摘要 ====================")
    print(f"  初筛总数: {len(basic_df)}")
    print(f"  最终入选: {len(final_df)}")
    if "avg_div_yield_hist" in final_df.columns and not final_df["avg_div_yield_hist"].isna().all():
        print(f"  平均当前股息率: {final_df['avg_div_yield_hist'].mean():.2f}%")
    if "historical_avg_yield" in final_df.columns and not final_df["historical_avg_yield"].isna().all():
        print(f"  平均历史股息率: {final_df['historical_avg_yield'].mean():.2f}%")
    if "P_min" in final_df.columns:
        print(f"  跌破P_min: {(final_df['price'] <= final_df['P_min']).sum()} 只")
    print("=================================================")

    # ── 输出格式对齐（与聚宽版 CSV 一致）──
    if "industry" in final_df.columns:
        final_df["industry"] = final_df["industry"].astype(str).str.rstrip("I").str.rstrip("Ⅰ")
    if "is_st" in final_df.columns:
        final_df["is_st"] = final_df["is_st"].astype(float)

    export_cols = [
        "code", "listed_years", "is_st",
        "industry", "pb_ratio", "pe_ratio", "price",
        "avg_div_yield_hist", "cash_cover_ratio",
        "div_years_count", "positive_profit_years",
        "debt_ratio", "pb_percentile",
        "P_avg", "P_min",
        "current_price", "current_yield_if_buy_now", "est_annual_dps",
        "lx", "next_P_min", "next_P_avg", "ma20", "signal_strength",
    ]
    export_cols = [c for c in export_cols if c in final_df.columns]

    file_date = date_str.replace("-", "")
    out_dir = Path(opts.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_filename = out_dir / f"{file_date}_dividend_screen.csv"
    final_df[export_cols].to_csv(out_filename, index=False, encoding="utf-8-sig")
    print(f"\n结果已保存至：{out_filename}（共 {len(final_df)} 只，编码 UTF-8 BOM，Excel可直接打开无乱码）")
    print(f"Tushare 调用统计：实际请求 {client.n_calls} 次，缓存命中 {client.n_cache_hits} 次。")

    return final_df, out_filename


# ============================================================
# 命令行入口
# ============================================================
def config_candidates():
    """配置文件查找顺序：运行时目录 -> 脚本目录 -> 上一级目录。"""
    return (RUNTIME_HOME / "config.json", BASE_DIR / "config.json", BASE_DIR.parent / "config.json")


def resolve_token(arg_token=None):
    if arg_token:
        return arg_token.strip()
    env = os.environ.get("TUSHARE_TOKEN") or os.environ.get("TS_TOKEN")
    if env:
        return env.strip()
    for path in config_candidates():
        if not path.exists():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for key in ("tushare_token", "tushareToken", "ts_token"):
            if cfg.get(key):
                return str(cfg[key]).strip()
    return None


def resolve_backup_token(arg_token=None):
    """备用通道 token（backup15000_ 前缀），用于 dividend/fina/cashflow 等单只查询。"""
    if arg_token:
        return arg_token.strip()
    env = os.environ.get("TUSHARE_TOKEN_BACKUP")
    if env:
        return env.strip()
    for path in config_candidates():
        if not path.exists():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for key in ("tushare_token_backup", "backup_token"):
            if cfg.get(key):
                return str(cfg[key]).strip()
    return None


def resolve_api_url(arg_api_url=None):
    """自定义 Tushare 网关地址（自建/代理），未配置则用 SDK 默认地址。"""
    if arg_api_url:
        return arg_api_url.strip()
    env = os.environ.get("TUSHARE_API_URL") or os.environ.get("TUSHARE_URL")
    if env:
        return env.strip()
    for path in config_candidates():
        if not path.exists():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for key in ("tushare_api_url", "tushare_url", "api_url"):
            if cfg.get(key):
                return str(cfg[key]).strip()
    return None


def check_permissions(client, date_str):
    """逐个探测本策略需要的接口权限，返回 (成功数, 总数)。"""
    d = ymd(date_str)
    samples = [
        ("stock_basic", dict(exchange="", list_status="L", fields="ts_code,name,list_date")),
        ("trade_cal", dict(exchange="SSE", start_date=d, end_date=d, is_open="1")),
        ("daily", dict(trade_date=d, fields="ts_code,trade_date,open,high,low,close,vol")),
        ("daily_basic", dict(trade_date=d, fields="ts_code,close,pb,pe_ttm,total_share")),
        ("dividend", dict(ts_code="600000.SH",
                          fields="ts_code,end_date,ann_date,div_proc,ex_date,cash_div,cash_div_tax")),
        ("fina_indicator", dict(ts_code="600000.SH", start_date="20250101", end_date=d,
                                fields="ts_code,ann_date,end_date,profit_dedt,debt_to_assets")),
        ("cashflow", dict(ts_code="600000.SH", start_date="20250101", end_date="20251231",
                          fields="ts_code,ann_date,end_date,n_cashflow_act")),
        ("index_classify", dict(level="L1", src="SW2021",
                                fields="index_code,industry_name,level")),
        ("index_member", dict(index_code="801780.SI", is_new="Y",
                              fields="index_code,con_code,in_date,out_date,is_new")),
        ("stock_st", dict(trade_date=d, fields="ts_code,name,trade_date,type,type_name")),
        ("stk_mins", dict(ts_code="600000.SH", freq="1min",
                          start_date=f"{date_str} 09:00:00", end_date=f"{date_str} 15:00:00",
                          fields="ts_code,trade_time,open,high,low,close,vol")),
    ]
    ok = 0
    print(f"Tushare 接口权限自检（{date_str}）：")
    for api, kw in samples:
        try:
            df = client.call(api, **kw)
            ok += 1
            print(f"  ✓ {api:18s} rows={len(df)}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {api:18s} {str(e)[:120]}")
    print(f"\n通过 {ok}/{len(samples)} 个接口。")
    if ok < len(samples):
        print("参考：本策略需要 2000 积分档接口（stock_basic/daily/daily_basic/dividend/"
              "fina_indicator/cashflow/index_classify 等）；stk_mins 需单独开通“A股历史分钟”。")
    return ok, len(samples)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="高股息策略 v10（Tushare 版）")
    p.add_argument("--date", default=None,
                   help="目标交易日（默认今天，脚本自动取它的前一交易日作为数据截止日）")
    p.add_argument("--token", default=None, help="Tushare token（也可用环境变量 TUSHARE_TOKEN）")
    p.add_argument("--backup-token", default=None,
                   help="备用通道 token（backup15000_...），用于分红/财务等单只接口加速")
    p.add_argument("--api-url", default=None,
                   help="Tushare 网关地址（自建/代理，如 http://tus.sxdata.top/tu_share/）")
    p.add_argument("--out-dir", default=str(DATA_DIR), help="结果输出目录")
    p.add_argument("--min-interval", type=float, default=0.35,
                   help="两次 Tushare 请求的最小间隔秒数（默认 0.35≈170次/分）")
    p.add_argument("--no-cache", action="store_true", help="完全禁用本地缓存（不读不写）")
    p.add_argument("--refresh", action="store_true", help="忽略已有缓存，重新请求并覆盖")
    p.add_argument("--skip-minute", action="store_true", help="跳过分钟线（lx 预测将为空）")
    p.add_argument("--limit", type=int, default=0,
                   help="调试用：行业筛选后只保留前 N 只，减少接口调用")
    p.add_argument("--codes", default="",
                   help="调试用：只测指定 ts_code，逗号分隔（如 600000.SH,601398.SH）")
    p.add_argument("--check", action="store_true",
                   help="只做 token/接口权限自检，不跑选股")
    return p.parse_args(argv)


def main(argv=None, client=None):
    args = parse_args(argv)
    opts = ScreenOptions(
        date=args.date or pd.Timestamp.today().strftime("%Y-%m-%d"),
        out_dir=Path(args.out_dir),
        enable_minute=not args.skip_minute,
        limit=args.limit,
        codes=tuple(args.codes.split(",")) if args.codes else (),
    )
    try:
        if client is None:
            # resolve_token 找不到时传 None，让 tushare 自己读环境变量/~/.tushare/tk.csv
            try:
                client = TushareClient(
                    token=resolve_token(args.token),
                    backup_token=resolve_backup_token(args.backup_token),
                    api_url=resolve_api_url(args.api_url),
                    min_interval=args.min_interval,
                    use_cache=not args.no_cache,
                    refresh=args.refresh,
                )
            except RuntimeError as e:
                print(f"✗ {e}")
                print("请任选一种方式配置 token（优先级从高到低）：")
                print("  1) 运行时加参数      --token 你的token")
                print("  2) 环境变量          export TUSHARE_TOKEN=你的token（建议写进 ~/.zshrc）")
                print("  3) 本目录配置文件    cp config.example.json config.json 后填入 token")
                print("  4) tushare 全局保存  .venv/bin/python -c \"import tushare as ts; ts.set_token('你的token')\"")
                return 2
        if getattr(client, "api_url", None):
            print(f"Tushare 网关：{client.api_url}")
        if getattr(client, "pro_backup", None) is not None:
            print("备用通道：已启用（分红/财务/ST/行业目录等单只查询走加速通道）")
        if args.check:
            client.refresh = True  # 自检必须走真实请求，避免缓存掩盖 token/权限问题
            ok, total = check_permissions(client, opts.date)
            return 0 if ok == total else 1
        run_screen(opts, client)
    except SystemExit as e:
        print(f"\n[{e}]")
        return 1
    except RuntimeError as e:
        print(f"\n✗ 运行失败：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
