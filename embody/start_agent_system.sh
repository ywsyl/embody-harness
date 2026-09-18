#!/usr/bin/env bash
# start_agent_system —— 一条命令拉起整套角色系统：interactor / orchestrator / supervisor 各自
# 一个独立 dsh 常驻进程在**后台**待机，本终端前台只打印 role-watch 的逐轮记录（事件到达、思考、
# 发言、工具调用、PTC、结果）。
#
# 三个角色必须共用一份 DSH_HOME：角色之间只经 rolebus 的共享 spool（$DSH_HOME/rolebus）通信，
# 各用各的 DSH_HOME 就是三个互不相通的收件箱——谁发给谁的事件都没人收，且失败是静默的。本脚本
# 缺省给三个角色指同一个仓库内目录 embody/.dsh-dev/system（已 gitignore），可用 DSH_HOME 或
# --dsh-home 换（换就要整体换，不能只给某一个角色换）。
#
# 缺省三件套的选择：interactor 管交互与任务下发、orchestrator 编排执行、supervisor 定时观察本体
# 与环境并告警（它是三者里唯一不收入站事件、由定时器唤醒的角色）。planner 是 interactor +
# orchestrator 的**合并版**，与它们不同时启动（见 dsh-plugins/rolebus/src/event-catalog.ts），
# 本脚本拒绝这种组合。
#
# 每个角色**怎么起**不在本文件里重写：物化 profile、seed settings、不挂 headless 而叠
# <Role>/resident.patch.yml、解析模型 key、单实例守卫全在 scripts/role-daemon.sh（角色的唤醒
# 方式也在那里与 dsh-plugins/*/src 里）。本文件只做这些事：定位共享 DSH_HOME 与角色、串行物化、
# 逐个后台拉起、启动健康检查、起一个前台 watcher、退出时把整棵进程树收干净。
#
# 本文件的变量名紧跟中文时必须加花括号（${VAR}，同 scripts/role-daemon.sh）：bash 3.2 在 UTF-8
# locale 下会把「（」的首字节并进变量名，拼出一个从没被赋值过的名字，set -u 下整个脚本就死在那一行。
#
# 为什么角色输出不直接打在终端：终端留给 role-watch，一眼能看出「哪个角色在干什么」。每个角色的
# 启动输出落在 $DSH_HOME/start-<role>.out，dsh 自己的逐行输出落在 $DSH_HOME/daemon-<role>.log
# （后者由 role-daemon.sh 的 tee 写）。两条路径都在收尾的汇总里打出来。
#
# 逐轮记录的落盘口径就是 watcher 的输出：本终端上打印的每一行同时追加进 $DSH_HOME/role-watch.log
# （scripts/role-watch.sh --log，逐行同源，不是第二套格式；历次启动按启动头分段）。--no-watch 时不
# 起 watcher，这个文件本次不写。
#
# 启动流水走完（角色都进了待机、watcher 已就位）会打一段 `####` 框的就绪块——那是「现在可以投
# 事件了」的信号，有角色没起来时同样打，标题换成「部分就绪」并点名是谁。它只在终端上，记录文件
# 仍由 watcher 独占。
#
# 三条已知取舍：
#   1) interactor 的 speak / speak_listen 走 /dev/tty（plugin_tools/tool_executor.py）：后台进程
#      仍有控制终端，所以它的播报会直接打在本终端上与 role-watch 交错，它反问你时也在本终端打字
#      回车。只想单独听/答 interactor：用 Communicator/local_start.py --role interactor。
#   2) 后台角色不挂各自的 watcher（ROLE_WATCH=0，那是 scripts/role-daemon.sh 的开关）：三份记录
#      打在同一终端上会互相盖；本脚本自己起一个覆盖全部角色的 watcher，加 --no-watch 则一个都不起。
#   3) 某个角色启动即退出时：打印它的日志尾巴、其余角色继续跑、本脚本最终以非零码退出。半个系统
#      在跑会静默丢事件，这一条必须看得见，所以不做「静默降级成还能跑的那几个」。
#
# 用法：
#   embody/start_agent_system.sh                       交互器 + 编排器 + 监督器（缺省）
#   embody/start_agent_system.sh --roles "interactor orchestrator"
#   embody/start_agent_system.sh --dsh-home /tmp/lv6   换共享 DSH_HOME（可与别的实例并存）
#   embody/start_agent_system.sh --from-start          watcher 连已记录的回合一起重放
#   embody/start_agent_system.sh --no-watch            只拉角色，终端不打印记录（Ctrl-C 仍收全套）
#
# 可覆盖环境变量（与 scripts/*.sh 同一套）：EMBODY / ENGINE / NODE22_BIN / DSH_HOME / WATCH_LOG
# （逐轮记录文件，缺省 $DSH_HOME/role-watch.log）/ WATCH_FULL=1（不做逐角色取舍，见 role-watch.sh：
# 缺省把监督器的 [思考] 与 [结果] 从记录里去掉，它的每一轮只留 [发言] 与工具行）。
# 本文件自己的三个节奏值：
#   START_STAGGER       角色之间隔几秒起（缺省 2）：三次 profile 物化与 settings.yaml 的读改写
#                       不并发跑（settings-seed 是读-改-写，非原子）。
#   START_CHECK_SECONDS 启动健康检查窗口（缺省 5）：窗口内进程没了就报出来并打日志尾巴。
#                       常驻启动不跑模型，这个窗口零模型请求。
#   WATCH_INTERVAL      watcher 读会话日志的节奏（缺省 = role-watch.sh 自己的 1s）。
#   KILL_GRACE_SECONDS  收尾时给引擎的收尾时间（缺省 5），到点强杀剩下的进程。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-roles.sh
source "$REPO_ROOT/scripts/lib-roles.sh"       # role_dir / role_profile_patch / role_resident_patch
# shellcheck source=scripts/lib-local-env.sh
source "$REPO_ROOT/scripts/lib-local-env.sh"   # local_env_value：只按行取值，不 source exp.bashrc

DEFAULT_ROLES="interactor orchestrator supervisor"

EMBODY="${EMBODY:-$REPO_ROOT}"
ENGINE="${ENGINE:-$(cd "$EMBODY/.." && pwd)}"
NODE22_BIN="${NODE22_BIN:-$(cd "$REPO_ROOT/../.." && pwd)/harness/.runtime/node22/bin}"
export PATH="$NODE22_BIN:$PATH"
export CI=true npm_config_verify_deps_before_run=false DSH_TELEMETRY_DISABLED=1
export EMBODY ENGINE EMBODY_CONFIGS_DIR="${EMBODY_CONFIGS_DIR:-$EMBODY/configs}" ENGINE_DIR="$ENGINE"

STAGGER="${START_STAGGER:-2}"
CHECK_SECONDS="${START_CHECK_SECONDS:-5}"
GRACE_SECONDS="${KILL_GRACE_SECONDS:-5}"
WATCH_INTERVAL="${WATCH_INTERVAL:-}"

show_usage() {
  # 从第 2 行打印到第一个空行为止 = 本文件头部注释；行号不写死（改注释不会漂）。
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# has_role <role> —— 本角色的名字是否在本次的 --roles 里（空格分隔，逐字比较）。
has_role() {
  case " $ROLES " in
    *" $1 "*) return 0 ;;
    *)        return 1 ;;
  esac
}

# archive_count <role> —— 本角色归档目录里的事件数（累计）：收件侧先归档再投递，这个数是
# 「确实收到过多少封」的判据（投递失败也留在归档里）。
archive_count() {
  find "$DSH_HOME/rolebus/archive/$1" -name '*.json' 2>/dev/null | wc -l | tr -d ' '
}

# kill_tree <pid> [signal] —— 深度优先收掉一棵进程树（先子后父）。
# 顺序是关键：父进程先没掉，它的子进程会被 init 收养，`pgrep -P` 就再也找不到它们——这正是
# 「Ctrl-C 之后还有 node 在跑」的来源（bash 收到 SIGTERM 时不会转给正在跑的 pnpm 管道，
# 见 scripts/role-daemon.sh 的文件头）。
#
# 停系统这件事也只能由这里做：三个角色是**非交互 shell 的后台作业**，按 POSIX 继承
# 「SIGINT 忽略」，所以终端上的 Ctrl-C 不会让它们退出（那个信号对本脚本有效、对它们无效）。
# Ctrl-C 之所以能收掉整套，是本脚本的 trap 显式 kill_tree 的结果。
kill_tree() {
  local pid="$1" sig="${2:-TERM}" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    kill_tree "$child" "$sig"
  done
  kill -"$sig" "$pid" 2>/dev/null || true
}

# 退出路径唯一：正常收尾、Ctrl-C、以及中途任何失败都走这里。重复调用被 CLEANED 挡住——
# EXIT 与 INT/TERM 会连着进来两次。
cleanup() {
  local role pid code any waited
  if [ "$CLEANED" = 1 ]; then return 0; fi
  CLEANED=1
  # 清理期间的第二次 Ctrl-C 不再进来：半清的状态比多跑几秒更难查。
  trap '' INT TERM
  echo
  echo "== 收尾：停 watcher 与各角色的进程树（最多等 ${GRACE_SECONDS}s，之后强杀）"
  if [ -n "$WATCHER_PID" ]; then
    kill "$WATCHER_PID" 2>/dev/null || true
    wait "$WATCHER_PID" 2>/dev/null || true
  fi
  for role in $ROLES; do
    # role 只可能是 lib-roles.sh 登记过的四个名字之一（未登记的在上面的校验里已经退出），
    # 所以这里的间接赋值不会把外部输入当代码求值。
    eval "pid=\${PID_$role:-}"
    if [ -n "$pid" ]; then kill_tree "$pid" TERM; fi
  done
  waited=0
  while [ "$waited" -lt "$GRACE_SECONDS" ]; do
    any=0
    for role in $ROLES; do
      eval "pid=\${PID_$role:-}"
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then any=1; fi
    done
    if [ "$any" -eq 0 ]; then break; fi
    sleep 1
    waited=$((waited + 1))
  done
  for role in $ROLES; do
    eval "pid=\${PID_$role:-}"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "   [$role] 还活着（收尾窗口用尽）→ 强杀" >&2
      kill_tree "$pid" KILL
    fi
  done
  # 收尸并报每个角色的退出码与归档数。只删自己写的那份标记（supervisor 的标记由 role-daemon.sh
  # 自己写自己删），且只在内容还是自己的 pid 时删——中途被强杀留下的旧标记由下一次启动覆盖。
  for role in $ROLES; do
    eval "pid=\${PID_$role:-}"
    [ -n "$pid" ] || continue
    code=0
    wait "$pid" 2>/dev/null || code=$?
    if [ "$role" != supervisor ] && [ -f "$DSH_HOME/$role.pid" ]; then
      if [ "$(cat "$DSH_HOME/$role.pid" 2>/dev/null || true)" = "$pid" ]; then
        rm -f "$DSH_HOME/$role.pid"
      fi
    fi
    echo "   [$role] exit=${code}，归档累计 $(archive_count "$role") 条事件；启动输出 $DSH_HOME/start-$role.out，dsh 输出 $DSH_HOME/daemon-$role.log"
  done
  [ -z "$WATCHER_PID" ] || echo "== 逐轮记录：${WATCH_LOG_FILE}（本次追加，历次启动都在里面）"
  echo "== 结束。"
  return 0
}

ROLES=""
ROLES_GIVEN=""
DSH_HOME_ARG="${DSH_HOME:-}"
WATCH=1
FROM_START=0
while [ $# -gt 0 ]; do
  case "$1" in
    --roles)
      [ -n "${2:-}" ] || { echo "start_agent_system: --roles 需要一个角色名，多个用空格分隔" >&2; exit 2; }
      ROLES="${2}"; ROLES_GIVEN=1; shift 2 ;;
    --dsh-home)   DSH_HOME_ARG="${2:-}"; shift 2 ;;
    --from-start) FROM_START=1; shift ;;
    --no-watch)   WATCH=0; shift ;;
    -h|--help)    show_usage; exit 0 ;;
    *) echo "start_agent_system: 未知参数 $1（见 --help）" >&2; exit 2 ;;
  esac
done
[ -n "$ROLES_GIVEN" ] || ROLES="$DEFAULT_ROLES"

# 角色名校验：逐字校验完再进后面的 eval —— 角色名只有 lib-roles.sh 登记过的那几个能过。
SEEN=""
for role in $ROLES; do
  case " $SEEN " in
    *" $role "*) echo "start_agent_system: 角色 $role 在 --roles 里出现了两次" >&2; exit 2 ;;
  esac
  SEEN="$SEEN $role"
  dir="$(role_dir "$role")" || exit 2      # 未登记的角色：lib-roles.sh 已把原因打到 stderr
  for patch in "$dir/cordis.patch.yml" "$dir/resident.patch.yml"; do
    [ -f "$patch" ] || {
      echo "start_agent_system: 角色 $role 缺 $patch —— 这个角色还没有 profile 层/常驻形态" >&2
      exit 1
    }
  done
done
# 两个版本不同时启动：planner 是 interactor + orchestrator 的合并版，事件目录里 cloud 那两条的
# target 是单值，同时跑会互相抢事件（见 event-catalog.ts 文件头）。
if has_role planner && { has_role interactor || has_role orchestrator; }; then
  echo "start_agent_system: planner 与 interactor/orchestrator 不同时启动（合并版 vs 分立版）" >&2
  echo "                  要跑合并版：--roles planner[ supervisor]；要跑分立版：去掉 planner。" >&2
  exit 2
fi

# DSH_HOME 一律换成绝对路径再往下传：相对路径每一处解析出的目录都不一样——引擎（`( cd <引擎根> &&
# pnpm dsh … )`，见 role-daemon.sh）按引擎根解，脚本侧（daemon 日志、profile 的 cp）按调用者的 cwd
# 解，于是 profile / 会话日志 / spool 会散在两个不同的 `.dsh-dev/…` 下，症状是「什么都没发生」
# 而不是报错。`~/` 也在这里展开（值是引号传进来的话，shell 不会替你展）。
case "$DSH_HOME_ARG" in
  '~')   DSH_HOME="$HOME" ;;
  '~/'*) DSH_HOME="$HOME/${DSH_HOME_ARG#'~/'}" ;;
  *)     DSH_HOME="${DSH_HOME_ARG:-$REPO_ROOT/.dsh-dev/system}" ;;
esac
mkdir -p "$DSH_HOME"
DSH_HOME="$(cd "$DSH_HOME" && pwd)"
export DSH_HOME
# 逐轮记录文件：口径是 watcher 的输出（scripts/role-watch.sh --log），这里只决定它落在哪。追加
# 不覆盖——同一次 DSH_HOME 下历次启动都进同一个文件，靠启动头分段（角色、时间、是否重放）。
WATCH_LOG_FILE="${WATCH_LOG:-$DSH_HOME/role-watch.log}"

# 单实例：同一 DSH_HOME 下同一个角色只能有一个常驻进程。会话写租约只挡得住「会话 id 相同」的那
# 一个，而第二个进程的 agent 建不起来时**它照样轮询同一个收件箱**——被它领走的事件无人处理，静默
# 丢件。所以这里先看标记文件，活着就拒绝启动，不做「覆盖式重启」（那等于删掉别人的上下文）。
#   - $DSH_HOME/<role>.pid：本脚本给非 supervisor 的角色写，Communicator/local_start.py 也写；
#   - supervisor 的那一份（$DSH_HOME/supervisor.pid）由 scripts/role-daemon.sh 自己写自己删，
#     **本脚本只读不写**：先写下去会让它的守卫看到「已有监督器在跑」而拒绝启动。
for role in $ROLES; do
  marker="$DSH_HOME/$role.pid"
  [ -f "$marker" ] || continue
  pid="$(cat "$marker" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) continue ;;                       # 内容不是 pid：当陈旧标记，启动时覆盖
  esac
  if kill -0 "$pid" 2>/dev/null; then
    echo "start_agent_system: $role 已经在这个 DSH_HOME 下跑着（pid ${pid}，标记 ${marker}）" >&2
    echo "  再起一个，它的 agent 会因会话写租约建不起来，但它仍在轮询同一个收件箱——" >&2
    echo "  被它领走的事件没有人处理，直接丢。先停掉它（kill ${pid}），或换 --dsh-home。" >&2
    exit 3
  fi
done

# 模型 key / 端点先查齐：三个角色共用一份 settings.yaml 与同一批 LOCAL_* 值，缺一个就是三个角色
# 都起不来，不如在这里一次说清（role-daemon.sh 自己也查 key，但那时已经起了两个进程）。
for name in LOCAL_API_KEY LOCAL_BASE_URL LOCAL_MODEL_ds; do
  if ! local_env_value "$name" >/dev/null; then
    echo "start_agent_system: 缺 ${name}（configs/exp.bashrc 或环境）——角色 agent 起不来，不启动。" >&2
    exit 1
  fi
done

# 物化 profile 与 settings 由本脚本**串行**做一次：交给三个 daemon 各自并发做的话，三次
# `pnpm add` 与同一份 settings.yaml 的读改写（settings-seed.sh 的 merge 不是原子写）会互相踩。
# 这里先做完，daemon 侧那次就是幂等空转。
echo "== 物化角色 profiles 到 ${DSH_HOME}（首次启动较慢：每个 profile 要 pnpm add 本地 bundle）"
if ! DSH_HOME="$DSH_HOME" ROLES="$ROLES" "$EMBODY/scripts/rolebus-setup.sh" | sed 's/^/   /'; then
  echo "start_agent_system: 角色 profile 物化失败（见上）——不启动任何角色。" >&2
  exit 1
fi
if ! DSH_HOME="$DSH_HOME" "$EMBODY/scripts/settings-seed.sh" | sed 's/^/   /'; then
  echo "start_agent_system: settings seed 失败（见上）——不启动任何角色。" >&2
  exit 1
fi

# 启动前把「东西在哪、怎么看、怎么投事件」一次说清：下面三个角色起来要几秒到几十秒（tsx 源码
# 启动），用户这会儿就能复制投事件命令到另一个终端。
cat <<BANNER

================================================================
 角色系统启动（共享 DSH_HOME=${DSH_HOME}）
   角色         = $ROLES
   spool        = $DSH_HOME/rolebus （三个角色共用它才能互相发事件）
   拦截输出     = $DSH_HOME/start-<role>.out（启动）、daemon-<role>.log（dsh 逐行）
   逐轮记录     = ${WATCH_LOG_FILE}（追加；就是下面本终端打印的那些行）
 本终端：role-watch 实时打印每轮的 事件 / 思考 / 发言 / 工具 / PTC / 结果。
 另开一个终端投一条事件（整行复制；缺 DSH_HOME 前缀会投到另一个 spool，常驻进程看不到）：
   PYTHONPATH=$EMBODY DSH_HOME=$DSH_HOME python3 -m plugin_tools.rolebus cloud-content-update \\
     --action create --description "把客厅的灯打开"
 Ctrl-C 退出（三个角色与它们的子进程一起收）。
================================================================

BANNER

# 信号：先收干净再**就地退出**，不从 trap 返回继续跑——返回的话守望循环会在东西已经收掉之后
# 再转一圈，收尾汇总后面又冒出「role-watch 已退出」，看着像没收干净。
on_signal() {
  cleanup
  trap - EXIT
  exit "$1"
}

CLEANED=0
WATCHER_PID=""
PID_interactor=""
PID_orchestrator=""
PID_planner=""
PID_supervisor=""
trap cleanup EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

for role in $ROLES; do
  boot_log="$DSH_HOME/start-$role.out"
  echo "== [$role] 后台拉起（启动输出：${boot_log#"$REPO_ROOT"/}）"
  # ROLE_WATCH=0：本角色不挂自己的 watcher（三份记录会互相盖），由下面一个 watcher 统一看。
  ROLE_WATCH=0 "$EMBODY/scripts/role-daemon.sh" --role "$role" >"$boot_log" 2>&1 &
  pid=$!
  eval "PID_$role=$pid"
  # 标记写的是 role-daemon.sh 那个 bash：它退出即整条管道（pnpm → node）结束，与清理时机一致。
  if [ "$role" != supervisor ]; then
    printf '%s\n' "$pid" > "$DSH_HOME/$role.pid"
  fi
  sleep "$STAGGER"
done

# 启动健康检查：常驻启动不跑模型，所以只是「进程还在不在」。发起不来就立刻报（附日志尾巴），
# 而不是等用户对着一个空终端猜。
echo "== 等 ${CHECK_SECONDS}s 看各角色是否活着（常驻启动不发模型请求）"
sleep "$CHECK_SECONDS"
STATUS=0
live=0
for role in $ROLES; do
  eval "pid=\${PID_$role:-}"
  if kill -0 "$pid" 2>/dev/null; then
    echo "   [$role] 在跑（pid ${pid}）"
    live=$((live + 1))
  else
    STATUS=1
    eval "DEAD_$role=1"
    echo "   [$role] 启动即退出 —— 日志尾巴（$DSH_HOME/start-$role.out）：" >&2
    tail -n 20 "$DSH_HOME/start-$role.out" 2>/dev/null | sed 's/^/      /' >&2
  fi
done
if [ "$live" -eq 0 ]; then
  echo "start_agent_system: 没有角色活下来，不启动 watcher。" >&2
  exit "$STATUS"
fi

WATCHER_PID=""
if [ "$WATCH" = 1 ]; then
  # 一个 watcher 覆盖全部角色：角色名逐个 --role 传（role-watch.sh 的可重复参数），显式带
  # --dsh-home —— 它缺省按角色各自取仓库内目录，与本次的共享 DSH_HOME 不是一处。
  WATCH_ARGS=()
  for role in $ROLES; do
    WATCH_ARGS["${#WATCH_ARGS[@]}"]="--role"
    WATCH_ARGS["${#WATCH_ARGS[@]}"]="$role"
  done
  if [ "$FROM_START" = 1 ]; then WATCH_ARGS["${#WATCH_ARGS[@]}"]="--from-start"; fi
  if [ -n "$WATCH_INTERVAL" ]; then
    WATCH_ARGS["${#WATCH_ARGS[@]}"]="--interval"
    WATCH_ARGS["${#WATCH_ARGS[@]}"]="$WATCH_INTERVAL"
  fi
  WATCH_ARGS["${#WATCH_ARGS[@]}"]="--dsh-home"
  WATCH_ARGS["${#WATCH_ARGS[@]}"]="$DSH_HOME"
  # 终端的每一行由 watcher 自己同时追加进记录文件（--log），本脚本不再旁路 tee 一份：两处各写
  # 一次会让格式分叉，也会让「终端上有没有」和「文件里有没有」出现分歧。
  WATCH_ARGS["${#WATCH_ARGS[@]}"]="--log"
  WATCH_ARGS["${#WATCH_ARGS[@]}"]="$WATCH_LOG_FILE"
  "$REPO_ROOT/scripts/role-watch.sh" "${WATCH_ARGS[@]}" &
  WATCHER_PID=$!
else
  echo "== --no-watch：不打印记录（本次也不写 ${WATCH_LOG_FILE}），只等角色（Ctrl-C 退出）。"
fi

# 就绪信号：上面那些是启动流水（物化、逐个拉起、健康检查），这一段是「现在可以开始用了」的那
# 一刻——请求的角色都已进入待机、watcher 已在盯记录。用不同的框线单独打出来，为的是在一屏流水
# 里一眼找到它；也是除 --no-watch 之外唯一该看的地方（不用去数 pid）。
# 半就绪也照样打（框线一样醒目）：有角色没起来时沉默或含糊过去，会让人以为整整齐齐都在跑。
DEAD_LIST=""
for role in $ROLES; do
  eval "dead=\${DEAD_$role:-0}"
  [ "$dead" = 0 ] || DEAD_LIST="$DEAD_LIST $role"
done
if [ "$STATUS" -eq 0 ]; then
  READY_HEAD="系统就绪"
  READY_NOTE="全部角色都在待机（常驻启动不发模型请求，等事件唤醒）"
else
  READY_HEAD="部分就绪"
  READY_NOTE="这些角色没起来：${DEAD_LIST# }（日志尾巴在上面；其余角色照常待机）"
fi
if [ "$WATCH" = 1 ]; then
  READY_RECORD="${WATCH_LOG_FILE}"
else
  READY_RECORD="本次未写（--no-watch）"
fi
# 先等 watcher 把自己的 banner 打出来（它在后台，谁先抢到终端不一定），就绪块压在最后一行。
sleep 1
echo
echo "################################################################"
echo "##  ${READY_HEAD}（启动耗时 ${SECONDS}s）：${READY_NOTE}"
echo "##  记录   = ${READY_RECORD}"
echo "##  投事件 = PYTHONPATH=$EMBODY DSH_HOME=$DSH_HOME python3 -m plugin_tools.rolebus \\"
echo "##             cloud-content-update --action create --description \"…\""
echo "##  停止   = 本终端 Ctrl-C（三个角色与它们的子进程一起收）"
echo "################################################################"
echo

# 守望循环：角色的进程一个个没了就报出来（每角色只报一次），全没了或 watcher 先退出就收尾。
# 不用 `wait -n`：本机 bash 3.2 没有它（脚本其余部分同样只用到 3.2 的能力）。
while :; do
  if [ -n "$WATCHER_PID" ] && ! kill -0 "$WATCHER_PID" 2>/dev/null; then
    echo "start_agent_system: role-watch 已退出，收掉整套角色。" >&2
    break
  fi
  live=0
  for role in $ROLES; do
    eval "pid=\${PID_$role:-}"
    if kill -0 "$pid" 2>/dev/null; then
      live=$((live + 1))
      continue
    fi
    eval "reported=\${DEAD_$role:-0}"
    if [ "$reported" = 0 ]; then
      eval "DEAD_$role=1"
      echo "[$role] 进程已退出（pid ${pid}）：之后发给它的事件只有归档，没有轮次。" >&2
      echo "        要看它为什么退出：tail -n 40 $DSH_HOME/start-$role.out" >&2
      STATUS=1
    fi
  done
  if [ "$live" -eq 0 ]; then
    echo "start_agent_system: 全部角色已退出。" >&2
    break
  fi
  sleep 1
done

exit "$STATUS"
