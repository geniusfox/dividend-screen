# dividend-screen · 高股息选股 skill

把《高股息策略量化选股 v10》打包成 DSH skill：一句话完成
Tushare 数据抓取 → 六道关筛选 → 锚定价 → 次日最低价预测 → CSV + Markdown 汇总。

## 一键安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/<your-github-user>/<repo>/main/install.sh) \
  --pkg-url https://github.com/<your-github-user>/<repo>/archive/refs/heads/main.tar.gz
```

安装器会：

1. 把 skill 复制到 `~/.agents/skills/dividend-screen/`；
2. 在 `~/.local/share/dividend-screen/` 建 venv、装依赖；
3. 交互式询问你的 Tushare token（写入 `config.json`，600 权限，不进仓库）；
4. 跑一次 11 个接口的权限自检。

> 安装完成后**新开一个 DSH 会话**（或重启 DSH），让它重新扫描 `~/.agents/skills`，
> 然后说：**“用高股息策略帮我筛选一下 A 股”**。

## 手动安装

```bash
git clone https://github.com/<your-github-user>/<repo>.git
cd <repo>
bash install.sh
```

也可以从 Release 下载 `dividend-screen-skill.tar.gz` 后用
`bash install.sh --pkg-url <tar.gz 地址>` 安装。

## 使用

```bash
# 全量后台跑（默认数据截止到"今天的前一交易日"）
~/.agents/skills/dividend-screen/scripts/run.sh --background

# 小样本验证
~/.agents/skills/dividend-screen/scripts/run.sh --limit 30 --skip-minute

# 生成 summary
~/.agents/skills/dividend-screen/scripts/report.sh
```

输出在 `~/.local/share/dividend-screen/data/`：

- `YYYYMMDD_dividend_screen.csv`（23 列，UTF-8 BOM）
- `YYYYMMDD_summary.md`（漏斗 + 列表 + 统计）
- `run_*.log`

## 要求

- macOS / Linux，Python 3.9+
- Tushare token（官方 2000+ 积分，或自建网关 + `tushare_api_url`）
- 首次全量约 1~2 小时，之后有缓存会快很多

## 安全

- token 只存在本机 `~/.local/share/dividend-screen/config.json`，不要提交到仓库。
- 安装脚本不会上传 token，也不会读取与安装无关的文件。

## 目录

```
SKILL.md              # skill 入口（触发词、运行方式）
install.sh            # 安装器
scripts/run.sh        # 前台/后台运行
scripts/report.sh     # 生成 summary
scripts/dividend_screen.py
scripts/make_summary.py
references/           # 流程、排错、字段口径
```
