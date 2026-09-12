#!/usr/bin/env python3
"""离线回放：真实入场点 × 历史 K 线，反事实重演「止损距离」与「入场方式」。

为什么不是跑仓库的完整回测引擎：本脚本要回答的是「同一批真实入场点，换一套
止损/入场规则会怎样」，需要逐笔反事实重演；引擎是为「策略对象 × 数据源」设计
的，套上去只会把成交语义摊薄。这里只复用生产脚本的 ATR 口径（`_atr_pct`）与
实测手续费率，其余保持显式、可读、可断言。

事实基础（最近 24h 实测，见 futures_signal_log.jsonl + 交易所成交）：
  * 入场后 2h 中位 MFE +0.27% vs 中位 MAE -1.67%，72% 先亏后赚；
  * 平仓后 1h 有 64% 继续朝原方向走，中位 +0.76%；
  * 93 笔成交名义额 20,801 USDT，手续费 8.32 → 单边 0.04%（taker）。

模拟口径（已知近似，逐条写在下面，A/B 之间口径一致）：
  * 出场三条腿都挂在交易所（fixed stop / trailing / take-profit），谁先触发谁生效；
  * 1 分钟 K 线内无法判定触发顺序 → 同一根里按「止损 > 移动止损 > 止盈」取最差
    的那个，对每个变体一视同仁，所以横向比较仍然有效；
  * 移动止损按「入场后的极值回撤 callback%」计算（与 Binance 的语义一致）；
  * 手续费单边 0.04%，无滑点；超过 --max-hold-bars 没触发就按最后一根收盘价了结。

用法：
    python agent/scripts/replay_futures_exits.py                     # 默认最近 48h
    python agent/scripts/replay_futures_exits.py --hours 24 --json out.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent/
sys.path.insert(0, str(ROOT))

DEFAULT_LOG = Path.home() / ".vibe-trading" / "futures_signal_log.jsonl"
TRADE_PROFILE = "binance-futures-paper-trade"

#: 单边 taker 费率（%）。实测：24h 名义额 20,801 USDT、手续费 8.32 USDT → 0.04%。
FEE_PCT = 0.04

#: 脚本侧平仓的原因前缀：这些不是入场记录。
_EXIT_PREFIXES = ("stop-loss", "take-profit", "trailing-stop")


def _as_float(value: object) -> float | None:
    """外部数值 → float；非数值返回 None（与生产脚本同口径）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms(iso_ts: str) -> int:
    """ISO 时间 → 毫秒时间戳。"""
    return int(datetime.fromisoformat(iso_ts).timestamp() * 1000)


def load_entry_points(log_path: Path, since_ms: int) -> list[dict]:
    """从权威 jsonl 取真实入场：symbol / side / ts / quantity。

    入场写的是 `status=order` 且 reason 不是止损止盈；脚本侧平仓用的也是
    `status=order`，靠 reason 前缀区分。
    """
    entries: list[dict] = []
    if not log_path.exists():
        return entries
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") != "order" or not isinstance(record.get("result"), dict):
            continue
        reason = str(record.get("reason") or "")
        if not reason or reason.startswith(_EXIT_PREFIXES):
            continue
        if str((record.get("result") or {}).get("profile_id") or "") != TRADE_PROFILE:
            continue
        quantity = _as_float(record.get("quantity"))
        side = str(record.get("side") or "")
        if not quantity or side not in ("long", "short") or not record.get("ts"):
            continue
        timestamp = _ms(record["ts"])
        if timestamp < since_ms:
            continue
        entries.append({"symbol": str(record["symbol"]), "side": side,
                        "ts": timestamp, "quantity": quantity, "reason": reason})
    return entries


def fetch_fills(ex, symbols: list[str], since_ms: int) -> dict[str, list[dict]]:
    """取每个品种的成交明细（用于还原真实成交价）。"""
    fills: dict[str, list[dict]] = {}
    for ccxt_symbol in symbols:
        raw = ccxt_symbol.split(":")[0].replace("/", "")
        try:
            rows = ex.fapiPrivateGetUserTrades({"symbol": raw, "startTime": since_ms, "limit": 1000})
        except Exception:  # noqa: BLE001 - 某个品种读不到就跳过它
            continue
        fills[ccxt_symbol] = sorted(
            ({"ts": int(row["time"]), "price": float(row["price"]),
              "side": row["side"], "qty": float(row["qty"])} for row in rows),
            key=lambda item: item["ts"],
        )
    return fills


def match_fill_price(entry: dict, fills: list[dict], tolerance_ms: int = 180_000) -> float | None:
    """把入场记录对上真实成交价：同方向、时间最近的成交。"""
    want_side = "BUY" if entry["side"] == "long" else "SELL"
    best, best_gap = None, tolerance_ms + 1
    for fill in fills:
        if fill["side"] != want_side:
            continue
        gap = abs(fill["ts"] - entry["ts"])
        if gap < best_gap:
            best, best_gap = fill, gap
    return best["price"] if best else None


def fetch_minute_bars(ex, ccxt_symbol: str, start_ms: int, end_ms: int) -> list[list[float]]:
    """按 1000 根一页取 1 分钟 K 线，返回 [ts, open, high, low, close, volume]。"""
    bars: list[list[float]] = []
    cursor = start_ms - (start_ms % 60_000)
    while cursor < end_ms:
        chunk = ex.fetch_ohlcv(ccxt_symbol, timeframe="1m", since=cursor, limit=1000)
        if not chunk:
            break
        bars.extend(chunk)
        next_cursor = int(chunk[-1][0]) + 60_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    return [bar for bar in bars if start_ms <= bar[0] <= end_ms]


def to_five_minute(bars: list[list[float]]) -> list[list[float]]:
    """1 分钟聚合成 5 分钟（按 :00/:05 对齐），供 ATR 使用。"""
    buckets: dict[int, list[float]] = {}
    for ts, open_, high, low, close, volume in bars:
        key = ts - (ts % 300_000)
        row = buckets.get(key)
        if row is None:
            buckets[key] = [float(key), open_, high, low, close, volume]
        else:
            row[2] = max(row[2], high)
            row[3] = min(row[3], low)
            row[4] = close
            row[5] += volume
    return [buckets[key] for key in sorted(buckets)]


def simulate_bracket(bars: list[list[float]], start_idx: int, side: str, entry: float,
                     quantity: float, *, stop_pct: float, trailing_pct: float,
                     take_profit_pct: float, fee_pct: float = FEE_PCT,
                     exit_fee_pct: float | None = None,
                     max_hold_bars: int = 480) -> dict:
    """从 bars[start_idx] 起按三条腿模拟出场，返回一笔的完整结果。

    fee_pct 是入场腿费率；exit_fee_pct 缺省与它相同。分开是为了给「maker 入场 +
    taker 出场」（挂单省一半入场费，但止损止盈只能是市价）一个准确的口径。
    """
    long_side = side == "long"
    stop = entry * (1 - stop_pct / 100) if long_side else entry * (1 + stop_pct / 100)
    target = entry * (1 + take_profit_pct / 100) if long_side else entry * (1 - take_profit_pct / 100)
    peak = entry
    margin_high, margin_low = entry, entry
    end = min(len(bars), start_idx + max_hold_bars)
    exit_price, exit_reason, held = None, "timeout", end - start_idx
    for idx in range(start_idx, end):
        high, low, close = bars[idx][2], bars[idx][3], bars[idx][4]
        margin_high, margin_low = max(margin_high, high), min(margin_low, low)
        peak = max(peak, high) if long_side else min(peak, low)
        trail = peak * (1 - trailing_pct / 100) if long_side else peak * (1 + trailing_pct / 100)
        hit_stop = low <= stop if long_side else high >= stop
        hit_trail = trailing_pct > 0 and (low <= trail if long_side else high >= trail)
        hit_target = high >= target if long_side else low <= target
        # 同一根内无法判定顺序：按最差优先（止损 > 移动止损 > 止盈）
        if hit_stop:
            exit_price, exit_reason, held = stop, "stop", idx - start_idx
        elif hit_trail:
            exit_price, exit_reason, held = trail, "trailing", idx - start_idx
        elif hit_target:
            exit_price, exit_reason, held = target, "take-profit", idx - start_idx
        if exit_price is not None:
            break
    if exit_price is None:
        exit_price = bars[end - 1][4] if end > start_idx else entry
    gross = (exit_price - entry) * quantity if long_side else (entry - exit_price) * quantity
    exit_rate = fee_pct if exit_fee_pct is None else exit_fee_pct
    fees = entry * quantity * fee_pct / 100 + exit_price * quantity * exit_rate / 100
    if long_side:
        mfe = (margin_high / entry - 1) * 100
        mae = (margin_low / entry - 1) * 100
    else:
        mfe = (entry / margin_low - 1) * 100
        mae = (entry / margin_high - 1) * 100
    return {"exit_reason": exit_reason, "exit_price": exit_price, "pnl": gross - fees,
            "gross": gross, "fees": fees, "held_bars": held, "mfe_pct": mfe, "mae_pct": mae}


def simulate_limit_entry(bars: list[list[float]], start_idx: int, side: str, ref_price: float,
                         quantity: float, *, pullback_pct: float, expiry_bars: int, **bracket) -> dict | None:
    """挂限价单入场：N 根内没碰到就跳过这笔（返回 None）。

    pullback_pct = 0 → 就在参考价挂 post-only（吃 maker 费，只需一个 tick 的让步）；
    > 0 → 等回踩这么多百分点。成交价一律是限价，不假设更优的成交。
    """
    long_side = side == "long"
    limit = ref_price * (1 - pullback_pct / 100) if long_side else ref_price * (1 + pullback_pct / 100)
    end = min(len(bars), start_idx + expiry_bars)
    for idx in range(start_idx, end):
        low, high = bars[idx][3], bars[idx][2]
        if (long_side and low <= limit) or (not long_side and high >= limit):
            return simulate_bracket(bars, idx, side, limit, quantity, **bracket)
    return None


def bootstrap_skip_control(samples: list[dict], bracket: dict, keep: int,
                           trials: int = 300, seed: int = 7) -> dict:
    """随机跳过同样多的信号——「回踩入场」的改善可能只是「少做几笔」的功劳。

    对照组：从同一批样本里随机留 keep 笔，用线上现状的规则模拟。若回踩变体的净
    盈亏落在随机组的区间里，就不能说拉回入场本身有作用。
    """
    import random

    rng = random.Random(seed)
    nets: list[float] = []
    for _ in range(trials):
        picked = rng.sample(range(len(samples)), min(keep, len(samples)))
        nets.append(sum(
            simulate_bracket(samples[i]["bars"], samples[i]["idx"], samples[i]["side"],
                             samples[i]["price"], samples[i]["quantity"],
                             stop_pct=max(2 * samples[i]["atr"], 0.05), **bracket)["pnl"]
            for i in picked
        ))
    nets.sort()
    return {"trials": trials, "keep": keep, "mean": sum(nets) / len(nets),
            "p05": nets[int(0.05 * (len(nets) - 1))], "p95": nets[int(0.95 * (len(nets) - 1))]}


def paired_bootstrap_diff(samples: list[dict], bracket: dict, distance_a, distance_b,
                          trials: int = 2000, seed: int = 11) -> dict:
    """配对自助：同一批样本换止损距离，净盈亏之差是否稳定不为 0。

    逐笔差值再过重采样，能避开「总额被两三笔极端单带跑」的错觉。区间含 0 就只能
    说「这一批样本分不出来」。
    """
    import random

    rng = random.Random(seed)
    diffs = [
        simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                         stop_pct=distance_a(s["atr"]), **bracket)["pnl"]
        - simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                           stop_pct=distance_b(s["atr"]), **bracket)["pnl"]
        for s in samples
    ]
    means = []
    for _ in range(trials):
        means.append(sum(diffs[rng.randrange(len(diffs))] for _ in range(len(diffs))) / len(diffs))
    means.sort()
    return {"mean_diff_per_trade": sum(diffs) / len(diffs),
            "p05": means[int(0.05 * (len(means) - 1))],
            "p95": means[int(0.95 * (len(means) - 1))],
            "trials": trials}


def bootstrap_mean_ci(values: list[float], trials: int = 2000, seed: int = 13) -> dict:
    """一组数的均值的自助区间。

    用来判定「被顺势闸挡掉的那批单」是不是真的在亏：区间整体 < 0 才说明这道闸是在
    删亏损单，而不是把盈利单一起删掉。
    """
    import random

    if not values:
        return {"n": 0, "mean": 0.0, "p05": 0.0, "p95": 0.0}
    rng = random.Random(seed)
    means = []
    for _ in range(trials):
        means.append(sum(values[rng.randrange(len(values))] for _ in range(len(values))) / len(values))
    means.sort()
    return {"n": len(values), "mean": sum(values) / len(values),
            "p05": means[int(0.05 * (len(means) - 1))], "p95": means[int(0.95 * (len(means) - 1))]}


def _split_half(samples: list[dict]) -> tuple[list[dict], list[dict]]:
    """按时间把样本切成前后两半，用于看结论稳不稳。"""
    ordered = sorted(samples, key=lambda s: s["ts"])
    middle = len(ordered) // 2
    return ordered[:middle], ordered[middle:]


def _summarize(name: str, results: list[dict], attempted: int) -> dict:
    """把一个变体的逐笔结果压成一行指标。"""
    filled = len(results)
    wins = [r["pnl"] for r in results if r["pnl"] > 0]
    losses = [r["pnl"] for r in results if r["pnl"] <= 0]
    total = sum(r["pnl"] for r in results)
    return {
        "variant": name, "attempted": attempted, "filled": filled,
        "net": total, "win_rate": (len(wins) / filled * 100) if filled else 0.0,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "expectancy": (total / filled) if filled else 0.0,
        "stop_outs": sum(1 for r in results if r["exit_reason"] == "stop"),
        "trailing_outs": sum(1 for r in results if r["exit_reason"] == "trailing"),
        "timeouts": sum(1 for r in results if r["exit_reason"] == "timeout"),
        "median_hold": statistics.median([r["held_bars"] for r in results]) if results else 0,
    }


#: 止损距离候选：(标签, 由 ATR% 算距离的函数)。2xATR 与线上 --stop-floor-atr 2 同口径。
STOP_CANDIDATES: tuple[tuple[str, object], ...] = (
    ("2xATR(现状)", lambda atr: max(2 * atr, 0.05)),
    ("3xATR", lambda atr: max(3 * atr, 0.05)),
    ("4xATR", lambda atr: max(4 * atr, 0.05)),
    ("2xATR且>=1.2%", lambda atr: max(2 * atr, 1.2)),
    ("固定2%", lambda atr: 2.0),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="权威 jsonl 路径")
    parser.add_argument("--hours", type=float, default=48.0, help="回放入场点的时间窗口（默认 48h）")
    parser.add_argument("--trailing", type=float, default=3.0,
                        help="移动止损 callback%%（默认 3，与线上一致）")
    parser.add_argument("--take-profit", type=float, default=8.0, help="固定止盈%%（默认 8，与线上一致）")
    parser.add_argument("--max-hold-bars", type=int, default=480,
                        help="最长持有（1 分钟根数，默认 480=8h）")
    parser.add_argument("--pullback-expiry", type=int, default=6,
                        help="回踩限价单的有效期（根数，默认 6=6min）")
    parser.add_argument("--json", default="", help="把逐变体结果写到这个 json 文件")
    args = parser.parse_args()

    from src.trading.connectors.binance.sdk import _exchange, build_config

    import importlib.util

    spec = importlib.util.spec_from_file_location("fl", Path(__file__).with_name("futures_signal_loop.py"))
    fl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fl)

    since_ms = int(time.time() * 1000) - int(args.hours * 3600_000)
    entries = load_entry_points(Path(args.log), since_ms)
    if not entries:
        print("窗口内没有入场记录")
        return 1
    ex = _exchange(build_config({"profile": "paper", "market_type": "usdm"}))
    symbols = sorted({entry["symbol"] for entry in entries})
    start = min(entry["ts"] for entry in entries) - 3600_000
    end = int(time.time() * 1000) + 60_000
    fills = fetch_fills(ex, symbols, since_ms - 3600_000)
    # 每个品种只拉一次 1 分钟 K 线（逐笔拉会把窗口重复请求几十遍）
    bars_by_symbol = {symbol: fetch_minute_bars(ex, symbol, start, end) for symbol in symbols}

    samples: list[dict] = []
    for entry in entries:
        price = match_fill_price(entry, fills.get(entry["symbol"], []))
        if not price:
            continue
        bars = bars_by_symbol.get(entry["symbol"]) or []
        # 入场那根 1 分钟 K 线的下标；之后的模拟都从它开始
        index = next((i for i, bar in enumerate(bars) if bar[0] >= entry["ts"]), None)
        if index is None:
            continue
        five = to_five_minute(bars[:index + 1])
        atr = fl._atr_pct(five) if len(five) > 20 else None
        if not atr:
            continue
        samples.append({**entry, "price": price, "bars": bars, "idx": index, "atr": atr})

    if not samples:
        print("没有能配上成交价/ATR 的入场点")
        return 1
    print(f"样本 {len(samples)} 笔入场（{datetime.fromtimestamp(since_ms/1000, timezone.utc):%m-%d %H:%MZ} 起）"
          f" | 中位 ATR(14,5m) {statistics.median(s['atr'] for s in samples):.2f}%")
    print(f"模拟口径：移动止损 {args.trailing}% / 固定止盈 {args.take_profit}% / "
          f"单边手续费 {FEE_PCT}% / 最长 {args.max_hold_bars} 根\n")

    bracket = {"trailing_pct": args.trailing, "take_profit_pct": args.take_profit,
               "max_hold_bars": args.max_hold_bars}
    variants: list[dict] = []

    # A 组：只换止损距离（其余与线上一致）
    for label, distance in STOP_CANDIDATES:
        results = [simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                    stop_pct=distance(s["atr"]), **bracket)
                   for s in samples]
        variants.append(_summarize(f"止损 {label}", results, len(samples)))

    # B 组：入场方式（止损沿用线上现状 2xATR，回踩成交后保持同一距离%）
    pullbacks = (("回踩 0.5xATR", lambda atr: 0.5 * atr),
                 ("回踩 1.0xATR", lambda atr: 1.0 * atr),
                 ("回踩 0.3%", lambda atr: 0.3))
    for label, pull_of in pullbacks:
        results = []
        for s in samples:
            outcome = simulate_limit_entry(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                           pullback_pct=pull_of(s["atr"]),
                                           expiry_bars=args.pullback_expiry,
                                           stop_pct=max(2 * s["atr"], 0.05), **bracket)
            if outcome:
                results.append(outcome)
        variants.append(_summarize(f"入场 {label}", results, len(samples)))

    # 配对自助：换止损距离带来的逐笔差，是否稳定不为 0
    baseline = dict(STOP_CANDIDATES)["2xATR(现状)"]
    print("配对自助（每笔差值，2000 次重采样）")
    for label, distance in STOP_CANDIDATES[1:]:
        stats = paired_bootstrap_diff(samples, bracket, distance, baseline)
        verdict = "分不出来" if stats["p05"] <= 0 <= stats["p95"] else "稳定不为 0"
        print(f"  {label:<16} 每笔差 {stats['mean_diff_per_trade']:+.3f}  "
              f"5%~95% [{stats['p05']:+.3f}, {stats['p95']:+.3f}]  {verdict}")
    print()

    # 对照：随机只做同样多笔 —— 用来判定「回踩入场」是真效应还是「少做几笔」
    best_pullback = next(row for row in variants if row["variant"] == "入场 回踩 1.0xATR")
    if best_pullback["filled"]:
        control = bootstrap_skip_control(samples, bracket, best_pullback["filled"])
        print(f"对照｜随机只做 {control['keep']} 笔（{control['trials']} 次）："
              f"均值 {control['mean']:.2f}，5%~95% 区间 [{control['p05']:.2f}, {control['p95']:.2f}]"
              f"  ← 回踩变体 {best_pullback['net']:.2f}")
        if control["p05"] <= best_pullback["net"] <= control["p95"]:
            print("     落进随机组区间：不能归因于回踩入场，样本还不够。")
        else:
            print("     落在随机组区间之外：回踩入场本身有效应，不是单纯少做几笔。")
        print()

    # 分段稳定：前后两半各自跑一遍现状规则与回踩 1xATR
    first, second = _split_half(samples)
    print(f"分段稳定（前 {len(first)} 笔 / 后 {len(second)} 笔）：")
    for label, part in (("前半", first), ("后半", second)):
        if not part:
            continue
        base = _summarize("现状", [simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                                    stop_pct=max(2 * s["atr"], 0.05), **bracket) for s in part],
                          len(part))
        pull_results = [out for s in part
                        if (out := simulate_limit_entry(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                                        pullback_pct=1.0 * s["atr"],
                                                        expiry_bars=args.pullback_expiry,
                                                        stop_pct=max(2 * s["atr"], 0.05), **bracket))]
        pull = _summarize("回踩1xATR", pull_results, len(part))
        print(f"  {label}: 现状 {base['net']:>8.2f}（{base['filled']} 笔） | "
              f"回踩1xATR {pull['net']:>8.2f}（成交 {pull['filled']}/{len(part)}）")
    print()

    header = (f"{'变体':<18}{'成交':>6}{'净盈亏':>9}{'胜率':>7}{'均盈':>7}{'均亏':>7}"
              f"{'期望/笔':>9}{'止损':>6}{'移动':>6}{'超时':>6}")
    print(header)
    print("-" * len(header))
    for row in variants:
        print(f"{row['variant']:<18}{row['filled']:>6}{row['net']:>9.2f}{row['win_rate']:>6.0f}%"
              f"{row['avg_win']:>7.2f}{row['avg_loss']:>7.2f}{row['expectancy']:>9.2f}"
              f"{row['stop_outs']:>6}{row['trailing_outs']:>6}{row['timeouts']:>6}")
    if args.json:
        Path(args.json).write_text(json.dumps(variants, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
