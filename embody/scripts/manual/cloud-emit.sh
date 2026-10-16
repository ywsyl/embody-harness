#!/usr/bin/env bash
# =============================================================================
# cloud-emit.sh —— 假装云端下发一条任务事件，投给 starter.sh 起的角色系统
#
# 这是**云端到达路径**的手工扳机：角色系统收云端事件走的是共享 spool 里的信封
# （<spool>/inbox/<target>/<event_id>.json，收件角色每 pollIntervalMs 扫一次，规则见
# dsh-plugins/rolebus/src/transport.ts）。云端侧把它写下来的那一步不在本仓；本仓里写这条路径的
# 只有 plugin_tools/rolebus.py（本脚本调它，不重造这段）。本脚本只补三件命令行手工投递容易做错的事：
#   1) 定 DSH_HOME —— 投到另一份 spool 是**静默失败**（文件写下去了，没有任何进程会看到），
#      所以缺省从**在跑的角色进程**的环境里认，而不是按自己 shell 的环境变量猜；
#   2) 选版本 —— 云端事件分属两个并存版本（分立版 cloud_update → interactor，
#      合并版 planner_cloud_update → planner，形状事实源 dsh-plugins/rolebus/src/event-catalog.ts），
#      两个版本不同时启动，所以缺省按「哪个角色在跑」判（判据在 python 侧：rolebus variant）；
#   3) 查收件人 —— 目标角色没在跑时，事件只是躺在收件箱里、没人当场认领；本脚本把这算作失败
#      （退出码 1，与 Communicator/cloud_event_sim.py 的「发出去了但没人订阅」同一约定）。
#
# 信封字段、action 值域、event_id / task_id 的生成规则全在 plugin_tools/rolebus.py（本脚本不抄
# 第二份：`CLOUD_EVENTS` 与动作值域 `CLOUD_ACTIONS` 在那里）；通用的、非云端的发件走
# scripts/manual/rolebus-emit.sh。
#
# 这里发的是**端侧词**：本脚本直接投 spool，不经 Communicator/receiver.py 的归一，而端侧桥只认
# 两个动作——两条动作走同一条云事件，随 --kind 定死（content → update，status → stop）。云端那套
# create / modify / interrupt / answer / info 是**云端词**，由 receiver 映射；要走那条真链路
# 用 Communicator/cloud_event_sim.py。
#
# 投内容类（update）会**真的让机器人开始干活**（规划 → 导航 → 动作 → 播报），不是空转；控制类
# （stop）会真的停下那件事（交互器把该任务置 `stopped`，再叫醒编排器收场）。同一句描述投两次是
# 两条事件、两个任务（event_id 不同），要重发就先想清楚现场。
#
# 用法：
#   scripts/manual/cloud-emit.sh "去517找小王，让他来518开会"                    # 内容类 update（→ interactor）
#   scripts/manual/cloud-emit.sh "用户叫停" --kind status                        # 控制类 stop（→ interactor）
#   scripts/manual/cloud-emit.sh "去517找小王" --planner                         # 合并版角色（→ planner）
#   scripts/manual/cloud-emit.sh --show                                         # 只看落点与各角色在不在跑
#
# 选项：
#   --kind <k>         content（缺省）= 改写下发任务内容 → action=update；
#                      status = 停下当前任务 → action=stop
#   --task-id <id>     任务号；缺省由 python 侧按 cloud-<Unix 秒> 占位
#   --planner          投合并版事件（目标 planner）；与 --split 互斥
#   --split            投分立版事件（目标 interactor）
#   --dsh-home <目录>  目标系统的 DSH_HOME（rolebus 共享 spool 是它的子目录 rolebus/）
#   --show             只打印解析结果，不投事件
#
# 缺省落点（不给 --dsh-home 时）：从在跑的角色进程环境里认 DSH_HOME；认不到才退回 $DSH_HOME，
# 再退回 ~/.dsh（后两级与 plugin_tools/rolebus.py 的 dsh_home() 同一口径）。
#
# 退出码：0 已投且收件角色在跑（--show 也是 0）；1 已投但收件箱那头没人（事件留在收件箱）；
#         2 用法错误；3 环境或依赖问题（DSH_HOME 认不出唯一一处 / python 侧拒绝投递）。
# =============================================================================

set -euo pipefail

# 本脚本在 scripts/manual/ 下，仓库根是上两级（scripts/ 下的自动路径脚本才是上一级）。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# --show 列出的角色（含 supervisor：它不收云端事件、由定时器唤醒，但它在跑就说明整套系统起着）。
ALL_ROLES="interactor orchestrator supervisor planner"

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# python 侧按「仓库根」找 plugin_tools / configs，故在子 shell 里 cd 过去，不动调用者的 cwd。
# DSH_HOME 显式传：本脚本可能刚从在跑的角色进程里认出它，而调用者的环境里没有。
rolebus() {
  ( cd "$REPO_ROOT" && PYTHONPATH="$REPO_ROOT" DSH_HOME="$DSH_HOME" python3 -m plugin_tools.rolebus "$@" )
}

# role_running <role> —— 与 starter.sh 的启动守卫同一口径读标记：$DSH_HOME/<role>.pid
# 的内容是活 pid 才算在跑（内容不是数字当陈旧标记）。supervisor 的标记由 role-daemon.sh 写。
role_running() {
  local marker pid
  marker="$DSH_HOME/$1.pid"
  [ -f "$marker" ] || return 1
  pid="$(cat "$marker" 2>/dev/null || true)"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null
}

# 在跑的角色进程各自把 DSH_HOME 记在 /proc/<pid>/environ 里（starter.sh export 给
# 所有子进程，role-daemon.sh 起的角色继承），所以那一份就是「这套系统实际在用哪个 spool」的
# 权威说法。本树里角色只有 role-daemon.sh 这一条启动路径，所以按它认就够。pgrep 的模式写成
# role-dae[m]on：不让本脚本自己的命令行被模式匹配到。
discover_dsh_homes() {
  local pid home
  DISCOVERED=""
  for pid in $(pgrep -f "role-dae[m]on.sh" 2>/dev/null || true); do
    [ -r "/proc/$pid/environ" ] || continue
    # 读不到（进程刚退出）就跳过这一个：赋值失败由 || continue 接住，不让 set -e 把整脚本带走。
    home="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^DSH_HOME=//p' | tail -n 1)" || continue
    [ -n "$home" ] || continue
    case " $DISCOVERED " in
      *" $home "*) ;;
      *) DISCOVERED="${DISCOVERED}${DISCOVERED:+ }$home" ;;
    esac
  done
}

DESCRIPTION=""
KIND="content"
TASK_ID=""
VARIANT=""
DSH_HOME_ARG=""
SHOW=0

while [ $# -gt 0 ]; do
  case "$1" in
    --kind)     [ -n "${2:-}" ] || { echo "cloud-emit: --kind 需要 content 或 status" >&2; exit 2; }
                KIND="$2"; shift 2 ;;
    --task-id)  [ -n "${2:-}" ] || { echo "cloud-emit: --task-id 需要一个任务号" >&2; exit 2; }
                TASK_ID="$2"; shift 2 ;;
    --dsh-home) [ -n "${2:-}" ] || { echo "cloud-emit: --dsh-home 需要一个目录" >&2; exit 2; }
                DSH_HOME_ARG="$2"; shift 2 ;;
    --planner)  [ -z "$VARIANT" ] || { echo "cloud-emit: --planner / --split 只能给一个" >&2; exit 2; }
                VARIANT="planner"; shift ;;
    --split)    [ -z "$VARIANT" ] || { echo "cloud-emit: --planner / --split 只能给一个" >&2; exit 2; }
                VARIANT="split"; shift ;;
    --show)     SHOW=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    --*)        echo "cloud-emit: 未知参数 $1（见 --help）" >&2; exit 2 ;;
    *)
      [ -z "$DESCRIPTION" ] || { echo "cloud-emit: 描述只给一个（第二个是 $1）——要发两条就投两次" >&2; exit 2; }
      DESCRIPTION="$1"; shift ;;
  esac
done

case "$KIND" in
  content|status) ;;
  *) echo "cloud-emit: --kind 只能是 content 或 status，收到 $KIND" >&2; exit 2 ;;
esac
# 动作不再是可选项：两条动作由同一条云事件承载，随 --kind 定死（与 python 侧的事件目录一一对应）。
case "$KIND" in
  content) ACTION="update" ;;
  status)  ACTION="stop" ;;
esac
if [ "$SHOW" != 1 ] && [ -z "$DESCRIPTION" ]; then
  echo "cloud-emit: 缺描述（云端下发的那句话；只查落点用 --show）" >&2; exit 2
fi

# ---- 1. DSH_HOME（= 投到哪一套系统）----
if [ -n "$DSH_HOME_ARG" ]; then
  DSH_HOME="$DSH_HOME_ARG"
  DSH_HOME_SRC="--dsh-home 指定"
else
  discover_dsh_homes
  # shellcheck disable=SC2086  # 有意按空格拆开数个数（里面是本脚本自己拼的目录名，不含空格）
  set -- $DISCOVERED
  if [ $# -eq 1 ]; then
    DSH_HOME="$1"
    DSH_HOME_SRC="从在跑的角色进程认出"
  elif [ $# -gt 1 ]; then
    echo "cloud-emit: 有 $# 套角色系统在跑，各自的 DSH_HOME 是：" >&2
    for d in "$@"; do echo "             $d" >&2; done
    echo "           投给哪一套得明说：--dsh-home <目录>（见 --show）" >&2
    exit 3
  else
    DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
    DSH_HOME_SRC="没角色在跑——按环境变量/缺省取，可能不是实际那套"
  fi
fi
# ROLEBUS_SPOOL_ROOT 优先于 DSH_HOME（与 spool_root() 同一约定），落点以 python 侧解析为准。
SPOOL="$(rolebus spool-root)" || { echo "cloud-emit: 取 spool 根失败（见上）" >&2; exit 3; }

# ---- 2. 版本与目标角色 ----
# 判据不留第二份：问 python 侧（plugin_tools/rolebus.py 的 cloud_variant —— 看哪个版本的
# 角色在跑，判不出来时按分立版）。它读的是 $DSH_HOME 下的角色标记，所以得等第 1 步把
# DSH_HOME 定下来再问（同上，python 子进程里显式带 DSH_HOME）。
if [ -z "$VARIANT" ]; then
  VARIANT="$(rolebus variant)" || { echo "cloud-emit: 取版本失败（见上）" >&2; exit 3; }
fi
# 两条端侧动作由同一条云事件承载，所以事件名只剩版本一维（动作仍由 --kind 定）。
case "$VARIANT" in
  split)   SUB="cloud-update";         TARGET="interactor" ;;
  planner) SUB="planner-cloud-update"; TARGET="planner" ;;
  *) echo "cloud-emit: 内部错误：$VARIANT 没有对应事件" >&2; exit 3 ;;
esac

# ---- 3. --show：只报落点与收件人状态 ----
if [ "$SHOW" = 1 ]; then
  echo "DSH_HOME    $DSH_HOME（$DSH_HOME_SRC）"
  echo "spool       $SPOOL"
  echo "角色          在跑?            已认领的归档"
  for role in $ALL_ROLES; do
    if role_running "$role"; then
      pid="$(cat "$DSH_HOME/$role.pid" 2>/dev/null || true)"
      state="在跑（pid ${pid}）"
    else
      state="没在跑"
    fi
    # 归档目录要角色认领过第一条事件才会出现，所以缺目录是常态（不是错误）。
    count=0
    if [ -d "$SPOOL/archive/$role" ]; then
      count="$(find "$SPOOL/archive/$role" -maxdepth 1 -name '*.json' | wc -l)"
    fi
    printf '  %-12s %-16s %s\n' "$role" "$state" "$count"
  done
  echo "本次会投    $SUB → $TARGET（$VARIANT 版，--kind $KIND）"
  [ -n "$DESCRIPTION" ] && echo "            action=$ACTION 描述=${DESCRIPTION}"
  exit 0
fi

# ---- 4. 投递（信封由 python 侧写：先 .pending-* 再原子 rename）----
echo "== 投 $SUB → $TARGET（$VARIANT 版，action=$ACTION）"
# 落点先亮出来：投错 spool 是静默失败，唯一能当场看出来的机会就是这一行。
echo "   DSH_HOME $DSH_HOME（$DSH_HOME_SRC）；spool $SPOOL"
case "$ACTION" in
  update)
    echo "   注意：update 会让机器人真的开始干活（规划 → 导航 → 动作 → 播报），不是空转。" ;;
esac
PY_ARGS=("$SUB" --action "$ACTION" --description "$DESCRIPTION")
if [ -n "$TASK_ID" ]; then PY_ARGS+=(--task-id "$TASK_ID"); fi
# python 侧的报错原样打出来（action 词表、信封字段冲突等都由它判），退出码归一律算 3。
rolebus "${PY_ARGS[@]}" || { echo "cloud-emit: python 侧投递被拒（原因见上）" >&2; exit 3; }

# ---- 5. 收件人在不在：不在就当场说清楚（事件不会自己消失，但也没人当场认领）----
if role_running "$TARGET"; then
  echo "   $TARGET 在跑（$DSH_HOME/$TARGET.pid），事件会被它扫到并认领。"
  exit 0
fi
echo "cloud-emit: $TARGET 没在跑（$DSH_HOME/$TARGET.pid 没有活进程）——事件已写进收件箱，但在它起来" >&2
echo "           之前没人认领；这就是「投到了别的 spool / 系统没起」的样子。" >&2
echo "           落点 $SPOOL/inbox/$TARGET；要撤回就删掉刚写的那份 .json（上面信封里有 event_id）。" >&2
exit 1
