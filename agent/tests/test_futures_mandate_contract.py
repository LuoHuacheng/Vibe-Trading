"""Futures mandate contract: perp symbols flow through the same mandate gate as
spot crypto, with the settlement suffix (e.g. ":USDT") stripped before any
data-loader call, and symbol-level gross exposure governing the leverage math.

Task 5 of the USDⓈ-M futures plan — loader-facing helpers never see a
":USDT"-suffixed symbol (the spot-proxy convention the crypto loader chain
speaks), while the gate contract (check_mandate) accepts a leveraged crypto
futures buy / denies beyond the configured caps unchanged.
"""

import pandas as pd
import pytest
from src.live import enforcement as enf
from src.live.mandate.model import (
    AssetClass, ConsentMeta, HardCaps, InstrumentType, Mandate, UniverseConstraint,
)

def _mandate(**kw):
    caps = kw.get("caps") or HardCaps(
        account_funding_usd=1000.0,
        max_order_notional_usd=5000.0,
        max_total_exposure_usd=20000.0,
        max_leverage=2.0,
        allowed_instruments=(InstrumentType.CRYPTO,),
        max_trades_per_day=10,
    )
    universe = kw.get("universe") or UniverseConstraint(
        asset_classes=(AssetClass.CRYPTO,),
        min_market_cap_usd=None,
        min_avg_daily_volume_usd=None,
        exclude_symbols=(),
    )
    consent = ConsentMeta(
        created_at="2026-09-09T00:00:00+00:00",
        consent_token_sha256="t" * 64,
        broker="binance",
        account_ref="acc-1",
        expires_at="2026-10-09T00:00:00+00:00",
    )
    return Mandate(schema_version=1, hard_caps=caps, universe=universe,
                   consent=consent, flatten_on_halt=False)

def _intent(**kw):
    base = dict(symbol="BTC/USDT:USDT", side="buy", notional_usd=1500.0,
                quantity=None, instrument_type=InstrumentType.CRYPTO,
                asset_class=AssetClass.CRYPTO, limit_price=None)
    base.update(kw)
    return enf.OrderIntent(**base)

class FakeLoader:
    name = "fake"
    def __init__(self):
        self.symbols = []
    def fetch(self, symbols, start, end, interval):
        self.symbols.extend(symbols)
        out = {}
        for sym in symbols:
            out[sym] = pd.DataFrame({"close": [61000.0], "volume": [1000.0]})
        return out

def test_avg_daily_dollar_volume_strips_settlement_suffix(monkeypatch):
    loader = FakeLoader()
    monkeypatch.setattr(enf, "_resolve_loader", lambda asset_class: loader)
    adv = enf.avg_daily_dollar_volume("BTC/USDT:USDT", AssetClass.CRYPTO)
    assert loader.symbols == ["BTC/USDT"]
    assert adv == 61_000_000.0

def test_last_price_usd_strips_settlement_suffix(monkeypatch):
    loader = FakeLoader()
    monkeypatch.setattr(enf, "_resolve_loader", lambda asset_class: loader)
    price = enf.last_price_usd("BTC/USDT:USDT", AssetClass.CRYPTO)
    assert loader.symbols == ["BTC/USDT"]
    assert price == 61000.0

def test_spot_symbol_loader_path_unchanged(monkeypatch):
    loader = FakeLoader()
    monkeypatch.setattr(enf, "_resolve_loader", lambda asset_class: loader)
    enf.avg_daily_dollar_volume("BTC/USDT", AssetClass.CRYPTO)
    assert loader.symbols == ["BTC/USDT"]

def test_post_trade_gross_exposure_long_and_short_rows():
    rows = [
        {"symbol": "BTC/USDT:USDT", "quantity": 1.0, "side": "long", "price": 61000.0},
        {"symbol": "ETH/USDT:USDT", "quantity": -2.0, "side": "short", "price": 3000.0},
    ]
    total = enf._post_trade_gross_exposure(rows, symbol="BTC/USDT:USDT", signed_order_notional=61000.0)
    assert total == 61000.0 * 2 + 2.0 * 3000.0

def test_post_trade_gross_exposure_sell_reduces_long_only():
    rows = [{"symbol": "BTC/USDT:USDT", "quantity": 1.0, "side": "long", "price": 61000.0}]
    # 卖出减多：signed 负数 61000 → 该 symbol 归零，不产生负敞口
    total = enf._post_trade_gross_exposure(rows, symbol="BTC/USDT:USDT", signed_order_notional=-61000.0)
    assert total == 0.0

def test_check_mandate_allows_crypto_futures_buy():
    assert enf.check_mandate(_mandate(), _intent(), positions=[], balance=None,
                             broker="binance", remote_tool="place_order",
                             daily_count=0) is None

def test_check_mandate_denies_beyond_leverage():
    caps = HardCaps(account_funding_usd=1000.0, max_order_notional_usd=5000.0,
                    max_total_exposure_usd=20000.0, max_leverage=2.0,
                    allowed_instruments=(InstrumentType.CRYPTO,), max_trades_per_day=10)
    breach = enf.check_mandate(_mandate(caps=caps), _intent(notional_usd=3000.0),
                               positions=[], balance=None, broker="binance",
                               remote_tool="place_order", daily_count=0)
    assert breach is not None and breach.limit == "max_leverage"

def test_check_mandate_denies_futures_symbol_on_exclude_list():
    universe = UniverseConstraint(asset_classes=(AssetClass.CRYPTO,),
                                  min_market_cap_usd=None, min_avg_daily_volume_usd=None,
                                  exclude_symbols=("BTC/USDT:USDT",))
    breach = enf.check_mandate(_mandate(universe=universe), _intent(),
                               positions=[], balance=None, broker="binance",
                               remote_tool="place_order", daily_count=0)
    assert breach is not None and breach.limit == "exclude_symbols"
