"""Service <-> SDK / mandate-gate wiring for Binance USD-M futures orders (Task 6).

service.place_order (Task 4) already accepts margin_mode / leverage /
reduce_only and only forwards each key into the SDK place_kwargs when it
was supplied (reduce_only only when True). These tests lock the two routing
contracts that the tool schema (Task 6) relies on:

  * paper futures profile  -> direct SDK place_order(config, **place_kwargs)
    receives the usdm config plus margin_mode/leverage (and reduce_only only
    when explicitly True);
  * live futures profile   -> src.live.sdk_order_gate.execute_live_order
    receives an OrderIntent typed CRYPTO/CRYPTO with an UPPERCASE symbol and
    place_kwargs carrying the margin parameters;
  * spot profile           -> keeps its old direct path with NO futures keys.

execute_live_order is imported INSIDE service.place_order's live branch
(from src.live.sdk_order_gate import execute_live_order, executed per call),
so the monkeypatch target is the module attribute
src.live.sdk_order_gate.execute_live_order -- not a service-level name.

The paper/spot tests stub service._sdk_module because the service builds the
connector config through module.build_config before branching (no real
Binance SDK / credentials / network involved).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import src.live.sdk_order_gate as gate
import src.trading.service as trading
from src.live.mandate.model import AssetClass, InstrumentType
from src.tools.trading_connector_tool import (
    TradingCheckTool,
    TradingPlaceOrderTool,
)


class StubModule:
    """Capture config + place_kwargs and return a controlled envelope.

    service.place_order calls module.build_config(profile.config, ...)
    before branching, so the stub resolves the profile config into an object
    carrying market_type (default "spot", mirroring the real SDK).
    """

    def __init__(self) -> None:
        self.captured = []

    def build_config(self, profile_config, overrides=None):
        payload = dict(profile_config or {})
        payload.update(overrides or {})
        payload.setdefault("market_type", "spot")
        return SimpleNamespace(**payload)

    def place_order(self, config, **kwargs):
        self.captured.append((config, kwargs))
        return {"status": "ok", "order_id": "x1", "symbol": kwargs["symbol"]}


# --------------------------------------------------------------------------- #
# Service routing: paper futures -> direct SDK with config + margin params      #
# --------------------------------------------------------------------------- #


def test_paper_futures_place_forwards_config_and_params(monkeypatch):
    """Paper futures order forwards the usdm config and margin_mode/leverage.

    reduce_only defaults to False and -- unlike the plan's draft, which predates
    Task 4's implementation -- the service omits the key entirely instead of
    sending reduce_only=False.
    """
    stub = StubModule()
    monkeypatch.setattr(trading, "_sdk_module", lambda connector: stub)
    out = trading.place_order(
        "BTC/USDT:USDT", profile_id="binance-futures-paper-trade",
        side="buy", quantity=0.01, margin_mode="isolated", leverage=5,
    )
    assert out["status"] == "ok"
    config, kwargs = stub.captured[0]
    assert config.market_type == "usdm"
    assert kwargs["margin_mode"] == "isolated"
    assert kwargs["leverage"] == 5
    assert "reduce_only" not in kwargs  # 默认不传：place_kwargs 不含 reduce_only


def test_paper_futures_place_forwards_reduce_only_when_true(monkeypatch):
    """An explicit reduce_only=True reaches the SDK as reduce_only=True."""
    stub = StubModule()
    monkeypatch.setattr(trading, "_sdk_module", lambda connector: stub)
    out = trading.place_order(
        "BTC/USDT:USDT", profile_id="binance-futures-paper-trade",
        side="sell", quantity=0.01, margin_mode="cross", leverage=3,
        reduce_only=True,
    )
    assert out["status"] == "ok"
    config, kwargs = stub.captured[0]
    assert kwargs["reduce_only"] is True


# --------------------------------------------------------------------------- #
# Service routing: live futures -> mandate gate (execute_live_order)            #
# --------------------------------------------------------------------------- #


def test_live_futures_place_routes_through_mandate_gate(monkeypatch):
    """Live futures order goes through the gate with a CRYPTO intent, UPPERCASE
    symbol and margin parameters inside place_kwargs.

    The gate itself is stubbed at src.live.sdk_order_gate.execute_live_order
    (the module attribute the service's in-function from-import reads at call
    time), so no mandate / quote / network code runs.
    """
    captured = {}

    def fake_gate(**kwargs):
        captured.update(kwargs)
        return {"status": "blocked", "decision": "deny", "reason": "no mandate"}

    monkeypatch.setattr(gate, "execute_live_order", fake_gate)
    out = trading.place_order(
        "btc/usdt:usdt", profile_id="binance-futures-live-trade",
        side="buy", quantity=0.01, margin_mode="cross", leverage=2,
    )
    assert out["status"] == "blocked"
    intent = captured["intent"]
    assert intent.instrument_type == InstrumentType.CRYPTO
    assert intent.asset_class == AssetClass.CRYPTO
    assert intent.symbol == "BTC/USDT:USDT"  # 服务层强制大写
    assert captured["broker"] == "binance"
    assert captured["place_kwargs"]["margin_mode"] == "cross"
    assert captured["place_kwargs"]["leverage"] == 2


# --------------------------------------------------------------------------- #
# Service routing: spot keeps its old direct path without futures params        #
# --------------------------------------------------------------------------- #


def test_spot_place_still_routes_without_futures_params(monkeypatch):
    """A plain spot order stays on the direct path and never carries futures keys."""
    stub = StubModule()
    monkeypatch.setattr(trading, "_sdk_module", lambda connector: stub)
    out = trading.place_order("BTC/USDT", profile_id="binance-paper-trade",
                              side="buy", quantity=0.01)
    assert out["status"] == "ok"
    config, kwargs = stub.captured[0]
    assert config.market_type == "spot"
    assert "margin_mode" not in kwargs
    assert "leverage" not in kwargs
    assert "reduce_only" not in kwargs


# --------------------------------------------------------------------------- #
# Tool schema: trading_place_order advertises the three USD-M futures params    #
# --------------------------------------------------------------------------- #


def test_place_order_tool_exposes_futures_params():
    """The place-order tool schema carries margin_mode / leverage / reduce_only."""
    props = TradingPlaceOrderTool.parameters["properties"]
    assert props["margin_mode"]["type"] == "string"
    assert props["margin_mode"]["enum"] == ["isolated", "cross"]
    assert "futures" in props["margin_mode"]["description"].lower()
    assert props["leverage"]["type"] == "integer"
    assert props["leverage"]["minimum"] == 1
    assert props["leverage"]["maximum"] == 125
    assert "futures" in props["leverage"]["description"].lower()
    assert props["reduce_only"]["type"] == "boolean"
    assert "futures" in props["reduce_only"]["description"].lower()


def test_futures_params_are_place_order_only_not_common():
    """The three futures keys must not leak into every read tool's schema."""
    assert "margin_mode" not in TradingCheckTool.parameters["properties"]
    assert "leverage" not in TradingCheckTool.parameters["properties"]
    assert "reduce_only" not in TradingCheckTool.parameters["properties"]


def test_place_order_tool_market_type_description_mentions_futures():
    """market_type help text covers the tradable USD-M futures profiles."""
    from src.tools.trading_connector_tool import TRADING_COMMON_PARAMETERS

    desc = TRADING_COMMON_PARAMETERS["market_type"]["description"].lower()
    assert "usdm" in desc and "futures" in desc


# --------------------------------------------------------------------------- #
# Tool forwarding: the three kwargs reach trading.place_order unchanged        #
# --------------------------------------------------------------------------- #


def test_place_order_tool_forwards_futures_params(monkeypatch):
    """margin_mode / leverage / reduce_only travel from tool kwargs to
    trading.place_order; a malformed leverage fails closed before the call.
    """
    seen = {}

    def fake_place_order(symbol, connection, **kwargs):
        seen.update({"symbol": symbol, "connection": connection, **kwargs})
        return {"status": "ok"}

    monkeypatch.setattr("src.tools.trading_connector_tool.place_order", fake_place_order)

    payload = json.loads(
        TradingPlaceOrderTool().execute(
            symbol="BTC/USDT:USDT",
            connection="binance-futures-paper-trade",
            side="buy",
            quantity=0.01,
            margin_mode="isolated",
            leverage=5,
            reduce_only=True,
        )
    )
    assert payload["status"] == "ok"
    assert seen["margin_mode"] == "isolated"
    assert seen["leverage"] == 5
    assert seen["reduce_only"] is True


def test_place_order_tool_rejects_malformed_leverage(monkeypatch):
    """A non-integer leverage aborts with an error envelope before the service."""
    calls = []

    def fail_place_order(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("place_order must not run for a malformed leverage")

    monkeypatch.setattr("src.tools.trading_connector_tool.place_order", fail_place_order)

    payload = json.loads(
        TradingPlaceOrderTool().execute(
            symbol="BTC/USDT:USDT",
            connection="binance-futures-paper-trade",
            side="buy",
            quantity=0.01,
            margin_mode="isolated",
            leverage="5x",
        )
    )
    assert payload["status"] == "error"
    assert payload["error"]
    assert calls == []
