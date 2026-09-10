"""Binance ccxt connector with host-separated spot and USD-M futures profiles.

``market_type="usdm"`` is dual-purpose: under the ``live-readonly`` profile it
is a strict Shadow Account observation surface (signed account and position
reads against ``fapi.binance.com`` only), while under the ``paper``/``live``
profiles it is a tradable USDⓈ-M market. Paper USD-M trades against the futures
testnet (``testnet.binancefuture.com``); live USD-M trades on ``fapi.binance.com``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import getproxies

from src.config.paths import get_runtime_root
from src.trading.connectors.binance.shaping import (
    as_iter as _as_iter,
    nonzero_balances as _nonzero_balances,
    normalize_symbol,
    obj_get as _obj_get,
    ohlcv_to_dict as _ohlcv_to_dict,
    order_to_dict as _order_to_dict,
    to_float as _to_float,
    trade_to_dict as _trade_to_dict,
)
from src.trading.connectors.binance.usdm import (
    DEFAULT_OBSERVATION_ABSOLUTE_TOLERANCE,
    UsdMObservationError,
    assert_exchange_endpoints,
    read_account_observation as _read_usdm_observation,
)

CONFIG_FILENAME = "binance.json"

#: Profiles this connector understands and their default account environment.
PROFILE_ENVIRONMENTS = {
    "paper": "paper",
    "live-readonly": "live",
    "live": "live",
}

DEFAULT_TESTNET_HOST = "https://testnet.binance.vision"
LIVE_HOST = "https://api.binance.com"
USDM_LIVE_HOST = "https://fapi.binance.com"
USDM_TESTNET_HOST = "https://testnet.binancefuture.com"


def is_usdm_shadow(cfg: BinanceConfig) -> bool:
    """Return whether a usdm config is a strict Shadow observation profile.

    market_type="usdm" is dual-purpose: live-readonly is the strict Shadow
    Account observation surface (unchanged), while paper/live are tradable
    USDⓈ-M surfaces. Only the Shadow combination is read-only.
    """
    return cfg.market_type == "usdm" and cfg.profile == "live-readonly"


def reject_shadow_surface(cfg: BinanceConfig) -> None:
    """Reject a Shadow-only usdm config from order and market-data surfaces.

    Supersedes the old blanket _reject_unsupported_usdm_surface rejection by
    narrowing it to the Shadow profile: tradable USDⓈ-M profiles pass.
    """
    if is_usdm_shadow(cfg):
        raise BinanceConfigError(
            "Binance USD-M Shadow Account is read-only; it observes account and "
            "position evidence only"
        )

class BinanceDependencyError(RuntimeError):
    """Raised when the optional ``ccxt`` package is not installed."""


class BinanceConfigError(RuntimeError):
    """Raised when the connector configuration is missing or invalid."""


@dataclass(frozen=True)
class BinanceConfig:
    """Binance connector connection settings.
    """

    api_key: str = ""
    api_secret: str = ""
    profile: str = "paper"
    market_type: str = "spot"
    observation_absolute_tolerance: float = DEFAULT_OBSERVATION_ABSOLUTE_TOLERANCE
    testnet_host: str = DEFAULT_TESTNET_HOST
    timeout: float = 15.0
    readonly: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "BinanceConfig":
        """Build a config from a JSON-like mapping, normalizing the profile."""
        payload = dict(data or {})
        profile = str(payload.get("profile") or "paper").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise BinanceConfigError("profile must be 'paper', 'live-readonly' or 'live'")
        market_type = str(payload.get("market_type") or "spot").strip().lower()
        if market_type not in {"spot", "usdm"}:
            raise BinanceConfigError("market_type must be 'spot' or 'usdm'")
        # usdm admits any profile: live-readonly is the strict Shadow
        # observation surface; paper/live are tradable USDⓈ-M profiles.
        raw_tolerance = payload.get("observation_absolute_tolerance")
        try:
            tolerance = float(
                raw_tolerance
                if raw_tolerance is not None
                else DEFAULT_OBSERVATION_ABSOLUTE_TOLERANCE
            )
        except (TypeError, ValueError) as exc:
            raise BinanceConfigError(
                "observation_absolute_tolerance must be non-negative and finite"
            ) from exc
        if not math.isfinite(tolerance) or tolerance < 0:
            raise BinanceConfigError(
                "observation_absolute_tolerance must be non-negative and finite"
            )
        return cls(
            api_key=str(payload.get("api_key") or "").strip(),
            api_secret=str(payload.get("api_secret") or "").strip(),
            profile=profile,
            market_type=market_type,
            observation_absolute_tolerance=tolerance,
            testnet_host=str(payload.get("testnet_host") or DEFAULT_TESTNET_HOST).strip(),
            timeout=float(payload.get("timeout") or 15.0),
            readonly=bool(payload.get("readonly", True)),
        )

    def with_overrides(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        profile: str | None = None,
        market_type: str | None = None,
        observation_absolute_tolerance: float | None = None,
        testnet_host: str | None = None,
    ) -> "BinanceConfig":
        """Return a copy with CLI/tool overrides applied."""
        payload = asdict(self)
        if api_key is not None:
            payload["api_key"] = api_key
        if api_secret is not None:
            payload["api_secret"] = api_secret
        if profile is not None:
            payload["profile"] = profile
        if market_type is not None:
            payload["market_type"] = market_type
        if observation_absolute_tolerance is not None:
            payload["observation_absolute_tolerance"] = observation_absolute_tolerance
        if testnet_host is not None:
            payload["testnet_host"] = testnet_host
        return BinanceConfig.from_mapping(payload)

    @property
    def environment(self) -> str:
        """Return ``paper`` or ``live`` for this profile."""
        return PROFILE_ENVIRONMENTS.get(self.profile, "paper")

    @property
    def is_testnet(self) -> bool:
        """Return whether this profile targets the testnet host/key."""
        return self.environment == "paper"

    @property
    def host(self) -> str:
        """Return the REST host this profile connects to."""
        if self.market_type == "usdm":
            return USDM_TESTNET_HOST if self.is_testnet else USDM_LIVE_HOST
        return self.testnet_host if self.is_testnet else LIVE_HOST


_OVERRIDE_KEYS = (
    "api_key", "api_secret", "profile", "market_type",
    "observation_absolute_tolerance", "testnet_host",
)


def build_config(profile_config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None) -> "BinanceConfig":
    """Resolve the effective config: saved file ← profile defaults ← CLI overrides.

    Credentials (``api_key`` / ``api_secret``) come from the saved
    ``~/.vibe-trading/binance.json``; the selected connector profile supplies the
    ``profile`` intent; CLI/tool overrides win last.

    Every non-None key in ``profile_config`` is copied over the saved file the
    same way, so a futures profile config such as ``{"profile": "paper",
    "market_type": "usdm"}`` reaches the final BinanceConfig that
    ``service._sdk_config`` builds from ``TradingProfile.config`` for reads and
    order placement.
    """
    base = asdict(load_config())
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = BinanceConfig.from_mapping(base)
    clean = {k: v for k, v in dict(overrides or {}).items() if k in _OVERRIDE_KEYS and v not in (None, "")}
    return cfg.with_overrides(**clean) if clean else cfg


def config_path() -> Path:
    """Return the user-level Binance config path."""
    return get_runtime_root() / CONFIG_FILENAME


def load_config() -> BinanceConfig:
    """Load Binance settings from ``~/.vibe-trading/binance.json``."""
    path = config_path()
    if not path.exists():
        return BinanceConfig()
    try:
        return BinanceConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BinanceConfigError(f"invalid Binance config at {path}: {exc}") from exc


def save_config(config: BinanceConfig) -> Path:
    """Persist Binance settings with owner-only permissions."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def ccxt_available() -> bool:
    """Return whether the optional ``ccxt`` library can be imported."""
    try:
        _require_ccxt()
        return True
    except BinanceDependencyError:
        return False


def check_status(config: BinanceConfig | None = None) -> dict[str, Any]:
    """Check SDK readiness, config completeness, and host separation.

    Returns a JSON-serializable health report. Does not place or mutate any
    broker state. The host-allowlist guard asserts that the client's resolved
    host is the testnet host for a paper profile, or ``api.binance.com`` for a
    live profile, so a key/host mismatch fails closed before any read.
    """
    cfg = config or load_config()
    report: dict[str, Any] = {
        "status": "ok",
        "config": _public_config(cfg),
        "sdk": {"package": "ccxt", "installed": ccxt_available()},
        "paper_guard": "host_separated",
        "host": cfg.host,
    }

    missing = _missing_fields(cfg)
    if missing:
        report["status"] = "error"
        report["error"] = f"Binance connector not configured: missing {', '.join(missing)}."
        return report

    if not report["sdk"]["installed"]:
        report["status"] = "error"
        report["error"] = "Optional dependency missing: install with `pip install ccxt`."
        return report

    try:
        _assert_host(cfg)
    except BinanceConfigError as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    try:
        snapshot = get_account_snapshot(cfg)
    except Exception as exc:  # noqa: BLE001 - health endpoint reports cleanly
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    account_summary = {
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
    }
    count_field = "positions" if cfg.market_type == "usdm" else "balances"
    account_summary[count_field] = len(snapshot.get(count_field, []))
    report["account"] = account_summary
    return report


def get_account_snapshot(config: BinanceConfig | None = None) -> dict[str, Any]:
    """Fetch spot balances or a strict USD-M Shadow Account observation.

    Returns the non-zero balances (each with ``free`` / ``used`` / ``total``)
    from ccxt's unified ``fetch_balance``.
    """
    cfg = config or load_config()
    _assert_host(cfg)
    ex = _exchange(cfg)
    if is_usdm_shadow(cfg):
        # Strict Shadow observation surface: signed account + position evidence.
        return read_account_observation(cfg, ex)
    if cfg.market_type == "usdm":
        balance = ex.fetch_balance()
        rows = [
            {
                "symbol": row["asset"],
                "free": row["free"],
                "used": row["used"],
                "total": row["total"],
            }
            for row in _nonzero_balances(balance)
        ]
        equity_usd = next(
            (row["total"] for row in rows if row["symbol"] == "USDT"),
            None,
        )
        return {
            "status": "ok",
            "profile": cfg.profile,
            "is_testnet": cfg.is_testnet,
            "host": cfg.host,
            "paper_guard": "host_separated",
            "balances": rows,
            "equity_usd": equity_usd,
            "market_type": "usdm",
        }
    balance = ex.fetch_balance()
    rows = _nonzero_balances(balance)
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "host": cfg.host,
        "paper_guard": "host_separated",
        "balances": rows,
    }


def get_positions(config: BinanceConfig | None = None) -> dict[str, Any]:
    """Fetch spot holdings or strict USD-M position evidence.

    Binance spot has no positions; holdings are the non-zero balances. On live
    accounts Binance may expose Simple Earn Flexible collateral in the spot
    payload as synthetic ``LD<ASSET>`` balances. When the read-only Simple Earn
    endpoint is available, replace those wrappers with their underlying asset
    and authoritative ``totalAmount`` so portfolio valuation neither misses nor
    double-counts flexible holdings.
    """
    cfg = config or load_config()
    _assert_host(cfg)
    ex = _exchange(cfg)
    if is_usdm_shadow(cfg):
        # Strict Shadow observation surface: signed account + position evidence.
        return read_account_observation(cfg, ex)
    if cfg.market_type == "usdm":
        # Tradable USDⓈ-M surface: direct ccxt position rows (no spot
        # Simple Earn / cost-basis semantics apply to futures).
        positions = []
        for item in _as_iter(ex.fetch_positions()):
            row = futures_position_row(item)
            if row is not None:
                positions.append(row)
        return {
            "status": "ok",
            "profile": cfg.profile,
            "is_testnet": cfg.is_testnet,
            "paper_guard": "host_separated",
            "positions": positions,
            "market_type": "usdm",
        }
    balance = ex.fetch_balance()
    spot_balances = _nonzero_balances(balance)
    earn_rows: list[dict[str, Any]] = []
    earn_wrappers: set[str] = set()
    earn_note: str | None = None
    if not cfg.is_testnet:
        try:
            payload = ex.sapi_get_simple_earn_flexible_position({"size": 100})
            for item in payload.get("rows", []) if isinstance(payload, Mapping) else []:
                asset = str(_obj_get(item, "asset", "")).strip().upper()
                quantity = _to_float(_obj_get(item, "totalAmount"))
                if not asset or not quantity:
                    continue
                earn_wrappers.add(f"LD{asset}")
                earn_rows.append(
                    {
                        "symbol": asset,
                        "quantity": quantity,
                        "free": 0.0,
                        "used": quantity,
                        "source": "simple_earn_flexible",
                    }
                )
        except Exception as exc:  # noqa: BLE001 - spot holdings still remain usable
            earn_note = f"Simple Earn Flexible holdings unavailable: {type(exc).__name__}"

    rows = [
        {
            "symbol": _obj_get(row, "asset"),
            "quantity": _obj_get(row, "total"),
            "free": _obj_get(row, "free"),
            "used": _obj_get(row, "used"),
            "source": "spot",
        }
        for row in spot_balances
        if str(_obj_get(row, "asset", "")).upper() not in earn_wrappers
    ] + earn_rows
    # Binance spot balances carry no cost basis, so the portfolio cannot show
    # cost or unrealized P/L on its own. Derive a weighted-average entry price
    # per asset from the account's own trade history; a myTrades failure
    # degrades the row to an unknown cost instead of failing the whole read.
    for row in rows:
        cost = _spot_average_cost(ex, str(row.get("symbol") or ""))
        if cost is not None:
            row["cost_price"] = cost
    result = {
        "status": "ok",
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "positions": rows,
    }
    if earn_note:
        result["note"] = earn_note
    return result


#: How far back spot trade history is scanned for cost basis. Positions opened
#: before this window (with no later fills) keep an unknown cost.
# ponytail: 180-day/1000-trade window; fetch-and-paginate from account
# creation if a position ever predates the window.
_COST_BASIS_LOOKBACK_DAYS = 180
_COST_BASIS_MAX_TRADES = 1000


def _spot_average_cost(ex: Any, asset: str) -> float | None:
    """Weighted-average entry price for one spot asset from trade history.

    Binance spot balances do not report cost basis, so the average is rebuilt
    from ``myTrades`` (ccxt unified trades). Sells reduce quantity at the
    running average — the standard average-cost method — and a fully closed
    position resets so a later re-entry starts fresh.

    Args:
        ex: The ccxt exchange client.
        asset: The base asset, e.g. ``"UNI"``.

    Returns:
        The average entry price in the quote asset, or ``None`` when there is
        no usable history (fresh/untraded balance, or a myTrades failure).
    """
    if not asset:
        return None
    try:
        since = int((datetime.now(timezone.utc).timestamp() - _COST_BASIS_LOOKBACK_DAYS * 86400) * 1000)
        trades = ex.fetch_my_trades(f"{asset}/USDT", since=since, limit=_COST_BASIS_MAX_TRADES)
    except Exception:  # noqa: BLE001 — cost basis is best-effort, never fatal
        return None
    qty = 0.0
    avg = 0.0
    for trade in sorted(trades, key=lambda item: _obj_get(item, "timestamp", 0) or 0):
        amount = _to_float(_obj_get(trade, "amount"))
        price = _to_float(_obj_get(trade, "price"))
        if not amount or not price or amount <= 0 or price <= 0:
            continue
        signed = amount if str(_obj_get(trade, "side", "")).lower() == "buy" else -amount
        if signed > 0:
            avg = price if qty <= 0 else (avg * qty + price * signed) / (qty + signed)
            qty += signed
        else:
            qty = max(0.0, qty + signed)
            if qty <= 0:
                avg = 0.0
    return avg if qty > 0 and avg > 0 else None


def _spot_trade_stats(ex: Any, asset: str) -> dict[str, Any] | None:
    """Lifetime trade statistics for one spot asset from ``myTrades``.

    Buys accumulate quantity at the weighted-average cost; sells reduce
    quantity at the running average and bank realized P/L (only when a cost
    basis exists — gifted balances that are simply sold have none). A plain
    sell-only history (e.g. a testnet gift being flushed) yields no cost and
    no realized P/L.

    Args:
        ex: The ccxt exchange client.
        asset: The base asset, e.g. ``"UNI"``.

    Returns:
        Aggregate stats, or ``None`` when the history could not be read.
    """
    if not asset:
        return None
    try:
        since = int((datetime.now(timezone.utc).timestamp() - _COST_BASIS_LOOKBACK_DAYS * 86400) * 1000)
        trades = ex.fetch_my_trades(f"{asset}/USDT", since=since, limit=_COST_BASIS_MAX_TRADES)
    except Exception:  # noqa: BLE001 — trade history is best-effort, never fatal
        return None
    buys = 0
    sells = 0
    buy_amount = 0.0
    sell_amount = 0.0
    qty = 0.0
    avg = 0.0
    realized = 0.0
    first_ts: int | None = None
    last_ts: int | None = None
    for trade in sorted(trades, key=lambda item: _obj_get(item, "timestamp", 0) or 0):
        ts = _obj_get(trade, "timestamp")
        amount = _to_float(_obj_get(trade, "amount"))
        price = _to_float(_obj_get(trade, "price"))
        if not amount or not price or amount <= 0 or price <= 0:
            continue
        first_ts = first_ts if first_ts is not None else (int(ts) if ts else None)
        last_ts = int(ts) if ts else last_ts
        if str(_obj_get(trade, "side", "")).lower() == "buy":
            buys += 1
            buy_amount += amount * price
            avg = price if qty <= 0 else (avg * qty + price * amount) / (qty + amount)
            qty += amount
        else:
            sells += 1
            sell_amount += amount * price
            if qty > 0 and avg > 0:
                realized += (price - avg) * min(amount, qty)
            qty = max(0.0, qty - amount)

    def _iso(ms: int | None) -> str | None:
        if not ms:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

    return {
        "symbol": asset,
        "trades": buys + sells,
        "buys": buys,
        "sells": sells,
        "buy_amount_usd": round(buy_amount, 2),
        "sell_amount_usd": round(sell_amount, 2),
        "net_qty": round(qty, 8),
        "avg_cost": round(avg, 8) if qty > 0 and avg > 0 else None,
        "realized_pnl_usd": round(realized, 2) if buys > 0 else None,
        "first_trade_at": _iso(first_ts),
        "last_trade_at": _iso(last_ts),
    }


def get_traded_stats(
    config: BinanceConfig | None = None,
    assets: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate spot trade statistics for a list of base assets.

    A per-asset read failure degrades that asset to an empty stats row rather
    than failing the whole call, so a rate-limit or network hiccup never
    blanks the entire trade history panel.

    Args:
        config: Binance connector settings.
        assets: Base assets (e.g. ``"UNI"``); ``None``/empty yields no rows.

    Returns:
        ``{"status": "ok", "assets": [...]}``.
    """
    cfg = config or load_config()
    if cfg.market_type == "usdm":
        # myTrades aggregation is spot semantics only: USD-M trade history
        # (fapi userTrades) has a different shape and is out of scope, so a
        # futures config short-circuits to empty rather than misuse spot reads.
        return {"status": "ok", "assets": []}
    _assert_host(cfg)
    if not assets:
        return {"status": "ok", "assets": []}
    ex = _exchange(cfg)
    rows = []
    for asset in assets:
        if not asset:
            continue
        stats = _spot_trade_stats(ex, asset)
        if stats is None:
            stats = {
                "symbol": asset,
                "trades": 0,
                "buys": 0,
                "sells": 0,
                "buy_amount_usd": 0.0,
                "sell_amount_usd": 0.0,
                "net_qty": 0.0,
                "avg_cost": None,
                "realized_pnl_usd": None,
                "first_trade_at": None,
                "last_trade_at": None,
            }
        rows.append(stats)
    return {"status": "ok", "assets": rows}


def get_open_orders(config: BinanceConfig | None = None, *, include_executions: bool = False) -> dict[str, Any]:
    """Fetch open orders and, optionally, recent personal trades.

    ``fetch_open_orders`` is called without a symbol to retrieve all open
    orders; some Binance setups require a symbol, so the call is wrapped and a
    note is returned on failure rather than failing the whole call. When
    ``include_executions`` is set, ``fetch_my_trades`` typically REQUIRES a
    symbol, so it is also wrapped and degrades to an empty list with a note.
    """
    cfg = config or load_config()
    reject_shadow_surface(cfg)
    _assert_host(cfg)
    ex = _exchange(cfg)
    symbol_required = _symbol_required_errors()
    result: dict[str, Any] = {
        "status": "ok",
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
    }
    # Only the "this call needs a symbol" family degrades to a note; auth /
    # network / rate-limit errors must propagate so the caller sees a real
    # failure instead of a misleading status:ok with an empty list.
    try:
        open_orders = ex.fetch_open_orders()
        result["open_orders"] = [_order_to_dict(item) for item in _as_iter(open_orders)]
    except symbol_required as exc:
        result["open_orders"] = []
        result["open_orders_note"] = f"fetch_open_orders without a symbol failed: {exc}"
    if include_executions:
        try:
            trades = ex.fetch_my_trades()
            result["executions"] = [_trade_to_dict(item) for item in _as_iter(trades)]
        except symbol_required as exc:
            result["executions"] = []
            result["executions_note"] = f"fetch_my_trades without a symbol failed: {exc}"
    return result


def get_quote(symbol: str, *, config: BinanceConfig | None = None, **_: Any) -> dict[str, Any]:
    """Fetch a latest ticker snapshot for ``symbol`` (ccxt unified format)."""
    cfg = config or load_config()
    reject_shadow_surface(cfg)
    _assert_host(cfg)
    ex = _exchange(cfg)
    if cfg.market_type == "usdm":
        # Tradable USDⓈ-M surface expects a ccxt unified perp symbol
        # (``BASE/USDT:USDT``); anything else is rejected up front.
        clean = normalize_futures_symbol(symbol)
        if clean is None:
            raise BinanceConfigError(
                f"could not resolve a USDT-settled USDⓈ-M symbol from '{symbol}'."
            )
    else:
        clean = normalize_symbol(symbol)
    ticker = ex.fetch_ticker(clean)
    result = {
        "status": "ok",
        "symbol": clean,
        "quote": {
            "bid": _obj_get(ticker, "bid"),
            "ask": _obj_get(ticker, "ask"),
            "last": _obj_get(ticker, "last"),
            "high": _obj_get(ticker, "high"),
            "low": _obj_get(ticker, "low"),
            "volume": _obj_get(ticker, "baseVolume"),
            "time": str(_obj_get(ticker, "timestamp", "")),
        },
    }
    if cfg.market_type == "usdm":
        # Futures consumers also read the last price at the envelope top level.
        result["last"] = _obj_get(ticker, "last")
        result["market_type"] = "usdm"
    return result


def search_instruments(
    query: str,
    *,
    config: BinanceConfig | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Resolve an exact spot pair against the selected Binance market catalog.

    Binance's exchange-info catalog has symbols and assets, but no stable
    human-readable asset names. The connector therefore resolves only explicit
    pair spellings (``ETH-USDT``, ``ETH/USDT`` or ``ETHUSDT``) and never guesses
    from prose such as ``Ethereum``.
    """
    cfg = config or load_config()
    _reject_unsupported_usdm_surface(cfg)
    _assert_host(cfg)

    requested = normalize_symbol(query)
    if "/" not in requested:
        return {"status": "ok", "query": query, "instruments": []}

    try:
        bounded_limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError, OverflowError):
        bounded_limit = 10

    markets = _exchange(cfg).load_markets()
    if not isinstance(markets, Mapping):
        raise BinanceConfigError("Binance load_markets returned a non-mapping payload")

    instruments: list[dict[str, Any]] = []
    for key, raw_market in markets.items():
        if not isinstance(raw_market, Mapping):
            continue
        native_symbol = normalize_symbol(str(raw_market.get("symbol") or key))
        if native_symbol != requested:
            continue
        if raw_market.get("spot") is False or raw_market.get("active") is False:
            continue
        base, quote = native_symbol.split("/", 1)
        instruments.append(
            {
                "symbol": f"{base}-{quote}",
                "native_symbol": native_symbol,
                "exchange_symbol": str(raw_market.get("id") or "").strip() or None,
                "base": base,
                "quote": quote,
                "market": "crypto",
                "type": "cryptocurrency",
                "exchange": "BINANCE",
                "active": raw_market.get("active"),
            }
        )
        if len(instruments) >= bounded_limit:
            break

    return {"status": "ok", "query": query, "instruments": instruments}


#: Project/canonical period token → ccxt unified timeframe (lowercase).
_TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "1H": "1h", "4h": "4h", "4H": "4h",
    "1d": "1d", "1D": "1d", "1w": "1w", "1W": "1w", "1M": "1M",
}


def get_historical_bars(
    symbol: str,
    *,
    config: BinanceConfig | None = None,
    period: str = "1d",
    limit: int = 90,
    **_: Any,
) -> dict[str, Any]:
    """Fetch historical OHLCV bars for ``symbol`` (ccxt unified format)."""
    cfg = config or load_config()
    _reject_unsupported_usdm_surface(cfg)
    _assert_host(cfg)
    ex = _exchange(cfg)
    clean = normalize_symbol(symbol)
    timeframe = _TIMEFRAME_MAP.get(period.strip(), "1d")
    bars = ex.fetch_ohlcv(clean, timeframe=timeframe, limit=int(limit))
    return {
        "status": "ok",
        "symbol": clean,
        "period": period,
        "bars": [_ohlcv_to_dict(item) for item in _as_iter(bars)],
    }


# ---------------------------------------------------------------------------
# Order placement (write path; guarded by host separation + profile readonly)
# ---------------------------------------------------------------------------

#: ccxt ``timeInForce`` values Binance spot accepts for limit orders. Binance
#: has no DAY policy; the unified ``"day"`` intent maps to GTC, which is the
#: Binance default and the closest equivalent.
_TIME_IN_FORCE_MAP = {
    "day": "GTC",
    "gtc": "GTC",
    "ioc": "IOC",
    "fok": "FOK",
}


def place_order(
    config: BinanceConfig | None = None,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    notional: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "day",
    margin_mode: str | None = None,
    leverage: int | None = None,
    reduce_only: bool = False,
    stop_price: float | None = None,
    callback_rate: float | None = None,
) -> dict[str, Any]:
    """Place a spot or USDⓈ-M futures order via ccxt's ``create_order``.

    The configured profile's host (testnet vs live) is the authoritative
    paper/live discriminator and is asserted before anything is submitted, so a
    testnet key can never reach the live host. The connector simply executes the
    intent the caller has already authorized; mandate/limit enforcement lives in
    a higher layer.

    Spot semantics: either ``quantity`` (base-asset amount) or ``notional``
    (quote-asset spend) must be given, never both. ``notional`` is only
    supported for market orders: ccxt's binance adapter forwards
    ``params={"quoteOrderQty": notional}`` so Binance sizes the order in the
    quote asset (e.g. spend 50 USDT of BTC). Limit orders require ``quantity``
    and ``limit_price``. The ``margin_mode``/``leverage``/``reduce_only``
    parameters are futures-only; passing any of them to a spot config returns an
    error envelope and never touches the exchange.

    USDⓈ-M semantics (``market_type="usdm"``): the strict Shadow profile
    (``live-readonly``) stays read-only and returns an error envelope, while
    tradable profiles (``paper``/``live``) require both ``margin_mode``
    (``"isolated"`` or ``"cross"``) and ``leverage`` (integer 1..125). The
    presets are applied through ``ex.set_margin_mode`` and ``ex.set_leverage``
    in the same guarded block as ``ex.create_order`` — any exception becomes an
    error envelope and no ``order_id`` is returned. ``reduce_only`` forwards
    ``params["reduceOnly"]=True`` on the futures branch. Futures sells reject
    ``notional`` (orders size by ``quantity``), and the symbol is normalized to
    the ccxt unified perp form (``BASE/USDT:USDT``).

    Args:
        config: Connector config; falls back to the saved config when ``None``.
        symbol: Trading pair in any accepted form (spot normalized to
            ``BASE/QUOTE``; futures to ``BASE/USDT:USDT``).
        side: ``"buy"`` or ``"sell"``.
        quantity: Base-asset amount. Mutually exclusive with ``notional``.
        notional: Quote-asset spend (market orders only). Mutually exclusive
            with ``quantity``; not accepted for futures sells.
        order_type: ``"market"``, ``"limit"``, ``"stop_market"`` or
            ``"take_profit_market"``. The two conditional types are
            reduce-only exchange-side stop / take-profit orders (they survive
            this process) and require ``stop_price``.
        limit_price: Required when ``order_type`` is ``"limit"``.
        time_in_force: Limit-order policy; ``"day"`` maps to Binance GTC.
        margin_mode: Futures-only; ``"isolated"`` or ``"cross"`` (required on
            tradable USDⓈ-M profiles).
        leverage: Futures-only; integer 1..125 (required on tradable USDⓈ-M
            profiles).
        reduce_only: Futures-only; close-position-only order flag.

    Returns:
        On success: ``{"status": "ok", "order_id": str, "symbol", "side",
        "profile", "order_type", "status", "filled", "amount", "price"}`` (plus
        ``market_type`` on futures). On any validation or execution failure:
        ``{"status": "error", "error": str}`` (fail-closed; nothing is submitted
        on a validation error).
    """
    cfg = config or load_config()

    if cfg.market_type == "usdm":
        if is_usdm_shadow(cfg):
            return {
                "status": "error",
                "error": "Binance USD-M Shadow Account is read-only",
            }
        return _place_usdm_order(
            cfg,
            symbol=symbol,
            side=side,
            quantity=quantity,
            notional=notional,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
            margin_mode=margin_mode,
            leverage=leverage,
            reduce_only=reduce_only,
            stop_price=stop_price,
            callback_rate=callback_rate,
        )

    if (
        margin_mode is not None
        or leverage is not None
        or reduce_only
        or stop_price is not None
        or callback_rate is not None
    ):
        return {
            "status": "error",
            "error": "margin_mode/leverage/reduce_only/stop_price/callback_rate are "
            "futures-only parameters; this is a spot profile.",
        }

    side_clean = str(side or "").strip().lower()
    if side_clean not in ("buy", "sell"):
        return {"status": "error", "error": "side must be 'buy' or 'sell'."}

    type_clean = str(order_type or "").strip().lower()
    if type_clean not in ("market", "limit"):
        return {"status": "error", "error": "order_type must be 'market' or 'limit'."}

    qty_given = quantity is not None
    notional_given = notional is not None
    if qty_given == notional_given:
        return {"status": "error", "error": "provide exactly one of 'quantity' or 'notional'."}

    qty_value = _to_float(quantity) if qty_given else None
    notional_value = _to_float(notional) if notional_given else None
    if qty_given and (qty_value is None or qty_value <= 0):
        return {"status": "error", "error": "quantity must be a positive number."}
    if notional_given and (notional_value is None or notional_value <= 0):
        return {"status": "error", "error": "notional must be a positive number."}

    if type_clean == "limit":
        if notional_given:
            return {"status": "error", "error": "limit orders require 'quantity', not 'notional'."}
        price_value = _to_float(limit_price)
        if price_value is None or price_value <= 0:
            return {"status": "error", "error": "limit orders require a positive 'limit_price'."}
    else:
        price_value = None

    # The connector merely executes against whatever environment the profile
    # selects; readonly/mandate gating is enforced upstream (service + profile
    # capabilities), not inside the connector. The host separation asserted next
    # is the one structural guard that cannot be bypassed here.
    try:
        _assert_host(cfg)
    except BinanceConfigError as exc:
        return {"status": "error", "error": str(exc)}

    clean_symbol = normalize_symbol(symbol)
    if not clean_symbol or "/" not in clean_symbol:
        return {"status": "error", "error": f"could not resolve a valid trading pair from symbol '{symbol}'."}

    params: dict[str, Any] = {}
    if type_clean == "limit":
        tif = _TIME_IN_FORCE_MAP.get(str(time_in_force or "").strip().lower())
        if tif is None:
            return {"status": "error", "error": "time_in_force must be one of 'day', 'gtc', 'ioc', 'fok'."}
        params["timeInForce"] = tif
        amount: float | None = qty_value
        price: float | None = price_value
    elif notional_given:
        # ccxt's binance adapter reads ``quoteOrderQty`` from params and sizes the
        # order in the quote asset; ``amount`` carries the same notional so callers
        # that inspect the unified amount see a sensible value.
        params["quoteOrderQty"] = notional_value
        amount = notional_value
        price = None
    else:
        amount = qty_value
        price = None

    try:
        ex = _exchange(cfg)
        order = ex.create_order(clean_symbol, type_clean, side_clean, amount, price, params)
    except Exception as exc:  # noqa: BLE001 - surface any ccxt/auth/network error as fail-closed
        return {"status": "error", "error": str(exc)}

    return {
        "status": "ok",
        "order_id": str(_obj_get(order, "id", "")),
        "symbol": _obj_get(order, "symbol", clean_symbol),
        "side": str(_obj_get(order, "side", side_clean)),
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "order_type": str(_obj_get(order, "type", type_clean)),
        "order_status": str(_obj_get(order, "status", "")),
        "filled": _obj_get(order, "filled"),
        "amount": _obj_get(order, "amount"),
        "price": _obj_get(order, "price"),
    }


def _place_usdm_order(
    cfg: BinanceConfig,
    *,
    symbol: str,
    side: str,
    quantity: float | None,
    notional: float | None,
    order_type: str,
    limit_price: float | None,
    time_in_force: str,
    margin_mode: str | None,
    leverage: int | None,
    reduce_only: bool,
    stop_price: float | None = None,
    callback_rate: float | None = None,
) -> dict[str, Any]:
    """Place a USDⓈ-M futures order on a tradable (non-Shadow) usdm profile.

    Shared spot order rules apply (side/type validation, exactly one of
    quantity/notional, market-only notional, limit price), plus the futures
    contract: both presets are required and validated, futures sells never
    size by notional, and reduce_only becomes params["reduceOnly"]. The
    margin/leverage presets and create_order share one guarded block so any
    exception yields a fail-closed error envelope with no order_id.
    """
    side_clean = str(side or "").strip().lower()
    if side_clean not in ("buy", "sell"):
        return {"status": "error", "error": "side must be 'buy' or 'sell'."}

    type_clean = str(order_type or "").strip().lower()
    if type_clean not in ("market", "limit", "stop_market", "take_profit_market", "trailing_stop_market"):
        return {
            "status": "error",
            "error": (
                "order_type must be 'market', 'limit', 'stop_market', "
                "'take_profit_market' or 'trailing_stop_market'."
            ),
        }
    conditional = type_clean in _CONDITIONAL_ORDER_TYPES
    trailing = type_clean == "trailing_stop_market"

    margin_clean = str(margin_mode or "").strip().lower()
    if not margin_mode or not margin_clean:
        return {"status": "error", "error": "margin_mode is required for USDⓈ-M orders ('isolated' or 'cross')."}
    if margin_clean not in ("isolated", "cross"):
        return {
            "status": "error",
            "error": f"margin_mode must be 'isolated' or 'cross', got '{margin_mode}'.",
        }
    if leverage is None:
        return {"status": "error", "error": "leverage is required for USDⓈ-M orders (integer 1..125)."}
    if isinstance(leverage, bool) or not isinstance(leverage, int) or not (1 <= leverage <= 125):
        return {"status": "error", "error": "leverage must be an integer between 1 and 125."}
    if reduce_only is not None and not isinstance(reduce_only, bool):
        return {"status": "error", "error": "reduce_only must be a boolean."}

    qty_given = quantity is not None
    notional_given = notional is not None
    if qty_given == notional_given:
        return {"status": "error", "error": "provide exactly one of 'quantity' or 'notional'."}

    qty_value = _to_float(quantity) if qty_given else None
    notional_value = _to_float(notional) if notional_given else None
    if qty_given and (qty_value is None or qty_value <= 0):
        return {"status": "error", "error": "quantity must be a positive number."}
    if notional_given and (notional_value is None or notional_value <= 0):
        return {"status": "error", "error": "notional must be a positive number."}
    if notional_given and side_clean == "sell":
        # USDⓈ-M sells close/short in base contracts; sizing by quote spend is a
        # spot-only convenience and is rejected up front.
        return {"status": "error", "error": "USDⓈ-M sell orders require 'quantity', not 'notional'."}

    stop_value: float | None = None
    callback_value: float | None = None
    if conditional:
        if notional_given:
            return {"status": "error", "error": "conditional USDⓈ-M orders require 'quantity', not 'notional'."}
        if not reduce_only:
            # A conditional order that is NOT reduce-only can open a fresh
            # position the moment it triggers — with nobody watching and no
            # margin preset applied at trigger time. Refuse that outright.
            return {"status": "error", "error": "conditional orders must set reduce_only=True."}
        if trailing:
            # A trailing stop follows the market itself, so it takes a callback
            # rate rather than a trigger price. Binance accepts 0.1%..5% only,
            # so anything outside that is rejected here instead of
            # round-tripping to the exchange.
            if stop_price is not None:
                return {
                    "status": "error",
                    "error": "'stop_price' is not valid for trailing_stop_market; use 'callback_rate'.",
                }
            callback_value = _to_float(callback_rate)
            if callback_value is None or not (0.1 <= callback_value <= 5.0):
                return {
                    "status": "error",
                    "error": "callback_rate must be between 0.1 and 5.0 (percent) for trailing_stop_market orders.",
                }
        else:
            stop_value = _to_float(stop_price)
            if stop_value is None or stop_value <= 0:
                return {
                    "status": "error",
                    "error": "stop_price must be a positive number for stop_market/take_profit_market orders.",
                }
            if callback_rate is not None:
                return {"status": "error", "error": "'callback_rate' is only valid for trailing_stop_market orders."}
    elif stop_price is not None or callback_rate is not None:
        return {"status": "error", "error": "stop_price/callback_rate are only valid for conditional order types."}

    if type_clean == "limit":
        if notional_given:
            return {"status": "error", "error": "limit orders require 'quantity', not 'notional'."}
        price_value = _to_float(limit_price)
        if price_value is None or price_value <= 0:
            return {"status": "error", "error": "limit orders require a positive 'limit_price'."}
    else:
        price_value = None

    clean_symbol = normalize_futures_symbol(symbol)
    if clean_symbol is None:
        return {
            "status": "error",
            "error": f"could not resolve a USDT-settled USDⓈ-M symbol from '{symbol}'.",
        }

    params: dict[str, Any] = {}
    if type_clean == "limit":
        tif = _TIME_IN_FORCE_MAP.get(str(time_in_force or "").strip().lower())
        if tif is None:
            return {"status": "error", "error": "time_in_force must be one of 'day', 'gtc', 'ioc', 'fok'."}
        params["timeInForce"] = tif
        amount: float | None = qty_value
        price: float | None = price_value
    elif conditional:
        # Exchange-side stop / take-profit / trailing stop: the order rests on
        # Binance, so the protection outlives this process. Sizing is by
        # contract quantity and reduce_only was enforced above.
        if trailing:
            params["callbackRate"] = callback_value
        else:
            params["stopPrice"] = stop_value
        amount = qty_value
        price = None
    elif notional_given:
        params["quoteOrderQty"] = notional_value
        amount = notional_value
        price = None
    else:
        amount = qty_value
        price = None
    if reduce_only:
        params["reduceOnly"] = True

    try:
        ex = _exchange(cfg)
    except Exception as exc:  # noqa: BLE001 - host/endpoint guard failures are fail-closed
        return {"status": "error", "error": str(exc)}
    margin_error = _ensure_futures_margin(ex, clean_symbol, margin_clean)
    if margin_error is not None:
        return {"status": "error", "error": margin_error}
    try:
        ex.set_leverage(leverage, clean_symbol)
        order = ex.create_order(
            clean_symbol,
            _CONDITIONAL_ORDER_TYPES.get(type_clean, type_clean),
            side_clean,
            amount,
            price,
            params,
        )
    except Exception as exc:  # noqa: BLE001 - surface any ccxt/auth/network error as fail-closed
        return {"status": "error", "error": str(exc)}

    return {
        "status": "ok",
        "order_id": str(_obj_get(order, "id", "")),
        "symbol": _obj_get(order, "symbol", clean_symbol),
        "side": str(_obj_get(order, "side", side_clean)),
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "order_type": str(_obj_get(order, "type", type_clean)),
        "order_status": str(_obj_get(order, "status", "")),
        "filled": _obj_get(order, "filled"),
        "amount": _obj_get(order, "amount"),
        "price": _obj_get(order, "price"),
        "market_type": "usdm",
    }


#: Caller-facing conditional order types mapped to the ccxt/Binance type string.
_CONDITIONAL_ORDER_TYPES = {
    "stop_market": "STOP_MARKET",
    "take_profit_market": "TAKE_PROFIT_MARKET",
    "trailing_stop_market": "TRAILING_STOP_MARKET",
}


#: Binance answers setMarginType with -4046 ("No need to change margin type.")
#: whenever the symbol already trades on the requested type. ccxt raises that
#: payload as an exception, so it is matched on text as well as on the code
#: attribute (ccxt does not always populate the latter for Binance errors).
_MARGIN_TYPE_ALREADY_SET = "-4046"
_MARGIN_TYPE_ALREADY_SET_PHRASE = "no need to change margin type"


def _margin_type_already_set(exc: BaseException) -> bool:
    """Return True when setMarginType failed only because nothing had to change.

    Margin type is a *persistent* per-symbol account setting, not a per-order
    preset: the first USD-M order on a symbol flips it, and every later order
    that asks for the same type is answered with -4046. Treating that as an
    error would refuse every position-less order after the first one.
    """
    code = getattr(exc, "code", None)
    if code is not None and str(code).lstrip("-") == _MARGIN_TYPE_ALREADY_SET.lstrip("-"):
        return True
    text = str(exc).lower()
    return _MARGIN_TYPE_ALREADY_SET in text or _MARGIN_TYPE_ALREADY_SET_PHRASE in text


def _ensure_futures_margin(ex: Any, symbol: str, margin_mode: str) -> str | None:
    """Verify or apply the symbol margin type without tripping Binance -4067/-4046.

    Binance rejects setMarginType (-4067, reported as "Position side cannot be
    changed...") whenever the symbol already has an open position, even when the
    requested type matches the current one. When a position exists this helper
    verifies the position's current margin type and skips the call; only
    position-less symbols actually call set_margin_mode. A position-less symbol
    whose type already matches is answered with -4046, which is the no-op it
    says it is and must not fail the order. A failed position read fails closed.
    """
    rows: list[Any] = []
    try:
        raw = ex.fetch_positions([symbol])
        rows = list(_as_iter(raw))
    except Exception:  # noqa: BLE001 - cannot verify consistency without the read
        return "could not read existing positions to verify margin mode for " + symbol
    current: str | None = None
    for row in rows:
        if str(_obj_get(row, "symbol") or "").strip().upper() != symbol.upper():
            continue
        mode = str(_obj_get(row, "marginMode") or "").strip().lower()
        if mode:
            current = mode
            break
    if current is not None:
        if current != margin_mode:
            return (
                "symbol " + symbol + " already trades on " + current + " margin; "
                "refusing requested " + margin_mode + ". Flatten first or pass " + current + "."
            )
        return None
    try:
        ex.set_margin_mode(margin_mode, symbol)
    except Exception as exc:  # noqa: BLE001 - e.g. resting orders block the change
        if _margin_type_already_set(exc):
            return None
        if "-4067" in str(exc):
            # Binance refuses the change while the symbol carries resting orders
            # (including conditional ones, which live in the Algo service). Say
            # so, instead of leaving the caller with the raw envelope.
            return (
                "could not set margin mode " + margin_mode + " on " + symbol
                + ": Binance refuses while the symbol has open orders (-4067); "
                "cancel its open/conditional orders first. " + str(exc)
            )
        return "could not set margin mode " + margin_mode + " on " + symbol + ": " + str(exc)
    return None


def cancel_order(
    config: BinanceConfig | None = None,
    order_id: str = "",
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Cancel an open order by id. Binance REQUIRES the order's symbol.

    Tradable USDⓈ-M profiles cancel through the futures client with the symbol
    normalized to ccxt unified perp form; the strict Shadow profile
    (``live-readonly``) is read-only and returns an error envelope instead.

    Args:
        config: Connector config; falls back to the saved config when ``None``.
        order_id: The exchange order id to cancel.
        symbol: The order's trading pair. Binance cannot cancel without it.

    Returns:
        On success: ``{"status": "ok", "order_id", "symbol", "side", "profile",
        "order_status"}``. On any validation or execution failure:
        ``{"status": "error", "error": str}`` (fail-closed).
    """
    cfg = config or load_config()

    if cfg.market_type == "usdm" and is_usdm_shadow(cfg):
        return {
            "status": "error",
            "error": "Binance USD-M Shadow Account is read-only",
        }

    order_id_clean = str(order_id or "").strip()
    if not order_id_clean:
        return {"status": "error", "error": "order_id is required to cancel an order."}
    if symbol is None or not str(symbol).strip():
        return {"status": "error", "error": "Binance cancel requires symbol."}

    try:
        _assert_host(cfg)
    except BinanceConfigError as exc:
        return {"status": "error", "error": str(exc)}

    if cfg.market_type == "usdm":
        clean_symbol = normalize_futures_symbol(symbol)
        if clean_symbol is None:
            return {
                "status": "error",
                "error": f"could not resolve a USDT-settled USDⓈ-M symbol from '{symbol}'.",
            }
    else:
        clean_symbol = normalize_symbol(symbol)
        if not clean_symbol or "/" not in clean_symbol:
            return {"status": "error", "error": f"could not resolve a valid trading pair from symbol '{symbol}'."}

    try:
        ex = _exchange(cfg)
        order = ex.cancel_order(order_id_clean, clean_symbol)
    except Exception as exc:  # noqa: BLE001 - surface any ccxt/auth/network error as fail-closed
        return {"status": "error", "error": str(exc)}

    result = {
        "status": "ok",
        "order_id": str(_obj_get(order, "id", order_id_clean)),
        "symbol": _obj_get(order, "symbol", clean_symbol),
        "side": str(_obj_get(order, "side", "")),
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "order_status": str(_obj_get(order, "status", "")),
    }
    if cfg.market_type == "usdm":
        result["market_type"] = "usdm"
    return result


def _perp_symbol(raw_symbol: Any) -> str:
    """Return the ccxt unified perp symbol for a raw Binance futures symbol.

    Binance's raw market endpoints speak compact symbols (BCHUSDT); the
    connector speaks the ccxt unified perp form (BCH/USDT:USDT).
    """
    text = str(raw_symbol or "").strip().upper()
    if "/" in text:
        return normalize_futures_symbol(text) or text
    if text.endswith("USDT") and len(text) > len("USDT"):
        return text[: -len("USDT")] + "/USDT:USDT"
    return text


def algo_order_row(item: Any) -> dict[str, Any] | None:
    """Map one Algo-service row to the connector's open-order shape.

    Returns None when the row carries no algo id or no symbol (nothing a caller
    could cancel or match on).
    """
    symbol = _perp_symbol(_obj_get(item, "symbol"))
    algo_id = _obj_get(item, "algoId")
    if not symbol or algo_id is None or str(algo_id).strip() == "":
        return None
    return {
        "order_id": str(algo_id),
        "symbol": symbol,
        "side": str(_obj_get(item, "side") or "").lower(),
        "order_type": str(_obj_get(item, "orderType") or "").lower(),
        "stop_price": _to_float(_obj_get(item, "triggerPrice")),
        "quantity": _to_float(_obj_get(item, "quantity")),
        "status": str(_obj_get(item, "algoStatus") or "").lower(),
    }


def get_open_algo_orders(config: BinanceConfig | None = None) -> dict[str, Any]:
    """List resting conditional (algo) orders on a tradable USDⓈ-M profile.

    Binance routes stop_market / take_profit_market orders through its Algo
    Order service, so they never appear in fetch_open_orders(). A caller that
    only reads the standard open-order list cannot tell that its protection is
    already armed, and cannot find the sibling leg left behind once one of them
    triggers. This is the read for that surface.

    Args:
        config: Connector config; falls back to the saved config when None.

    Returns:
        On success: {"status": "ok", "orders": [row, ...]} where each row is
        shaped by algo_order_row (order_id is the algo id). On failure:
        {"status": "error", "error": str} (fail-closed).
    """
    cfg = config or load_config()
    try:
        _assert_host(cfg)
    except BinanceConfigError as exc:
        return {"status": "error", "error": str(exc)}
    if cfg.market_type != "usdm":
        return {"status": "error", "error": "conditional (algo) orders are USDⓈ-M futures-only."}
    if is_usdm_shadow(cfg):
        return {"status": "error", "error": "Binance USD-M Shadow Account is read-only"}
    try:
        ex = _exchange(cfg)
        raw = ex.fapiPrivateGetOpenAlgoOrders({})
    except Exception as exc:  # noqa: BLE001 - surface auth/network errors fail-closed
        return {"status": "error", "error": str(exc)}
    orders = [row for row in (algo_order_row(item) for item in _as_iter(raw)) if row is not None]
    return {
        "status": "ok",
        "orders": orders,
        "count": len(orders),
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "market_type": "usdm",
    }


def cancel_algo_order(
    config: BinanceConfig | None = None,
    algo_id: str = "",
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Cancel one resting conditional (algo) order by its algo id.

    Conditional orders cannot be cancelled through the standard order endpoint:
    Binance answers -2013 ("Order does not exist") because the order lives in
    the Algo service. The cancel is idempotent — an order that already
    triggered, or was already cancelled, is reported as already_gone rather
    than as a failure, which is the state the caller asked for.

    Args:
        config: Connector config; falls back to the saved config when None.
        algo_id: The algo order id (order_id from get_open_algo_orders).
        symbol: Optional trading pair, echoed back for the caller's bookkeeping.

    Returns:
        On success: {"status": "ok", "order_id", "symbol", "already_gone": bool}.
        On failure: {"status": "error", "error": str} (fail-closed).
    """
    cfg = config or load_config()
    try:
        _assert_host(cfg)
    except BinanceConfigError as exc:
        return {"status": "error", "error": str(exc)}
    if cfg.market_type != "usdm":
        return {"status": "error", "error": "conditional (algo) orders are USDⓈ-M futures-only."}
    if is_usdm_shadow(cfg):
        return {"status": "error", "error": "Binance USD-M Shadow Account is read-only"}
    algo_clean = str(algo_id or "").strip()
    if not algo_clean:
        return {"status": "error", "error": "algo_id is required to cancel a conditional order."}
    def _gone(text: str) -> bool:
        """Already gone: -2013 ("Order does not exist") on the standard order
        endpoint, -2011 ("Unknown order sent") on the Algo one. Either way the
        order triggered or was cancelled, which is the state the caller wants."""
        lowered = text.lower()
        return (
            "does not exist" in lowered
            or "unknown order" in lowered
            or "-2013" in lowered
            or "-2011" in lowered
        )

    def _result(already_gone: bool) -> dict[str, Any]:
        return {
            "status": "ok",
            "order_id": algo_clean,
            "symbol": symbol,
            "already_gone": already_gone,
            "profile": cfg.profile,
            "is_testnet": cfg.is_testnet,
            "paper_guard": "host_separated",
            "market_type": "usdm",
        }

    try:
        ex = _exchange(cfg)
        delete = lambda: ex.fapiPrivateDeleteAlgoOrder({"algoId": algo_clean})  # noqa: E731
        try:
            delete()
        except Exception as exc:  # noqa: BLE001 - fail-closed except the idempotent cases
            text = str(exc)
            if _gone(text):
                return _result(True)
            if "-1021" not in text:
                return {"status": "error", "error": text}
            # -1021 is the recvWindow/timestamp check: this machine's clock had
            # drifted from Binance's for that request, which is transient. One
            # retry almost always lands, and leaving the order resting would
            # block new entries on the symbol with -4067.
            try:
                delete()
            except Exception as retry_exc:  # noqa: BLE001
                if _gone(str(retry_exc)):
                    return _result(True)
                return {"status": "error", "error": str(retry_exc)}
    except Exception as exc:  # noqa: BLE001 - exchange construction failures
        return {"status": "error", "error": str(exc)}
    return _result(False)


def get_futures_context(
    config: BinanceConfig | None = None,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Read USDⓈ-M derivatives context: funding, basis and open interest.

    Funding rate, mark price and index price come from ONE batch premiumIndex
    call covering the whole market, so adding symbols to it is free. Open
    interest has no batch endpoint and is read per requested symbol (a symbol
    that fails is simply omitted, with the failures counted).

    Open-interest *change* is deliberately not fetched here: Binance serves that
    history from the fapiData endpoints, which ccxt refuses on testnet
    ("does not have a testnet/sandbox URL for fapiData endpoints"). Callers that
    want a change compare successive reads themselves.

    Args:
        config: Connector config; falls back to the saved config when None.
        symbols: Optional unified perp symbols to read open interest for.

    Returns:
        On success: {"status": "ok", "funding": {symbol: {...}},
        "open_interest": {symbol: float}, "open_interest_errors": int}. On
        failure: {"status": "error", "error": str} (fail-closed).
    """
    cfg = config or load_config()
    try:
        _assert_host(cfg)
    except BinanceConfigError as exc:
        return {"status": "error", "error": str(exc)}
    if cfg.market_type != "usdm":
        return {"status": "error", "error": "derivatives context is USDⓈ-M futures-only."}
    if is_usdm_shadow(cfg):
        return {"status": "error", "error": "Binance USD-M Shadow Account is read-only"}
    try:
        ex = _exchange(cfg)
        raw = ex.fapiPublicGetPremiumIndex({})
    except Exception as exc:  # noqa: BLE001 - surface auth/network errors fail-closed
        return {"status": "error", "error": str(exc)}

    funding: dict[str, dict[str, Any]] = {}
    for item in _as_iter(raw):
        symbol = _perp_symbol(_obj_get(item, "symbol"))
        if not symbol:
            continue
        funding[symbol] = {
            "mark_price": _to_float(_obj_get(item, "markPrice")),
            "index_price": _to_float(_obj_get(item, "indexPrice")),
            "funding_rate": _to_float(_obj_get(item, "lastFundingRate")),
            "next_funding_time": _obj_get(item, "nextFundingTime"),
        }

    open_interest: dict[str, float] = {}
    failures = 0
    for symbol in symbols or []:
        clean = normalize_futures_symbol(symbol)
        if clean is None:
            failures += 1
            continue
        try:
            row = ex.fetch_open_interest(clean)
        except Exception:  # noqa: BLE001 - one symbol must not sink the batch
            failures += 1
            continue
        value = _to_float(_obj_get(row, "openInterestAmount"))
        if value is None:
            value = _to_float(_obj_get(row, "openInterestValue"))
        if value is not None:
            open_interest[clean] = value
        else:
            failures += 1

    return {
        "status": "ok",
        "funding": funding,
        "open_interest": open_interest,
        "open_interest_errors": failures,
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "market_type": "usdm",
    }


# ---------------------------------------------------------------------------
# SDK plumbing
# ---------------------------------------------------------------------------


def _require_ccxt() -> ModuleType:
    try:
        import ccxt  # type: ignore
    except ModuleNotFoundError as exc:
        raise BinanceDependencyError("ccxt is not installed; run `pip install ccxt`.") from exc
    return ccxt


def _symbol_required_errors() -> tuple[type[BaseException], ...]:
    """ccxt exception classes that mean "this call needs a symbol" (degrade-only).

    Auth/network/rate-limit errors are deliberately excluded so they propagate
    rather than being masked as a status:ok note.
    """
    ccxt = _require_ccxt()
    names = ("ArgumentsRequired", "BadSymbol", "NotSupported")
    classes = tuple(getattr(ccxt, n) for n in names if hasattr(ccxt, n))
    return classes or (ValueError,)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_futures_symbol(symbol: str) -> str | None:
    """Normalize a USDⓈ-M perpetual symbol to ccxt unified format.

    Accepts BTC/USDT:USDT, BTC/USDT and BTC-USDT and returns the canonical
    BTC/USDT:USDT. Only USDT-settled linear perps are supported (no COIN-M);
    an empty or unresolvable input returns None.
    """
    clean = (symbol or "").strip().upper().replace("-", "/")
    if not clean:
        return None
    if ":" in clean:
        main, _, settlement = clean.partition(":")
        if settlement != "USDT":
            return None
        clean = main
    if "/" not in clean:
        return None
    base, quote = clean.split("/", 1)
    if not base or "/" in quote or quote != "USDT":
        return None
    return f"{base}/USDT:USDT"


def futures_position_row(row: Any) -> dict[str, Any] | None:
    """Map one ccxt fetch_positions row to a futures trade-read row.

    Fields: symbol (ccxt unified), quantity signed so a short is negative
    (falls back to contracts signed by side), side (long/short), price and
    mark_price from the row's mark price, plus unrealized_pnl, leverage and
    margin_mode as reported by ccxt. Returns None when the row carries no
    usable symbol or quantity.
    """
    symbol = str(_obj_get(row, "symbol") or "").strip()
    side = str(_obj_get(row, "side") or "").strip().lower()
    quantity = _to_float(_obj_get(row, "quantity"))
    if quantity is None:
        contracts = _to_float(_obj_get(row, "contracts"))
        if contracts is None:
            return None
        # ccxt positions report unsigned contracts + a side; keep the quantity
        # signed so a short position reads negative.
        quantity = -contracts if side == "short" else contracts
    if not symbol or not quantity:
        return None
    mark_price = _to_float(_obj_get(row, "markPrice"))
    return {
        "symbol": symbol,
        "quantity": quantity,
        "side": side,
        "price": mark_price,
        "mark_price": mark_price,
        # Entry price is the position's cost basis. The Shadow observation row
        # has always carried it as entry_price; the trade read needs it too, or
        # every downstream cost / break-even view shows a blank.
        "entry_price": _to_float(_obj_get(row, "entryPrice")),
        "unrealized_pnl": _to_float(_obj_get(row, "unrealizedPnl")),
        "leverage": _obj_get(row, "leverage"),
        "margin_mode": _obj_get(row, "marginMode"),
    }


def _reject_unsupported_usdm_surface(cfg: BinanceConfig) -> None:
    if cfg.market_type == "usdm":
        raise BinanceConfigError(
            "Binance USD-M Shadow Account exposes account and position reads only"
        )


def read_account_observation(cfg: BinanceConfig, exchange: Any) -> dict[str, Any]:
    """USD-M Shadow Account observation for a live-readonly (Shadow) config.

    SDK-level, config-first adapter over the low-level usdm reader (imported
    here as _read_usdm_observation): it derives the observation's source
    profile / host / tolerance from the connector config and translates
    incoherent-state errors into BinanceConfigError. Only the strict Shadow
    profile (is_usdm_shadow) reaches this path.
    """
    try:
        return _read_usdm_observation(
            exchange,
            source_profile="binance-live-sdk-readonly",
            host=cfg.host,
            now=_utc_now,
            absolute_tolerance=cfg.observation_absolute_tolerance,
        )
    except UsdMObservationError as exc:
        raise BinanceConfigError(str(exc)) from None


def _exchange(cfg: BinanceConfig):
    """Build a ccxt Binance client bound to the configured market/environment."""
    ccxt = _require_ccxt()
    client_config: dict[str, Any] = {
        "apiKey": cfg.api_key,
        "secret": cfg.api_secret,
        "enableRateLimit": True,
        "timeout": int(cfg.timeout * 1000),
        # Signed Binance requests have a narrow timestamp window. Let ccxt
        # measure the exchange clock before the first private request so a
        # sleeping laptop or an imperfect system clock does not force users to
        # reconnect or recreate an otherwise valid read-only API key.
        "options": {
            "adjustForTimeDifference": True,
            "recvWindow": 10_000,
        },
    }
    if cfg.market_type == "usdm" and cfg.is_testnet:
        # ccxt >= 4.5.76 gates the legacy USD-M testnet host behind an explicit
        # opt-out of its deprecation warning (t.me/ccxt_announcements/92; ccxt
        # suggests demo trading). Paper/futures profiles target
        # testnet.binancefuture.com, so accepting the warning is the required,
        # documented opt-in for this supported testnet flow.
        client_config["options"]["disableFuturesSandboxWarning"] = True
    if cfg.market_type == "usdm" and not is_usdm_shadow(cfg):
        # fetch_open_orders without a symbol raises a loud warning on futures
        # (stricter rate limits). The connector deliberately calls it symbol-
        # less and degrades symbol-required failures to a note, so acknowledge
        # the warning explicitly on the tradable futures client.
        client_config["options"]["fetchOpenOrders"] = {"warnWithoutSymbol": False}
    # ``requests``/ccxt does not consistently inherit the macOS System Proxy.
    # urllib resolves both conventional proxy environment variables and the
    # active macOS network proxy, so local desktop connectors follow the same
    # route as the user's browser without persisting proxy details or secrets.
    system_proxies = getproxies()
    proxies = {
        scheme: str(system_proxies[scheme]).strip()
        for scheme in ("http", "https")
        if str(system_proxies.get(scheme) or "").strip()
    }
    if proxies:
        client_config["proxies"] = proxies
    exchange_class = ccxt.binanceusdm if cfg.market_type == "usdm" else ccxt.binance
    ex = exchange_class(client_config)
    ex.set_sandbox_mode(cfg.is_testnet)
    if cfg.market_type == "usdm":
        try:
            assert_exchange_endpoints(ex, allow_testnet=cfg.is_testnet)
        except UsdMObservationError as exc:
            raise BinanceConfigError(str(exc)) from None
    return ex


def _assert_host(cfg: BinanceConfig) -> None:
    """Fail closed when the resolved host does not match the declared environment.

    The host is the authoritative discriminator: testnet keys cannot reach the
    live host. A live profile must resolve to ``api.binance.com``; a paper
    profile must resolve to the configured testnet host.
    """
    host = (urlparse(cfg.host).hostname or cfg.host or "").lower()
    if cfg.is_testnet:
        # Paper host expectations split by market: USD-M paper always targets the
        # futures testnet constant (spot testnet_host must not leak in), while
        # spot paper compares against its own configured testnet host.
        if cfg.market_type == "usdm":
            expected = (urlparse(USDM_TESTNET_HOST).hostname or USDM_TESTNET_HOST or "").lower()
        else:
            expected = (urlparse(cfg.testnet_host).hostname or cfg.testnet_host or "").lower()
        if host != expected:
            raise BinanceConfigError(
                f"Configured profile is paper, but the resolved host '{host}' is not the testnet host '{expected}'."
            )
        return
    expected_url = USDM_LIVE_HOST if cfg.market_type == "usdm" else LIVE_HOST
    expected = urlparse(expected_url).hostname or expected_url
    if host != expected:
        raise BinanceConfigError(
            f"Configured profile is live, but the resolved host '{host}' is not the live host '{expected}'."
        )


def _missing_fields(cfg: BinanceConfig) -> list[str]:
    missing = []
    if not cfg.api_key:
        missing.append("api_key")
    if not cfg.api_secret:
        missing.append("api_secret")
    return missing


def _public_config(cfg: BinanceConfig) -> dict[str, Any]:
    """Config snapshot with secrets redacted."""
    data = asdict(cfg)
    if data.get("api_secret"):
        data["api_secret"] = "***redacted***"
    if data.get("api_key"):
        data["api_key"] = data["api_key"][:4] + "***"
    data["host"] = cfg.host
    return data
