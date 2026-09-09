"""Regression tests for parameter-dependent query tools in the agent loop."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.context import ContextBuilder
from src.agent.loop import AgentLoop
from src.agent.tools import ToolRegistry
from src.agent.trace import TraceWriter
from src.tools.fund_flow_tool import FundFlowTool
from src.tools.get_fundamentals_tool import GetFundamentalsTool
from src.tools.market_data_tool import MarketDataTool
from src.tools.market_screener_tool import MarketScreenerTool
from src.tools.symbol_search_tool import SymbolSearchTool


@pytest.mark.parametrize(
    ("tool_cls", "first_args", "second_args"),
    [
        (
            MarketDataTool,
            {
                "codes": ["AAPL.US"],
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
            },
            {
                "codes": ["MSFT.US"],
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
            },
        ),
        (
            GetFundamentalsTool,
            {
                "symbols": ["AAPL.US"],
                "fields": ["roe"],
                "start": "2025-01-01",
                "end": "2025-01-31",
            },
            {
                "symbols": ["MSFT.US"],
                "fields": ["roe"],
                "start": "2025-01-01",
                "end": "2025-01-31",
            },
        ),
        (
            MarketScreenerTool,
            {"market": "us", "sort_by": "volume", "top_n": 5},
            {"market": "hk", "sort_by": "amount", "top_n": 10},
        ),
        (
            SymbolSearchTool,
            {"query": "Apple", "limit": 5},
            {"query": "Microsoft", "limit": 5},
        ),
    ],
)
def test_repeatable_query_executes_again_with_different_arguments(
    monkeypatch,
    tmp_path: Path,
    tool_cls: type,
    first_args: dict[str, object],
    second_args: dict[str, object],
) -> None:
    """A successful query must not suppress the next iteration's symbol."""
    calls: list[dict[str, object]] = []
    tool = tool_cls()

    def _execute(**kwargs: object) -> str:
        calls.append(kwargs)
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(tool, "execute", _execute)
    registry = ToolRegistry()
    registry.register(tool)
    agent = AgentLoop(registry=registry, llm=SimpleNamespace(), max_iterations=2)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory.run_dir = str(run_dir)
    trace = TraceWriter(run_dir)
    messages: list[dict[str, object]] = []
    react_trace: list[dict[str, object]] = []

    for iteration, (call_id, arguments) in enumerate(
        (("call_first", first_args), ("call_second", second_args)), start=1
    ):
        agent._process_tool_calls(
            [
                SimpleNamespace(
                    id=call_id,
                    name=tool.name,
                    arguments=arguments,
                )
            ],
            ContextBuilder,
            messages,
            trace,
            react_trace,
            iteration,
        )
    trace.close()

    assert [
        {key: value for key, value in call.items() if key != "run_dir"}
        for call in calls
    ] == [first_args, second_args]
    assert len(messages) == 2
    assert not any(
        event["type"] == "tool_skipped" for event in TraceWriter.read(run_dir)
    )


def test_cleared_result_reopens_the_dedup_gate(monkeypatch, tmp_path: Path) -> None:
    """#1343: a non-repeatable tool blocked by the dedup ledger must become
    callable again once microcompact has cleared its only result.

    This drives the real gate in ``_process_tool_calls`` rather than only the
    return value of ``_microcompact``: the first call fills ``_called_ok``, the
    second is correctly skipped while the result is still readable, and the
    third must actually execute once the result has been cleared. Without the
    ledger update at the call site the third call is skipped too, which is the
    44-blocked-retries deadlock this fixes.
    """
    from src.agent.loop import KEEP_RECENT

    calls: list[dict[str, object]] = []
    # One of the five tools #1343 actually saw blocked; GetFundamentalsTool
    # above is repeatable and therefore has no gate to exercise.
    tool = FundFlowTool()
    assert not tool.repeatable, "test needs a non-repeatable tool to have a gate at all"

    def _execute(**kwargs: object) -> str:
        calls.append(kwargs)
        # Long enough that microcompact clears it (>100 chars).
        return json.dumps({"status": "ok", "rows": ["x" * 200]})

    monkeypatch.setattr(tool, "execute", _execute)
    registry = ToolRegistry()
    registry.register(tool)
    agent = AgentLoop(registry=registry, llm=SimpleNamespace(), max_iterations=5)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory.run_dir = str(run_dir)
    trace = TraceWriter(run_dir)
    messages: list[dict[str, object]] = []
    react_trace: list[dict[str, object]] = []
    args = {"code": "600584.SH"}

    def _call(call_id: str, iteration: int) -> None:
        agent._process_tool_calls(
            [SimpleNamespace(id=call_id, name=tool.name, arguments=dict(args))],
            ContextBuilder,
            messages,
            trace,
            react_trace,
            iteration,
        )

    _call("call_1", 1)
    assert len(calls) == 1, "first call must execute"

    _call("call_2", 2)
    assert len(calls) == 1, "second call must be skipped while the result is readable"

    # Pad past KEEP_RECENT so the real result is old enough to be cleared, then
    # run the actual production path rather than reaching into _called_ok.
    for i in range(KEEP_RECENT + 1):
        messages.append({
            "role": "tool",
            "tool_call_id": f"pad_{i}",
            "name": "padding_tool",
            "content": "y" * 200,
        })
    reopened = agent._microcompact_and_unblock(messages, trace, 3)
    assert tool.name in reopened, f"{tool.name} should have been re-opened, got {reopened}"

    _call("call_3", 4)
    trace.close()

    assert len(calls) == 2, (
        "third call must execute: its only result was cleared from context, so "
        "'use the previous result' points at [cleared]"
    )
    events = TraceWriter.read(run_dir)
    assert any(e["type"] == "microcompact_cleared" for e in events), (
        "the clear must leave a trace event; this layer used to act silently"
    )
