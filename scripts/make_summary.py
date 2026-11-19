#!/usr/bin/env python
# coding: utf-8
"""把选股结果 CSV + 运行日志整理成一份 Markdown summary。

用法：
    .venv/bin/python make_summary.py \
        --csv data/20260812_dividend_screen.csv \
        --log data/run_full_20260812.log \
        --out data/20260812_summary.md
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent


def parse_funnel(log_path: Path) -> dict:
    text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
    pats = {
        "universe": r"总共有 (\d+) 只股票参与筛选",
        "after_st": r"剔除ST/上市不满\d+年后剩余 (\d+) 只",
        "after_industry": r"符合现金牛行业白名单筛选条件的股票有 (\d+) 只",
        "with_price": r"有 (\d+) 只股票具有有效价格",
        "div_ok": r"分红年数达标的股票 (\d+) 只",
        "pb_pre": r"PB <= 1\.5 预筛：(\d+) -> (\d+) 只",
        "gates": r"六道关: (\d+)/(\d+)",
        "after_yield": r"应用股息率筛选后剩余: (\d+) 只",
        "after_debt": r"负债率 <= 70\.0% 的股票: (\d+) 只",
        "after_pb": r"PB <= 1\.5 且百分位 <= 30\.0% 的股票: (\d+) 只",
        "calls": r"实际请求 (\d+) 次，缓存命中 (\d+) 次",
    }
    out = {}
    for key, pat in pats.items():
        m = re.findall(pat, text)
        if not m:
            out[key] = None
        elif key in ("pb_pre", "gates", "calls"):
            out[key] = m[-1]
        else:
            out[key] = m[-1]
    return out


def fmt(v, digits=2, suffix=""):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, str):
        return v
    return f"{v:.{digits}f}{suffix}"


def advice(row) -> str:
    if pd.isna(row.get("P_min")):
        return "—"
    if row["price"] <= row["P_min"]:
        return "深度安全边际"
    if pd.notna(row.get("P_avg")) and row["price"] <= row["P_avg"]:
        return "估值合理"
    return "等待回调"


def build(csv_path: Path, log_path: Path) -> str:
    df = pd.read_csv(csv_path, dtype={"code": str})
    funnel = parse_funnel(log_path)
    lines = []

    lines.append("# 高股息策略 v10 —— Tushare 全量筛选结果\n")
    m = re.match(r"(\d{8})_dividend_screen", csv_path.stem)
    cutoff = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}" if m else "—"
    lines.append(f"- 结果文件：`{csv_path.name}`")
    lines.append(f"- 运行日志：`{log_path.name if log_path.exists() else '—'}`")
    lines.append(f"- 数据口径：不复权价；近 5 年分红/财务；数据截止 {cutoff}\n")

    lines.append("## 筛选漏斗\n")
    lines.append("| 步骤 | 剩余数量 |")
    lines.append("|---|---:|")
    steps = [
        ("初始股票池", funnel.get("universe")),
        ("剔除 ST / 次新", funnel.get("after_st")),
        ("现金牛行业白名单", funnel.get("after_industry")),
        ("有有效价格", funnel.get("with_price")),
        ("PB ≤ 1.5 预筛", funnel.get("pb_pre")[1] if funnel.get("pb_pre") else None),
        ("分红年数 ≥ 2", funnel.get("div_ok")),
        ("六道关通过", funnel.get("gates")[0] if funnel.get("gates") else None),
        ("股息率 4%~8%", funnel.get("after_yield")),
        ("负债率 ≤ 70%", funnel.get("after_debt")),
        ("PB ≤ 1.5 且分位 ≤ 30%", funnel.get("after_pb")),
        ("最终入选", len(df)),
    ]
    for name, val in steps:
        lines.append(f"| {name} | {val if val is not None else '—'} |")
    lines.append("")

    if df.empty:
        lines.append("## 结果\n\n本期没有股票通过全部筛选条件。\n")
        return "\n".join(lines)

    lines.append("## 入选列表\n")
    lines.append("| 代码 | 行业 | 现价 | 股息率% | PB | PB分位% | 负债率% | 现金流覆盖 | 预估DPS | P_avg | P_min | lx | 信号 | 建议 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for _, r in df.sort_values("avg_div_yield_hist", ascending=False).iterrows():
        lines.append(
            f"| {r['code']} | {r['industry']} | {fmt(r.get('price'))} | "
            f"{fmt(r.get('avg_div_yield_hist'))} | {fmt(r.get('pb_ratio'))} | "
            f"{fmt(r.get('pb_percentile'), 1)} | {fmt(r.get('debt_ratio'), 1)} | "
            f"{fmt(r.get('cash_cover_ratio'))} | {fmt(r.get('est_annual_dps'), 4)} | "
            f"{fmt(r.get('P_avg'))} | {fmt(r.get('P_min'))} | {fmt(r.get('lx'))} | "
            f"{r.get('signal_strength', '—')} | {advice(r)} |"
        )
    lines.append("")

    lines.append("## 统计摘要\n")
    lines.append(f"- 入选数量：**{len(df)}** 只")
    lines.append(f"- 平均股息率：{df['avg_div_yield_hist'].mean():.2f}%"
                 f"（中位数 {df['avg_div_yield_hist'].median():.2f}%，"
                 f"区间 {df['avg_div_yield_hist'].min():.2f}%~{df['avg_div_yield_hist'].max():.2f}%）")
    if "historical_avg_yield" in df.columns and df["historical_avg_yield"].notna().any():
        lines.append(f"- 平均历史股息率：{df['historical_avg_yield'].mean():.2f}%")
    lines.append(f"- PB：均值 {df['pb_ratio'].mean():.2f}，中位数 {df['pb_ratio'].median():.2f}；"
                 f"PB 分位均值 {df['pb_percentile'].mean():.1f}%")
    lines.append(f"- 资产负债率：均值 {df['debt_ratio'].mean():.1f}%，"
                 f"最高 {df['debt_ratio'].max():.1f}%")
    if "cash_cover_ratio" in df.columns and df["cash_cover_ratio"].notna().any():
        lines.append(f"- 现金流覆盖：均值 {df['cash_cover_ratio'].mean():.2f} 倍，"
                     f"最低 {df['cash_cover_ratio'].min():.2f} 倍")
    below_min = int((df["price"] <= df["P_min"]).sum()) if "P_min" in df.columns else 0
    below_avg = int((df["price"] <= df["P_avg"]).sum()) if "P_avg" in df.columns else 0
    lines.append(f"- 现价 ≤ P_min（深度安全边际）：**{below_min}** 只；"
                 f"现价 ≤ P_avg（估值合理）：**{below_avg}** 只")
    if "lx" in df.columns and df["lx"].notna().any():
        lines.append(f"- lx 预测：{int(df['lx'].notna().sum())} 只有效，"
                     f"平均 lx {df['lx'].mean():.2f}，"
                     f"相对现价平均折价 {(1 - df['lx'].mean() / df['price'].mean()) * 100:.1f}%")
    else:
        lines.append("- lx 预测：本次无有效数据（分钟线未开通或数据不足）")
    if "signal_strength" in df.columns:
        weak = int((df["signal_strength"] == "weak").sum())
        lines.append(f"- 强弱信号：weak {weak} 只 / normal {len(df) - weak} 只")
    lines.append("")

    lines.append("> 口径说明：P_min 按无风险利率×1.3 反推（更保守），P_avg 按历史平均股息率反推；\n"
                 "> 当历史平均股息率高于 3.51% 时 P_avg 会低于 P_min，属公式本身特性。\n")
    lines.append("## 行业分布\n")
    lines.append("| 行业 | 数量 |")
    lines.append("|---|---:|")
    for name, cnt in df["industry"].value_counts().items():
        lines.append(f"| {name} | {cnt} |")
    lines.append("")

    lines.append("## 运行统计\n")
    if funnel.get("calls"):
        lines.append(f"- Tushare 实际请求 {funnel['calls'][0]} 次，缓存命中 {funnel['calls'][1]} 次")
    lines.append("- 输出列与聚宽版一致（23 列）；价格均不复权；分红金额 = 每股分红 × 年末总股本")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="生成选股结果 Markdown summary")
    ap.add_argument("--csv", default=str(BASE_DIR / "data" / "20260812_dividend_screen.csv"))
    ap.add_argument("--log", default=str(BASE_DIR / "data" / "run_full_20260812.log"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    csv_path = Path(args.csv)
    log_path = Path(args.log)
    out_path = Path(args.out) if args.out else csv_path.with_name(
        csv_path.stem.replace("_dividend_screen", "") + "_summary.md")
    md = build(csv_path, log_path)
    out_path.write_text(md, encoding="utf-8")
    print(f"summary 已写入：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
