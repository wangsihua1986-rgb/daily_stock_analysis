# -*- coding: utf-8 -*-
"""
短线荐股功能单元测试

覆盖纯逻辑部分（不依赖网络与真实行情）：
- 止盈/止损价计算与非法输入
- 最长持有截止交易日计算（跳过周末/节假日）
- 硬规则初筛（ST/板块前缀/字段缺失/区间边界）
- 盘中模式硬筛门槛折算（成交额/换手率按已交易时段折算）
- 盘中当日走势健康筛选（阳线/冲高回落/大幅低开）
- 收盘价序列构造（剔除当天半根K线、盘中追加现价）
- 盘中荐股即时定价（现价定买入参考价并直接进入监控）
- 技术面筛选（均线多头、短期超涨）
- LLM 回复解析容错（含幻觉代码过滤）
- 持仓卖出事件状态机（止盈/止损/到期/优先级）
- 盘前荐股触发条件与交易时段判断
- 状态文件读写（含损坏文件降级）
"""

from __future__ import annotations

from datetime import date, time as dt_time

import pytest

from src.services.swing_picks_service import (
    MODE_INTRADAY,
    MODE_POSTMARKET,
    MODE_PREMARKET,
    PICK_STATUS_OPEN,
    PICK_STATUS_PENDING,
    SwingPick,
    _normalize_snapshot_df,
    _SINA_SNAPSHOT_COLUMN_MAP,
    apply_entry_pricing,
    build_close_series,
    compute_deadline_date,
    compute_stop_price,
    compute_target_price,
    intraday_elapsed_minutes,
    load_state,
    parse_llm_pick_response,
    passes_hard_filter,
    passes_intraday_health,
    passes_technical_filter,
    picks_from_state,
    picks_to_state,
    resolve_pick_mode,
    save_state,
)
from src.services.swing_picks_worker import (
    build_sell_notification,
    evaluate_position,
    is_in_trading_session,
    parse_morning_time,
    should_run_morning_pick,
)


def _valid_row(**overrides):
    """构造一条能通过硬筛的合成快照记录，供测试按需破坏单个字段。"""
    row = {
        "code": "600000",
        "name": "测试股份",
        "price": 12.5,
        "change_pct": 3.2,
        "amount": 5e8,
        "turnover_rate": 5.0,
        "volume_ratio": 1.8,
        "float_mv": 8e9,
        "change_60d": 15.0,
    }
    row.update(overrides)
    return row


def _pick(**overrides) -> SwingPick:
    """构造一条监控中的合成持仓记录。"""
    fields = dict(
        code="600000", name="测试股份", pick_date="2026-07-02", ref_price=10.0,
        entry_price=10.0, target_price=10.5, stop_price=9.7,
        deadline_date="2026-07-06", status=PICK_STATUS_OPEN,
    )
    fields.update(overrides)
    return SwingPick(**fields)


class TestPriceComputation:
    """止盈/止损价计算。"""

    def test_target_price(self):
        assert compute_target_price(10.0, 5.0) == 10.5

    def test_stop_price(self):
        assert compute_stop_price(10.0, 3.0) == 9.7

    def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError):
            compute_target_price(0, 5.0)
        with pytest.raises(ValueError):
            compute_stop_price(10.0, -1)


class TestDeadline:
    """最长持有截止日：买入日算第 1 个交易日。"""

    @staticmethod
    def _weekday_only(d: date) -> bool:
        return d.weekday() < 5  # 周一~周五视为交易日

    def test_three_days_over_weekend(self):
        # 周四买入，持有3个交易日 -> 周四(1) 周五(2) 下周一(3)
        assert compute_deadline_date(date(2026, 7, 2), 3, self._weekday_only) == date(2026, 7, 6)

    def test_one_day_is_same_day(self):
        assert compute_deadline_date(date(2026, 7, 2), 1, self._weekday_only) == date(2026, 7, 2)

    def test_invalid_hold_days(self):
        with pytest.raises(ValueError):
            compute_deadline_date(date(2026, 7, 2), 0, self._weekday_only)


class TestHardFilter:
    """全市场快照硬规则初筛。"""

    def test_valid_row_passes(self):
        assert passes_hard_filter(_valid_row())

    @pytest.mark.parametrize("overrides", [
        {"name": "ST测试"},           # ST 股
        {"name": "退市测试"},          # 退市风险
        {"code": "688001"},           # 科创板（不在允许前缀）
        {"code": "830001"},           # 北交所
        {"price": 2.0},               # 低价股
        {"price": 150.0},             # 高价股
        {"change_pct": 0.5},          # 昨日不够强势
        {"change_pct": 9.9},          # 接近涨停（无法低吸）
        {"amount": 1e8},              # 流动性不足
        {"volume_ratio": 0.8},        # 量能不活跃
        {"turnover_rate": 20.0},      # 换手过热
        {"float_mv": 1e9},            # 微盘
        {"change_60d": 80.0},         # 已暴涨
        {"price": None},              # 核心字段缺失（价格所有源必有）
        {"amount": None},             # 核心字段缺失（成交额所有源必有）
    ])
    def test_rejections(self, overrides):
        assert not passes_hard_filter(_valid_row(**overrides))

    def test_missing_optional_fields_still_passes(self):
        # 模拟新浪兜底快照：缺换手率/量比/流通市值/60日涨幅，
        # 仅核心字段齐全时仍应通过（缺失的可选项不参与判断，不是"宁缺毋滥"）。
        row = _valid_row(turnover_rate=None, volume_ratio=None, float_mv=None, change_60d=None)
        assert passes_hard_filter(row)


class TestHardFilterIntraday:
    """盘中模式硬筛：成交额/换手率门槛按已交易时长动态折算，量比不折算。"""

    T_1000 = dt_time(10, 0)   # 开盘 30 分钟，量额折算系数 0.25（下限兜底）
    T_1400 = dt_time(14, 0)   # 已交易 180 分钟，量额折算系数 0.75

    def test_partial_amount_passes_intraday_only(self):
        # 盘中累计成交 6000 万：按全天标准（2亿）不达标，按 10:00 折算门槛（5000万）达标
        row = _valid_row(amount=6e7, turnover_rate=2.0)
        assert not passes_hard_filter(row)
        assert passes_hard_filter(row, intraday_time=self.T_1000)

    def test_amount_scale_grows_with_elapsed_time(self):
        # 同样 6000 万成交，14:00 触发时门槛已升到 1.5 亿（2亿×0.75），不再达标
        row = _valid_row(amount=6e7, turnover_rate=2.0)
        assert not passes_hard_filter(row, intraday_time=self.T_1400)
        # 1.6 亿成交在 14:00 达标
        assert passes_hard_filter(_valid_row(amount=1.6e8, turnover_rate=2.0), intraday_time=self.T_1400)

    def test_turnover_band_scaled_both_ends(self):
        # 10:00 换手 5%（暗示全天约 20%）超折算上限 3.75%，判定过热
        assert not passes_hard_filter(_valid_row(amount=6e7, turnover_rate=5.0), intraday_time=self.T_1000)
        # 10:00 换手 0.4%（暗示全天约 1.6%）低于折算下限 0.5%，热度不足
        assert not passes_hard_filter(_valid_row(amount=6e7, turnover_rate=0.4), intraday_time=self.T_1000)

    def test_volume_ratio_not_scaled(self):
        # 量比天生是盘中口径（当前每分钟量 vs 5日均），不折算：0.8 仍被拒
        row = _valid_row(amount=6e7, turnover_rate=2.0, volume_ratio=0.8)
        assert not passes_hard_filter(row, intraday_time=self.T_1000)

    def test_change_min_relaxed_in_opening_minutes(self):
        # 开盘 5 分钟（09:35）涨幅下限放宽到约 0.17%：涨 0.3% 可通过
        row = _valid_row(amount=6e7, turnover_rate=2.0, change_pct=0.3)
        assert passes_hard_filter(row, intraday_time=dt_time(9, 35))
        # 10:00 起恢复全额 1% 下限：涨 0.3% 被拒
        assert not passes_hard_filter(row, intraday_time=self.T_1000)


class TestIntradayElapsed:
    """已交易分钟数计算（用于门槛动态折算）。"""

    @pytest.mark.parametrize("t,expected", [
        (dt_time(9, 0), 0),      # 开盘前
        (dt_time(10, 0), 30),    # 上午开盘半小时
        (dt_time(11, 30), 120),  # 上午收盘
        (dt_time(12, 0), 120),   # 午休（保持上午累计）
        (dt_time(14, 0), 180),   # 下午 1 小时
        (dt_time(15, 30), 240),  # 收盘后（全天）
    ])
    def test_elapsed_minutes(self, t, expected):
        assert intraday_elapsed_minutes(t) == expected


class TestResolvePickMode:
    """荐股口径解析：市场阶段优先，日历不可用时按时钟兜底。"""

    NOON = dt_time(10, 0)

    @pytest.mark.parametrize("phase,expected", [
        ("premarket", MODE_PREMARKET),
        ("intraday", MODE_INTRADAY),
        ("lunch_break", MODE_INTRADAY),
        ("closing_auction", MODE_INTRADAY),
        ("postmarket", MODE_POSTMARKET),
    ])
    def test_phase_mapping(self, phase, expected):
        assert resolve_pick_mode(phase, self.NOON) == expected

    @pytest.mark.parametrize("t,expected", [
        (dt_time(9, 0), MODE_PREMARKET),    # 日历不可用 + 开盘前
        (dt_time(10, 0), MODE_INTRADAY),    # 日历不可用 + 盘中
        (dt_time(15, 30), MODE_POSTMARKET), # 日历不可用 + 收盘后
    ])
    def test_unknown_phase_falls_back_to_clock(self, t, expected):
        assert resolve_pick_mode("unknown", t) == expected


def _intraday_row(**overrides):
    """构造一条当日走势健康的合成快照记录（现价高于开盘、贴近当日高点、未低开）。"""
    row = {"price": 10.5, "open": 10.2, "high": 10.6, "pre_close": 10.0}
    row.update(overrides)
    return row


class TestIntradayHealth:
    """盘中当日分时走势健康筛选（核心安全闸，字段缺失即拒绝）。"""

    def test_healthy_row_passes(self):
        assert passes_intraday_health(_intraday_row())

    def test_below_open_fails(self):
        # 现价跌破今开（当日阴线），开盘后在走弱
        assert not passes_intraday_health(_intraday_row(price=10.1))

    def test_deep_pullback_from_high_fails(self):
        # 冲高 11.0 回落到 10.5，回撤 4.5% > 3% 上限
        assert not passes_intraday_health(_intraday_row(high=11.0))

    def test_gap_down_open_fails(self):
        # 今开 9.7 低于昨收 10.0 的 98%（大幅低开硬拉形态）
        assert not passes_intraday_health(_intraday_row(open=9.7, price=9.9, high=9.95))

    @pytest.mark.parametrize("missing", ["price", "open", "high", "pre_close"])
    def test_missing_any_field_rejected(self, missing):
        # 安全闸宁可错杀：任一字段缺失（停牌/脏数据）直接拒绝，不允许绕过健康检查
        assert not passes_intraday_health(_intraday_row(**{missing: None}))


class TestBuildCloseSeries:
    """收盘价序列构造：剔除当天半根K线、盘中追加现价。"""

    DATES = ["2026-07-01", "2026-07-02", "2026-07-03"]
    CLOSES = [10.0, 10.2, 10.1]

    def test_drops_today_partial_bar(self):
        # 日线最后一行是今天的实时半根K线 -> 剔除
        assert build_close_series(self.DATES, self.CLOSES, "2026-07-03") == [10.0, 10.2]

    def test_appends_current_price_intraday(self):
        series = build_close_series(self.DATES, self.CLOSES, "2026-07-03", current_price=10.5)
        assert series == [10.0, 10.2, 10.5]

    def test_datetime_dates_supported(self):
        # 部分数据源 date 列是 datetime 而非字符串，str() 前 10 位可比较
        from datetime import datetime as _dt
        dates = [_dt(2026, 7, 2), _dt(2026, 7, 3)]
        assert build_close_series(dates, [10.2, 10.1], "2026-07-03") == [10.2]

    def test_none_closes_skipped(self):
        assert build_close_series(self.DATES, [10.0, None, 10.1], "2026-07-04") == [10.0, 10.1]


class _StubConfig:
    """定价测试用的最小配置桩。"""
    swing_picks_take_profit_pct = 5.0
    swing_picks_stop_loss_pct = 3.0


class TestEntryPricing:
    """共享定价函数（盘中即时定价与 worker 开盘回填共用同一实现）。"""

    def test_entry_priced_and_opened(self):
        pick = SwingPick(code="600000", name="测试", pick_date="2026-07-06", ref_price=10.0)
        apply_entry_pricing(pick, 10.0, _StubConfig())
        assert pick.status == PICK_STATUS_OPEN
        assert pick.entry_price == 10.0
        assert pick.target_price == 10.5 and pick.stop_price == 9.7

    def test_invalid_price_raises(self):
        # 非正价格属调用方违约（硬筛保证现价 3-100 元），直接抛 ValueError 而非静默
        pick = SwingPick(code="600000", name="测试", pick_date="2026-07-06", ref_price=0.0)
        with pytest.raises(ValueError):
            apply_entry_pricing(pick, 0.0, _StubConfig())


class TestTechnicalFilter:
    """均线多头 + 短期未超涨。"""

    def test_bullish_alignment_passes(self):
        # 稳步上行序列：MA5 > MA10 > MA20 且近5日涨幅温和
        closes = [10 + i * 0.1 for i in range(25)]
        assert passes_technical_filter(closes)

    def test_bearish_alignment_fails(self):
        closes = [12 - i * 0.1 for i in range(25)]  # 持续下跌
        assert not passes_technical_filter(closes)

    def test_short_series_fails(self):
        assert not passes_technical_filter([10.0] * 10)

    def test_recent_surge_fails(self):
        # 前面横盘，最后5天暴涨40%（短期反转风险）
        closes = [10.0] * 20 + [11.0, 12.0, 13.0, 14.0, 14.0]
        assert not passes_technical_filter(closes)


class TestLlmResponseParsing:
    """LLM 精选回复解析容错。"""

    VALID = ["600000", "000001"]

    def test_clean_json(self):
        text = '[{"code": "600000", "reason": "主线强势"}]'
        assert parse_llm_pick_response(text, self.VALID) == [{"code": "600000", "reason": "主线强势"}]

    def test_json_wrapped_in_text(self):
        text = '好的，以下是精选：\n```json\n[{"code": "000001", "reason": "银行防守"}]\n```'
        result = parse_llm_pick_response(text, self.VALID)
        assert result and result[0]["code"] == "000001"

    def test_hallucinated_code_filtered(self):
        text = '[{"code": "999999", "reason": "不存在的股"}, {"code": "600000", "reason": "ok"}]'
        result = parse_llm_pick_response(text, self.VALID)
        assert [item["code"] for item in result] == ["600000"]

    def test_garbage_returns_empty(self):
        assert parse_llm_pick_response("今天不适合买股票。", self.VALID) == []
        assert parse_llm_pick_response("", self.VALID) == []


class TestEvaluatePosition:
    """持仓卖出事件状态机。"""

    TODAY = date(2026, 7, 3)
    NOON = dt_time(10, 30)

    def test_target_hit(self):
        assert evaluate_position(_pick(), 10.6, self.TODAY, self.NOON) == "target"

    def test_stop_hit(self):
        assert evaluate_position(_pick(), 9.6, self.TODAY, self.NOON) == "stop"

    def test_holding_continues(self):
        assert evaluate_position(_pick(), 10.1, self.TODAY, self.NOON) is None

    def test_expired_at_deadline_afternoon(self):
        deadline_day = date(2026, 7, 6)
        assert evaluate_position(_pick(), 10.1, deadline_day, dt_time(14, 45)) == "expired"

    def test_not_expired_before_force_time(self):
        deadline_day = date(2026, 7, 6)
        assert evaluate_position(_pick(), 10.1, deadline_day, dt_time(14, 0)) is None

    def test_expired_after_deadline_any_time(self):
        assert evaluate_position(_pick(), 10.1, date(2026, 7, 7), dt_time(9, 40)) == "expired"

    def test_target_beats_expired(self):
        # 到期日到价：优先按止盈处理
        deadline_day = date(2026, 7, 6)
        assert evaluate_position(_pick(), 10.6, deadline_day, dt_time(14, 50)) == "target"

    def test_pending_status_ignored(self):
        pick = _pick(status=PICK_STATUS_PENDING)
        assert evaluate_position(pick, 10.6, self.TODAY, self.NOON) is None

    def test_notification_text_contains_prices(self):
        text = build_sell_notification(_pick(), "target", 10.6)
        assert "10.5" in text and "600000" in text and "止盈" in text


class TestMorningTrigger:
    """盘前荐股触发条件与时段判断。"""

    def test_should_run_when_time_reached_and_not_picked(self):
        assert should_run_morning_pick(dt_time(9, 1), dt_time(9, 0), "2026-07-01", date(2026, 7, 2))

    def test_skip_when_already_picked_today(self):
        assert not should_run_morning_pick(dt_time(9, 1), dt_time(9, 0), "2026-07-02", date(2026, 7, 2))

    def test_skip_before_morning_time(self):
        assert not should_run_morning_pick(dt_time(8, 59), dt_time(9, 0), "", date(2026, 7, 2))

    def test_parse_morning_time_fallback(self):
        assert parse_morning_time("bad-value") == dt_time(9, 0)
        assert parse_morning_time("10:15") == dt_time(10, 15)

    @pytest.mark.parametrize("t,expected", [
        (dt_time(9, 29), False),
        (dt_time(9, 30), True),
        (dt_time(11, 30), True),
        (dt_time(12, 0), False),
        (dt_time(13, 0), True),
        (dt_time(15, 0), True),
        (dt_time(15, 1), False),
    ])
    def test_trading_session(self, t, expected):
        assert is_in_trading_session(t) is expected


class TestSnapshotNormalization:
    """快照 DataFrame -> 标准化字典列表，重点覆盖代码前缀归一化。"""

    def test_sina_code_prefix_stripped(self):
        # 新浪接口"代码"列实测带 sh/sz 交易所前缀，归一化前会导致板块前缀判断全部失败
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame([
            {"代码": "sh600000", "名称": "浦发银行", "最新价": 12.5, "涨跌幅": 3.2, "成交额": 5e8},
            {"代码": "sz000001", "名称": "平安银行", "最新价": 10.0, "涨跌幅": 1.5, "成交额": 4e8},
        ])
        rows = _normalize_snapshot_df(df, _SINA_SNAPSHOT_COLUMN_MAP)
        assert [r["code"] for r in rows] == ["600000", "000001"]
        assert rows[0]["price"] == 12.5 and rows[0]["change_pct"] == 3.2

    def test_already_bare_code_unaffected(self):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "最新价": 12.5, "涨跌幅": 3.2, "成交额": 5e8}])
        rows = _normalize_snapshot_df(df, _SINA_SNAPSHOT_COLUMN_MAP)
        assert rows[0]["code"] == "600000"


class TestStatePersistence:
    """状态文件读写与容错。"""

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SWING_PICKS_STATE_PATH", str(tmp_path / "positions.json"))
        state = picks_to_state({"last_pick_date": "2026-07-02"}, [_pick()])
        save_state(state)
        loaded = load_state()
        picks = picks_from_state(loaded)
        assert loaded["last_pick_date"] == "2026-07-02"
        assert len(picks) == 1 and picks[0].code == "600000"
        assert picks[0].status == PICK_STATUS_OPEN

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SWING_PICKS_STATE_PATH", str(tmp_path / "nope.json"))
        assert load_state() == {"last_pick_date": "", "picks": []}

    def test_corrupt_file_degrades_gracefully(self, tmp_path, monkeypatch):
        path = tmp_path / "positions.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("SWING_PICKS_STATE_PATH", str(path))
        assert load_state() == {"last_pick_date": "", "picks": []}
