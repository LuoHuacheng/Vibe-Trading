"""离线回放脚本的纯函数测试（不触网）。

回放的口径一旦错了，A/B 结论就是错的，所以这里把出场模拟、回踩成交、5 分钟
聚合、入场点筛选全部钉在构造 K 线上。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "replay_futures_exits.py"
PROFILE = "binance-futures-paper-trade"


def _load():
    spec = importlib.util.spec_from_file_location("replay_futures_exits_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rp():
    return _load()


def _bar(minute: int, open_: float, high: float, low: float, close: float) -> list[float]:
    """第 minute 根 1 分钟 K 线。"""
    return [1_700_000_000_000 + minute * 60_000, open_, high, low, close, 1.0]


def test_simulate_stop_and_take_profit_hit(rp):
    """止损与止盈各自触发时，成交价就是那条腿的价。"""
    bars = [_bar(0, 100, 101, 99.5, 100), _bar(1, 100, 100.5, 97.0, 98.0)]
    out = rp.simulate_bracket(bars, 0, "long", 100.0, 1.0,
                              stop_pct=2.0, trailing_pct=0.0, take_profit_pct=8.0)
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == pytest.approx(98.0)
    assert out["mae_pct"] == pytest.approx(-3.0)

    bars = [_bar(0, 100, 101, 99.5, 100), _bar(1, 100, 108.0, 99.8, 107.0)]
    out = rp.simulate_bracket(bars, 0, "long", 100.0, 1.0,
                              stop_pct=2.0, trailing_pct=0.0, take_profit_pct=8.0)
    assert out["exit_reason"] == "take-profit"
    assert out["exit_price"] == pytest.approx(108.0)


def test_simulate_same_bar_takes_the_worse_leg(rp):
    """同一根里止损和止盈都碰到 → 按最差（止损）算，不进 lucky-path 幻想。"""
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 109.0, 97.0, 105.0)]
    out = rp.simulate_bracket(bars, 0, "long", 100.0, 1.0,
                              stop_pct=2.0, trailing_pct=0.0, take_profit_pct=8.0)
    assert out["exit_reason"] == "stop"


def test_simulate_trailing_and_timeout(rp):
    """移动止损按入场后极值回撤触发；一直不动则超时按最后一根收盘了结。"""
    bars = [_bar(0, 100, 101, 99, 100), _bar(1, 100, 111, 100, 110), _bar(2, 110, 110, 105.9, 106)]
    out = rp.simulate_bracket(bars, 0, "long", 100.0, 1.0,
                              stop_pct=10.0, trailing_pct=3.0, take_profit_pct=20.0)
    assert out["exit_reason"] == "trailing"
    assert out["exit_price"] == pytest.approx(111 * 0.97)

    flat = [_bar(i, 100, 100.2, 99.8, 100) for i in range(4)]
    out = rp.simulate_bracket(flat, 0, "long", 100.0, 1.0,
                              stop_pct=5.0, trailing_pct=3.0, take_profit_pct=8.0)
    assert out["exit_reason"] == "timeout"
    assert out["exit_price"] == pytest.approx(100.0)


def test_simulate_short_side_is_mirrored(rp):
    """空头：止损在上方、止盈在下方。"""
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 103.0, 99.0, 102.0)]
    out = rp.simulate_bracket(bars, 0, "short", 100.0, 1.0,
                              stop_pct=2.0, trailing_pct=0.0, take_profit_pct=8.0)
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == pytest.approx(102.0)


def test_fees_are_charged_on_both_sides(rp):
    """手续费按双边名义额收，净盈亏 = 毛利 - 双边费。"""
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 102, 99.9, 101)]
    out = rp.simulate_bracket(bars, 0, "long", 100.0, 10.0,
                              stop_pct=5.0, trailing_pct=0.0, take_profit_pct=2.0)
    assert out["exit_reason"] == "take-profit"
    assert out["gross"] == pytest.approx(20.0)
    assert out["fees"] == pytest.approx((100.0 + 102.0) * 10 * rp.FEE_PCT / 100)
    assert out["pnl"] == pytest.approx(out["gross"] - out["fees"])


def test_limit_entry_fills_on_pullback_or_skips(rp):
    """回踩：碰到限价才成交，成交价是限价（更好），碰不到这笔就不做。"""
    bracket = {"stop_pct": 2.0, "trailing_pct": 3.0, "take_profit_pct": 8.0}
    no_dip = [_bar(i, 100, 100.3, 99.95, 100.2) for i in range(6)]
    assert rp.simulate_limit_entry(no_dip, 0, "long", 100.0, 1.0,
                                  pullback_pct=0.5, expiry_bars=6, **bracket) is None

    dip = [_bar(0, 100, 100.3, 99.0, 100.2), _bar(1, 100, 101, 100, 101)]
    out = rp.simulate_limit_entry(dip, 0, "long", 100.0, 1.0,
                                  pullback_pct=0.5, expiry_bars=6, **bracket)
    assert out is not None

    # pullback_pct=0 = 就在参考价挂 post-only：碰到即成交，碰不到就不做
    touch = rp.simulate_limit_entry(dip, 0, "long", 100.0, 1.0,
                                    pullback_pct=0.0, expiry_bars=6, **bracket)
    assert touch is not None and touch["exit_price"] is not None
    never_touched = [_bar(i, 101.0, 100.5, 100.8, 100.9) for i in range(6)]
    assert rp.simulate_limit_entry(never_touched, 0, "long", 100.0, 1.0,
                                   pullback_pct=0.0, expiry_bars=6, **bracket) is None


def test_maker_entry_pays_less_on_the_entry_leg_only(rp):
    """maker 入场 + taker 出场：两条腿费率不同，必须分开算。"""
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 102, 99.9, 101)]
    taker = rp.simulate_bracket(bars, 0, "long", 100.0, 10.0,
                                stop_pct=5.0, trailing_pct=0.0, take_profit_pct=2.0)
    maker = rp.simulate_bracket(bars, 0, "long", 100.0, 10.0,
                                stop_pct=5.0, trailing_pct=0.0, take_profit_pct=2.0,
                                fee_pct=0.02, exit_fee_pct=0.04)
    assert maker["gross"] == pytest.approx(taker["gross"])
    assert maker["fees"] == pytest.approx(100.0 * 10 * 0.02 / 100 + 102.0 * 10 * 0.04 / 100)
    assert maker["fees"] < taker["fees"]


def test_bootstrap_mean_ci_flags_a_losing_subset(rp):
    """被闸掉的那批单均值显著为负时，区间整体应落在 0 以下。"""
    losing = rp.bootstrap_mean_ci([-2.0, -1.5, -2.5, -1.0, -3.0, -2.2])
    assert losing["mean"] < 0 and losing["p95"] < 0
    mixed = rp.bootstrap_mean_ci([1.0, -1.0])
    assert mixed["p05"] <= 0 <= mixed["p95"]
    assert rp.bootstrap_mean_ci([])["n"] == 0


def test_to_five_minute_aggregates_ohlcv(rp):
    """5 分钟聚合：开=首、高=最大、低=最小、收=末、量=求和。"""
    base = 1_700_000_000_000 - (1_700_000_000_000 % 300_000)
    bars = [[base + i * 60_000, 100 + i, 100 + i + 1, 100 + i - 1, 100 + i + 0.5, 2.0] for i in range(5)]
    out = rp.to_five_minute(bars)
    assert len(out) == 1
    row = out[0]
    assert row[0] == base
    assert row[1] == pytest.approx(100.0)
    assert row[2] == pytest.approx(105.0)
    assert row[3] == pytest.approx(99.0)
    assert row[4] == pytest.approx(104.5)
    assert row[5] == pytest.approx(10.0)


def test_load_entry_points_keeps_only_entries(rp, tmp_path):
    """入场点筛选：脚本侧平仓、别的 profile、缺数量都要剔除。"""
    def record(ts, side, reason, quantity, profile=PROFILE, status="order"):
        return json.dumps({"ts": ts, "symbol": "BTC/USDT:USDT", "side": side, "quantity": quantity,
                           "status": status, "reason": reason,
                           "result": {"status": "ok", "profile_id": profile}})

    path = tmp_path / "log.jsonl"
    path.write_text("\n".join([
        record("2026-09-11T00:00:00+00:00", "long", "breakout", 1.0),
        record("2026-09-11T01:00:00+00:00", "long", "stop-loss 95 <= 96 (entry 100)", 1.0),
        record("2026-09-11T02:00:00+00:00", "long", "breakout", 1.0, profile="binance-paper"),
        record("2026-09-11T03:00:00+00:00", "long", "breakout", None),
        json.dumps({"ts": "2026-09-11T04:00:00+00:00", "round": "idle", "detail": "x"}),
    ]), encoding="utf-8")

    entries = rp.load_entry_points(path, since_ms=0)
    assert [e["reason"] for e in entries] == ["breakout"]
    assert entries[0]["side"] == "long"


def test_summarize_metrics(rp):
    """汇总口径：胜率、均盈/均亏、每笔期望。"""
    results = [{"pnl": 4.0, "exit_reason": "take-profit", "held_bars": 10},
               {"pnl": -2.0, "exit_reason": "stop", "held_bars": 20},
               {"pnl": -2.0, "exit_reason": "stop", "held_bars": 30}]
    row = rp._summarize("v", results, attempted=4)
    assert row["filled"] == 3 and row["attempted"] == 4
    assert row["net"] == pytest.approx(0.0)
    assert row["win_rate"] == pytest.approx(100 / 3)
    assert row["avg_win"] == pytest.approx(4.0)
    assert row["avg_loss"] == pytest.approx(-2.0)
    assert row["expectancy"] == pytest.approx(0.0)
    assert row["stop_outs"] == 2 and row["median_hold"] == 20


def _sample(ts: int, price: float = 100.0) -> dict:
    """一笔可模拟的样本：横盘 1 分钟 K 线，永远不会触发任何腿。"""
    bars = [_bar(i, 100, 100.2, 99.8, 100) for i in range(30)]
    return {"symbol": "BTC/USDT:USDT", "side": "long", "ts": ts, "quantity": 1.0,
            "price": price, "bars": bars, "idx": 0, "atr": 0.5}


def test_split_half_keeps_every_sample_in_time_order(rp):
    """分段：按时间切成前后两半，两半合起来还是全部样本。"""
    samples = [_sample(ts) for ts in (3000, 1000, 2000, 4000)]
    first, second = rp._split_half(samples)
    assert [s["ts"] for s in first] == [1000, 2000]
    assert [s["ts"] for s in second] == [3000, 4000]


def test_paired_bootstrap_diff_is_zero_for_identical_rules(rp):
    """同一个止损距离配对相减：每笔差 0，区间必须含 0（=「分不出来」）。"""
    samples = [_sample(1000 + i) for i in range(8)]
    bracket = {"trailing_pct": 3.0, "take_profit_pct": 8.0, "max_hold_bars": 480}
    stats = rp.paired_bootstrap_diff(samples, bracket, lambda atr: 1.0, lambda atr: 1.0, trials=50)
    assert stats["mean_diff_per_trade"] == pytest.approx(0.0)
    assert stats["p05"] <= 0 <= stats["p95"]


def test_bootstrap_skip_control_is_reproducible_and_full_keep_matches(rp):
    """随机跳过对照：同种子可复现；全留时等于整批的净盈亏。"""
    samples = [_sample(1000 + i) for i in range(10)]
    bracket = {"trailing_pct": 3.0, "take_profit_pct": 8.0, "max_hold_bars": 480}
    full = rp.bootstrap_skip_control(samples, bracket, keep=10, trials=20)
    # 横盘不触发任何腿 → 只有双边手续费：每笔 -(100+100)*0.04% = -0.08，10 笔 = -0.8
    assert full["mean"] == pytest.approx(-0.8)

    first = rp.bootstrap_skip_control(samples, bracket, keep=3, trials=20)
    again = rp.bootstrap_skip_control(samples, bracket, keep=3, trials=20)
    assert first["mean"] == pytest.approx(again["mean"])
    assert first["p05"] <= first["mean"] <= first["p95"]
