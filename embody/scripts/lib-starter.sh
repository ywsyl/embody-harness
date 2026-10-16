#!/usr/bin/env bash
# =============================================================================
# lib-starter.sh —— starter.sh 的全部实现。
#
# starter.sh 是这台机器上唯一的启动入口，但入口脚本本身只做一件事：算 REPO_ROOT、source
# 它依赖的库、**按启动顺序调用本文件的函数**。为什么这么分：每一步的判据与理由都写在代码
# 旁边（即本文件），入口脚本要能一眼看清「这一步排在什么位置、在什么之后」——两者混在一起
# 时，顺序会被函数体淹掉。
#
# 依赖调用方（starter.sh）先备好：
#   - 已 source configs/lib-shell-paths.sh（cmcc_*）、configs/scripts-env.sh（节奏参数与
#     embody_* 自检）、scripts/lib-local-env.sh（local_env_value）、scripts/lib-roles.sh（role_*）；
#   - REPO_ROOT = embody/（本文件里没有一处自己推它）；
#   - STARTER_SELF = 入口脚本的绝对路径（只有 --help 打印用法时用得到，见 starter_show_usage）。
#
# 各函数之间靠**同一份全局变量**传递状态（ROLES / DSH_HOME / STATUS / PID_* / AGENT_ARGS …）：
# 它们都在启动脚本那一个 shell 里跑，这是 shell 库的取值口径（同 lib-roles.sh）。
# =============================================================================

# -----------------------------------------------------------------------------
# 用法
# -----------------------------------------------------------------------------

# starter_show_usage <入口脚本路径> —— 打印该脚本头部注释里的用法说明（第 2 行到第一个空行）。
# 路径由调用方传入：本函数定义在本文件里，BASH_SOURCE[0] 指的是本文件，不是入口脚本。
starter_show_usage() {
  sed -n '2,/^$/p' "$1" | sed 's/^# \{0,1\}//'
}

# -----------------------------------------------------------------------------
# 步骤 1：环境（ROS 三件套 + DDS 参数）
# -----------------------------------------------------------------------------

# starter_setup_ros_env —— 起三个 setup.bash，再导出 DDS 三件套的取值。
starter_setup_ros_env() {
  local setup
  # setup.bash 含未加保护的变量引用（AMENT_TRACE_SETUP_FILES 等），nounset 下 source 会失败。
  set +u
  setup="$(cmcc_path ROS_SETUP)" || { echo "starter: 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
  source "$setup"
  setup="$(cmcc_path UNITREE_ROS_SETUP)" || { echo "starter: 取不到 configs/paths.py 的 UNITREE_ROS_SETUP" >&2; exit 1; }
  source "$setup"
  # 机器人运行时包（TTS / ASR / 状态订阅等技能需要）取**运行中的安装**，路径见 configs/paths.py
  # 的 CMCC_ROBOT_SETUP；不要写 /root/cmcc_robot，那是陈旧副本。
  cmcc_require_setup "[starter]" || exit 1
  set -u

  export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"
  export CYCLONEDDS_URI="$(cmcc_conf CYCLONEDDS_URI)"
  export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$(cmcc_conf ROS_DOMAIN_ID)}"
}

# -----------------------------------------------------------------------------
# 步骤 1b：模型服务参数（LOCAL_*）+ 运行时自检
# -----------------------------------------------------------------------------

# starter_setup_model_env —— 取模型服务参数、自检 node 与引擎产物、把 PATH 与导出备好。
#
# LOCAL_* 组件侧与角色侧共用同一份：取「环境优先，其次 install/secret.env」（scripts/lib-local-env.sh
# 的口径），取到就 export 给两边。**不在这里编默认值**——模型服务参数只有 secret.env 一处，写死
# 第二份就会出现「换终端时两边不一致」（组件侧手工单跑同样不编：只原样沿用环境里的值）。
# 缺失时当场拒：这三个值缺了角色 agent 起不来，而那一步在组件已经起好之后。
starter_setup_model_env() {
  local name value
  for name in LOCAL_API_KEY LOCAL_BASE_URL LOCAL_MODEL_ds; do
    value="$(local_env_value "$name")" || {
      echo "starter: 缺 ${name}（环境或 install/secret.env）——角色 agent 起不来，不启动。" >&2
      exit 1
    }
    export "$name=$value"
  done
  embody_check_node starter || exit 1
  embody_check_engine_build starter || exit 1
  embody_apply_env
}

# -----------------------------------------------------------------------------
# 步骤 2：参数
# -----------------------------------------------------------------------------

# starter_parse_args "$@" —— 参数（单一 case；不认识的直接拒）+ 互斥校验。
# 结果落在调用方 shell 的全局变量上：ROLES / DSH_HOME_ARG / WATCH / FROM_START / ROLES_ONLY /
# OBSERVER_ON / FORCE / AGENT_ARGS（组件侧参数原样转交）。
starter_parse_args() {
  local default_roles="interactor orchestrator supervisor"
  local roles_given=""
  local saw_component_switch=0
  ROLES=""
  DSH_HOME_ARG="${DSH_HOME:-}"
  WATCH=1
  FROM_START=0
  ROLES_ONLY=0
  FORCE=0           # 收掉上一套再起：角色侧由本脚本收（starter_guard_single_instance），组件侧转交 start_agent.py
  OBSERVER_ON=1     # 本轮起不起状态观测器：决定了监督器的首轮延迟要不要顶成 0（见 starter_init_teardown）
  AGENT_ARGS=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-frame-producer|--no-vision-observer|--no-motion-status|--no-tts-service|--no-receiver)
        AGENT_ARGS["${#AGENT_ARGS[@]}"]="$1"; saw_component_switch=1; shift ;;
      --force)
        # 两侧都认：本脚本拿它收在跑的角色，原样转交给组件侧（start_agent.py 的 --force 收旧那套组件）。
        # 所以它**不算**组件侧开关——--roles-only 下它照样有对象（在跑的角色）。--roles-only 只拉角色，
        # 转交的那一份落在 AGENT_ARGS 里没人读（那条流水根本不起 start_agent.py）。
        FORCE=1
        AGENT_ARGS["${#AGENT_ARGS[@]}"]="$1"; shift ;;
      --no-status-observer)
        # 单独列出来：它决定「本轮有没有观测器可等」（监督器首轮延迟要不要顶成 0 的判据之一）
        OBSERVER_ON=0
        AGENT_ARGS["${#AGENT_ARGS[@]}"]="$1"; saw_component_switch=1; shift ;;
      --roles)
        [ -n "${2:-}" ] || { echo "starter: --roles 需要一个角色名，多个用空格分隔" >&2; exit 2; }
        ROLES="$2"; roles_given=1; shift 2 ;;
      --dsh-home)
        [ -n "${2:-}" ] || { echo "starter: --dsh-home 需要一个目录" >&2; exit 2; }
        DSH_HOME_ARG="$2"; shift 2 ;;
      --from-start) FROM_START=1; shift ;;
      --no-watch)   WATCH=0; shift ;;
      --roles-only) ROLES_ONLY=1; shift ;;
      -h|--help)
        starter_show_usage "$STARTER_SELF"; exit 0 ;;
      *)
        echo "starter: 未知参数 $1（见 --help）" >&2; exit 2 ;;
    esac
  done
  [ -n "$roles_given" ] || ROLES="$default_roles"

  # --roles-only 是「根本不起 start_agent.py」，--no-* 那些开关没有对象，同时给只能是笔误。
  # --force 不在此列：它两侧都认（见上面那一条）。
  if [ "$ROLES_ONLY" = 1 ] && [ "$saw_component_switch" = 1 ]; then
    echo "starter: --roles-only 不能与组件侧开关（--no-*）同时给。" >&2
    echo "         --roles-only 根本不起 start_agent.py，那些开关没有对象。" >&2
    exit 2
  fi
}

# -----------------------------------------------------------------------------
# 步骤 2b：本次拉起的终端日志
# -----------------------------------------------------------------------------

# starter_start_terminal_log —— 本脚本（以及它拉起的 watcher）打在终端上的每一行，同时落一份到
# runtime_files/logs/<拉起时刻>.log：文件名就是拉起那一刻（YYYYmmdd-HHMMSS），一次拉起一份、
# 只增不改名，也不覆盖上一轮（同一秒里连起两次才会追加进同一份）。
#
# 为什么单独存这一份：角色 watcher 的逐轮记录另有 $DSH_HOME/role-watch.log，组件各自的输出另有
# runtime_files/logs/<组件名>.log，唯独本脚本自己打的启动流水、守卫结论、收尾汇总此前只存在于那个
# 终端里——终端一关，这轮为什么没起来就查不出来了。
#
# 排在参数之后：--help 与参数写错都不是「一次拉起」，不该在 logs/ 里留一份文件（`./starter.sh --help`
# 那条零副作用自检照旧）。代价是前面两步（ROS/模型环境）失败时没有日志——那几句本就打在终端上，且
# 都是每次一样的配置类报错；真正要看回放的，是从这里往下这一整轮。
#
# 实现口径：tee 挂在一个进程替换上（`exec > >(tee …)`），而**不是** scripts/role-watch.sh 那种
# 「tee 挂在管道末尾」的写法。后者在这里用不了：它要求整份输出套进一条管道，对本脚本而言那意味着
# 重新 exec 自己（还要把 stdout 变成管道，连带改掉子进程的缓冲与 Ctrl-C 语义）。代价写在明处：
# bash 退出时不等 tee，文件比进程退出晚几毫秒补齐最后几行（tee 每读到一截就写一截，运行期间是
# 逐行可见的，不是退出时一次性写）。
#
# 两条都是实测出来的，别省：
#   · `trap '' INT` —— 终端 Ctrl-C 打到整个前台进程组，tee 也在里面。不忽略的话 tee 先死，收尾
#     那几行既进不了日志，又让本脚本写到一个没人读的管道上（SIGPIPE，整脚本静默死）。
#   · 先 `: >>"$LOG_FILE"` 试一次写 —— tee 打不开文件时，本脚本每一行输出都撞上 SIGPIPE，
#     那种死法只有一句 exit=141，看不出是日志文件写不了。
starter_start_terminal_log() {
  local dir
  dir="$(cmcc_path RUNTIME_LOGS_DIR)" || { echo "starter: 取不到 configs/paths.py 的 RUNTIME_LOGS_DIR" >&2; exit 1; }
  mkdir -p "$dir" || { echo "starter: 建不了日志目录 $dir" >&2; exit 1; }
  TERMINAL_LOG="$dir/$(date +%Y%m%d-%H%M%S).log"
  : >>"$TERMINAL_LOG" || { echo "starter: 写不了终端日志 $TERMINAL_LOG（目录不可写？）" >&2; exit 1; }
  exec > >( trap '' INT; exec tee -a "$TERMINAL_LOG" ) 2>&1
}

# -----------------------------------------------------------------------------
# 步骤 3：落点
# -----------------------------------------------------------------------------

# starter_check_layout —— start_agent.py 能不能用（见入口脚本头部「落点」）。
# 判据是它要的依赖是否与它同目录——这正是它内部算根的方式，所以是可检的，不用试跑。
# --roles-only 不起组件，没有可检查的对象。
starter_check_layout() {
  [ "$ROLES_ONLY" = 0 ] || return 0
  if [ ! -f "$REPO_ROOT/start_agent.py" ] || [ ! -d "$REPO_ROOT/configs" ]; then
    echo "starter: 找不到可用的 start_agent.py —— 它要从自己所在目录导入 configs/" >&2
    if [ -f "$REPO_ROOT/start_agent.py" ]; then
      echo "         存在 $REPO_ROOT/start_agent.py，但它按「自己所在目录 = 仓库根」找依赖，那里没有 configs/。" >&2
    fi
    echo "         把 configs/ 放回仓库根（start_agent.py 的 _ROOT 也按这一级算），或改它内部的根计算。" >&2
    exit 1
  fi
}

# -----------------------------------------------------------------------------
# 步骤 4：角色校验
# -----------------------------------------------------------------------------

# starter_role_in_list <role> —— ROLES（空格分隔）里有没有这个角色，整词匹配。
starter_role_in_list() {
  case " $ROLES " in
    *" $1 "*) return 0 ;;
    *)        return 1 ;;
  esac
}

# starter_validate_roles —— 逐字校验完再进后面的 eval：角色名只有 lib-roles.sh 登记过的那几个能过。
starter_validate_roles() {
  local role dir patch seen=""
  for role in $ROLES; do
    case " $seen " in
      *" $role "*) echo "starter: 角色 $role 在 --roles 里出现了两次" >&2; exit 2 ;;
    esac
    seen="$seen $role"
    dir="$(role_dir "$role")" || exit 2      # 未登记的角色：lib-roles.sh 已把原因打到 stderr
    for patch in "$dir/cordis.patch.yml" "$dir/resident.patch.yml"; do
      [ -f "$patch" ] || {
        echo "starter: 角色 $role 缺 $patch —— 这个角色还没有 profile 层/常驻形态" >&2
        exit 1
      }
    done
  done
  # 两个版本不同时启动：planner 是 interactor + orchestrator 的合并版，事件目录里 cloud 那两条的
  # target 是单值，同时跑会互相抢事件（见 dsh-plugins/rolebus/src/event-catalog.ts 文件头）。
  if starter_role_in_list planner && { starter_role_in_list interactor || starter_role_in_list orchestrator; }; then
    echo "starter: planner 与 interactor/orchestrator 不同时启动（合并版 vs 分立版）" >&2
    echo "          要跑合并版：--roles planner[ supervisor]；要跑分立版：去掉 planner。" >&2
    exit 2
  fi
}

# -----------------------------------------------------------------------------
# 步骤 5：共享 DSH_HOME 与逐轮记录文件
# -----------------------------------------------------------------------------

# starter_resolve_dsh_home —— 只算一次，算完就 export。
# 取值（优先级 / 缺省 / 绝对化 / mkdir 的理由见 scripts/lib-roles.sh 的 role_system_dsh_home）：
# 组件侧的 receiver 与角色侧的 spool 必须落在同一份上，所以这里是唯一一次解析。
starter_resolve_dsh_home() {
  DSH_HOME="$(role_system_dsh_home "$DSH_HOME_ARG")" || exit 1
  export DSH_HOME
  # 逐轮记录文件：口径是 watcher 的输出（scripts/role-watch.sh --log），这里只决定它落在哪。追加
  # 不覆盖——同一次 DSH_HOME 下历次启动都进同一个文件，靠启动头分段（角色、时间、是否重放）。
  WATCH_LOG_FILE="${WATCH_LOG:-$DSH_HOME/role-watch.log}"
}

# -----------------------------------------------------------------------------
# 步骤 6：单实例守卫
# -----------------------------------------------------------------------------

# starter_guard_single_instance —— 在起任何东西之前查标记文件。
#
# 同一 DSH_HOME 下同一个角色只能有一个常驻进程。会话写租约只挡得住「会话 id 相同」的那一个，而
# 第二个进程的 agent 建不起来时**它照样轮询同一个收件箱**——被它领走的事件无人处理，静默丢件。
# 所以这里先看标记文件：活着就拒绝启动（**不做「覆盖式重启」**：那等于删掉别人的上下文），
# `--force` 时才就地收掉它——在跑的角色没有别人替本脚本收（组件那一半由 start_agent.py 自己的
# --force 收），两半各收各的、都排在拉起之前。
#   - $DSH_HOME/<role>.pid：本脚本给非 supervisor 的角色写；
#   - supervisor 的那一份（$DSH_HOME/supervisor.pid）由 scripts/role-daemon.sh 自己写自己删，
#     **本脚本只读不写**：先写下去会让它的守卫看到「已有监督器在跑」而拒绝启动；收它同样只按
#     这个标记的 pid 收——它的进程树里挂着那个 role-watch（ROLE_WATCH 由这里传 0 时没有）。
# 排在组件之前有两个理由：被拒的这一路是零副作用退出（不该已经拆掉在跑的那一套），而收了角色之后
# 直接往下走时，清收件箱与任务收尾也要赶在被收掉的这套的替代者上线之前——原来的角色还在轮询同一份
# 收件箱，收它就得赶在投递之前。
starter_guard_single_instance() {
  local role marker pid
  for role in $ROLES; do
    marker="$DSH_HOME/$role.pid"
    [ -f "$marker" ] || continue
    pid="$(cat "$marker" 2>/dev/null || true)"
    case "$pid" in
      ''|*[!0-9]*) continue ;;                       # 内容不是 pid：当陈旧标记，启动时覆盖
    esac
    kill -0 "$pid" 2>/dev/null || continue           # 进程不在了：陈旧标记，启动时覆盖
    if [ "$FORCE" = 1 ]; then
      starter_stop_running_role "$role" "$pid" "$marker"
      continue
    fi
    echo "starter: $role 已经在这个 DSH_HOME 下跑着（pid ${pid}，标记 ${marker}）" >&2
    echo "  再起一个，它的 agent 会因会话写租约建不起来，但它仍在轮询同一个收件箱——" >&2
    echo "  被它领走的事件没有人处理，直接丢。先停掉它（kill ${pid}），或换 --dsh-home；" >&2
    echo "  要连它一起收掉再起：加 --force。" >&2
    exit 3
  done
}

# starter_marker_is_role <pid> <role> —— 那个 pid 现在还是不是这个角色的 role-daemon.sh。
#
# 只给 --force 用，判据是**命令行**（`role-daemon.sh` + 紧跟的 `--role <角色>`）：标记文件里的 pid
# 可能早就退出了，而那个号被别的进程捡走（判活只看 kill -0，认不出这种复用）。拒绝启动那一侧判错
# 只是「要人手动删个文件」，收树那一侧判错就是**把无关进程连子孙一起杀**——所以这里宁漏杀不误杀：
# 认不出就只当陈旧标记，交给拉起时覆盖（starter_launch_roles 无条件重写那个文件）。
# 读不到 /proc/<pid>/cmdline（进程已退出、权限不足）同样当「不是它」。
starter_marker_is_role() {
  local pid="$1" role="$2" line seen_daemon=0 expect_role=0
  [ -r "/proc/$pid/cmdline" ] || return 1
  while IFS= read -r line; do
    case "${line##*/}" in role-daemon.sh) seen_daemon=1 ;; esac
    if [ "$expect_role" = 1 ]; then
      [ "$line" = "$role" ] && [ "$seen_daemon" = 1 ] && return 0
      return 1                                     # --role 后面跟的不是本角色：不是它
    fi
    if [ "$line" = "--role" ]; then expect_role=1; fi
  done < <(tr '\0' '\n' < "/proc/$pid/cmdline")
  return 1
}

# starter_stop_running_role <role> <pid> <marker> —— --force：收掉在跑的那个角色（整棵树，先子后父，
# 见 starter_kill_tree），等它自己走完收尾，宽限用尽才强杀——等待与强杀都走收尾那一条
# （starter_stop_wait），不另立一套口径。
#
# 收树而不是只 kill 根：角色那一棵里挂着 node（pnpm dsh）与它的全部子进程，根一死它们就被 init 收养、
# pgrep -P 再也找不到——正是「Ctrl-C 之后还有 node 在跑」的来源（见 scripts/role-daemon.sh 文件头）。
# 先 TERM 是让 role-daemon 自己走清理路径（它的 trap 会收拾 watcher 与监督者的 pidfile）。
# 收不干净就当场拒：两套并跑会静默丢事件，宁可不起（与 start_agent.py 的收树同一个取舍）。
starter_stop_running_role() {
  local role="$1" pid="$2" marker="$3"
  if ! starter_marker_is_role "$pid" "$role"; then
    echo "⚠️ $role 的标记 ${marker} 写着 pid ${pid}，但它已经不是 $role 的 role-daemon.sh" >&2
    echo "   —— 当陈旧标记，不拿它去杀（pid 可能已被复用）；拉起时覆盖那个文件。" >&2
    return 0
  fi
  echo "== --force：收掉在跑着的 $role（pid ${pid}，标记 ${marker}）"
  starter_kill_tree "$pid" TERM
  # starter_stop_wait 读的是收尾设施那一步才赋值的 GRACE_SECONDS，这里按同一把尺子顶上（两者同源：
  # configs/scripts-env.sh 的 KILL_GRACE_SECONDS）。
  GRACE_SECONDS="$KILL_GRACE_SECONDS"
  starter_stop_wait "$pid" "$role"
  # 再看一眼确认真没了：进程退出到被父进程回收之间有个窗口，那期间 `kill -0` 对僵尸仍算活着
  # （旧那套的父进程是**另一个终端里的 starter.sh**，回收时间不由本进程掌握），给一拍再判。
  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    echo "starter: $role（pid ${pid}）收不掉 —— 请手动 kill -9 ${pid} 后重试。" >&2
    echo "         收不干净不启动：两套会抢同一个收件箱。" >&2
    exit 1
  fi
}

# -----------------------------------------------------------------------------
# 清收件箱
# -----------------------------------------------------------------------------

# starter_archive_count <role> —— 本角色归档目录里的事件数：收件侧先归档再投递，这个数是「确实收到过
# 多少封」的判据（投递失败也留在归档里）。清收件箱**不动归档**，所以对 RESET_ON_START_ROLES
# 的角色它是本轮的累计起点（收尾报账读它），对表外角色是跨轮累计。
starter_archive_count() {
  find "$DSH_HOME/rolebus/archive/$1" -name '*.json' 2>/dev/null | wc -l | tr -d ' '
}

# starter_clear_role_inboxes —— 清掉**重开一局的那些角色**收件箱里还没被领走的事件。
#
# 名单取自 configs/agents.py 的 RESET_ON_START_ROLES，与会话归档同一张表（那边挪在 scripts/role-daemon.sh
# 里、本角色拉起之前；表上写着为什么两件事必须同进同退）。表外的角色收件箱原样留着。
# 取不到名单就退出——当成空表处理会静默变成「一个都不清」，而调用方以为自己清了。
#
# 删哪些由收件侧的判据取反决定，不另立一套：deliver() 只处理「不以 `.` 开头且以 .json 结尾」的
# 名字，所以这里只删这两条同时成立的普通文件。逐名 case 而不是 `"$dir"/*.json` 一把梭：后者靠
# 路径展开跳过点开头的名字，而 dotglob 要是从调用者的 BASHOPTS 漏进来（bash 启动时会照它设置
# shell 选项），`.bad-`/`.misroute-` 残留就会被一起删掉。
#
# spool 根不认 $ROLEBUS_SPOOL_ROOT：那个变量只有发件侧的工具认（plugin_tools/rolebus.py 的
# spool_root、scripts/manual/rolebus-emit.sh 的 ROLEBUS_ROOT），角色侧（transport.ts 的
# defaultSpoolRoot → resolveDshHome）一律按 $DSH_HOME 解——在这儿认它，就成了清一个角色根本
# 不读的目录，真正的收件箱原样留着。
#
# 排在组件之前：receiver 一上线就往 spool 投事件，晚于它清就是把启动窗口里收到的信删掉。
starter_clear_role_inboxes() {
  local role dir f name n total=0 line="" reset_roles
  reset_roles="$(cmcc_agents RESET_ON_START_ROLES)" || {
    echo "starter: 取不到 configs/agents.py 的 RESET_ON_START_ROLES（清不清收件箱没有第二个来源）" >&2
    exit 1
  }
  for role in $reset_roles; do
    dir="$DSH_HOME/rolebus/inbox/$role"
    n=0
    if [ -d "$dir" ]; then
      for f in "$dir"/*; do
        name="${f##*/}"
        case "$name" in
          .*)     continue ;;   # .bad-<name> / .misroute-<name> 残留：收件侧留着不再重读
          *.json) ;;            # 收件侧唯一认得的待投递件
          *)      continue ;;   # <event_id>.pending-<pid>-<uuid> 中间态：没写全，收件侧也不读
        esac
        rm -f "$f"
        n=$((n + 1))
      done
    fi
    total=$((total + n))
    line="$line $role=$n"
  done
  echo "== 清角色收件箱（只清 $reset_roles）：只删未领走的事件（保留 .bad-/.misroute-/.pending- 残留与 archive/）"
  if [ "$total" -gt 0 ]; then
    echo "  ${line# } → 共删 ${total} 条"
  else
    echo "  ${line# } → 没有未领走的事件"
  fi
}

# -----------------------------------------------------------------------------
# 步骤 6b/6c：清单里上一轮的收尾（走 task_manager，唯一写者）
# -----------------------------------------------------------------------------

# starter_interrupt_executing_tasks —— 上一轮还停在 executing 的任务标成 interrupted。
#
# 清单（runtime_files/tasks/pending_tasks.yaml）里还停在 executing 的任务，是上一轮角色系统退出
# （被收掉、掉电、Ctrl-C）时没做完的——本轮没有任何人在执行它们。
# 统一标成 interrupted（configs/agents.py 的 TASK_STATUS）。这批因此排在交互器开机自查的最前面：
# 有 interrupted 时它只问这些（挑法在 plugin_tools/task_board.py 的 boot_check_tasks），用户先把
# 「接着做还是算了」定下来，其余待做的等这批清了再问。
# **只改 status**，last_update_time 与其它字段原样不动：这不是模型的一次改动，是系统的收尾动作，
# 时间戳留着才看得出这条任务什么时候被搁下的。
#
# 走 task_manager 而不是直接 import task_board：清单的写者只有这一个（见 plugin_tools/
# task_manager.py 的模块文档），启动收尾也是它的一种改动，不该在 shell 里另开一条写路径。
# 退出码：模块自身失败也返回 0，所以非零只可能是起不来（解释器/模块导入坏了）——那是树坏了，
# 失败要报出来（与取 RESET_ON_START_ROLES 同一口径）。清单损坏时它返回一条 {"error"} 且
# 退出 0：错误行打在 stderr 上，不拦启动——一份读不动的清单不该让整台机器人起不来。
starter_interrupt_executing_tasks() {
  echo "== 启动收尾：上一轮还停在 executing 的任务标成 interrupted（只改 status）"
  if ! PYTHONPATH=$EMBODY python3 -m plugin_tools.task_manager --interrupt-executing >/dev/null; then
    echo "starter: 启动收尾失败（plugin_tools/task_manager.py --interrupt-executing 非零退出）" >&2
    exit 1
  fi
}

# starter_expire_stopped_tasks —— 停了太久的 stopped 任务移入历史存档。
#
# 清单里 status=stopped（用户暂停 / 取消）且 last_update_time 已超过
# configs/configs.py 的 TASK_STOPPED_EXPIRE_HOURS（现 72 小时）的任务，本轮移出清单、按
# task_id 落成同目录的历史存档（plugin_tools/task_board.expire_stopped_tasks）。
# 为什么要清：清单正是交互器开机自查要问的那批（boot_check_tasks 取 pending / stopped），
# 一条几个月前「先不做」的任务会让以后每一轮重启都再问用户一次。
# **只搬不删**：字段一个不改（status 仍是 stopped、时间戳仍是用户搁下它的那一刻），存档照旧能
# 被交互器 task_list 的 task_range=history 读到，任务号也仍被占用（不会重号）。
# 与 6b 同一把锁、同一个唯一写者、同一退出码口径（模块自身失败也返回 0，非零只可能是起不来）。
starter_expire_stopped_tasks() {
  echo "== 启动清理：把停了太久仍停在 stopped 的任务移入历史存档"
  if ! PYTHONPATH=$EMBODY python3 -m plugin_tools.task_manager --expire-stopped >/dev/null; then
    echo "starter: 启动清理失败（plugin_tools/task_manager.py --expire-stopped 非零退出）" >&2
    exit 1
  fi
}

# -----------------------------------------------------------------------------
# 步骤 7：收尾设施（唯一出口，先装好再起东西）
# -----------------------------------------------------------------------------

# starter_kill_tree <pid> [signal] —— 深度优先收掉一棵进程树（先子后父）。顺序是关键：父进程先没掉，
# 它的子进程会被 init 收养，`pgrep -P` 就再也找不到它们——这正是「Ctrl-C 之后还有 node 在跑」
# 的来源（bash 收到 SIGTERM 时不会转给正在跑的 pnpm 管道，见 scripts/role-daemon.sh 的文件头）。
starter_kill_tree() {
  local pid="$1" sig="${2:-TERM}" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    starter_kill_tree "$child" "$sig"
  done
  kill -"$sig" "$pid" 2>/dev/null || true
}

# starter_stop_signal <pid> —— 给根进程 TERM，让它自己走完整收尾流程（不等）。两个根进程（角色与组件）
# 都装了自己的收尾路径，所以**先都发信号**，两边并行收，最后各自等——串着等会白等两个宽限期。
starter_stop_signal() {
  [ -n "$1" ] || return 0
  kill -TERM "$1" 2>/dev/null || true
}

# starter_stop_wait <pid> <名字> —— 等根进程自己退出；宽限用尽才强杀整棵树（先子后父，见 starter_kill_tree）。
starter_stop_wait() {
  local pid="$1" name="$2" waited=0
  [ -n "$pid" ] || return 0
  while [ "$waited" -lt "$GRACE_SECONDS" ]; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
    waited=$((waited + 1))
  done
  echo "   [$name] 收尾窗口用尽 → 强杀整棵树" >&2
  starter_kill_tree "$pid" KILL
}

# starter_cleanup —— 退出路径唯一：正常结束、Ctrl-C、以及中途任何失败都走这里。重复调用被 CLEANED
# 挡住——EXIT 与 INT/TERM 会连着进来两次。
#
# 会话历史的归档不在这里：它挪在**该角色拉起之前**（scripts/role-daemon.sh 的 reset_role_history），
# 由 configs/agents.py 的 RESET_ON_START_ROLES 决定谁清谁留。收尾时挪有两个说不清的地方：归档属于
# 「下一轮从哪儿开始」而不属于「这一轮怎么结束」，且那时角色进程已经死透、判不出「这份会话是不是有
# 活进程在写」（那条判据要靠会话目录里的 flock 租约）。
starter_cleanup() {
  local role pid code any waited
  if [ "$CLEANED" = 1 ]; then return 0; fi
  CLEANED=1
  # 清理期间的第二次 Ctrl-C 不再进来：半清的状态比多跑几秒更难查。
  trap '' INT TERM
  echo
  echo "== 收尾：停 watcher，同时给角色与组件发 TERM（各自最多等 ${GRACE_SECONDS}s，之后强杀）"
  if [ -n "$WATCHER_PID" ]; then
    kill "$WATCHER_PID" 2>/dev/null || true
    wait "$WATCHER_PID" 2>/dev/null || true
  fi
  for role in $ROLES; do
    # role 只可能是 lib-roles.sh 登记过的四个名字之一（未登记的在校验里已经退出），
    # 所以这里的间接赋值不会把外部输入当代码求值。
    eval "pid=\${PID_$role:-}"
    if [ -n "$pid" ]; then starter_kill_tree "$pid" TERM; fi
  done
  # 组件的根进程装着状态观测器与帧生产者的收尾路径（收尾顺序上角色先、组件后，但信号一起发，
  # 两边并行收）。
  starter_stop_signal "$COMPONENTS_PID"
  # 等角色退出：宽限用尽再强杀。
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
      starter_kill_tree "$pid" KILL
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
    echo "   [$role] exit=${code}，归档累计 $(starter_archive_count "$role") 条事件；启动输出 $DSH_HOME/start-$role.out，dsh 输出 $DSH_HOME/daemon-$role.log"
  done
  [ -z "$WATCHER_PID" ] || echo "== 逐轮记录：${WATCH_LOG_FILE}（本次追加，历次启动都在里面）"
  # 组件：TERM 早就发了（上面 starter_stop_signal），这里等它自己收完，用尽宽限才强杀整棵树。
  starter_stop_wait "$COMPONENTS_PID" "组件"
  if [ -n "$COMPONENTS_PID" ]; then
    COMPONENTS_CODE=0
    wait "$COMPONENTS_PID" 2>/dev/null || COMPONENTS_CODE=$?
    echo "   [组件] start_agent.py exit=${COMPONENTS_CODE}；日志 ${COMPONENTS_LOG}"
  fi
  echo "== 结束。"
  return 0
}

# starter_on_signal <退出码> —— 信号路径：先收干净再**就地退出**，不从 trap 返回继续跑——返回的话
# 守望循环会在东西已经收掉之后再转一圈，收尾汇总后面又冒出「组件已退出」。
starter_on_signal() {
  starter_cleanup
  trap - EXIT
  exit "$1"
}

# starter_init_teardown —— 计数器与三处路径先备好，再装 trap：starter_cleanup 读得到
# GRACE_SECONDS / RUNTIME_LOGS_DIR / 各 PID_*，否则 trap 一旦触发就是在 nounset 下读未定义变量。
# （这里失败还没起过任何东西，不需要 cleanup。）
starter_init_teardown() {
  STAGGER="$START_STAGGER"
  CHECK_SECONDS="$START_CHECK_SECONDS"
  GRACE_SECONDS="$KILL_GRACE_SECONDS"
  # 监督器的首轮延迟**不在这里 sleep**：它由监督器自己的定时器执行（dsh-plugins/supervisor-loop
  # 的 firstTickDelayMs，秒数事实源是 configs/configs.py 的 SUPERVISOR_FIRST_TICK_DELAY_SEC，
  # 由 scripts/role-daemon.sh 换成毫秒过桥）。本脚本只负责一件事：**本轮没有观测器可等**时，把那个
  # 延迟顶成 0 —— 那种情况下第一轮没有要等的对象，白等一分钟没有意义：
  #   --roles-only           这条流水根本不起 start_agent.py（观测器在里面）
  #   --no-status-observer   本轮明确不要观测器；此时该报的正是「它真不在」，不该先沉默一分钟
  # 未导出时 role-daemon.sh 才去 configs 取值，所以这里只在两种情况之一才设。
  if [ "$ROLES_ONLY" = 1 ] || [ "$OBSERVER_ON" = 0 ]; then
    SUPERVISOR_FIRST_TICK_DELAY_MS=0
    export SUPERVISOR_FIRST_TICK_DELAY_MS
  fi
  RUNTIME_LOGS_DIR="$(cmcc_path RUNTIME_LOGS_DIR)" || { echo "starter: 取不到 configs/paths.py 的 RUNTIME_LOGS_DIR" >&2; exit 1; }
  COMPONENTS_LOG="$RUNTIME_LOGS_DIR/starter_components.log"

  CLEANED=0
  STATUS=0
  COMPONENTS_PID=""
  WATCHER_PID=""
  PID_interactor=""
  PID_orchestrator=""
  PID_planner=""
  PID_supervisor=""
  trap starter_cleanup EXIT
  trap 'starter_on_signal 130' INT
  trap 'starter_on_signal 143' TERM
}

# -----------------------------------------------------------------------------
# 步骤 8：后台组件
# -----------------------------------------------------------------------------

# starter_start_components —— 起 start_agent.py 的全部组件（含云端收发桥 receiver）。
starter_start_components() {
  if [ "$ROLES_ONLY" = 1 ]; then
    echo "== --roles-only：不起后台组件（不碰硬件），只拉角色系统。"
    return 0
  fi
  mkdir -p "$RUNTIME_LOGS_DIR"
  if [ -f "$COMPONENTS_LOG" ]; then
    mv -f "$COMPONENTS_LOG" "$COMPONENTS_LOG.prev"
  fi
  # 云端下发事件上屏的标记：receiver 每条 robot_agent_task 打的正文就带这个前缀
  # （Communicator/receiver.py 的 _log_cloud_event）。取值只有一处 = configs/configs.py；
  # 取不到就当场拒 —— 空串会让下面那个 grep 匹配**每一行**，把终端刷成组件日志的副本。
  local cloud_tag
  cloud_tag="$(cmcc_conf CLOUD_EVENT_LOG_TAG)" || cloud_tag=""
  [ -n "$cloud_tag" ] || {
    echo "starter: 取不到 configs/configs.py 的 CLOUD_EVENT_LOG_TAG（云端事件上屏没有第二个来源）" >&2
    exit 1
  }
  echo "== 后台起组件：python3 start_agent.py cloud ${AGENT_ARGS[*]:-}"
  echo "   日志 ${COMPONENTS_LOG}（上一轮留 .prev）；状态观测器另有 ${RUNTIME_LOGS_DIR}/status_observer.log"
  echo "   DSH_HOME ${DSH_HOME}（云端事件由组件的 receiver 投到这份 spool，角色系统也从它读）"
  echo "   云端下发给本智能体的事件（${cloud_tag}）另外照打在本终端上（它同时进上面那份日志）"
  # 直接跑 start_agent.py：环境已在上面备齐（它自己不做环境准备），cwd 由入口脚本的 cd 保证是仓库根。
  #
  # 输出的两条腿（`> >(…)`，不是另起一个 tail 进程跟在日志后面）：
  #   · tee —— 组件的每一行照旧整份落 COMPONENTS_LOG（上一轮的 .prev 前面已轮换），watch loop 与
  #     崩溃路径上那些 `tail -n 20 "$COMPONENTS_LOG"` 照读；
  #   · grep —— 只把云端下发那几行挑到本脚本的 stdout 上，也就是终端；它同时跟着本脚本的 stdout
  #     进 <拉起时刻>.log（见 starter_start_terminal_log），所以这一行在终端与两份日志里都在。
  # 为什么用进程替换：python 一退出管道就 EOF，tee 与 grep 自己结束，收尾那一步不必再记一个 pid
  # 来收（starter_cleanup 只管 COMPONENTS_PID）。**$! 拿到的仍然是 python 的 pid**（进程替换是它
  # 的重定向，不是包了一层），所以 kill -0 / starter_stop_wait / starter_kill_tree 全都照旧。
  # grep 不加 `trap '' INT`：Ctrl-C 打到整个前台进程组时它跟着死不要紧 —— 没有东西依赖它。必须
  # 活着的是本脚本自己的那个 stdout tee，理由见 starter_start_terminal_log。
  python3 "$REPO_ROOT/start_agent.py" cloud "${AGENT_ARGS[@]+"${AGENT_ARGS[@]}"}" \
    > >( tee -a "$COMPONENTS_LOG" | grep --line-buffered -F "$cloud_tag" ) 2>&1 &
  COMPONENTS_PID=$!

  # 启动健康检查：这一窗口内进程就没了说明导入/环境有问题（最常见是没 source ROS、或已有另一套
  # agent 在跑被拒），附日志尾巴报出来，不带着半个系统往下走。
  echo "== 等 ${CHECK_SECONDS}s 看组件进程是否活着"
  sleep "$CHECK_SECONDS"
  if ! kill -0 "$COMPONENTS_PID" 2>/dev/null; then
    echo "starter: 组件进程启动即退出 —— 日志尾巴（$COMPONENTS_LOG）：" >&2
    tail -n 20 "$COMPONENTS_LOG" 2>/dev/null | sed 's/^/      /' >&2
    exit 1
  fi
}

# -----------------------------------------------------------------------------
# 步骤 9：物化角色 profile 与 settings
# -----------------------------------------------------------------------------

# starter_materialize_roles —— **串行**做一次。交给各 daemon 并发做的话，多次 `pnpm add` 与同一份
# settings.yaml 的读改写（settings-seed.sh 的 merge 不是原子写）会互相踩。这里先做完，daemon 侧
# 那次就是幂等空转。
starter_materialize_roles() {
  echo "== 物化角色 profiles 到 ${DSH_HOME}（首次启动较慢：每个 profile 要 pnpm add 本地 bundle）"
  if ! DSH_HOME="$DSH_HOME" ROLES="$ROLES" "$EMBODY/scripts/rolebus-setup.sh" | sed 's/^/   /'; then
    echo "starter: 角色 profile 物化失败（见上）——不启动任何角色。" >&2
    exit 1
  fi
  if ! DSH_HOME="$DSH_HOME" "$EMBODY/scripts/settings-seed.sh" | sed 's/^/   /'; then
    echo "starter: settings seed 失败（见上）——不启动任何角色。" >&2
    exit 1
  fi
}

# -----------------------------------------------------------------------------
# 步骤 10：角色系统 banner
# -----------------------------------------------------------------------------

# starter_print_banner —— 启动前把「东西在哪、怎么看、怎么投事件」一次说清：角色起来要几秒到
# 几十秒（tsx 源码启动），用户这会儿就能复制投事件命令到另一个终端。
starter_print_banner() {
  cat <<BANNER

================================================================
 角色系统启动（共享 DSH_HOME=${DSH_HOME}）
   角色         = $ROLES
   spool        = $DSH_HOME/rolebus （角色之间共用它才能互相发事件）
   拦截输出     = $DSH_HOME/start-<role>.out（启动）、daemon-<role>.log（dsh 逐行）
   逐轮记录     = ${WATCH_LOG_FILE}（追加；就是下面本终端打印的那些行）
   本终端输出   = ${TERMINAL_LOG}（本次拉起的一份，运行期间逐行落盘）
 本终端：role-watch 实时打印每轮的 事件 / 思考 / 发言 / 工具 / PTC / 结果。
 另开一个终端投一条事件（整行复制；缺 DSH_HOME 前缀会投到另一个 spool，常驻进程看不到）：
   PYTHONPATH=$EMBODY DSH_HOME=$DSH_HOME python3 -m plugin_tools.rolebus cloud-update \\
     --action update --description "把客厅的灯打开"
 Ctrl-C 退出（角色、它们的子进程与后台组件一起收）。
================================================================

BANNER
}

# -----------------------------------------------------------------------------
# 步骤 11：逐角色后台拉起
# -----------------------------------------------------------------------------

# starter_launch_roles —— 每个角色一个 role-daemon.sh，起完记 pid 与标记文件。
starter_launch_roles() {
  local role boot_log pid
  for role in $ROLES; do
    boot_log="$DSH_HOME/start-$role.out"
    # 三个角色都**立刻**拉起，包括监督器：它的「第一轮读到的还是上一轮的账」那道坎不在这里挡，
    # 而是由它自己的定时器延迟首轮（见 starter_init_teardown 与 Supervisor/resident.patch.yml 的
    # firstTickDelayMs）。此前那段 sleep 排在本次循环里，把启动顺序也绑上了——排在监督器后面的角色
    # （以及 --roles 把 supervisor 写在前面时的全部角色）会被它一起拖住。
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
}

# -----------------------------------------------------------------------------
# 步骤 12：等角色健康
# -----------------------------------------------------------------------------

# starter_check_roles_health —— 判据只是「进程还在不在」；交互器这一窗口里会跑一轮开机自查。
# 一个都没活下来就不起 watcher（它会对着空角色表转），直接走收尾（EXIT trap）。
starter_check_roles_health() {
  local role pid live=0
  echo "== 等 ${CHECK_SECONDS}s 看各角色是否活着（只看进程；开机自查那一轮不影响判活）"
  sleep "$CHECK_SECONDS"
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
    echo "starter: 没有角色活下来，不起来 watcher。" >&2
    exit "$STATUS"
  fi
}

# -----------------------------------------------------------------------------
# 步骤 13：一个覆盖全部角色的 watcher
# -----------------------------------------------------------------------------

# starter_start_watcher —— 起 role-watch.sh（--no-watch 时不挂）。
starter_start_watcher() {
  local role
  if [ "$WATCH" != 1 ]; then
    echo "== --no-watch：不打印记录（本次也不写 ${WATCH_LOG_FILE}），只等角色（Ctrl-C 退出）。"
    return 0
  fi
  # 角色名逐个 --role 传（role-watch.sh 的可重复参数），显式带 --dsh-home —— 它缺省按角色各自取
  # 仓库内目录，与本次的共享 DSH_HOME 不是一处。
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
}

# -----------------------------------------------------------------------------
# 步骤 14：就绪块
# -----------------------------------------------------------------------------

# starter_print_ready —— 上面那些是启动流水，这一段是「现在可以开始用了」的那一刻——请求的角色都已
# 进入待机、watcher 已在盯记录。用不同的框线单独打出来，为的是在一屏流水里一眼找到它；也是除
# --no-watch 之外唯一该看的地方（不用去数 pid）。半就绪也照样打（框线一样醒目）：有角色没起来时
# 沉默或含糊过去，会让人以为整整齐齐都在跑。
starter_print_ready() {
  local role dead ready_head ready_note ready_record dead_list=""
  for role in $ROLES; do
    eval "dead=\${DEAD_$role:-0}"
    [ "$dead" = 0 ] || dead_list="$dead_list $role"
  done
  if [ "$STATUS" -eq 0 ]; then
    ready_head="系统就绪"
    ready_note="全部角色都在待机（常驻启动不发模型请求，等事件唤醒）"
  else
    ready_head="部分就绪"
    ready_note="这些角色没起来：${dead_list# }（日志尾巴在上面；其余角色照常待机）"
  fi
  if [ "$WATCH" = 1 ]; then
    ready_record="${WATCH_LOG_FILE}"
  else
    ready_record="本次未写（--no-watch）"
  fi
  # 先等 watcher 把自己的 banner 打出来（它在后台，谁先抢到终端不一定），就绪块压在最后一行。
  sleep 1
  echo
  echo "################################################################"
  echo "##  ${ready_head}（启动耗时 ${SECONDS}s）：${ready_note}"
  echo "##  记录   = ${ready_record}"
  echo "##  投事件 = PYTHONPATH=$EMBODY DSH_HOME=$DSH_HOME python3 -m plugin_tools.rolebus \\"
  echo "##             cloud-update --action update --description \"…\""
  echo "##  停止   = 本终端 Ctrl-C（角色、它们的子进程与后台组件一起收）"
  echo "################################################################"
  echo
}

# -----------------------------------------------------------------------------
# 步骤 15：守望
# -----------------------------------------------------------------------------

# starter_watch_loop —— 任一侧死掉就收整套。不用 `wait -n`：本机脚本其余部分同样只用到 bash 3.2 的能力。
starter_watch_loop() {
  local role pid live reported
  while :; do
    if [ -n "$COMPONENTS_PID" ] && ! kill -0 "$COMPONENTS_PID" 2>/dev/null; then
      echo "starter: 后台组件进程已退出（角色没有观测源了）→ 收掉角色系统" >&2
      echo "         日志尾巴（$COMPONENTS_LOG）：" >&2
      tail -n 20 "$COMPONENTS_LOG" 2>/dev/null | sed 's/^/      /' >&2
      STATUS=1
      break
    fi
    if [ -n "$WATCHER_PID" ] && ! kill -0 "$WATCHER_PID" 2>/dev/null; then
      echo "starter: role-watch 已退出，收掉整套角色。" >&2
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
      echo "starter: 全部角色已退出。" >&2
      break
    fi
    sleep 1
  done
}
