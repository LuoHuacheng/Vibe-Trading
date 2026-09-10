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


# --- Exchange-side conditional orders (stop / take-profit that rest on the
# --- exchange, so protection outlives the process that placed them).


def test_futures_place_stop_market_sends_stop_price_and_reduce_only(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="sell", quantity=0.01,
                         order_type="stop_market", stop_price=59000.0,
                         reduce_only=True, margin_mode="isolated", leverage=5)
    assert out["status"] == "ok"
    create = [c for c in fake.calls if c[0] == "create_order"][0]
    _symbol, type_, side, amount, params = create[1:]
    assert type_ == "STOP_MARKET"
    assert side == "sell" and amount == 0.01
    assert params["stopPrice"] == 59000.0
    assert params["reduceOnly"] is True


def test_futures_place_take_profit_market_uses_its_own_ccxt_type(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         order_type="take_profit_market", stop_price=81000.0,
                         reduce_only=True, margin_mode="isolated", leverage=5)
    assert out["status"] == "ok"
    create = [c for c in fake.calls if c[0] == "create_order"][0]
    assert create[2] == "TAKE_PROFIT_MARKET"
    assert create[5]["stopPrice"] == 81000.0


def test_conditional_orders_require_reduce_only(fake):
    """A non-reduce-only conditional could open a position nobody is watching."""
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="sell", quantity=0.01,
                         order_type="stop_market", stop_price=59000.0,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "reduce_only" in out["error"]
    assert not [c for c in fake.calls if c[0] == "create_order"]


def test_conditional_orders_require_a_positive_stop_price(fake):
    for bad in (None, 0, -1):
        out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="sell", quantity=0.01,
                             order_type="stop_market", stop_price=bad,
                             reduce_only=True, margin_mode="isolated", leverage=5)
        assert out["status"] == "error" and "stop_price" in out["error"], (bad, out)
    assert not [c for c in fake.calls if c[0] == "create_order"]


def test_conditional_orders_reject_notional(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", notional=100.0,
                         order_type="stop_market", stop_price=59000.0,
                         reduce_only=True, margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "quantity" in out["error"]
    assert not [c for c in fake.calls if c[0] == "create_order"]


def test_stop_price_is_rejected_on_plain_order_types(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         order_type="market", stop_price=59000.0,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "conditional" in out["error"]
    assert not [c for c in fake.calls if c[0] == "create_order"]


def test_unknown_order_type_is_rejected(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         order_type="trailing_stop", margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "order_type" in out["error"]


def test_futures_place_trailing_stop_sends_the_callback_rate(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         order_type="trailing_stop_market", callback_rate=3.0,
                         reduce_only=True, margin_mode="isolated", leverage=5)
    assert out["status"] == "ok"
    create = [c for c in fake.calls if c[0] == "create_order"][0]
    assert create[2] == "TRAILING_STOP_MARKET"
    assert create[5]["callbackRate"] == 3.0
    assert create[5]["reduceOnly"] is True
    assert "stopPrice" not in create[5]


def test_trailing_stop_requires_a_callback_rate_in_range(fake):
    for bad in (None, 0.0, 0.05, 5.5):
        out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                             order_type="trailing_stop_market", callback_rate=bad,
                             reduce_only=True, margin_mode="isolated", leverage=5)
        assert out["status"] == "error" and "callback_rate" in out["error"], (bad, out)
    assert not [c for c in fake.calls if c[0] == "create_order"]


def test_trailing_stop_rejects_a_stop_price_and_vice_versa(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         order_type="trailing_stop_market", callback_rate=2.0,
                         stop_price=59000.0, reduce_only=True,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "stop_price" in out["error"]
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="sell", quantity=0.01,
                         order_type="stop_market", stop_price=59000.0, callback_rate=2.0,
                         reduce_only=True, margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "callback_rate" in out["error"]
    assert not [c for c in fake.calls if c[0] == "create_order"]


def test_trailing_stop_still_requires_reduce_only(fake):
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         order_type="trailing_stop_market", callback_rate=3.0,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "error" and "reduce_only" in out["error"]


def test_spot_profile_rejects_stop_price(monkeypatch):
    ex = FakeUsdmOrders()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    cfg = bn.BinanceConfig.from_mapping({"profile": "paper", "api_key": "k", "api_secret": "s"})
    out = bn.place_order(cfg, symbol="BTC/USDT", side="sell", quantity=0.01,
                         order_type="stop_market", stop_price=59000.0, reduce_only=True)
    assert out["status"] == "error" and "futures-only" in out["error"]
    assert not ex.calls


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


# --- Conditional orders live in Binance's Algo service, not in the standard
# --- open-order endpoint, so they need their own read and cancel path.


class FakeAlgoExchange:
    """只实现 Algo 服务需要的两个原始接口。"""

    def __init__(self, rows=None, delete_error=None):
        self.rows = rows if rows is not None else []
        self.delete_error = delete_error
        self.deleted: list[dict] = []

    def fapiPrivateGetOpenAlgoOrders(self, params):
        return self.rows

    def fapiPrivateDeleteAlgoOrder(self, params):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(dict(params))
        return {"algoId": params.get("algoId"), "code": "200", "msg": "success"}


def _algo_cfg(**kw):
    return bn.BinanceConfig.from_mapping(
        {**{"profile": "paper", "market_type": "usdm", "api_key": "k", "api_secret": "s"}, **kw}
    )


def test_open_algo_orders_maps_and_normalizes_the_raw_rows(monkeypatch):
    ex = FakeAlgoExchange(rows=[
        {"algoId": 1000000200157472, "symbol": "BCHUSDT", "orderType": "TAKE_PROFIT_MARKET",
         "side": "BUY", "quantity": "0.806", "triggerPrice": "244.5", "algoStatus": "NEW"},
        {"algoId": "", "symbol": "BCHUSDT", "orderType": "STOP_MARKET"},  # 无 id → 丢弃
    ])
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.get_open_algo_orders(_algo_cfg())
    assert out["status"] == "ok" and out["count"] == 1
    row = out["orders"][0]
    assert row["order_id"] == "1000000200157472"
    assert row["symbol"] == "BCH/USDT:USDT"       # BCHUSDT → ccxt unified perp
    assert row["order_type"] == "take_profit_market"
    assert row["stop_price"] == 244.5
    assert row["quantity"] == 0.806
    assert row["status"] == "new"


def test_open_algo_orders_is_rejected_off_usdm():
    cfg = bn.BinanceConfig.from_mapping({"profile": "paper", "api_key": "k", "api_secret": "s"})
    out = bn.get_open_algo_orders(cfg)
    assert out["status"] == "error" and "futures-only" in out["error"]


def test_algo_helpers_reject_the_shadow_profile():
    cfg = _algo_cfg(profile="live-readonly")
    assert "read-only" in bn.get_open_algo_orders(cfg)["error"]
    assert "read-only" in bn.cancel_algo_order(cfg, "1")["error"]


def test_cancel_algo_order_uses_the_algo_endpoint(monkeypatch):
    ex = FakeAlgoExchange()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.cancel_algo_order(_algo_cfg(), "1000000200157380", symbol="BCH/USDT:USDT")
    assert out["status"] == "ok" and out["already_gone"] is False
    assert ex.deleted == [{"algoId": "1000000200157380"}]


def test_cancel_algo_order_is_idempotent_when_already_gone(monkeypatch):
    """-2013 means the order triggered or was cancelled: the desired state."""
    ex = FakeAlgoExchange(
        delete_error=Exception('binanceusdm {"code":-2013,"msg":"Order does not exist."}')
    )
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.cancel_algo_order(_algo_cfg(), "1000000200157380")
    assert out["status"] == "ok" and out["already_gone"] is True


def test_cancel_algo_order_surfaces_other_errors(monkeypatch):
    ex = FakeAlgoExchange(delete_error=Exception('binanceusdm {"code":-2015,"msg":"Invalid API-key"}'))
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.cancel_algo_order(_algo_cfg(), "1")
    assert out["status"] == "error" and "Invalid API-key" in out["error"]


class FakeRetryAlgoExchange(FakeAlgoExchange):
    """先失败 N 次（模拟 -1021 时钟漂移），之后成功。"""

    def __init__(self, fail_times=1):
        super().__init__()
        self.fail_times = fail_times
        self.calls = 0

    def fapiPrivateDeleteAlgoOrder(self, params):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise Exception(
                'binanceusdm {"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}'
            )
        self.deleted.append(dict(params))
        return {"algoId": params.get("algoId"), "code": "200", "msg": "success"}


def test_cancel_algo_order_retries_once_on_a_timestamp_rejection(monkeypatch):
    """-1021 is a clock-drift rejection, not a refusal: one retry lands."""
    ex = FakeRetryAlgoExchange(fail_times=1)
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.cancel_algo_order(_algo_cfg(), "42")
    assert out["status"] == "ok" and out["already_gone"] is False
    assert ex.calls == 2 and ex.deleted == [{"algoId": "42"}]


def test_cancel_algo_order_gives_up_after_the_retry(monkeypatch):
    ex = FakeRetryAlgoExchange(fail_times=2)
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.cancel_algo_order(_algo_cfg(), "42")
    assert out["status"] == "error" and "-1021" in out["error"]
    assert ex.calls == 2


def test_resting_orders_margin_rejection_names_the_cause(monkeypatch):
    """-4067 with no position means resting orders; the message must say so."""
    err = Exception(
        'binanceusdm {"code":-4067,"msg":"Position side cannot be changed if there exists open orders."}'
    )
    ex = FakeUsdmOrders(margin_error=err)
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.place_order(_cfg(), symbol="BTC/USDT:USDT", side="buy", quantity=0.01,
                         margin_mode="isolated", leverage=5)
    assert out["status"] == "error"
    assert "open orders" in out["error"] and "-4067" in out["error"]
    assert not [c for c in ex.calls if c[0] == "create_order"]


def test_cancel_algo_order_requires_an_id(monkeypatch):
    ex = FakeAlgoExchange()
    monkeypatch.setattr(bn, "_exchange", lambda cfg: ex)
    out = bn.cancel_algo_order(_algo_cfg(), "  ")
    assert out["status"] == "error" and "algo_id" in out["error"]
    assert ex.deleted == []
