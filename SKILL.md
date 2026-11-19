---
name: dividend-screen
description: 当用户想用高股息/红利策略筛选 A 股、跑"高股息策略 v10"、按股息率/PB/负债率/现金流选股、生成高股息选股列表或摘要时使用。包含 Tushare 数据抓取、六道关筛选、P_avg/P_min 锚定价、次日最低价 lx 预测、CSV 导出与 Markdown 汇总。
---

# 高股息选股（dividend-screen）

一句话用法：**"用高股息策略筛选一下 A 股"** → 本 skill 负责自检、跑批、出列表和 summary。

## 前置条件

- 已运行安装脚本（`install.sh`），或手动把本目录放到 `~/.agents/skills/dividend-screen/`。
- Tushare token 已写入 `~/.local/share/dividend-screen/config.json`（安装时交互写入）。
- 首次全量运行约 **1~2 小时**；之后有缓存会快很多。

## 首次/每次先自检

```bash
~/.agents/skills/dividend-screen/scripts/run.sh --check
```

11 个接口全部通过才继续。失败时先看 `references/TROUBLESHOOTING.md`。

## 运行

**默认后台全量（推荐）**：数据截止到"今天的前一交易日"。

```bash
~/.agents/skills/dividend-screen/scripts/run.sh --background
```

- 缓存是增量的：同日期重跑几乎 0 请求，隔日只补新交易日/新分钟线；分红、财务、PB 序列默认 7 天刷新，`--refresh` 强制全量。
- 立即返回日志路径和 PID；
- 用读取日志/等待后台任务的方式跟踪进度（每 100 只分红打印一次，最后输出统计摘要）；
- 完成后生成结果文件：`~/.local/share/dividend-screen/data/YYYYMMDD_dividend_screen.csv`。

**小样本先验证**（约 1~5 分钟）：

```bash
.../scripts/run.sh --limit 30 --skip-minute
.../scripts/run.sh --codes 600019.SH,600028.SH,601006.SH
```

**指定日期**（默认今天，脚本自动取前一交易日）：

```bash
.../scripts/run.sh --background --date 2026-08-13
```

**分钟线**：默认开启（需要网关开通"历史分钟"权限）。没权限时脚本会自动跳过 lx，不影响其余结果；想省时间加 `--skip-minute`。

## 完成后出报告

```bash
~/.agents/skills/dividend-screen/scripts/report.sh
```

生成 `data/YYYYMMDD_summary.md`（筛选漏斗 + 入选列表 + 统计 + 行业分布），同时打印路径。

向用户汇报时至少包含：

1. 入选数量、平均股息率、PB 均值/分位、负债率、现金流覆盖；
2. 入选列表（代码、行业、现价、股息率、PB、PB 分位）；
3. 结果文件路径（CSV + summary.md）；
4. 全量运行耗时/调用次数，以及本期为 0 只时的原因（漏斗卡在哪一步）。

## 字段口径（汇报时别解释错）

- `avg_div_yield_hist`：近三年平均每股分红 / 现价；`historical_avg_yield` 仅用于报告，不在 CSV 里。
- `P_min`：每股分红 / (无风险利率 × 1.3)，更保守；`P_avg`：每股分红 / 历史平均股息率。
  历史平均股息率高于 3.51% 时 `P_avg < P_min`，属公式特性，不是数据错误。
- `lx`：次日最低价预测；`next_P_min/next_P_avg` 是低吸价。
- `is_st` 0/1；`code` 为 6 位数字；价格为不复权价。

## 配置与安全

- token/网关/备用 token 只在 `~/.local/share/dividend-screen/config.json`（600 权限）。
- **不要把 token 打印到对话、日志或提交到仓库。**
- 用户没配 token 时，让用户运行安装脚本或手动编辑该 config.json，不要向用户索要 token 原文。
- 全量跑批放后台；不要在对话里同步等待一小时。

## 更多

- 运行/汇报细节：`references/WORKFLOW.md`
- 报错处理：`references/TROUBLESHOOTING.md`
- 策略口径与接口限制：`references/STRATEGY.md`
