# -*- coding: utf-8 -*-
"""
短线荐股服务（Swing Picks，仅 A 股）

职责：
1. 每个交易日盘前从全 A 股快照做硬规则初筛（剔除 ST/低流动性/超涨等）
2. 对初筛候选拉取日线做技术面筛选（均线多头排列、短期未超涨）
3. 调用 LLM 按"A股复合策略"思路精选最终 N 只，给出推荐理由
4. 计算每只的买入参考价、止盈价、止损价、最长持有截止日
5. 持仓状态落盘到 data/swing_picks/positions.json，并推送荐股通知

边界：只做荐股与提醒，不执行任何真实交易。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
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


def passes_hard_filter(row: Dict[str, Any]) -> bool:
    """判断一条全市场快照记录是否通过硬规则初筛。

    参数 row 为标准化后的字典，键：code/name/price/change_pct/amount/
    turnover_rate/volume_ratio/float_mv/change_60d（缺失值为 None）。
    任一关键字段缺失即不通过（宁缺毋滥）。
    """
    code = str(row.get("code") or "")
    name = str(row.get("name") or "")
    # 只做沪深主板+创业板；剔除 ST / 退市风险
    if not code.startswith(ALLOWED_CODE_PREFIXES):
        return False
    if "ST" in name.upper() or "退" in name:
        return False

    checks = (
        ("price", HARD_FILTER_MIN_PRICE, HARD_FILTER_MAX_PRICE),
        ("change_pct", HARD_FILTER_MIN_CHANGE_PCT, HARD_FILTER_MAX_CHANGE_PCT),
        ("turnover_rate", HARD_FILTER_MIN_TURNOVER, HARD_FILTER_MAX_TURNOVER),
        ("float_mv", HARD_FILTER_MIN_FLOAT_MV, HARD_FILTER_MAX_FLOAT_MV),
    )
    for key, low, high in checks:
        value = row.get(key)
        if value is None or not (low <= float(value) <= high):
            return False

    amount = row.get("amount")
    if amount is None or float(amount) < HARD_FILTER_MIN_AMOUNT:
        return False
    volume_ratio = row.get("volume_ratio")
    if volume_ratio is None or float(volume_ratio) < HARD_FILTER_MIN_VOLUME_RATIO:
        return False
    # 60 日涨幅可能缺失（新股等），缺失时按不通过处理
    change_60d = row.get("change_60d")
    if change_60d is None or float(change_60d) > HARD_FILTER_MAX_60D_CHANGE:
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

def _fetch_spot_snapshot() -> List[Dict[str, Any]]:
    """拉取全 A 股实时快照并标准化字段。

    数据源：akshare 东方财富接口（盘前返回上一交易日收盘口径）。
    复用 AkshareFetcher 的随机 UA + 限流机制，避免云服务器/海外 IP
    高频直连东财接口时被识别为爬虫而断连（RemoteDisconnected）。
    返回：标准化字典列表；失败时返回空列表（调用方决定如何降级）。
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
            logger.warning("[短线荐股] 全市场快照获取失败 (attempt %d/3): %s", attempt, exc)
            time.sleep(min(3 * attempt, 10))
    if df is None or df.empty:
        if last_error is not None:
            logger.error("[短线荐股] 全市场快照最终失败: %s", last_error)
        return []

    # 东财中文列名 -> 标准键（部分列缺失时取 None）
    column_map = {
        "代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "change_pct",
        "成交额": "amount", "换手率": "turnover_rate", "量比": "volume_ratio",
        "流通市值": "float_mv", "60日涨跌幅": "change_60d",
    }
    rows: List[Dict[str, Any]] = []
    for _, record in df.iterrows():
        row: Dict[str, Any] = {}
        for cn_col, key in column_map.items():
            value = record.get(cn_col)
            try:
                row[key] = None if value is None or str(value) in ("", "-", "nan") else (
                    str(value) if key in ("code", "name") else float(value)
                )
            except (TypeError, ValueError):
                row[key] = None
        rows.append(row)
    logger.info("[短线荐股] 全市场快照获取成功：%d 只", len(rows))
    return rows


def _rank_hard_filtered(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对硬筛通过的候选按活跃度排序并截断。

    排序依据：量比 × 换手率（越高代表短线资金关注度越高）。
    """
    passed = [row for row in rows if passes_hard_filter(row)]
    passed.sort(
        key=lambda r: float(r.get("volume_ratio") or 0) * float(r.get("turnover_rate") or 0),
        reverse=True,
    )
    logger.info("[短线荐股] 硬规则初筛：%d -> %d（取前 %d）",
                len(rows), len(passed), HARD_FILTER_TOP_N)
    return passed[:HARD_FILTER_TOP_N]


def _apply_technical_filter(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对候选逐只拉取日线并做技术面筛选（均线多头+未超涨）。

    单只失败不影响整体（跳过该股）；结果截断到 TECH_FILTER_TOP_N。
    """
    from data_provider import DataFetcherManager

    manager = DataFetcherManager()
    survivors: List[Dict[str, Any]] = []
    for row in candidates:
        if len(survivors) >= TECH_FILTER_TOP_N:
            break
        code = row.get("code") or ""
        try:
            df, _source = manager.get_daily_data(code, days=40)
            closes = [float(v) for v in df["close"].tolist() if v is not None]
            if passes_technical_filter(closes):
                survivors.append(row)
        except Exception as exc:
            logger.warning("[短线荐股] %s 日线获取/筛选失败，跳过: %s", code, exc)
    logger.info("[短线荐股] 技术面筛选：%d -> %d", len(candidates), len(survivors))
    return survivors


def _build_llm_prompt(candidates: List[Dict[str, Any]], pick_count: int, config: Any) -> str:
    """构造 LLM 精选提示词：候选行情表 + A股复合策略要点 + JSON 输出要求。"""
    lines = [
        f"你是A股短线交易顾问。以下是今日通过量价初筛和均线多头筛选的 {len(candidates)} 只候选股，",
        f"请按\"A股复合策略\"思路精选 {pick_count} 只做 2-3 天短线（当天买入，"
        f"目标 +{config.swing_picks_take_profit_pct}% 止盈 / -{config.swing_picks_stop_loss_pct}% 止损）。",
        "",
        "精选原则：",
        "1. 优先主线板块内的强势股，避开跟风股与单日冲高股；",
        "2. 量价配合优先：量比高、换手适中（3%-10%）、昨日放量上涨；",
        "3. 规避风险：明显连续大涨后的高位股、有减持/利空传闻的股不选；",
        "4. 行业适当分散，避免5只集中在同一板块。",
        "",
        "候选列表（代码|名称|现价|昨日涨幅%|换手率%|量比|流通市值亿|60日涨幅%）：",
    ]
    for row in candidates:
        float_mv_yi = (row.get("float_mv") or 0) / 1e8
        lines.append(
            f"{row.get('code')}|{row.get('name')}|{row.get('price')}|"
            f"{row.get('change_pct')}|{row.get('turnover_rate')}|{row.get('volume_ratio')}|"
            f"{float_mv_yi:.0f}|{row.get('change_60d')}"
        )
    lines += [
        "",
        f"只输出一个 JSON 数组，恰好 {pick_count} 个元素，不要输出其他文字：",
        '[{"code": "股票代码", "reason": "一句话推荐理由（30字内，注明所属板块/主线）"}]',
    ]
    return "\n".join(lines)


def _llm_select(candidates: List[Dict[str, Any]], config: Any) -> List[Dict[str, str]]:
    """调用 LLM 从候选中精选 N 只；LLM 不可用或解析失败时按排序兜底。"""
    pick_count = min(int(config.swing_picks_count), len(candidates))
    selected: List[Dict[str, str]] = []
    try:
        from src.analyzer import GeminiAnalyzer

        analyzer = GeminiAnalyzer(config)
        prompt = _build_llm_prompt(candidates, pick_count, config)
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

def generate_daily_picks(config: Any, *, notify: bool = True) -> List[SwingPick]:
    """执行一次完整的"每日荐股"流程并落盘、推送。

    流程：快照 -> 硬筛 -> 技术筛 -> LLM 精选 -> 生成持仓记录 -> 保存 -> 推送。

    参数：
        config: 全局 Config 对象（读取 swing_picks_* 配置）
        notify: 是否推送通知（CLI dry-run 时传 False）

    返回：本次新生成的荐股列表；筛选无结果时返回空列表并推送说明。
    """
    from src.core.trading_calendar import get_market_now, is_market_open

    today = get_market_now("cn").date()
    if not is_market_open("cn", today):
        logger.info("[短线荐股] 今天不是 A 股交易日，跳过荐股")
        return []

    rows = _fetch_spot_snapshot()
    if not rows:
        logger.error("[短线荐股] 快照不可用，今日荐股失败")
        if notify:
            _send_notification("【短线荐股】今日行情快照获取失败，未能生成荐股，请稍后手动重试。")
        return []

    candidates = _apply_technical_filter(_rank_hard_filtered(rows))
    if not candidates:
        logger.warning("[短线荐股] 今日无候选通过筛选（弱市属正常现象）")
        if notify:
            _send_notification("【短线荐股】今日全市场无股票通过筛选条件，建议空仓观望。")
        return []

    selected = _llm_select(candidates, config)
    row_by_code = {str(r.get("code")): r for r in candidates}
    is_trading_day = lambda d: is_market_open("cn", d)  # noqa: E731
    deadline = compute_deadline_date(today, int(config.swing_picks_max_hold_days), is_trading_day)

    state = load_state()
    existing = picks_from_state(state)
    active_codes = {p.code for p in existing if p.status in ACTIVE_STATUSES}
    new_picks: List[SwingPick] = []
    for item in selected:
        code = item["code"]
        if code in active_codes:
            logger.info("[短线荐股] %s 已在持仓监控中，跳过重复推荐", code)
            continue
        row = row_by_code.get(code) or {}
        new_picks.append(SwingPick(
            code=code,
            name=str(row.get("name") or ""),
            pick_date=today.isoformat(),
            ref_price=float(row.get("price") or 0),
            deadline_date=deadline.isoformat(),
            reason=item.get("reason") or "",
        ))

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
