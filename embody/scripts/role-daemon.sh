#!/usr/bin/env bash
# role-daemon.sh —— 常驻待机启动任一角色：进程起来**不跑模型**，此后由该角色的唤醒源叫它。
#
# 常驻形态：不挂 headless、不传 task，用 --patch 叠 <Role>/resident.patch.yml
#   → 唤醒源被打开，本角色的 agent 建出来停在 idle。唤醒源有三类：
#     · 事件型（interactor / orchestrator / planner）：rolebus.resident=true，
#       每收到一封事件按该封事件的 priority 叫醒它（P1 steer 交给最近的步骤边界 / P2 followup
#       独占下一轮 / P3 inject 只投不唤醒；发件方没给就用该事件的固定优先级），处理完回到 idle；
#     · 定时观察型（supervisor）：@embody/dsh-supervisor-loop 每隔 n 秒自己读一遍观测
#       文件跑一轮，它不接任何入站事件；
#     · 开机自查（interactor）：叠加层给了 rolebus.startupWake，装配完成就主动起一轮（要问
#       用户的任务由代码在拉起前挑好、拼进那段文本，见下面的 boot_check_tasks_text 与
#       Interactor/prompts/ 的【开机自查】）——**这是唯一一种启动就产生模型请求的形态**，其余
#       角色/未配置时仍是零请求。
#   三类都是开机没有任何问候。
#
# 开机自查只有交互器有（上面第三类）：清单该不该问用户是一件要跟用户一来一回的事。其余角色
# 起来直接待机——编排器也一样，它的第一轮模型请求来自事件（通常是用户挑中它的任务之后，
# 交互器发来的那条 task_update_int）。
#
# 常驻必须没有 headless bundle：headless 的启动插件**要求**恰好一条 task（缺了直接
# program.error 退出），而常驻恰恰不传 task——带着它会以 "a task is required" 在启动瞬间退出。
#
# 进程为什么不退出：rolebus 的收件轮询 setInterval 持有事件循环（空闲 agent 自身不持有
# 任何句柄，没有这个定时器 node 会直接退出）。启动只建出一个会话：会话日志里只有 boot
# 事件（配了 startupWake 的交互器还多一条开机自查消息，随后就是那一轮），没有任何问候。
#
# 同一 DSH_HOME 同时只能有一个常驻进程。事件型角色靠会话 id 固定（rolebus-<role>）带来的写
# 租约挡住第二个进程：它会报 SessionAlreadyOwnedError，日志里只留一条 `[rolebus] 常驻 agent
# 建立失败…`，而它照样轮询同一个收件箱，领走的事件无人处理。监督器的会话 id 由它自己的叠加层
# 给（supervisor-loop），续不续接又由 configs/agents.py 的 RESET_ON_START_ROLES 决定——挡第二个
# 进程的那道保护跟着换地方（不在表里是写租约，在表里是 reset_role_history 自己的试锁），所以它
# 另有一道最早的 <DSH_HOME>/supervisor.pid 守卫。其余角色的 <DSH_HOME>/<role>.pid 由 starter.sh
# 写（cloud-emit.sh 按它判收件人在不在跑），本脚本不碰；本脚本起的单角色进程不写那个标记。
#
# 拉起前的清历史（reset_role_history）：configs/agents.py 的 RESET_ON_START_ROLES 列出「每轮
# 重开一局」的角色——本脚本在拉起它们之前把上一轮的会话整份归档到 $DSH_HOME/session-archive/
# （表上写着为什么这件事与「清收件箱」绑在一张表里）。不在表里的角色续接上一轮；两类角色都会
# 归档轮转出去的那几份（没有进程会 resume 它们，理由见函数注释）。
# 监督器另有几处与其余角色不同：单实例由 supervisor_guard 的 pidfile 守住、轮询间隔 / 上下文
# 预算 / 会话日志上限取自 configs/configs.py、每轮读的三份观测路径取自 configs/paths.py（都经
# 进程环境过桥给叠加层的 `!!js`，见下面的 supervisor 分支）。
#
# 唤醒它（事件型角色）：另开一个终端投一条事件，例如
#   scripts/manual/rolebus-emit.sh interactor interaction_request --source orchestrator \
#     --task-id task-7 --payload "description=出门前确认是否带伞"
# 本脚本 banner 里的示例只是一句提示；事件类型与路由的事实源是
# dsh-plugins/rolebus/src/event-catalog.ts（shell 侧不重复那份目录）。
#
# 若本角色白名单里有 speak / speak_listen（configs/agents.py），它们调的是机器人上的语音服务
# （plugin_tools/audio_tools.py → skills/tts-speak 的 run_tts_speak.sh、skills/asr-listener 的
# run_asr.sh）：要真实喇叭与麦克风，但不碰控制终端，后台跑也照样出声、开麦。speak_listen 会一直
# 占到 ASR 收音窗口结束（configs.ASR_LISTEN_WINDOW），那期间本角色不接别的事件。
#
# 启动时同时拉起 scripts/role-watch.sh（同一个 DSH_HOME）：每轮的思考、工具调用与
# PTC 结果直接打在本终端上；不拉的话本终端的 stdout 只有 speak 的播报文本。这些行同时追加进
# <DSH_HOME>/role-watch.log（可覆盖 WATCH_LOG）——逐轮记录的落盘口径就是 watcher 的输出。
#
# 编排器与其余事件型角色有一处不同：它的 profile 层有一行 task-context（每轮把待做清单里归它
# 执行、且已开始的条目注入模型输入，清单不再走 task_list 工具），那一行要的清单路径经本脚本
# 过桥（见下面 orchestrator 分支的 ORCHESTRATOR_TASK_LIST_PATH）。
#
# 用法：
#   scripts/role-daemon.sh --role orchestrator      常驻待机（Ctrl-C 退出）
#   scripts/role-daemon.sh --role interactor
#   scripts/role-daemon.sh --role planner           interactor+orchestrator 的合并版（另起一版）
#   scripts/role-daemon.sh --role supervisor        定时观察循环唤醒（自己读观测文件，不收入站事件）
#
# 可覆盖环境变量：EMBODY / ENGINE / NODE_BIN_DIR / DSH_HOME / ROLE_WATCH（=0 不跟 watcher）。
# 换角色的清历史口径改 configs/agents.py 的 RESET_ON_START_ROLES，不在环境变量里另立一个开关。
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

# 路径与运行时统一来自 configs/scripts-env.sh；自检挡住「node 版本落在 [24.0.0, 24.2.0)，
# dsh 静默空转」这一类无声失败。
# shellcheck source=../configs/scripts-env.sh
source "$REPO_ROOT/configs/scripts-env.sh"
# shellcheck source=../configs/lib-shell-paths.sh
source "$REPO_ROOT/configs/lib-shell-paths.sh"   # cmcc_path：路径只认 configs/paths.py
embody_check_node role-daemon || exit 1
embody_apply_env

# 从 configs/configs.py 取一个值。本仓库的可调参数只有那一处事实源，而 cordis 的配置层读不到
# Python——监督器那几个值经进程环境过桥（见下面的 supervisor 分支）。$1 是在 configs.configs
# 的命名空间里求值的表达式。取不到就报错返回：让它以 NaN 装载也会失败，但报出来的是插件校验，
# 看不出根因在这儿。
# 路径同理，取值器是上面 source 进来的 cmcc_path（configs/paths.py 那一侧）。
configs_value() {
  (cd "$EMBODY" && python3 -c "from configs.configs import *; print($1)") || {
    echo "role-daemon: 取不到 configs/configs.py 的 $1（启动要用它，没有第二个来源）" >&2
    return 1
  }
}

# 同上，取值器换成 configs/agents.py —— 与智能体绑定的表与开关都在那一侧（工具白名单、
# 任务状态、各角色开关）。$1 同样是在该模块命名空间里求值的表达式（可以直接调表外的
# 辅助函数，例如 role_model_provider('interactor')）。agents.py 不 import 任何东西、没有
# 模块级副作用，所以不像 lib-shell-paths.sh 的 _cmcc_val 那样要把 stdout 转到 stderr。
agents_value() {
  (cd "$EMBODY" && python3 -c "from configs.agents import *; print($1)") || {
    echo "role-daemon: 取不到 configs/agents.py 的 $1（启动要用它，没有第二个来源）" >&2
    return 1
  }
}

# 开机自查要注入的那批任务（文本块）：挑法与渲染都在 plugin_tools/task_board.py 的
# boot_check_text（有 interrupted 就只给 interrupted，否则给 pending / stopped），这里只把它
# 搬进进程环境，叠加层的 `!!js` 再取回去拼进 startupWake。**挑任务由代码做**，模型开机不再
# 自己去读整份清单——所以这一块必须在这一轮起来之前就定下来，取不到就不启动（不编空串：
# 空串会让那一轮看起来像「没有要问的任务」，模型就真的不问了）。
# `end=''` 是必要的：块本身要原样进 startupWake，别让 print 多吃一行。
boot_check_tasks_text() {
  # 取值期间被 import 的模块往 stdout 打的东西（configs/paths.py 读设备配置失败就打印一行）
  # 会混进返回值里，所以那段转到 stderr —— 同 lib-shell-paths.sh 的 _cmcc_val。取完再打印。
  (cd "$EMBODY" && python3 -c "
import contextlib, sys
with contextlib.redirect_stdout(sys.stderr):
    from plugin_tools.task_board import boot_check_text
    text = boot_check_text()
print(text, end='')") || {
    echo "role-daemon: 取不到开机自查的任务清单文本（plugin_tools/task_board.py 的 boot_check_text）" >&2
    return 1
  }
}

# 监督器的单实例守卫：挡住第二个进程的那道保护会随配置换地方（见文件头第 4 段），本函数是两者
# 之前、也早于任何副作用的那一道——它给出的是「已经有一个在跑」这句话，而不是一个建不起 agent
# 的进程在轮询同一份观测。按 kill -0 判活，进程没了就当陈旧文件覆盖。判活只看 pid 存活，不看那个
# pid 现在是谁：万一 pid 被复用，错的是「拒绝启动」这一侧，报错里给出 pidfile 路径，删掉即可起步。
#
# 调用点在 DSH_HOME 定下来之后、任何副作用之前——挡下第二个进程时不该已经动过它的 profile 与
# settings。用局部变量判活：全局 SUPERVISOR_PIDFILE 的含义是「我拥有这个文件」（cleanup 会删
# 它），挡下第二个进程时它不属于我，不能认领。
supervisor_guard() {
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
}

# reset_role_history —— 把本角色上一轮留下的会话整份挪进 $DSH_HOME/session-archive/，让本轮
# 从空会话开始；挪哪几份分两类，判据是「引擎重启时会 resume 哪一份」：
#
#   · 基础 id 那一份（目录名 = role_session_id）：**引擎重启续接的就是它**——事件型角色是
#     rolebus.resident 的固定 id，监督器是叠加层的 sessionId（Supervisor/resident.patch.yml），
#     两边都是「stat 得到就 resume」。所以它续不续接由 configs/agents.py 的 RESET_ON_START_ROLES
#     决定：在表里就挪走（本轮空会话），不在表里就原样留下（本轮接着上一轮）。
#   · 轮转出去的那几份（<基础 id>.<时间戳>，见 supervisor-loop/src/index.ts 第 4 条约定）：
#     没有任何进程会 resume 它们，所以**无论本角色在不在表里都挪走**——留在 sessions/ 里会被
#     引擎当成一份「目录名与日志里的会话 id 对不上」的正常会话列出来，且它们只增不减。
#
# 名单与「为什么和清收件箱绑在一张表里」都写在那份表上；这里只管会话这一半（收件箱由
# starter.sh 清，它要排在后台组件之前，早于本脚本）。
#
# 归档名带上会话目录自己的名字：同一 projectKey 下轮转过的几份会在同一秒里一起归档，只用
# <project>.<时间戳> 会撞名——`mv` 撞到已存在的目录不是覆盖，是把这一份塞进那一份里面
# （静默嵌套，比覆盖更难查）。
#
# 正被持有的会话**不挪**：会话目录里的 session.lock 是 POSIX flock，常驻进程活着就占着它，
# 挪走等于抽掉它脚下的租约（那个进程仍在轮询同一个收件箱，被它领走的事件无人处理）。判据就是
# 拿同一把锁试一次：不问进程名、不看 pid 文件，问的是「这份会话此刻有没有人在写」。轮转出去的
# 那几份不用问（没有进程会持有它们）；基础 id 那一份只在本角色清历史时才问——不清就是不碰，
# 它此刻正被那条常驻进程持有，那正是本轮的续接对象。
reset_role_history() {
  local reset_roles
  reset_roles="$(cmcc_agents RESET_ON_START_ROLES)" || {
    echo "role-daemon: 取不到 configs/agents.py 的 RESET_ON_START_ROLES（清不清历史会话没有第二个来源）" >&2
    exit 1
  }
  local session_id base session_dir project stamp n=0 n_base=0
  local archive_root="$DSH_HOME/session-archive"
  session_id="$(role_session_id "$ROLE")"
  case " $reset_roles " in
    *" $ROLE "*) n_base=1 ;;   # 本角色清历史：基础 id 那一份也挪
  esac
  stamp="$(date +%Y%m%d-%H%M%S)"
  # 前缀匹配把两类都捞进来，逐份按目录名区分（精确匹配会漏掉轮转件）。
  for session_dir in "$DSH_HOME"/sessions/*/"$session_id"*; do
    [ -d "$session_dir" ] || continue
    base="$(basename "$session_dir")"
    if [ "$base" = "$session_id" ]; then
      [ "$n_base" = 1 ] || continue
      if [ -f "$session_dir/session.lock" ] && ! flock -n "$session_dir/session.lock" true 2>/dev/null; then
        echo "role-daemon: 会话 $session_dir 正被一个活进程持有 —— 本角色在这份 DSH_HOME 下已经在跑。" >&2
        echo "             不归档（那会抽掉它脚下的租约）。先停掉那个进程，或换 --dsh-home。" >&2
        exit 1
      fi
    fi
    project="$(basename "$(dirname "$session_dir")")"
    mkdir -p "$archive_root"
    mv "$session_dir" "$archive_root/$project.$base.$stamp"
    n=$((n + 1))
  done
  if [ "$n_base" = 1 ] && [ "$n" -eq 0 ]; then
    echo "   - 没有可归档的会话（$DSH_HOME/sessions/*/$session_id 不存在），本轮从空会话开始"
  elif [ "$n_base" = 1 ]; then
    echo "   - 上一轮会话 $n 份已归档到 ${archive_root}（本轮从空会话开始；要留下它：把 $ROLE 从 configs/agents.py 的 RESET_ON_START_ROLES 里去掉）"
  elif [ "$n" -gt 0 ]; then
    echo "   - 轮转出去的旧会话 $n 份已归档到 ${archive_root}（本轮续接 $session_id 那一份）"
  fi
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

# 两件事都排在任何副作用之前：挡下第二个进程时不该已经动过它的 profile 与 settings，归档
# 也会被同一次「先判活再动手」保护住。此刻 DSH_HOME 已定，正是最早的可用点。
if [ "$ROLE" = supervisor ]; then supervisor_guard; fi
reset_role_history

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
LOCAL_API_KEY="$(local_env_value LOCAL_API_KEY)" || { echo "缺少 LOCAL_API_KEY（install/secret.env）" >&2; exit 1; }
export LOCAL_API_KEY
# web_search 的密钥（base bundle 的 web-search-deepseek 行，apiKeyEnv: DEEPSEEK_API_KEY）也从同一份
# 文件带进本进程环境：有就带上、没有不编——缺了只是 web_search 调不通，web_fetch 走的是另一条路。
if DEEPSEEK_API_KEY="$(local_env_value DEEPSEEK_API_KEY)"; then
  export DEEPSEEK_API_KEY
fi

# 文件围栏（dsh-plugins/file-guard）的允许范围：**每个角色都一样**（configs/paths.py 的
# RUNTIME_DIR = <仓库根>/runtime_files），所以放在逐角色分支之外、无条件 export —— 与上面
# 的 ROLE_MODEL_PROVIDER 同形。四个角色的 profile 层用 `!!js process.env.…` 取回它当
# allowedRoot（本机临时目录由插件自己补，不过桥）。取不到就退出：这一格空了插件会以
# 「allowedRoot 是空的」报错起不来，但报出来的是插件校验，看不出根因在这儿。
ROLE_FILE_GUARD_ALLOWED_ROOTS="$(cmcc_path RUNTIME_DIR)" || exit 1
export ROLE_FILE_GUARD_ALLOWED_ROOTS

case "$ROLE" in
  # 交互器的流式播报（dsh-plugins/speech-stream）两项事实源分两处：**开关**是「与智能体绑定的
  # 开关」，在 configs/agents.py；**阈值**是通用参数，在 configs/configs.py。这里取值后放进程
  # 环境，Interactor/cordis.patch.yml 的 `!!js process.env.*` 再取回去。
  # 开关是 Python 布尔，过桥成 '1'/'0'（那边判 `!== '1'` 即关：取不到就是上面 exit 1，
  # 不存在「读不到就当开」）。记录过期阈值只是工具侧的读判据，不经环境。
  interactor)
    SPEECH_STREAM_ENABLED="$(agents_value '1 if SPEECH_STREAM_ENABLED else 0')" || exit 1
    SPEECH_MAX_CHARS_PER_CHUNK="$(configs_value 'SPEECH_MAX_CHARS_PER_CHUNK')" || exit 1
    SPEECH_DELIVERY_MODE="$(configs_value 'SPEECH_DELIVERY_MODE')" || exit 1
    export SPEECH_STREAM_ENABLED SPEECH_MAX_CHARS_PER_CHUNK SPEECH_DELIVERY_MODE
    # 开机自查要注入的那批任务（代码挑的）：本角色的叠加层用 `!!js process.env.*` 把它拼进
    # startupWake，那一轮的输入就带着它（见 Interactor/resident.patch.yml 与 prompts 的
    # 【开机自查】）。**在这一刻取一次**：挑选要与 starter.sh 的收尾（executing → interrupted）
    # 同一个时间点，早于此取到的还是上一轮的 executing。
    INTERACTOR_BOOT_TASKS="$(boot_check_tasks_text)" || exit 1
    export INTERACTOR_BOOT_TASKS
    # 每轮注入的【支持地点清单】（dsh-plugins/location-context）：正文由 configs/configs.py 的
    # supported_locations_text() 现渲染（名字的抽取口径仍只有 load_supported_locations 一处），
    # 这里取一次放进程环境，profile 层的 `!!js process.env.*` 再取回去。**启动时取一次是刻意的**：
    # waypoint_nav 的工具说明（plugin_tools/bridge.py 装载时注入）也是这个新鲜度——两条路不同步，
    # 就会出现「注入的清单里有这个地点、调 waypoint_nav 却被硬拒」的窗口期。
    INTERACTOR_SUPPORTED_LOCATIONS="$(configs_value 'supported_locations_text()')" || exit 1
    export INTERACTOR_SUPPORTED_LOCATIONS
    WAKE_EXAMPLE='interaction_request --source orchestrator --task-id task-7 --payload "question=出门前确认是否带伞"' ;;
  # 编排器的 profile 层有一行 task-context：每轮把待做清单里归它执行、且已开始的条目注入模型
  # 输入（清单不再走 task_list 工具）。那一行要的 path 只有 configs/paths.py 一处事实源，与下面
  # 监督器那三条观测同一条过桥路径——这里取到绝对值放进进程环境，profile 层的
  # `!!js process.env.*` 再取回去。缺了它编排器会以「missing required value path」起不来：
  # 不编默认路径是刻意的。
  orchestrator)
    ORCHESTRATOR_TASK_LIST_PATH="$(cmcc_path RUNTIME_PENDING_TASKS_FILE)" || exit 1
    export ORCHESTRATOR_TASK_LIST_PATH
    WAKE_EXAMPLE='task_update_int --source interactor --task-id task-7 --priority P1 --payload "description=去517找小王，让他来518开会"'
    ;;
  # planner 只收第三方事件（没有 rolebus_send_* 可发），唤醒它就用云端下发那一条。
  planner)      WAKE_EXAMPLE='planner_cloud_update --source cloud --task-id task-7 --payload "action=update" --payload "description=去517找小王，让他来518开会"' ;;
  # 监督器收不到任何事件（目录里没有 target 为 supervisor 的条目），投给它也没人领：
  # 唤醒源是常驻叠加层打开的定时观察循环，这里的提示替换掉上面那两行「投一条事件」。
  supervisor)
    # 本角色的可调参数（轮询间隔、上下文预算）只有 configs/configs.py 一处事实源，这里取值后
    # 放进程环境，叠加层里的 `!!js process.env.*` 再取回去。
    SUPERVISOR_INTERVAL_MS="$(configs_value 'int(SUPERVISOR_INTERVAL * 1000)')" || exit 1
    SUPERVISOR_COMPACT_RETAIN_TOKENS="$(configs_value 'supervisor_compact_retain_tokens()')" || exit 1
    SUPERVISOR_COMPACT_THRESHOLD_RATIO="$(configs_value 'SUPERVISOR_CONTEXT_CEILING / supervisor_context_window()')" || exit 1
    SUPERVISOR_SESSION_MAX_BYTES="$(configs_value 'int(SUPERVISOR_SESSION_MAX_MB * 1024 * 1024)')" || exit 1
    export SUPERVISOR_INTERVAL_MS SUPERVISOR_COMPACT_RETAIN_TOKENS SUPERVISOR_COMPACT_THRESHOLD_RATIO
    export SUPERVISOR_SESSION_MAX_BYTES
    # 首轮延迟（毫秒）：监督器前几跳丢掉，等观测器把它要读的三份文件写成本轮的内容。
    # **调用方可以预先设好这个变量把它顶掉**（starter.sh 在「本轮没有观测器可等」时设成 0：
    # --roles-only 这条流水不起观测器、--no-status-observer 本轮本来就没有观测器——两种情况都没
    # 有要等的对象，让第一轮白等一分钟没有意义）。这条顶替是「没有对象可等」的判据，不是第二个
    # 事实源：取值范围与含义仍由 configs/configs.py 定义。
    if [ -z "${SUPERVISOR_FIRST_TICK_DELAY_MS:-}" ]; then
      SUPERVISOR_FIRST_TICK_DELAY_MS="$(configs_value 'int(SUPERVISOR_FIRST_TICK_DELAY_SEC * 1000)')" || exit 1
    fi
    export SUPERVISOR_FIRST_TICK_DELAY_MS
    # 每轮要读的三份观测（视觉观察 / 本体状态 / 运动状态）：路径只有 configs/paths.py 一处
    # 事实源，同样过桥。取值器给的是绝对路径（paths.py 由 ROOT_DIR 推导），叠加层不再依赖
    # 启动工作目录——换目录启动读到的还是同一份。
    SUPERVISOR_VISION_OBS="$(cmcc_path RUNTIME_VISION_OBSERVATION)" || exit 1
    SUPERVISOR_ROBOT_STATUS="$(cmcc_path RUNTIME_ROBOT_STATUS)" || exit 1
    SUPERVISOR_MOTION_STATUS="$(cmcc_path RUNTIME_MOTION_STATUS)" || exit 1
    export SUPERVISOR_VISION_OBS SUPERVISOR_ROBOT_STATUS SUPERVISOR_MOTION_STATUS
    # 每轮旁路跑一遍的 runtime_files 体积清理脚本：路径同样只有 configs/paths.py 一处事实源
    # （清理判据在脚本与 configs 里，插件只负责起进程）。
    SUPERVISOR_CLEANUP_SCRIPT="$(cmcc_path RUNTIME_CLEANUP_SCRIPT)" || exit 1
    export SUPERVISOR_CLEANUP_SCRIPT
    # 每轮注入的【支持地点清单】（dsh-plugins/location-context）：本角色要判一条任务里的地点是否
    # 合法，而它的工具面里没有 waypoint_nav。取值与「启动时取一次」的理由同上面 interactor 支。
    SUPERVISOR_SUPPORTED_LOCATIONS="$(configs_value 'supported_locations_text()')" || exit 1
    export SUPERVISOR_SUPPORTED_LOCATIONS
    # 只用于 banner：把解出来的间隔摆在眼前。它和 watcher 那句「每 1s 读一次会话日志」都叫
    # 「间隔」，但管的是两件事——看错会以为这里的配置没生效。不进环境，故不 export。
    SUPERVISOR_INTERVAL_SEC="$(configs_value 'SUPERVISOR_INTERVAL')" || exit 1
    WAKE_HINT="本角色不收入站事件：唤醒源是常驻叠加层里的定时观察循环，等它自己跑下一轮
  （观察间隔 ${SUPERVISOR_INTERVAL_SEC} 秒，取自 configs/configs.py 的 SUPERVISOR_INTERVAL；观测源见 Supervisor/resident.patch.yml）" ;;
  *)            WAKE_EXAMPLE='<event_type> --source <peer> --task-id task-7 --payload "description=…"' ;;
esac

# 本角色的模型路由覆盖值：空串 = 不改路由（照走 agent-default-model，思考开着），非空 = 改走
# 那条关思考的镜像路由。**逐角色的开/关只有 configs/agents.py 的 ROLE_THINKING 一处事实源**，
# 这里只把它解析成「该走哪条 provider」放进进程环境，各角色常驻叠加层的
# `!!js process.env.ROLE_MODEL_PROVIDER` 再取回去。
# 对**每个**角色都无条件 export（开思考时是空串）：叠加层用 `?? ''` 兜底，但变量没导出与导出成
# 空串是两回事，保持一致更省事。取值失败会以 UnknownRoleError 退出（表里漏登记角色 = 这个开关
# 静默失效，宁可起不来）。
ROLE_MODEL_PROVIDER="$(agents_value "role_model_provider('$ROLE')")" || exit 1
export ROLE_MODEL_PROVIDER
WAKE_HINT="${WAKE_HINT:-另开一个终端投事件：
   scripts/manual/rolebus-emit.sh $ROLE $WAKE_EXAMPLE}"

# banner 那句「启动不跑模型」只对没配 startupWake 的角色成立：配了它的角色装配完成就先跑
# 一轮（见 dsh-plugins/rolebus 的 startupWake，现在只有交互器）。判据取常驻叠加层本身，不另立
# 一张角色名单——配置改了这里跟着对。
if grep -q '^[[:space:]]*startupWake:' "$RESIDENT_PATCH" 2>/dev/null; then
  STARTUP_NOTE="启动先跑一轮开机自查，此后等唤醒源叫它"
else
  STARTUP_NOTE="启动不跑模型，等唤醒源叫它"
fi

cat <<BANNER

================================================================
 常驻待机（角色 ${ROLE}）：${STARTUP_NOTE}（Ctrl-C 退出）。
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
  # 收拢的那一个文件，与 starter.sh 的总 watcher 同一路径，靠启动头分段。
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
