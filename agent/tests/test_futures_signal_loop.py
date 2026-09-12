"""合约信号循环脚本的离线回归测试。

``agent/scripts`` 不是包，所以脚本按路径加载。所有测试都不触网：券商读取、
下单、日志与状态文件全部替换成桩件。

覆盖的是「一个脏值炸掉一整轮」这一类缺陷：状态文件/券商返回里任何一格不是
数字，都不该让这一轮所有品种的持仓管理一起消失（实测 'dict <= 0' 就会）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "futures_signal_loop.py"
SYMBOL = "BTC/USDT:USDT"


def _load_loop():
    """按路径加载被测脚本（它不是可导入的包模块）。"""
    spec = importlib.util.spec_from_file_location("futures_signal_loop_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeExchange:
    """只实现 run_round 快照阶段需要的最小接口。"""

    markets = {SYMBOL: {"limits": {"amount": {"min": 0.001}, "cost": {"min": 10}}}}

    def __init__(self, bars):
        self._bars = bars

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        return self._bars[-limit:] if limit else self._bars

    def fetch_ticker(self, symbol):
        return {"last": self._bars[-1][4]}

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.6f}"


def _flat_bars(count: int = 120) -> list[list[float]]:
    """横盘 K 线（收 100）：不触发任何止盈止损，专测保护挂单路径。"""
    return [[float(i), 100.0, 100.5, 99.5, 100.0, 10.0] for i in range(count)]


class _FakeLLM:
    """固定回一个合法的空信号集：本轮没有机会。"""

    def invoke(self, prompt):  # noqa: ARG002 - 与真实 LLM 接口对齐
        return SimpleNamespace(content=json.dumps({"signals": []}))


@pytest.fixture()
def loop():
    return _load_loop()


def _wire(loop, monkeypatch, tmp_path, *, state, positions):
    """把脚本的外部依赖全部换成桩件，返回本轮日志的行列表（调用后读取）。"""
    log_path = tmp_path / "log.jsonl"
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(loop, "LOG_PATH", log_path)
    monkeypatch.setattr(loop, "STATE_PATH", state_path)
    monkeypatch.setattr(loop, "_refresh_portfolio", lambda: None)
    monkeypatch.setattr(loop, "tg_send", lambda text: None)
    monkeypatch.setattr(loop, "_broker_positions", lambda: list(positions))
    monkeypatch.setattr(loop, "_resting_protection_orders", lambda: {})
    return log_path, lambda: [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def _empty_state():
    return {"peaks": {}, "protection": {}, "open_interest": {}, "cooldowns": {}, "positions": {}}


def test_as_float_rejects_non_numeric(loop):
    """外部来源的数值要么转成 float，要么 None —— bool 不算数值。"""
    assert loop._as_float("1.5") == 1.5
    assert loop._as_float(3) == 3.0
    assert loop._as_float({"a": 1}) is None
    assert loop._as_float([1]) is None
    assert loop._as_float("soon") is None
    assert loop._as_float(None) is None
    assert loop._as_float(True) is None


def test_check_exit_survives_dirty_values(loop):
    """脏值进 check_exit 只能「判不了」（None），不能抛异常打断整轮。"""
    assert loop.check_exit({"side": "long", "entry": {"x": 1}}, 100.0,
                           stop_price={"s": 1}, take_profit=None, trailing=3) is None
    assert loop.check_exit({"side": "long", "entry": 100.0, "peak": {"p": 1}}, 100.0,
                           stop_price=95.0, take_profit=108.0, trailing=3) is None
    # 正常值的行为不变
    assert loop.check_exit({"side": "long", "entry": 100.0}, 94.0,
                           stop_price=95.0, take_profit=108.0, trailing=3) is not None


def test_exchange_side_stop_reason_classification(loop):
    """止损成交（止盈腿还挂着）才补记冷却；判不了就不记。"""
    tracked = {"stop_order_id": "s1", "trailing_order_id": "t1", "take_profit_order_id": "tp1"}
    live_tp = [{"order_id": "tp1", "order_type": "take_profit_market"}]
    assert loop._exchange_side_stop_reason(tracked, live_tp) != ""
    # 止损腿还挂着 → 这次平仓不是它干的
    assert loop._exchange_side_stop_reason(tracked, live_tp + [{"order_id": "s1"}]) == ""
    # 止盈也没了 → 分不清哪条腿成交
    assert loop._exchange_side_stop_reason(tracked, []) == ""
    # 没有本地记录 / 记录本身是脏值
    assert loop._exchange_side_stop_reason({}, live_tp) == ""
    assert loop._exchange_side_stop_reason("garbage", live_tp) == ""
    # 分类结果确实能让 _record_cooldown 生效（它只认 stop-/trailing- 前缀）
    state = {}
    loop._record_cooldown(state, SYMBOL, loop._exchange_side_stop_reason(tracked, live_tp))
    assert SYMBOL in state["cooldowns"]


def test_traceback_tail_keeps_the_stack(loop):
    """轮次异常要留下栈，否则只记 exc 定位不到出错的那一行。"""
    try:
        raise ValueError("boom")
    except ValueError:
        tail = loop._traceback_tail()
    assert "Traceback" in tail
    assert "ValueError: boom" in tail


def test_run_round_survives_dirty_protection_record(loop, monkeypatch, tmp_path):
    """保护记录里全是对象：整轮仍要跑完，并按百分比兜底算出止损/止盈。"""
    state = _empty_state()
    state["protection"] = {SYMBOL: {
        "stop_price": {"bad": 1}, "take_profit": {"bad": 2}, "quantity": {"bad": 3},
        "mode": "both", "stop_order_id": "1", "trailing_order_id": "2",
        "take_profit_order_id": "3",
    }}
    state["peaks"] = {SYMBOL: {"bad": 1}}
    positions = [{"symbol": SYMBOL, "side": "long", "quantity": 1.0, "entry_price": 100.0}]
    _, read_log = _wire(loop, monkeypatch, tmp_path, state=state, positions=positions)

    loop.run_round(
        _FakeExchange(_flat_bars()), _FakeLLM(), trade=False, top=10,
        symbols_arg=[SYMBOL], stop_loss=5.0, take_profit=8.0, trailing=3.0,
        margin_mode="isolated", leverage=5, protection="both", bars_limit=100,
        max_positions=10, derivatives=False, cooldown_hours=6.0,
        long_regime_gate=False, stop_floor_atr=0.0,
    )

    records = read_log()
    assert not [r for r in records if r.get("round") == "error"], records
    dry_run = [r for r in records if r.get("status") == "dry-run"
               and r.get("detail") == "would arm exchange-side protection"]
    assert dry_run, records
    # entry=100、stop-loss 5%、take-profit 8% → 兜底点位 95 / 108
    assert dry_run[0]["stop_price"] == pytest.approx(95.0)
    assert dry_run[0]["take_profit"] == pytest.approx(108.0)


def test_run_round_survives_non_dict_protection_record(loop, monkeypatch, tmp_path):
    """保护记录整个被写坏成字符串时，也不能让 .get 打断整轮。"""
    state = _empty_state()
    state["protection"] = {SYMBOL: "written-by-hand"}
    positions = [{"symbol": SYMBOL, "side": "short", "quantity": 2.0, "entry_price": 100.0}]
    _, read_log = _wire(loop, monkeypatch, tmp_path, state=state, positions=positions)

    loop.run_round(
        _FakeExchange(_flat_bars()), _FakeLLM(), trade=False, top=10,
        symbols_arg=[SYMBOL], stop_loss=5.0, take_profit=8.0, trailing=3.0,
        margin_mode="isolated", leverage=5, protection="both", bars_limit=100,
        max_positions=10, derivatives=False, cooldown_hours=6.0,
        long_regime_gate=False, stop_floor_atr=0.0,
    )

    records = read_log()
    assert not [r for r in records if r.get("round") == "error"], records
    assert [r for r in records if r.get("round") == "idle"], records


def test_run_round_skips_dirty_broker_rows(loop, monkeypatch, tmp_path):
    """券商返回的持仓行里有脏值：跳过那一行，其余持仓照常管理。"""
    state = _empty_state()
    positions = [
        {"symbol": SYMBOL, "side": "long", "quantity": {"bad": 1}, "entry_price": 100.0},
        "not-a-row",
        {"symbol": "ETH/USDT:USDT", "side": "long", "quantity": 1.0, "entry_price": 100.0},
    ]
    _, read_log = _wire(loop, monkeypatch, tmp_path, state=state, positions=positions)

    loop.run_round(
        _FakeExchange(_flat_bars()), _FakeLLM(), trade=False, top=10,
        symbols_arg=[SYMBOL, "ETH/USDT:USDT"], stop_loss=5.0, take_profit=8.0, trailing=3.0,
        margin_mode="isolated", leverage=5, protection="both", bars_limit=100,
        max_positions=10, derivatives=False, cooldown_hours=6.0,
        long_regime_gate=False, stop_floor_atr=0.0,
    )

    records = read_log()
    assert not [r for r in records if r.get("round") == "error"], records
    armed = {r.get("symbol") for r in records if r.get("status") == "dry-run"}
    assert armed == {"ETH/USDT:USDT"}, records


def test_manage_positions_cancels_stray_exchange_legs(loop, monkeypatch, tmp_path):
    """仓位还在但腿对不上时，交易所侧多挂出来的孤儿腿也要一起撤。

    只按本地记录撤，孤儿腿会一直活到 Binance 用 -4067 锁死该 symbol 的保证金模式。
    """
    state = _empty_state()
    state["protection"] = {SYMBOL: {
        "stop_price": 95.0, "take_profit": 108.0, "quantity": 9.0, "mode": "both",
        "stop_order_id": "s1", "trailing_order_id": "t1", "take_profit_order_id": "tp1",
    }}
    positions = [{"symbol": SYMBOL, "side": "long", "quantity": 1.0, "entry_price": 100.0}]
    log_path, _ = _wire(loop, monkeypatch, tmp_path, state=state, positions=positions)

    stray = {"order_id": "stray1", "order_type": "trailing_stop_market", "symbol": SYMBOL}
    open_rows = {SYMBOL: [stray]}
    # 桩件要跟着撤单走：交易所的条件单列表在撤掉之后就不该再报那条腿
    monkeypatch.setattr(loop, "_resting_protection_orders", lambda: {SYMBOL: list(open_rows[SYMBOL])})
    cancelled: list[str] = []

    def _cancel(order_id, profile=None, symbol=None):  # noqa: ARG001 - 对齐真实撤单签名
        cancelled.append(str(order_id))
        open_rows[SYMBOL] = [r for r in open_rows[SYMBOL] if str(r["order_id"]) != str(order_id)]
        return {"status": "ok"}

    monkeypatch.setattr(loop, "_cancel_algo", _cancel)
    armed: list[str] = []
    import src.trading.service  # noqa: F401 - 先导入，才能替换它导出的 place_order

    monkeypatch.setattr("src.trading.service.place_order",
                        lambda *args, **kwargs: armed.append(str(kwargs.get("order_type")))
                        or {"status": "ok", "order_id": "new-" + str(kwargs.get("order_type"))})

    loop.run_round(
        _FakeExchange(_flat_bars()), _FakeLLM(), trade=True, top=10,
        symbols_arg=[SYMBOL], stop_loss=5.0, take_profit=8.0, trailing=3.0,
        margin_mode="isolated", leverage=5, protection="both", bars_limit=100,
        max_positions=10, derivatives=False, cooldown_hours=6.0,
        long_regime_gate=False, stop_floor_atr=0.0,
    )

    assert "stray1" in cancelled, cancelled
    assert set(cancelled) >= {"s1", "t1", "tp1", "stray1"}, cancelled
    # 撤掉旧腿之后必须原样挂回来，不能留下没有保护的空仓
    assert set(armed) == {"stop_market", "trailing_stop_market", "take_profit_market"}, armed


def test_entry_log_records_signal_features(loop, monkeypatch, tmp_path):
    """入场记录必须带上信号特征，否则没法归因「哪类信号赚钱」。"""
    import src.trading.service  # noqa: F401 - 先导入，才能替换它导出的 place_order

    monkeypatch.setattr("src.trading.service.place_order",
                        lambda *args, **kwargs: {"status": "ok", "order_id": "o1",
                                                 "filled": kwargs.get("quantity"), "price": 100.0})
    monkeypatch.setattr(loop, "_cancel_algo", lambda *args, **kwargs: {"status": "ok"})
    payload = json.dumps({"signals": [{
        "symbol": SYMBOL, "side": "long", "notional": 200, "entry": 100.0,
        "stop_loss": 95.0, "take_profit": 108.0, "confidence": 0.72, "reason": "reclaim of range high",
    }]})
    llm = SimpleNamespace(invoke=lambda prompt: SimpleNamespace(content=payload))
    state = _empty_state()
    _, read_log = _wire(loop, monkeypatch, tmp_path, state=state, positions=[])

    loop.run_round(
        _FakeExchange(_flat_bars()), llm, trade=True, top=10, symbols_arg=[SYMBOL],
        stop_loss=5.0, take_profit=8.0, trailing=3.0, margin_mode="isolated",
        leverage=5, protection="both", bars_limit=100, max_positions=10,
        derivatives=False, cooldown_hours=6.0, long_regime_gate=False, stop_floor_atr=0.0,
    )

    entries = [r for r in read_log() if r.get("status") == "order" and r.get("reason") == "reclaim of range high"]
    assert entries, read_log()
    feature = entries[0]["signal"]
    assert feature["confidence"] == pytest.approx(0.72)
    assert feature["entry"] == pytest.approx(100.0)
    assert feature["stop_loss"] == pytest.approx(95.0)
    assert feature["take_profit"] == pytest.approx(108.0)
    assert feature["notional"] == pytest.approx(200.0)
    assert entries[0]["atr_pct"] == pytest.approx(1.0)   # 横盘 K 线：ATR 1.0/100 = 1%


def test_place_entry_uses_post_only_then_falls_back(loop):
    """post-only 立即成交吃 maker；被拒/超时撤单转市价；半成交补剩余。"""
    def make_env(limit_result=None, market_price=101.0):
        state = {"placed": [], "cancelled": []}

        def place(**kwargs):
            state["placed"].append(dict(kwargs))
            if kwargs.get("order_type") == "limit":
                return limit_result or {"status": "ok", "order_id": "L1", "filled": 0.0, "price": None}
            return {"status": "ok", "order_id": "M1", "filled": kwargs.get("quantity"),
                    "price": market_price}

        def cancel(order_id):
            state["cancelled"].append(order_id)
            return {"status": "ok"}

        return state, place, cancel

    def call(place, read_order, cancel, **kwargs):
        for key, value in (("side", "buy"), ("quantity", 2.0), ("limit_price", 100.0),
                           ("maker", True), ("maker_wait", 1.0)):
            kwargs.setdefault(key, value)
        return loop.place_entry(place, read_order, cancel, sleep=lambda _s: None, **kwargs)

    # 1) 立即成交 → maker，成交量/均价取自订单快照
    state, place, cancel = make_env()
    out = call(place, lambda _oid: {"status": "ok", "order_status": "closed",
                                    "filled": 2.0, "average": 100.0}, cancel)
    assert out["entry_style"] == "maker" and out["filled"] == 2.0 and out["price"] == 100.0
    assert state["placed"][0]["post_only"] is True and state["cancelled"] == []

    # 2) post-only 被拒（会立即吃单）→ 直接市价，不丢这一笔
    state, place, cancel = make_env(limit_result={"status": "error",
                                                  "error": "Order would immediately match and take"})
    out = call(place, lambda _oid: {"status": "ok", "order_status": "new"}, cancel)
    assert out["entry_style"] == "market" and "immediately match" in out["maker_reject"]
    assert [item["order_type"] for item in state["placed"]] == ["limit", "market"]

    # 3) 一直挂着 → 撤单 + 市价补全额
    state, place, cancel = make_env()
    out = call(place, lambda _oid: {"status": "ok", "order_status": "new", "filled": 0.0},
               cancel, side="sell", quantity=3.0)
    assert state["cancelled"] == ["L1"]
    assert out["entry_style"] == "maker+market" and out["filled"] == 3.0

    # 4) 半成交后超时 → maker 部分保留，缺口市价补，均价按两腿加权
    state, place, cancel = make_env()

    def partial(_oid):
        if state["cancelled"]:
            return {"status": "ok", "order_status": "canceled", "filled": 1.0, "average": 99.0}
        return {"status": "ok", "order_status": "new", "filled": 1.0, "average": 99.0}

    out = call(place, partial, cancel, quantity=2.0, limit_price=99.0)
    assert out["entry_style"] == "maker+market" and out["filled"] == 2.0
    assert out["price"] == pytest.approx((1.0 * 99.0 + 1.0 * 101.0) / 2.0)

    # 5) 读状态失败 → 撤单转市价（不把仓位悬着）
    state, place, cancel = make_env()
    out = call(place, lambda _oid: {"status": "error", "error": "network"}, cancel, quantity=1.0)
    assert state["cancelled"] == ["L1"] and out["entry_style"] == "maker+market"

    # 6) maker 关掉 → 直接市价，不碰限价路径
    state, place, cancel = make_env()
    out = call(place, lambda _oid: {"status": "ok", "order_status": "new"},
               cancel, maker=False, quantity=1.0)
    assert out["entry_style"] == "market" and state["placed"][0]["order_type"] == "market"


def test_maker_entry_price_uses_own_side_of_the_book(loop):
    """买单挂买一、卖单挂卖一：挂对手价会被交易所当吃单拒绝。"""
    class _Ex:
        def fetch_ticker(self, symbol):
            return {"bid": 99.5, "ask": 100.5}

    assert loop.maker_entry_price(_Ex(), "BTC/USDT:USDT", "buy") == 99.5
    assert loop.maker_entry_price(_Ex(), "BTC/USDT:USDT", "sell") == 100.5

    class _Broken:
        def fetch_ticker(self, symbol):
            raise RuntimeError("no book")

    assert loop.maker_entry_price(_Broken(), "BTC/USDT:USDT", "buy") == 0.0
