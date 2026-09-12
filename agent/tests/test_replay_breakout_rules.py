"""机械突破规则回放的纯函数测试（不触网）。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "replay_breakout_rules.py"


def _load():
    spec = importlib.util.spec_from_file_location("replay_breakout_rules_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def bp():
    return _load()


def _bar(index: int, high: float, low: float, close: float, volume: float = 10.0) -> list[float]:
    """第 index 根 5 分钟 K 线。"""
    return [1_700_000_000_000 + index * 300_000, close, high, low, close, volume]


def _flat(count: int = 20) -> list[list[float]]:
    return [_bar(i, 100.0, 99.0, 99.5) for i in range(count)]


def test_breakout_long_is_detected_from_closed_bars(bp):
    """收盘破前 20 根高点且放量 → 多头信号，入场价=收盘价，模拟从下一根开始。"""
    bars = _flat() + [_bar(20, 101.5, 99.6, 101.0, volume=30.0), _bar(21, 101.6, 100.9, 101.2)]
    found = bp.find_entries(bars, "BTC/USDT:USDT", lookback=20, vol_ratio=1.5, min_gap_bars=12)
    assert len(found) == 1
    entry = found[0]
    assert entry["side"] == "long"
    assert entry["price"] == pytest.approx(101.0)
    assert entry["signal_bar"] == 20
    assert entry["idx"] == 21                      # 信号在收盘 → 下一根才可能触发保护腿
    assert entry["quantity"] == pytest.approx(bp.NOTIONAL / 101.0)


def test_breakdown_short_and_volume_filter(bp):
    """镜像的空头；量能不够则不出信号。"""
    bars = _flat() + [_bar(20, 99.4, 97.0, 97.5, volume=30.0), _bar(21, 98.0, 97.2, 97.8)]
    found = bp.find_entries(bars, "ETH/USDT:USDT", lookback=20, vol_ratio=1.5, min_gap_bars=12)
    assert [e["side"] for e in found] == ["short"]

    quiet = _flat() + [_bar(20, 101.5, 99.6, 101.0, volume=10.0), _bar(21, 101.6, 100.9, 101.2)]
    assert bp.find_entries(quiet, "ETH/USDT:USDT", lookback=20, vol_ratio=1.5, min_gap_bars=12) == []


def test_min_gap_suppresses_back_to_back_signals(bp):
    """同一品种连续两根都满足条件时，只留第一根（默认间隔 12 根）。"""
    bars = _flat() + [_bar(20, 101.5, 99.6, 101.0, volume=30.0),
                      _bar(21, 102.5, 100.6, 102.0, volume=30.0),
                      _bar(22, 102.6, 101.9, 102.2)]   # 最后一根不做信号（没有下一根可模拟）
    assert len(bp.find_entries(bars, "SOL/USDT:USDT", lookback=20, vol_ratio=1.5, min_gap_bars=12)) == 1
    assert len(bp.find_entries(bars, "SOL/USDT:USDT", lookback=20, vol_ratio=1.5, min_gap_bars=0)) == 2


def test_fetch_bars_caches_to_disk(bp, tmp_path):
    """同区间第二次取数必须命中缓存，不再打网络。"""
    calls = []

    class _Ex:
        def fetch_ohlcv(self, symbol, timeframe=None, limit=None, since=None):
            calls.append(since)
            return [[since, 1.0, 1.0, 1.0, 1.0, 1.0]]

    start, end = 1_700_000_000_000, 1_700_000_000_000 + 900_000
    first = bp.fetch_bars(_Ex(), "BTC/USDT:USDT", "5m", start, end, cache_dir=tmp_path)
    second = bp.fetch_bars(_Ex(), "BTC/USDT:USDT", "5m", start, end, cache_dir=tmp_path)
    assert first == second and len(first) == 3
    assert len(calls) == 3                       # 第二次没有新请求
    assert list(tmp_path.glob("*.json"))         # 缓存文件确实落盘


def test_fetch_bars_advances_even_on_a_stuck_page(bp, tmp_path):
    """交易所一直回同一根停在 since 上的 K 线时，分页也必须推进并退出。"""
    calls = []

    class _Stuck:
        def fetch_ohlcv(self, symbol, timeframe=None, limit=None, since=None):
            calls.append(since)
            return [[1_700_000_000_000, 1.0, 1.0, 1.0, 1.0, 1.0]]   # 永远是同一根

    start = 1_700_000_000_000
    bars = bp.fetch_bars(_Stuck(), "LTC/USDT:USDT", "5m", start, start + 1_500_000, cache_dir=tmp_path)
    assert len(calls) <= 6                       # 每页至少推进一根，不会退化成死循环
    assert len(bars) == 1                        # 越界/重复的行被去重


def test_fetch_bars_dedupes_and_sorts(bp, tmp_path):
    """重复/越界的行要去掉并按时间排序。"""
    class _Ex:
        def fetch_ohlcv(self, symbol, timeframe=None, limit=None, since=None):
            return [[since, 1.0, 1.0, 1.0, 1.0, 1.0], [since, 2.0, 2.0, 2.0, 2.0, 2.0]]

    start, end = 1_700_000_000_000, 1_700_000_000_000 + 300_000
    bars = bp.fetch_bars(_Ex(), "ETH/USDT:USDT", "5m", start, end, cache_dir=tmp_path)
    assert len(bars) == 1 and bars[0][0] == start
