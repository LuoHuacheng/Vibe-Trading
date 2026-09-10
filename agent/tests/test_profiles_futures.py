"""USDⓈ-M futures trading profiles (paper testnet + live, mandate-gated).

Task 3 of the Binance USDⓈ-M trading plan: the two futures TradingProfile
entries appended to BINANCE_PROFILES carry market_type="usdm" so the
connector's tradable USDⓈ-M surface (paper/live profiles) is reachable
from the service layer, while the Shadow live-readonly profile stays untouched.
"""

from src.trading.connectors.binance import sdk as bn
from src.trading.connections import is_portfolio_connection_profile
from src.trading.profiles import list_profiles, profile_by_id
from src.trading.types import READ_CAPABILITIES


def test_futures_profiles_present():
    ids = {p.id for p in list_profiles()}
    assert "binance-futures-paper-trade" in ids
    assert "binance-futures-live-trade" in ids


def test_futures_profiles_carry_usdm_config():
    for pid, env in [("binance-futures-paper-trade", "paper"), ("binance-futures-live-trade", "live")]:
        p = profile_by_id(pid)
        assert p.environment == env
        assert p.transport == "broker_sdk"
        assert p.readonly is False
        assert p.config["market_type"] == "usdm"


def test_live_futures_profile_requires_mandate():
    p = profile_by_id("binance-futures-live-trade")
    assert "orders.place.requires_mandate" in p.capabilities
    assert "orders.place" not in p.capabilities


def test_spot_profiles_unchanged():
    ids = {p.id for p in list_profiles()}
    for pid in ("binance-paper-sdk", "binance-live-sdk-readonly", "binance-paper-trade", "binance-live-trade"):
        assert pid in ids


# --- Task 3 Step 5: profile.config must reach the final BinanceConfig -------
# service._sdk_config passes profile.config straight into
# module.build_config(profile_config, overrides) when no connection is scoped,
# or spreads it into {**profile.config, **credentials, ...} when one is.
# build_config copies every non-None profile_config key over the saved file
# before applying allowlisted overrides, so the futures profiles'
# market_type="usdm" lands on the BinanceConfig that service hands to
# place_order/reads. These tests lock that pass-through.


def test_futures_profile_config_reaches_binance_config():
    for pid in ("binance-futures-paper-trade", "binance-futures-live-trade"):
        p = profile_by_id(pid)
        cfg = bn.build_config(dict(p.config), {})
        assert cfg.market_type == "usdm"
        assert cfg.profile == p.config["profile"]


def test_futures_paper_profile_host_is_futures_testnet():
    p = profile_by_id("binance-futures-paper-trade")
    cfg = bn.build_config(dict(p.config), {})
    assert cfg.is_testnet is True
    assert cfg.host == bn.USDM_TESTNET_HOST


def test_futures_live_profile_host_is_fapi():
    p = profile_by_id("binance-futures-live-trade")
    cfg = bn.build_config(dict(p.config), {})
    assert cfg.is_testnet is False
    assert cfg.host == bn.USDM_LIVE_HOST


# --- Plan A: a read-only USDⓈ-M testnet profile for the portfolio page -------
# The portfolio page only accepts read-only connections, so the tradable futures
# profiles can never be a source there. This profile is the read-only source: it
# declares the USDⓈ-M market type without borrowing the spot profile's meaning.


def test_futures_readonly_profile_is_portfolio_eligible():
    p = profile_by_id("binance-futures-paper-readonly")
    assert p.environment == "paper"
    assert p.transport == "broker_sdk"
    assert p.readonly is True
    assert p.capabilities == READ_CAPABILITIES
    assert p.config == {"profile": "paper", "market_type": "usdm"}
    assert is_portfolio_connection_profile(p) is True


def test_futures_readonly_profile_host_is_futures_testnet():
    p = profile_by_id("binance-futures-paper-readonly")
    cfg = bn.build_config(dict(p.config), {})
    assert cfg.is_testnet is True
    assert cfg.host == bn.USDM_TESTNET_HOST


def test_futures_readonly_profile_carries_no_order_capability():
    p = profile_by_id("binance-futures-paper-readonly")
    assert not any(".place" in cap or "requires_mandate" in cap for cap in p.capabilities)