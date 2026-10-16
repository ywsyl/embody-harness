#!/usr/bin/env bash
# role-watch.sh —— 实时打印常驻角色的会话日志：事件到达、思考（reason）、发言、工具调用（含结果）、
# PTC 子调用与回执。开发时用来看「角色到底干了什么」——常驻形态的 CLI 自己不打印每轮内容，
# 唯一的逐事件记录就是 <DSH_HOME>/sessions/<cwd>/<本角色会话 id>/session.v3.jsonl.zstd
# （会话 id 见下面的 role_session_id：事件型角色是 rolebus-<role>，监督器是它叠加层里写的那一个；
# 监督器运行期轮转出来的会话是 <基础 id>.<时间戳>，按前缀匹配取最新一份）。
#
# 为什么用「会话日志」而不是 daemon-<role>.log：后者是进程级 stdout（banner、dsh 启动
# 报错），一轮里的工具调用与模型输出都不在上面。会话日志是引擎写的 append-only JSONL
# （zstd 压缩），每个事件一行，事件类型与字段见 packages/core/session 的 SessionEventMap。
#
# 实现：轮询解码整个文件，只打印新增行（bash 3.2 无关联数组，用 `${!name}` 间接展开记
# 每个角色的已打印行数）。zstd 在文件被并发追加时可能因尾部半帧报错退出，这里只取
# 完整行、忽略它的 stderr 与非零退出。
#
# 用法：
#   scripts/role-watch.sh                          # 看全部已登记角色（interactor + orchestrator + planner + supervisor）
#   scripts/role-watch.sh --role orchestrator      # 只看编排器的 PTC 输出
#   scripts/role-watch.sh --role interactor --dsh-home /tmp/lv6
#   scripts/role-watch.sh --from-start             # 连已记录的回合一起重放（缺省只打印新事件）
#   scripts/role-watch.sh --interval 5             # 每 5 秒读一次会话日志（缺省 1 秒）
#   scripts/role-watch.sh --log /tmp/watch.log     # 终端上看到的每一行同时追加进这个文件
#   scripts/role-watch.sh --full                   # 不做记录取舍（[发言]、回合分隔线、编排器的 [工具] 行都原样打）
#
# 可覆盖环境变量：DSH_HOME（与 `--dsh-home` 等价，后者优先）/ NODE_BIN_DIR / INTERVAL
# （= --interval）/ WATCH_LOG（= --log）/ WATCH_FULL（= --full）。同一 DSH_HOME 必须与
# scripts/role-daemon.sh 起的那个一致，否则看的是另一份会话日志。
#
# 记录取舍（FILTER 里的 SILENT_ALL 与 SILENT_BY_KIND），只有三条：
#   · 所有角色都不打 [发言]（助手正文）：事件型角色每轮那一段是动手前的自述，真正发生的事在
#     [工具] / [PTC] / [结果] 里；交互器的发言还与它的 [工具] speak 参数逐字重复。
#   · 监督器与交互器不打回合分隔线（`──── 回合 N 结束（completed）────`）：这两路一轮一件事，
#     线只是把每件事隔开。编排器的照打。
#   · 编排器不打 [工具] 行（`[orchestrator] [工具] run_code {…}`）：它的动作全在紧接着的
#     [PTC] 里，宿主那一条只是调用壳。名字仍记进 CALLS，所以 `↳ [结果] run_code:` 照打。
#   · 其余一概保留：[思考]、[PTC]、[结果] 每个角色都打，[工具] 除编排器外都打。监督器每 5 秒
#     一轮、它那些行确实多，但要不要压是取舍之外的事——这里只按上面三条办。
#
# 结果怎么打（不参与上面那两条取舍）：工具执行的结果一律进记录——PTC 子调用是行内的
# `[PTC] 名 参数 -> 回执`，宿主工具调用是调用行下面一条 `  ↳ [结果] 名: …`。后一种的两条事件
# （tool/call 与 tool/result）在会话日志里只靠 callId 关联，结果行因此把工具名也带上——两条
# 之间通常夹着 run_code 的一串 [PTC]，光看缩进认不出是哪一次调用返回的（见 FILTER 的 CALLS）。
# 这两条事件的间隔可以很长（编排器一次 run_code 里的导航、视觉是几十秒到几分钟），多半会跨过
# 读文件的节拍——而每轮是新起一个 node 进程，所以名字与「这一段的 [PTC] 失败」都从整份会话日志
# 里补出来，不看流过这一轮的恰好是几行（与路由同一处理，见 FILTER 的 WATCH_DECODED）。
# 结果文本**不截断**，参数也不截断（`[PTC] 名 参数` 与 `[工具] 名 参数` 都原样打全）：日志的用途
# 是复查这一次调用到底传了什么、返回了什么，截掉的尾巴得回去解会话日志。**例外是自定义工具清单
# 之外的工具**——清单是 plugin_tools/available_tools.yaml，名单由 shell 侧读好过桥（见本脚本的
# tool_catalog，判据只有这一处，不在这手抄名字）。清单外的是引擎原生的 run_code / bash / read /
# grep / glob / 网页 / 状态查询与 rolebus 按事件目录生成的 rolebus_send_*：它们的返回要么是成片
# 的原文（文件内容、网页、命令回显），要么是内部记账，这一行要看的是这次调用成没成，所以**成功
# 只渲染成「✅ 成功」**——压掉的是那次返回，不是报错。清单里的自定义工具结果原样进记录，只有两个
# 例外（robot_state 与 task_list 的返回也是成片的原文——整份状态快照、整份待做清单——照旧折掉）。
# 落盘回执不是「✅ 成功」而是更准的一句：rolebus_send_* 里标了 writesTaskBoard 的两条 task_update
# 写入成功报「✅ 成功写入」、改动没改变内容报「⏸ 无变化」（同一条判据的两种形态，见 FILTER 的
# boardReceipt）。
# **失败照常把报错原文打出来**（`❌ 失败：…`），清单里外一样，工具的报错在任何工具上都不省。
# 成不成一般看 ToolResultBlock 的 isError；**bash 例外**——非零退出是一次正常返回，它把
# `[exit code: N]` 标在输出里（系统提示要求模型逐个核对的就是这个标记），所以 bash 的成败看那个
# 标记（见 FILTER 的 bashFailed）。这只改渲染——模型拿到的与会话日志里存的仍是原文，要复查那些
# 返回值就解会话日志。
# 一次报错只打一条**详细**的，且打在出错点上。编排器的动作全在 PTC 子调用里：程序里那一次
# `tools.x()` 失败时，`[PTC] 名 参数 [失败] -> 工具自己抛的错` 就是最靠前的出错点——工具名、
# 入参、原始报错都在那一行。run_code 的结果行因此有两种复读：**失败回执**把它再包一层引擎壳
# （`Error: code run failed (<kind>): ToolCallError: …`）外加一段引擎栈帧（`    at …bootstrap.ts`）；
# **程序自己的汇总**——它接住子调用的错、把失败当返回值交回来（编排器「收集每个目标的 ok/err」
# 就是这种写法）时，返回的汇总里那条 `err` 就是上面 [PTC] 行上同一个错的原文。两种都不复读：
# 那次结果行只渲染成「❌ 失败」，细节看它上面那条 [PTC]。程序自己抛的错（没有对应的 [PTC] 行，
# 别处都没有）仍要在结果行里给出，但只给剥掉壳与栈帧的那一句。判重看文案：出错点已经出现在这次
# run_code 的某条 [PTC] 失败行里就不复读（**含就行**：汇总里多写的 target / 次数在那些 [PTC]
# 行的入参里已经带了）；程序又抛了另一个错（不在那些行里）照样打。别的宿主工具失败时只有结果行
# 这一处，同样只报一条，前面加上「❌ 失败：」标出这是错的（长什么样仍是它自己的报错原文）。
# 与上面那些取舍一样只改渲染，全文仍在会话日志里。
# 不参与取舍的一类：模型报错与超时。[报错]（模型请求失败、回合因错误结束）与 [中断]（回合被
# 取消或被重启补记）每个角色都打，有没有 --full 都一样——上面那几条恰好让一次失败的痕迹最容易被
# 抹掉（正常回合分隔线不打，报错若也不打，记录里就只剩事件到达那行，看着与角色没动过一样）。
# --full 恢复的是上面那三条取舍（[发言]、回合分隔线、编排器的 [工具] 行）；**「结果怎么打」不受它
# 管**——折成「✅ 成功」的那些在 --full 下照折，要看全文就解下面那份会话日志。被取舍掉的内容仍然
# 完整地在会话日志里（这里只是不渲染，没有丢弃），要复核就 --full 重放或直接解那一份会话日志。
#
# 事件行（[角色] 【角色事件】…）按简短格式打：只留事件头与载荷，插件在 renderEnvelope 里拼进去的
# 目录 description 与收尾指令不打（见 FILTER 的 trimEvent）；唤醒路径的 [事件·next-step] 标记也
# 不打。角色当轮读到的仍是插件的原文，会话日志里同样一字未改——这只是渲染，不是格式定义。
#
# 发件工具带收件角色：`[工具] rolebus_send_observe_event → interactor {…}`（PTC 里是
# [PTC] 行同样处理）。路由表不是抄的事件目录，是从插件写给模型的工具描述里读出来的（见 FILTER
# 的 learnRoutes），所以目录改了这里跟着变、描述改了最多是不注明。路由的来源是**整份会话日志**
# 而不只是接下来流过的行：缺省跳过已记录的行，而请求头与系统提示往往就在跳过的那一段里，所以
# 启动时先把已记下的部分扫一遍（见 FILTER 的 WATCH_DECODED），半路接手/轮换后照样带注记。
#
# 记录行的角色前缀：interactor 是 `**[interactor]`（角色括号前一个 `**`，其余角色只到
# `[supervisor]`，见 FILTER 的 MARKS）。只标记录行：watcher 自己的提示行（「跳过已记录的 N 行」
# 那类）不带标记。
#
# 落盘（--log）：本脚本是逐轮记录的唯一渲染口径，所以「记到文件里」就是把它 stdout/stderr
# 上的每一行同时追加进 FILE——终端看到什么，文件里就有什么，不需要第二套格式。每次启动先写
# 一行启动头（时间、角色、DSH_HOME、是否重放），所以同一个文件被多次启动追加时按启动头分段；
# 多个 watcher 同时写同一个文件（starter.sh 的总 watcher 与 role-daemon.sh 的单
# 角色 watcher）也靠它分辨谁是谁。speak 的文本在终端上只有这一处：工具层把它交给机器人上的 TTS
# 脚本（plugin_tools/audio_tools.py），自己不额外打行；同一份文本在会话日志里是这次调用的 arguments。
#
# --interval 管的是**本脚本多久读一次会话日志文件**，与角色自己多久被唤醒一次无关：监督器的
# 观察间隔是 configs/configs.py 的 SUPERVISOR_INTERVAL（经 role-daemon.sh 过桥给叠加层），
# 那个值打在它每一轮「跳过」/观测抬头上，也打在 role-daemon.sh 的 banner 里。本脚本的缺省值
# 比它小，是为了让一轮里的每条事件尽快出现——跟上角色的节拍反而会看到整轮一次涌出来。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib-roles.sh
source "$REPO_ROOT/scripts/lib-roles.sh"

# shellcheck source=../configs/scripts-env.sh
source "$REPO_ROOT/configs/scripts-env.sh"   # NODE_BIN_DIR（zstd 解码走 PATH 上的 node）
embody_check_node role-watch || exit 1
embody_apply_env

usage() {
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

ROLES=""
# 环境里的 DSH_HOME 与 `--dsh-home` 等价（同 role-daemon.sh / rolebus-setup.sh）；都没有才按
# 角色取仓库内隔离目录。少了这一层，`DSH_HOME=/tmp/x role-watch.sh` 会去读缺省目录、安静地
# 看着另一份会话日志。
DSH_HOME_ARG="${DSH_HOME:-}"
INTERVAL="${INTERVAL:-1}"
FROM_START="${FROM_START:-0}"
LOG="${WATCH_LOG:-}"
FULL="${WATCH_FULL:-0}"
while [ $# -gt 0 ]; do
  case "$1" in
    --role)       ROLES="${ROLES:+$ROLES }${2:-}"; shift 2 ;;
    --dsh-home)   DSH_HOME_ARG="${2:-}"; shift 2 ;;
    --interval)   INTERVAL="${2:-}"; shift 2 ;;
    --log)        LOG="${2:-}"; shift 2 ;;
    --from-start) FROM_START=1; shift ;;
    --full)       FULL=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "role-watch: 未知参数 $1（见 --help）" >&2; exit 2 ;;
  esac
done
[ -n "$ROLES" ] || ROLES="$(role_all)"
# 渲染取舍在 FILTER 里按角色决定，开关经环境过桥给它（管道里逐行起的是同一个规格）。
export WATCH_FULL="$FULL"
# 自定义工具清单（plugin_tools/available_tools.yaml）的工具名，同样过桥给 FILTER：它靠这份名单
# 分开「结果原样进记录」与「只报成不成」两批（见文件头「结果怎么打」）。清单的解析只有一处
# （plugin_tools/tool_parser.py 的 _load_tool_entries，路径出 configs/paths.py 的
# AVAILABLE_TOOLS_FILE），这里只把名字捞出来，不自己读 yaml。
# 放在参数之后（与 starter.sh 的终端日志同一位置）：--help 与参数错误不该为此起一个 python。
# **读不到就退出，不退回「什么都不在清单里」**：那会让每个工具的结果都被压成成不成——正是这条
# 规则的反面，且看着与正常运行一模一样。
tool_catalog() {
  (cd "$REPO_ROOT" && python3 -c "
from plugin_tools.tool_parser import load_tool_names
names = load_tool_names()
assert names, 'available_tools.yaml 里一条工具都没有'
print(' '.join(names))
") || {
    echo "role-watch: 取不到 plugin_tools/available_tools.yaml 的工具名（哪些工具的结果进记录只有这一份判据，没有第二个来源）" >&2
    return 1
  }
}
WATCH_TOOL_CATALOG="$(tool_catalog)" || exit 1
export WATCH_TOOL_CATALOG

# say / warn —— 终端那一行，加上（设了 --log 时）同一行进文件。记录只有这一个口径，所以两条
# 流各配一个，不合并：合并会把 stderr 上的提示混进 stdout（`role-watch | grep` 这类用法会多出
# 本来不该有的行）。结尾显式 return 0：设了 set -e，`[ -n "" ] && …` 这种写法在没设 --log 时
# 会让整个脚本以非零退出。
say() {
  printf '%s\n' "$*"
  [ -z "$LOG" ] || printf '%s\n' "$*" >> "$LOG"
  return 0
}
warn() {
  printf '%s\n' "$*" >&2
  [ -z "$LOG" ] || printf '%s\n' "$*" >> "$LOG"
  return 0
}

if [ -n "$LOG" ]; then
  mkdir -p "$(dirname "$LOG")"
  # 启动头：同一个文件被多轮/多个 watcher 追加，靠它分段。
  {
    printf '\n==== %s role-watch 启动：角色 [%s]%s%s ====\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$ROLES" \
      "${DSH_HOME_ARG:+ DSH_HOME=$DSH_HOME_ARG}" \
      "$([ "$FROM_START" = 1 ] && printf ' （--from-start 重放已记录回合）' || true)"
  } >> "$LOG"
fi

declare_role_home() {
  if [ -n "$DSH_HOME_ARG" ]; then DSH_HOME="$DSH_HOME_ARG"; else DSH_HOME="$(role_default_dsh_home "$1")"; fi
}

# 把一行事件渲染成可读文本。字段名取自引擎的 SessionEventMap；这里对可选字段一律走可选
# 链，事件形状变了只会少打印一行，不会让整个 watcher 崩掉。
FILTER="$(cat <<'JS'
const readline = require('node:readline')
const role = process.env.WATCH_ROLE ?? '?'
// 记录取舍（详细理由见文件头「记录取舍」）：所有角色都不打助手正文；回合分隔线对监督器与交互器
// 不打——这两路一轮一件事，线只是把每件事隔开；编排器的 [工具] 行也不打——它的动作全在 [PTC]
// 里，宿主那一条只是 `run_code {…}` 的调用壳。其余一律留：[思考]、[PTC]、[结果] 每个角色都打，
// [工具] 除编排器外都打。监督器每 5 秒一轮、行数确实多，但那是实况，取舍只按上头那三条来，
// 不替谁另做主。
// 报错与中断行不归这三条管，任何角色都打（见下面 llm/retry 与 turn/end 两处）。
// --full（或 WATCH_FULL=1）恢复全量。
const SILENT_ALL = ['text']
const SILENT_BY_KIND = { 'turn-end': ['supervisor', 'interactor'], 'tool-call': ['orchestrator'] }
const dropped = kind => process.env.WATCH_FULL !== '1'
  && (SILENT_ALL.includes(kind) || (SILENT_BY_KIND[kind] ?? []).includes(role))
// 事件行只留「事件头 + 载荷」：renderEnvelope 拼进去的目录 description（紧跟事件头的第二行）与
// 收尾指令（最后一行）每封都印一遍，属纯噪音。这只是不打印——角色那一轮读到的仍是插件的原文，
// 会话日志里也是原文。按位置取而不是按文案匹配：文案的唯一事实源在 dsh-plugins/rolebus/src/
// resident.ts，这里不复制一份；结构变了最多多印或少印一行。
const trimEvent = text => {
  const lines = String(text).split('\n').map(line => line.trim()).filter(Boolean)
  return (lines.length < 3 ? lines : [lines[0], ...lines.slice(2, -1)]).join(' ')
}
// rolebus 发件工具 → 收件角色。事件目录（dsh-plugins/rolebus/src/event-catalog.ts）是「谁发给
// 谁」的唯一事实源，本文件不抄它——只读插件自己写给模型的那句话：
// `Sends "<event_type>" from role "<source>" to role "<target>".`。同一句在两个面上都出现：
// 宿主工具在 request/header 的工具描述里，PTC 的 SDK 声明在系统提示里（见 createEventTool）。
// 工具名由事件类型按同处那条约定拼出（`rolebus_send_<event_type>`）。这句话将来改了只是不注明，
// 不影响别的行。
// 引号写成 \x22：`FILTER="$(cat <<'JS' … JS)"` 这一层里，bash 3.2 找 $() 的收尾时会连带按
// 引号与括号的配对读过去；正则里出现引号字符（尤其是把引号写进字符类里取反那种）会让它提前
// 闭合引号状态，把一个裸的右括号露在 0 层深度上（注释里写一个也一样），$() 就在那一行提前
// 收尾，后面的整段 JS 被当成 shell 展开，报的还是「unbound variable / bad substitution」这类
// 看不出根因的错。本段因此不出现任何一个 ASCII 引号或裸右括号。
const SEND_ROUTE = /Sends \x22([a-z_]+)\x22 from role \x22[a-z_]+\x22 to role \x22([a-z_]+)\x22/g
const ROUTES = Object.create(null)
const learnRoutes = text => {
  for (const [, event, target] of String(text ?? '').matchAll(SEND_ROUTE)) {
    ROUTES[`rolebus_send_${event}`] = target
  }
}
const routeOf = name => (typeof name === 'string' ? ROUTES[name] : undefined)
// 这两类事件是那句话唯一的载体（宿主工具表 / PTC 的 SDK 声明），两处都只为学路由、本身不打印。
const learnFromEvent = event => {
  if (event?.type === 'request/header') {
    for (const tool of event.data?.header?.tools ?? []) learnRoutes(tool?.description)
  } else if (event?.type === 'system/message') {
    learnRoutes(textOf(event.data?.message?.content))
  }
}
const one = value => String(value ?? '').replace(/\s+/g, ' ').trim()
const clip = (value, max) => { const text = one(value); return text.length > max ? `${text.slice(0, max)}…` : text }
const textOf = content => (Array.isArray(content) ? content : [])
  .map(part => (typeof part?.text === 'string' ? part.text : ''))
  .filter(Boolean)
  .join(' ')
// 记录行的角色前缀：interactor 在角色括号前多一个 **（它是唯一与人对话的那一路，在记录里一眼
// 挑得出来），其余角色只有角色名。标记在前不在后——`**[interactor] …` 整行按 Markdown 读时开
// 头就是加粗的角色名。只管渲染的记录行；watcher 自己的提示行（「跳过已记录的 N 行」那类，见下
// 面的 warn 调用）不带标记。
const MARKS = { interactor: '**' }
const out = text => console.log(`${MARKS[role] ?? ''}[${role}] ${text}`)
// 发件工具带上收件角色：`[工具] rolebus_send_x → interactor {…}`。查不到路由的（手工 emit 的
// 临时事件、将来新增的别的工具）不加，不猜。
const withRoute = name => `${name}${routeOf(name) ? ` → ${routeOf(name)}` : ''}`
// 结果只在记录里报成不成的工具，判据只有一条：**它不在自定义工具清单里**——清单是
// plugin_tools/available_tools.yaml，由 shell 侧读好经环境过桥（见 role-watch.sh 的 tool_catalog）。
// 清单外的有引擎原生的 run_code / bash / read / grep / glob / web / stat… 与 rolebus 按事件目录
// 生成的 rolebus_send_*：它们的返回要么是成片的原文（文件内容、网页、命令回显），要么是内部记账，
// 这一行要看的是这次调用成没成，不是它返回了什么。
// 只报成不成的是**返回**，不是报错：失败照常走下面的渲染，报错原文一个字不少（宿主工具见
// resultLine，PTC 见 tool/ptc-dispatch）。也只管渲染——模型那一侧照旧拿原文（task_list 是交互器
// 查清单原文与 step 编号的那条路），会话日志里一字未改，所以**读了记录文件的人仍能看到全文**。
// 按清单判而不是按长度或手抄一份名字：加一条自定义工具自动就保留全文结果，不用回来改这个脚本；
// 同名工具也不会一次收一次不收（看着像渲染坏了）。成不成看 ToolResultBlock 的 isError，不猜返回
// 文案——**bash 是那个例外**（见下）。
// 空名单不能当「清单是空的」用：那会让**每个**工具的结果都被压成成不成——正是这条规则的反面，而
// 记录看着与正常运行一模一样。shell 侧取不到就退出（见 tool_catalog），这里守住手工调用的那一路。
const CATALOG_TOOLS = new Set(String(process.env.WATCH_TOOL_CATALOG ?? '').split(' ').filter(Boolean))
if (CATALOG_TOOLS.size === 0) {
  throw new Error('WATCH_TOOL_CATALOG 为空：工具清单没读到（哪些工具的结果进记录只有这一份判据）')
}
// 例外：这两个虽在清单里，返回也是成片的原文（robot_state 是整份状态快照、task_list 是整份待做
// 清单），与清单外那些一样只报成不成（用户 2026-10-13 定）。清单里其余那些的结果原样进记录。
const OK_ONLY_ALSO = new Set(['robot_state', 'task_list'])
const okOnly = name => !CATALOG_TOOLS.has(name) || OK_ONLY_ALSO.has(name)
// 落盘回执给出比「✅ 成功」更准的一句：rolebus 发件工具里标了 writesTaskBoard 的那两条（task_update_orc
// 与 task_update_int）由工具侧拼一份回执（dsh-plugins/rolebus 的 render），第一行说投没投出去、
// 第二行是落盘确认。「投给谁」已经由工具名后面的 `→ 收件方` 带了，task_id / status / owner 又在
// 调用参数里——整段复读没有信息量。确认句本身只有两种（plugin_tools/task_manager.py 的 _summary）：
// 这次真改了清单是 `✅ 已写入 <任务号>：…`，改动没改变内容是 `⏸ 无变化 <任务号>：…`——**后者不能
// 也报成功**：那一次清单一个字没动、也没叫醒收件方，与真落了盘是两件事，报成同一句就看不出来了。
// 按工具名收而不是只看文案：编排器 run_code 的汇总里会把 task_summary 原样带回来（那是程序自己
// 的返回值），只按文案收会把那条 run_code 的结果也报成「✅ 成功写入」——它按清单外那一档只该是
// 「✅ 成功」。
const SEND_TOOL = /^rolebus_send_/
const boardReceipt = (name, receipt) => {
  if (!SEND_TOOL.test(String(name ?? ''))) return undefined
  const text = one(receipt)
  if (text.includes('✅ 已写入')) return '✅ 成功写入'
  if (text.includes('⏸ 无变化')) return '⏸ 无变化'
  return undefined
}
// 成功那一支要不要压成一句短话：三条判据在这里合成一处，宿主结果行与 PTC 回执都只问它，两处
// 不会各说各话。先说落盘回执（那两句比「✅ 成功」准），再按 okOnly 收成「✅ 成功」。失败那一支
// 不进这里（报错原文一个字不省，见各处的说明）。
const shortSuccess = (name, failed, receipt) => {
  if (failed) return undefined
  return boardReceipt(name, receipt) ?? (okOnly(name) ? '✅ 成功' : undefined)
}
// bash 的成败不在 isError 上：非零退出是一次正常返回，工具把 `[exit code: N]` 标在输出里，系统
// 提示要求模型逐个核对的就是这个标记，这里照同一处判据收。标记没有就当跑成了（工具契约：
// 非零退出才标）。这不是猜文案，是照它自己声明的格式读。
const EXIT_CODE = /\[exit code: (\d+)\]/
const bashFailed = (name, text) => {
  const code = name === 'bash' ? EXIT_CODE.exec(String(text ?? ''))?.[1] : undefined
  return code !== undefined && code !== '0'
}
// run_code 失败时回执是引擎自己包的一层壳（见文件头「结果怎么打」）：首行是
// `Error: code run failed (<kind>): ToolCallError: <出错点>`，后面跟着引擎自己的栈帧。壳与栈帧都
// 不是出错点，剥掉只剩那一句。别的工具的报错不长这样，原样返回（照样压成一行）。
const RUN_CODE_SHELL = /^(?:Error: )?code run failed \([a-z-]+\): (?:ToolCallError: )?/
const originOf = text => {
  const head = one(String(text ?? '').split('\n')[0])
  return RUN_CODE_SHELL.test(head) ? head.replace(RUN_CODE_SHELL, '') : one(text)
}
// 失败回执里那句错本身。工具/引擎抛出的原文在 [PTC] 行上是 `Error: <工具自己的报错>`；程序把
// 子调用的错当返回值交回来时，汇总里带的是 `<工具自己的报错>`——同一个错、少了那个前缀。认
// 「结果行的正文只是在复读那条 [PTC]」要拿这一句来比。
const saidIn = receipt => one(receipt).replace(/^Error: /, '')
// 这一段执行的失败是不是已经打在那条 [PTC] 行上了。判据是文案，两个方向各管一路：结果是引擎壳
// （见 originOf）时，结果行自己要给的那一句被某条失败回执包含；结果是程序自己的汇总时，汇总正文
// 里含某条失败回执的那一句。**含就行，不要求正文只在说它**：汇总里多写的那点（试了哪个 target、
// 第几次）在上面那条 [PTC] 行的入参里已经带了，复读没有信息量。全文仍在会话日志里。
const alreadyReported = (full, reported) => {
  const text = one(full)
  const origin = originOf(full)
  return reported.some(receipt => {
    if (origin !== '' && receipt.includes(origin)) return true
    const said = saidIn(receipt)
    return said !== '' && text.includes(said)
  })
}
// 结果行的正文（不报成不成的那些工具）：这一次执行的失败已经打在上面那条 [PTC] 行上时只报
// 「❌ 失败」（细节在那一行上，两种复读见 alreadyReported）；否则成功的原样打返回（含程序自己
// return 的汇总），失败的给出剥掉引擎壳与栈帧的那一句（程序自己抛的错只有这里能看）。
const resultLine = (failed, full, reported) => {
  if (alreadyReported(full, reported)) return '❌ 失败'
  if (!failed) return one(full)
  const origin = originOf(full)
  return origin === '' ? '❌ 失败' : `❌ 失败：${origin}`
}
// 宿主工具调用（tool/call）→ 它的结果（tool/result）：会话日志里两条事件只靠 callId 关联。
// 名字用来把结果行挂回它的调用行（两条之间通常夹着 run_code 的一串 [PTC]，光看缩进认不出是哪
// 一次调用返回的）；failures 记这一段执行里打过的 [PTC] 失败回执，run_code 的结果行靠它判重
// （见文件头「结果怎么打」）。**不能只靠流过这一轮的几行**：每轮是新起的 node 进程，而调用与
// 返回之间可能隔着几秒到几分钟（编排器的 run_code 就是，导航与视觉都在程序里），落进两次轮询
// ——那一轮的 map 里没有这次调用，名字与判重就一起丢了。症状是同一次调用一次报成不了、一次又
// 把返回原文打出来。所以启动时先拿整份已记录的会话把两者补齐，与路由同样处理（见下面
// WATCH_DECODED 那段）；这段的进展也照这份记，判重看的是**这条调用**的失败，不是上一次的。
const CALLS = new Map()
let openCall = null   // 当前这一次调用：夹在它和它的结果之间的 [PTC] 都算它的
const traceCall = event => {
  if (event?.type === 'tool/call') {
    openCall = event.data?.callId
    CALLS.set(openCall, { name: event.data?.name, failures: [] })
  } else if (event?.type === 'tool/ptc-dispatch') {
    if (openCall !== null && event.data?.isError === true) {
      CALLS.get(openCall)?.failures.push(textOf(event.data?.content))
    }
  } else if (event?.type === 'tool/result') {
    openCall = null
  }
}
// 路由还要从**已经记下来的**那部分会话里补：watcher 缺省从头跳过已记录的行（只打印接下来发生的
// 事），而请求头与系统提示往往早在跳过的那一段里——下一轮未必再写一条，于是光靠流过去的行会一直
// 学不到路由、一条注记也打不出来。所以先把已经解出来的那份会话日志扫一遍（只挑这两类行，不打印；
// 路径由 shell 经 WATCH_DECODED 传来）。放在这里是因为要用到上面全部声明。
// 这一趟只挑两类行：事件行（学路由）与工具事件行（补 CALLS）。按事件名挑而不是按整段的
// `"type":"…"` 挑：省去对引擎那份 JSON 怎么排空白的依赖，万一以后改紧凑/宽松也不会静默失效。
const TRACE_LINE = /request\/header|system\/message|tool\/call|tool\/result|tool\/ptc-dispatch/
if (process.env.WATCH_DECODED) {
  let recorded = ''
  try {
    recorded = require('node:fs').readFileSync(process.env.WATCH_DECODED, 'utf8')
  } catch {
    recorded = ''   // 文件读不到就当作没记过：最多少几条注记，不改别的输出
  }
  for (const line of recorded.split('\n')) {
    if (!TRACE_LINE.test(line)) continue
    try {
      const event = JSON.parse(line)
      learnFromEvent(event)
      traceCall(event)
    } catch {
      continue   // zstd 尾部半帧留下的残行：跳过，不打断渲染
    }
  }
}
readline.createInterface({ input: process.stdin }).on('line', line => {
  if (line.trim() === '') return
  let event
  try { event = JSON.parse(line) } catch { return }
  const data = event.data ?? {}
  switch (event.type) {
    case 'request/header':
    case 'system/message':
      learnFromEvent(event)
      break
    // 一次到达会产生不止一条 spliced（唤醒路径会再拼接一次），只有带内容的那些才是事件本身。
    // 监督器的定时观测也走这条路（source.plugin = 'supervisor-loop'，与插件的 PLUGIN_SOURCE
    // 同名）：那是本角色自己的唤醒源在报时，不是「有东西到了」——一轮一条会把逐轮的思考与工具
    // 刷掉，所以不打。观测原文在模型每轮的 user message 里，要看整段去解会话日志。
    case 'agent/inbox/spliced': {
      const arrivals = (data.inserted ?? []).filter(message => message?.source?.plugin !== 'supervisor-loop')
      const texts = arrivals.map(message => textOf(message?.content)).filter(Boolean).map(trimEvent)
      // 事件行就是事件头 + 载荷（见 trimEvent）。原先前缀的 [事件·target] 是唤醒路径的判据
      // （next-step = steer/inject 交给最近的步骤边界、next-turn = followup 独占下一轮，见
      // event-catalog.priority 经 PRIORITY_WAKE_MODES 推出的唤醒方式），按简短格式不再打；
      // 要分路径就解会话日志里同一条 spliced。
      if (texts.length > 0) out(clip(texts.join(' / '), 300))
      break
    }
    case 'assistant/message':
      for (const part of data.message?.content ?? []) {
        if (part.type === 'reasoning') {
          if (!dropped('reasoning')) out(`[思考] ${clip(part.text, 600)}`)
        } else if (part.type === 'text') {
          if (!dropped('text')) out(`[发言] ${clip(part.text, 600)}`)
        }
      }
      break
    // 宿主工具调用：名字按 callId 记下，下面的 tool/result 靠它把结果挂回这一次调用（见 CALLS）。
    // 编排器这一行不打（见 SILENT_BY_KIND）：它的调用是 `run_code {…}` 那个壳，动作全在紧接着的
    // [PTC] 里；traceCall 照记，所以下面的结果行仍认得名字。
    case 'tool/call':
      traceCall(event)
      if (!dropped('tool-call')) out(`[工具] ${withRoute(data.name)} ${one(data.arguments)}`)
      break
    // PTC：模型写的程序里每一次 `await tools.x(...)` 都是一条 dispatch —— 编排器的
    // 实际动作全在这里，run_code 自身的调用见上面的 tool/call。参数与回执都原样打，不截断
    // （见下面「结果」）。
    case 'tool/ptc-dispatch': {
      const args = one(JSON.stringify(data.arguments))
      const receipt = textOf(data.content)
      const failed = data.isError === true || bashFailed(data.name, receipt)
      // 失败的回执原样打——这一行就是本次报错的出错点（见文件头「结果怎么打」）；traceCall
      // 把它记在这次调用的名下，好让 run_code 的结果行认出「这条错已经打过了」。
      traceCall(event)
      const only = shortSuccess(data.name, failed, receipt)
      // 成功时压成一句短话的那批（判据见 shortSuccess 与 boardReceipt）：整行只留「-> ✅ 成功」
      // 或「-> ✅ 成功写入」。失败不进这一支——照常带 `[失败]` 与回执原文，要的就是那条报错。
      if (only === undefined) {
        out(`[PTC] ${withRoute(data.name)} ${args}${failed ? ' [失败]' : ''}`
          + `${receipt === '' ? '' : ' -> ' + one(receipt)}`)
      } else {
        out(`[PTC] ${withRoute(data.name)} ${args} -> ${only}`)
      }
      break
    }
    // 结果行跟着调用行走：`↳` 标出这是某次调用的返回，后面带上工具名——中间可能已经插进好几条
    // [PTC]（run_code 的那些子调用），光靠缩进认不出是哪一次。认不出 callId 的（异常路径）退回
    // 不带名字。编排器的调用行不打（见 SILENT_BY_KIND），它这里常常是孤零零一条——名字仍认得。
    // **结果不截断**（参数也不截，见 tool/call 与 tool/ptc-dispatch）：日志的用途是复查这一次调用
    // 到底传了什么、返回了什么；例外是清单外那些与落盘回执，它们只报结果那一句
    // （见各自的说明）。
    case 'tool/result': {
      const callId = data.message?.source?.callId
      const call = CALLS.get(callId)
      CALLS.delete(callId)
      traceCall(event)   // 这一段执行到此为止：后面来的 [PTC] 不算它的
      const name = call?.name
      const block = data.message?.content?.[0]
      const full = textOf(block?.content)
      const failed = block?.isError === true || bashFailed(name, full)
      const reported = call?.failures ?? []
      if (!dropped('result')) {
        const only = shortSuccess(name, failed, full)
        const text = only === undefined ? resultLine(failed, full, reported) : only
        out(`  ↳ [结果]${name === undefined ? '' : ' ' + name + ':'} ${text}`)
      }
      break
    }
    // 模型请求失败与超时。`llm/retry` 是适配器/SDK 抛出的那一次失败（pi-ai 把超时归成 code
    // TIMEOUT，见 llm-pi-ai 的 stream.ts），重试策略在这一行之后等待；重试耗尽或不可重试时由
    // turn/end 的 error 收尾（见下面）。这类行任何角色都打、也不受 --full 影响：它不是取舍的对
    // 象，是「日志里为什么没东西」的答案——交互器与监督器的正常回合分隔线是不打的，报错若也丢，
    // 一次失败在记录里就只剩事件到达那行，看着与「角色没动」一模一样。
    case 'llm/retry': {
      const failure = data.failure ?? {}
      const attempt = data.maxRetries === undefined
        ? '第 ' + data.retry + ' 次'
        : '第 ' + data.retry + '/' + data.maxRetries + ' 次'
      // delayMs 是退避算出来的浮点数（7.218899481749182 这种），记到 0.1s 就够读了。
      const wait = Math.round((data.delayMs ?? 0) / 100) / 10
      out('[报错] 模型请求失败，即将' + attempt + '重试（等待 ' + wait + 's）：'
        + clip(failure.message, 400))
      break
    }
    // 收尾分三种：模型报错（error，LlmFailure 原文）、被取消/被重启补记（aborted/interrupted，
    // 成因见 AgentCancelCause: user/parent/hook/disposed）、正常收尾（completed，以及 max-tokens
    // 与 blocked）。前两类一律打——它们是「这一轮没成」的唯一痕迹；正常收尾仍按上面那条角色取舍
    // 办，另外两种少见收尾也照打，免得它们像无事发生。
    case 'turn/end': {
      const reason = data.reason ?? {}
      const kind = reason.kind ?? '?'
      if (kind === 'error') {
        const failure = reason.error ?? {}
        const code = failure.code === undefined ? '' : '（' + failure.code + '）'
        out('[报错] 回合 ' + data.turn + ' 因错误结束' + code + '：' + clip(failure.message, 400))
      } else if (kind === 'aborted' || kind === 'interrupted') {
        const cause = kind === 'aborted' ? ': ' + (reason.reason?.kind ?? '?') : ''
        out('[中断] 回合 ' + data.turn + ' 结束（' + kind + cause + '）')
      } else if (kind !== 'completed' || !dropped('turn-end')) {
        out('──── 回合 ' + data.turn + ' 结束（' + kind + '）────')
      }
      break
    }
    default:
      break
  }
})
JS
)"

# 找本角色的常驻会话日志（会话 id 见 lib-roles.sh 的 role_session_id）。
# 目录名按前缀匹配：监督器的日志到上限会轮转出 <基础 id>.<时间戳> 的会话，精确匹配会一直
# 盯着旧的那份说「没有新事件」。`ls -t` 已按 mtime 排序，取到的是最新一份。
session_file() {
  local dir="$1/sessions"
  [ -d "$dir" ] || return 1
  ls -t "$dir"/*/"$2"*/session.v3.jsonl.zstd 2>/dev/null | head -1
}

# render_new <role> <起始行号> —— 把本角色从该行起的新事件渲染出来；设了 --log 时同一串文本
# 同时追加进日志文件。tee 挂在管道末尾，而不是把整个脚本的 stdout 套一层 `exec > >(tee …)`：
# 后者在退出时和一个还没写完的 tee 抢时机，记录的最后几行会丢。
render_new() {
  # WATCH_DECODED：把整份已解出来的会话日志一并给它，好让 FILTER 从**跳过的那一段**里补出
  # rolebus 路由（见 FILTER 里的说明）——只喂新行的话，轮换/重启后常常一条注记都打不出来。
  if [ -n "$LOG" ]; then
    tail -n "+$2" "$DECODED" | WATCH_ROLE="$1" WATCH_DECODED="$DECODED" node -e "$FILTER" | tee -a "$LOG"
  else
    tail -n "+$2" "$DECODED" | WATCH_ROLE="$1" WATCH_DECODED="$DECODED" node -e "$FILTER"
  fi
}

DECODED="$(mktemp)"
trap 'rm -f "$DECODED"' EXIT

say "role-watch: 角色 [$ROLES]，每 ${INTERVAL}s 读一次会话日志（Ctrl-C 退出）"
# 「1s」是上面那个读取节奏，不是角色被唤醒的间隔——两者都叫「间隔」，混起来会以为角色的
# 周期没生效。放在 banner 里是因为这是唯一一眼能看错的地方。
say "            （这是 watcher 读文件的节奏；角色的观察间隔见它每轮的抬头上和 role-daemon 的 banner）"
[ -z "$LOG" ] || say "            记录追加进：$LOG"
for role in $ROLES; do
  declare_role_home "$role"
  # DSH_HOME 逐行打出来：看错 DSH_HOME 时症状是「什么都没发生」（安静地盯着另一份会话
  # 日志），而不是报错——只有把解析到的根摆在眼前才看得出来。
  session_id="$(role_session_id "$role")"
  file="$(session_file "$DSH_HOME" "$session_id" || true)"
  if [ -n "$file" ]; then
    say "  [$role] DSH_HOME=$DSH_HOME"
    say "         $file"
  else
    say "  [$role] DSH_HOME=$DSH_HOME —— 还没有会话日志（$DSH_HOME/sessions/*/$session_id*/），等它出现"
  fi
  eval "seen_$role=0"
  # 两个计数器配成一对：已打印的行数，以及那些行属于哪份日志。轮转换文件时必须一起归零，
  # 否则新文件行数少于旧文件的已打印数，`total > seen` 不再成立（见循环里的说明）。
  eval "file_$role=''"
done
say ""

while :; do
  # 起它的那个进程（role-daemon.sh，或用户自己的 shell）没了就退出。daemon 只在 Ctrl-C
  # 那条路上会连带杀掉本进程：它收到单独的 SIGTERM 时，trap 要等前台那条 dsh 跑完才执行，
  # 那之前本进程会一直对着一个已经没有进程在写的会话日志打印。
  kill -0 "$PPID" 2>/dev/null || exit 0
  for role in $ROLES; do
    declare_role_home "$role"
    file="$(session_file "$DSH_HOME" "$(role_session_id "$role")" || true)"
    [ -n "$file" ] || continue
    # 轮转后 session_file 会指向新的一份：行数计数器不跟着归零的话，新文件的行数少于旧文件的
    # 已打印数，`total > seen` 不再成立——本角色从此一行都不打印，看着像整个角色卡死。
    eval "prev_file=\$file_$role"
    if [ "$file" != "$prev_file" ]; then
      eval "file_$role=\$file"
      eval "seen_$role=0"
      [ -z "$prev_file" ] || warn "[$role] 会话已轮转，改看新的一份：$file"
    fi
    zstd -dc "$file" > "$DECODED" 2>/dev/null || true   # 尾部半帧会让 zstd 非零退出，完整行已在 stdout
    total="$(wc -l < "$DECODED" | tr -d ' ')"
    eval "seen=\$seen_$role"
    # 缺省从头跳过已有内容：这是「看接下来发生什么」的工具，一上来重放整份会话（可能几百行）
    # 会把新事件冲掉。要重放加 --from-start。
    if [ "$seen" -eq 0 ] && [ "$FROM_START" != 1 ]; then
      eval "seen_$role=$total"
      seen="$total"
      warn "[$role] （跳过已记录的 $total 行，只打印接下来发生的事；要重放加 --from-start）"
    fi
    if [ "$total" -gt "$seen" ]; then
      render_new "$role" "$((seen + 1))"
      eval "seen_$role=$total"
    fi
  done
  sleep "$INTERVAL"
done
