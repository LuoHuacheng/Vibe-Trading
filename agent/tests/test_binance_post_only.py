"""Binance 下单接口的 maker（post-only）参数测试：不触网，用假 exchange 断言参数。

费率差是实打实的（本账户 maker 0.02% / taker 0.04%），但参数没送到 ccxt 就等于
没生效 —— 这组测试专门钉住「参数真的进了 create_order 的 params」和「用错场景
要被拒绝而不是静默忽略」。
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def sdk():
    return importlib.import_module("src.trading.connectors.binance.sdk")


class _FakeExchange:
    """只实现 place_order 会用到的三个方法，并记下 create_order 收到的全部参数。"""

    def __init__(self):
        self.calls: list[dict] = []

    def set_leverage(self, leverage, symbol):
        return None

    def set_margin_mode(self, mode, symbol):
        return None

    def create_order(self, symbol, order_type, side, amount, price, params):
        self.calls.append({"symbol": symbol, "type": order_type, "side": side,
                           "amount": amount, "price": price, "params": dict(params)})
        return {"id": "1", "symbol": symbol, "side": side, "type": order_type,
                "status": "open", "filled": 0.0, "amount": amount, "price": price}


@pytest.fixture()
def wired(sdk, monkeypatch):
    exchange = _FakeExchange()
    monkeypatch.setattr(sdk, "_exchange", lambda cfg: exchange)
    monkeypatch.setattr(sdk, "_assert_host", lambda cfg: None)
    monkeypatch.setattr(sdk, "_ensure_futures_margin", lambda ex, symbol, mode: None)
    cfg = sdk.BinanceConfig.from_mapping({"profile": "paper", "market_type": "usdm",
                                          "api_key": "k", "api_secret": "s"})
    return cfg, exchange


def test_usdm_post_only_limit_reaches_ccxt_params(sdk, wired):
    """post-only 限价单：params 里必须同时有 postOnly 和 GTC。"""
    cfg, exchange = wired
    out = sdk.place_order(cfg, symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                          order_type="limit", limit_price=70000.0, post_only=True,
                          margin_mode="isolated", leverage=5)
    assert out["status"] == "ok", out
    # GTX = Binance 的 post-only：实测传 postOnly 布尔会被忽略、以 taker 成交
    assert exchange.calls[0]["params"] == {"timeInForce": "GTX"}
    assert exchange.calls[0]["type"] == "limit"
    assert exchange.calls[0]["price"] == 70000.0


def test_usdm_post_only_is_refused_on_market_orders(sdk, wired):
    """市价单 + post-only 必须被拒，不能静默忽略（否则照样吃 taker 费）。"""
    cfg, exchange = wired
    out = sdk.place_order(cfg, symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                          order_type="market", post_only=True, margin_mode="isolated", leverage=5)
    assert out["status"] == "error"
    assert "post_only" in out["error"]
    assert exchange.calls == []                    # 一个单都不能发出去


def test_usdm_limit_without_post_only_stays_plain(sdk, wired):
    """不传 post_only 时不许多出 postOnly，老行为不能被改。"""
    cfg, exchange = wired
    out = sdk.place_order(cfg, symbol="BTC/USDT:USDT", side="sell", quantity=0.01,
                          order_type="limit", limit_price=70000.0,
                          margin_mode="isolated", leverage=5)
    assert out["status"] == "ok", out
    assert "postOnly" not in exchange.calls[0]["params"]


def test_get_order_reports_fill_state(sdk, monkeypatch):
    """get_order：maker 入场要靠它区分「成交」和「还挂着」。"""

    class _Ex:
        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "symbol": symbol, "status": "closed", "filled": 0.5,
                    "amount": 0.5, "average": 70000.0, "price": None}

    monkeypatch.setattr(sdk, "_exchange", lambda cfg: _Ex())
    monkeypatch.setattr(sdk, "_assert_host", lambda cfg: None)
    cfg = sdk.BinanceConfig.from_mapping({"profile": "paper", "market_type": "usdm",
                                          "api_key": "k", "api_secret": "s"})
    out = sdk.get_order(cfg, "42", symbol="BTC/USDT:USDT")
    assert out["status"] == "ok"
    assert out["order_status"] == "closed"
    assert out["filled"] == pytest.approx(0.5)
    assert out["average"] == pytest.approx(70000.0)


def test_get_order_requires_symbol(sdk):
    """Binance 读单也要 symbol，缺了就报错而不是猜。"""
    out = sdk.get_order(None, "42")
    assert out["status"] == "error"
