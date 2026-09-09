#!/usr/bin/env python3
"""把 testnet 信号循环日志导出为 Shadow Account 可用的 generic trade journal。

来源: ~/.vibe-trading/testnet_signal_log.jsonl（testnet_signal_loop 的订单日志）
目标: 与 ``src.tools.trade_journal_parsers`` 的 generic 格式兼容的 CSV，
      列 = datetime,symbol,name,side,quantity,price,amount,fee。

规则:
  * 只取 ``status == "order"`` 且 ``result.status == "ok"`` 的真实成交
    （signal = dry-run 信号、error/hold/rejected 均跳过）
  * 符号 ``BTC/USDT`` → ``BTC-USDT``（crypto 行情 loader 用的 OKX 风格）
  * 输出后顺带做 FIFO 配对统计，提示盈利 roundtrip 是否达到
    Shadow 提取门槛（>=5）。

用法:
  python agent/scripts/export_testnet_journal.py [--log PATH] [--output PATH]
  python agent/scripts/export_testnet_journal.py --selftest
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # agent/
sys.path.insert(0, str(ROOT))

DEFAULT_LOG = Path.home() / ".vibe-trading" / "testnet_signal_log.jsonl"
DEFAULT_OUTPUT = Path.home() / ".vibe-trading" / "testnet_journal.csv"
MIN_PROFITABLE = 5


def _is_filled_order(record: dict) -> bool:
    """成交记录判定：status=order 且 result.status=ok。"""
    return (
        record.get("status") == "order"
        and isinstance(record.get("result"), dict)
        and record.get("result", {}).get("status") == "ok"
    )


def _to_row(record: dict) -> dict | None:
    """一条成交记录 → generic journal 行；缺价格/数量返回 None。"""
    res = record.get("result") or {}
    price = float(res.get("price") or 0)
    filled = float(res.get("filled") or res.get("amount") or 0)
    if not price or not filled:
        return None
    side = str(record.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        return None
    symbol = str(record.get("symbol") or "").strip().replace("/", "-").upper()
    if not symbol:
        return None
    amount = round(price * filled, 8)
    return {
        "datetime": str(record.get("ts") or "").replace("T", " ").replace("+00:00", ""),
        "symbol": symbol,
        "name": symbol,
        "side": side,
        "quantity": round(filled, 8),
        "price": price,
        "amount": amount,
        "fee": 0.0,
    }


def convert_log(log_path: Path, output_path: Path) -> list[dict]:
    """读取日志并写出 journal CSV；返回写出的行（供统计复用）。"""
    rows: list[dict] = []
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _is_filled_order(record):
                row = _to_row(record)
                if row:
                    rows.append(row)
    rows.sort(key=lambda r: r["datetime"])
    if not rows:
        raise ValueError(f"No filled orders in {log_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _pair_stats(journal_path: Path) -> tuple[int, int, float]:
    """FIFO 配对统计：(roundtrips, profitable, total_pnl)。"""
    from src.tools.trade_journal_parsers import parse_file, records_to_dataframe
    from src.tools.trade_journal_tool import pair_trades_fifo

    _fmt, records = parse_file(journal_path)
    roundtrips = pair_trades_fifo(records_to_dataframe(records))
    profitable = [rt for rt in roundtrips if rt["pnl"] > 0]
    total_pnl = sum(rt["pnl"] for rt in roundtrips)
    return len(roundtrips), len(profitable), total_pnl


def _selftest() -> None:
    """最小自检：已知记录 → 期望行数/字段/过滤行为。"""
    sample = [
        {"ts": "2026-09-01T06:08:30+00:00", "symbol": "BTC/USDT", "side": "buy",
         "status": "order", "result": {"status": "ok", "price": 79052.01, "filled": 0.00632}},
        {"ts": "2026-09-01T06:09:00+00:00", "symbol": "BTC/USDT", "side": "sell",
         "status": "order", "result": {"status": "ok", "price": 79500.0, "filled": 0.00632}},
        {"ts": "2026-09-01T06:10:00+00:00", "symbol": "SOL/USDT", "side": "buy",
         "status": "signal", "reason": "dry-run"},  # 跳过: signal
        {"ts": "2026-09-01T06:11:00+00:00", "symbol": "XRP/USDT", "side": "buy",
         "status": "order", "result": {"status": "error"}},  # 跳过: result 非 ok
        "not a json line\n",  # 跳过: 坏行
    ]
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "log.jsonl"
        out = Path(td) / "journal.csv"
        log.write_text("".join(json.dumps(r) + "\n" if isinstance(r, dict) else r for r in sample))
        rows = convert_log(log, out)
        assert len(rows) == 2, f"expect 2 rows, got {len(rows)}"
        assert rows[0]["symbol"] == "BTC-USDT", rows[0]
        assert rows[1]["side"] == "sell" and rows[0]["side"] == "buy"
        assert rows[1]["symbol"] == "BTC-USDT" and rows[0]["symbol"] == rows[1]["symbol"]
        with out.open() as fh:
            header = fh.readline().strip().split(",")
        assert header == ["datetime", "symbol", "name", "side", "quantity", "price", "amount", "fee"]
        n, prof, pnl = _pair_stats(out)
        assert (n, prof) == (1, 1) and pnl > 0
    print("selftest OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help=f"信号日志（默认 {DEFAULT_LOG}）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"输出 journal CSV（默认 {DEFAULT_OUTPUT}）")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args()

    if args.selftest:
        _selftest()
        return 0

    try:
        rows = convert_log(args.log, args.output)
    except FileNotFoundError:
        print(f"[export] 日志不存在: {args.log}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"[export] {exc}", file=sys.stderr)
        return 1

    print(f"[export] 写出 {len(rows)} 笔成交 -> {args.output}")
    print(f"[export] 范围 {rows[0]['datetime'][:16]} ~ {rows[-1]['datetime'][:16]}")
    try:
        roundtrips, profitable, total_pnl = _pair_stats(args.output)
    except Exception as exc:  # noqa: BLE001 — 配对统计失败不影响导出本身
        print(f"[export] 配对统计失败（跳过）: {exc}")
        return 0
    print(f"[export] FIFO roundtrip {roundtrips} 笔 | 盈利 {profitable} 笔 | 合计盈亏 {total_pnl:+.2f} USDT")
    if profitable < MIN_PROFITABLE:
        print(f"[export] 盈利 roundtrip < {MIN_PROFITABLE}，Shadow 提取暂不可用；继续积累数据后重跑")
    else:
        print(f"[export] 盈利 roundtrip >= {MIN_PROFITABLE}，可直接运行 Shadow 提取")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
