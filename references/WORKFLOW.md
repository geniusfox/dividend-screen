# 运行与汇报流程（给 agent）

## 0. 预检

```bash
test -x ~/.local/share/dividend-screen/venv/bin/python || echo "未安装"
~/.agents/skills/dividend-screen/scripts/run.sh --check
```

`--check` 应 11/11 通过。失败按 `TROUBLESHOOTING.md` 处理。

## 1. 确定日期

- 默认不传 `--date`：脚本取"今天"并自动用**前一交易日**作为数据截止日。
- 用户指定日期时传 `--date YYYY-MM-DD`（该日之后的数据不会使用）。
- 全量运行前不需要再确认日期，除非用户明确问"最新数据到哪天"。

## 2. 先小样本，再全量

```bash
# 30 只行业样本，跳过分钟线，约 1~5 分钟
.../scripts/run.sh --limit 30 --skip-minute

# 指定股票，验证锚定价/lx/CSV
.../scripts/run.sh --codes 600019.SH,600028.SH,601006.SH
```

小样本通过后再跑全量。

## 3. 全量放后台

```bash
.../scripts/run.sh --background
```

返回 PID、日志路径。预计耗时：首次 1~2 小时（受网关限流影响），有缓存后大幅缩短。

跟踪进度：

```bash
tail -f <日志>
```

日志关键行：`[第3步] 分红预取: n/1040`、`六道关: n/388`、`最终入选: N`。

不要在主对话里同步等待；用后台任务 + 定期查看日志。

## 4. 出报告

```bash
.../scripts/report.sh
```

会选最新的 CSV 和日志，生成 `data/YYYYMMDD_summary.md`。

## 5. 汇报模板

```
入选 N 只（数据截止 YYYY-MM-DD）
漏斗：5538 → 5334 → 1040 → 863 → 388 → 108 → 69 → 44
平均股息率 4.85%，PB 均值 1.06 / 分位 8.7%，负债率均值 41.2%
分布：交通运输 10、公用事业 9、...
文件：CSV 路径 / summary 路径
```

若入选 0 只：指出漏斗卡在哪一步（股息率、负债率还是 PB），并给出该步剩余名单前几行。

## 6. 停止

后台任务需要停止时，按日志开头的 PID `kill <PID>`，或使用运行环境的后台任务管理。
