# -*- coding: utf-8 -*-
"""
短线荐股服务（Swing Picks，仅 A 股）

职责：
1. 每个交易日从全 A 股快照做硬规则初筛（剔除 ST/低流动性/超涨等）
2. 对初筛候选拉取日线做技术面筛选（均线多头排列、短期未超涨）
3. 调用 LLM 按"A股复合策略"思路精选最终 N 只，给出推荐理由
4. 计算每只的买入参考价、止盈价、止损价、最长持有截止日
5. 持仓状态落盘到 data/swing_picks/positions.json，并推送荐股通知

双模式（按触发时刻的市场阶段自动切换，无需额外开关）：
- 盘前模式（开盘前触发）：快照为上一交易日收盘口径，"涨跌幅"是昨日涨幅；
  荐股后等开盘由 worker 回填开盘价作为买入参考价。
- 盘中模式（连续竞价/午休/尾盘竞价时段触发，如默认 10:00）：快照为当日实时口径，
  "涨跌幅"自动变为今日实时涨幅（即"当天正在走强"）；成交额/换手率门槛按
  已交易时长动态折算（10:00 约 25%，随时间推移趋近 100%），涨幅下限在开盘
  初期同步放宽；额外做当日分时走势健康筛选（阳线、未冲高大幅回落、未大幅低开，
  相关字段缺失即拒绝）；荐股当下直接以现价确定买入参考价并立即进入监控。
- 收盘后触发（如服务器宕机后下午恢复补跑）：跳过当日荐股并标记已处理，
  避免用全天数据套盘中门槛产生错误推荐。

市场阶段优先由 trading_calendar.infer_market_phase 判定（能识别午休/收盘），
日历库不可用时按本地时钟兜底。

边界：只做荐股与提醒，不执行任何真实交易。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 持仓状态枚举：
# pending_entry: 已荐股，等待开盘后记录买入参考成交价
# open:          已记录买入价，监控中
# target_hit:    已触发止盈提醒（终态）
# stop_hit:      已触发止损提醒（终态）
# expired:       持有到期强制卖出提醒（终态）
PICK_STATUS_PENDING = "pending_entry"
PICK_STATUS_OPEN = "open"
PICK_STATUS_TARGET = "target_hit"
PICK_STATUS_STOP = "stop_hit"
PICK_STATUS_EXPIRED = "expired"
ACTIVE_STATUSES = (PICK_STATUS_PENDING, PICK_STATUS_OPEN)

# 状态文件默认路径（项目根目录 data/swing_picks/positions.json）
_DEFAULT_STATE_PATH = Path("data") / "swing_picks" / "positions.json"

# 硬筛选阈值（集中定义，便于调整；这些是策略常数而非用户配置项）
HARD_FILTER_MIN_PRICE = 3.0          # 最低股价（元），剔除低价问题股
HARD_FILTER_MAX_PRICE = 100.0        # 最高股价（元），控制单手成本
HARD_FILTER_MIN_AMOUNT = 2e8         # 最低日成交额（元），保证流动性
HARD_FILTER_MIN_TURNOVER = 2.0       # 最低换手率（%），要求有交易热度
HARD_FILTER_MAX_TURNOVER = 15.0      # 最高换手率（%），剔除极端过热
HARD_FILTER_MIN_VOLUME_RATIO = 1.2   # 最低量比，要求量能活跃
HARD_FILTER_MIN_CHANGE_PCT = 1.0     # 最低昨日涨幅（%），要求强势
HARD_FILTER_MAX_CHANGE_PCT = 8.0     # 最高昨日涨幅（%），排除涨停无法低吸
HARD_FILTER_MIN_FLOAT_MV = 3e9       # 最低流通市值（元），剔除微盘操纵风险
HARD_FILTER_MAX_FLOAT_MV = 1e11      # 最高流通市值（元），太大短线弹性差
HARD_FILTER_MAX_60D_CHANGE = 60.0    # 60 日累计涨幅上限（%），排除已暴涨股
TECH_FILTER_MAX_5D_CHANGE = 25.0     # 近 5 日累计涨幅上限（%），A股短期反转风险
HARD_FILTER_TOP_N = 25               # 硬筛后按活跃度保留的候选数
TECH_FILTER_TOP_N = 12               # 技术筛后送入 LLM 的候选数
ALLOWED_CODE_PREFIXES = ("60", "00", "30")  # 沪深主板+创业板；排除科创板/北交所

# 盘中模式相关常数
# A 股连续竞价时段（worker 的交易时段判断也从这里取，保证 9:30/15:00 只定义一处）
TRADING_SESSIONS_CN = ((dt_time(9, 30), dt_time(11, 30)), (dt_time(13, 0), dt_time(15, 0)))
TOTAL_SESSION_MINUTES = 240              # A 股全天连续竞价总分钟数（2 小时 x 2）
INTRADAY_VOLUME_RATIO = 0.25             # 量额折算系数下限：开盘首 30 分钟成交约占全天 25%（成交前高后低，非线性）
CHANGE_MIN_RAMP_MINUTES = 30             # 涨幅下限的放宽窗口：开盘后前 30 分钟内按比例放宽（刚开盘大多数股票涨幅未展开）
INTRADAY_MAX_PULLBACK_PCT = 3.0          # 现价距当日最高点最大回撤（%），过滤"冲高后大幅回落"
INTRADAY_MIN_OPEN_TO_PRECLOSE = 0.98     # 今开不得低于昨收的比例，过滤"大幅低开硬拉"（多为出货形态）

# 荐股口径（由触发时刻的市场阶段解析而来）
MODE_PREMARKET = "premarket"             # 盘前：按昨日数据筛选，等开盘回填买入价
MODE_INTRADAY = "intraday"               # 盘中：按今日实时数据筛选，现价即时定买入价
MODE_POSTMARKET = "postmarket"           # 收盘后：跳过当日荐股（无法当日买入，且盘中门槛不适用）


@dataclass
class SwingPick:
    """一条短线荐股记录（同时也是持仓跟踪单元）。

    字段：
        code/name:      股票代码与名称
        pick_date:      荐股日期（ISO 格式 YYYY-MM-DD，即建议买入日）
        ref_price:      荐股时的参考价（盘前为昨收）
        entry_price:    买入参考价（开盘后以实际行情回填；据此算止盈/止损）
        target_price:   止盈价（entry * (1 + 止盈%/100)）
        stop_price:     止损价（entry * (1 - 止损%/100)）
        deadline_date:  最长持有截止交易日（含当日，收盘前未达标强制提醒卖出）
        reason:         推荐理由（LLM 生成或技术面兜底说明）
        status:         状态机，见模块顶部枚举说明
        exit_price:     触发卖出提醒时的价格（用于事后统计胜率）
        exit_date:      触发卖出提醒的日期
        notified_events: 已推送过的事件列表，防止重复推送
    """

    code: str
    name: str
    pick_date: str
    ref_price: float
    entry_price: Optional[float] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    deadline_date: str = ""
    reason: str = ""
    status: str = PICK_STATUS_PENDING
    exit_price: Optional[float] = None
    exit_date: Optional[str] = None
    notified_events: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 纯逻辑函数（无网络/IO，便于单元测试）
# ---------------------------------------------------------------------------

def compute_target_price(entry_price: float, take_profit_pct: float) -> float:
    """按买入价和止盈百分比计算止盈价，保留两位小数。

    参数不合法（<=0）时抛出 ValueError，避免生成无意义价位。
    """
    if entry_price <= 0 or take_profit_pct <= 0:
        raise ValueError("买入价与止盈百分比必须为正数")
    return round(entry_price * (1 + take_profit_pct / 100.0), 2)


def compute_stop_price(entry_price: float, stop_loss_pct: float) -> float:
    """按买入价和止损百分比计算止损价，保留两位小数。"""
    if entry_price <= 0 or stop_loss_pct <= 0:
        raise ValueError("买入价与止损百分比必须为正数")
    return round(entry_price * (1 - stop_loss_pct / 100.0), 2)


def compute_deadline_date(
    pick_date: date,
    max_hold_days: int,
    is_trading_day: Callable[[date], bool],
) -> date:
    """计算最长持有截止交易日。

    规则：买入日算第 1 个交易日，持有 max_hold_days 个交易日；
    例如 max_hold_days=3 时，截止日为买入日之后的第 2 个交易日。

    参数：
        pick_date:      买入日（应为交易日）
        max_hold_days:  最长持有交易日数（>=1）
        is_trading_day: 判断某天是否交易日的函数（注入以便测试）
    """
    if max_hold_days < 1:
        raise ValueError("最长持有天数必须 >= 1")
    remaining = max_hold_days - 1
    current = pick_date
    guard = 0
    while remaining > 0:
        current = current + timedelta(days=1)
        guard += 1
        if guard > 30:  # 防御：日历异常时避免死循环（30 天内必有足够交易日）
            break
        if is_trading_day(current):
            remaining -= 1
    return current


def intraday_elapsed_minutes(t: dt_time) -> int:
    """计算给定时刻 A 股已完成的连续竞价分钟数。

    参数：
        t: 当前时刻（A 股时区的本地时间）

    返回：0~240 的整数。开盘前为 0；午休期间为 120（上午已走完）；
    收盘后为 240。用于按已交易时长折算盘中门槛。
    """
    minutes_of = lambda x: x.hour * 60 + x.minute  # noqa: E731
    now_m = minutes_of(t)
    elapsed = 0
    for start, end in TRADING_SESSIONS_CN:
        elapsed += max(0, min(now_m, minutes_of(end)) - minutes_of(start))
    return elapsed


def intraday_hard_filter_scales(t: dt_time) -> tuple:
    """按已交易时长计算盘中硬筛的两个折算系数。

    参数：
        t: 荐股触发时刻

    返回：(量额折算系数, 涨幅下限折算系数)
    - 量额系数：已交易分钟占全天比例，下限 INTRADAY_VOLUME_RATIO（A 股成交
      前高后低，开盘首 30 分钟约占全天 25%，故 10:00 前后取 0.25 而非线性 0.125），
      上限 1.0。作用于成交额门槛与换手率上下限。
    - 涨幅下限系数：开盘后前 CHANGE_MIN_RAMP_MINUTES 分钟内线性放宽
      （刚开盘大多数股票当日涨幅未展开，按全天标准 1% 会误杀全市场），
      之后恢复 1.0。仅作用于涨幅下限，8% 上限任何时刻都有效（排除接近涨停）。
    """
    elapsed = intraday_elapsed_minutes(t)
    amount_scale = min(1.0, max(INTRADAY_VOLUME_RATIO, elapsed / TOTAL_SESSION_MINUTES))
    change_min_scale = min(1.0, elapsed / CHANGE_MIN_RAMP_MINUTES)
    return amount_scale, change_min_scale


def resolve_pick_mode(phase: str, now_time: dt_time) -> str:
    """由市场阶段解析荐股口径（纯函数，便于测试）。

    参数：
        phase:    trading_calendar.MarketPhase 的取值（str 枚举，如 "premarket"/
                  "intraday"/"lunch_break"/"closing_auction"/"postmarket"；
                  日历库不可用时为 "unknown"）
        now_time: 当前时刻（phase 不可用时按时钟兜底）

    返回：MODE_PREMARKET / MODE_INTRADAY / MODE_POSTMARKET 之一。
    午休与尾盘竞价归入盘中（快照数据仍是当日有效口径）；
    unknown/non_trading 按本地时钟兜底判断，保证日历库缺失时功能不中断。
    """
    if phase == "premarket":
        return MODE_PREMARKET
    if phase in ("intraday", "lunch_break", "closing_auction"):
        return MODE_INTRADAY
    if phase == "postmarket":
        return MODE_POSTMARKET
    # 日历不可用（unknown 等）：按时钟兜底
    if now_time < TRADING_SESSIONS_CN[0][0]:
        return MODE_PREMARKET
    if now_time >= TRADING_SESSIONS_CN[1][1]:
        return MODE_POSTMARKET
    return MODE_INTRADAY


def passes_hard_filter(row: Dict[str, Any], *, intraday_time: Optional[dt_time] = None) -> bool:
    """判断一条全市场快照记录是否通过硬规则初筛。

    参数：
        row:           标准化后的字典，键：code/name/price/change_pct/amount/
                       turnover_rate/volume_ratio/float_mv/change_60d（缺失值为 None）
        intraday_time: 盘中模式下传触发时刻，None 表示盘前模式。盘中快照的
                       成交额/换手率是"到目前为止"的累计值，按已交易时长动态
                       折算门槛（上下限同步折算）；涨幅下限在开盘初期同步放宽。
                       涨跌幅在盘中自动是今日实时涨幅（"今日正在强"）。

    返回：True 表示通过初筛。任一核心字段缺失即不通过（宁缺毋滥）。
    """
    code = str(row.get("code") or "")
    name = str(row.get("name") or "")
    # 只做沪深主板+创业板；剔除 ST / 退市风险
    if not code.startswith(ALLOWED_CODE_PREFIXES):
        return False
    if "ST" in name.upper() or "退" in name:
        return False

    # 盘中模式下成交额/换手率是"到目前为止"的累计值，门槛按已交易时长动态折算；
    # 量比不折算——它天生就是"当前每分钟量 vs 过去5日每分钟均量"的盘中口径。
    if intraday_time is None:
        amount_scale, change_min_scale = 1.0, 1.0
    else:
        amount_scale, change_min_scale = intraday_hard_filter_scales(intraday_time)

    # 核心字段（价格/涨跌幅/成交额）所有快照源都会提供，缺失即拒绝
    required_checks = (
        ("price", HARD_FILTER_MIN_PRICE, HARD_FILTER_MAX_PRICE),
        ("change_pct", HARD_FILTER_MIN_CHANGE_PCT * change_min_scale, HARD_FILTER_MAX_CHANGE_PCT),
    )
    for key, low, high in required_checks:
        value = row.get(key)
        if value is None or not (low <= float(value) <= high):
            return False

    amount = row.get("amount")
    if amount is None or float(amount) < HARD_FILTER_MIN_AMOUNT * amount_scale:
        return False

    # 以下字段仅东财等富字段源提供；轻量兜底源（如新浪）缺失时不参与该项判断，
    # 而不是直接拒绝——避免主数据源被限流时兜底源完全无法产出候选。
    optional_checks = (
        ("turnover_rate", HARD_FILTER_MIN_TURNOVER * amount_scale, HARD_FILTER_MAX_TURNOVER * amount_scale),
        ("volume_ratio", HARD_FILTER_MIN_VOLUME_RATIO, None),
        ("float_mv", HARD_FILTER_MIN_FLOAT_MV, HARD_FILTER_MAX_FLOAT_MV),
        ("change_60d", None, HARD_FILTER_MAX_60D_CHANGE),
    )
    for key, low, high in optional_checks:
        value = row.get(key)
        if value is None:
            continue
        value = float(value)
        if low is not None and value < low:
            return False
        if high is not None and value > high:
            return False
    return True


def passes_technical_filter(closes: List[float]) -> bool:
    """基于最近收盘价序列做技术面筛选（多头排列 + 短期未超涨）。

    参数：
        closes: 按时间升序排列的收盘价列表（至少 20 个交易日）

    规则：
    1. MA5 >= MA10 >= MA20（均线多头排列，趋势向上）
    2. 近 5 日累计涨幅 <= 25%（规避 A 股短期反转效应）
    3. 现价不低于 MA10（未破短期趋势）
    """
    if len(closes) < 20:
        return False
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    if not (ma5 >= ma10 >= ma20):
        return False
    base = closes[-6]
    if base <= 0:
        return False
    change_5d = (closes[-1] - base) / base * 100
    if change_5d > TECH_FILTER_MAX_5D_CHANGE:
        return False
    return closes[-1] >= ma10


def passes_intraday_health(row: Dict[str, Any]) -> bool:
    """盘中模式专用：判断当日分时走势是否健康（避免"昨天好、今天崩"）。

    参数：
        row: 标准化快照字典，需含 price/open/high/pre_close 四个字段。
             东财与新浪快照源都提供这些列，任一缺失或非正说明该行数据异常
             （停牌、脏数据等），直接拒绝——这是盘中模式的核心安全闸，
             宁可错杀不可放行（与硬筛可选字段"缺失跳过"的策略不同）。

    三条规则：
    1. 现价 >= 今开：当日为阳线，开盘后仍在走强；
    2. (当日最高 - 现价) / 当日最高 <= INTRADAY_MAX_PULLBACK_PCT%：
       不是冲高后大幅回落的（回落超阈值说明当日抛压重）；
    3. 今开 >= 昨收 * INTRADAY_MIN_OPEN_TO_PRECLOSE：排除大幅低开的。

    返回：True 表示当日走势健康；任一字段缺失/非正或任一规则不满足返回 False。
    """
    values = {}
    for key in ("price", "open", "high", "pre_close"):
        value = row.get(key)
        if value is None or float(value) <= 0:
            return False
        values[key] = float(value)

    if values["price"] < values["open"]:
        return False
    if values["open"] < values["pre_close"] * INTRADAY_MIN_OPEN_TO_PRECLOSE:
        return False
    if (values["high"] - values["price"]) / values["high"] * 100 > INTRADAY_MAX_PULLBACK_PCT:
        return False
    return True


def build_close_series(
    dates: List[Any],
    closes: List[Any],
    today_iso: str,
    current_price: Optional[float] = None,
) -> List[float]:
    """构造技术面筛选用的收盘价序列，处理盘中"半根K线"问题。

    盘中拉日线时，部分数据源会把今天尚未走完的实时 bar 作为最后一行返回
    （且不同源行为不一致），若直接混入均线计算会产生偏差。统一做法：
    剔除日期 >= 今天的行，只保留截至昨天的完整K线；盘中模式再把快照现价
    追加到末尾充当"最新收盘"，使 MA 与近5日涨幅都包含今天的实时状态。

    参数：
        dates:         与 closes 一一对应的日期（datetime 或字符串均可，
                       转 str 后前 10 位为 YYYY-MM-DD 才能正确比较）
        closes:        收盘价列表（None 项跳过）
        today_iso:     今天日期的 ISO 字符串
        current_price: 盘中模式传快照现价（追加到序列末尾）；盘前传 None

    返回：升序收盘价序列（float 列表）。
    """
    series = [
        float(c)
        for d, c in zip(dates, closes)
        if c is not None and str(d)[:10] < today_iso
    ]
    if current_price is not None and current_price > 0:
        series.append(float(current_price))
    return series


def parse_llm_pick_response(text: str, valid_codes: List[str]) -> List[Dict[str, str]]:
    """从 LLM 回复中解析精选结果 JSON。

    期望格式：[{"code": "600000", "reason": "..."}, ...]
    做了两层容错：先整体解析，失败再用正则提取第一个 JSON 数组。
    只保留 code 在候选集中的条目，防止 LLM 幻觉出候选之外的代码。
    """
    if not text:
        return []
    candidates_set = set(valid_codes)

    def _normalize(items: Any) -> List[Dict[str, str]]:
        result: List[Dict[str, str]] = []
        if not isinstance(items, list):
            return result
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if code in candidates_set:
                result.append({"code": code, "reason": str(item.get("reason") or "").strip()})
        return result

    try:
        return _normalize(json.loads(text))
    except (ValueError, TypeError):
        pass
    match = re.search(r"\[[\s\S]*?\]", text)
    if match:
        try:
            return _normalize(json.loads(match.group(0)))
        except (ValueError, TypeError):
            pass
    logger.warning("[短线荐股] LLM 回复无法解析为 JSON，将使用技术面排序兜底")
    return []


# ---------------------------------------------------------------------------
# 数据获取与筛选流水线（含网络 IO）
# ---------------------------------------------------------------------------

# 各数据源中文列名 -> 标准键。东财字段最全；新浪缺换手率/量比/流通市值/60日涨幅，
# 这些字段在 passes_hard_filter 中按"缺失则跳过该项判断"处理，不会导致新浪兜底完全失效。
# 今开/最高/昨收 两源都有，供盘中模式的当日走势健康筛选（passes_intraday_health）使用。
_EM_SNAPSHOT_COLUMN_MAP = {
    "代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "change_pct",
    "成交额": "amount", "换手率": "turnover_rate", "量比": "volume_ratio",
    "流通市值": "float_mv", "60日涨跌幅": "change_60d",
    "今开": "open", "最高": "high", "昨收": "pre_close",
}
_SINA_SNAPSHOT_COLUMN_MAP = {
    "代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "change_pct",
    "成交额": "amount",
    "今开": "open", "最高": "high", "昨收": "pre_close",
}


def _normalize_snapshot_df(df: Any, column_map: Dict[str, str]) -> List[Dict[str, Any]]:
    """把快照 DataFrame 按列名映射转换为标准化字典列表（缺失列/脏值取 None）。

    代码字段统一走 normalize_stock_code 归一化：新浪源返回的"代码"可能带
    sh/sz 交易所前缀（如 sh600000），不归一化会导致 ALLOWED_CODE_PREFIXES
    判断全部失败；东财源本身是纯 6 位数字，归一化是幂等操作、不受影响。
    """
    from data_provider.base import normalize_stock_code

    rows: List[Dict[str, Any]] = []
    for _, record in df.iterrows():
        row: Dict[str, Any] = {}
        for cn_col, key in column_map.items():
            value = record.get(cn_col)
            try:
                if value is None or str(value) in ("", "-", "nan"):
                    row[key] = None
                elif key == "code":
                    row[key] = normalize_stock_code(str(value))
                elif key == "name":
                    row[key] = str(value)
                else:
                    row[key] = float(value)
            except (TypeError, ValueError):
                row[key] = None
        rows.append(row)
    return rows


def _fetch_spot_snapshot() -> List[Dict[str, Any]]:
    """拉取全 A 股实时快照并标准化字段。

    主数据源：akshare 东方财富接口（字段最全，盘前返回上一交易日收盘口径）。
    复用 AkshareFetcher 的随机 UA + 限流机制，避免云服务器/海外 IP
    高频直连东财接口时被识别为爬虫而断连（RemoteDisconnected）。
    主源彻底失败时（如该 VPS 出口 IP 被限制），回退到新浪轻量快照接口
    ——字段较少但通常不受同样的限制。
    返回：标准化字典列表；两个源都失败时返回空列表（调用方决定如何降级）。
    """
    try:
        import akshare as ak
        from data_provider.akshare_fetcher import AkshareFetcher
    except ImportError:
        logger.error("[短线荐股] akshare 未安装，无法获取全市场快照")
        return []

    anti_block = AkshareFetcher()
    df = None
    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            anti_block._set_random_user_agent()
            anti_block._enforce_rate_limit()
            df = ak.stock_zh_a_spot_em()
            break
        except Exception as exc:  # 网络类异常：退避后重试
            last_error = exc
            logger.warning("[短线荐股] 全市场快照获取失败(东财, attempt %d/3): %s", attempt, exc)
            time.sleep(min(3 * attempt, 10))

    if df is not None and not df.empty:
        rows = _normalize_snapshot_df(df, _EM_SNAPSHOT_COLUMN_MAP)
        logger.info("[短线荐股] 全市场快照获取成功(东财)：%d 只", len(rows))
        return rows

    if last_error is not None:
        logger.warning("[短线荐股] 东财快照最终失败，尝试新浪兜底: %s", last_error)
    try:
        anti_block._set_random_user_agent()
        anti_block._enforce_rate_limit()
        sina_df = ak.stock_zh_a_spot()
    except Exception as exc:
        logger.error("[短线荐股] 新浪兜底快照也失败: %s", exc)
        return []
    if sina_df is None or sina_df.empty:
        logger.error("[短线荐股] 新浪兜底快照返回为空")
        return []
    rows = _normalize_snapshot_df(sina_df, _SINA_SNAPSHOT_COLUMN_MAP)
    logger.info("[短线荐股] 全市场快照获取成功(新浪兜底，字段较少)：%d 只", len(rows))
    return rows


def _rank_hard_filtered(
    rows: List[Dict[str, Any]], *, intraday_time: Optional[dt_time] = None,
) -> List[Dict[str, Any]]:
    """对硬筛通过的候选按活跃度排序并截断。

    参数：
        rows:          标准化快照记录列表
        intraday_time: 盘中模式下传触发时刻（用于动态折算门槛），None 为盘前模式

    盘中模式额外做当日分时走势健康筛选（先过硬筛再过健康筛，
    健康筛只用快照已有字段，无额外网络开销）。
    排序依据：量比 × 换手率（越高代表短线资金关注度越高）。
    返回：截断到 HARD_FILTER_TOP_N 的候选列表。
    """
    intraday = intraday_time is not None
    passed = [row for row in rows if passes_hard_filter(row, intraday_time=intraday_time)]
    if intraday:
        healthy = [row for row in passed if passes_intraday_health(row)]
        logger.info("[短线荐股] 盘中走势健康筛选：%d -> %d", len(passed), len(healthy))
        passed = healthy
    passed.sort(
        key=lambda r: float(r.get("volume_ratio") or 0) * float(r.get("turnover_rate") or 0),
        reverse=True,
    )
    logger.info("[短线荐股] 硬规则初筛：%d -> %d（取前 %d）",
                len(rows), len(passed), HARD_FILTER_TOP_N)
    return passed[:HARD_FILTER_TOP_N]


def _apply_technical_filter(
    candidates: List[Dict[str, Any]],
    *,
    today: date,
    intraday: bool = False,
) -> List[Dict[str, Any]]:
    """对候选逐只拉取日线并做技术面筛选（均线多头+未超涨）。

    统一通过 build_close_series 剔除日线里今天的"半根K线"（盘中拉取时
    部分源会返回当日实时 bar，混入均线计算会有偏差）；盘中模式再把快照
    现价追加为最新价，使"现价站上 MA10 / 近5日涨幅"都反映今天的实时状态。

    单只失败不影响整体（跳过该股）；结果截断到 TECH_FILTER_TOP_N。
    """
    from data_provider import DataFetcherManager

    manager = DataFetcherManager()
    today_iso = today.isoformat()
    survivors: List[Dict[str, Any]] = []
    for row in candidates:
        if len(survivors) >= TECH_FILTER_TOP_N:
            break
        code = row.get("code") or ""
        try:
            df, _source = manager.get_daily_data(code, days=40)
            current_price = float(row.get("price") or 0) if intraday else None
            closes = build_close_series(
                df["date"].tolist(), df["close"].tolist(), today_iso, current_price)
            if passes_technical_filter(closes):
                survivors.append(row)
        except Exception as exc:
            logger.warning("[短线荐股] %s 日线获取/筛选失败，跳过: %s", code, exc)
    logger.info("[短线荐股] 技术面筛选：%d -> %d", len(candidates), len(survivors))
    return survivors


def _build_llm_prompt(
    candidates: List[Dict[str, Any]], pick_count: int, config: Any, *, intraday: bool = False,
) -> str:
    """构造 LLM 精选提示词：候选行情表 + A股复合策略要点 + JSON 输出要求。

    盘中模式下涨幅列是今日实时涨幅（非昨日），措辞相应切换，
    并附今开/当日最高两列供 AI 判断当日分时形态。
    """
    change_label = "今日涨幅%" if intraday else "昨日涨幅%"
    momentum_rule = (
        "2. 量价配合优先：量比高、换手适中（3%-10%）、今日放量走强（数据为盘中实时口径，"
        "换手率是到当前时刻的累计值）；" if intraday
        else "2. 量价配合优先：量比高、换手适中（3%-10%）、昨日放量上涨；"
    )
    header_cols = f"代码|名称|现价|{change_label}|换手率%|量比|流通市值亿|60日涨幅%"
    if intraday:
        header_cols += "|今开|当日最高"
    lines = [
        f"你是A股短线交易顾问。以下是今日通过量价初筛和均线多头筛选的 {len(candidates)} 只候选股，",
        f"请按\"A股复合策略\"思路精选 {pick_count} 只做 2-3 天短线（当天买入，"
        f"目标 +{config.swing_picks_take_profit_pct}% 止盈 / -{config.swing_picks_stop_loss_pct}% 止损）。",
        "",
        "精选原则：",
        "1. 优先主线板块内的强势股，避开跟风股与单日冲高股；",
        momentum_rule,
        "3. 规避风险：明显连续大涨后的高位股、有减持/利空传闻的股不选；",
        "4. 行业适当分散，避免5只集中在同一板块。",
        "",
        f"候选列表（{header_cols}）：",
    ]
    for row in candidates:
        float_mv_yi = (row.get("float_mv") or 0) / 1e8
        line = (
            f"{row.get('code')}|{row.get('name')}|{row.get('price')}|"
            f"{row.get('change_pct')}|{row.get('turnover_rate')}|{row.get('volume_ratio')}|"
            f"{float_mv_yi:.0f}|{row.get('change_60d')}"
        )
        if intraday:
            line += f"|{row.get('open')}|{row.get('high')}"
        lines.append(line)
    lines += [
        "",
        f"只输出一个 JSON 数组，恰好 {pick_count} 个元素，不要输出其他文字：",
        '[{"code": "股票代码", "reason": "一句话推荐理由（30字内，注明所属板块/主线）"}]',
    ]
    return "\n".join(lines)


def _llm_select(
    candidates: List[Dict[str, Any]], config: Any, *, intraday: bool = False,
) -> List[Dict[str, str]]:
    """调用 LLM 从候选中精选 N 只；LLM 不可用或解析失败时按排序兜底。"""
    pick_count = min(int(config.swing_picks_count), len(candidates))
    selected: List[Dict[str, str]] = []
    try:
        from src.analyzer import GeminiAnalyzer

        analyzer = GeminiAnalyzer(config)
        prompt = _build_llm_prompt(candidates, pick_count, config, intraday=intraday)
        text = analyzer.generate_text(prompt, max_tokens=1024, temperature=0.3)
        selected = parse_llm_pick_response(text or "", [str(r.get("code")) for r in candidates])
    except Exception as exc:
        logger.warning("[短线荐股] LLM 精选失败，使用技术面排序兜底: %s", exc)

    if len(selected) < pick_count:
        # 兜底：按初筛活跃度排序补足（保证每天都有产出）
        chosen = {item["code"] for item in selected}
        for row in candidates:
            if len(selected) >= pick_count:
                break
            code = str(row.get("code"))
            if code not in chosen:
                selected.append({"code": code, "reason": "量价活跃+均线多头（技术面兜底入选）"})
    return selected[:pick_count]


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------

def get_state_path() -> Path:
    """返回状态文件路径（可用环境变量 SWING_PICKS_STATE_PATH 覆盖，便于测试）。"""
    override = os.getenv("SWING_PICKS_STATE_PATH", "").strip()
    return Path(override) if override else _DEFAULT_STATE_PATH


def load_state() -> Dict[str, Any]:
    """读取持仓状态；文件不存在或损坏时返回空状态（不让主流程崩溃）。"""
    path = get_state_path()
    if not path.exists():
        return {"last_pick_date": "", "picks": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("状态文件根节点不是对象")
        data.setdefault("last_pick_date", "")
        data.setdefault("picks", [])
        return data
    except Exception as exc:
        logger.error("[短线荐股] 状态文件读取失败，按空状态处理: %s", exc)
        return {"last_pick_date": "", "picks": []}


def save_state(state: Dict[str, Any]) -> None:
    """原子写入持仓状态（先写临时文件再替换，避免中断产生半截文件）。"""
    path = get_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def picks_from_state(state: Dict[str, Any]) -> List[SwingPick]:
    """把状态字典中的 picks 反序列化为 SwingPick 列表，坏记录跳过。"""
    picks: List[SwingPick] = []
    for item in state.get("picks", []):
        try:
            known = {k: item[k] for k in item if k in SwingPick.__dataclass_fields__}
            picks.append(SwingPick(**known))
        except (TypeError, KeyError) as exc:
            logger.warning("[短线荐股] 跳过无法解析的持仓记录: %s (%s)", item, exc)
    return picks


def picks_to_state(state: Dict[str, Any], picks: List[SwingPick]) -> Dict[str, Any]:
    """把 SwingPick 列表写回状态字典（返回新字典，不修改入参）。"""
    new_state = dict(state)
    new_state["picks"] = [asdict(p) for p in picks]
    return new_state


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

def apply_entry_pricing(pick: SwingPick, price: float, config: Any) -> None:
    """按给定买入价为一条荐股记录定价并进入监控状态（service/worker 共用）。

    参数：
        pick:   荐股记录（就地修改 entry_price/target_price/stop_price/status）
        price:  买入参考价。盘中荐股传荐股当下的现价；盘前模式由 worker
                在开盘后传开盘价（缺失时最新价）
        config: 全局 Config（读取止盈/止损百分比）

    返回：无（就地修改 pick）。
    异常：price 或配置的止盈/止损百分比非正时抛 ValueError（由 compute_* 抛出）。
    """
    pick.entry_price = round(float(price), 2)
    pick.target_price = compute_target_price(float(price), float(config.swing_picks_take_profit_pct))
    pick.stop_price = compute_stop_price(float(price), float(config.swing_picks_stop_loss_pct))
    pick.status = PICK_STATUS_OPEN


def _build_new_picks(
    selected: List[Dict[str, str]],
    row_by_code: Dict[str, Dict[str, Any]],
    active_codes: set,
    today: date,
    deadline: date,
    config: Any,
    intraday: bool,
) -> List[SwingPick]:
    """把 LLM 精选结果构造成新的荐股持仓记录列表。

    参数：
        selected:     精选结果（code + reason）
        row_by_code:  代码 -> 标准化快照记录（取名称/现价）
        active_codes: 已在监控中的代码集合（跳过重复推荐）
        today:        荐股日
        deadline:     最长持有截止日
        config:       全局 Config
        intraday:     盘中模式时直接以现价定买入价并进入监控
                      （现价经硬筛保证在 3-100 元区间，必为正数）

    返回：新生成的 SwingPick 列表。
    """
    new_picks: List[SwingPick] = []
    for item in selected:
        code = item["code"]
        if code in active_codes:
            logger.info("[短线荐股] %s 已在持仓监控中，跳过重复推荐", code)
            continue
        row = row_by_code.get(code) or {}
        pick = SwingPick(
            code=code,
            name=str(row.get("name") or ""),
            pick_date=today.isoformat(),
            ref_price=float(row.get("price") or 0),
            deadline_date=deadline.isoformat(),
            reason=item.get("reason") or "",
        )
        if intraday:
            # 盘中即时定价：开盘价已是几十分钟前的旧价格，用现价才不失真
            apply_entry_pricing(pick, pick.ref_price, config)
        new_picks.append(pick)
    return new_picks


def _skip_postmarket_pick(today: date, notify: bool) -> None:
    """收盘后触发时跳过当日荐股：标记已处理避免 worker 反复重试，并说明原因。

    参数：
        today:  当天日期
        notify: 是否推送说明通知

    返回：无。
    """
    logger.warning("[短线荐股] 触发时已收盘，跳过今日荐股（全天数据不适用盘中门槛，且当日已无法买入）")
    state = load_state()
    state["last_pick_date"] = today.isoformat()
    save_state(state)
    if notify:
        _send_notification("【短线荐股】今日触发时已收盘，跳过荐股；明日将按计划正常运行。")


def generate_daily_picks(config: Any, *, notify: bool = True) -> List[SwingPick]:
    """执行一次完整的"每日荐股"流程并落盘、推送。

    流程：快照 -> 硬筛(盘中含走势健康筛) -> 技术筛 -> LLM 精选 -> 生成持仓记录 -> 保存 -> 推送。
    模式：按触发时刻的市场阶段自动选择盘前/盘中口径（见模块顶部说明）；
    收盘后触发则跳过当日荐股。

    参数：
        config: 全局 Config 对象（读取 swing_picks_* 配置）
        notify: 是否推送通知（CLI dry-run 时传 False）

    返回：本次新生成的荐股列表；筛选无结果或已收盘时返回空列表并推送说明。
    """
    from src.core.trading_calendar import get_market_now, infer_market_phase, is_market_open

    market_now = get_market_now("cn")
    today = market_now.date()
    if not is_market_open("cn", today):
        logger.info("[短线荐股] 今天不是 A 股交易日，跳过荐股")
        return []

    mode = resolve_pick_mode(str(infer_market_phase("cn", market_now).value), market_now.time())
    if mode == MODE_POSTMARKET:
        _skip_postmarket_pick(today, notify)
        return []
    intraday = mode == MODE_INTRADAY
    logger.info("[短线荐股] 本次按%s口径荐股", "盘中" if intraday else "盘前")

    rows = _fetch_spot_snapshot()
    if not rows:
        logger.error("[短线荐股] 快照不可用，今日荐股失败")
        if notify:
            _send_notification("【短线荐股】今日行情快照获取失败，未能生成荐股，请稍后手动重试。")
        return []

    intraday_time = market_now.time() if intraday else None
    candidates = _apply_technical_filter(
        _rank_hard_filtered(rows, intraday_time=intraday_time), today=today, intraday=intraday)
    if not candidates:
        logger.warning("[短线荐股] 今日无候选通过筛选（弱市属正常现象）")
        if notify:
            _send_notification("【短线荐股】今日全市场无股票通过筛选条件，建议空仓观望。")
        return []

    selected = _llm_select(candidates, config, intraday=intraday)
    row_by_code = {str(r.get("code")): r for r in candidates}
    is_trading_day = lambda d: is_market_open("cn", d)  # noqa: E731
    deadline = compute_deadline_date(today, int(config.swing_picks_max_hold_days), is_trading_day)

    state = load_state()
    existing = picks_from_state(state)
    active_codes = {p.code for p in existing if p.status in ACTIVE_STATUSES}
    new_picks = _build_new_picks(selected, row_by_code, active_codes, today, deadline, config, intraday)

    state["last_pick_date"] = today.isoformat()
    state = picks_to_state(state, existing + new_picks)
    save_state(state)
    logger.info("[短线荐股] 今日荐股完成：%d 只，截止日 %s", len(new_picks), deadline.isoformat())

    if notify and new_picks:
        _send_notification(format_picks_notification(new_picks, config))
    return new_picks


def format_picks_notification(picks: List[SwingPick], config: Any) -> str:
    """把荐股列表格式化为推送用 Markdown 文本。"""
    today = picks[0].pick_date if picks else datetime.now().date().isoformat()
    lines = [
        f"## 📈 今日短线荐股（{today}）",
        "",
        f"策略：2-3 天短线｜止盈 +{config.swing_picks_take_profit_pct}%｜"
        f"止损 -{config.swing_picks_stop_loss_pct}%｜最迟 {picks[0].deadline_date if picks else ''} 收盘前卖出",
        "",
    ]
    for i, p in enumerate(picks, 1):
        if p.entry_price:
            # 盘中模式：荐股当下已按现价定好买入参考价与止盈/止损价（不再重复显示参考价）
            lines.append(
                f"**{i}. {p.name}（{p.code}）** 买入参考 {p.entry_price} 元"
                f"｜止盈 {p.target_price} 元｜止损 {p.stop_price} 元")
        else:
            lines.append(f"**{i}. {p.name}（{p.code}）** 参考价 {p.ref_price} 元")
        lines.append(f"   - 理由：{p.reason}")
    lines += [
        "",
        "买入后系统将盘中监控，触及止盈/止损或到期会推送卖出提醒。",
        "⚠️ 仅供参考，不构成投资建议；系统不会自动下单。",
    ]
    return "\n".join(lines)


def _send_notification(content: str, *, dedup_key: Optional[str] = None) -> bool:
    """通过已配置的通知渠道推送消息；渠道未配置或失败只记日志不抛异常。"""
    try:
        from src.notification import NotificationService

        service = NotificationService()
        if not service.is_available():
            logger.warning("[短线荐股] 未配置任何通知渠道，消息未推送")
            return False
        return service.send(content, dedup_key=dedup_key)
    except Exception as exc:
        logger.error("[短线荐股] 通知推送失败: %s", exc)
        return False
