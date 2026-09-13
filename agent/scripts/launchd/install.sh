#!/usr/bin/env bash
# 安装 / 卸载 / 查看 Binance USDⓈ-M 测试网合约信号循环的 launchd 服务（macOS）。
#
#   bash agent/scripts/launchd/install.sh install             # 只安装：渲染 plist，不装载、不运行
#   bash agent/scripts/launchd/install.sh install --dry-run   # 只分析不下单
#   bash agent/scripts/launchd/install.sh install --symbols BTC/USDT:USDT --runs 96
#   bash agent/scripts/launchd/install.sh start               # 手动启动（常驻循环）
#   bash agent/scripts/launchd/install.sh stop                # 手动停止
#   bash agent/scripts/launchd/install.sh status|restart|logs|uninstall
#
# 不随登录自启：plist 渲染到 ~/.vibe-trading/launchd/，而不是 ~/Library/LaunchAgents/。
# launchd 只在登录时自动装载后者，所以放在前者 = 没人显式 bootstrap 就不存在。
# 注意 KeepAlive=true 会让任务「一被装载就运行」（实测 RunAtLoad=false 拦不住），
# 因此唯一的运行入口是 start，stop（bootout）之后即停死。
# 默认值：十个主流 USDT 永续 · 每 300s 一轮 · 288 轮（约 24h，跑完由 KeepAlive
# 立刻开新一轮）· 交易所侧 both 保护（固定止损 + 移动止损 + 止盈，回调 1.5%）·
# 最低置信度 0.6 · 再入场冷却 15m · 每轮最多 3 仓 · 止损距离下限 2×ATR(14,5m) ·
# 最多同时 10 仓 · isolated 5x。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TEMPLATE="$HERE/futures_signal_loop.plist.template"
LABEL="com.vibe-trading.futures-signal-loop"
# 刻意不放 ~/Library/LaunchAgents/：那个目录会在登录时被 launchd 自动装载，
# 而 KeepAlive=true 会让装载即刻变成运行（= 登录自启）。
STATE_DIR="$HOME/.vibe-trading/launchd"
PLIST="$STATE_DIR/$LABEL.plist"
# 旧版本装在这里。留着它登录就会跑，install/uninstall 都会顺手清掉。
LEGACY_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG="$ROOT/.vibe-dev/logs/futures-loop.launchd.log"

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
SYMBOLS="BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,BNB/USDT:USDT,XRP/USDT:USDT,DOGE/USDT:USDT,ADA/USDT:USDT,AVAX/USDT:USDT,LINK/USDT:USDT,LTC/USDT:USDT"
INTERVAL=300
RUNS=288
PROTECTION=both
TRAILING=1.5
MIN_CONFIDENCE=0.6
REENTRY_COOLDOWN_MIN=15
MAX_SIGNALS_PER_ROUND=3
STOP_FLOOR_ATR=2.0
MAX_POSITIONS=10
LEVERAGE=5
MARGIN_MODE=isolated
TRADE=1

usage() {
  # 打印文件顶部的用法注释（第 2 行到 set -euo pipefail 之前），这样加命令时不用手改行号。
  sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d'
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
    --min-confidence) MIN_CONFIDENCE="$2"; shift 2 ;;
    --reentry-cooldown-min) REENTRY_COOLDOWN_MIN="$2"; shift 2 ;;
    --max-signals-per-round) MAX_SIGNALS_PER_ROUND="$2"; shift 2 ;;
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
  mkdir -p "$STATE_DIR" "$ROOT/.vibe-dev/logs"
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
      -e "s|__MIN_CONFIDENCE__|$MIN_CONFIDENCE|g" \
      -e "s|__REENTRY_COOLDOWN_MIN__|$REENTRY_COOLDOWN_MIN|g" \
      -e "s|__MAX_SIGNALS_PER_ROUND__|$MAX_SIGNALS_PER_ROUND|g" \
      -e "s|__STOP_FLOOR_ATR__|$STOP_FLOOR_ATR|g" \
      -e "s|__MAX_POSITIONS__|$MAX_POSITIONS|g" \
      -e "s|__LEVERAGE__|$LEVERAGE|g" \
      -e "s|__MARGIN_MODE__|$MARGIN_MODE|g" > "$PLIST"
  # 精确匹配真正的占位符：模板注释里也含有 __ 形式的说明文字，不能一概而论
  placeholders="__ROOT__|__PYTHON__|__SYMBOLS__|__INTERVAL__|__RUNS__|__PROTECTION__|__TRAILING__|__MIN_CONFIDENCE__|__REENTRY_COOLDOWN_MIN__|__MAX_SIGNALS_PER_ROUND__|__STOP_FLOOR_ATR__|__MAX_POSITIONS__|__LEVERAGE__|__MARGIN_MODE__|__TRADE_FLAG__"
  if grep -qE "$placeholders" "$PLIST"; then
    echo "render left placeholders behind:" >&2
    grep -nE "$placeholders" "$PLIST" >&2
    exit 1
  fi
}

is_loaded() {
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1
}

is_running() {
  launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -q "state = running"
}

job_pid() {
  launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/pid = /{print $3; exit}'
}

bootstrap_job() {
  # bootout 后立刻 bootstrap 会偶发 "Bootstrap failed: 5: Input/output error"
  # （launchd 还在卸载旧实例）。退避重试几次即可。
  local attempt
  for attempt in 1 2 3 4; do
    if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
      return 0
    fi
    echo "bootstrap 第 $attempt 次失败，3s 后重试" >&2
    sleep 3
  done
  echo "bootstrap 失败。可试：launchctl enable $DOMAIN/$LABEL; launchctl print-disabled $DOMAIN | grep $LABEL" >&2
  return 1
}

require_installed() {
  if [ ! -f "$PLIST" ]; then
    echo "未安装（缺 $PLIST）：先跑 bash $0 install" >&2
    return 1
  fi
}

warn_legacy() {
  if [ -f "$LEGACY_PLIST" ]; then
    echo "警告：旧位置仍有 plist，登录会被自动装载并立刻运行：$LEGACY_PLIST" >&2
    echo "      跑 bash $0 install 会自动移除它。" >&2
    return 0
  fi
  return 1
}

cmd_install() {
  render
  plutil -lint "$PLIST"
  # 迁移 + 幂等：先卸掉同名任务（无论它从哪个路径装载的），再删旧位置文件。
  # 不这么做的话，旧 plist 留在 LaunchAgents 里，下次登录照样自动跑。
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  if [ -f "$LEGACY_PLIST" ]; then
    rm -f "$LEGACY_PLIST"
    echo "已移除旧位置 plist（登录自启来源）：$LEGACY_PLIST"
  fi
  echo "installed: $PLIST"
  echo "  状态:    未装载、未运行 —— 不随登录自启"
  echo "  启动:    bash $0 start"
  echo "  日志:    $LOG"
  echo "  记录:    ~/.vibe-trading/futures_signal_log.jsonl"
}

cmd_start() {
  require_installed || exit 1
  if is_running; then
    echo "$LABEL 已在运行 (pid $(job_pid))"
    return 0
  fi
  if ! is_loaded; then
    echo "装载 $LABEL..."
    bootstrap_job
  else
    # 正常不会走到这（KeepAlive=true 装载即运行），留个兜底。
    launchctl kickstart "$DOMAIN/$LABEL"
  fi
  # KeepAlive=true：装载即运行，给它一点时间进 running。
  local i
  for i in 1 2 3 4 5; do
    if is_running; then
      echo "$LABEL 已启动 (pid $(job_pid))"
      echo "日志: tail -f $LOG"
      return 0
    fi
    sleep 1
  done
  echo "$LABEL 装载了但没进 running；看 $LOG" >&2
  launchctl print "$DOMAIN/$LABEL" 2>&1 | grep -E "state = |last exit code" >&2 || true
  exit 1
}

cmd_stop() {
  if ! is_loaded; then
    echo "$LABEL 未装载（本来就没在跑）"
    return 0
  fi
  echo "停止 $LABEL..."
  # 必须 bootout：KeepAlive=true 会把单纯 kill 掉的进程立刻拉回来。
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  # bootout 之后 launchd 可能还短暂认得这个 label，直接判死会误报，轮询几秒。
  local i
  for i in 1 2 3 4 5; do
    if ! is_loaded; then
      echo "$LABEL 已停止（plist 保留，登录不会自动起来）"
      return 0
    fi
    sleep 1
  done
  echo "bootout 后任务仍在，请手动检查：launchctl print $DOMAIN/$LABEL" >&2
  exit 1
}

cmd_restart() {
  cmd_stop
  cmd_start
}

cmd_status() {
  if is_running; then
    printf "%-24s running (pid %s)\n" "$LABEL" "$(job_pid)"
  elif is_loaded; then
    printf "%-24s loaded, not running\n" "$LABEL"
  else
    printf "%-24s not loaded\n" "$LABEL"
  fi
  if [ -f "$PLIST" ]; then
    printf "%-24s %s\n" "plist" "$PLIST"
  else
    printf "%-24s %s (缺失；先跑 install)\n" "plist" "$PLIST"
  fi
  if is_loaded; then
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "last exit code" || true
  fi
  warn_legacy || true
}

cmd_logs() {
  touch "$LOG"
  tail -f "$LOG"
}

case "$CMD" in
  install) cmd_install ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$LEGACY_PLIST"
    echo "uninstalled $LABEL"
    ;;
  -h|--help|help) usage ;;
  *)
    echo "unknown command: $CMD" >&2
    echo >&2
    usage >&2
    exit 2
    ;;
esac
