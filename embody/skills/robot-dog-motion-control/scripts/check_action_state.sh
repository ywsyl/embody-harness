#!/bin/bash
# 动作状态查询（是否站立）—— check_action_state.py 的环境包装
#
# 用法:
#   ./check_action_state.sh                  # 读一帧当前状态
#   ./check_action_state.sh --wait-standing  # 轮询等到静止站立(action_state=2)
#   ./check_action_state.sh --timeout 5      # 自定义等待上限
#   ./check_action_state.sh --wait-state 2 --consecutive 2   # 连续 2 帧都是 2 才算等到
#
# 退出码见 check_action_state.py 顶部注释（0=站立 / 1=非站立 / 2=超时 / 3=环境错误）。
# 注意：Motion_Manager 的动作判据读的是**进程内那份常驻订阅**（motion_status.py，按时间保持
# configs.MOTION_IDLE_HOLD_SEC 秒静止），不再起本脚本 —— 本脚本启动到收到第一帧要 2~3s，
# 比短动作本身还长。等待类模式现在只给人手动排查用；开机清理与站立闸门仍用它的读一帧模式。

# ROS 2 环境与运行中的安装：全部取自 configs/（唯一事实源）。
# 这里原先直接写 /usr/bin/cmcc_robot/install/setup.bash —— 值虽然是对的（它确实指向
# 正在运行的安装，不是 /root/cmcc_robot 那棵 8/3 陈旧副本），但绕过了 configs/paths.py，
# 换安装时这一处不会跟着变。CMCC_ROBOT_SETUP 经该符号链接取，值等价但可换。
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"
ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
    echo "[MOTION] ❌ 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
source "$ROS_SETUP"
UNITREE_SETUP="$(cmcc_path UNITREE_ROS_SETUP)" || {
    echo "[MOTION] ❌ 取不到 configs/paths.py 的 UNITREE_ROS_SETUP" >&2; exit 1; }
source "$UNITREE_SETUP"
cmcc_require_setup "[MOTION]" || exit 1

export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"
export CYCLONEDDS_URI="$(cmcc_conf CYCLONEDDS_URI)"
export ROS_DOMAIN_ID="$(cmcc_conf ROS_DOMAIN_ID)"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

exec python3 "$SCRIPT_DIR/check_action_state.py" "$@"
