#!/usr/bin/env python3
"""Binance USDⓈ-M 测试网自动信号循环（LLM 分析行情 → 合约下单，含止盈止损）。

与现货版 testnet_signal_loop.py 的区别（合约语义，全部是必需差异）：

  * 品种取 USDⓈ-M 永续（BTC/USDT:USDT），行情也走合约市场；
  * 下单传 quantity + margin_mode + leverage —— 合约不接受现货的 notional
    语义，且必须显式声明保证金模式；
  * 平仓一律带 reduce_only=True，否则反向单会开出一个新仓；
  * 支持做空（现货版只会 buy 加仓 / sell 减仓）；
  * 持仓以券商返回为准（get_positions），本地文件只存峰值/谷值供移动止盈；
  * 止盈止损按标记价判定，名义额下限/最小下单量在本地先校验，避免 -4164。

每轮：
  1. 读合约持仓 → 对每笔持仓做止盈/止损/移动止盈止损，触发即以 reduce_only 平仓；
  2. 抓成交额 top N 永续的 5m K 线；
  3. 调 LLM 出结构化信号（long/short/hold）；
  4. dry-run 只记录；--trade 才真下单（可开多/开空，已有持仓的品种跳过不叠加）。

日志追加写 ~/.vibe-trading/futures_signal_log.jsonl，峰值状态写
~/.vibe-trading/futures_trade_state.json。默认跑 12 轮（每轮 5 分钟 = 1 小时）。
Ctrl-C 安全退出。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent/
sys.path.insert(0, str(ROOT))

READ_PROFILE = "binance-futures-paper-readonly"
TRADE_PROFILE = "binance-futures-paper-trade"
DEFAULT_TOP = 10
DEFAULT_BARS = 100
DEFAULT_MAX_POSITIONS = 5
MAX_NOTIONAL = 1000.0
LOG_PATH = Path.home() / ".vibe-trading" / "futures_signal_log.jsonl"
STATE_PATH = Path.home() / ".vibe-trading" / "futures_trade_state.json"
API_BASE = os.getenv("VIBE_TRADING_API", "http://127.0.0.1:8899")

TG_TOKEN = os.getenv("TESTNET_TG_TOKEN", "")
TG_CHAT = os.getenv("TESTNET_TG_CHAT", "")

# 稳定币基准，不作为交易标的
_STABLE_BASES = frozenset({"USDC", "USDT", "FDUSD", "TUSD", "DAI", "BUSD", "EUR", "AEUR"})

SYSTEM_PROMPT = """You are a crypto USD-M perpetual futures signal generator for a Binance FUTURES TESTNET account.
The account can go LONG and SHORT with leverage, and every position carries a liquidation risk.

For EACH pair in the data below decide:
- side: "long" (open buy exposure), "short" (open sell exposure), or "hold" (do nothing)
- notional: USDT notional for the entry (integer, max {max_notional}); size is quantity = notional / price
- entry: entry price (current last price)
- stop_loss: stop price (long: below entry; short: above entry)
- take_profit: target price (long: above entry; short: below entry)
- confidence: 0.0-1.0
- reason: one short sentence

FIELD LEGEND (one line per symbol; chg/ema/vwap/fund/basis numbers are percentages):
- last: last trade price
- chg5 / chg20 / chgN: percent change over the last 5 / 20 / N bars
- rng: lowest..highest price inside the window
- pos: where last sits inside rng (0.0 = at the low, 1.0 = at the high)
- volRatio: average volume of the last 5 bars versus the rest of the window (above 1 = expanding)
- rsi: RSI(14) over the window (0..100)
- atr%: ATR(14) as a percent of price (volatility scale; a stop closer than this is noise)
- ema20 / ema50: percent distance of last from EMA20 / EMA50
- vwap: percent distance of last from the window VWAP
- fund: current funding rate in percent per funding interval (negative = shorts pay longs)
- nextFund: hours until the next funding settlement
- basis: mark price versus index price in percent (positive = perp rich)
- oi: open interest, oiChg: percent change since the previous round

Rules:
- Prefer setups where several fields agree (trend, position in range, volume, RSI). A single reading is not a signal.
- A funding-rate extreme together with a fast oiChg is a squeeze setup, not a trend.
- Never exceed {max_notional} USDT notional per order.
- Respect liquidation: a stop_loss must be much closer than a realistic liquidation price.
- Only react to clear momentum or reversal setups. When unsure, hold.
- Orders below the exchange minimum notional are skipped by the runner, so do not size below ~100 USDT.

OUTPUT FORMAT (absolute requirement):
Your entire reply must be ONE valid JSON object and NOTHING else.
- No markdown, no code fences, no tables, no headings, no bullets, no explanation.
- The first character of your reply is {{ and the last character is }}.
- Example exactly:
{{"signals": [{{"symbol": "BTC/USDT:USDT", "side": "long", "notional": 200, "entry": 79000.0, "stop_loss": 77500.0, "take_profit": 81500.0, "confidence": 0.7, "reason": "reclaim of 5m range high"}}]}}
"""


def _now() -> str:
    """当前 UTC 时间（秒级 ISO）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(record: dict) -> None:
    """追加一条 JSONL 记录。"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def tg_send(text: str) -> None:
    """推送到 Telegram；未配置时静默跳过，失败不影响交易。"""
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        import httpx

        httpx.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text},
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - 通知失败不能中断交易循环
        pass


def _fmt(value: float | None, digits: int = 4) -> str:
    """格式化价格/金额；None 显示 —。"""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _refresh_portfolio() -> None:
    """触发 Web UI portfolio 快照刷新；失败静默（不影响交易循环）。"""
    try:
        import httpx

        httpx.post(f"{API_BASE}/api/portfolio/refresh", timeout=180)
    except Exception:  # noqa: BLE001 - Web UI 刷新失败不影响交易
        pass


def _signal_msg(sig: dict, result: dict | None = None) -> str:
    """构造 Telegram 推送文本。"""
    side = str(sig.get("side") or "").lower()
    direction = {"long": "开多", "short": "开空"}.get(side, side)
    emoji = "🟢" if side == "long" else "🔴" if side == "short" else ""
    entry = sig.get("entry") or (result or {}).get("price")
    conf = sig.get("confidence")
    conf_s = f"{float(conf) * 100:.0f}%" if conf is not None else "—"
    return (
        f"{emoji} Vibe-Trading 合约测试网：\n"
        f"品种：{sig.get('symbol')}\n"
        f"方向：{direction}\n"
        f"入场：{_fmt(entry)}\n"
        f"止损：{_fmt(sig.get('stop_loss'))}\n"
        f"TP：{_fmt(sig.get('take_profit'))}\n"
        f"置信度：{conf_s}\n"
        f"理由：{sig.get('reason') or ''}"
    )


def load_state() -> dict:
    """返回 {"peaks": {symbol: price}}；缺失或损坏时返回空结构。"""
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("peaks", {})
                data.setdefault("protection", {})
                data.setdefault("open_interest", {})
                return data
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {"peaks": {}, "protection": {}, "open_interest": {}}


def save_state(state: dict) -> None:
    """写回本地状态（父目录自动创建）。"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def fetch_top_symbols(ex, top: int) -> list[str]:
    """成交额 top N 的 USDT 结算永续合约（排除稳定币基准）。"""
    ex.load_markets()
    tickers = ex.fetch_tickers()
    rows = [
        (symbol, ticker.get("quoteVolume") or 0)
        for symbol, ticker in tickers.items()
        if symbol.endswith(":USDT") and (ticker.get("quoteVolume") or 0) > 0
    ]
    rows.sort(key=lambda row: -row[1])
    return [symbol for symbol, _ in rows if symbol.split("/")[0] not in _STABLE_BASES][:top]


def _ema(values: list[float], period: int) -> float | None:
    """指数移动平均（种子 = 前 period 个值的简单平均）。"""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = value * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI（0..100）；样本不足返回 None。"""
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def _atr_pct(bars: list[list[float]], period: int = 14) -> float | None:
    """ATR（Wilder）占现价的百分比，作为波动率刻度。"""
    if len(bars) <= period:
        return None
    true_ranges = []
    for i in range(1, len(bars)):
        high, low, prev_close = bars[i][2], bars[i][3], bars[i - 1][4]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    last = bars[-1][4]
    return atr / last * 100 if last > 0 else None


def _vwap(bars: list[list[float]]) -> float | None:
    """窗口内成交量加权的典型价。"""
    total_volume = sum(bar[5] for bar in bars)
    if total_volume <= 0:
        return None
    weighted = sum(((bar[2] + bar[3] + bar[4]) / 3) * bar[5] for bar in bars)
    return weighted / total_volume


def _pct_change(closes: list[float], lookback: int) -> float | None:
    """最近 lookback 根 K 线的涨跌幅（百分比）。"""
    if lookback <= 0 or len(closes) <= lookback:
        return None
    base = closes[-1 - lookback]
    return (closes[-1] / base - 1) * 100 if base else None


def _rel_pct(value: float | None, reference: float | None) -> float | None:
    """value 相对 reference 的百分比距离。"""
    if value is None or not reference:
        return None
    return (value / reference - 1) * 100


def _context_text(symbol: str, context: dict | None, previous_oi: float | None) -> list[str]:
    """衍生品上下文片段：资金费、下次结算、标记/指数基差、持仓量与跨轮变化。"""
    if not context:
        return []
    parts: list[str] = []
    funding = (context.get("funding") or {}).get(symbol) or {}
    rate = funding.get("funding_rate")
    if rate is not None:
        parts.append(f"fund={rate * 100:+.4f}%")
    next_ms = funding.get("next_funding_time")
    if next_ms:
        hours = (float(next_ms) - time.time() * 1000) / 3_600_000
        if hours > 0:
            parts.append(f"nextFund={hours:.1f}h")
    basis = _rel_pct(funding.get("mark_price"), funding.get("index_price"))
    if basis is not None:
        parts.append(f"basis={basis:+.3f}%")
    oi = (context.get("open_interest") or {}).get(symbol)
    if oi is not None:
        parts.append(f"oi={oi:.4g}")
        if previous_oi:
            parts.append(f"oiChg={(oi / previous_oi - 1) * 100:+.1f}%")
    return parts


def build_market_snapshot(
    symbols: list[str],
    ex,
    *,
    bars_limit: int,
    context: dict | None = None,
    previous_oi: dict | None = None,
) -> tuple[str, dict[str, float]]:
    """逐 symbol 拉 K 线并压成一行特征；返回 (文本, {symbol: 现价})。

    每行的字段：现价、5/20/窗口涨跌幅、窗口高低、现价在区间中的位置、
    近 5 根量能比、RSI、ATR%、距 EMA20/EMA50/VWAP 的百分比距离，以及
    （开启衍生品时）资金费、下次结算、基差、持仓量与跨轮变化。单品种失败
    只跳过该行，不影响整轮。
    """
    lines: list[str] = []
    prices: dict[str, float] = {}
    for symbol in symbols:
        try:
            bars = ex.fetch_ohlcv(symbol, timeframe="5m", limit=bars_limit)
        except Exception:  # noqa: BLE001 - 单品种失败不拖垮整轮
            continue
        if not bars:
            continue
        closes = [bar[4] for bar in bars]
        highs = [bar[2] for bar in bars]
        lows = [bar[3] for bar in bars]
        volumes = [bar[5] for bar in bars]
        last = closes[-1]
        prices[symbol] = last
        window = len(bars)
        highest, lowest = max(highs), min(lows)
        recent_volume = sum(volumes[-5:]) / max(1, len(volumes[-5:]))
        earlier_volume = sum(volumes[:-5]) / max(1, len(volumes[:-5])) if len(volumes) > 5 else recent_volume
        fields = [f"last={last:.4f}"]
        # 窗口涨跌幅 = 首根到末根（lookback 用 window-1，否则 100 根时算不出来）
        for label, lookback in (("chg5", 5), ("chg20", 20), (f"chg{window}", window - 1)):
            change = _pct_change(closes, lookback)
            if change is not None:
                fields.append(f"{label}={change:+.2f}%")
        fields.append(f"rng=[{lowest:.4f},{highest:.4f}]")
        if highest > lowest:
            fields.append(f"pos={(last - lowest) / (highest - lowest):.2f}")
        if earlier_volume:
            fields.append(f"volRatio={recent_volume / earlier_volume:.2f}")
        rsi = _rsi(closes)
        if rsi is not None:
            fields.append(f"rsi={rsi:.1f}")
        atr = _atr_pct(bars)
        if atr is not None:
            fields.append(f"atr%={atr:.2f}")
        for label, period in (("ema20", 20), ("ema50", 50)):
            distance = _rel_pct(last, _ema(closes, period))
            if distance is not None:
                fields.append(f"{label}={distance:+.2f}%")
        vwap_distance = _rel_pct(last, _vwap(bars))
        if vwap_distance is not None:
            fields.append(f"vwap={vwap_distance:+.2f}%")
        fields.extend(_context_text(symbol, context, (previous_oi or {}).get(symbol)))
        lines.append(f"{symbol}: " + " ".join(fields))
    return "\n".join(lines), prices


def parse_signals(text: str) -> list[dict]:
    """解析 LLM 输出：优先严格 JSON，失败回退 markdown 表格。"""
    try:
        start, end = text.index("{"), text.rindex("}")
        payload = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return _parse_markdown_signals(text)
    return payload.get("signals") or []


# 固定单笔金额（保守档，低于 MAX_NOTIONAL 上限）
_FALLBACK_NOTIONAL = 200.0

_CONFIDENCE_MAP = {
    "high": 0.85, "very high": 0.9, "speculative": 0.4,
    "medium-high": 0.7, "medium high": 0.7, "medium": 0.6,
    "low": 0.4, "weak": 0.3, "neutral": 0.3,
}


def _markdown_nums(text: str) -> list[float]:
    """提取字符串中的数字（去千分位逗号）。"""
    return [float(n.replace(",", "")) for n in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", text)]


def _markdown_confidence(text: str) -> float | None:
    """从置信度文字（如 Medium-High）映射到数值。"""
    lowered = text.lower()
    for key, value in _CONFIDENCE_MAP.items():
        if key in lowered:
            return value
    return None


def _parse_markdown_signals(text: str) -> list[dict]:
    """从 markdown 表格提取信号（LLM 不遵守 JSON 时的兜底）。"""
    signals = []
    for line in text.splitlines():
        if "|" not in line or line.strip().startswith("|" * 2) or "---" in line:
            continue
        cells = [cell.strip() for cell in line.split("|") if cell.strip()]
        pair = next(
            (
                cell
                for cell in cells
                if "/" in cell and cell.split("/")[0].isupper() and len(cell.split("/")[0]) <= 12
            ),
            None,
        )
        if not pair:
            continue
        pair = pair.replace("**", "").replace("\x60", "").strip()
        if ":" not in pair:
            # 合约符号必须带结算后缀，否则解析不出 USDT 结算永续
            pair = pair + ":USDT"
        dir_cell = next(
            (
                cell
                for cell in cells
                if any(word in cell.lower() for word in ("long", "short", "neutral", "wait", "avoid", "hold"))
            ),
            None,
        )
        if not dir_cell:
            continue
        direction = dir_cell.lower()
        nums: list[float] = []
        for cell in cells:
            cell_nums = _markdown_nums(cell)
            if not cell_nums:
                continue
            if any(ch in cell for ch in ("–", "—", "~", "-")) and len(cell_nums) > 1:
                cell_nums = cell_nums[:1]
            nums.extend(cell_nums)
        if "short" in direction and "avoid" not in direction:
            side = "short"
        elif "long" in direction and "avoid" not in direction:
            side = "long"
        else:
            side = "hold"
        confidence = None
        for cell in cells:
            confidence = _markdown_confidence(cell)
            if confidence is not None:
                break
        signals.append(
            {
                "symbol": pair,
                "side": side,
                "notional": _FALLBACK_NOTIONAL,
                "entry": nums[0] if len(nums) >= 1 else None,
                "stop_loss": nums[1] if len(nums) >= 2 else None,
                "take_profit": nums[2] if len(nums) >= 3 else None,
                "confidence": confidence,
                "reason": dir_cell,
            }
        )
    return signals


def order_quantity(ex, symbol: str, notional: float, price: float) -> tuple[float | None, str]:
    """把 USDT 名义额换算成合约数量，并先过交易所的最小量与最小名义额。

    Returns:
        (quantity, reason)：成功时 reason 为空；被拒时 quantity 为 None，
        reason 说明原因（避免下单后才吃 Binance -4164 错误）。
    """
    if price <= 0 or notional <= 0:
        return None, "price/notional must be positive"
    market = (ex.markets or {}).get(symbol)
    if not market:
        return None, f"{symbol} is not a loaded market"
    limits = market.get("limits") or {}
    amount_min = (limits.get("amount") or {}).get("min") or 0
    cost_min = (limits.get("cost") or {}).get("min") or 0
    quantity = notional / price
    if amount_min and quantity < float(amount_min):
        return None, f"quantity {quantity:.8f} below min amount {amount_min}"
    if cost_min and quantity * price < float(cost_min):
        return None, f"notional {quantity * price:.2f} below min cost {cost_min}"
    try:
        quantity = float(ex.amount_to_precision(symbol, quantity))
    except Exception:  # noqa: BLE001 - 精度换算失败就用原值，由交易所兜底拒绝
        pass
    if quantity <= 0:
        return None, "quantity rounds to zero"
    if cost_min and quantity * price < float(cost_min):
        return None, f"notional {quantity * price:.2f} below min cost {cost_min} after rounding"
    return quantity, ""


#: 信号给出的止损/止盈点位距入场价的合法区间（百分比）。太近会被交易所
#: 当作无效触发价拒单（或上一根 K 线就扫掉），太远则形同没有保护。
_MIN_LEVEL_DISTANCE_PCT = 0.05
_MAX_LEVEL_DISTANCE_PCT = 60.0


def _distance_pct(entry: float, level: float) -> float:
    """返回点位距入场价的百分比距离（绝对值）。"""
    return abs(level - entry) / entry * 100 if entry > 0 else 0.0


def resolve_exit_levels(
    signal: dict,
    side: str,
    entry: float,
    *,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> tuple[float, float, str]:
    """解析该用哪个止损/止盈价，返回 (stop_price, take_profit_price, source)。

    优先采用 LLM 信号里给出的点位（更贴合形态），但只在结构上合法时：
    方向正确（多头 stop < entry < take_profit，空头相反）、为正、
    且距离落在 _MIN/_MAX_LEVEL_DISTANCE_PCT 之间。任一条件不满足即回退到
    --stop-loss / --take-profit 两个百分比换算出的价位，source 标 "percent"。
    """
    long_side = side != "short"
    if entry <= 0:
        return 0.0, 0.0, "percent"
    pct_stop = entry * (1 - stop_loss_pct / 100) if long_side else entry * (1 + stop_loss_pct / 100)
    pct_target = entry * (1 + take_profit_pct / 100) if long_side else entry * (1 - take_profit_pct / 100)
    try:
        stop = float(signal.get("stop_loss")) if signal.get("stop_loss") is not None else None
        target = float(signal.get("take_profit")) if signal.get("take_profit") is not None else None
    except (TypeError, ValueError):
        return pct_stop, pct_target, "percent"
    if stop is None or target is None or stop <= 0 or target <= 0:
        return pct_stop, pct_target, "percent"
    ordered = stop < entry < target if long_side else target < entry < stop
    if not ordered:
        return pct_stop, pct_target, "percent"
    stop_distance = _distance_pct(entry, stop)
    target_distance = _distance_pct(entry, target)
    if not (_MIN_LEVEL_DISTANCE_PCT <= stop_distance <= _MAX_LEVEL_DISTANCE_PCT):
        return pct_stop, pct_target, "percent"
    if not (_MIN_LEVEL_DISTANCE_PCT <= target_distance <= _MAX_LEVEL_DISTANCE_PCT):
        return pct_stop, pct_target, "percent"
    return stop, target, "signal"


def check_exit(
    position: dict,
    price: float,
    *,
    stop_price: float,
    take_profit: float,
    trailing: float,
) -> str | None:
    """方向感知的止损/止盈/移动止盈判定，返回平仓原因或 None。

    position: {"side": "long"|"short", "entry": price, "peak": price}。
    stop_price / take_profit 是**价格**（来自 LLM 信号或百分比兜底，由
    resolve_exit_levels 决定）；trailing 是移动止盈回撤百分比（多头跟踪
    最高价、空头跟踪最低价），传 0 表示禁用——交易所侧移动止损在跑时由它
    接管，两边不能同时裁决。
    """
    entry = float(position.get("entry") or 0)
    stop = float(stop_price or 0)
    target = float(take_profit or 0)
    if entry <= 0 or price <= 0 or stop <= 0 or target <= 0:
        return None
    side = str(position.get("side") or "long").lower()
    long_side = side != "short"
    if long_side and price <= stop:
        return f"stop-loss {price:.4f} <= {stop:.4f} (entry {entry:.4f})"
    if long_side and price >= target:
        return f"take-profit {price:.4f} >= {target:.4f} (entry {entry:.4f})"
    if not long_side and price >= stop:
        return f"stop-loss {price:.4f} >= {stop:.4f} (entry {entry:.4f})"
    if not long_side and price <= target:
        return f"take-profit {price:.4f} <= {target:.4f} (entry {entry:.4f})"

    if not trailing or trailing <= 0:
        return None

    extreme = float(position.get("peak") or entry)
    if long_side:
        if price > extreme:
            position["peak"] = price
            return None
        drawdown = (extreme - price) / extreme * 100 if extreme > 0 else 0.0
        if extreme > entry and drawdown >= trailing:
            return f"trailing-stop drawdown {drawdown:.1f}% from peak {extreme:.4f}"
        return None
    if price < extreme:
        position["peak"] = price
        return None
    rebound = (price - extreme) / extreme * 100 if extreme > 0 else 0.0
    if extreme < entry and rebound >= trailing:
        return f"trailing-stop rebound {rebound:.1f}% from trough {extreme:.4f}"
    return None


def _broker_positions() -> list[dict]:
    """读合约持仓（以券商返回为准）。"""
    from src.trading.service import get_positions

    payload = get_positions(READ_PROFILE)
    if str(payload.get("status") or "").lower() != "ok":
        raise RuntimeError(str(payload.get("error") or "positions read failed"))
    return list(payload.get("positions") or [])


def _algo_config():
    """条件单所在 profile 的配置（Binance 把它们放在 Algo 服务里）。"""
    from src.trading.connectors.binance.sdk import build_config

    return build_config({"profile": "paper", "market_type": "usdm"})


def _cancel_algo(order_id: str, profile_id: str | None = None, *, symbol: str | None = None) -> dict:
    """撤一条条件单：走 Algo 接口，普通撤单接口对它只会回 -2013。

    签名与 service.cancel_order 对齐（profile_id 忽略——本脚本固定用合约测试网
    profile），这样 disarm_protection 的 cancel 注入点可以直接互换。
    """
    from src.trading.connectors.binance.sdk import cancel_algo_order

    return cancel_algo_order(_algo_config(), order_id, symbol=symbol)


def _read_protection_rows() -> list[dict]:
    """读交易所侧条件单，最多问两次：空答复或读失败都再确认一遍。

    测试网实测出现过「明明挂着却回空列表」以及偶发读失败。把一次空答复当成
    「没挂保护」会重复挂单，把一次读失败当成「没挂单」会漏掉要清理的残留，
    所以两次都不行才下结论（失败即抛，由调用方 fail-closed）。
    """
    from src.trading.connectors.binance.sdk import get_open_algo_orders

    last_error = "algo order read failed"
    for attempt in range(2):
        payload = get_open_algo_orders(_algo_config())
        if isinstance(payload, dict) and str(payload.get("status")) == "ok":
            rows = payload.get("orders") or []
            if rows or attempt == 1:
                return rows
        else:
            last_error = str((payload or {}).get("error") or last_error)
        time.sleep(1)
    raise RuntimeError(last_error)


def _resting_protection_orders() -> dict[str, list[dict]]:
    """返回 {symbol: [条件单行]} —— 交易所上还挂着的 stop/take-profit。

    这些单**不在**普通挂单接口里：Binance 把条件单放进独立的 Algo 服务，
    fetch_open_orders() 永远看不到它们（实测踩过）。所以这里必须用
    get_open_algo_orders()，否则既发现不了已挂的保护，也找不到要撤的兄弟单。
    """
    rows = _read_protection_rows()
    result: dict[str, list[dict]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        order_type = str(row.get("order_type") or "").strip().lower()
        if order_type not in ("stop_market", "take_profit_market"):
            continue
        symbol = str(row.get("symbol") or "")
        if symbol:
            result.setdefault(symbol, []).append(row)
    return result


#: 每个条件单类型在本地状态里对应的键。
_LEG_KEYS = {
    "stop_market": "stop_order_id",
    "take_profit_market": "take_profit_order_id",
    "trailing_stop_market": "trailing_order_id",
}

#: 保护模式 → 要挂在交易所上的条件单类型。
_PROTECTION_LEGS = {
    "fixed": ("stop_market", "take_profit_market"),
    "trailing": ("trailing_stop_market", "take_profit_market"),
    "both": ("stop_market", "trailing_stop_market", "take_profit_market"),
    "off": (),
}


def protection_legs(mode: str) -> tuple[str, ...]:
    """返回该保护模式下应挂在交易所的条件单类型（未知模式按 fixed）。"""
    return _PROTECTION_LEGS.get(str(mode or "").strip().lower(), _PROTECTION_LEGS["fixed"])


def disarm_protection(cancel, state: dict, symbol: str, *, trade: bool,
                      resting_orders: list[dict] | None = None) -> None:
    """撤掉该品种的条件单：本地记录的 id 加上交易所上的残留。"""
    tracked = (state.get("protection") or {}).pop(symbol, None) or {}
    order_ids: dict[str, str] = {}
    for key in _LEG_KEYS.values():
        if tracked.get(key):
            order_ids[str(tracked[key])] = key
    for row in resting_orders or []:
        order_id = str(row.get("order_id") or "")
        if order_id:
            order_ids.setdefault(order_id, str(row.get("order_type") or ""))
    for order_id, label in order_ids.items():
        if not trade:
            _log({"ts": _now(), "symbol": symbol, "status": "dry-run",
                  "detail": f"would cancel {label} {order_id}"})
            continue
        try:
            result = cancel(order_id, TRADE_PROFILE, symbol=symbol)
            _log({"ts": _now(), "symbol": symbol, "status": "cancel", "detail": label,
                  "order_id": order_id, "result": result})
        except Exception as exc:  # noqa: BLE001 - 撤不掉也不能拖垮循环
            _log({"ts": _now(), "symbol": symbol, "status": "error",
                  "detail": f"cancel {label} {order_id}: {exc}"})


def arm_protection(place, state: dict, symbol: str, position: dict, *,
                   mode: str, stop_price: float, take_profit_price: float,
                   trailing_pct: float, margin_mode: str, leverage: int, trade: bool) -> None:
    """按保护模式给持仓挂 reduce_only 条件单（进程死了也有效）。

    fixed    : stop_market + take_profit_market（固定止损 + 止盈）
    trailing : trailing_stop_market（交易所自己跟市价、回调 trailing_pct%）+ 止盈
    both     : 三者都挂（移动单被撤/失效时仍有固定底线）

    所有腿都是 reduce_only，任何一条成交只会减仓、不会反向开仓；兄弟单由
    下一轮的残留清理撤掉（交易所不会自动撤）。
    """
    quantity = abs(float(position.get("quantity") or 0))
    legs = protection_legs(mode)
    if quantity <= 0 or take_profit_price <= 0 or ("stop_market" in legs and stop_price <= 0):
        return
    if "trailing_stop_market" in legs and not (0.1 <= trailing_pct <= 5.0):
        # Binance only accepts a 0.1..5.0 callback; refuse here with a reason
        # instead of letting the exchange reject a half-armed set.
        _log({"ts": _now(), "symbol": symbol, "status": "error",
              "detail": f"trailing {trailing_pct}% outside Binance's 0.1-5.0 callback range"})
        return
    close_side = "sell" if position["side"] == "long" else "buy"
    plan: list[tuple[str, dict, float]] = []
    for order_type in legs:
        if order_type == "take_profit_market":
            plan.append((order_type, {"stop_price": take_profit_price}, take_profit_price))
        elif order_type == "trailing_stop_market":
            plan.append((order_type, {"callback_rate": trailing_pct}, trailing_pct))
        else:
            plan.append((order_type, {"stop_price": stop_price}, stop_price))
    if not trade:
        _log({"ts": _now(), "symbol": symbol, "side": close_side, "quantity": quantity,
              "status": "dry-run", "detail": "would arm exchange-side protection",
              "legs": [item[0] for item in plan], "stop_price": stop_price,
              "take_profit": take_profit_price, "trailing": trailing_pct})
        return
    record: dict = {}
    for order_type, extra, price in plan:
        try:
            result = place(
                symbol, TRADE_PROFILE, side=close_side, quantity=quantity,
                order_type=order_type, reduce_only=True,
                margin_mode=margin_mode, leverage=leverage,
                session_id=f"futures-protect-{int(time.time())}", **extra,
            )
            _log({"ts": _now(), "symbol": symbol, "side": close_side, "quantity": quantity,
                  "status": "protect", "detail": order_type, "price": price,
                  "result": result})
            if str((result or {}).get("status")) == "ok":
                record[_LEG_KEYS[order_type]] = (result or {}).get("order_id")
        except Exception as exc:  # noqa: BLE001 - 挂不上就靠脚本侧兜底
            _log({"ts": _now(), "symbol": symbol, "status": "error",
                  "detail": f"{order_type} at {price}: {exc}"})
    if record:
        state.setdefault("protection", {})[symbol] = {
            **record,
            "mode": mode,
            "stop_price": stop_price,
            "take_profit": take_profit_price,
            "trailing": trailing_pct,
            "quantity": quantity,
        }


def manage_positions(state: dict, prices: dict[str, float], *, trade: bool,
                     stop_loss: float, take_profit: float, trailing: float,
                     margin_mode: str, leverage: int, protection: str,
                     resting: dict | None = None, can_arm: bool = True) -> None:
    """每轮先管持仓：触发止盈止损就以 reduce_only 平仓，未触发则确保有交易所侧保护。"""
    from src.trading.service import place_order

    legs = protection_legs(protection)
    # 交易所侧移动止损在跑时，脚本侧那条就得让位：两边各按自己的极值算，
    # 同时开会互相打架（脚本会先平仓并把交易所单撤掉）。
    script_trailing = 0.0 if "trailing_stop_market" in legs else trailing
    if resting is None:
        resting = _resting_protection_orders() if legs else {}
    for symbol, broker_position in list((state.get("positions") or {}).items()):
        price = prices.get(symbol)
        if not price or price <= 0:
            continue
        tracked = (state.get("protection") or {}).get(symbol) or {}
        if tracked.get("stop_price") and tracked.get("take_profit"):
            stop_price = float(tracked["stop_price"])
            target = float(tracked["take_profit"])
        else:
            # 首次接管或本地文件丢失：用 --stop-loss/--take-profit 从入场价换算
            stop_price, target, _ = resolve_exit_levels(
                {},
                broker_position["side"],
                float(broker_position["entry"]),
                stop_loss_pct=stop_loss,
                take_profit_pct=take_profit,
            )
        held = {
            "side": broker_position["side"],
            "entry": broker_position["entry"],
            "peak": (state.get("peaks") or {}).get(symbol) or broker_position["entry"],
        }
        reason = check_exit(held, price, stop_price=stop_price, take_profit=target, trailing=script_trailing)
        state.setdefault("peaks", {})[symbol] = held["peak"]
        if not reason:
            if not legs or not can_arm:
                # 读不到保护状态时不动保护（不重复挂、也不清理），但脚本侧
                # 的止盈止损照旧生效——平仓不该被一个读失败拖住。
                continue
            held_orders = resting.get(symbol) or []
            have = {str(row.get("order_type") or "").strip().lower() for row in held_orders}
            if have == set(legs):
                continue  # 该模式要的腿都在，无需动作
            if have:
                # 只剩部分腿（兄弟单成交/被手动撤掉）：先清干净再重挂，避免残缺状态
                disarm_protection(_cancel_algo, state, symbol, trade=trade, resting_orders=held_orders)
            arm_protection(
                place_order, state, symbol, broker_position,
                mode=protection, stop_price=stop_price, take_profit_price=target,
                trailing_pct=trailing, margin_mode=margin_mode, leverage=leverage,
                trade=trade,
            )
            continue
        # 触发：先撤交易所侧挂单，再市价平仓（避免两边同时动作）
        disarm_protection(_cancel_algo, state, symbol, trade=trade, resting_orders=resting.get(symbol))
        quantity = abs(float(broker_position["quantity"]))
        if quantity <= 0:
            continue
        close_side = "sell" if held["side"] == "long" else "buy"
        if not trade:
            _log({"ts": _now(), "symbol": symbol, "side": close_side, "quantity": quantity,
                  "status": "signal", "reason": reason})
            continue
        try:
            result = place_order(
                symbol, TRADE_PROFILE, side=close_side, quantity=quantity,
                order_type="market", margin_mode=margin_mode, leverage=leverage,
                reduce_only=True, session_id=f"futures-exit-{int(time.time())}",
            )
            _log({"ts": _now(), "symbol": symbol, "side": close_side, "quantity": quantity,
                  "status": "order", "reason": reason, "result": result})
            if str(result.get("status")) == "ok":
                tg_send(f"🔴 平仓 {symbol}  {quantity:.6f}  — {reason}")
        except Exception as exc:  # noqa: BLE001 - 单笔失败不终止循环
            _log({"ts": _now(), "symbol": symbol, "side": close_side, "quantity": quantity,
                  "status": "error", "reason": reason, "detail": str(exc)})


def run_round(ex, llm, *, trade: bool, top: int, symbols_arg: list[str],
              stop_loss: float, take_profit: float, trailing: float,
              margin_mode: str, leverage: int, protection: str = "fixed",
              bars_limit: int = DEFAULT_BARS, max_positions: int = DEFAULT_MAX_POSITIONS,
              derivatives: bool = True) -> None:
    """跑一轮：先管持仓，再取信号，再（可选）开仓。"""
    from src.trading.service import place_order

    state = load_state()

    # 1) 持仓真相来自券商；读不到就不动仓（fail-closed）
    try:
        raw_positions = _broker_positions()
    except Exception as exc:  # noqa: BLE001
        _log({"ts": _now(), "round": "error", "detail": f"positions: {exc}"})
        return
    state["positions"] = {
        str(row.get("symbol")): {
            "side": str(row.get("side") or ("short" if float(row.get("quantity") or 0) < 0 else "long")),
            "quantity": float(row.get("quantity") or 0),
            "entry": float(row.get("entry_price") or row.get("price") or 0),
        }
        for row in raw_positions
        if row.get("symbol") and float(row.get("quantity") or 0) != 0
    }

    # 1b) 交易所侧残留：仓位已经没了，止损/止盈还挂在那儿 → 撤掉
    resting: dict[str, list[dict]] = {}
    protection_readable = True
    if protection_legs(protection):
        try:
            resting = _resting_protection_orders()
        except Exception as exc:  # noqa: BLE001
            # 读不到就不碰保护：本轮不清理、不重挂、也不开新仓（新仓会失去
            # 交易所侧保护）。已有仓位的脚本侧止盈止损继续生效。
            protection_readable = False
            _log({"ts": _now(), "round": "warn", "detail": f"algo orders unreadable: {exc}"})
        for symbol in [sym for sym in resting if sym not in state["positions"]]:
            _log({"ts": _now(), "symbol": symbol, "status": "cleanup",
                  "detail": "position closed; cancelling leftover protection"})
            disarm_protection(_cancel_algo, state, symbol, trade=trade,
                              resting_orders=resting.get(symbol))

    # 2) 选品 → 衍生品上下文 → 特征快照
    symbols = symbols_arg or fetch_top_symbols(ex, top)
    context: dict | None = None
    if derivatives:
        try:
            from src.trading.connectors.binance.sdk import get_futures_context

            context = get_futures_context(_algo_config(), symbols)
            if str(context.get("status")) != "ok":
                _log({"ts": _now(), "round": "warn",
                      "detail": f"derivatives: {context.get('error')}"})
                context = None
        except Exception as exc:  # noqa: BLE001 - 上下文缺失不该阻挡交易
            _log({"ts": _now(), "round": "warn", "detail": f"derivatives: {exc}"})
            context = None
    snapshot, prices = build_market_snapshot(
        symbols,
        ex,
        bars_limit=bars_limit,
        context=context,
        previous_oi=state.get("open_interest") or {},
    )
    if not snapshot:
        _log({"ts": _now(), "round": "error", "detail": "no market data"})
        save_state(state)
        return
    if context and context.get("open_interest"):
        # 下一轮的 oiChg 就是拿这一轮的值做基准（测试网没有 fapiData 历史接口）
        state["open_interest"] = {**(state.get("open_interest") or {}), **context["open_interest"]}

    # 持仓品种可能不在 top 列表里：补一次报价，否则止盈止损检查不到
    for symbol in list(state["positions"]):
        if symbol in prices:
            continue
        try:
            ticker = ex.fetch_ticker(symbol)
            last = float(ticker.get("last") or 0)
            if last > 0:
                prices[symbol] = last
        except Exception:  # noqa: BLE001 - 单品种拉价失败跳过
            pass

    manage_positions(
        state, prices, trade=trade, stop_loss=stop_loss, take_profit=take_profit,
        trailing=trailing, margin_mode=margin_mode, leverage=leverage,
        protection=protection, resting=resting, can_arm=protection_readable,
    )
    save_state(state)

    # 3) 取信号
    prompt = (
        SYSTEM_PROMPT.format(max_notional=int(MAX_NOTIONAL))
        + f"\n\nCurrent time (UTC): {_now()}\n"
        + f"Top {len(symbols)} USD-M perpetuals by 24h volume:\n{snapshot}\n"
        + "Generate trading signals now."
    )
    try:
        reply = llm.invoke(prompt)
        text = reply.content if hasattr(reply, "content") else str(reply)
    except Exception as exc:  # noqa: BLE001 - 一次失败不能拖死循环
        _log({"ts": _now(), "round": "error", "detail": f"llm: {exc}"})
        return

    signals = parse_signals(text)
    if not signals:
        _log({"ts": _now(), "round": "error", "detail": "llm returned no parseable signals"})
        return

    # 4) 执行
    for signal in signals:
        symbol = str(signal.get("symbol") or "").strip()
        side = str(signal.get("side") or "hold").strip().lower()
        reason = str(signal.get("reason") or "")
        try:
            notional = float(signal.get("notional") or 0)
        except (TypeError, ValueError):
            notional = 0.0

        if symbol not in symbols:
            _log({"ts": _now(), "symbol": symbol, "side": side, "status": "rejected",
                  "detail": "not in universe"})
            continue
        if side == "buy":
            side = "long"
        elif side == "sell":
            side = "short"
        if side not in ("long", "short"):
            _log({"ts": _now(), "symbol": symbol, "side": side, "status": "hold", "reason": reason})
            continue
        if symbol in state["positions"]:
            # 已有持仓不叠加：仓位管理交给止盈止损，避免越亏越加
            _log({"ts": _now(), "symbol": symbol, "side": side, "status": "skipped",
                  "detail": "position already open"})
            continue
        if not protection_readable:
            _log({"ts": _now(), "symbol": symbol, "side": side, "status": "skipped",
                  "detail": "protection state unreadable; not opening unprotected positions"})
            break
        if len(state["positions"]) >= max_positions:
            # 品种池可以放大，但同时在手的仓位数必须有闸：否则一轮就可能把
            # 保证金铺满（测试网账户 ~4800 USDT，10 个 300 名义仓 = 3000）。
            _log({"ts": _now(), "symbol": symbol, "side": side, "status": "skipped",
                  "detail": f"position cap {max_positions} reached"})
            break

        notional = min(max(notional, 0.0), MAX_NOTIONAL)
        price = prices.get(symbol) or float(signal.get("entry") or 0)
        quantity, reject = order_quantity(ex, symbol, notional, price)
        if quantity is None:
            _log({"ts": _now(), "symbol": symbol, "side": side, "notional": notional,
                  "status": "rejected", "detail": reject})
            continue

        if not trade:
            _log({"ts": _now(), "symbol": symbol, "side": side, "notional": notional,
                  "quantity": quantity, "status": "signal", "reason": reason})
            continue

        order_side = "buy" if side == "long" else "sell"
        try:
            result = place_order(
                symbol, TRADE_PROFILE, side=order_side, quantity=quantity,
                order_type="market", margin_mode=margin_mode, leverage=leverage,
                session_id=f"futures-loop-{int(time.time())}",
            )
            _log({"ts": _now(), "symbol": symbol, "side": side, "quantity": quantity,
                  "status": "order", "reason": reason, "result": result})
            if str(result.get("status")) == "ok":
                tg_send(_signal_msg(signal, result))
                filled = float(result.get("filled") or 0) or quantity
                entry = float(result.get("price") or price or 0)
                if entry > 0 and filled > 0:
                    position = {"side": side, "quantity": filled, "entry": entry}
                    state["positions"][symbol] = position
                    state.setdefault("peaks", {})[symbol] = entry
                    if protection_legs(protection):
                        # 立刻把保护挂到交易所：进程死了它也还在
                        stop_price, target, source = resolve_exit_levels(
                            signal, side, entry,
                            stop_loss_pct=stop_loss, take_profit_pct=take_profit,
                        )
                        _log({"ts": _now(), "symbol": symbol, "status": "levels",
                              "detail": source, "stop_price": stop_price,
                              "take_profit": target})
                        arm_protection(
                            place_order, state, symbol, position,
                            mode=protection, stop_price=stop_price,
                            take_profit_price=target, trailing_pct=trailing,
                            margin_mode=margin_mode, leverage=leverage, trade=trade,
                        )
        except Exception as exc:  # noqa: BLE001 - 单笔失败继续循环
            _log({"ts": _now(), "symbol": symbol, "side": side, "quantity": quantity,
                  "status": "error", "detail": str(exc)})

    save_state(state)
    _refresh_portfolio()


def _synth_bars(count: int = 100) -> list[list[float]]:
    """构造一段震荡上行、量能递增的 OHLCV，供指标自测使用。"""
    bars = []
    for i in range(count):
        base = 100.0 + i * 0.1 + (1.0 if i % 3 == 0 else -0.5)
        bars.append([float(i), base - 0.2, base + 0.4, base - 0.5, base, float(i + 1)])
    return bars


class _FakeBarsExchange:
    """只实现 build_market_snapshot 需要的 fetch_ohlcv。"""

    def __init__(self, bars):
        self._bars = bars

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        return self._bars[-limit:] if limit else self._bars


class _FakeMarketExchange:
    """自测用假交易所：只实现 order_quantity 需要的最小接口。"""

    def __init__(self, markets, precision=None):
        self.markets = markets
        self._precision = precision

    def amount_to_precision(self, symbol, amount):
        """按给定步长向下取整（模拟 ccxt 的精度换算）。"""
        step = self._precision
        if not step:
            return f"{amount:.8f}"
        return f"{float(int(amount / step)) * step:.8f}"


def _selftest() -> None:
    """离线自测：方向感知的止损/止盈、点位解析、名义额/最小量校验（不触网）。"""
    # 多头：价格跌破止损价
    long_pos = {"side": "long", "entry": 100.0}
    assert check_exit(long_pos, 94.0, stop_price=95.0, take_profit=108.0, trailing=3)
    # 多头：未跌破止损也未到止盈
    assert check_exit(long_pos, 96.0, stop_price=95.0, take_profit=108.0, trailing=3) is None
    # 多头：触及止盈
    assert check_exit(long_pos, 109.0, stop_price=95.0, take_profit=108.0, trailing=3)
    # 空头：涨过止损价才是亏损
    short_pos = {"side": "short", "entry": 100.0}
    assert check_exit(short_pos, 106.0, stop_price=105.0, take_profit=92.0, trailing=3)
    # 空头：触及止盈
    assert check_exit(short_pos, 91.0, stop_price=105.0, take_profit=92.0, trailing=3)
    # 空头：中间区域不动
    assert check_exit(short_pos, 96.0, stop_price=105.0, take_profit=92.0, trailing=3) is None

    # 移动止盈（多头）：peak 120 → 115（回撤 4.2% > 3）
    assert check_exit({"side": "long", "entry": 115.0, "peak": 120.0}, 115.0,
                      stop_price=100.0, take_profit=130.0, trailing=3)
    # 未创新高前（peak == entry）不因小回撤离场
    assert check_exit({"side": "long", "entry": 100.0}, 97.0,
                      stop_price=90.0, take_profit=120.0, trailing=3) is None
    # 创新高：peak 上移且不触发
    rising = {"side": "long", "entry": 112.0, "peak": 112.0}
    assert check_exit(rising, 114.0, stop_price=100.0, take_profit=130.0, trailing=3) is None
    assert rising["peak"] == 114.0

    # 移动止盈（空头）：谷值 80 → 84（反弹 5% > 3）
    assert check_exit({"side": "short", "entry": 85.0, "peak": 80.0}, 84.0,
                      stop_price=90.0, take_profit=75.0, trailing=3)
    # 创新低：谷值下移且不触发
    falling = {"side": "short", "entry": 85.0, "peak": 85.0}
    assert check_exit(falling, 82.0, stop_price=90.0, take_profit=75.0, trailing=3) is None
    assert falling["peak"] == 82.0

    # 点位解析：合法的信号价优先采用
    stop, target, source = resolve_exit_levels(
        {"stop_loss": 95.0, "take_profit": 110.0}, "long", 100.0,
        stop_loss_pct=5, take_profit_pct=8,
    )
    assert source == "signal" and (stop, target) == (95.0, 110.0)
    stop, target, source = resolve_exit_levels(
        {"stop_loss": 105.0, "take_profit": 90.0}, "short", 100.0,
        stop_loss_pct=5, take_profit_pct=8,
    )
    assert source == "signal" and (stop, target) == (105.0, 90.0)
    # 方向反了 → 回退百分比
    stop, target, source = resolve_exit_levels(
        {"stop_loss": 105.0, "take_profit": 90.0}, "long", 100.0,
        stop_loss_pct=5, take_profit_pct=8,
    )
    assert source == "percent" and (round(stop, 4), round(target, 4)) == (95.0, 108.0)
    # 缺失点位 → 回退百分比
    stop, target, source = resolve_exit_levels({}, "long", 100.0, stop_loss_pct=5, take_profit_pct=8)
    assert source == "percent" and (round(stop, 4), round(target, 4)) == (95.0, 108.0)
    # 贴得太近（距离 < 0.05%）→ 回退百分比
    stop, target, source = resolve_exit_levels(
        {"stop_loss": 99.99, "take_profit": 110.0}, "long", 100.0,
        stop_loss_pct=5, take_profit_pct=8,
    )
    assert source == "percent"
    # 非数字 → 回退百分比
    stop, target, source = resolve_exit_levels(
        {"stop_loss": "soon", "take_profit": None}, "long", 100.0,
        stop_loss_pct=5, take_profit_pct=8,
    )
    assert source == "percent"

    # 保护模式 → 该挂哪几条腿
    assert protection_legs("fixed") == ("stop_market", "take_profit_market")
    assert protection_legs("trailing") == ("trailing_stop_market", "take_profit_market")
    assert protection_legs("both") == ("stop_market", "trailing_stop_market", "take_profit_market")
    assert protection_legs("off") == ()
    assert protection_legs("nonsense") == ("stop_market", "take_profit_market")

    # 交易所侧移动止损在跑时，脚本侧那条必须让位（trailing=0 即禁用）
    armed = {"side": "long", "entry": 115.0, "peak": 120.0}
    assert check_exit(armed, 115.0, stop_price=100.0, take_profit=130.0, trailing=0) is None
    assert check_exit({"side": "long", "entry": 115.0, "peak": 120.0}, 115.0,
                      stop_price=100.0, take_profit=130.0, trailing=3) is not None

    # 数量换算：名义额 → 数量，按精度取整
    ex = _FakeMarketExchange(
        {"BTC/USDT:USDT": {"limits": {"amount": {"min": 0.001}, "cost": {"min": 100}}}},
        precision=0.001,
    )
    quantity, reason = order_quantity(ex, "BTC/USDT:USDT", 200.0, 80000.0)
    assert reason == "" and abs(quantity - 0.002) < 1e-9, (quantity, reason)
    # 数量达标但名义额低于交易所下限 → 拒单并说明原因（避免 -4164）
    quantity, reason = order_quantity(ex, "BTC/USDT:USDT", 80.0, 80000.0)
    assert quantity is None and "min cost" in reason, (quantity, reason)
    # 数量低于最小下单量 → 拒单
    quantity, reason = order_quantity(ex, "BTC/USDT:USDT", 50.0, 80000.0)
    assert quantity is None and "min amount" in reason, (quantity, reason)
    # 未知品种 → 拒单
    quantity, reason = order_quantity(ex, "NOPE/USDT:USDT", 200.0, 10.0)
    assert quantity is None and "not a loaded market" in reason

    # 指标：纯函数，用构造序列验证
    rising = [float(i) for i in range(1, 101)]
    assert _ema(rising, 10) is not None and _ema(rising, 10) < rising[-1]
    assert _rsi(rising) == 100.0                      # 全涨 → 100
    assert _rsi([float(100 - i) for i in range(100)]) == 0.0   # 全跌 → 0
    assert abs(_pct_change(rising, 10) - (100 / 90 - 1) * 100) < 1e-9
    assert _pct_change(rising, 200) is None           # 样本不足
    assert _atr_pct(_synth_bars()) is not None
    assert abs(_rel_pct(110.0, 100.0) - 10.0) < 1e-9
    assert _rel_pct(None, 100.0) is None

    # 快照行：新字段齐、价格表正确、上下文可拼进来
    ex = _FakeBarsExchange(_synth_bars())
    text, prices = build_market_snapshot(["BTC/USDT:USDT"], ex, bars_limit=100)
    assert prices == {"BTC/USDT:USDT": prices["BTC/USDT:USDT"]} and "BTC/USDT:USDT" in prices
    for field in ("last=", "chg5=", "rng=[", "pos=", "volRatio=", "rsi=", "atr%=", "ema20=", "vwap="):
        assert field in text, (field, text[:200])
    assert "prev20_min" not in text                   # 名不副实的字段已去掉
    context = {"funding": {"BTC/USDT:USDT": {"funding_rate": -0.0001, "mark_price": 101.0, "index_price": 100.0}},
               "open_interest": {"BTC/USDT:USDT": 110.0}}
    text, _ = build_market_snapshot(["BTC/USDT:USDT"], ex, bars_limit=100,
                                    context=context, previous_oi={"BTC/USDT:USDT": 100.0})
    for field in ("fund=", "basis=", "oi=", "oiChg="):
        assert field in text, (field, text[:220])

    print("selftest OK")


class _RoundTimeout(Exception):
    """单轮超过硬时限。"""


def _round_timeout_handler(signum, frame):  # noqa: ARG001
    """SIGALRM 处理器：抛出单轮超时。"""
    raise _RoundTimeout(f"round exceeded {signum} timeout")


def main() -> int:
    """命令行入口。"""
    import signal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade", action="store_true", help="真实下单（默认 dry-run 只记录信号）")
    parser.add_argument("--interval", type=int, default=300, help="轮询间隔秒（默认 300）")
    parser.add_argument("--runs", type=int, default=12, help="最大轮数（默认 12 = 1 小时）")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"成交额 top N（默认 {DEFAULT_TOP}）")
    parser.add_argument("--bars", type=int, default=DEFAULT_BARS,
                        help=f"每个品种拉多少根 5m K 线（默认 {DEFAULT_BARS}）")
    parser.add_argument("--max-positions", dest="max_positions", type=int, default=DEFAULT_MAX_POSITIONS,
                        help=f"同时在手的最大仓位数（默认 {DEFAULT_MAX_POSITIONS}）")
    parser.add_argument("--no-derivatives", action="store_true",
                        help="不拉资金费/持仓量等衍生品上下文")
    parser.add_argument("--symbols", default="", help="逗号分隔的固定品种，覆盖 top N 选品")
    parser.add_argument("--stop-loss", type=float, default=5.0, help="止损百分比（默认 5）")
    parser.add_argument("--take-profit", type=float, default=8.0, help="止盈百分比（默认 8）")
    parser.add_argument("--trailing", type=float, default=3.0, help="移动止盈回撤百分比（默认 3）")
    parser.add_argument("--leverage", type=int, default=5, help="杠杆倍数（默认 5）")
    parser.add_argument("--margin-mode", default="isolated", choices=("isolated", "cross"),
                        help="保证金模式（默认 isolated）")
    parser.add_argument("--protection", default="fixed", choices=("fixed", "trailing", "both", "off"),
                        help="交易所侧保护：fixed=固定止损+止盈（默认）、trailing=移动止损+止盈"
                             "（推荐）、both=三者都挂、off=不挂")
    parser.add_argument("--no-protection", action="store_true",
                        help="等同 --protection off（只靠脚本轮询止损）")
    parser.add_argument("--selftest", action="store_true", help="运行离线自测（不下单不触网）")
    args = parser.parse_args()

    if args.selftest:
        _selftest()
        return 0

    from src.providers.llm import build_llm
    from src.trading.connectors.binance.sdk import _exchange, build_config

    protection_mode = "off" if args.no_protection else args.protection
    if "trailing_stop_market" in protection_legs(protection_mode) and not (0.1 <= args.trailing <= 5.0):
        print(f"[futures-loop] --trailing {args.trailing}% 超出 Binance 的回调区间 0.1~5.0，"
              f"请改小或改用 --protection fixed")
        return 2

    symbols_arg = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    llm = build_llm()
    ex = _exchange(build_config({"profile": "paper", "market_type": "usdm"}))
    print(f"[futures-loop] LLM 就绪 | {'TRADE 模式' if args.trade else 'dry-run 模式'} | "
          f"每 {args.interval}s 一轮 | 最多 {args.runs} 轮")
    print(f"[futures-loop] 读 profile {READ_PROFILE} | 下单 profile {TRADE_PROFILE}")
    print(f"[futures-loop] {args.margin_mode} {args.leverage}x | 止损 {args.stop_loss}% | "
          f"止盈 {args.take_profit}% | 移动止盈回撤 {args.trailing}% | "
          f"交易所侧保护 {protection_mode}"
          + (f"（{'/'.join(protection_legs(protection_mode))}）" if protection_legs(protection_mode) else ""))
    print(f"[futures-loop] 品种 top {args.top} | 每品种 {args.bars} 根 5m | 最多同时 {args.max_positions} 仓 | "
          f"衍生品上下文 {'关' if args.no_derivatives else '开'}")
    print(f"[futures-loop] 日志: {LOG_PATH}")
    print(f"[futures-loop] 峰值状态: {STATE_PATH}")

    round_timeout = max(10, args.interval - 30)
    signal.signal(signal.SIGALRM, _round_timeout_handler)

    try:
        for index in range(args.runs):
            try:
                signal.alarm(round_timeout)
                run_round(
                    ex, llm, trade=args.trade, top=args.top, symbols_arg=symbols_arg,
                    stop_loss=args.stop_loss, take_profit=args.take_profit,
                    trailing=args.trailing, margin_mode=args.margin_mode,
                    leverage=args.leverage, protection=protection_mode,
                    bars_limit=args.bars, max_positions=args.max_positions,
                    derivatives=not args.no_derivatives,
                )
            except _RoundTimeout as exc:
                _log({"ts": _now(), "round": "timeout", "detail": f"round timed out after {round_timeout}s"})
                tg_send(f"⚠️ 第 {index + 1} 轮超时（已跳过）：{exc}")
            except Exception as exc:  # noqa: BLE001 - 单轮任何异常都不能终止循环
                _log({"ts": _now(), "round": "error", "detail": f"round failed: {exc}"})
                tg_send(f"⚠️ 第 {index + 1} 轮异常（已跳过）：{exc}")
            finally:
                signal.alarm(0)
            if index < args.runs - 1:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[futures-loop] 已停止（Ctrl-C）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
