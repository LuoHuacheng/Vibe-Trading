# launchd 常驻：Binance USDⓈ-M 测试网合约信号循环

把 `agent/scripts/futures_signal_loop.py` 装成 macOS 用户级 launchd 服务：**手动启停**（不随登录自启）、崩溃自动重启、日志落盘。

> ⚠️ **只打测试网**（`testnet.binancefuture.com`）。实盘那条路需要 mandate 门禁，本服务不含它。

## 快速开始

~~~~bash
# 1) 只安装：渲染 plist 到 ~/.vibe-trading/launchd/，不装载、不运行
bash agent/scripts/launchd/install.sh install --dry-run   # 先干跑观察（只分析、不下单）
bash agent/scripts/launchd/install.sh install             # 确认信号合理后切真下单（测试网）

# 2) 手动启动 / 停止 —— install 本身永远不启动任何东西
bash agent/scripts/launchd/install.sh start
bash agent/scripts/launchd/install.sh stop

# 3) 看状态 / 日志 / 重启 / 卸载
bash agent/scripts/launchd/install.sh status
bash agent/scripts/launchd/install.sh logs
bash agent/scripts/launchd/install.sh restart
bash agent/scripts/launchd/install.sh uninstall
~~~~

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--symbols` | 十个主流 USDT 永续 | **建议固定真实品种**。测试网的成交额是假的，按 top-N 选品会把 `牛来/USDT:USDT` 这类测试网假符号选进来 |
| `--interval` | 300 | 每轮间隔秒数 |
| `--runs` | 288 | 最大轮数（288 × 5 分钟 ≈ 24h）；跑完正常退出，`KeepAlive` 立刻开新一轮，状态文件续用 |
| `--protection` | both | `fixed` / `trailing` / `both` / `off`。**默认 both**：固定止损、移动止损、止盈三条腿全挂交易所，脚本只判定不持有 |
| `--trailing` | 1.5 | 移动止损回调百分比（Binance 只接受 0.1~5）。**别调回 3**：3% 比实际止损距离宽 3~5 倍，等于永不触发 |
| `--min-confidence` | 0.6 | 开仓最低信号置信度。0 = 关闭。低于阈值的信号记 `skipped` 不下单 |
| `--reentry-cooldown-min` | 15 | 任意平仓后同品种再入场冷却分钟数（止损另有 `--cooldown-hours`）。0 = 关闭 |
| `--max-signals-per-round` | 3 | 每轮最多开仓数。0 = 不限 |
| `--maker-fallback-market` | 关 | maker 超时后是否转市价。**默认关**：只撤单跳过，不为成交去吃 taker 费 |
| `--stop-floor-atr` | 2 | 止损距离下限（× ATR(14,5m)）。LLM 给的止损常只有 0.07%~1.8%，落在噪声里；夹到 2×ATR 后脚本判定价与交易所挂单价一致。0 = 关闭 |
| `--max-positions` | 10 | 同时在手最大仓位数——放大品种池前必须有这道闸 |
| `--leverage` / `--margin-mode` | 5 / isolated | 全局生效 |
| `--dry-run` | 关 | 只分析不下单（等价于去掉 `--trade`） |
| `--python` | 仓库 `.venv/bin/python` | 换解释器时用 |

## 生成的 plist

- 模板：`futures_signal_loop.plist.template`（占位符 `__ROOT__`/`__PYTHON__`/`__SYMBOLS__` 等）
- 渲染后：`~/.vibe-trading/launchd/com.vibe-trading.futures-signal-loop.plist`

**刻意不放 `~/Library/LaunchAgents/`。** launchd 只在登录时自动装载那个目录里的 plist，所以放进去就等于"登录自启"。放 `~/.vibe-trading/launchd/` 则相反：没人显式 `launchctl bootstrap` 它就不存在，重启后也不会自己回来。

关键字段：

- `RunAtLoad` = `false`：装载不等于运行。
- `KeepAlive` = `true`：任何退出都重启（`--runs` 跑完算"一轮结束"，不是"该停"）。
  ⚠️ 实测坑：**`KeepAlive` 一在，launchd 会在任务"一被装载"时立刻启动它，`RunAtLoad=false` 拦不住**（`KeepAlive` 写成 `{SuccessfulExit: true}` 也一样）。
  所以唯一的运行入口是 `start`（bootstrap），`stop` 必须用 `bootout` —— 单纯 `kill` 进程会被 KeepAlive 立刻拉回来。
- `ThrottleInterval 30`（防重启风暴）、`ProgramArguments` 带 `-u`（stdout 不缓冲，日志实时）。

`install` 和 `uninstall` 都会顺手清掉旧位置 `~/Library/LaunchAgents/com.vibe-trading.futures-signal-loop.plist`（老版本装在那里，留着它登录照样跑）；`status` 若发现旧文件还在会告警。

launchd 启动的进程环境极简，所以 plist 里显式给了 `PATH`；工作目录设为仓库根，脚本据此解析 `agent/.env` 与 `~/.vibe-trading`。

## 日志

| 文件 | 内容 |
|---|---|
| `.vibe-dev/logs/futures-loop.launchd.log` | stdout（横幅、异常栈） |
| `.vibe-dev/logs/futures-loop.launchd.err.log` | stderr |
| `~/.vibe-trading/futures_signal_log.jsonl` | **权威操作记录**：信号、下单、保护、清理、每轮状态 |
| `~/.vibe-trading/futures_trade_state.json` | 峰谷值、已挂保护单 id、上轮持仓量 |

排查优先看 jsonl：其中有 `status` 字段（`signal` / `order` / `protect` / `cancel` / `cleanup` / `cooldown` / `idle` / `warn` / `error`）。`cooldown` 表示「仓位是被交易所侧止损打掉的，已给该品种上冷静期」。

## 手动控制（不经过本脚本）

~~~~bash
L=com.vibe-trading.futures-signal-loop
P=~/.vibe-trading/launchd/$L.plist
launchctl print gui/501/$L | head -20
launchctl bootstrap gui/501 "$P"   # 启动（KeepAlive 会让它立刻跑）
launchctl bootout   gui/501/$L     # 停止（kill 无效，会被 KeepAlive 拉回来）
~~~~

## 已知运行时特性

- **测试网很不稳**：`exchangeInfo` 超时、`openAlgoOrders` 读空/读部分、LLM 503 都会出现。循环会跳过该轮、下一轮补；保护单是否齐全**以本地记录为准**，不依赖那个会读丢的列表接口。
- **保护单在 Binance 的 Algo 服务里**，标准挂单接口（含 CLI/网页）看不到；查它用 SDK 的 `get_open_algo_orders` 或看 jsonl。
- **残留清理把 trailing 一起算上**：Binance 拒绝在 symbol 还挂着单时改保证金模式（`-4067`），孤儿条件单会让那个品种再也开不出新仓。清理名单取「交易所条件单列表 ∪ 本地记录」再减去当前持仓；三种条件单都要收，漏掉 `trailing_stop_market` 就会攒下孤儿单。
- **仓位还在、但腿对不上时也要按交易所列表清**：仓位已消失的品种走上面的并集清理；仓位**还在**但本地记录与仓位不符（数量对不上、止损价被夹过、保护模式换过）时，同样把该 symbol 交易所侧读到的腿一起撤掉再重挂 —— 否则该品种上多挂出来的孤儿腿永远清不掉。
- 持仓真相来自券商，重启后会自动接管并补齐缺失的保护腿。
- **止损只有一个价**：`--stop-floor-atr` 把 LLM 给的价位撑到至少 2×ATR(14,5m)，并把同一个值同时写进 state 与交易所挂单 —— 脚本判定价 == 交易所挂单价。不夹的话会出现「脚本按 0.5% 判、交易所挂 3%」，实际生效的永远是最紧那条，而且还带 5 分钟盲窗。
- 止损止盈是**已挂在交易所**的 reduce_only 条件单，进程停了也有效。
