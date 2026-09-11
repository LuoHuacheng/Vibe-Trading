#!/usr/bin/env bash
# 安装 / 卸载 / 查看 Binance USDⓈ-M 测试网合约信号循环的 launchd 常驻服务（macOS）。
#
#   bash agent/scripts/launchd/install.sh install             # 默认：测试网真下单
#   bash agent/scripts/launchd/install.sh install --dry-run   # 只分析不下单
#   bash agent/scripts/launchd/install.sh install --symbols BTC/USDT:USDT --runs 96
#   bash agent/scripts/launchd/install.sh status|restart|logs|uninstall
#
# 默认值：十个主流 USDT 永续 · 每 300s 一轮 · 288 轮（约 24h，跑完由 KeepAlive
# 立刻开新一轮）· 交易所侧 both 保护（固定止损 + 移动止损 + 止盈，回调 3%）·
# 止损距离下限 2×ATR(14,5m) · 最多同时 10 仓 · isolated 5x。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TEMPLATE="$HERE/futures_signal_loop.plist.template"
LABEL="com.vibe-trading.futures-signal-loop"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
SYMBOLS="BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,BNB/USDT:USDT,XRP/USDT:USDT,DOGE/USDT:USDT,ADA/USDT:USDT,AVAX/USDT:USDT,LINK/USDT:USDT,LTC/USDT:USDT"
INTERVAL=300
RUNS=288
PROTECTION=both
TRAILING=3
STOP_FLOOR_ATR=2.0
MAX_POSITIONS=10
LEVERAGE=5
MARGIN_MODE=isolated
TRADE=1

usage() {
  sed -n '2,12p' "$0"
}

CMD=install
if [ $# -gt 0 ]; then CMD="$1"; shift; fi

while [ $# -gt 0 ]; do
  case "$1" in
    --symbols) SYMBOLS="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --protection) PROTECTION="$2"; shift 2 ;;
    --trailing) TRAILING="$2"; shift 2 ;;
    --stop-floor-atr) STOP_FLOOR_ATR="$2"; shift 2 ;;
    --max-positions) MAX_POSITIONS="$2"; shift 2 ;;
    --leverage) LEVERAGE="$2"; shift 2 ;;
    --margin-mode) MARGIN_MODE="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --dry-run) TRADE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

render() {
  mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/.vibe-dev/logs"
  if [ "$TRADE" = "1" ]; then
    sed -e "s|__TRADE_FLAG__|--trade|" "$TEMPLATE"
  else
    sed -e "/__TRADE_FLAG__/d" "$TEMPLATE"
  fi | sed \
      -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__ROOT__|$ROOT|g" \
      -e "s|__SYMBOLS__|$SYMBOLS|g" \
      -e "s|__INTERVAL__|$INTERVAL|g" \
      -e "s|__RUNS__|$RUNS|g" \
      -e "s|__PROTECTION__|$PROTECTION|g" \
      -e "s|__TRAILING__|$TRAILING|g" \
      -e "s|__STOP_FLOOR_ATR__|$STOP_FLOOR_ATR|g" \
      -e "s|__MAX_POSITIONS__|$MAX_POSITIONS|g" \
      -e "s|__LEVERAGE__|$LEVERAGE|g" \
      -e "s|__MARGIN_MODE__|$MARGIN_MODE|g" > "$PLIST"
  # 精确匹配真正的占位符：模板注释里也含有 __ 形式的说明文字，不能一概而论
  if grep -qE "__ROOT__|__PYTHON__|__SYMBOLS__|__INTERVAL__|__RUNS__|__PROTECTION__|__TRAILING__|__STOP_FLOOR_ATR__|__MAX_POSITIONS__|__LEVERAGE__|__MARGIN_MODE__|__TRADE_FLAG__" "$PLIST"; then
    echo "render left placeholders behind:" >&2
    grep -nE "__ROOT__|__PYTHON__|__SYMBOLS__|__INTERVAL__|__RUNS__|__PROTECTION__|__TRAILING__|__STOP_FLOOR_ATR__|__MAX_POSITIONS__|__LEVERAGE__|__MARGIN_MODE__|__TRADE_FLAG__" "$PLIST" >&2
    exit 1
  fi
}

case "$CMD" in
  install)
    render
    plutil -lint "$PLIST"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    # bootout 后立刻 bootstrap 会偶发 "Bootstrap failed: 5: Input/output error"
    # （launchd 还在卸载旧实例）。退避重试几次即可。
    sleep 2
    loaded=0
    for attempt in 1 2 3 4; do
      if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
        loaded=1
        break
      fi
      echo "bootstrap 第 $attempt 次失败，3s 后重试" >&2
      sleep 3
    done
    if [ "$loaded" != "1" ]; then
      echo "bootstrap 失败。可试：launchctl enable $DOMAIN/$LABEL; launchctl print-disabled $DOMAIN | grep $LABEL" >&2
      exit 1
    fi
    sleep 3
    launchctl print "$DOMAIN/$LABEL" | grep -E "state = |pid = " || true
    echo "installed: $PLIST"
    echo "logs:      $ROOT/.vibe-dev/logs/futures-loop.launchd.log"
    echo "records:   ~/.vibe-trading/futures_signal_log.jsonl"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "uninstalled $LABEL"
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "restarted $LABEL"
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state = |pid = |last exit code|program =" || echo "not loaded"
    ;;
  logs)
    tail -f "$ROOT/.vibe-dev/logs/futures-loop.launchd.log"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
