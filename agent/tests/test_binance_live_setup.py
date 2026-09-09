"""Unit tests for the Binance USDⓈ-M live enablement entry script."""
from __future__ import annotations

import pytest

from scripts.binance_futures_live_setup import build_payload, load_env_file


def test_load_env_file_parses_keys_and_ignores_other_lines(tmp_path):
    env = tmp_path / "live.env"
    env.write_text('''export BINANCE_LIVE_API_KEY="k123"
export BINANCE_LIVE_API_SECRET="s456"
# comment line ignored
export BINANCE_CREDENTIAL_ENVIRONMENT="LIVE"
''')
    assert load_env_file(env) == {"key": "k123", "secret": "s456"}


def test_load_env_file_requires_both_keys(tmp_path):
    env = tmp_path / "live.env"
    env.write_text('''export BINANCE_LIVE_API_KEY="k123"
''')
    with pytest.raises(ValueError, match="BINANCE_LIVE_API_KEY and BINANCE_LIVE_API_SECRET"):
        load_env_file(env)


def test_load_env_file_rejects_duplicate_key(tmp_path):
    env = tmp_path / "live.env"
    env.write_text('''export BINANCE_LIVE_API_KEY="k1"
export BINANCE_LIVE_API_KEY="k2"
export BINANCE_LIVE_API_SECRET="s"
''')
    with pytest.raises(ValueError, match="duplicate"):
        load_env_file(env)


def test_build_payload_targets_live_usdm():
    payload = build_payload({"key": "k1", "secret": "s1"})
    assert payload == {
        "profile": "live",
        "market_type": "usdm",
        "api_key": "k1",
        "api_secret": "s1",
    }
