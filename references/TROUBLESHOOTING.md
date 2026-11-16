# 常见问题

## 全部接口报"没有接口(xxx)访问权限"

token 已通过鉴权，但账号积分/权限不足。需要 2000 积分档；到
https://tushare.pro/user/ 查看积分，或换有权限的 token。用 `--check` 确认。

## 报"您的token不对"

token 复制错误或失效。重新复制后更新
`~/.local/share/dividend-screen/config.json` 的 `tushare_token`。

## 自建/代理网关

config.json 增加：

```json
{"tushare_api_url": "http://your-gateway/path/"}
```

该网关通常还要求：显式 `fields`、`index_member` 按行业取成分、`cashflow` 单次 ≤1 年。
本项目已适配，无需额外设置。

## 频繁 `edge_conn_limit` / `upstream_pool_exhausted` / `rate_limited`

网关限流，脚本会自动退避重试，日志里的黄色警告属正常。不要并发跑多个实例。

## 运行很慢

首次全量 1~2 小时属正常（约 3000+ 次请求）。中断后重跑会复用
`data/cache/`，基本秒级续跑。想快速验证用 `--limit 30 --skip-minute`。

## `stk_mins` 无权限 / lx 为空

分钟线需要网关单独开通"历史分钟"。不影响其余结果；可加 `--skip-minute` 跳过。

## 部分股票没有财务数据

个别股票 `fina_indicator` 可能超时或被上游丢弃，脚本会跳过该股并继续；
日志中会有 `xxx fina_indicator 失败`。

## 重装 / 升级

重新运行 `install.sh` 即可；`config.json` 和 `data/` 在
`~/.local/share/dividend-screen/`，不会被覆盖。
