# dividend-screen（高股息选股 + 股票底部预测 skill）

把《高股息策略量化选股 v10》和「股票底部预测」一起打包成 DSH skill，一句话让 agent 完成：

| 功能 | 流程 | 入口 |
|---|---|---|
| **高股息选股** | 数据抓取 → 六道关筛选 → 锚定价 → CSV + Markdown 汇总 | `scripts/run.sh` / `scripts/report.sh` |
| **股票底部预测** | 日线 + 当天 1 分钟线 → 支撑/压力位（`la`/`lx`/`ha`）→ 买点信号 CSV | `scripts/bottom.sh` |

两个功能共用同一份 token 配置、同一个 Tushare 网关和同一套增量缓存。

## 安装

需要：macOS/Linux、Python 3.9+、网络、Tushare token（官方 2000+ 积分，或自建网关）。

推荐：一条命令安装。

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/geniusfox/dividend-screen/main/install.sh)
```

离线/本地安装（解压后进入目录）：

```bash
bash install.sh
```

指定安装包地址（镜像、Release 或 fork）：

```bash
bash install.sh --pkg-url https://example.com/dividend-screen-skill.tar.gz
```

安装器会：

1. 把 skill 复制到 `~/.agents/skills/dividend-screen/`；
2. 在 `~/.local/share/dividend-screen/` 建 venv、装依赖；
3. 交互式询问 token（写入 `config.json`，600 权限，不进仓库）；
4. 跑一次 11 个接口的权限自检。

> 安装完成后**新开一个 DSH 会话**（或重启 DSH），让它重新扫描 `~/.agents/skills`，
> 然后说“用高股息策略筛选一下 A 股”即可。

## 调用备忘

日常命令、增量缓存、排错速查：见 [MEMO.md](MEMO.md)。

## 使用

装完后对 DSH 说：

> 用高股息策略帮我筛选一下 A 股

或命令行：

```bash
~/.local/share/dividend-screen/bin/dividend-screen --limit 30 --skip-minute
~/.agents/skills/dividend-screen/scripts/run.sh --background
~/.agents/skills/dividend-screen/scripts/report.sh
~/.local/share/dividend-screen/bin/dividend-screen-bottom --codes 600795,002060 -v
```

默认取"今天的前一交易日"作为数据截止日。缓存是增量的：

- 首跑全量约 1~2 小时（约 3000 次请求）；
- **同日期重跑接近 0 请求**（全量缓存实测：0 请求，本地计算约 2 秒）；
- 隔日重跑只补新交易日/分钟线，约 90 次请求；
- 分红、财务、PB 序列默认 7 天刷新；要最新数据加 `--refresh`。

## 股票底部预测怎么用

```bash
# 全量（股票池约 666 只；稳定态每天约 25 次 API 调用）
~/.agents/skills/dividend-screen/scripts/bottom.sh

# 后台
~/.agents/skills/dividend-screen/scripts/bottom.sh --background

# 小样本
~/.agents/skills/dividend-screen/scripts/bottom.sh --codes 600795,002060 -v
```

输出 `data/YYYYMMDD_forecast_price.csv`，每只股票一行，重点是：

| 列 | 含义 |
|---|---|
| `la` | 支撑中枢（越低越接近底部区间） |
| `lx` | 支撑下沿（最保守的低吸价） |
| `ha` | 压力中枢 |
| `ma20` | 20 日均线；`lx < ma20×0.85` 时 `signal_strength=weak`（风险提示） |

口径、缓存分层与排错见 [references/BOTTOM_FORECAST.md](references/BOTTOM_FORECAST.md)。

## 输出

- `~/.local/share/dividend-screen/data/YYYYMMDD_dividend_screen.csv`（23 列）
- `~/.local/share/dividend-screen/data/YYYYMMDD_summary.md`（漏斗 + 列表 + 统计）
- `~/.local/share/dividend-screen/data/YYYYMMDD_forecast_price.csv`（底部预测，19 列）
- `data/run_*.log` / `data/bottom_run_*.log`（运行日志）

## 安全

- token 只存在本机 `config.json`，安装器不会上传；不要把 token 提交到仓库或发到聊天里。
- 数据来自 Tushare / 你配置的网关，仅本地缓存。

## 卸载

```bash
rm -rf ~/.agents/skills/dividend-screen ~/.local/share/dividend-screen
```
