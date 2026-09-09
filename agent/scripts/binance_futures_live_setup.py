#!/usr/bin/env python3
"""Binance USDⓈ-M 实盘启用入口（跳过实盘后的保留入口）。

后续要开启实盘合约交易时，用它把 live key 填进连接器配置：

  1. 在 Binance 主账户开通 USDⓈ-M 交易权限并签发 API key（fapi 权限；
     只读检查可先用只读 key）。
  2. 把 key 写进 env 文件（见下方示例）。
  3. 先只读自检（不写任何配置、不触达下单）：
         python agent/scripts/binance_futures_live_setup.py check --env <path>
  4. 确认无误后激活（自动备份现有 binance.json）：
         python agent/scripts/binance_futures_live_setup.py activate --env <path> --yes
  5. 实盘下单前：为 broker=binance 提交 mandate（allowed_instruments 含
     crypto、asset_classes 含 crypto），随后经 binance-futures-live-trade
     profile + orders.place.requires_mandate 门禁下单。

激活会改写 ~/.vibe-trading/binance.json（连接器单配置槽：key 与 profile
同文件）。原配置自动备份为 binance.json.bak-<时间戳>，可随时：

         python agent/scripts/binance_futures_live_setup.py restore

示例 env 文件（如 ~/.local-trading/live.env）：
    export BINANCE_LIVE_API_KEY="your-key"
    export BINANCE_LIVE_API_SECRET="your-secret"
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_ROOT = REPO_ROOT / "agent"
sys.path.insert(0, str(AGENT_ROOT))

_ENV_PREFIX = "export BINANCE_LIVE_API_"


def _sdk():
    from src.trading.connectors.binance import sdk as bn  # type: ignore

    return bn


def load_env_file(path: Path) -> dict[str, str]:
    """Parse the env file into {key, secret}; missing/duplicate handled."""
    creds: dict[str, str] = {}
    for raw in path.open(encoding="utf-8"):
        line = raw.strip()
        if not line.startswith(_ENV_PREFIX):
            continue
        key_part, _, value_part = line.partition("=")
        field = key_part[len(_ENV_PREFIX):].strip()
        if field not in ("KEY", "SECRET"):
            continue
        value = value_part.strip().strip('"').strip("'")
        if field in creds:
            raise ValueError("duplicate BINANCE_LIVE_API_" + field + " in " + str(path))
        creds[field] = value
    if set(creds) != {"KEY", "SECRET"}:
        raise ValueError(str(path) + " must define BINANCE_LIVE_API_KEY and BINANCE_LIVE_API_SECRET")
    return {"key": creds["KEY"], "secret": creds["SECRET"]}


def build_payload(creds: dict[str, str]) -> dict[str, object]:
    """Saved-config payload for the live USDⓈ-M trading profile."""
    return {
        "profile": "live",
        "market_type": "usdm",
        "api_key": creds["key"],
        "api_secret": creds["secret"],
    }


def _config_path() -> Path:
    return Path(_sdk().config_path())


def cmd_status() -> int:
    bn = _sdk()
    print("config_path:", bn.config_path())
    print("live futures profile: binance-futures-live-trade (orders.place.requires_mandate)")
    try:
        cfg = bn.load_config()
        print("saved profile:", cfg.profile, "| market_type:", cfg.market_type)
        print("saved key prefix:", (cfg.api_key[:4] + "***") if cfg.api_key else "(empty)")
    except Exception as exc:  # noqa: BLE001
        print("saved config error:", type(exc).__name__, str(exc)[:160])
    print("hint: activate 子命令把 live key 写入该文件（自动备份，需 --yes）")
    return 0


def cmd_check(env: Path) -> int:
    creds = load_env_file(env)
    bn = _sdk()
    cfg = bn.BinanceConfig.from_mapping(
        {"profile": "live", "market_type": "usdm", "api_key": creds["key"], "api_secret": creds["secret"]}
    )
    print("live host:", cfg.host, "| market_type:", cfg.market_type)
    st = bn.check_status(cfg)
    if st.get("status") != "ok":
        print("check FAILED:", st.get("error", st))
        return 1
    print("check ok | account:", st.get("account"))
    print("未写入任何配置；确认无误后用 activate 激活。")
    return 0


def cmd_activate(env: Path, yes: bool) -> int:
    if not yes:
        print("激活会改写 ~/.vibe-trading/binance.json（会先备份）。请加 --yes 确认。")
        return 2
    creds = load_env_file(env)
    bn = _sdk()
    path = _config_path()
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(path.name + ".bak-" + stamp)
        shutil.copy2(path, backup)
        print("backup:", backup)
    bn.save_config(bn.BinanceConfig.from_mapping(build_payload(creds)))
    print("activated: profile=live market_type=usdm ->", path)
    print("下一步：提交 mandate（broker=binance）后经 binance-futures-live-trade 下单。")
    return 0


def cmd_restore() -> int:
    path = _config_path()
    candidates = sorted(path.parent.glob(path.name + ".bak-*"), reverse=True)
    if not candidates:
        print("no backup found under", path.parent)
        return 1
    shutil.copy2(candidates[0], path)
    print("restored from", candidates[0])
    return 0


def _selftest() -> None:
    import tempfile

    bn = _sdk()
    with tempfile.TemporaryDirectory() as td:
        env = Path(td) / "live.env"
        env.write_text(
            'export BINANCE_LIVE_API_KEY="k123"\n'
            'export BINANCE_LIVE_API_SECRET="s456"\n'
            "# comment line ignored\n"
            'export BINANCE_CREDENTIAL_ENVIRONMENT="LIVE"\n'
        )
        creds = load_env_file(env)
        assert creds == {"key": "k123", "secret": "s456"}, creds
        payload = build_payload(creds)
        assert payload["profile"] == "live" and payload["market_type"] == "usdm"
        cfg = bn.BinanceConfig.from_mapping(payload)
        assert cfg.is_testnet is False and cfg.host == bn.USDM_LIVE_HOST
        try:
            load_env_file(Path(td) / "missing.env")
            raise SystemExit("selftest failed: missing file should raise")
        except FileNotFoundError:
            pass
        partial = Path(td) / "partial.env"
        partial.write_text('export BINANCE_LIVE_API_KEY="k"\n')
        try:
            load_env_file(partial)
            raise SystemExit("selftest failed: partial file should raise")
        except ValueError:
            pass
    print("selftest OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看当前 binance 连接器配置与激活提示")
    p_check = sub.add_parser("check", help="用 live env 里的 key 做只读自检（不写配置）")
    p_check.add_argument("--env", required=True, type=Path)
    p_act = sub.add_parser("activate", help="把 live key 写入连接器配置（自动备份）")
    p_act.add_argument("--env", required=True, type=Path)
    p_act.add_argument("--yes", action="store_true")
    sub.add_parser("restore", help="从最近备份恢复 binance.json")
    sub.add_parser("selftest", help="运行离线自检")
    args = parser.parse_args()

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "check":
        return cmd_check(args.env)
    if args.cmd == "activate":
        return cmd_activate(args.env, args.yes)
    if args.cmd == "restore":
        return cmd_restore()
    if args.cmd == "selftest":
        _selftest()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
