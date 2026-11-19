---
name: dividend-screen
description: 当用户想用高股息/红利策略筛选 A 股、跑"高股息策略 v10"、按股息率/PB/负债率/现金流选股、生成高股息选股列表或摘要；或者想做股票底部/买点预测、算支撑压力位、判断某只股票是否接近底部时使用。包含 Tushare 数据抓取、六道关筛选、P_avg/P_min 锚定价、底部预测（日线+1分钟线，输出 hc/hd/hx/ha、lc/ld/lx/la）、CSV 导出与 Markdown 汇总。
---

# 高股息选股 + 股票底部预测（dividend-screen）

本 skill 有两个功能，共用同一套 Tushare token / 网关 / 缓存目录：

| 功能 | 一句话 | 入口 |
|---|---|---|
| 功能一：高股息选股 | "用高股息策略筛选一下 A 股" | `scripts/run.sh` + `scripts/report.sh` |
| 功能二：股票底部预测 | "算一下这些股票的买点/支撑位" | `scripts/bottom.sh` |

功能一（高股息选股）：本 skill 负责自检、跑批、出列表和 summary。

## 前置条件

- 已运行安装脚本（`install.sh`），或手动把本目录放到 `~/.agents/skills/dividend-screen/`。
- Tushare token 已写入 `~/.local/share/dividend-screen/config.json`（安装时交互写入）。
- 首次全量运行约 **1~2 小时**；之后有缓存会快很多。

## 功能一：首次/每次先自检

```bash
~/.agents/skills/dividend-screen/scripts/run.sh --check
```

11 个接口全部通过才继续。失败时先看 `references/TROUBLESHOOTING.md`。

## 功能一：运行

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

## 功能一：完成后出报告

```bash
~/.agents/skills/dividend-screen/scripts/report.sh
```

生成 `data/YYYYMMDD_summary.md`（筛选漏斗 + 入选列表 + 统计 + 行业分布），同时打印路径。

向用户汇报时至少包含：

1. 入选数量、平均股息率、PB 均值/分位、负债率、现金流覆盖；
2. 入选列表（代码、行业、现价、股息率、PB、PB 分位）；
3. 结果文件路径（CSV + summary.md）；
4. 全量运行耗时/调用次数，以及本期为 0 只时的原因（漏斗卡在哪一步）。

## 功能一：字段口径（汇报时别解释错）

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

## 功能二：股票底部预测

对股票池里的每一只，用**目标交易日的日线 + 当天 1 分钟线**算出支撑/压力价位与买点信号。

### 算什么

| 列 | 含义 |
|---|---|
| `hc` / `hd` / `hx` | 由当日最高价推演的三个压力位 |
| `ha` | 三者均值（压力中枢） |
| `lc` / `ld` / `lx` | 由当日最低价推演的三个支撑位 |
| `la` | 三者均值（支撑中枢） |
| `low_after_high` / `high_after_low` | 最高点之后的最低点 / 最低点之后的最高点 |
| `no` / `ma20` / `signal_strength` | 平盘价 / 20 日均线 / 弱势信号（`lx < ma20×0.85`） |

口径：不复权价；`c.high`/`c.low` 取**日线**，`high`/`low` 等极值取**分钟线**；
最高/最低点取「最后一次出现」。**`signal_strength=weak` 只是风险提示，不是买卖建议。**

### 运行

```bash
# 全量（股票池约 666 只；稳定态每天约 25 次 API 调用）
~/.agents/skills/dividend-screen/scripts/bottom.sh

# 后台跑，立即返回日志路径
~/.agents/skills/dividend-screen/scripts/bottom.sh --background

# 小样本先验证
~/.agents/skills/dividend-screen/scripts/bottom.sh --codes 600795,002060 -v
```

输出：`~/.local/share/dividend-screen/data/YYYYMMDD_forecast_price.csv`（UTF-8 BOM）。

### 缓存（为什么第二次很快）

历史日线/分钟线**永久**缓存（收盘即终态），当日数据 6 小时 TTL（防盘中数据被固化），
交易日历 30 天。同一天重跑 **0 次请求**（实测 695 次缓存命中、约 1 秒）；
隔日只补 1 个交易日的日线 + 当天全部分钟线（约 25 次调用）。

### 汇报时给什么

1. 股票池规模、成功/跳过数量、弱势信号数量；
2. 重点股票的 `la`（支撑中枢）/ `lx`（支撑下沿）/ `ha`（压力中枢）/ `ma20`；
3. 结果 CSV 路径；
4. 调用次数与耗时。

细节（接口、口径、限制、性能）见 `references/BOTTOM_FORECAST.md`。

## 更多

- 调用速查（可直接粘给新 session）：`MEMO.md`
- 运行/汇报细节：`references/WORKFLOW.md`
- 报错处理：`references/TROUBLESHOOTING.md`
- 策略口径与接口限制：`references/STRATEGY.md`
- 底部预测的口径/缓存/限制：`references/BOTTOM_FORECAST.md`
