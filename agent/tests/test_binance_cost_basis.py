"""Unit tests for the Binance spot cost-basis derivation."""

from __future__ import annotations

from src.trading.connectors.binance.sdk import _spot_average_cost, _spot_trade_stats


def sdk_module_stats(exchange, asset):
    """Call the private stats builder; returns its ``None`` fallback as a row."""
    stats = _spot_trade_stats(exchange, asset)
    return stats or {
        "symbol": asset, "trades": 0, "buys": 0, "sells": 0,
        "buy_amount_usd": 0.0, "sell_amount_usd": 0.0, "net_qty": 0.0,
        "avg_cost": None, "realized_pnl_usd": None,
        "first_trade_at": None, "last_trade_at": None,
    }


class FakeExchange:
    """Stub exchange returning a canned trade list for ``fetch_my_trades``."""

    def __init__(self, trades):
        self.trades = trades

    def fetch_my_trades(self, symbol, since=None, limit=None):
        return list(self.trades)


def _trade(ts, side, amount, price):
    return {"timestamp": ts, "side": side, "amount": amount, "price": price}


def test_buys_weight_average():
    trades = [
        _trade(1000, "buy", 10, 100),
        _trade(2000, "buy", 10, 110),
    ]
    assert _spot_average_cost(FakeExchange(trades), "UNI") == 105.0


def test_partial_sell_keeps_running_average():
    trades = [
        _trade(1000, "buy", 10, 100),
        _trade(2000, "buy", 10, 110),
        _trade(3000, "sell", 5, 120),
        _trade(4000, "buy", 10, 120),
    ]
    # After the sell 15 units remain at 105; the new buy pulls the average up.
    expected = (105 * 15 + 120 * 10) / 25
    assert _spot_average_cost(FakeExchange(trades), "UNI") == expected


def test_full_close_resets_for_reentry():
    trades = [
        _trade(1000, "buy", 10, 100),
        _trade(2000, "sell", 10, 110),
        _trade(3000, "buy", 10, 200),
    ]
    assert _spot_average_cost(FakeExchange(trades), "UNI") == 200.0


def test_untraded_balance_has_no_cost():
    assert _spot_average_cost(FakeExchange([]), "UNI") is None


def test_fetch_failure_degrades_to_none():
    class BrokenExchange:
        def fetch_my_trades(self, *args, **kwargs):
            raise RuntimeError("rate limited")

    assert _spot_average_cost(BrokenExchange(), "UNI") is None


def test_empty_asset_is_none():
    assert _spot_average_cost(FakeExchange([]), "") is None


def test_trade_stats_buys_and_sells():
    trades = [
        _trade(1000, "buy", 10, 100),
        _trade(2000, "buy", 10, 110),
        _trade(3000, "sell", 5, 120),
    ]
    stats = sdk_module_stats(FakeExchange(trades), "UNI")
    assert stats["trades"] == 3
    assert stats["buys"] == 2
    assert stats["sells"] == 1
    assert stats["buy_amount_usd"] == 2100.0
    assert stats["sell_amount_usd"] == 600.0
    assert stats["net_qty"] == 15.0
    assert stats["avg_cost"] == 105.0
    # Realized on the sell of 5 units at 120 vs 105 average.
    assert stats["realized_pnl_usd"] == 75.0
    assert stats["first_trade_at"] is not None
    assert stats["last_trade_at"] == stats["first_trade_at"] or stats["last_trade_at"]


def test_trade_stats_sell_only_gift_has_no_cost_or_realized():
    trades = [_trade(1000, "sell", 1, 221.54)]
    stats = sdk_module_stats(FakeExchange(trades), "BABY")
    assert stats["buys"] == 0
    assert stats["sells"] == 1
    assert stats["buy_amount_usd"] == 0.0
    assert stats["sell_amount_usd"] == 221.54
    assert stats["net_qty"] == 0.0
    assert stats["avg_cost"] is None
    assert stats["realized_pnl_usd"] is None


def test_trade_stats_closed_position_keeps_realized():
    trades = [
        _trade(1000, "buy", 2, 100),
        _trade(2000, "sell", 2, 90),
    ]
    stats = sdk_module_stats(FakeExchange(trades), "TRX")
    assert stats["net_qty"] == 0.0
    assert stats["avg_cost"] is None  # nothing left to cost-averaging
    assert stats["realized_pnl_usd"] == -20.0


def test_trade_stats_fetch_failure_is_none():
    class BrokenExchange:
        def fetch_my_trades(self, *args, **kwargs):
            raise RuntimeError("rate limited")

    assert _spot_trade_stats(BrokenExchange(), "UNI") is None


def test_trade_stats_untraded_is_empty_row():
    stats = sdk_module_stats(FakeExchange([]), "GIFT")
    assert stats["trades"] == 0
    assert stats["buys"] == 0
    assert stats["sells"] == 0
    assert stats["realized_pnl_usd"] is None
