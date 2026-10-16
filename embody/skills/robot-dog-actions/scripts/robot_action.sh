#!/bin/bash
# 机器狗动作控制脚本
#
# 接口（《机器人功能接口规格.md》第 4 节「动作执行」）：
#   Topic   /catch_turtle/action_type
#   Message homi_speech_interface/msg/RobdogAction（三个 string 字段）
#     actiontype     = motorSkill（默认，走马达技能通道）/ followMe / sportMode / robot_move / move_points
#     actionargument = 动作名（motorSkill 通道），取值见下方清单；其余通道为各自参数
#     sourcetype     = local-agent（本机智能体发起）
#
# 用法: ./robot_action.sh <动作名> [通道]     # 通道缺省 motorSkill；工具层只传动作名

# 初始化 ROS 2 环境（路径与参数取自 configs/，唯一事实源）
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"
ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
    echo "[ACTION] ❌ 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
source "$ROS_SETUP"
UNITREE_SETUP="$(cmcc_path UNITREE_ROS_SETUP)" || {
    echo "[ACTION] ❌ 取不到 configs/paths.py 的 UNITREE_ROS_SETUP" >&2; exit 1; }
source "$UNITREE_SETUP"

# 机器人安装路径经 configs/paths.py 取（唯一事实源），不写具体安装目录：必须用
# **运行中的安装**，RobdogAction 的消息定义与 libw_esr.so 都以运行版为准。
# （/root/cmcc_robot 是 8/3 的陈旧副本，接口定义与运行版不同，库也不是同一份。）
cmcc_require_setup "[ACTION]" || exit 1
CMCC_LIB="$(cmcc_path CMCC_ROBOT_LIB_DIR)" || {
    echo "[ACTION] ❌ 取不到 configs/paths.py 的 CMCC_ROBOT_LIB_DIR" >&2; exit 1; }

export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"
export CYCLONEDDS_URI="$(cmcc_conf CYCLONEDDS_URI)"
export ROS_DOMAIN_ID="$(cmcc_conf ROS_DOMAIN_ID)"

# 添加 libw_esr.so 库路径（运行版自带，随安装走）
export LD_LIBRARY_PATH="$CMCC_LIB:$LD_LIBRARY_PATH"

ACTION=$1
# 第二个位置参数 = 动作通道，缺省 motorSkill（工具层只传动作名，兼容不变）
ACTIONTYPE="${2:-motorSkill}"

if [ -z "$ACTION" ]; then
    echo "用法: $0 <动作名> [通道]"
    echo "  通道缺省 motorSkill；其余通道的动作名不受动作清单约束。"
    echo "  例: $0 dance"
    echo "      $0 on followMe      # 开跟随（未实测，见 SKILL.md）"
    echo ""
    echo "🎵 舞蹈表演:"
    echo "  dance      - 跳舞"
    echo "  danceV2    - 舞蹈2"
    echo "  happy      - 开心"
    echo "  stretch    - 伸懒腰"
    echo "  twistBody  - 扭身"
    echo "  twistAss   - 扭屁股"
    echo "  shakeBody  - 抖身（快抖）"
    echo ""
    echo "🐕 基础动作:"
    echo "  standUp    - 站立"
    echo "  sitDown    - 坐下"
    echo "  getDown    - 趴下"
    echo "  makeBow    - 鞠躬"
    echo ""
    echo "👋 互动动作:"
    echo "  greeting   - 打招呼"
    echo "  handShake  - 握手"
    echo "  fingerHeart - 比心"
    echo "  newYearCall - 拜年"
    echo "  leap       - 扑人"
    echo "  jumpForward - 前跳"
    echo ""
    echo "🤸 特技动作:"
    echo "  walkUpsideDown - 倒立行走"
    echo "  standErect - 直立站立"
    echo "  backflip   - 后空翻"
    echo "  turnOver   - 翻滚"
    echo "  twistJump  - 扭身跳"
    exit 1
fi

# 调用来源落在 RobdogAction 的 sourcetype 字段上。端上既有取值是
# speech_offline / speech_online / app / device（语音、APP、设备自身发起）；
# 本机智能体发起的一律标 local-agent，该取值已写进规格文档第 4 节。
# 调用方可用 ACTION_CALLER 覆盖。
SOURCETYPE="${ACTION_CALLER:-local-agent}"

echo "🐕 准备执行动作: $ACTION"

PUB_OUT=$(ros2 topic pub --once /catch_turtle/action_type \
    homi_speech_interface/msg/RobdogAction \
    "{actiontype: '${ACTIONTYPE}', actionargument: '${ACTION}', sourcetype: '${SOURCETYPE}'}" 2>&1)
PUB_RC=$?

if [ $PUB_RC -ne 0 ]; then
    echo "❌ 动作指令下发失败 (code=$PUB_RC): $ACTION" >&2
    echo "$PUB_OUT" >&2
    exit 1
fi

echo "✅ 动作指令已发送: $ACTION"
