# 高股息选股 · 调用 Memo

> 对应 skill `dividend-screen` v1.1.1 · 更新于 2026-11-19
> 用途：在新 session / 新机器里快速接上这套流程。

## 0. 直接粘给新 session 的一句话

```text
用 dividend-screen 技能跑高股息选股：
1) 先执行 ~/.agents/skills/dividend-screen/scripts/run.sh --check，确认 11/11 接口通过；
2) 通过后执行 scripts/run.sh --background，把日志路径告诉我，先不用等我；
3) 跑完执行 scripts/report.sh，把入选列表、漏斗和统计摘要给我。
默认用今天的前一交易日作为数据截止日，不要向我要 token（已在本机 config.json）。
```

## 1. 路径

| 内容 | 路径 |
|---|---|
| skill 入口 | `~/.agents/skills/dividend-screen/SKILL.md` |
| 运行脚本 | `~/.agents/skills/dividend-screen/scripts/run.sh` |
| 汇总脚本 | `~/.agents/skills/dividend-screen/scripts/report.sh` |
| 数据/缓存 | `~/.local/share/dividend-screen/data/` |
| 配置（token） | `~/.local/share/dividend-screen/config.json`（600，勿提交） |
| 结果 CSV | `data/YYYYMMDD_dividend_screen.csv` |
| 汇总 MD | `data/YYYYMMDD_summary.md` |
| 运行日志 | `data/run_YYYYMMDD_HHMMSS.log` |
| 仓库 | https://github.com/geniusfox/dividend-screen |

## 2. 标准流程

```bash
# 1) 自检（约 30 秒；强制走真实请求，不受缓存影响）
~/.agents/skills/dividend-screen/scripts/run.sh --check

# 2) 先小样本验证（可选，约 1~5 分钟）
~/.agents/skills/dividend-screen/scripts/run.sh --limit 30 --skip-minute

# 3) 后台全量（默认数据截止到"今天的前一交易日"）
~/.agents/skills/dividend-screen/scripts/run.sh --background
#    → 返回 PID、日志路径；首次全量约 1~2 小时

# 4) 看进度（另开终端或让 agent 读日志）
tail -f ~/.local/share/dividend-screen/data/run_*.log

# 5) 出汇总（选最新 CSV + 最新日志）
~/.agents/skills/dividend-screen/scripts/report.sh
```

也可以直接命令行调用（不经过 agent）：

```bash
~/.local/share/dividend-screen/bin/dividend-screen --limit 30 --skip-minute
~/.local/share/dividend-screen/bin/dividend-screen --date 2026-08-13
~/.local/share/dividend-screen/bin/dividend-screen-report
```

## 3. 常用参数

| 参数 | 作用 |
|---|---|
| `--date YYYY-MM-DD` | 目标交易日；默认今天，脚本自动取它的前一交易日 |
| `--limit N` | 行业筛选后只跑前 N 只（调试用） |
| `--codes A,B` | 只跑指定 `ts_code`（调试用，如 `600019.SH,600028.SH`） |
| `--skip-minute` | 跳过分钟线，`lx` 列为空，速度快很多 |
| `--refresh` | 忽略所有缓存/TTL，强制全量刷新 |
| `--no-cache` | 完全不读不写缓存 |
| `--min-interval 0.35` | 两次请求最小间隔秒数 |
| `--check` | 只做接口权限自检，不选股 |

## 4. 增量缓存（为什么第二次很快）

| 数据 | 刷新策略 |
|---|---|
| 交易日历、行业目录/成分 | 30 天 |
| 股票池 | 1 天 |
| 分红、扣非/负债率、PB 五年序列 | 7 天 |
| 年度现金流 | 最近一年 30 天，更早永久 |
| 按交易日的行情/估值、分钟线、ST 名单 | 历史永久，只补新日期 |

实测：

- 3 只样本：首跑 53 次请求 → 同日期重跑 **0 次** → 隔日 4 次；
- 全量 44 只 + 全量缓存 + 断网：**0 次请求，本地计算 2.3 秒**；
- 全量隔日重跑约 **90 次**；每 7 天刷新一次分红/财务/PB（约 1000 次）。

需要最新财报/分红时加 `--refresh`。

## 5. 结果怎么读

- **漏斗**：5538 → 5334 → 1040 → PB 预筛 → 分红年数 → 六道关 → 股息率 → 负债率 → PB 分位 → 最终。
- 关键列：`avg_div_yield_hist`（当前股息率）、`pb_ratio`/`pb_percentile`、`debt_ratio`、
  `cash_cover_ratio`、`P_avg`/`P_min`（锚定价）、`lx`（次日最低价预测）。
- `P_min` 按无风险利率×1.3 反推，`P_avg` 按历史平均股息率反推；
  历史股息率 > 3.51% 时 `P_avg < P_min` 属公式特性。
- 入选 0 只时，先看漏斗卡在哪一步，再决定是否放宽阈值。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| 全部接口“无访问权限” | token 积分/权限不足；`--check` 确认，检查 `config.json` |
| `token 不对` | 重新粘贴 token 到 `~/.local/share/dividend-screen/config.json` |
| `stk_mins` 无权限 / `lx` 为空 | 网关未开通历史分钟；加 `--skip-minute` 或忽略 |
| 频繁 `edge_conn_limit` / `rate_limited` | 网关限流，脚本自动退避；不要并发跑多个实例 |
| 运行很慢 | 首次正常；隔日只补差异。想更快可 `--skip-minute` |
| 想重装 | `bash <(curl -fsSL https://raw.githubusercontent.com/geniusfox/dividend-screen/main/install.sh)` |

## 7. 新机器从零安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/geniusfox/dividend-screen/main/install.sh)
```

按提示粘贴自己的 Tushare token；装完**新开一个 DSH session**，再用第 0 节那句话。
