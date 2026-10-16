#!/bin/bash
# 机器狗运动控制命令 - 直接 ROS 2 发布版本
# 用法: ./motion_direct.sh <command> <speed> <duration>
# 示例: ./motion_direct.sh turnright 1.2 0.8

# 加载 ROS 2 环境（路径与参数取自 configs/，唯一事实源）。
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"
ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
    echo "[MOTION] ❌ 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
source "$ROS_SETUP"

# source 运行中的安装（经 /usr/bin/cmcc_robot/install 符号链接取）；别写
# /root/cmcc_robot/setup.bash，那是 8/3 的陈旧副本。
# 本脚本保留 CMCC_SETUP 变量而不是用 cmcc_require_setup：下面 3 个 bash -c 子 shell
# 要把这个路径**当字符串插值**进去，所以需要变量本身。
CMCC_SETUP="$(cmcc_path CMCC_ROBOT_SETUP)" || {
    echo "[MOTION] ❌ 取不到 configs/paths.py 的 CMCC_ROBOT_SETUP" >&2; exit 1; }
source "$CMCC_SETUP"

RMW="$(cmcc_conf RMW_IMPLEMENTATION)"
DOMAIN_ID="$(cmcc_conf ROS_DOMAIN_ID)"
export RMW_IMPLEMENTATION="$RMW"
export ROS_DOMAIN_ID="$DOMAIN_ID"

COMMAND="${1:-}"
# 默认速度 0.5 与 configs.MOVE_LINEAR_SPEED(0.6) 不同，是**有意保留**的：本脚本是手动
# 调试入口，默认慢一点好观察；走工具层下发的运动用 MOVE_LINEAR_SPEED。改这里等于改
# 调试手感，别为了「统一」把两者并掉。
SPEED="${2:-0.5}"
DURATION="${3:-1.0}"

if [[ -z "$COMMAND" ]]; then
    echo "用法: motion_direct.sh <命令> [速度] [持续时间]"
    echo "命令: forward, backward, left, right, turnleft, turnright, stop"
    exit 1
fi

# 转换命令为 Twist 消息
case "$COMMAND" in
    forward)
        LINEAR_X="$SPEED"
        LINEAR_Y="0"
        ANGULAR_Z="0"
        ;;
    backward)
        LINEAR_X="-$SPEED"
        LINEAR_Y="0"
        ANGULAR_Z="0"
        ;;
    left)
        LINEAR_X="0"
        LINEAR_Y="$SPEED"
        ANGULAR_Z="0"
        ;;
    right)
        LINEAR_X="0"
        LINEAR_Y="-$SPEED"
        ANGULAR_Z="0"
        ;;
    turnleft)
        LINEAR_X="0"
        LINEAR_Y="0"
        ANGULAR_Z="$SPEED"
        ;;
    turnright)
        LINEAR_X="0"
        LINEAR_Y="0"
        ANGULAR_Z="-$SPEED"
        ;;
    stop)
        LINEAR_X="0"
        LINEAR_Y="0"
        ANGULAR_Z="0"
        ;;
    *)
        echo "❌ 未知命令: $COMMAND"
        exit 1
        ;;
esac

echo "📤 直接发布命令: $COMMAND, 速度: $SPEED, 时长: ${DURATION}秒"

# 发布速度指令
if [[ "$COMMAND" == "stop" ]]; then
    # 停止命令只发布一次
    bash -c "source $ROS_SETUP && source $CMCC_SETUP && export RMW_IMPLEMENTATION=$RMW && export ROS_DOMAIN_ID=$DOMAIN_ID && ros2 topic pub --once /catch_turtle/ctrl_instruct geometry_msgs/msg/Twist \"{linear: {x: 0.0, y: 0.0, z: 0.0}, angular: {x: 0.0, y: 0.0, z: 0.0}}\" 2>&1"
    echo "✅ 已停止"
else
    # 计算发布次数 (10Hz * 时长)
    COUNT=$(python3 -c "print(int($DURATION * 10))")
    if [[ $COUNT -lt 1 ]]; then
        COUNT=1
    fi
    
    # 使用 --times 参数发布多次
    bash -c "source $ROS_SETUP && source $CMCC_SETUP && export RMW_IMPLEMENTATION=$RMW && export ROS_DOMAIN_ID=$DOMAIN_ID && ros2 topic pub --times $COUNT /catch_turtle/ctrl_instruct geometry_msgs/msg/Twist \"{linear: {x: $LINEAR_X, y: $LINEAR_Y, z: 0.0}, angular: {x: 0.0, y: 0.0, z: $ANGULAR_Z}}\" 2>&1"
    
    # 发送停止指令
    sleep 0.1
    bash -c "source $ROS_SETUP && source $CMCC_SETUP && export RMW_IMPLEMENTATION=$RMW && export ROS_DOMAIN_ID=$DOMAIN_ID && ros2 topic pub --once /catch_turtle/ctrl_instruct geometry_msgs/msg/Twist \"{linear: {x: 0.0, y: 0.0, z: 0.0}, angular: {x: 0.0, y: 0.0, z: 0.0}}\" 2>&1"
    echo "✅ 完成 $COMMAND 动作"
fi
