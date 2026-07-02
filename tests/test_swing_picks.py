# -*- coding: utf-8 -*-
"""
短线荐股功能单元测试

覆盖纯逻辑部分（不依赖网络与真实行情）：
- 止盈/止损价计算与非法输入
- 最长持有截止交易日计算（跳过周末/节假日）
- 硬规则初筛（ST/板块前缀/字段缺失/区间边界）
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
    PICK_STATUS_OPEN,
    PICK_STATUS_PENDING,
    SwingPick,
    compute_deadline_date,
    compute_stop_price,
    compute_target_price,
    load_state,
    parse_llm_pick_response,
    passes_hard_filter,
    passes_technical_filter,
    picks_from_state,
    picks_to_state,
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
        {"price": None},              # 关键字段缺失
        {"change_60d": None},         # 60日涨幅缺失
    ])
    def test_rejections(self, overrides):
        assert not passes_hard_filter(_valid_row(**overrides))


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
