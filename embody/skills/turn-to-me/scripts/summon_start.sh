#!/bin/bash
# 开启声源定位功能
# 使用方法: ./summon_start.sh

# ROS 环境取自 configs/（唯一事实源）
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"
ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
    echo "[SUMMON] ❌ 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
source "$ROS_SETUP"

# source 运行中的安装（经 /usr/bin/cmcc_robot/install 符号链接取）；
# 别写 /root/cmcc_robot/setup.bash，那是 8/3 的陈旧副本。
cmcc_require_setup "[SUMMON]" || exit 1

export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"
export ROS_DOMAIN_ID="$(cmcc_conf ROS_DOMAIN_ID)"

echo "🎯 正在开启声源定位..."

# 直接发布到 /follow_me/start topic
ros2 topic pub /follow_me/start std_msgs/msg/String "data: 'start'" --once

echo "✅ 声源定位已开启！机器狗将转向声源位置"
