"""The price gate must reach the same verdict in every language it reads.

The gate keys on a phrase list, and a phrase list is written in whatever
language the bug report arrived in. Twice now that has left it strictly
leakier on one side than the other:

- ``\\bclose\\b`` does not match "closed at", which is how an answer actually
  states an observed price. "The stock closed at 412.35." with zero tool calls
  passed, while "该股收盘 412.35。" — the same fabricated claim — was caught.
  Same for "last traded at" and "quoted at".
- Chinese had the mirror-image hole: 成交价 / 最新价 / 股价 / 收报 are as
  ordinary as 收盘价 and none of them matched.

So the assertion here is deliberately NOT a literal per language. Each case
runs the same claim through both spellings and asserts the two verdicts are
EQUAL. A per-language literal is what let the two halves drift apart, and it
would let the next patch move the breakage to the other side while staying
green — which is exactly what #1249 does today (it inverts the asymmetry for
derived returns rather than removing it).

Both arms are pinned. A gate whose tests only assert rejection cannot see
itself closing on everything.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from src.agent.grounding import GroundingLedger


def _has_issues(text: str) -> bool:
    """Validate a final answer with no tool evidence at all."""
    ledger = GroundingLedger(
        run_dir=pathlib.Path(tempfile.mkdtemp()),
        user_message="analyze",
        history=None,
    )
    return bool(ledger.validate_final_answer(text).issues)


# Unsourced price claims: no tool ran, so both spellings must be rejected.
_MUST_REJECT = [
    ("The stock closed at 412.35.", "该股收盘 412.35。"),
    ("The stock last traded at 412.35.", "该股最新成交价 412.35。"),
    ("Shares were quoted at 412.35.", "该股报价 412.35。"),
    ("The share price is 412.35.", "该股股价 412.35。"),
    ("It opened at 400.10.", "开盘 400.10。"),
    ("The closing price was 412.35.", "收盘价为 412.35。"),
]

# Not price claims at all: both spellings must pass. Without this arm the
# suite is satisfied by a gate that rejects everything.
_MUST_ACCEPT = [
    ("The meeting closed at 5pm.", "会议 5 点结束。"),
    ("The deal closed at a 30% premium.", "交易以 30% 溢价成交。"),
    ("Volume traded 1200000 shares.", "成交量 1200000 股。"),
    ("I could not retrieve the price.", "我没能取到价格。"),
    ("Gross margin fell 3.6pp.", "毛利率下降 3.6 个百分点。"),
    # The past-tense verbs only count when followed by "at". Without that
    # constraint a bare "closed"/"traded" turns any nearby number into a
    # price claim, and these two are the cases that catch it — the numbers
    # are small enough that the aggregate-amount mask does not hide them.
    ("The position closed 3 days later.", "该仓位 3 天后平掉。"),
    ("They traded 8 times last week.", "他们上周交易了 8 次。"),
]


@pytest.mark.parametrize(("english", "chinese"), _MUST_REJECT)
def test_unsourced_price_claims_rejected_in_both_languages(
    english: str, chinese: str
) -> None:
    en, zh = _has_issues(english), _has_issues(chinese)
    assert en == zh, (
        f"verdicts disagree by language: EN={en} ZH={zh}\n"
        f"  EN: {english}\n  ZH: {chinese}"
    )
    assert en, "an unsourced price claim must be rejected"


@pytest.mark.parametrize(("english", "chinese"), _MUST_ACCEPT)
def test_non_price_statements_accepted_in_both_languages(
    english: str, chinese: str
) -> None:
    en, zh = _has_issues(english), _has_issues(chinese)
    assert en == zh, (
        f"verdicts disagree by language: EN={en} ZH={zh}\n"
        f"  EN: {english}\n  ZH: {chinese}"
    )
    assert not en, "a non-price statement must not be rejected"
