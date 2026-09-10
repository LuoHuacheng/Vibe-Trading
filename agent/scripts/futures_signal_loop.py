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

Rules:
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
                return data
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {"peaks": {}}


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


def build_market_snapshot(symbols: list[str], ex) -> str:
    """逐 symbol 拉 5m K 线压缩成一行；单 symbol 失败只跳过该行。"""
    lines = []
    for symbol in symbols:
        try:
            bars = ex.fetch_ohlcv(symbol, timeframe="5m", limit=20)
        except Exception:  # noqa: BLE001 - 单品种失败不拖垮整轮
            continue
        if not bars:
            continue
        closes = [bar[4] for bar in bars]
        volume = sum(bar[5] for bar in bars)
        lines.append(
            f"{symbol}: last={closes[-1]:.4f} prev20_min={closes[0]:.4f} "
            f"chg%={(closes[-1] / closes[0] - 1) * 100:+.2f} vol20={volume:.0f}"
        )
    return "\n".join(lines)


def snapshot_prices(snapshot: str) -> dict[str, float]:
    """从快照文本里解析每个品种的 last 价。"""
    prices: dict[str, float] = {}
    for line in snapshot.splitlines():
        if ":" not in line or "last=" not in line:
            continue
        symbol = line.split(":", 1)[0].strip()
        try:
            prices[symbol] = float(line.split("last=")[1].split(" ")[0])
        except (IndexError, ValueError):
            continue
    return prices


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


def check_exit(
    position: dict,
    price: float,
    *,
    stop_loss: float,
    take_profit: float,
    trailing: float,
) -> str | None:
    """方向感知的止盈/止损/移动止盈止损判定，返回平仓原因或 None。

    position: {"side": "long"|"short", "entry": price, "peak": price}。
    多头跟踪最高价、空头跟踪最低价；三个阈值均为正百分比。
    """
    entry = float(position.get("entry") or 0)
    if entry <= 0 or price <= 0:
        return None
    side = str(position.get("side") or "long").lower()
    long_side = side != "short"
    gain_pct = (price - entry) / entry * 100 if long_side else (entry - price) / entry * 100
    if gain_pct <= -stop_loss:
        return f"stop-loss {gain_pct:+.1f}% (entry {entry:.4f})"
    if gain_pct >= take_profit:
        return f"take-profit {gain_pct:+.1f}% (entry {entry:.4f})"

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


def manage_positions(state: dict, prices: dict[str, float], *, trade: bool,
                     stop_loss: float, take_profit: float, trailing: float,
                     margin_mode: str, leverage: int) -> None:
    """每轮先对券商持仓做止盈止损，触发即以 reduce_only 平仓。"""
    from src.trading.service import place_order

    positions = state.get("positions") or {}
    for symbol, broker_position in positions.items():
        price = prices.get(symbol)
        if not price or price <= 0:
            continue
        held = {
            "side": broker_position["side"],
            "entry": broker_position["entry"],
            "peak": (state.get("peaks") or {}).get(symbol) or broker_position["entry"],
        }
        reason = check_exit(held, price, stop_loss=stop_loss, take_profit=take_profit, trailing=trailing)
        state.setdefault("peaks", {})[symbol] = held["peak"]
        if not reason:
            continue
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
              margin_mode: str, leverage: int) -> None:
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

    # 2) 选品与快照
    symbols = symbols_arg or fetch_top_symbols(ex, top)
    snapshot = build_market_snapshot(symbols, ex)
    if not snapshot:
        _log({"ts": _now(), "round": "error", "detail": "no market data"})
        save_state(state)
        return
    prices = snapshot_prices(snapshot)

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
                    state["positions"][symbol] = {"side": side, "quantity": filled, "entry": entry}
                    state.setdefault("peaks", {})[symbol] = entry
        except Exception as exc:  # noqa: BLE001 - 单笔失败继续循环
            _log({"ts": _now(), "symbol": symbol, "side": side, "quantity": quantity,
                  "status": "error", "detail": str(exc)})

    save_state(state)
    _refresh_portfolio()


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
    """离线自测：方向感知的止盈止损 + 名义额/最小量校验（不触网）。"""
    # 多头：跌 6% (>5) 触发止损
    assert check_exit({"side": "long", "entry": 100.0}, 94.0, stop_loss=5, take_profit=8, trailing=3)
    # 多头：跌 4% 不触发
    assert check_exit({"side": "long", "entry": 100.0}, 96.0, stop_loss=5, take_profit=8, trailing=3) is None
    # 多头：涨 9% 触发止盈
    assert check_exit({"side": "long", "entry": 100.0}, 109.0, stop_loss=5, take_profit=8, trailing=3)
    # 空头：价格上涨才是亏损，涨 6% 触发止损
    assert check_exit({"side": "short", "entry": 100.0}, 106.0, stop_loss=5, take_profit=8, trailing=3)
    # 空头：跌 9% 触发止盈
    assert check_exit({"side": "short", "entry": 100.0}, 91.0, stop_loss=5, take_profit=8, trailing=3)
    # 空头：跌 4% 不触发
    assert check_exit({"side": "short", "entry": 100.0}, 96.0, stop_loss=5, take_profit=8, trailing=3) is None

    # 多头移动止盈：peak 120 → 115（回撤 4.2% > 3）触发
    assert check_exit({"side": "long", "entry": 115.0, "peak": 120.0}, 115.0,
                      stop_loss=5, take_profit=8, trailing=3)
    # 多头未创新高前（peak == entry）不因小回撤离场
    assert check_exit({"side": "long", "entry": 100.0}, 97.0, stop_loss=5, take_profit=8, trailing=3) is None
    # 多头创新高：peak 上移且不触发
    rising = {"side": "long", "entry": 112.0, "peak": 112.0}
    assert check_exit(rising, 114.0, stop_loss=5, take_profit=8, trailing=3) is None
    assert rising["peak"] == 114.0

    # 空头移动止盈：谷值 80 → 84（反弹 5% > 3）触发
    assert check_exit({"side": "short", "entry": 85.0, "peak": 80.0}, 84.0,
                      stop_loss=5, take_profit=8, trailing=3)
    # 空头创新低：谷值下移且不触发
    falling = {"side": "short", "entry": 85.0, "peak": 85.0}
    assert check_exit(falling, 82.0, stop_loss=5, take_profit=8, trailing=3) is None
    assert falling["peak"] == 82.0

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
    parser.add_argument("--symbols", default="", help="逗号分隔的固定品种，覆盖 top N 选品")
    parser.add_argument("--stop-loss", type=float, default=5.0, help="止损百分比（默认 5）")
    parser.add_argument("--take-profit", type=float, default=8.0, help="止盈百分比（默认 8）")
    parser.add_argument("--trailing", type=float, default=3.0, help="移动止盈回撤百分比（默认 3）")
    parser.add_argument("--leverage", type=int, default=5, help="杠杆倍数（默认 5）")
    parser.add_argument("--margin-mode", default="isolated", choices=("isolated", "cross"),
                        help="保证金模式（默认 isolated）")
    parser.add_argument("--selftest", action="store_true", help="运行离线自测（不下单不触网）")
    args = parser.parse_args()

    if args.selftest:
        _selftest()
        return 0

    from src.providers.llm import build_llm
    from src.trading.connectors.binance.sdk import _exchange, build_config

    symbols_arg = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    llm = build_llm()
    ex = _exchange(build_config({"profile": "paper", "market_type": "usdm"}))
    print(f"[futures-loop] LLM 就绪 | {'TRADE 模式' if args.trade else 'dry-run 模式'} | "
          f"每 {args.interval}s 一轮 | 最多 {args.runs} 轮")
    print(f"[futures-loop] 读 profile {READ_PROFILE} | 下单 profile {TRADE_PROFILE}")
    print(f"[futures-loop] {args.margin_mode} {args.leverage}x | 止损 {args.stop_loss}% | "
          f"止盈 {args.take_profit}% | 移动止盈回撤 {args.trailing}%")
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
                    leverage=args.leverage,
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
