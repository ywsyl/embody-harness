#!/usr/bin/env bash
# role-daemon.sh —— 常驻待机启动任一角色：进程起来**不跑模型**，此后由该角色的唤醒源叫它。
#
# 常驻形态：不挂 headless、不传 task，用 --patch 叠 <Role>/resident.patch.yml
#   → 唤醒源被打开，本角色的 agent 建出来停在 idle。两类唤醒源：
#     · 事件型（interactor / orchestrator / planner）：rolebus.resident=true，
#       每收到一封事件按该条目的 wakeMode 叫醒它（当前都是 steer：交给最近的步骤边界），
#       处理完回到 idle；
#     · 定时观察型（supervisor）：@embody/dsh-supervisor-loop 每隔 n 秒自己读一遍观测
#       文件跑一轮，它不接任何入站事件。
#   两类都是开机没有任何问候，也没有任何模型请求。
#
# 常驻必须没有 headless bundle：headless 的启动插件**要求**恰好一条 task（缺了直接
# program.error 退出），而常驻恰恰不传 task——带着它会以 "a task is required" 在启动瞬间退出。
#
# 进程为什么不退出：rolebus 的收件轮询 setInterval 持有事件循环（空闲 agent 自身不持有
# 任何句柄，没有这个定时器 node 会直接退出）。启动只建出一个空会话：会话日志里只有 boot
# 事件，没有任何消息，也不发任何模型请求——所以开机既不会问候，也不会误触发一轮。
#
# 同一 DSH_HOME 同时只能有一个常驻进程。事件型角色靠会话 id 固定（rolebus-<role>）带来的写
# 租约挡住第二个进程：它会报 SessionAlreadyOwnedError，日志里只留一条 `[rolebus] 常驻 agent
# 建立失败…`，而它照样轮询同一个收件箱，领走的事件无人处理。监督器不吃这层保护——它每次启动
# 换一份新会话（见 supervisor_prepare），会话 id 不重复，所以改用 <DSH_HOME>/supervisor.pid
# 判活。Communicator/local_start.py 另有一份 <DSH_HOME>/<role>.pid 单实例标记。
#
# 监督器的启动形态有三处与其余角色不同（supervisor_prepare + supervisor 分支）：
# 单实例由 pidfile 守住、每次启动归档**所有**本角色的会话（含运行期轮转出来的，新进程只判断
# **现在**的状态，不继承旧的「已告警过哪些」），这两件在 DSH_HOME 定下来的当场就做——守卫
# 要在任何副作用之前落地；轮询间隔、上下文预算与会话日志上限取自 configs/configs.py（经进程
# 环境过桥给叠加层的 `!!js`）。FRESH_SESSION=0 可退回续接基础 id 那一份会话。
#
# 唤醒它（事件型角色）：另开一个终端投一条事件，例如
#   scripts/rolebus-emit.sh interactor interaction_request --source orchestrator \
#     --task-id task-7 --payload "description=出门前确认是否带伞"
# 本脚本 banner 里的示例只是一句提示；事件类型与路由的事实源是
# dsh-plugins/rolebus/src/event-catalog.ts（shell 侧不重复那份目录）。
#
# 若本角色白名单里有 speak / speak_listen（configs/agent_plugin.py），它们经 /dev/tty 播报并
# 阻塞读一行（plugin_tools/tool_executor.py 的 _say_text / _read_line），被唤醒的轮次就**必须在
# 真实终端里跑**：没有控制终端时 speak 退回 stderr、listen 退回 stdin。
#
# 启动时同时拉起 scripts/role-watch.sh（同一个 DSH_HOME）：每轮的思考、工具调用与
# PTC 结果直接打在本终端上；不拉的话本终端的 stdout 只有 speak 的播报文本。这些行同时追加进
# <DSH_HOME>/role-watch.log（可覆盖 WATCH_LOG）——逐轮记录的落盘口径就是 watcher 的输出。
#
# 用法：
#   scripts/role-daemon.sh --role orchestrator      常驻待机（Ctrl-C 退出）
#   scripts/role-daemon.sh --role interactor
#   scripts/role-daemon.sh --role planner           interactor+orchestrator 的合并版（另起一版）
#   scripts/role-daemon.sh --role supervisor        定时观察循环唤醒（自己读观测文件，不收入站事件）
#
# 可覆盖环境变量：EMBODY / ENGINE / NODE22_BIN / DSH_HOME / ROLE_WATCH（=0 不跟 watcher）
# / FRESH_SESSION（=0 时监督器续接上一份会话，缺省每次启动归档旧会话、从零开始）
# DSH_HOME 缺省 = embody/.dsh-dev/<role>（仓库内、已 gitignore，与该角色的试跑脚本共用）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib-roles.sh
source "$REPO_ROOT/scripts/lib-roles.sh"

show_usage() {
  # 从第 2 行打印到第一个空行为止 = 本文件头部注释，行号不写死（改注释不会漂）。
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

ROLE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --role)   ROLE="${2:-}"; shift 2 ;;
    -h|--help) show_usage; exit 0 ;;
    *) echo "role-daemon: 未知参数 $1（只接受 --role <角色>）" >&2; exit 2 ;;
  esac
done
[ -n "$ROLE" ] || { echo "role-daemon: 缺 --role（例如 --role orchestrator）" >&2; exit 2; }

EMBODY="${EMBODY:-$REPO_ROOT}"
ENGINE="${ENGINE:-$(cd "$EMBODY/.." && pwd)}"
NODE22_BIN="${NODE22_BIN:-$(cd "$REPO_ROOT/../.." && pwd)/harness/.runtime/node22/bin}"
export PATH="$NODE22_BIN:$PATH"
export CI=true npm_config_verify_deps_before_run=false DSH_TELEMETRY_DISABLED=1
export EMBODY_CONFIGS_DIR="$EMBODY/configs" ENGINE_DIR="$ENGINE"

# 从 configs/configs.py 取一个值。本仓库的可调参数只有那一处事实源，而 cordis 的配置层读不到
# Python——监督器那几个值经进程环境过桥（见下面的 supervisor 分支）。$1 是在 configs.configs
# 的命名空间里求值的表达式。取不到就报错返回：让它以 NaN 装载也会失败，但报出来的是插件校验，
# 看不出根因在这儿。
configs_value() {
  (cd "$EMBODY" && python3 -c "from configs.configs import *; print($1)") || {
    echo "role-daemon: 取不到 configs/configs.py 的 $1（启动要用它，没有第二个来源）" >&2
    return 1
  }
}

# 监督器的启动准备：单实例守卫 + 每次启动换一份新会话。两件事是一次决策，放在一处；调用点在
# DSH_HOME 定下来之后、任何副作用之前——挡下第二个进程时不该已经动过它的 profile 与 settings。
#
# 为什么需要单实例守卫：会话写租约只在会话 id 相同时才挡得住第二个进程，而下面每次启动都把
# 上一份会话归档、让固定 id 空出来——归档会把正在跑的那个进程的租约一起挪走，双开就没人挡了。
# pidfile 按 kill -0 判活，进程没了就当陈旧文件覆盖。判活只看 pid 存活，不看那个 pid 现在是
# 谁：万一 pid 被复用，错的是「拒绝启动」这一侧，报错里给出 pidfile 路径，删掉即可起步。
#
# 为什么每次启动换一份新会话：监督器是唯一一个「跑不完」的角色，会话日志就是它的记忆。续接
# 上一份意味着新进程从旧的「已经告警过哪些」中间态开始，而它要判断的是**现在**的状态。
# 旧会话不删：整份目录挪到 $DSH_HOME/session-archive/ 留档。必须挪出 sessions/ 之外——留在
# 里面会被引擎当成一份「目录名与日志里的会话 id 对不上」的会话列出来。
#
# FRESH_SESSION=0 跳过归档，插件于是续接**基础 id 那一份**（supervisor-loop 本身）——运行期
# 轮转出去的那几份（<基础 id>.<时间戳>）不参与续接，它们是各自独立的会话。要接着看最新那一份
# 得另外挑，不在这个开关的含义里。
supervisor_prepare() {
  # 用局部变量判活：全局 SUPERVISOR_PIDFILE 的含义是「我拥有这个文件」（cleanup 会删它），
  # 挡下第二个进程时它不属于我，不能认领。
  local pidfile="$DSH_HOME/supervisor.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null || echo 0)" 2>/dev/null; then
    {
      echo "role-daemon: 已有监督器在跑（pid $(cat "$pidfile")）；同一份 DSH_HOME 只允许一个。"
      echo "              确认它没在跑就删掉再启动：rm $pidfile"
    } >&2
    exit 1
  fi
  echo $$ > "$pidfile"
  SUPERVISOR_PIDFILE="$pidfile"

  [ "${FRESH_SESSION:-1}" = "1" ] || return 0
  local session_id session_dir project stamp archive_root="$DSH_HOME/session-archive"
  session_id="$(role_session_id supervisor)"
  stamp="$(date +%Y%m%d-%H%M%S)"
  # 前缀匹配：运行期日志到上限会轮转出 <基础 id>.<时间戳> 的会话（见插件头部第 4 条约定），
  # 精确匹配会漏掉最新那份，把它留在 sessions/ 里当一份正常会话列出来。
  for session_dir in "$DSH_HOME"/sessions/*/"$session_id"*; do
    [ -d "$session_dir" ] || continue
    project="$(basename "$(dirname "$session_dir")")"
    mkdir -p "$archive_root"
    # 归档名必须带上会话目录自己的名字：同一 projectKey 下轮转过的几份会在同一秒里一起归档，
    # 只用 <project>.<时间戳> 会撞名——`mv` 撞到已存在的目录不是覆盖，是把这一份塞进那一份里
    # 面（静默嵌套，比覆盖更难查）。
    mv "$session_dir" "$archive_root/$project.$(basename "$session_dir").$stamp"
    echo "   - 上一份会话已归档到 ${archive_root}（要续接上一份：FRESH_SESSION=0 启动）"
  done
}

RESIDENT_PATCH="$(role_resident_patch "$ROLE")"
PROFILE_PATCH="$(role_profile_patch "$ROLE")"
# 缺文件就退出：否则会在 dsh 启动后才以 `--patch` 读不到文件的形式炸，看不出是角色没有常驻形态。
[ -f "$RESIDENT_PATCH" ] || {
  echo "role-daemon: 角色 $ROLE 缺常驻叠加层 $RESIDENT_PATCH —— 该角色还没有常驻形态" >&2
  exit 1
}
[ -f "$PROFILE_PATCH" ] || {
  echo "role-daemon: 角色 $ROLE 缺 profile 层事实源 $PROFILE_PATCH" >&2
  exit 1
}

DSH_HOME="${DSH_HOME:-$(role_default_dsh_home "$ROLE")}"
export DSH_HOME
# 日志带角色名：多个角色可以共用同一个 DSH_HOME（共享 spool 才能互相发事件），此时
# 同名日志会被后启动的那个 `tee` 截断，把先启动角色的历史抹掉。
LOG="$DSH_HOME/daemon-$ROLE.log"
PROFILE="$DSH_HOME/profiles/$ROLE"

# 监督器的启动准备（单实例守卫 + 归档上一份会话）在有副作用之前：挡下第二个进程时，不该
# 已经动过它的 profile 与 settings。此刻 DSH_HOME 已定，正是最早的可用点。
if [ "$ROLE" = supervisor ]; then supervisor_prepare; fi

echo "== ROLE=$ROLE DSH_HOME=$DSH_HOME"
ROLES="$ROLE" "$EMBODY/scripts/rolebus-setup.sh" | sed 's/^/   /'
# 幂等：试跑脚本给这份共享 profile 补过 headless，常驻要把它摘掉（原因见文件头）。
node -e '
const fs = require("node:fs")
const manifest = JSON.parse(fs.readFileSync(process.argv[1], "utf8"))
const bundles = manifest.dsh.profile.bundles
const at = bundles.indexOf("@deepseek-ai/dsh-headless")
if (at >= 0) {
  bundles.splice(at, 1)
  fs.writeFileSync(process.argv[1], JSON.stringify(manifest, null, 2) + "\n")
  console.error("   - 摘掉 bundle @deepseek-ai/dsh-headless（常驻不传 task，headless 会要求一条）")
}
' "$PROFILE/package.json"
"$EMBODY/scripts/settings-seed.sh" | sed 's/^/   /'
# shellcheck source=lib-local-env.sh
source "$EMBODY/scripts/lib-local-env.sh"
LOCAL_API_KEY="$(local_env_value LOCAL_API_KEY)" || { echo "缺少 LOCAL_API_KEY（configs/exp.bashrc）" >&2; exit 1; }
export LOCAL_API_KEY

case "$ROLE" in
  interactor)   WAKE_EXAMPLE='interaction_request --source orchestrator --task-id task-7 --payload "question=出门前确认是否带伞"' ;;
  orchestrator) WAKE_EXAMPLE='task_content_update --source interactor --task-id task-7 --payload "action=create" --payload "task_description=去517找小王，让他来518开会"' ;;
  # planner 只收第三方事件（没有 rolebus_send_* 可发），唤醒它就用云端下发那一条。
  planner)      WAKE_EXAMPLE='planner_cloud_content_update --source cloud --task-id task-7 --payload "action=create" --payload "description=去517找小王，让他来518开会"' ;;
  # 监督器收不到任何事件（目录里没有 target 为 supervisor 的条目），投给它也没人领：
  # 唤醒源是常驻叠加层打开的定时观察循环，这里的提示替换掉上面那两行「投一条事件」。
  supervisor)
    # 本角色的可调参数（轮询间隔、上下文预算）只有 configs/configs.py 一处事实源，这里取值后
    # 放进程环境，叠加层里的 `!!js process.env.*` 再取回去。
    SUPERVISOR_INTERVAL_MS="$(configs_value 'int(SUPERVISOR_INTERVAL * 1000)')" || exit 1
    SUPERVISOR_COMPACT_RETAIN_TOKENS="$(configs_value 'SUPERVISOR_RETAIN_ROUNDS * SUPERVISOR_ROUND_TOKENS')" || exit 1
    SUPERVISOR_COMPACT_THRESHOLD_RATIO="$(configs_value 'SUPERVISOR_CONTEXT_CEILING / supervisor_context_window()')" || exit 1
    SUPERVISOR_SESSION_MAX_BYTES="$(configs_value 'int(SUPERVISOR_SESSION_MAX_MB * 1024 * 1024)')" || exit 1
    export SUPERVISOR_INTERVAL_MS SUPERVISOR_COMPACT_RETAIN_TOKENS SUPERVISOR_COMPACT_THRESHOLD_RATIO
    export SUPERVISOR_SESSION_MAX_BYTES
    # 只用于 banner：把解出来的间隔摆在眼前。它和 watcher 那句「每 1s 读一次会话日志」都叫
    # 「间隔」，但管的是两件事——看错会以为这里的配置没生效。不进环境，故不 export。
    SUPERVISOR_INTERVAL_SEC="$(configs_value 'SUPERVISOR_INTERVAL')" || exit 1
    WAKE_HINT="本角色不收入站事件：唤醒源是常驻叠加层里的定时观察循环，等它自己跑下一轮
  （观察间隔 ${SUPERVISOR_INTERVAL_SEC} 秒，取自 configs/configs.py 的 SUPERVISOR_INTERVAL；观测源见 Supervisor/resident.patch.yml）" ;;
  *)            WAKE_EXAMPLE='<event_type> --source <peer> --task-id task-7 --payload "description=…"' ;;
esac
WAKE_HINT="${WAKE_HINT:-另开一个终端投事件：
   scripts/rolebus-emit.sh $ROLE $WAKE_EXAMPLE}"

cat <<BANNER

================================================================
 常驻待机（角色 ${ROLE}）：启动不跑模型，等唤醒源叫它（Ctrl-C 退出）。
 $WAKE_HINT
 本终端会跟着打印每轮发生了什么（事件、思考、工具/PTC、结果）——见下。
 进程级完整输出也落在：$LOG
================================================================

BANNER

# 每轮的思考与工具调用只有会话日志里有（常驻进程的 stdout 只有 speak 的输出），所以
# 启动时把 watcher 一起拉起来：本终端直接就是「这个角色每轮在干什么」。DSH_HOME 显式
# 传给它——少了这一层，它会去看缺省目录下的另一份会话日志，而人以为看的是这个进程。
# 关掉它：ROLE_WATCH=0。
WATCH_PID=""
SUPERVISOR_PIDFILE=""
# 退出时收拾两样：watcher 是本脚本的子进程（不杀的话 dsh 退出后它还在轮询），以及监督器的
# pidfile（不删的话下次启动会拿一个死 pid 去 kill -0，虽然判得出、但文件一直是脏的）。
# 两条都是「可能不存在」的收拾，故用 `[ … ] && …`：非最后一条的测试失败不会触发 set -e。
cleanup() {
  [ -n "$WATCH_PID" ] && kill "$WATCH_PID" 2>/dev/null
  [ -n "$SUPERVISOR_PIDFILE" ] && rm -f "$SUPERVISOR_PIDFILE"
  return 0
}
trap cleanup EXIT INT TERM

if [ "${ROLE_WATCH:-1}" = "1" ]; then
  # 记录落盘跟着 watcher 走（同一份 watcher 输出，不另起一套格式）：单角色启动也追加进按 DSH_HOME
  # 收拢的那一个文件，与 start_agent_system.sh 的总 watcher 同一路径，靠启动头分段。
  "$EMBODY/scripts/role-watch.sh" --role "$ROLE" --dsh-home "$DSH_HOME" \
    --log "${WATCH_LOG:-$DSH_HOME/role-watch.log}" &
  WATCH_PID=$!
fi

set +e
( cd "$ENGINE" && pnpm dsh --profile "$ROLE" --patch "$RESIDENT_PATCH" ) 2>&1 | tee "$LOG"
status="${PIPESTATUS[0]}"
set -e
echo
# 变量名紧跟中文时必须加花括号（bash 3.2 在某些 locale 下会把 `（` 的首字节并进变量名）。
echo "== $ROLE dsh exit=${status}（完整输出：${LOG}）"
