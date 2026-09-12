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
  2. 定品种池（成交额 top N 或 --symbols 固定名单），抓它们的 5m K 线；
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
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent/
sys.path.insert(0, str(ROOT))

READ_PROFILE = "binance-futures-paper-readonly"
TRADE_PROFILE = "binance-futures-paper-trade"
DEFAULT_TOP = 10
DEFAULT_BARS = 100
DEFAULT_MAX_POSITIONS = 10
MAX_NOTIONAL = 1000.0

#: 同品种止损后的冷静期（小时）。实测最集中的亏损来自「同一品种被反复止损又立刻
#: 再进」：18h 窗口里 ADA 一个品种这样贡献了 -15.7，而按 6h 冷却回放能省下约 12。
COOLDOWN_HOURS = 6.0

#: 多头顺势闸的参考品种：它自己走弱时不开多。
REGIME_SYMBOL = "BTC/USDT:USDT"

#: 止损距离下限（× ATR(14,5m)）。LLM 给的止损常只有 0.07%~1.8%，落在噪声里；
#: 夹到 2×ATR 后，脚本判定价与交易所挂单价一致，且不再贴着噪声。0 = 关闭。
STOP_FLOOR_ATR = 2.0
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


def _as_float(value: object) -> float | None:
    """把外部来源（状态文件 / 券商 / LLM）的数值转成 float；非数值返回 None。

    这些值会直接进入 ``<= 0`` 一类的守卫，而 float() 或比较抛出的 TypeError 会
    穿出 manage_positions 一路冒到 main 的「单轮异常」处理器 —— 整轮作废，所有
    品种的持仓管理一起跳过（实测：保护记录里 stop_price 是个对象就够触发）。
    脏值必须在这里退化成「按缺失处理」，而不是把一轮的交易全丢掉。
    bool 不算数值：True 当 1.0 用只会把脏数据掩盖过去。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _traceback_tail(limit: int = 12, max_chars: int = 2000) -> str:
    """当前异常的栈（截断）。只在 except 块里调用才有意义。"""
    return "".join(traceback.format_exc(limit=limit))[-max_chars:]


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
    """返回本地状态；缺失或损坏时返回空结构。

    结构：{"peaks": {symbol: 极值}, "protection": {symbol: 已挂腿},
    "open_interest": {...}, "cooldowns": {symbol: 上次止损时间}}。
    """
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("peaks", {})
                data.setdefault("protection", {})
                data.setdefault("open_interest", {})
                data.setdefault("cooldowns", {})
                return data
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {"peaks": {}, "protection": {}, "open_interest": {}, "cooldowns": {}}


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


#: 触发冷却的平仓原因前缀（脚本侧止损/移动止损）。止盈平仓不冷却。
_COOLDOWN_REASONS = ("stop-loss", "trailing-stop")


def _hours_since(iso_ts: str, now_s: float | None = None) -> float | None:
    """距今多少小时；时间戳不可解析时返回 None。"""
    try:
        parsed = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now_dt = (
        datetime.fromtimestamp(now_s, timezone.utc) if now_s is not None
        else datetime.now(timezone.utc)
    )
    return (now_dt - parsed).total_seconds() / 3600.0


def _exchange_side_stop_reason(tracked: object, resting_rows: list[dict] | None) -> str:
    """仓位消失是不是交易所侧止损打掉的？判得了返回冷却原因，判不了返回空串。

    只认能确证的那一种签名：本地记录里止损/移动止损的 id 已不在交易所的未成交
    条件单里（成交或消失了），而止盈腿还挂着。止盈一旦成交会把止盈单消耗掉，
    所以「止盈还在 + 止损不在」只能是止损成交 —— 这正是脚本自己没参与、因此
    从没记过冷却的那一类（protection=both 下绝大多数止损都走这条路）。

    证据不足一律返回空串：条件单列表在测试网会偶发读丢，宁可漏一次冷却，也不
    拿残缺的列表去冷却一个没止损的品种（白等 6 小时）。
    """
    if not isinstance(tracked, dict):
        return ""
    stop_ids = {str(tracked[key]) for key in ("stop_order_id", "trailing_order_id") if tracked.get(key)}
    tp_id = tracked.get("take_profit_order_id")
    if not stop_ids or not tp_id:
        return ""
    open_ids = {
        str(row.get("order_id"))
        for row in (resting_rows or [])
        if isinstance(row, dict) and row.get("order_id") is not None
    }
    if stop_ids & open_ids:
        return ""                       # 止损腿还挂着 → 这次平仓不是它干的
    if str(tp_id) not in open_ids:
        return ""                       # 止盈也没了 → 分不清哪条腿成交
    return "stop-loss (exchange-side fill)"


def _record_cooldown(state: dict, symbol: str, reason: str) -> None:
    """止损类平仓后给该品种上冷却（止盈不算）。"""
    if not any(str(reason).startswith(prefix) for prefix in _COOLDOWN_REASONS):
        return
    state.setdefault("cooldowns", {})[symbol] = _now()


def _cooldown_reason(state: dict, symbol: str, cooldown_hours: float,
                    now_s: float | None = None) -> str:
    """同品种止损冷却闸：返回拦截原因，放行返回空串。"""
    if cooldown_hours <= 0:
        return ""
    recorded = (state.get("cooldowns") or {}).get(symbol)
    if not recorded:
        return ""
    age = _hours_since(recorded, now_s)
    if age is None or age < 0:
        return ""
    if age < cooldown_hours:
        return f"cooldown {age:.1f}h/{cooldown_hours:g}h since last stop-out"
    return ""


def _regime_verdict(closes: list[float]) -> tuple[bool, str]:
    """多头顺势闸判定（纯函数）：参考品种要在 EMA50 上方且窗口内为正。"""
    if len(closes) < 51:
        return False, "regime: not enough bars"
    ema50 = _ema(closes, 50)
    last = closes[-1]
    if ema50 is None or ema50 <= 0:
        return False, "regime: EMA50 unavailable"
    change = _pct_change(closes, min(99, len(closes) - 1))
    if last <= ema50:
        return False, f"regime: px {last:.6g} <= EMA50 {ema50:.6g}"
    if change is None or change <= 0:
        return False, f"regime: window change {0.0 if change is None else change:+.2f}% <= 0"
    return True, f"regime ok: px/EMA50 {_rel_pct(last, ema50):+.2f}%, chg {change:+.2f}%"


def _long_regime_reason(ex, symbol: str = REGIME_SYMBOL) -> str:
    """多头闸取数：读不到就拦（宁可不开多，也不在弱势里开多）。"""
    try:
        bars = ex.fetch_ohlcv(symbol, timeframe="5m", limit=100)
    except Exception as exc:  # noqa: BLE001 - 读不到按拦截处理
        return f"regime: {symbol} unreadable ({exc})"
    closes = [bar[4] for bar in (bars or [])]
    ok, detail = _regime_verdict(closes)
    return "" if ok else detail


def _universe_header(*, n: int, fixed: bool) -> str:
    """拼 prompt 里的品种池标题，按真实来源措辞。

    固定 ``--symbols`` 与成交额 top N 是两个不同的池子，措辞不能混：早先这里
    写死 "top N by 24h volume"，传 ``--symbols`` 时会误导 LLM 以为手上这十个
    是成交额排名。而测试网上成交额是假的、排名毫无意义（见 launchd/README）。

    Args:
        n: 本轮实际进入快照的品种数。
        fixed: True = 来自 ``--symbols`` 的固定名单；False = 成交额 top N。

    Returns:
        一行标题，把「N 个 USD-M 永续」的真实来源讲清楚。
    """
    if fixed:
        return f"Tradable universe ({n} USD-M perpetuals, fixed watchlist):"
    return f"Top {n} USD-M perpetuals by 24h quote volume:"


def apply_stop_floor(side: str, entry: float, stop_price: float,
                     atr_pct: float | None, multiple: float) -> tuple[float, str]:
    """把止损距离撑到至少 multiple × ATR%；返回 (止损价, 说明)。

    为什么要这道夹子：LLM 给的止损经常只有 0.07%~1.8%，而持仓中位 55 分钟 ≈ 11 根
    5m 的期望波动约 3.3×ATR —— 止损落在噪声里，被扫是大概率。夹过的价位会同时
    写进 state 和交易所挂单，保证「脚本判定价 == 交易所挂单价」。
    """
    try:
        atr_value = float(atr_pct)
    except (TypeError, ValueError):
        return stop_price, ""
    if multiple <= 0 or atr_value <= 0 or entry <= 0 or stop_price <= 0:
        return stop_price, ""
    min_distance = multiple * atr_value / 100.0 * entry
    current = abs(entry - stop_price)
    if current >= min_distance:
        return stop_price, ""
    floored = entry - min_distance if str(side) != "short" else entry + min_distance
    note = (f"atr-floor {current / entry * 100:.2f}% -> {multiple * atr_value:.2f}% "
            f"({multiple:g}xATR, stop {stop_price:.6g} -> {floored:.6g})")
    return floored, note


def _atr_from_map(atr_map: dict | None, symbol: str) -> float | None:
    """从快照指标里取 ATR%；兼容 {symbol: atr} 与 {symbol: {atr_pct: atr}} 两种形状。"""
    entry = (atr_map or {}).get(symbol)
    if isinstance(entry, dict):
        return entry.get("atr_pct")
    return entry


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
    """逐 symbol 拉 K 线并压成一行特征；返回 (文本, {symbol: 现价}, {symbol: 指标})。

    指标里带 atr_pct —— 开仓时的止损下限夹子要用它，所以不能只留在文本里。

    每行的字段：现价、5/20/窗口涨跌幅、窗口高低、现价在区间中的位置、
    近 5 根量能比、RSI、ATR%、距 EMA20/EMA50/VWAP 的百分比距离，以及
    （开启衍生品时）资金费、下次结算、基差、持仓量与跨轮变化。单品种失败
    只跳过该行，不影响整轮。
    """
    lines: list[str] = []
    prices: dict[str, float] = {}
    metrics: dict[str, dict] = {}
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
        metrics[symbol] = {"last": last}
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
        metrics[symbol]["atr_pct"] = atr
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
    return "\n".join(lines), prices, metrics


def parse_signals(text: str) -> list[dict] | None:
    """解析 LLM 输出，返回信号列表或 None。

    关键区分：合法的 `{"signals": []}` 表示「本轮没有机会」，是正常结果；
    只有完全解析不出 payload 才算不守契约（None）。把前者当成失败会白白
    重试一次 LLM，还会把一轮记成 error。
    """
    try:
        start, end = text.index("{"), text.rindex("}")
        payload = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        markdown = _parse_markdown_signals(text)
        return markdown or None
    if not isinstance(payload, dict):
        return None
    signals = payload.get("signals")
    if signals is None:
        return None
    return list(signals) if isinstance(signals, list) else None


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


#: post-only 入场的默认等待秒数：等到就吃 maker 费，等不到就撤单补市价。
MAKER_ENTRY_WAIT = 15.0


def maker_entry_price(ex, symbol: str, order_side: str) -> float:
    """post-only 该挂哪一档：买单挂买一、卖单挂卖一。

    挂在对手价会被 Binance 当成吃单整单拒绝（-5022），所以必须落在本方最优价。
    盘口读不到就返回 0 → 调用方退回市价，不拿一笔交易去赌。
    """
    try:
        ticker = ex.fetch_ticker(symbol)
    except Exception:  # noqa: BLE001 - 盘口读不到就退回市价
        return 0.0
    price = _as_float((ticker or {}).get("bid" if order_side == "buy" else "ask"))
    return price if price and price > 0 else 0.0


def place_entry(place, read_order, cancel, *, side: str, quantity: float, limit_price: float,
                maker: bool, maker_wait: float, sleep=time.sleep, **order_kwargs) -> dict:
    """开仓：先试 post-only（maker 费），被拒或超时就补市价。

    出场是条件市价单，只能吃 taker；只有入场这一腿能省（本账户 maker 0.02% /
    taker 0.04%，回放里 maker 入场在 5m/1m 上成交率 99.3%~99.8%）。返回值和
    place_order 同形状并多带 entry_style，调用方不必分支。
    """
    def market(**extra) -> dict:
        extra.setdefault("quantity", quantity)
        return place(side=side, order_type="market", **extra, **order_kwargs)

    def fill_price(result: dict) -> float:
        """成交均价。place_order 的响应经常不带价（实测市价单 price/average 都是
        None），但同一订单 fetch_order 带 —— 入场价算错会把止损止盈一起算错，
        所以缺价时补一次读单。读不到就返回 0，由调用方回退。"""
        price = _as_float((result or {}).get("average")) or _as_float((result or {}).get("price"))
        if price:
            return price
        result_id = str((result or {}).get("order_id") or "")
        if not result_id:
            return 0.0
        snapshot = read_order(result_id) or {}
        return (_as_float(snapshot.get("average")) or _as_float(snapshot.get("price")) or 0.0)

    if not maker or maker_wait <= 0 or limit_price <= 0:
        result = market()
        return {**result, "price": fill_price(result) or None, "entry_style": "market"}
    posted = place(side=side, quantity=quantity, order_type="limit", limit_price=limit_price,
                   post_only=True, **order_kwargs) or {}
    order_id = str(posted.get("order_id") or "")
    if str(posted.get("status")) != "ok" or not order_id:
        # 会被立刻吃掉的单会被整单拒绝（-5022）或限价参数不合法 → 直接市价
        result = market()
        return {**result, "price": fill_price(result) or None, "entry_style": "market",
                "maker_reject": str(posted.get("error") or "")}
    deadline = time.time() + maker_wait
    while time.time() < deadline:
        sleep(1.0)
        snapshot = read_order(order_id) or {}
        if str(snapshot.get("status")) != "ok":
            break                        # 读不到状态就别把仓位悬着，撤单转市价
        status = str(snapshot.get("order_status") or "").lower()
        if status in ("closed", "filled"):
            return {**posted, "status": "ok", "order_status": status,
                    "filled": _as_float(snapshot.get("filled")) or quantity,
                    "price": _as_float(snapshot.get("average")) or limit_price,
                    "entry_style": "maker"}
        if status in ("canceled", "cancelled", "rejected", "expired"):
            break
    # 超时或其他终态：撤掉剩余，缺口用市价补，别让本笔变成半仓
    cancel(order_id)
    final = read_order(order_id) or {}
    maker_filled = _as_float(final.get("filled")) or 0.0
    maker_price = _as_float(final.get("average")) or limit_price
    remaining = max(0.0, quantity - maker_filled)
    if remaining <= 0:
        return {**posted, "status": "ok", "order_status": "closed", "filled": maker_filled,
                "price": maker_price, "entry_style": "maker"}
    top_up = market(quantity=remaining) or {}
    market_filled = _as_float(top_up.get("filled")) or 0.0
    # 市价腿的成交价也要读出来：缺了它会把入场价算成限价单的价（实测得到 60000
    # 这种离市价几千刀的假价），止损止盈跟着全错。
    market_price = fill_price(top_up) or maker_price
    total = maker_filled + market_filled
    blended = (maker_filled * maker_price + market_filled * market_price) / total if total > 0 else 0.0
    return {**top_up, "status": str(top_up.get("status") or "error"), "filled": total,
            "price": blended, "entry_style": "maker+market", "maker_filled": maker_filled}


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
    entry = _as_float(position.get("entry")) or 0.0
    stop = _as_float(stop_price) or 0.0
    target = _as_float(take_profit) or 0.0
    price = _as_float(price) or 0.0
    trailing = _as_float(trailing) or 0.0
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

    if trailing <= 0:
        return None

    extreme = _as_float(position.get("peak")) or entry
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
    """返回 {symbol: [条件单行]} —— 交易所上还挂着的 stop/take-profit/trailing。

    这些单**不在**普通挂单接口里：Binance 把条件单放进独立的 Algo 服务，
    fetch_open_orders() 永远看不到它们（实测踩过）。所以这里必须用
    get_open_algo_orders()，否则既发现不了已挂的保护，也找不到要撤的兄弟单。

    三种条件单**都要**收进来，不能只收 stop/take-profit：漏掉
    trailing_stop_market 时，「仓位已平 → 撤残留」看不见移动止损，孤儿单就
    一直留在交易所上（实测后果：Binance 以 -4067 拒绝再改该 symbol 的保证金
    模式，于是那个品种再也开不出新仓）。
    """
    rows = _read_protection_rows()
    result: dict[str, list[dict]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        order_type = str(row.get("order_type") or "").strip().lower()
        if order_type not in ("stop_market", "trailing_stop_market", "take_profit_market"):
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
                   trailing_pct: float, margin_mode: str, leverage: int, trade: bool,
                   only: set[str] | None = None) -> None:
    """按保护模式给持仓挂 reduce_only 条件单（进程死了也有效）。

    fixed    : stop_market + take_profit_market（固定止损 + 止盈）
    trailing : trailing_stop_market（交易所自己跟市价、回调 trailing_pct%）+ 止盈
    both     : 三者都挂（移动单被撤/失效时仍有固定底线）

    所有腿都是 reduce_only，任何一条成交只会减仓、不会反向开仓；兄弟单由
    下一轮的残留清理撤掉（交易所不会自动撤）。
    """
    quantity = abs(float(position.get("quantity") or 0))
    # only 用于「补齐」场景：只挂缺的那几条腿，不重新挂已有的
    legs = tuple(leg for leg in protection_legs(mode) if only is None or leg in only)
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
        result: dict = {}
        for attempt in range(2):
            try:
                result = place(
                    symbol, TRADE_PROFILE, side=close_side, quantity=quantity,
                    order_type=order_type, reduce_only=True,
                    margin_mode=margin_mode, leverage=leverage,
                    session_id=f"futures-protect-{int(time.time())}", **extra,
                )
            except Exception as exc:  # noqa: BLE001 - 挂不上就靠脚本侧兜底
                result = {"status": "error", "error": str(exc)}
            if str((result or {}).get("status")) == "ok":
                break
            # 第一次失败最常见的原因是校验保证金模式时读持仓的瞬时网络抖动
            # （实测：紧接着的止盈腿就成功了）。隔两秒重试一次，仍失败就留给
            # 下一轮补齐——但要把「保护不完整」明确记下来。
            if attempt == 0:
                time.sleep(2)
        _log({"ts": _now(), "symbol": symbol, "side": close_side, "quantity": quantity,
              "status": "protect", "detail": order_type, "price": price, "result": result})
        if str((result or {}).get("status")) == "ok":
            record[_LEG_KEYS[order_type]] = (result or {}).get("order_id")
    missing = [leg for leg in legs if _LEG_KEYS[leg] not in record]
    if missing and trade:
        _log({"ts": _now(), "symbol": symbol, "status": "warn",
              "detail": f"protection incomplete: missing {','.join(missing)}; re-arms next round"})
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
                     resting: dict | None = None, can_arm: bool = True,
                     atr_map: dict | None = None, stop_floor_atr: float = 0.0) -> None:
    """每轮先管持仓：触发止盈止损就以 reduce_only 平仓，未触发则确保有交易所侧保护。"""
    from src.trading.service import place_order

    legs = protection_legs(protection)
    # 交易所侧移动止损在跑时，脚本侧那条就得让位：两边各按自己的极值算，
    # 同时开会互相打架（脚本会先平仓并把交易所单撤掉）。
    script_trailing = 0.0 if "trailing_stop_market" in legs else trailing
    if resting is None:
        resting = _resting_protection_orders() if legs else {}
    for symbol, broker_position in list((state.get("positions") or {}).items()):
        price = _as_float(prices.get(symbol))
        if not price or price <= 0:
            continue
        entry = _as_float(broker_position.get("entry")) or 0.0
        tracked = (state.get("protection") or {}).get(symbol)
        if not isinstance(tracked, dict):
            # 记录被写坏（手工改过/写了一半）：当没有记录处理，别让 .get 打断整轮
            tracked = {}
        recorded_stop = _as_float(tracked.get("stop_price"))
        recorded_target = _as_float(tracked.get("take_profit"))
        if recorded_stop and recorded_target:
            stop_price, target = recorded_stop, recorded_target
        else:
            # 首次接管或本地文件丢失：用 --stop-loss/--take-profit 从入场价换算
            stop_price, target, _ = resolve_exit_levels(
                {},
                broker_position["side"],
                entry,
                stop_loss_pct=stop_loss,
                take_profit_pct=take_profit,
            )
        # ATR 夹子：脚本判定用的价位必须和交易所挂单一致，否则两边各判各的
        stop_price, floor_note = apply_stop_floor(
            broker_position["side"], entry, stop_price,
            _atr_from_map(atr_map, symbol), stop_floor_atr,
        )
        if floor_note:
            _log({"ts": _now(), "symbol": symbol, "status": "levels-adjusted",
                  "detail": floor_note})
        peak = _as_float((state.get("peaks") or {}).get(symbol))
        held = {
            "side": broker_position["side"],
            "entry": entry,
            "peak": peak or entry,
        }
        reason = check_exit(held, price, stop_price=stop_price, take_profit=target, trailing=script_trailing)
        state.setdefault("peaks", {})[symbol] = held["peak"]
        if not reason:
            if not legs or not can_arm:
                # 读不到保护状态时不动保护（不重复挂、也不清理），但脚本侧
                # 的止盈止损照旧生效——平仓不该被一个读失败拖住。
                continue
            # 以**本地记录**为准判断保护是否齐全。Binance 的条件单列表接口在
            # 测试网会偶发读丢已挂的腿，照着「读到的才存在」补挂就会挂出重复单
            # （实测发生过）。交易所读取只用于「仓位已消失 → 清残留」。
            expected = set(legs)
            tracked = (state.get("protection") or {}).get(symbol)
            if not isinstance(tracked, dict):
                tracked = {}
            current_qty = abs(_as_float(broker_position.get("quantity")) or 0.0)
            recorded_qty = _as_float(tracked.get("quantity")) or 0.0
            if tracked and str(tracked.get("mode") or "") != protection:
                # 保护模式换过：撤掉旧腿（本地 id + 交易所列表），再按新模式挂
                disarm_protection(_cancel_algo, state, symbol, trade=trade,
                                  resting_orders=resting.get(symbol))
                tracked = {}
            tracked_types = {leg for leg in expected if tracked.get(_LEG_KEYS[leg])}
            size_matches = recorded_qty > 0 and abs(recorded_qty - current_qty) < 1e-9
            recorded_stop = _as_float(tracked.get("stop_price"))
            stop_matches = (
                recorded_stop is not None
                and abs(recorded_stop - stop_price) < 1e-9
            )
            if tracked_types == expected and size_matches and stop_matches:
                continue  # 记录齐全、仓位没变、止损价没被夹过 → 不查交易所、不动保护
            if tracked_types and (not size_matches or not stop_matches):
                # 仓位数量变了（部分成交/加仓），或止损价被 ATR 夹子改过：清了重挂。
                # 带上交易所列表：只撤本地记录的 id 时，该品种上多挂出来的孤儿腿
                # （例如部分撤单失败留下的）会一直活到 -4067 把保证金模式锁死。
                disarm_protection(_cancel_algo, state, symbol, trade=trade,
                                  resting_orders=resting.get(symbol))
                tracked_types = set()
            missing = expected - tracked_types
            if missing:
                # 记录里没有的腿，才去交易所确认一次；两次都读不到才补挂
                try:
                    confirm = _resting_protection_orders().get(symbol) or []
                except Exception:  # noqa: BLE001 - 确认不了就不补挂
                    confirm = []
                confirmed = {str(row.get("order_type") or "").strip().lower() for row in confirm}
                missing = {leg for leg in missing if leg not in confirmed}
                if not missing:
                    continue
            arm_protection(
                place_order, state, symbol, broker_position,
                mode=protection, stop_price=stop_price, take_profit_price=target,
                trailing_pct=trailing, margin_mode=margin_mode, leverage=leverage,
                trade=trade, only=missing,
            )
            continue
        # 触发：先撤交易所侧挂单，再市价平仓（避免两边同时动作）
        disarm_protection(_cancel_algo, state, symbol, trade=trade, resting_orders=resting.get(symbol))
        quantity = abs(_as_float(broker_position.get("quantity")) or 0.0)
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
                # 止损类平仓 → 该品种进冷静期，别让同一个坑立刻再钓一次
                _record_cooldown(state, symbol, reason)
                tg_send(f"🔴 平仓 {symbol}  {quantity:.6f}  — {reason}")
        except Exception as exc:  # noqa: BLE001 - 单笔失败不终止循环
            _log({"ts": _now(), "symbol": symbol, "side": close_side, "quantity": quantity,
                  "status": "error", "reason": reason, "detail": str(exc)})


def run_round(ex, llm, *, trade: bool, top: int, symbols_arg: list[str],
              stop_loss: float, take_profit: float, trailing: float,
              margin_mode: str, leverage: int, protection: str = "fixed",
              bars_limit: int = DEFAULT_BARS, max_positions: int = DEFAULT_MAX_POSITIONS,
              derivatives: bool = True, cooldown_hours: float = COOLDOWN_HOURS,
              long_regime_gate: bool = True,
              stop_floor_atr: float = STOP_FLOOR_ATR,
              maker_entry: bool = True, maker_wait: float = MAKER_ENTRY_WAIT) -> None:
    """跑一轮：先管持仓，再取信号，再（可选）开仓。"""
    from src.trading.service import cancel_order, get_order, place_order

    state = load_state()

    # 1) 持仓真相来自券商；读不到就不动仓（fail-closed）
    try:
        raw_positions = _broker_positions()
    except Exception as exc:  # noqa: BLE001
        _log({"ts": _now(), "round": "error", "detail": f"positions: {exc}"})
        return
    # 券商返回的持仓逐字段过边界：任何一格是脏值（网络层偶发返回对象/字符串），
    # 都不该让这一轮的所有持仓管理一起消失。
    state["positions"] = {}
    for row in raw_positions:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        quantity = _as_float(row.get("quantity")) or 0.0
        if quantity == 0:
            continue
        entry = _as_float(row.get("entry_price"))
        if entry is None:
            entry = _as_float(row.get("price"))
        state["positions"][str(row["symbol"])] = {
            "side": str(row.get("side") or ("short" if quantity < 0 else "long")),
            "quantity": quantity,
            "entry": entry or 0.0,
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
        if protection_readable:
            # 两边取并集：只按交易所列表清理，会把「仓位已平但本地记录还在」
            # 的情况（例如止盈在交易所侧成交、我们没参与那次平仓）留成孤儿
            # 记录，下一轮又拿它当退出价。读失败时不碰保护，交给下一轮。
            stale = (set(resting) | set(state.get("protection") or {})) - set(state["positions"])
            for symbol in sorted(stale):
                # 仓位在交易所侧消失＝保护单成交打掉的，脚本自己没参与那次平仓，
                # 所以 _record_cooldown 都没被调用过 —— 冷却闸在这里补记，否则
                # 「同品种止损冷却 6h」在 protection=both 下永远不生效。
                reason = _exchange_side_stop_reason(
                    (state.get("protection") or {}).get(symbol), resting.get(symbol)
                )
                if reason:
                    _log({"ts": _now(), "symbol": symbol, "status": "cooldown",
                          "detail": reason})
                    _record_cooldown(state, symbol, reason)
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
    snapshot, prices, metrics = build_market_snapshot(
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
        atr_map=metrics, stop_floor_atr=stop_floor_atr,
    )
    save_state(state)

    # 3) 取信号
    prompt = (
        SYSTEM_PROMPT.format(max_notional=int(MAX_NOTIONAL))
        + f"\n\nCurrent time (UTC): {_now()}\n"
        # 标题按品种池的真实来源措辞：--symbols 是固定名单，不是成交额排名。
        # n 用 len(symbols)（实际进入快照的品种数），可能少于 symbols_arg。
        + _universe_header(n=len(symbols), fixed=bool(symbols_arg))
        + f"\n{snapshot}\n"
        + "Generate trading signals now."
    )
    try:
        reply = llm.invoke(prompt)
        text = reply.content if hasattr(reply, "content") else str(reply)
    except Exception as exc:  # noqa: BLE001 - 一次失败不能拖死循环
        _log({"ts": _now(), "round": "error", "detail": f"llm: {exc}"})
        return

    signals = parse_signals(text)
    if signals is None:
        # 真的解析不出 payload 才算不守契约。一轮报废太贵，带一句提醒重试一次，
        # 并把原文片段留证。
        _log({"ts": _now(), "round": "warn", "detail": "llm output unparseable; retrying once",
              "raw": text[:200]})
        try:
            retry_reply = llm.invoke(
                prompt + "\n\nREMINDER: reply with ONE JSON object and nothing else."
            )
            retry_text = retry_reply.content if hasattr(retry_reply, "content") else str(retry_reply)
            signals = parse_signals(retry_text)
            if signals:
                text = retry_text
        except Exception as exc:  # noqa: BLE001 - 重试失败就按本轮无信号处理
            _log({"ts": _now(), "round": "warn", "detail": f"llm retry failed: {exc}"})
            signals = []
    if signals is None:
        _log({"ts": _now(), "round": "error", "detail": "llm returned no parseable signals",
              "raw": text[:200]})
        return
    if not signals:
        # 合法的空信号集：本轮没有值得做的机会，安静跳过
        _log({"ts": _now(), "round": "idle", "detail": "no setups this round (empty signal set)"})
        return

    # 4) 执行
    long_gate: str | None = None  # 多头顺势闸（懒加载：每轮最多读一次参考品种）
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
        blocked = _cooldown_reason(state, symbol, cooldown_hours)
        if not blocked and side == "long" and long_regime_gate:
            if long_gate is None:
                long_gate = _long_regime_reason(ex)
            blocked = long_gate
        if blocked:
            _log({"ts": _now(), "symbol": symbol, "side": side, "status": "skipped",
                  "detail": blocked})
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
            def _place(**kwargs) -> dict:  # noqa: E306 - 闭包把本笔的 symbol/profile 绑好
                return place_order(symbol, TRADE_PROFILE,
                                   session_id=f"futures-loop-{int(time.time())}", **kwargs)

            result = place_entry(
                _place,
                lambda order_id: get_order(order_id, TRADE_PROFILE, symbol=symbol),
                lambda order_id: cancel_order(order_id, TRADE_PROFILE, symbol=symbol),
                side=order_side, quantity=quantity,
                limit_price=maker_entry_price(ex, symbol, order_side) if maker_entry else 0.0,
                maker=maker_entry, maker_wait=maker_wait,
                margin_mode=margin_mode, leverage=leverage,
            )
            _log({"ts": _now(), "symbol": symbol, "side": side, "quantity": quantity,
                  "status": "order", "reason": reason,
                  # 信号自己的特征必须落盘：没有 confidence / 点位就无法回答
                  # 「哪类信号赚钱」，参数只能靠猜。实际生效的止损止盈另有
                  # levels / levels-adjusted 两条记录。
                  "signal": {
                      "confidence": _as_float(signal.get("confidence")),
                      "entry": _as_float(signal.get("entry")),
                      "stop_loss": _as_float(signal.get("stop_loss")),
                      "take_profit": _as_float(signal.get("take_profit")),
                      "notional": notional,
                  },
                  "atr_pct": _as_float(_atr_from_map(metrics, symbol)),
                  # maker / maker+market / market：费率归因要靠它
                  "entry_style": (result or {}).get("entry_style"),
                  "result": result})
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
                        # 止损下限：判定价 = 挂单价，且都不贴着噪声
                        stop_price, floor_note = apply_stop_floor(
                            side, entry, stop_price,
                            _atr_from_map(metrics, symbol), stop_floor_atr,
                        )
                        if floor_note:
                            _log({"ts": _now(), "symbol": symbol,
                                  "status": "levels-adjusted", "detail": floor_note})
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

    # 信号解析的三种结果必须区分开：合法空集 / 不守契约 / 正常信号
    assert parse_signals('{"signals": []}') == []
    assert parse_signals("完全没有 JSON 的一段话") is None
    parsed = parse_signals('{"signals": [{"symbol": "BTC/USDT:USDT", "side": "hold"}]}')
    assert parsed is not None and len(parsed) == 1 and parsed[0]["side"] == "hold"

    # 止损下限夹子：让「脚本判定价 == 交易所挂单价」，且都不贴着噪声
    floored, note = apply_stop_floor("long", 100.0, 99.9, 0.5, 2.0)   # 0.1% → 1.0%
    assert abs(floored - 99.0) < 1e-9 and note, (floored, note)
    wide, no_note = apply_stop_floor("long", 100.0, 95.0, 0.5, 2.0)   # 本已够宽 → 不动
    assert wide == 95.0 and no_note == ""
    short_floored, _ = apply_stop_floor("short", 100.0, 100.1, 0.5, 2.0)
    assert abs(short_floored - 101.0) < 1e-9
    assert apply_stop_floor("long", 100.0, 99.9, 0.5, 0.0)[0] == 99.9  # 关闭 → 不动
    assert apply_stop_floor("long", 100.0, 99.9, None, 2.0)[0] == 99.9 # 没 ATR → 不动
    # 形状兼容：快照指标是 {symbol: {atr_pct: x}}，传错形状不能炸掉整轮
    assert _atr_from_map({"BTC/USDT:USDT": {"atr_pct": 0.42}}, "BTC/USDT:USDT") == 0.42
    assert _atr_from_map({"BTC/USDT:USDT": 0.42}, "BTC/USDT:USDT") == 0.42
    assert _atr_from_map(None, "BTC/USDT:USDT") is None
    assert apply_stop_floor("long", 100.0, 99.9, {"atr_pct": 0.5}, 2.0)[0] == 99.9  # 脏值不炸

    # 入场闸 1：同品种止损冷却
    state_cd = {"cooldowns": {"ADA/USDT:USDT": "2026-09-10T10:00:00+00:00"}}
    now_s = datetime.fromisoformat("2026-09-10T13:00:00+00:00").timestamp()
    assert _cooldown_reason(state_cd, "ADA/USDT:USDT", 6.0, now_s) != ""      # 3h < 6h → 拦
    assert _cooldown_reason(state_cd, "ADA/USDT:USDT", 2.0, now_s) == ""      # 3h > 2h → 放
    assert _cooldown_reason(state_cd, "LTC/USDT:USDT", 6.0, now_s) == ""      # 没记录 → 放
    assert _cooldown_reason(state_cd, "ADA/USDT:USDT", 0.0, now_s) == ""      # 冷却关闭
    assert _cooldown_reason({"cooldowns": {"X/USDT:USDT": "垃圾时间戳"}},
                            "X/USDT:USDT", 6.0, now_s) == ""                  # 坏时间戳不误拦
    assert _hours_since("not-a-time") is None
    cd_state = {}
    _record_cooldown(cd_state, "ADA/USDT:USDT", "stop-loss 0.2081 <= 0.2090 (entry 0.2118)")
    assert "ADA/USDT:USDT" in cd_state["cooldowns"]
    _record_cooldown(cd_state, "LTC/USDT:USDT", "take-profit 53.25 >= 53.20 (entry 52.39)")
    assert "LTC/USDT:USDT" not in cd_state["cooldowns"]                        # 止盈不冷却

    # 入场闸 2：多头顺势闸（纯函数）
    assert _regime_verdict([float(i) for i in range(1, 101)])[0] is True        # 稳步上行 → 放行
    assert _regime_verdict([float(100 - i) for i in range(100)])[0] is False    # 稳步下行 → 拦
    assert _regime_verdict([1.0] * 60)[0] is False                              # 贴平 EMA50 → 拦
    assert _regime_verdict([1.0] * 10)[0] is False                              # 样本不足 → 拦

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

    # 品种池标题：两种来源措辞必须分开，不能都声称是成交额排名
    fixed_hdr = _universe_header(n=10, fixed=True)
    top_hdr = _universe_header(n=10, fixed=False)
    assert "fixed watchlist" in fixed_hdr, fixed_hdr
    assert "24h quote volume" not in fixed_hdr, fixed_hdr   # 固定名单不得声称按成交额选
    assert "24h quote volume" in top_hdr, top_hdr
    assert "fixed watchlist" not in top_hdr, top_hdr
    assert "10" in fixed_hdr and "10" in top_hdr

    # 快照行：新字段齐、价格表正确、上下文可拼进来
    ex = _FakeBarsExchange(_synth_bars())
    text, prices, metrics = build_market_snapshot(["BTC/USDT:USDT"], ex, bars_limit=100)
    assert metrics["BTC/USDT:USDT"].get("atr_pct") is not None, metrics
    assert prices == {"BTC/USDT:USDT": prices["BTC/USDT:USDT"]} and "BTC/USDT:USDT" in prices
    for field in ("last=", "chg5=", "rng=[", "pos=", "volRatio=", "rsi=", "atr%=", "ema20=", "vwap="):
        assert field in text, (field, text[:200])
    assert "prev20_min" not in text                   # 名不副实的字段已去掉
    context = {"funding": {"BTC/USDT:USDT": {"funding_rate": -0.0001, "mark_price": 101.0, "index_price": 100.0}},
               "open_interest": {"BTC/USDT:USDT": 110.0}}
    text, _, _ = build_market_snapshot(["BTC/USDT:USDT"], ex, bars_limit=100,
                                       context=context, previous_oi={"BTC/USDT:USDT": 100.0})
    for field in ("fund=", "basis=", "oi=", "oiChg="):
        assert field in text, (field, text[:220])

    # 挂保护腿：第一次失败（瞬时读抖动）要重试一次，第二次成功即记下 id。
    # 自测不碰真日志：临时把 LOG_PATH 指到临时目录。
    import tempfile

    global LOG_PATH
    saved_log, LOG_PATH = LOG_PATH, Path(tempfile.mkdtemp()) / "selftest.jsonl"
    try:
        calls: list[str] = []

        def flaky_place(*args, **kwargs):
            order_type = str(kwargs.get("order_type"))
            calls.append(order_type)
            if order_type == "trailing_stop_market" and calls.count(order_type) == 1:
                return {"status": "error", "error": "transient position read failure"}
            return {"status": "ok", "order_id": "id-" + order_type}

        state = {"protection": {}}
        arm_protection(
            flaky_place, state, "BTC/USDT:USDT",
            {"side": "long", "quantity": 1.0, "entry": 100.0},
            mode="trailing", stop_price=95.0, take_profit_price=110.0,
            trailing_pct=3.0, margin_mode="isolated", leverage=5, trade=True,
        )
        recorded = state["protection"]["BTC/USDT:USDT"]
        assert recorded["trailing_order_id"] == "id-trailing_stop_market"
        assert recorded["take_profit_order_id"] == "id-take_profit_market"
        assert calls.count("trailing_stop_market") == 2, calls
    finally:
        LOG_PATH = saved_log

    # 脏数值边界：状态文件/券商/LLM 给的非数值必须退化成「按缺失处理」。
    # 实测：保护记录里 stop_price 是个对象时，float(...) 抛出的 TypeError 会
    # 穿出 manage_positions，把整轮（所有品种的持仓管理）一起带走。
    assert _as_float("1.5") == 1.5 and _as_float(3) == 3.0
    assert _as_float({"a": 1}) is None and _as_float([1]) is None and _as_float("x") is None
    assert _as_float(None) is None
    assert _as_float(True) is None                    # bool 不算数值，别当 1.0 用
    # 脏值进 check_exit 也不能抛，只能按「判不了」返回 None
    assert check_exit({"side": "long", "entry": {"x": 1}}, 100.0,
                      stop_price={"s": 1}, take_profit=None, trailing=3) is None
    assert check_exit({"side": "long", "entry": 100.0, "peak": {"p": 1}}, 100.0,
                      stop_price=95.0, take_profit=108.0, trailing=3) is None

    # 交易所侧止损 → 下一轮补记冷却；证据不足一律不记
    live_tp = [{"order_id": "tp1", "order_type": "take_profit_market"}]
    stop_filled = {"stop_order_id": "s1", "trailing_order_id": "t1",
                   "take_profit_order_id": "tp1"}
    assert _exchange_side_stop_reason(stop_filled, live_tp) != ""
    assert _exchange_side_stop_reason(stop_filled, live_tp + [{"order_id": "s1"}]) == ""
    assert _exchange_side_stop_reason(stop_filled, []) == ""      # 止盈也没了 → 分不清
    assert _exchange_side_stop_reason({}, live_tp) == ""          # 没有本地记录
    assert _exchange_side_stop_reason("garbage", live_tp) == ""   # 记录本身是脏值
    cd = {}
    _record_cooldown(cd, "BTC/USDT:USDT", _exchange_side_stop_reason(stop_filled, live_tp))
    assert "BTC/USDT:USDT" in cd.get("cooldowns", {})

    # 轮次异常必须留下栈：只记 exc 时定位不到出错的那一行
    try:
        raise ValueError("boom")
    except ValueError:
        tail = _traceback_tail()
    assert "Traceback" in tail and "ValueError: boom" in tail, tail

    # 品种池参数互斥：--symbols 与 --top 同时给出必须报错，不能静默让 --top 失效
    import io
    from contextlib import redirect_stderr

    parser = _build_parser()
    ok_args = parser.parse_args(["--symbols", "BTC/USDT:USDT,ETH/USDT:USDT"])
    assert ok_args.symbols == "BTC/USDT:USDT,ETH/USDT:USDT", ok_args
    assert ok_args.top == DEFAULT_TOP, ok_args.top             # 默认值保留，供 --symbols 为空时兜底
    assert parser.parse_args([]).top == DEFAULT_TOP            # 两个都不传 → 走 top N
    with redirect_stderr(io.StringIO()) as err:
        try:
            parser.parse_args(["--symbols", "BTC/USDT:USDT", "--top", "5"])
        except SystemExit as exc:
            code = exc.code
        else:
            code = None
    assert code == 2, f"--symbols 与 --top 同时给出应报错退出，实际 code={code}"
    assert "--top" in err.getvalue() and "--symbols" in err.getvalue(), err.getvalue()

    print("selftest OK")


class _RoundTimeout(Exception):
    """单轮超过硬时限。"""


def _round_timeout_handler(signum, frame):  # noqa: ARG001
    """SIGALRM 处理器：抛出单轮超时。"""
    raise _RoundTimeout(f"round exceeded {signum} timeout")


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    品种池的两个来源互斥：``--symbols``（固定名单）与 ``--top``（成交额排名）
    同时给出时无法判断意图，argparse 报错退出，而不是静默让 ``--top`` 失效。
    抽成独立函数是为了让 ``_selftest`` 能直接断言这条互斥规则。

    Returns:
        配好全部参数（含品种池互斥组）的解析器。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade", action="store_true", help="真实下单（默认 dry-run 只记录信号）")
    parser.add_argument("--interval", type=int, default=300, help="轮询间隔秒（默认 300）")
    parser.add_argument("--runs", type=int, default=12, help="最大轮数（默认 12 = 1 小时）")
    # 品种池只有两个来源，互斥：--symbols 固定名单 vs --top 成交额排名。
    # 同时给出时 argparse 直接报错，而不是静默让 --top 失效（早先就是静默的）。
    universe = parser.add_mutually_exclusive_group()
    universe.add_argument("--top", type=int, default=DEFAULT_TOP,
                          help=f"成交额 top N（默认 {DEFAULT_TOP}）；与 --symbols 互斥")
    universe.add_argument("--symbols", default="",
                          help="逗号分隔的固定品种；给出即选中固定名单，与 --top 互斥")
    parser.add_argument("--bars", type=int, default=DEFAULT_BARS,
                        help=f"每个品种拉多少根 5m K 线（默认 {DEFAULT_BARS}）")
    parser.add_argument("--max-positions", dest="max_positions", type=int, default=DEFAULT_MAX_POSITIONS,
                        help=f"同时在手的最大仓位数（默认 {DEFAULT_MAX_POSITIONS}）")
    parser.add_argument("--no-derivatives", action="store_true",
                        help="不拉资金费/持仓量等衍生品上下文")
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
    parser.add_argument("--cooldown-hours", dest="cooldown_hours", type=float, default=COOLDOWN_HOURS,
                        help=f"同品种止损后的冷静期小时数（默认 {COOLDOWN_HOURS:g}，0=关闭）")
    parser.add_argument("--no-long-regime-gate", action="store_true",
                        help=f"关闭多头顺势闸（默认开：{REGIME_SYMBOL} 走弱时不开多）")
    parser.add_argument("--stop-floor-atr", dest="stop_floor_atr", type=float,
                        default=STOP_FLOOR_ATR,
                        help=f"止损距离下限（× ATR(14,5m)，默认 {STOP_FLOOR_ATR:g}，0=关闭）")
    parser.add_argument("--no-maker-entry", action="store_true",
                        help="关掉 post-only 入场（默认开：maker 0.02%% vs taker 0.04%%）")
    parser.add_argument("--maker-entry-wait", dest="maker_entry_wait", type=float,
                        default=MAKER_ENTRY_WAIT,
                        help=f"post-only 等待秒数，超时撤单转市价（默认 {MAKER_ENTRY_WAIT:g}）")
    parser.add_argument("--selftest", action="store_true", help="运行离线自测（不下单不触网）")
    return parser


def main() -> int:
    """命令行入口。"""
    import signal

    parser = _build_parser()
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
    universe = f"固定 {len(symbols_arg)} 个品种" if symbols_arg else f"成交额 top {args.top}"
    print(f"[futures-loop] 品种 {universe} | 每品种 {args.bars} 根 5m | 最多同时 {args.max_positions} 仓 | "
          f"衍生品上下文 {'关' if args.no_derivatives else '开'}")
    print(f"[futures-loop] 入场闸 | 同品种止损冷却 {args.cooldown_hours:g}h | "
          f"多头顺势闸 {'关' if args.no_long_regime_gate else '开（参考 ' + REGIME_SYMBOL + '）'}")
    print(f"[futures-loop] 止损下限 {args.stop_floor_atr:g}xATR(14,5m)"
          + ("（关闭）" if args.stop_floor_atr <= 0 else ""))
    maker_entry = not args.no_maker_entry
    print(f"[futures-loop] 入场 "
          + (f"post-only 挂买一/卖一，{args.maker_entry_wait:g}s 未成交则撤单转市价"
             if maker_entry else "市价（maker 关）"))
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
                    cooldown_hours=args.cooldown_hours,
                    long_regime_gate=not args.no_long_regime_gate,
                    stop_floor_atr=args.stop_floor_atr,
                    maker_entry=maker_entry, maker_wait=args.maker_entry_wait,
                )
            except _RoundTimeout as exc:
                _log({"ts": _now(), "round": "timeout", "detail": f"round timed out after {round_timeout}s"})
                tg_send(f"⚠️ 第 {index + 1} 轮超时（已跳过）：{exc}")
            except Exception as exc:  # noqa: BLE001 - 单轮任何异常都不能终止循环
                # 只记 exc 时定位不到出错的那一行（实测 03:21 那条 'dict <= 0'
                # 查了一整轮）：栈尾一起落盘，下次不用靠猜。
                _log({"ts": _now(), "round": "error", "detail": f"round failed: {exc}",
                      "traceback": _traceback_tail()})
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
