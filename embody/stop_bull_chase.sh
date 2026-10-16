#!/usr/bin/env bash
# =============================================================================
# stop_bull_chase.sh —— 一键停止斗牛：向 /bullfight_ctl 下发一条结束信令（status=01）
#
# 只是 skills/bull-chase/scripts/bull_chase.py --stop 的 shell 包装，负责备齐环境；
# 信令的构造、等待算法订阅者、发送后的落地点全在那个模块里（本脚本不抄第二份话题名与 payload）。
# 它**不是启动入口**（根目录的入口仍只有 starter.sh / start_agent.py）：本脚本不碰组件、
# 不碰角色系统，随时可跑，也不要求它们正在跑。
#
# 收场结论不在本脚本里：算法收到 01 会回报 08（端侧收到结束信令），正在跑的那一场 bull_chase
# 订阅到 08 后以「08：用户下发结束指令」返回。所以要确认真的停了，看那个工具调用的结果。
#
# 环境口径与 starter.sh 一致（ROS 三件套 + DDS 三项，全经 configs/ 取值，不在这里写死路径）：
# setup.bash 含未加保护的变量引用，nounset 下 source 会失败，故临时 set +u。
# ROS_DOMAIN_ID 环境优先——想空跑验证（不发到在跑的那套系统上）用：
#   ROS_DOMAIN_ID=99 ./stop_bull_chase.sh
#
# 用法：
#   ./stop_bull_chase.sh                 # 停斗牛（SN 取 BULL_CHASE_SN / 设备配置 devSn）
#   ./stop_bull_chase.sh --sn 12345      # 指定 SN（其余参数原样透传给技能模块）
# =============================================================================
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=configs/lib-shell-paths.sh
source "$REPO_ROOT/configs/lib-shell-paths.sh"   # cmcc_path / cmcc_conf / cmcc_require_setup

set +u
ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
    echo "[bull-chase-stop] ❌ 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
source "$ROS_SETUP"
UNITREE_SETUP="$(cmcc_path UNITREE_ROS_SETUP)" || {
    echo "[bull-chase-stop] ❌ 取不到 configs/paths.py 的 UNITREE_ROS_SETUP" >&2; exit 1; }
source "$UNITREE_SETUP"
# 运行中的机器人安装（不是 /root/cmcc_robot 那份陈旧副本），路径见 configs/paths.py
cmcc_require_setup "[bull-chase-stop]" || exit 1
set -u

export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"
export CYCLONEDDS_URI="$(cmcc_conf CYCLONEDDS_URI)"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$(cmcc_conf ROS_DOMAIN_ID)}"

# --stop 恒在：本脚本永远只走停止路径，误传参数也不会变成「跑一整场斗牛」
exec python3 "$REPO_ROOT/skills/bull-chase/scripts/bull_chase.py" --stop "$@"
