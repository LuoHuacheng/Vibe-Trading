#!/usr/bin/env python3
"""机械突破规则的长历史回放：拿样本量换信号保真度。

`replay_futures_exits.py` 用的是真实入场点，保真但两天只有 67 笔，任何参数改动
都过不了自助检验。这里换一个可复现的**机械信号**（20 根新高/新低 + 量比），在
几十天历史上生成上千笔入场，专门检验保护腿参数（止损距离、移动止损、止盈）和
入场方式（市价 vs 回踩）。

它测的不是当前 LLM 信号的收益，而是「突破追高 + 交易所侧保护」这个组合的结构
性质：止损该多宽、移动止损该多松。信度换的是外部效度——结论要回到真实入场点上
复核。

规则（全部只用已收盘的 K 线，无未来函数）：
  * 多头：收盘价 > 前 lookback 根最高价，且当根量 ≥ 前 lookback 根均量 × vol-ratio；
  * 空头：镜像；
  * 入场价 = 信号那根的收盘价（市价语义），模拟从下一根开始；
  * 同一品种两次入场至少间隔 min-gap 根（默认 12 = 1h），避免同一段行情被重复计；
  * 止损距离 / 移动止损 / 止盈由变体给出，出场三条腿与线上一致。

用法：
    python agent/scripts/replay_breakout_rules.py --days 60
    python agent/scripts/replay_breakout_rules.py --days 90 --timeframe 5m --json /tmp/b60.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent/
sys.path.insert(0, str(ROOT))

SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT",
           "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT", "LINK/USDT:USDT", "LTC/USDT:USDT")

#: 单笔名义额（USDT）——与线上 --max-positions 下的单笔规模同量级，便于和实盘对比。
NOTIONAL = 300.0

#: 本测试网账户实测费率（fapiPrivateGetCommissionRate）：maker 0.02% / taker 0.04%。
#: 入场可以挂 post-only 吃 maker；止损止盈是条件市价单，只能吃 taker。
MAKER_FEE_PCT = 0.02

DEFAULT_CACHE = Path("/tmp/vibe-replay-cache")


def _load_replay():
    """复用回放工具的模拟器，避免两处实现漂移。"""
    spec = importlib.util.spec_from_file_location(
        "replay_futures_exits", Path(__file__).with_name("replay_futures_exits.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_bars(ex, ccxt_symbol: str, timeframe: str, start_ms: int, end_ms: int,
               cache_dir: Path = DEFAULT_CACHE) -> list[list[float]]:
    """取 K 线：缓存 + 只补缺的那一段，返回**请求区间内**的 bar（按时间升序）。

    缓存键按小时对齐：`now` 每次跑都不同，用精确区间当键会让缓存永远不命中，
    每跑一次都重新拉 180 页。命中后只补最后一根到 end_ms 之间的缺口。
    """
    interval = {"1m": 60_000, "5m": 300_000}.get(timeframe, 300_000)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 每个品种一个文件、只往里补缺的那段：用「品种+精确区间」当键的话，`now` 一变
    # 键就变，每跑一次都从头拉 180 页。
    cache = cache_dir / f"{ccxt_symbol.replace('/', '_').replace(':', '_')}_{timeframe}.json"
    cached: list[list[float]] = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else []
    spans: list[tuple[int, int]] = []
    if not cached:
        spans.append((start_ms, end_ms))
    else:
        if cached[0][0] > start_ms + interval:                 # 前面缺
            spans.append((start_ms, int(cached[0][0])))
        if cached[-1][0] < end_ms - interval:                  # 后面缺（最后一根可能未收盘）
            spans.append((int(cached[-1][0]) + interval, end_ms))
    fresh: list[list[float]] = []
    for span_start, span_end in spans:
        cursor = span_start
        while cursor < span_end:
            chunk = ex.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, since=cursor, limit=1000)
            if not chunk:
                break
            fresh.extend(chunk)
            # 每页至少推进一根：交易所若返回一根停在 since 上的 K 线，按「末根 +1ms」
            # 推进会退化成几百万次请求（实测直接把进程挂死）。
            cursor = max(int(chunk[-1][0]) + interval, cursor + interval)
            time.sleep(0.15)
    if fresh:
        merged = {int(bar[0]): bar for bar in cached + fresh}
        cached = [merged[key] for key in sorted(merged)]
        cache.write_text(json.dumps(cached), encoding="utf-8")
    return [bar for bar in cached if start_ms <= bar[0] <= end_ms]


def regime_series(closes: list[float], period: int = 50) -> list[bool]:
    """逐根的 BTC 顺势标记，口径与生产 `_regime_verdict` 完全一致。

    生产那份每次都要重算整段 EMA，这里改成增量推进：17k 根 5m 逐根重算会把回放
    拖成十几分钟。正确性由测试对着 `_regime_verdict` 逐根比对。
    """
    flags = [False] * len(closes)
    if len(closes) < period + 1:
        return flags
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for idx in range(period, len(closes)):
        ema = closes[idx] * k + ema * (1 - k)
        lookback = min(99, idx)
        base = closes[idx - lookback]
        change = (closes[idx] / base - 1) * 100 if base else None
        flags[idx] = bool(ema > 0 and closes[idx] > ema and change is not None and change > 0)
    return flags


def find_entries(bars: list[list[float]], symbol: str, *, lookback: int = 20, vol_ratio: float = 1.5,
                 min_gap_bars: int = 12, notional: float = NOTIONAL) -> list[dict]:
    """机械突破信号：返回可直接喂给 simulate_* 的样本列表。"""
    entries: list[dict] = []
    last_taken = -10**9
    for idx in range(lookback, len(bars) - 1):
        window = bars[idx - lookback:idx]
        prior_high = max(bar[2] for bar in window)
        prior_low = min(bar[3] for bar in window)
        average_volume = sum(bar[5] for bar in window) / len(window)
        close = bars[idx][4]
        side = None
        if average_volume > 0:
            if close > prior_high and bars[idx][5] >= average_volume * vol_ratio:
                side = "long"
            elif close < prior_low and bars[idx][5] >= average_volume * vol_ratio:
                side = "short"
        if side is None or idx - last_taken < min_gap_bars:
            continue
        entries.append({
            "symbol": symbol, "side": side, "ts": int(bars[idx][0]),
            "price": close, "quantity": notional / close if close > 0 else 0.0,
            "bars": bars, "idx": idx + 1,      # 信号在收盘，模拟从下一根开始
            "atr": None,                        # 由 main 用生产口径补
            "signal_bar": idx,
        })
        last_taken = idx
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=60.0, help="回放历史长度（默认 60 天）")
    parser.add_argument("--timeframe", default="5m", choices=("1m", "5m"), help="入场与出场的 K 线周期")
    parser.add_argument("--symbols", default="", help="逗号分隔的品种（默认十个永续）")
    parser.add_argument("--lookback", type=int, default=20, help="突破窗口（根，默认 20）")
    parser.add_argument("--vol-ratio", type=float, default=1.5, help="量能倍数门槛（默认 1.5）")
    parser.add_argument("--min-gap-bars", type=int, default=12, help="同品种两次入场最小间隔（默认 12）")
    parser.add_argument("--trailing", type=float, default=3.0,
                        help="基准移动止损%（默认 3，与线上一致）")
    parser.add_argument("--trailing-grid", default="2,3,4,5", help="移动止损候选，逗号分隔")
    parser.add_argument("--take-profit", type=float, default=8.0, help="固定止盈%（默认 8，与线上一致）")
    parser.add_argument("--max-hold-bars", type=int, default=96, help="最长持有（根，默认 96=8h@5m）")
    parser.add_argument("--pullback-expiry", type=int, default=6, help="回踩限价单有效期（根，默认 6）")
    parser.add_argument("--maker-expiry", default="1,6",
                        help="post-only 入场有效期（根，逗号分隔，默认 1,6）")
    parser.add_argument("--json", default="", help="结果写出路径")
    args = parser.parse_args()

    from src.trading.connectors.binance.sdk import _exchange, build_config

    rp = _load_replay()
    spec = importlib.util.spec_from_file_location("fl", Path(__file__).with_name("futures_signal_loop.py"))
    fl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fl)

    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or list(SYMBOLS)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.days * 86400_000)
    ex = _exchange(build_config({"profile": "paper", "market_type": "usdm"}))

    samples: list[dict] = []
    for symbol in symbols:
        bars = fetch_bars(ex, symbol, args.timeframe, start_ms, end_ms)
        if len(bars) < args.lookback + 50:
            print(f"  {symbol}: 只有 {len(bars)} 根，跳过")
            continue
        found = find_entries(bars, symbol, lookback=args.lookback, vol_ratio=args.vol_ratio,
                             min_gap_bars=args.min_gap_bars)
        # ATR 用生产脚本的口径：bar 序号之前的序列
        for entry in found:
            entry["atr"] = fl._atr_pct(bars[:entry["signal_bar"] + 1])
        samples.extend([entry for entry in found if entry["atr"]])
        print(f"  {symbol}: {len(bars)} 根 → {len(found)} 个信号")

    if not samples:
        print("没有样本")
        return 1
    span = (max(s["ts"] for s in samples) - min(s["ts"] for s in samples)) / 86400_000
    print(f"\n样本 {len(samples)} 笔 | 覆盖 {span:.1f} 天 | "
          f"中位 ATR {statistics.median(s['atr'] for s in samples):.2f}% | "
          f"多头 {sum(1 for s in samples if s['side'] == 'long')} / "
          f"空头 {sum(1 for s in samples if s['side'] == 'short')}")
    print(f"规则：{args.lookback} 根新高/新低 + 量比>={args.vol_ratio}，"
          f"同品种间隔 >={args.min_gap_bars} 根，单笔名义额 {NOTIONAL:.0f} USDT，"
          f"单边费 {rp.FEE_PCT}%\n")

    variants: list[dict] = []
    trailing_grid = [float(item) for item in args.trailing_grid.split(",") if item.strip()]
    base_bracket = {"trailing_pct": args.trailing, "take_profit_pct": args.take_profit,
                    "max_hold_bars": args.max_hold_bars}

    # A 组：止损距离
    for label, distance in rp.STOP_CANDIDATES:
        results = [rp.simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                       stop_pct=distance(s["atr"]), **base_bracket) for s in samples]
        variants.append(rp._summarize(f"止损 {label}", results, len(samples)))
    # B 组：入场方式（止损沿用 2xATR）
    for label, pull_of in (("回踩 0.5xATR", lambda atr: 0.5 * atr),
                           ("回踩 1.0xATR", lambda atr: 1.0 * atr)):
        results = []
        for s in samples:
            outcome = rp.simulate_limit_entry(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                              pullback_pct=pull_of(s["atr"]),
                                              expiry_bars=args.pullback_expiry,
                                              stop_pct=max(2 * s["atr"], 0.05), **base_bracket)
            if outcome:
                results.append(outcome)
        variants.append(rp._summarize(f"入场 {label}", results, len(samples)))
    # C 组：移动止损松紧（止损沿用 2xATR）
    for trailing in trailing_grid:
        results = [rp.simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                       stop_pct=max(2 * s["atr"], 0.05), trailing_pct=trailing,
                                       take_profit_pct=args.take_profit,
                                       max_hold_bars=args.max_hold_bars) for s in samples]
        variants.append(rp._summarize(f"移动止损 {trailing:g}%", results, len(samples)))

    # D 组：maker 入场（就在信号价挂 post-only），出场仍吃 taker
    maker_expiries = [int(item) for item in args.maker_expiry.split(",") if item.strip()]
    for expiry in maker_expiries:
        label = f"maker {expiry}根"
        results = []
        for s in samples:
            outcome = rp.simulate_limit_entry(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                              pullback_pct=0.0, expiry_bars=expiry,
                                              stop_pct=max(2 * s["atr"], 0.05),
                                              fee_pct=MAKER_FEE_PCT, exit_fee_pct=rp.FEE_PCT,
                                              **base_bracket)
            if outcome:
                results.append(outcome)
        variants.append(rp._summarize(f"入场 {label}", results, len(samples)))

    header = (f"{'变体':<18}{'成交':>6}{'净盈亏':>10}{'胜率':>7}{'均盈':>7}{'均亏':>7}"
              f"{'期望/笔':>9}{'止损':>6}{'移动':>6}{'超时':>6}")
    print(header)
    print("-" * len(header))
    for row in variants:
        print(f"{row['variant']:<18}{row['filled']:>6}{row['net']:>10.2f}{row['win_rate']:>6.0f}%"
              f"{row['avg_win']:>7.2f}{row['avg_loss']:>7.2f}{row['expectancy']:>9.2f}"
              f"{row['stop_outs']:>6}{row['trailing_outs']:>6}{row['timeouts']:>6}")

    # 显著性：配对自助（同批样本换规则）
    print("\n配对自助（每笔差值，2000 次重采样）")
    baseline = dict(rp.STOP_CANDIDATES)["2xATR(现状)"]
    for label, distance in rp.STOP_CANDIDATES[1:]:
        stats = rp.paired_bootstrap_diff(samples, base_bracket, distance, baseline)
        verdict = "分不出来" if stats["p05"] <= 0 <= stats["p95"] else "稳定不为 0"
        print(f"  {label:<16} 每笔差 {stats['mean_diff_per_trade']:+.3f}  "
              f"5%~95% [{stats['p05']:+.3f}, {stats['p95']:+.3f}]  {verdict}")
    pull = next(row for row in variants if row["variant"] == "入场 回踩 1.0xATR")
    control = rp.bootstrap_skip_control(samples, base_bracket, pull["filled"], trials=300)
    print(f"\n对照｜随机只做 {control['keep']} 笔：均值 {control['mean']:.2f}，"
          f"5%~95% [{control['p05']:.2f}, {control['p95']:.2f}]  ← 回踩变体 {pull['net']:.2f}")

    # E 组：顺势闸。与生产同口径的 BTC 标记（px>EMA50 且近 100 根涨跌>0 = risk-on）
    btc_bars = fetch_bars(ex, "BTC/USDT:USDT", args.timeframe, start_ms, end_ms)
    flags = regime_series([bar[4] for bar in btc_bars])
    regime_by_ts = {int(bar[0]): flag for bar, flag in zip(btc_bars, flags)}
    for s in samples:
        s["risk_on"] = regime_by_ts.get(int(s["ts"]))
    usable = [s for s in samples if s.get("risk_on") is not None]
    ungated_net = variants[0]["net"]
    print(f"\n顺势闸（同口径 BTC 标记，覆盖 {len(usable)}/{len(samples)} 笔；"
          f"全量基线净 {ungated_net:+.2f}）")
    gate_specs = (
        ("空头闸：risk-on 不做空", lambda s: not (s["side"] == "short" and s["risk_on"])),
        ("多头闸：risk-off 不做多", lambda s: not (s["side"] == "long" and s["risk_on"] is False)),
        ("双闸", lambda s: not ((s["side"] == "short" and s["risk_on"])
                                or (s["side"] == "long" and s["risk_on"] is False))),
    )
    for label, keep in gate_specs:
        kept = [s for s in usable if keep(s)]
        skipped = [s for s in usable if not keep(s)]
        kept_row = rp._summarize(label, [rp.simulate_bracket(s["bars"], s["idx"], s["side"], s["price"],
                                                            s["quantity"],
                                                            stop_pct=max(2 * s["atr"], 0.05),
                                                            **base_bracket) for s in kept], len(usable))
        skipped_pnl = [rp.simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                           stop_pct=max(2 * s["atr"], 0.05), **base_bracket)["pnl"]
                       for s in skipped]
        ci = rp.bootstrap_mean_ci(skipped_pnl)
        verdict = ("删的是亏损单" if ci["p95"] < 0
                   else "删的是盈利单" if ci["p05"] > 0 else "分不出来")
        print(f"  {label:<20} 保留 {kept_row['filled']:>4} 笔 → 净 {kept_row['net']:+8.2f}，"
              f"期望/笔 {kept_row['expectancy']:+.3f} | 闸掉 {ci['n']:>4} 笔每笔 {ci['mean']:+.3f} "
              f"[{ci['p05']:+.3f}, {ci['p95']:+.3f}] {verdict}")

    # 费前/费后：毛边小于手续费时，参数怎么调都是在给交易所打工
    base_results = [rp.simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                        stop_pct=max(2 * s["atr"], 0.05), **base_bracket) for s in samples]
    gross = sum(r["gross"] for r in base_results)
    fees = sum(r["fees"] for r in base_results)
    notional_total = sum(abs(s["quantity"] * s["price"]) for s in samples)
    print(f"\n费前/费后（现状规则）：毛盈亏 {gross:+.2f} | 手续费 -{fees:.2f} | "
          f"净 {gross - fees:+.2f} | 双边费率 {fees / notional_total * 100:.4f}% / "
          f"毛边 {gross / notional_total * 100:+.4f}%")
    print(f"  → 每笔毛边 {gross / len(samples):+.3f} USDT，手续费 {fees / len(samples):.3f} USDT")

    # 多空与分段
    for side in ("long", "short"):
        part = [s for s in samples if s["side"] == side]
        if not part:
            continue
        row = rp._summarize(side, [rp.simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                                       stop_pct=max(2 * s["atr"], 0.05), **base_bracket)
                                   for s in part], len(part))
        print(f"{'多头' if side == 'long' else '空头'}：{row['filled']} 笔，净 {row['net']:+.2f}，"
              f"胜率 {row['win_rate']:.0f}%，期望/笔 {row['expectancy']:+.2f}")
    first, second = rp._split_half(samples)
    print("\n分段稳定（现状规则）：")
    for label, part in (("前半", first), ("后半", second)):
        row = rp._summarize(label, [rp.simulate_bracket(s["bars"], s["idx"], s["side"], s["price"], s["quantity"],
                                                        stop_pct=max(2 * s["atr"], 0.05), **base_bracket)
                                    for s in part], len(part))
        detail = []
        for side in ("long", "short"):
            legs = [s for s in part if s["side"] == side]
            if not legs:
                continue
            side_row = rp._summarize(side, [rp.simulate_bracket(s["bars"], s["idx"], s["side"], s["price"],
                                                               s["quantity"],
                                                               stop_pct=max(2 * s["atr"], 0.05), **base_bracket)
                                           for s in legs], len(legs))
            detail.append(f"{'多' if side == 'long' else '空'} {side_row['net']:+.2f}")
        print(f"  {label}: {row['filled']} 笔，净 {row['net']:+.2f}，期望/笔 {row['expectancy']:+.2f}"
              f"  [{' | '.join(detail)}]")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"samples": len(samples), "days": span, "variants": variants}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n已写出 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
