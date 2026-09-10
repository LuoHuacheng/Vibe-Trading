"""Binance USD-M futures trade-read surface (Task 2).

Paper/live + usdm profiles read balance/positions/quote/open orders directly
through ccxt (fetch_balance / fetch_positions / fetch_ticker), while the
live-readonly Shadow profile keeps the strict signed observation path.
"""

import pytest

from src.trading.connectors.binance import sdk as bn


def _cfg(**kw):
    base = {"profile": "paper", "market_type": "usdm", "api_key": "k", "api_secret": "s"}
    base.update(kw)
    return bn.BinanceConfig.from_mapping(base)


class FakeUsdm:
    """双写记录型假交易所：调用全录进 self.calls。"""

    def __init__(self, balances=None, positions=None, ticker=None, open_orders=None):
        self.balances = balances or {"USDT": {"free": 1000.0, "used": 0.0, "total": 1000.0}}
        self.positions = positions or [{
            "symbol": "BTC/USDT:USDT", "contracts": 1.0, "side": "long",
            "entryPrice": 60000.0, "markPrice": 61000.0, "unrealizedPnl": 1000.0,
            "leverage": "5x", "marginMode": "isolated",
        }]
        self.ticker = ticker or {"symbol": "BTC/USDT:USDT", "last": 61000.0}
        self.open_orders = open_orders or []
        self.calls = []

    def fetch_balance(self):
        self.calls.append("fetch_balance")
        return self.balances

    def fetch_positions(self):
        self.calls.append("fetch_positions")
        return self.positions

    def fetch_ticker(self, symbol):
        self.calls.append(("fetch_ticker", symbol))
        return self.ticker

    def fetch_open_orders(self):
        self.calls.append("fetch_open_orders")
        return self.open_orders


def test_trade_account_snapshot_uses_direct_balance(monkeypatch):
    ex = FakeUsdm()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.get_account_snapshot(_cfg())
    assert out["status"] == "ok" and "fetch_balance" in ex.calls
    assert out["equity_usd"] == 1000.0
    assert [r["symbol"] for r in out["balances"]] == ["USDT"]
    assert out["market_type"] == "usdm"


def test_trade_positions_are_direct_rows(monkeypatch):
    ex = FakeUsdm()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.get_positions(_cfg())
    assert "fetch_positions" in ex.calls
    row = out["positions"][0]
    assert row["symbol"] == "BTC/USDT:USDT"
    assert row["quantity"] == 1.0 and row["side"] == "long"
    assert row["price"] == 61000.0
    # The cost basis travels with the row so downstream cost / P&L views are
    # not blank on a futures position.
    assert row["entry_price"] == 60000.0


def test_shadow_positions_still_use_observation(monkeypatch):
    calls = {}

    def fake_obs(cfg, ex):
        calls["obs"] = True
        return {"source": "binance-usdm", "status": "ok"}

    monkeypatch.setattr(bn, "read_account_observation", fake_obs)
    ex = FakeUsdm()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.get_positions(_cfg(profile="live-readonly"))
    assert calls.get("obs") is True and "fetch_positions" not in ex.calls


def test_quote_open_orders_open_for_trade_usdm(monkeypatch):
    ex = FakeUsdm()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    q = bn.get_quote("BTC/USDT:USDT", config=_cfg())
    assert q["status"] == "ok"
    assert (q.get("price") or q.get("last")) == 61000.0
    oo = bn.get_open_orders(_cfg())
    assert oo["status"] == "ok" and "open_orders" in oo


def test_shadow_quote_and_open_orders_rejected(monkeypatch):
    ex = FakeUsdm()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    with pytest.raises(bn.BinanceConfigError):
        bn.get_quote("BTC/USDT:USDT", config=_cfg(profile="live-readonly"))
    with pytest.raises(bn.BinanceConfigError):
        bn.get_open_orders(_cfg(profile="live-readonly"))


def test_normalize_futures_symbol():
    assert bn.normalize_futures_symbol("btc-usdt") == "BTC/USDT:USDT"
    assert bn.normalize_futures_symbol("BTC/USDT") == "BTC/USDT:USDT"
    assert bn.normalize_futures_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"
    assert bn.normalize_futures_symbol("BTC/USDC") is None
    assert bn.normalize_futures_symbol("") is None


def test_spot_reads_untouched(monkeypatch):
    ex = FakeUsdm()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    cfg = bn.BinanceConfig.from_mapping({"profile": "paper", "api_key": "k", "api_secret": "s"})
    bn.get_account_snapshot(cfg)
    bn.get_quote("BTC/USDT", config=cfg)
    assert ("fetch_ticker", "BTC/USDT") in ex.calls


def test_traded_stats_guard_usdm(monkeypatch):
    ex = FakeUsdm()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.get_traded_stats(_cfg(), assets=["BTC"])
    assert out == {"status": "ok", "assets": []}
    assert not ex.calls  # 完全不发 myTrades


# --- Derivatives context: one batch call for funding/basis, per-symbol OI ----


class FakeContextExchange:
    """premiumIndex 批量 + 逐品种 openInterest（含可注入的失败）。"""

    def __init__(self, funding=None, oi=None, oi_error=None, funding_error=None):
        self.funding = funding if funding is not None else [
            {"symbol": "BCHUSDT", "markPrice": "247.5", "indexPrice": "247.77",
             "lastFundingRate": "-0.00014348", "nextFundingTime": 1789056000000},
        ]
        self.oi = oi or {"BCHUSDT": "70934872027.7"}
        self.oi_error = oi_error
        self.funding_error = funding_error
        self.oi_calls: list[str] = []

    def fapiPublicGetPremiumIndex(self, params):
        if self.funding_error is not None:
            raise self.funding_error
        return self.funding

    def fetch_open_interest(self, symbol):
        self.oi_calls.append(symbol)
        if self.oi_error is not None:
            raise self.oi_error
        raw = symbol.split("/")[0] + "USDT"
        return {"symbol": symbol, "openInterestAmount": self.oi.get(raw)}


def test_futures_context_maps_funding_and_open_interest(monkeypatch):
    ex = FakeContextExchange()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.get_futures_context(_cfg(), ["BCH/USDT:USDT"])
    assert out["status"] == "ok"
    assert out["funding"]["BCH/USDT:USDT"]["funding_rate"] == -0.00014348
    assert out["funding"]["BCH/USDT:USDT"]["mark_price"] == 247.5
    assert out["open_interest"]["BCH/USDT:USDT"] == 70934872027.7
    assert out["open_interest_errors"] == 0
    assert ex.oi_calls == ["BCH/USDT:USDT"]


def test_futures_context_rejects_spot_and_shadow():
    spot = bn.BinanceConfig.from_mapping({"profile": "paper", "api_key": "k", "api_secret": "s"})
    assert "futures-only" in bn.get_futures_context(spot)["error"]
    assert "read-only" in bn.get_futures_context(_cfg(profile="live-readonly"))["error"]


def test_futures_context_counts_open_interest_failures(monkeypatch):
    ex = FakeContextExchange(oi_error=Exception("boom"))
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.get_futures_context(_cfg(), ["BCH/USDT:USDT"])
    assert out["status"] == "ok"
    assert out["open_interest"] == {} and out["open_interest_errors"] == 1


def test_futures_context_surfaces_the_batch_failure(monkeypatch):
    ex = FakeContextExchange(funding_error=Exception("boom"))
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.get_futures_context(_cfg())
    assert out["status"] == "error" and "boom" in out["error"]
