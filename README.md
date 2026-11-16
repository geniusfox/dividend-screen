# dividend-screen（高股息选股 skill）

把《高股息策略量化选股 v10》打包成 DSH skill：一句话让 agent 完成
数据抓取 → 六道关筛选 → 锚定价 → 次日最低价预测 → CSV + Markdown 汇总。

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

## 使用

装完后对 DSH 说：

> 用高股息策略帮我筛选一下 A 股

或命令行：

```bash
~/.local/share/dividend-screen/bin/dividend-screen --limit 30 --skip-minute
~/.agents/skills/dividend-screen/scripts/run.sh --background
~/.agents/skills/dividend-screen/scripts/report.sh
```

默认取"今天的前一交易日"作为数据截止日；全量首次约 1~2 小时，之后有缓存会快很多。

## 输出

- `~/.local/share/dividend-screen/data/YYYYMMDD_dividend_screen.csv`（23 列）
- `~/.local/share/dividend-screen/data/YYYYMMDD_summary.md`（漏斗 + 列表 + 统计）
- `data/run_*.log`（运行日志）

## 安全

- token 只存在本机 `config.json`，安装器不会上传；不要把 token 提交到仓库或发到聊天里。
- 数据来自 Tushare / 你配置的网关，仅本地缓存。

## 卸载

```bash
rm -rf ~/.agents/skills/dividend-screen ~/.local/share/dividend-screen
```
