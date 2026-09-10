"""Binance USDⓈ-M futures order placement/cancel paths (Task 4).

Tradable usdm profiles (paper/live) place and cancel orders against ccxt's
binanceusdm client with margin presets (set_margin_mode + set_leverage) applied
before create_order; the live-readonly Shadow profile stays strictly read-only.
Spot profiles reject futures-only parameters and otherwise keep their behavior.
"""

import pytest

from src.trading.connectors.binance import sdk as bn


def _cfg(**kw):
    base = {"profile": "paper", "market_type": "usdm", "api_key": "k", "api_secret": "s"}
    base.update(kw)
    return bn.BinanceConfig.from_mapping(base)


class FakeUsdmOrders:
    def __init__(self, positions=None, margin_error=None):
        self.calls = []
        self.positions = positions or []
        self.margin_error = margin_error

    def fetch_positions(self, symbols=None):
        self.calls.append(("fetch_positions", symbols))
        return self.positions

    def set_margin_mode(self, margin_mode, symbol=None, params=None):
        self.calls.append(("set_margin_mode", margin_mode, symbol))
        if self.margin_error is not None:
            raise self.margin_error

    def set_leverage(self, leverage, symbol=None, params=None):
        self.calls.append(("set_leverage", leverage, symbol))

    def create_order(self, symbol, type_, side, amount, price, params):
        self.calls.append(("create_order", symbol, type_, side, amount, params))
        return {"id": "o1", "symbol": symbol, "side": side, "type": type_,
                "status": "closed", "filled": amount, "amount": amount, "price": price}

    def cancel_order(self, order_id, symbol):
        self.calls.append(("cancel_order", order_id, symbol))
        return {"id": order_id, "symbol": symbol, "status": "canceled"}


@pytest.fixture
def fake(monkeypatch):
    ex = FakeUsdmOrders()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    return ex


def test_futures_place_sets_presets_then_creates(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy",
                         quantity=0.01, margin_mode="isolated", leverage=5)
    assert out["status"] == "ok"
    assert ("set_margin_mode", "isolated", "BTC/USDT:USDT") in fake.calls
    assert ("set_leverage", 5, "BTC/USDT:USDT") in fake.calls
    create = [c for c in fake.calls if c[0] == "create_order"]
    assert len(create) == 1
    sym, typ, side, amount, params = create[0][1:]
    assert sym == "BTC/USDT:USDT" and typ == "market" and side == "buy"


def test_futures_place_reduce_only(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="sell",
                         quantity=0.01, margin_mode="cross", leverage=3, reduce_only=True)
    assert out["status"] == "ok"
    create = [c for c in fake.calls if c[0] == "create_order"][0]
    assert create[5].get("reduceOnly") is True


def test_futures_place_requires_presets(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01)
    assert out["status"] == "error" and "margin_mode" in out["error"]


def test_futures_place_rejects_bad_margin_mode(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         margin_mode="hedge", leverage=5)
    assert out["status"] == "error"


def test_futures_place_rejects_sell_notional(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="sell", notional=100.0,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "quantity" in out["error"]


def test_shadow_cannot_place_or_cancel():
    cfg = _cfg(profile="live-readonly")
    out = bn.place_order(cfg, symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "read-only" in out["error"]
    out = bn.cancel_order(cfg, "o1", symbol="BTC/USDT:USDT")
    assert out["status"] == "error" and "read-only" in out["error"]


def test_spot_place_rejects_futures_params(monkeypatch):
    ex = FakeUsdmOrders()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    cfg = bn.BinanceConfig.from_mapping({"profile": "paper", "api_key": "k", "api_secret": "s"})
    out = bn.place_order(cfg, symbol="BTC/USDT", side="buy", quantity=0.01,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "futures-only" in out["error"]
    assert not ex.calls  # 未触达交易所


def test_spot_place_still_works_without_futures_params(monkeypatch):
    ex = FakeUsdmOrders()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    cfg = bn.BinanceConfig.from_mapping({"profile": "paper", "api_key": "k", "api_secret": "s"})
    out = bn.place_order(cfg, symbol="BTC/USDT", side="buy", quantity=0.01)
    assert out["status"] == "ok"


def test_futures_cancel_ok(fake):
    out = bn.cancel_order(_cfg(), "o1", symbol="BTC/USDT:USDT")
    assert out["status"] == "ok"
    assert ("cancel_order", "o1", "BTC/USDT:USDT") in fake.calls


def test_futures_place_skips_margin_set_when_position_matches(fake):
    fake.positions = [{"symbol": "BTC/USDT:USDT", "marginMode": "cross"}]
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         margin_mode="cross", leverage=5)
    assert out["status"] == "ok"
    assert not [c for c in fake.calls if c[0] == "set_margin_mode"]
    assert ("set_leverage", 5, "BTC/USDT:USDT") in fake.calls
    assert len([c for c in fake.calls if c[0] == "create_order"]) == 1


def test_futures_place_rejects_margin_mismatch_with_position(fake):
    fake.positions = [{"symbol": "BTC/USDT:USDT", "marginMode": "isolated"}]
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         margin_mode="cross", leverage=5)
    assert out["status"] == "error" and "isolated" in out["error"]
    assert not [c for c in fake.calls if c[0] == "create_order"]


def test_futures_place_tolerates_margin_type_already_set(monkeypatch):
    """-4046 ("No need to change margin type.") is a no-op, not a failure.

    Margin type persists per symbol on the account: the first order flips it and
    every later position-less order asking for the same type is answered with
    -4046. Failing there would refuse every order after the first one.
    """
    ex = FakeUsdmOrders(
        margin_error=Exception(
            'binanceusdm {"code":-4046,"msg":"No need to change margin type."}'
        )
    )
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "ok"
    assert ("set_leverage", 5, "BTC/USDT:USDT") in ex.calls
    assert len([c for c in ex.calls if c[0] == "create_order"]) == 1


def test_futures_place_tolerates_margin_type_already_set_via_code(monkeypatch):
    """ccxt sometimes carries the rejection on the code attribute, not the text."""
    err = Exception("binanceusdm rejected setMarginType")
    err.code = "-4046"
    ex = FakeUsdmOrders(margin_error=err)
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "ok"
    assert len([c for c in ex.calls if c[0] == "create_order"]) == 1


def test_futures_place_surfaces_other_margin_errors(monkeypatch):
    """Every other setMarginType failure still fails the order closed."""
    ex = FakeUsdmOrders(
        margin_error=Exception('binanceusdm {"code":-2015,"msg":"Invalid API-key"}')
    )
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "Invalid API-key" in out["error"]
    assert not [c for c in ex.calls if c[0] == "create_order"]
