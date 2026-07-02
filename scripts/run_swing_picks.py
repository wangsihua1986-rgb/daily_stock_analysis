# -*- coding: utf-8 -*-
"""
短线荐股手动触发脚本

用法（在项目根目录执行）：
    python scripts/run_swing_picks.py                # 立即生成今日荐股并推送
    python scripts/run_swing_picks.py --dry-run      # 只生成不推送（试跑）
    python scripts/run_swing_picks.py --monitor-once # 手动执行一轮持仓监控
    python scripts/run_swing_picks.py --status       # 查看当前持仓状态

说明：常驻监控由 Web/API 服务进程内的后台 worker 负责（SWING_PICKS_ENABLED=true），
本脚本用于手动补跑荐股、临时检查持仓，不长驻运行。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 保证从项目根目录导入 src 包（脚本可能从任意目录执行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    """脚本入口：解析参数并执行对应动作，返回进程退出码（0=成功）。"""
    parser = argparse.ArgumentParser(description="短线荐股手动触发")
    parser.add_argument("--dry-run", action="store_true", help="只生成荐股，不推送通知")
    parser.add_argument("--monitor-once", action="store_true", help="手动执行一轮持仓监控")
    parser.add_argument("--status", action="store_true", help="查看当前持仓状态")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from src.config import get_config
    from src.services.swing_picks_service import generate_daily_picks, load_state

    if args.status:
        state = load_state()
        print(f"上次荐股日期: {state.get('last_pick_date') or '（无）'}")
        for item in state.get("picks", []):
            print(
                f"{item.get('pick_date')} {item.get('code')} {item.get('name')} "
                f"状态={item.get('status')} 买入={item.get('entry_price')} "
                f"止盈={item.get('target_price')} 止损={item.get('stop_price')} "
                f"截止={item.get('deadline_date')} 卖出={item.get('exit_price')}"
            )
        return 0

    if args.monitor_once:
        from src.services.swing_picks_worker import SwingPicksWorker

        stats = SwingPicksWorker().run_once()
        print(f"监控完成: {stats}")
        return 0

    config = get_config()
    picks = generate_daily_picks(config, notify=not args.dry_run)
    if not picks:
        print("今日未生成荐股（非交易日 / 数据不可用 / 无候选达标，详见日志）")
        return 1
    for p in picks:
        print(f"{p.code} {p.name} 参考价={p.ref_price} 截止={p.deadline_date} 理由={p.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
