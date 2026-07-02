# 短线荐股（Swing Picks）专题

每个交易日自动从全 A 股挑选若干只 2-3 天短线候选：当天买入，触及止盈/止损目标推送卖出提醒，最后一个持有日仍未达标则推送强制卖出提醒。

**能力边界：本功能只做荐股与提醒，不对接券商、不自动下单。** 收到提醒后需自行在券商 App 操作。

## 工作流程

1. **盘前荐股（默认 09:00）**：
   - 拉取全 A 股行情快照：优先东方财富源（字段最全），若该源在当前网络环境下持续失败（部分云服务器/海外 IP 会被限制），自动降级到新浪轻量快照源；
   - 硬规则初筛：仅沪深主板+创业板，剔除 ST/退市风险，核心字段（价格 3-100 元、日成交额 ≥2 亿、昨日涨幅 1%-8%）所有快照源必有、缺失即拒绝；换手率 2%-15%、量比 ≥1.2、流通市值 30 亿-1000 亿、60 日涨幅 ≤60% 仅东财源提供，新浪兜底场景下这些字段缺失时不参与判断（不会因此拒绝候选，但精度低于东财源）；
   - 技术面筛选：MA5 ≥ MA10 ≥ MA20 多头排列、近 5 日累计涨幅 ≤25%、现价不破 MA10；
   - LLM 精选：按"A股复合策略"思路（主线优先、量价配合、行业分散、规避高位股）精选最终 N 只；LLM 不可用时按量价活跃度兜底，保证有产出；
   - 推送荐股通知（复用已配置的通知渠道），并落盘到持仓状态文件。
2. **开盘后回填买入价**：以开盘价（缺失时取最新价）作为买入参考价，计算止盈价/止损价。
3. **盘中监控**（交易时段内按配置间隔轮询）：
   - 现价 ≥ 止盈价 → 推送"止盈卖出提醒"；
   - 现价 ≤ 止损价 → 推送"止损卖出提醒"；
   - 最后持有日 14:40 后仍未达标 → 推送"到期强制卖出提醒"（停牌拖过截止日的，恢复后任意交易时刻提醒）。
4. 每条持仓的每个事件只提醒一次；所有状态与卖出价都记录在状态文件中，可用于事后统计胜率。

## 配置（.env）

```env
SWING_PICKS_ENABLED=true              # 总开关（默认 false）
SWING_PICKS_COUNT=5                   # 每天精选数量
SWING_PICKS_TAKE_PROFIT_PCT=5         # 止盈涨幅 %
SWING_PICKS_STOP_LOSS_PCT=3           # 止损跌幅 %
SWING_PICKS_MAX_HOLD_DAYS=3           # 最长持有交易日数（含买入日）
SWING_PICKS_MORNING_TIME=09:00        # 盘前荐股时间
SWING_PICKS_MONITOR_INTERVAL_MINUTES=5 # 盘中监控轮询间隔（分钟）
```

启用后重启 Web/API 服务（`server.py` / VPS 上 `systemctl restart dsa`），后台 worker 会随服务进程自动运行；无需依赖 `SCHEDULE_ENABLED`。

## 手动触发

```bash
python scripts/run_swing_picks.py            # 立即生成今日荐股并推送
python scripts/run_swing_picks.py --dry-run  # 只生成不推送（试跑）
python scripts/run_swing_picks.py --monitor-once  # 手动执行一轮持仓监控
python scripts/run_swing_picks.py --status   # 查看当前持仓状态
```

## 状态文件

路径 `data/swing_picks/positions.json`（可用 `SWING_PICKS_STATE_PATH` 覆盖）。每条记录包含：代码/名称、荐股日、参考价、买入参考价、止盈/止损价、截止日、状态（`pending_entry`/`open`/`target_hit`/`stop_hit`/`expired`）、卖出价与卖出日、已推送事件。

## 相关代码

- `src/services/swing_picks_service.py` — 筛选流水线、价位计算、状态持久化、荐股通知
- `src/services/swing_picks_worker.py` — 后台监控线程（荐股触发、买入价回填、到价/到期提醒）
- `scripts/run_swing_picks.py` — 手动触发 CLI
- `tests/test_swing_picks.py` — 纯逻辑单元测试

## 风险提示

荐股结果由规则筛选与 AI 生成，仅供参考，不构成投资建议；短线交易风险高，请自行控制仓位。
