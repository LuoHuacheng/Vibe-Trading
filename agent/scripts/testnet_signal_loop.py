#!/usr/bin/env python3
"""Binance testnet 持续交易信号循环（含止盈止损 + 移动止盈止损）。

每 5 分钟一轮：
  1. 抓取成交额 top10 现货品种的 5m K 线
  2. 调用配置的 LLM 生成结构化信号（buy/sell/hold）
  3. dry-run 模式只记录信号；--trade 模式通过 binance-paper-trade 下单
  4. 每轮先对现有持仓做止盈/止损/移动止盈止损检查，触发即卖出平仓
  5. 信号 + 订单追加写入 ~/.vibe-trading/testnet_signal_log.jsonl，
     持仓成本/峰值持久化到 ~/.vibe-trading/testnet_trade_state.json

默认运行 8 小时（96 轮）。Ctrl-C 安全退出。
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

PROFILE = "binance-paper-trade"
TOP_N = 20
MAX_NOTIONAL = 1000.0
LOG_PATH = Path.home() / ".vibe-trading" / "testnet_signal_log.jsonl"

TG_TOKEN = os.getenv("TESTNET_TG_TOKEN", "")
TG_CHAT = os.getenv("TESTNET_TG_CHAT", "")
STATE_PATH = Path.home() / ".vibe-trading" / "testnet_trade_state.json"

SYSTEM_PROMPT = """You are a crypto spot trading signal generator for a Binance testnet paper account.
Analyze the 5-minute candlestick data below for the top-volume USDT pairs and decide for EACH pair:
- side: "buy", "sell", or "hold" (hold means no trade this round)
- notional: USDT amount for buy/sell (integer, max {max_notional})
- entry: entry price (current last price)
- stop_loss: suggested stop-loss price (for buy: below entry; for sell: above entry)
- take_profit: suggested take-profit price (for buy: above entry; for sell: below entry)
- confidence: 0.0-1.0 strength of the signal
- reason: one short sentence

Rules:
- Never exceed {max_notional} USDT per order.
- Only react to clear short-term momentum / reversal setups. When unsure, hold.
- stop_loss / take_profit / confidence are required for buy and sell; use null for hold.

OUTPUT FORMAT (absolute requirement):
Your entire reply must be ONE valid JSON object and NOTHING else.
- No markdown, no code fences, no tables, no headings, no bullet points, no explanation.
- The first character of your reply is {{ and the last character is }}.
- Example exactly:
{{"signals": [{{"symbol": "BTC/USDT", "side": "buy", "notional": 500, "entry": 79000.0, "stop_loss": 75050.0, "take_profit": 85320.0, "confidence": 0.75, "reason": "momentum breakout above resistance"}}]}}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(record: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def tg_send(text: str) -> None:
    """推送消息到 Telegram；未配置 token/chat 时静默跳过，失败不影响交易。"""
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        import httpx

        httpx.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text},
            timeout=10,
        )
    except Exception:  # noqa: BLE001 — 通知失败不能中断交易循环
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

        httpx.post("http://127.0.0.1:8000/api/portfolio/refresh", timeout=180)
    except Exception:  # noqa: BLE001 — Web UI 刷新失败不影响交易
        pass


def _signal_msg(sig: dict, result: dict | None = None) -> str:
    """按 '🟢/🟡 Vibe-Trading testnet：币种/方向/入场/止损/TP/置信度/理由' 格式构造推送。"""
    side = str(sig.get("side") or "").lower()
    direction = "买入" if side == "buy" else "卖出" if side == "sell" else side
    emoji = "🟢" if side == "buy" else "🟡" if side == "sell" else ""
    entry = sig.get("entry") or (result or {}).get("price")
    conf = sig.get("confidence")
    conf_s = f"{float(conf) * 100:.0f}%" if conf is not None else "—"
    return (
        f"{emoji} Vibe-Trading testnet：\n"
        f"币种：{sig.get('symbol')}\n"
        f"方向：{direction}\n"
        f"入场：{_fmt(entry)}\n"
        f"止损：{_fmt(sig.get('stop_loss'))}\n"
        f"TP：{_fmt(sig.get('take_profit'))}\n"
        f"置信度：{conf_s}\n"
        f"理由：{sig.get('reason') or ''}"
    )


def load_state() -> dict:
    """持仓状态：{symbol: {cost, peak, qty}}。cost/peak 为买入均价与最高价。"""
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {"positions": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def fetch_top_symbols(ex) -> list[str]:
    """成交额 top N 的 USDT 计价现货对（排除稳定币对）。"""
    tickers = ex.fetch_tickers()
    rows = [
        (s, t.get("quoteVolume") or 0)
        for s, t in tickers.items()
        if s.endswith("/USDT") and (t.get("quoteVolume") or 0) > 0
    ]
    rows.sort(key=lambda r: -r[1])
    stable = {"USDC", "USDT", "FDUSD", "TUSD", "DAI", "BUSD", "EUR", "AEUR"}
    picked = [s for s, _ in rows if s.split("/")[0] not in stable][:TOP_N]
    return picked


def build_market_snapshot(symbols: list[str], ex=None) -> str:
    """逐 symbol 拉 5m K 线压缩成一行快照文本。

    ex: 已 load_markets 的 ccxt 客户端。传入时直接 fetch_ohlcv（复用
    元数据缓存，每个 symbol 只发一次 klines 请求）；否则回退
    ``service.get_history``（每次新建客户端+重拉 exchangeInfo，慢）。
    单 symbol 失败只跳过该行，不影响整轮。
    """
    if ex is not None:
        lines = []
        for sym in symbols:
            try:
                bars = ex.fetch_ohlcv(sym, timeframe="5m", limit=20)
                if not bars:
                    continue
                closes = [b[4] for b in bars]
                vol = sum(b[5] for b in bars)
                lines.append(
                    f"{sym}: last={closes[-1]:.4f} prev20_min={closes[0]:.4f} "
                    f"chg%={(closes[-1]/closes[0]-1)*100:+.2f} vol20={vol:.0f}"
                )
            except Exception:  # noqa: BLE001 — 单币拉取失败跳过，不拖垮整轮
                continue
        return "\n".join(lines)

    from src.trading.service import get_history

    lines = []
    for sym in symbols:
        try:
            h = get_history(sym, PROFILE, period="5m", limit=20)
        except Exception:  # noqa: BLE001
            continue
        rows = h.get("bars") or h.get("data") or []
        if not rows:
            continue
        closes = [r["close"] for r in rows]
        vol = sum(r.get("volume") or 0 for r in rows)
        lines.append(
            f"{sym}: last={closes[-1]:.4f} prev20_min={closes[0]:.4f} "
            f"chg%={(closes[-1]/closes[0]-1)*100:+.2f} vol20={vol:.0f}"
        )
    return "\n".join(lines)


def parse_signals(text: str) -> list[dict]:
    """解析 LLM 输出：优先严格 JSON，失败回退 markdown 表格。"""
    try:
        start, end = text.index("{"), text.rindex("}")
        payload = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return _parse_markdown_signals(text)
    return payload.get("signals") or []


# 固定单笔金额（保守档，低于 MAX_NOTIONAL 上限）
_FALLBACK_NOTIONAL = 500.0


def _markdown_nums(text: str) -> list[float]:
    """提取字符串中的数字（去千分位逗号）。"""
    return [
        float(n.replace(",", ""))
        for n in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", text)
    ]


_CONFIDENCE_MAP = {
    "high": 0.85, "very high": 0.9, "speculative": 0.4,
    "medium-high": 0.7, "medium high": 0.7, "medium": 0.6,
    "low": 0.4, "weak": 0.3, "neutral": 0.3,
}


def _markdown_confidence(text: str) -> float | None:
    t = text.lower()
    for key, val in _CONFIDENCE_MAP.items():
        if key in t:
            return val
    return None


def _parse_markdown_signals(text: str) -> list[dict]:
    """从 markdown 表格提取信号（LLM 不遵守 JSON 时的兜底）。

    行如 ``| BTC/USDT | Long | 79,050–79,100 | 78,500 | 79,940 | ... | Medium-High |``。
    数字列按 Entry Zone → Stop Loss → TP1 → TP2 顺序取值。
    """
    signals = []
    for line in text.splitlines():
        if "|" not in line or line.strip().startswith("|" * 2) or "---" in line:
            continue
        cells = [c.strip() for c in line.split("|") if c.strip()]
        pair = next((c for c in cells if "/" in c and c.split("/")[0].isupper() and len(c.split("/")[0]) <= 12), None)
        if not pair:
            continue
        pair = pair.replace("**", "").replace("`", "").strip()
        dir_cell = next(
            (c for c in cells if any(
                w in c.lower() for w in ("long", "short", "neutral", "wait", "avoid", "hold", "weak"))),
            None,
        )
        if not dir_cell:
            continue
        d = dir_cell.lower()
        # 区间单元格（Entry Zone 如 "79,050–79,100"）只取第一个数字，避免错位
        nums = []
        for cell in cells:
            cell_nums = _markdown_nums(cell)
            if not cell_nums:
                continue
            if any(ch in cell for ch in ("–", "—", "~", "-")) and len(cell_nums) > 1:
                cell_nums = cell_nums[:1]
            nums.extend(cell_nums)
        entry = nums[0] if len(nums) >= 1 else None
        stop_loss = nums[1] if len(nums) >= 2 else None
        take_profit = nums[2] if len(nums) >= 3 else None
        conf = next((_markdown_confidence(c) for c in cells if _markdown_confidence(c) is not None), None)
        if "short" in d and "weak" not in d and "avoid" not in d:
            side = "sell"
        elif "long" in d and "weak" not in d and "avoid" not in d and "spec" not in d:
            side = "buy"
        else:
            signals.append({"symbol": pair, "side": "hold", "notional": 0, "reason": dir_cell})
            continue
        signals.append({
            "symbol": pair, "side": side, "notional": _FALLBACK_NOTIONAL,
            "entry": entry, "stop_loss": stop_loss, "take_profit": take_profit,
            "confidence": conf, "reason": dir_cell,
        })
    return signals


def check_exit(pos: dict, price: float, *, stop_loss: float, take_profit: float, trailing: float) -> str | None:
    """止盈/止损/移动止盈止损判定。返回平仓原因或 None。

    pos: {cost, peak, qty}；price: 当前价；三个阈值均为百分比正数。
    trailing: 峰值回撤百分比（先创新高后才生效）。
    """
    cost, peak = pos["cost"], max(pos.get("peak") or pos["cost"], pos["cost"])
    if cost <= 0 or price <= 0:
        return None
    pnl_pct = (price - cost) / cost * 100
    if pnl_pct <= -stop_loss:
        return f"stop-loss {pnl_pct:+.1f}% (cost {cost:.4f})"
    if pnl_pct >= take_profit:
        return f"take-profit {pnl_pct:+.1f}% (cost {cost:.4f})"
    if price > peak:
        pos["peak"] = price
        return None
    drawdown = (peak - price) / peak * 100
    if peak > cost and drawdown >= trailing:
        return f"trailing-stop drawdown {drawdown:.1f}% from peak {peak:.4f}"
    return None


def manage_positions(place, state: dict, prices: dict[str, float], *,
                     stop_loss: float, take_profit: float, trailing: float, trade: bool) -> None:
    """每轮先对现有持仓做止盈/止损/移动止盈止损检查，触发即卖出平仓。"""
    for sym, pos in list(state["positions"].items()):
        price = prices.get(sym)
        if not price or price <= 0:
            continue
        reason = check_exit(pos, price, stop_loss=stop_loss, take_profit=take_profit, trailing=trailing)
        if reason:
            qty = float(pos.get("qty") or 0)
            if qty <= 0:
                del state["positions"][sym]
                continue
            if not trade:
                _log({"ts": _now(), "symbol": sym, "side": "sell", "notional": 0,
                      "status": "signal", "reason": reason})
            else:
                try:
                    result = place(
                        sym, PROFILE, side="sell", quantity=qty,
                        order_type="market", session_id=f"testnet-exit-{int(time.time())}",
                    )
                    _log({"ts": _now(), "symbol": sym, "side": "sell", "quantity": qty,
                          "status": "order", "reason": reason, "result": result})
                    if str(result.get("status")) == "ok":
                        tg_send(f"🔴 平仓 {sym}  {qty:.4f}  — {reason}")
                except Exception as exc:  # noqa: BLE001 — 单笔失败不终止循环
                    _log({"ts": _now(), "symbol": sym, "side": "sell", "quantity": qty,
                          "status": "error", "reason": reason, "detail": str(exc)})
            del state["positions"][sym]
        elif reason is None and price > pos.get("peak", 0):
            save_state(state)  # peak 已上移


def run_round(ex, llm, *, trade: bool, stop_loss: float, take_profit: float, trailing: float) -> None:
    from src.trading.service import place_order

    state = load_state()
    symbols = fetch_top_symbols(ex)
    snapshot = build_market_snapshot(symbols, ex)
    if not snapshot:
        _log({"ts": _now(), "round": "error", "detail": "no market data"})
        return

    # 当前价格表（供止盈止损与成本记录使用）
    prices: dict[str, float] = {}
    for line in snapshot.splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            sym = parts[0].strip()
            try:
                last = float(parts[1].split("last=")[1].split(" ")[0])
                prices[sym] = last
            except (IndexError, ValueError):
                continue

    # 第一步：现有持仓的止盈/止损/移动止盈止损
    # 补齐持仓价格：不在 top10 的持仓也要检查（如 PAXG）
    for sym in state["positions"]:
        if sym not in prices:
            try:
                tk = ex.fetch_ticker(sym)
                last = float(tk.get("last") or 0)
                if last > 0:
                    prices[sym] = last
            except Exception:  # noqa: BLE001 — 单币拉价失败跳过
                pass
    manage_positions(place_order, state, prices,
                     stop_loss=stop_loss, take_profit=take_profit, trailing=trailing, trade=trade)

    prompt = (
        f"Current time (UTC): {_now()}\n"
        f"Top {len(symbols)} pairs by 24h volume:\n{snapshot}\n"
        "Generate trading signals now."
    )
    try:
        reply = llm.invoke(prompt)
        text = reply.content if hasattr(reply, "content") else str(reply)
    except Exception as exc:  # noqa: BLE001 — one bad call must not kill the loop
        _log({"ts": _now(), "round": "error", "detail": f"llm: {exc}"})
        save_state(state)
        return

    signals = parse_signals(text)
    if not signals:
        _log({"ts": _now(), "round": "error", "detail": "llm returned no parseable signals"})
        save_state(state)
        return

    for sig in signals:
        symbol = str(sig.get("symbol") or "").strip()
        side = str(sig.get("side") or "hold").strip().lower()
        notional = float(sig.get("notional") or 0)
        reason = str(sig.get("reason") or "")

        if symbol not in symbols:
            _log({"ts": _now(), "symbol": symbol, "side": side, "status": "rejected", "detail": "not in top list"})
            continue
        if side not in ("buy", "sell"):
            _log({"ts": _now(), "symbol": symbol, "side": side, "status": "hold", "reason": reason})
            continue
        notional = min(max(notional, 0), MAX_NOTIONAL)
        if notional <= 0:
            _log({"ts": _now(), "symbol": symbol, "side": side, "status": "hold", "reason": reason})
            continue

        if not trade:
            _log({"ts": _now(), "symbol": symbol, "side": side, "notional": notional,
                  "status": "signal", "reason": reason})
            continue

        try:
            result = place_order(
                symbol, PROFILE, side=side, notional=notional,
                order_type="market", session_id=f"testnet-loop-{int(time.time())}",
            )
            _log({"ts": _now(), "symbol": symbol, "side": side, "notional": notional,
                  "status": "order", "result": result, "reason": reason})
            if side == "buy" and str(result.get("status")) == "ok":
                tg_send(_signal_msg(sig, result))
                filled = float(result.get("filled") or 0)
                price = float(result.get("price") or prices.get(symbol) or 0)
                if filled > 0 and price > 0:
                    pos = state["positions"].setdefault(symbol, {"cost": price, "peak": price, "qty": 0.0})
                    total_qty = float(pos["qty"]) + filled
                    pos["cost"] = (float(pos["cost"]) * float(pos["qty"]) + price * filled) / total_qty
                    pos["qty"] = total_qty
                    pos["peak"] = max(float(pos.get("peak") or price), price)
            elif side == "sell" and str(result.get("status")) == "ok":
                tg_send(_signal_msg(sig, result))
                state["positions"].pop(symbol, None)
        except Exception as exc:  # noqa: BLE001 — fail one order, continue loop
            _log({"ts": _now(), "symbol": symbol, "side": side, "notional": notional,
                  "status": "error", "detail": str(exc)})

    save_state(state)
    _refresh_portfolio()


def _selftest() -> None:
    """止盈/止损/移动止盈止损判定自测。"""
    def mk(cost, peak=None, qty=1.0):
        return {"cost": cost, "peak": peak or cost, "qty": qty}

    # 止损：跌 6% (>5) 触发
    assert check_exit(mk(100.0), 94.0, stop_loss=5, take_profit=8, trailing=3) is not None
    # 止损未到：跌 4% 不触发
    assert check_exit(mk(100.0), 96.0, stop_loss=5, take_profit=8, trailing=3) is None
    # 止盈：涨 9% (>8) 触发
    assert check_exit(mk(100.0), 109.0, stop_loss=5, take_profit=8, trailing=3) is not None
    # 未到止盈：涨 5% 不触发
    assert check_exit(mk(100.0), 105.0, stop_loss=5, take_profit=8, trailing=3) is None
    # 移动止盈：peak=120，现价 115，回撤 4.2% (>3) 触发（cost 贴近 peak，止盈/止损不抢先）
    assert check_exit(mk(115.0, peak=120.0), 115.0, stop_loss=5, take_profit=8, trailing=3) is not None
    # 回撤未到：120→116.5 = 2.92% < 3 不触发
    assert check_exit(mk(115.0, peak=120.0), 116.5, stop_loss=5, take_profit=8, trailing=3) is None
    # 创新高：peak 上移且不触发（peak 恒 ≥ cost）
    pos = mk(112.0, peak=112.0)
    assert check_exit(pos, 114.0, stop_loss=5, take_profit=8, trailing=3) is None
    assert pos["peak"] == 114.0
    # 未创新高前（peak==cost）回撤不触发（防止刚买入小波动就离场）
    assert check_exit(mk(100.0), 97.0, stop_loss=5, take_profit=8, trailing=3) is None
    print("selftest OK")


# 单轮硬超时（秒）：防止单轮内任意网络/LLM 调用永久挂起拖死整个循环。
# 必须小于轮询间隔 interval。alarm 信号会中断阻塞中的 socket 调用。
class _RoundTimeout(Exception):
    """单轮超过硬时限。"""


def _round_timeout_handler(signum, frame):  # noqa: ARG001
    raise _RoundTimeout(f"round exceeded {signum} timeout")


def main() -> int:
    import signal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade", action="store_true", help="真实下单（默认 dry-run 只记录信号）")
    parser.add_argument("--interval", type=int, default=300, help="轮询间隔秒（默认 300）")
    parser.add_argument("--runs", type=int, default=96, help="最大轮数（默认 96 = 8 小时）")
    parser.add_argument("--stop-loss", type=float, default=5.0, help="止损百分比（默认 5）")
    parser.add_argument("--take-profit", type=float, default=8.0, help="止盈百分比（默认 8）")
    parser.add_argument("--trailing", type=float, default=3.0, help="移动止盈止损回撤百分比（默认 3）")
    parser.add_argument("--selftest", action="store_true", help="运行止盈止损判定自测")
    args = parser.parse_args()

    if args.selftest:
        _selftest()
        return 0

    from src.providers.llm import build_llm
    from src.trading.connectors.binance.sdk import build_config, _exchange

    llm = build_llm()
    ex = _exchange(build_config({"profile": "paper"}))
    print(f"[signal-loop] LLM 就绪 | {'TRADE 模式' if args.trade else 'dry-run 模式'} | 每 {args.interval}s 一轮 | 最多 {args.runs} 轮")
    print(f"[signal-loop] 止损 {args.stop_loss}% | 止盈 {args.take_profit}% | 移动止盈回撤 {args.trailing}%")
    print(f"[signal-loop] 日志: {LOG_PATH}")
    print(f"[signal-loop] 持仓状态: {STATE_PATH}")

    round_timeout = max(10, args.interval - 30)  # 预留 sleep 余量，不超 interval
    signal.signal(signal.SIGALRM, _round_timeout_handler)

    try:
        for i in range(args.runs):
            try:
                signal.alarm(round_timeout)
                run_round(ex, llm, trade=args.trade,
                          stop_loss=args.stop_loss, take_profit=args.take_profit, trailing=args.trailing)
            except _RoundTimeout as exc:
                _log({"ts": _now(), "round": "timeout", "detail": f"round timed out after {round_timeout}s"})
                tg_send(f"⚠️ 第 {i + 1} 轮超时（已跳过）：{exc}")
            except Exception as exc:  # noqa: BLE001 — 单轮任何异常（网络/交易所）都不能终止循环
                _log({"ts": _now(), "round": "error", "detail": f"round failed: {exc}"})
                tg_send(f"⚠️ 第 {i + 1} 轮异常（已跳过）：{exc}")
            finally:
                signal.alarm(0)
            if i < args.runs - 1:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[signal-loop] 已停止（Ctrl-C）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
