# -*- coding: utf-8 -*-
"""
短线荐股后台监控 Worker

职责（单个后台线程，随 Web/API 服务进程运行，由 SWING_PICKS_ENABLED 开关控制）：
1. 交易日到达配置的荐股时间（默认 10:00，盘中口径；配置早于开盘则为盘前口径）
   且当天未荐股时，触发每日荐股
2. 为盘前口径产生的待入场记录回填买入参考价（开盘后以开盘价为准）并计算
   止盈/止损价；盘中口径的荐股在生成时已定价，无需回填
3. 交易时段内按配置间隔轮询持仓价格：
   - 触及止盈价 -> 推送"止盈卖出提醒"
   - 触及止损价 -> 推送"止损卖出提醒"
   - 最后持有日 14:40 后仍未达标 -> 推送"到期强制卖出提醒"
4. 所有状态变化落盘，同一事件只推送一次

边界：只提醒不下单；行情获取失败时跳过本轮，下一轮重试。
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time as dt_time
from typing import Any, Callable, Dict, Optional

from src.services.swing_picks_service import (
    PICK_STATUS_EXPIRED,
    PICK_STATUS_OPEN,
    PICK_STATUS_PENDING,
    PICK_STATUS_STOP,
    PICK_STATUS_TARGET,
    TRADING_SESSIONS_CN,
    SwingPick,
    _send_notification,
    apply_entry_pricing,
    generate_daily_picks,
    load_state,
    picks_from_state,
    picks_to_state,
    save_state,
)

logger = logging.getLogger(__name__)

# A 股交易时段（连续竞价）——统一取自 service 的单一定义，避免 9:30/15:00 多处硬编码
MORNING_SESSION, AFTERNOON_SESSION = TRADING_SESSIONS_CN
# 最后持有日的强制卖出提醒时间（留出收盘前的操作时间）
FORCE_SELL_REMINDER_TIME = dt_time(14, 40)
# worker 线程基础轮询节拍（秒）；实际行情轮询频率由配置的分钟间隔控制
WORKER_LOOP_SECONDS = 30


# ---------------------------------------------------------------------------
# 纯逻辑函数（无 IO，便于单元测试）
# ---------------------------------------------------------------------------

def is_in_trading_session(t: dt_time) -> bool:
    """判断给定时间是否处于 A 股连续竞价时段（上午或下午）。"""
    return (MORNING_SESSION[0] <= t <= MORNING_SESSION[1]) or (
        AFTERNOON_SESSION[0] <= t <= AFTERNOON_SESSION[1]
    )


def parse_morning_time(value: str) -> dt_time:
    """解析 HH:MM 格式的盘前荐股时间；格式非法时回退默认 09:00。"""
    try:
        parts = str(value).strip().split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError, TypeError):
        logger.warning("[短线荐股] SWING_PICKS_MORNING_TIME=%r 格式非法，回退 09:00", value)
        return dt_time(9, 0)


def should_run_morning_pick(now_time: dt_time, morning_time: dt_time, last_pick_date: str, today: date) -> bool:
    """判断当前是否应该触发每日荐股。

    条件：已到配置的盘前时间，且状态里记录的上次荐股日期不是今天。
    """
    return now_time >= morning_time and last_pick_date != today.isoformat()


def evaluate_position(pick: SwingPick, price: float, today: date, now_time: dt_time) -> Optional[str]:
    """评估一只持仓当前应触发的卖出事件。

    参数：
        pick:     持仓记录（status 必须为 open 才有意义）
        price:    最新价
        today:    今天日期（A股时区）
        now_time: 当前时间

    返回：'target'（止盈）/'stop'（止损）/'expired'（到期强制卖）/None（继续持有）
    优先级：止盈/止损先于到期判断（到价立即提醒，不等到期）。
    """
    if pick.status != PICK_STATUS_OPEN:
        return None
    if pick.target_price and price >= pick.target_price:
        return "target"
    if pick.stop_price and price <= pick.stop_price:
        return "stop"
    if pick.deadline_date:
        try:
            deadline = date.fromisoformat(pick.deadline_date)
        except ValueError:
            return None
        # 截止日 14:40 后仍未达标 -> 到期强制卖；若因停牌等拖过截止日，之后任意交易时刻也提醒
        if today > deadline or (today == deadline and now_time >= FORCE_SELL_REMINDER_TIME):
            return "expired"
    return None


def build_sell_notification(pick: SwingPick, event: str, price: float) -> str:
    """生成卖出提醒推送文本。

    参数 event 取值 'target'/'stop'/'expired'，其余值按到期处理。
    """
    profit_pct = 0.0
    if pick.entry_price:
        profit_pct = (price - pick.entry_price) / pick.entry_price * 100
    titles = {
        "target": "🎯 止盈卖出提醒",
        "stop": "🛑 止损卖出提醒",
        "expired": "⏰ 持有到期卖出提醒",
    }
    title = titles.get(event, titles["expired"])
    lines = [
        f"## {title}",
        "",
        f"**{pick.name}（{pick.code}）** 现价 {price} 元（{profit_pct:+.2f}%）",
        f"- 买入参考价：{pick.entry_price} 元（{pick.pick_date}）",
        f"- 止盈价：{pick.target_price} 元｜止损价：{pick.stop_price} 元",
    ]
    if event == "expired":
        lines.append(f"- 已到最长持有期（截止 {pick.deadline_date}），按纪律建议收盘前卖出。")
    else:
        lines.append("- 已触及目标价位，建议按纪律执行卖出。")
    lines.append("")
    lines.append("⚠️ 仅供参考，不构成投资建议；系统不会自动下单。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Worker 主体
# ---------------------------------------------------------------------------

class SwingPicksWorker:
    """短线荐股后台 worker：荐股触发 + 买入价回填 + 盘中监控。"""

    def __init__(self, config_provider: Optional[Callable[[], Any]] = None) -> None:
        """初始化 worker。

        参数：
            config_provider: 返回最新 Config 的函数（默认 get_config，注入以便测试）
        """
        if config_provider is None:
            from src.config import get_config

            config_provider = get_config
        self._config_provider = config_provider
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_monitor_at: Optional[datetime] = None

    # ---- 单轮执行（也供 CLI --monitor-once 调用） ----

    def run_once(self, now: Optional[datetime] = None) -> Dict[str, int]:
        """执行一轮检查，返回统计信息（供日志与测试断言）。

        异常处理：任何一步失败只记录日志，不让 worker 线程退出。
        """
        stats = {"picked": 0, "entries_filled": 0, "sell_alerts": 0}
        try:
            config = self._config_provider()
            if not getattr(config, "swing_picks_enabled", False):
                return stats

            from src.core.trading_calendar import get_market_now, is_market_open

            market_now = get_market_now("cn", now)
            today = market_now.date()
            if not is_market_open("cn", today):
                return stats
            now_time = market_now.time()

            stats["picked"] = self._maybe_run_morning_pick(config, now_time, today)
            if is_in_trading_session(now_time):
                stats["entries_filled"] = self._fill_pending_entries(config, today)
                # 刚荐完股的这一轮跳过监控：监控用的行情缓存可能比荐股定价的
                # 快照旧（最长约 20 分钟），立即监控可能对新持仓误发卖出提醒
                if stats["picked"] == 0 and self._should_poll_quotes(market_now, config):
                    stats["sell_alerts"] = self._monitor_open_positions(today, now_time)
        except Exception as exc:
            logger.error("[短线荐股] worker 单轮执行异常: %s", exc, exc_info=True)
        return stats

    def _maybe_run_morning_pick(self, config: Any, now_time: dt_time, today: date) -> int:
        """到达配置的荐股时间且当天未荐股时触发荐股，返回新增数量。"""
        state = load_state()
        morning_time = parse_morning_time(getattr(config, "swing_picks_morning_time", "09:00"))
        if not should_run_morning_pick(now_time, morning_time, state.get("last_pick_date", ""), today):
            return 0
        logger.info("[短线荐股] 触发每日荐股（%s）", today.isoformat())
        return len(generate_daily_picks(config, notify=True))

    def _fill_pending_entries(self, config: Any, today: date) -> int:
        """开盘后为待入场记录回填买入参考价并计算止盈/止损价。

        买入参考价取开盘价（缺失时取最新价），模拟"当天开盘附近买入"。
        """
        state = load_state()
        picks = picks_from_state(state)
        pending = [p for p in picks if p.status == PICK_STATUS_PENDING]
        if not pending:
            return 0

        from data_provider import DataFetcherManager

        manager = DataFetcherManager()
        filled = 0
        for pick in pending:
            try:
                quote = manager.get_realtime_quote(pick.code)
                price = getattr(quote, "open_price", None) or getattr(quote, "price", None)
                if not price or float(price) <= 0:
                    continue
                # 定价逻辑与盘中即时定价共用同一函数，保证两种路径规则一致
                apply_entry_pricing(pick, float(price), config)
                filled += 1
                logger.info("[短线荐股] %s 买入参考价 %.2f（止盈 %.2f/止损 %.2f）",
                            pick.code, pick.entry_price, pick.target_price, pick.stop_price)
            except Exception as exc:
                logger.warning("[短线荐股] %s 回填买入价失败，下一轮重试: %s", pick.code, exc)
        if filled:
            save_state(picks_to_state(state, picks))
        return filled

    def _monitor_open_positions(self, today: date, now_time: dt_time) -> int:
        """轮询监控中的持仓，触发卖出事件时推送提醒并落盘，返回提醒数。"""
        state = load_state()
        picks = picks_from_state(state)
        open_picks = [p for p in picks if p.status == PICK_STATUS_OPEN]
        if not open_picks:
            return 0

        from data_provider import DataFetcherManager

        manager = DataFetcherManager()
        alerts = 0
        changed = False
        status_by_event = {
            "target": PICK_STATUS_TARGET,
            "stop": PICK_STATUS_STOP,
            "expired": PICK_STATUS_EXPIRED,
        }
        for pick in open_picks:
            try:
                quote = manager.get_realtime_quote(pick.code)
                price = getattr(quote, "price", None)
                if not price or float(price) <= 0:
                    continue
                event = evaluate_position(pick, float(price), today, now_time)
                if not event or event in pick.notified_events:
                    continue
                pick.status = status_by_event[event]
                pick.exit_price = float(price)
                pick.exit_date = today.isoformat()
                pick.notified_events.append(event)
                changed = True
                dedup_key = f"swing_picks:{pick.code}:{pick.pick_date}:{event}"
                if _send_notification(build_sell_notification(pick, event, float(price)), dedup_key=dedup_key):
                    alerts += 1
            except Exception as exc:
                logger.warning("[短线荐股] %s 监控失败，下一轮重试: %s", pick.code, exc)
        if changed:
            save_state(picks_to_state(state, picks))
        return alerts

    def _should_poll_quotes(self, market_now: datetime, config: Any) -> bool:
        """按配置的分钟间隔限制行情轮询频率（worker 线程节拍更快）。"""
        interval_minutes = max(1, int(getattr(config, "swing_picks_monitor_interval_minutes", 5)))
        if self._last_monitor_at is not None:
            elapsed = (market_now - self._last_monitor_at).total_seconds()
            if elapsed < interval_minutes * 60:
                return False
        self._last_monitor_at = market_now
        return True

    # ---- 线程生命周期 ----

    def start(self) -> None:
        """启动后台线程（daemon，随主进程退出）。重复调用无副作用。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="swing-picks-worker", daemon=True)
        self._thread.start()
        logger.info("[短线荐股] 后台监控 worker 已启动")

    def stop(self) -> None:
        """请求停止后台线程（最多等待一个节拍）。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=WORKER_LOOP_SECONDS + 5)
            self._thread = None
        logger.info("[短线荐股] 后台监控 worker 已停止")

    def _loop(self) -> None:
        """线程主循环：固定节拍执行 run_once，直到收到停止信号。"""
        while not self._stop_event.wait(WORKER_LOOP_SECONDS):
            self.run_once()


def start_swing_picks_worker_if_enabled() -> Optional[SwingPicksWorker]:
    """若配置开启短线荐股，创建并启动 worker；否则返回 None。

    供 api/app.py 的 lifespan 调用；配置读取失败时返回 None 不影响服务启动。
    """
    try:
        from src.config import get_config

        if not getattr(get_config(), "swing_picks_enabled", False):
            return None
        worker = SwingPicksWorker()
        worker.start()
        return worker
    except Exception as exc:
        logger.error("[短线荐股] worker 启动失败（不影响主服务）: %s", exc)
        return None
