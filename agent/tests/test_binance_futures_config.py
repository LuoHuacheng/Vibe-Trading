"""Binance USD-M futures connector configuration surface (Task 1).

Covers the config-layer contract for treating market_type="usdm" as either a
Shadow observation (profile live-readonly) or a tradable surface (profiles
paper/live): profile admission, futures testnet host selection,
Shadow/trade discrimination helpers, and host assertion.
"""

import pytest

from src.trading.connectors.binance import sdk as bn


def _cfg(**kw):
    base = {"profile": "paper", "market_type": "usdm", "api_key": "k", "api_secret": "s"}
    base.update(kw)
    return bn.BinanceConfig.from_mapping(base)


def test_usdm_allowed_on_paper_and_live():
    assert _cfg().profile == "paper"
    assert _cfg(profile="live").profile == "live"


def test_usdm_rejected_on_unknown_profile():
    with pytest.raises(bn.BinanceConfigError):
        _cfg(profile="nope")


def test_usdm_paper_host_is_futures_testnet():
    assert _cfg().is_testnet
    assert _cfg().host == bn.USDM_TESTNET_HOST
    assert "testnet.binancefuture.com" in bn.USDM_TESTNET_HOST


def test_usdm_live_host_is_fapi():
    assert _cfg(profile="live").host == bn.USDM_LIVE_HOST


def test_spot_paper_host_unchanged():
    cfg = bn.BinanceConfig.from_mapping({"profile": "paper", "api_key": "k", "api_secret": "s"})
    assert cfg.host == bn.DEFAULT_TESTNET_HOST


def test_shadow_and_trade_discrimination():
    assert bn.is_usdm_shadow(_cfg(profile="live-readonly")) is True
    assert bn.is_usdm_shadow(_cfg()) is False
    with pytest.raises(bn.BinanceConfigError):
        bn.reject_shadow_surface(_cfg(profile="live-readonly"))
    bn.reject_shadow_surface(_cfg())  # trading profile does not raise


def test_assert_host_accepts_futures_testnet_and_live():
    bn._assert_host(_cfg())                       # paper+usdm -> futures testnet
    bn._assert_host(_cfg(profile="live"))         # live+usdm -> fapi
    bn._assert_host(bn.BinanceConfig.from_mapping(  # spot unchanged
        {"profile": "paper", "api_key": "k", "api_secret": "s"}))


def test_assert_host_spot_paper_is_self_consistent_with_arbitrary_testnet_host():
    # Regression placeholder: the plan draft named this case
    # rejects_mismatched_testnet_host, which contradicts its semantics - for a
    # spot paper profile both cfg.host and the _assert_host expected value derive
    # from the same cfg.testnet_host field, so any testnet_host is
    # self-consistent and a real mismatch cannot be constructed via
    # from_mapping. Writing a raises() assertion here would be misleading, so it
    # is kept consistent with the existing _assert_host tests (the real guard
    # lives in cfg.host and the ccxt sandbox client; see plan Step 3 note). Only
    # two invariants are locked: spot paper host tracks testnet_host, and the
    # usdm paper host/expected come from the USDM_TESTNET_HOST constant rather
    # than cfg.testnet_host (the spot testnet host).
    spot = bn.BinanceConfig.from_mapping(
        {"profile": "paper", "api_key": "k", "api_secret": "s",
         "testnet_host": "https://evil.example"})
    assert spot.host == spot.testnet_host
    bn._assert_host(spot)  # expected and host share cfg.testnet_host, passes

    usdm = bn.BinanceConfig.from_mapping(
        {"profile": "paper", "market_type": "usdm", "api_key": "k", "api_secret": "s",
         "testnet_host": "https://evil.example"})
    assert usdm.host == bn.USDM_TESTNET_HOST  # constant, ignores testnet_host drift
    bn._assert_host(usdm)  # expected also from USDM_TESTNET_HOST, passes
