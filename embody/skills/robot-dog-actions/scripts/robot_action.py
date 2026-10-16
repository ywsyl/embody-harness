#!/usr/bin/env python3
"""
机器狗动作控制脚本 (Python版本)

接口（《机器人功能接口规格.md》第 4 节「动作执行」）：
  Topic   /catch_turtle/action_type
  Message homi_speech_interface/msg/RobdogAction
    actiontype     = motorSkill（默认）/ followMe / sportMode / robot_move / move_points
    actionargument = 动作名（motorSkill 通道）、通道参数（其余通道）
    sourcetype     = local-agent（本机智能体发起）

用法: python3 robot_action.py <动作名> [通道]     # 通道缺省 motorSkill

需要先 source 正在运行的安装（/usr/bin/cmcc_robot/install/setup.bash），
RobdogAction 的定义以运行版为准。
"""

import sys
import os
from pathlib import Path

# 仓库根（本文件 skills/robot-dog-actions/scripts 上三级）入 sys.path，以便 import configs
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 设置 ROS 2 环境。这三个值决定「跟谁通信」，唯一来源是 configs/configs.py ——
# 原先这里把 RMW_IMPLEMENTATION / CYCLONEDDS_URI 逐字抄了一份，还漏了
# ROS_DOMAIN_ID（全靠调用方的 .sh 兜着）。domain 或 DDS 实现不对时，节点照样
# 起得来但收不到任何话题，表现为静默无数据，所以不留第二份拷贝。
from configs.configs import (
    CYCLONEDDS_URI as _CYCLONEDDS_URI,
    RMW_IMPLEMENTATION as _RMW_IMPLEMENTATION,
    ROS_DOMAIN_ID as _ROS_DOMAIN_ID,
)
from configs.paths import (
    CMCC_ROBOT_SETUP as _CMCC_ROBOT_SETUP,
    ROS_SETUP as _ROS_SETUP,
    UNITREE_ROS_SETUP as _UNITREE_ROS_SETUP,
)

os.environ['RMW_IMPLEMENTATION'] = _RMW_IMPLEMENTATION
os.environ['CYCLONEDDS_URI'] = _CYCLONEDDS_URI
# 一律用 setdefault：调用方（.sh 包装）若已指定 domain，以调用方为准、不覆盖；
# 只有谁都没设时才落到 configs 的默认值。上面两个沿用原有的覆盖式赋值，不改行为。
os.environ.setdefault('ROS_DOMAIN_ID', str(_ROS_DOMAIN_ID))

try:
    import rclpy
    from rclpy.node import Node
    from homi_speech_interface.msg import RobdogAction
except ImportError as e:
    print(f"❌ 错误: 无法导入 ROS 2 模块: {e}")
    print("请确保已初始化 ROS 2 环境:")
    for _setup in (_ROS_SETUP, _UNITREE_ROS_SETUP, _CMCC_ROBOT_SETUP):
        print(f"  source {_setup}")
    sys.exit(1)


# 固定的动作通道标识，与规格文档第 4 节的 actiontype 取值一致
ACTION_TYPE = "motorSkill"

# robdog_control_node 的 actiontype 分派取值（该话题唯一消费方，见 SKILL.md「消费方」）。
# 本脚本默认走 motorSkill；其余通道经第二个位置参数显式指定，动作名不再受下方白名单约束
# （各通道的 actionargument 取值体系不同，不能拿动作名去比）。
ACTION_CHANNELS = ('motorSkill', 'followMe', 'sportMode', 'robot_move', 'move_points')

# 与 skills/robot-dog-actions/SKILL.md 的动作清单、tool_executor.py 的 _VALID_ACTIONS 保持一致
# ——唯一例外是清单末尾的 emergencyStop，它有意只存在于本脚本（理由见该行注释）。
# 前 14 个是规格文档第 4 节原有的；后 8 个（handShake 起）是 2026-09-17 从端侧补全的——
# 依据是 speech_core 的离线语音配置 offline_asr.yaml 与 robdog_control_node 二进制里的技能名，
# 两条线索都命中（见 SKILL.md「动作清单的来源」），但尚未逐条实发验证。
VALID_ACTIONS = [
    'dance', 'danceV2', 'happy', 'stretch',
    'standUp', 'sitDown', 'getDown',
    'greeting', 'fingerHeart', 'newYearCall',
    'leap', 'jumpForward', 'walkUpsideDown', 'standErect',
    # —— 2026-09-17 补全（离线语音指令键 → 动作名）——
    'handShake',    # handShake
    'shakeBody',    # shakeit
    'twistAss',     # twistedhisbuttoc（"twist his butt"）
    'makeBow',      # compilation
    'backflip',     # backflip
    'turnOver',     # roll
    'twistJump',    # twistjump
    'twistBody',    # twistbody
    # —— 急停：不属于可选的表演动作，只供 Motion_Manager 的 stop(emergency=True) 调用 ——
    # 规格第 4 节的急停一栏（actiontype=motorSkill，actionargument 留空）。
    # 刻意**不**同步进 tool_executor.py 的 _VALID_ACTIONS，也不进 available_tools.yaml
    # 的 enum：放进去就等于把急停暴露成 LLM 可选的普通动作。
    'emergencyStop',
]


# 调用来源标识，默认 local-agent，可用环境变量 ACTION_CALLER 覆盖。
# 端上既有取值是 speech_offline / speech_online / app / device；
# local-agent 是本机智能体的约定取值（已写进规格文档第 4 节）。
def _call_source():
    return os.environ.get("ACTION_CALLER", "").strip() or "local-agent"


class RobotActionPublisher(Node):
    """机器狗动作发布节点"""

    def __init__(self):
        super().__init__('robot_action_publisher')
        self.publisher = self.create_publisher(
            RobdogAction,
            '/catch_turtle/action_type',
            10
        )

    def send_action(self, action, actiontype=ACTION_TYPE):
        """发送动作指令

        Args:
            action: 动作名称 (dance, standUp, sitDown等)
            actiontype: 动作通道，默认 motorSkill（马达技能）；其余取值见 ACTION_CHANNELS
        """
        msg = RobdogAction()
        msg.actiontype = actiontype
        msg.actionargument = action
        msg.sourcetype = _call_source()

        print(f"🐕 准备执行动作: {action}" + (f"（通道 {actiontype}）" if actiontype != ACTION_TYPE else ""))
        self.publisher.publish(msg)
        print(f"✅ 动作指令已发送: {action}")


def show_help():
    """显示帮助信息"""
    print("用法: python3 robot_action.py <动作名> [通道]")
    print("  通道默认 motorSkill（马达技能）；其余通道的动作名不受动作清单约束。")
    print("  例: python3 robot_action.py dance")
    print("      python3 robot_action.py on followMe    # 开跟随（未实测，见 SKILL.md）")
    print()
    print("🎵 舞蹈表演:")
    print("  dance       - 跳舞")
    print("  danceV2     - 舞蹈2")
    print("  happy       - 开心")
    print("  stretch     - 伸懒腰")
    print("  twistBody   - 扭身")
    print("  twistAss    - 扭屁股")
    print("  shakeBody   - 抖身（快抖）")
    print()
    print("🐕 基础动作:")
    print("  standUp     - 站立")
    print("  sitDown     - 坐下")
    print("  getDown     - 趴下")
    print("  makeBow     - 鞠躬")
    print()
    print("👋 互动动作:")
    print("  greeting    - 打招呼")
    print("  handShake   - 握手")
    print("  fingerHeart - 比心")
    print("  newYearCall - 拜年")
    print("  leap        - 扑人")
    print("  jumpForward - 前跳")
    print()
    print("🤸 特技动作:")
    print("  walkUpsideDown - 倒立行走")
    print("  standErect  - 直立站立")
    print("  backflip    - 后空翻")
    print("  turnOver    - 翻滚")
    print("  twistJump   - 扭身跳")


def main():
    """主函数"""
    # 检查参数
    if len(sys.argv) < 2:
        show_help()
        sys.exit(1)

    action = sys.argv[1]
    # 第二个位置参数 = 动作通道，缺省 motorSkill（与工具层 `robot_action.sh <动作名>` 兼容）
    actiontype = sys.argv[2] if len(sys.argv) > 2 else ACTION_TYPE

    if actiontype not in ACTION_CHANNELS:
        print(f"❌ 未知的动作通道: {actiontype}")
        print(f"可用通道: {', '.join(ACTION_CHANNELS)}")
        sys.exit(1)

    # 只对 motorSkill 校验动作名白名单：其它通道（followMe/sportMode/...）的
    # actionargument 是另一套取值（on/off/walk...），拿马达技能清单去卡会全判成非法。
    if actiontype == ACTION_TYPE and action not in VALID_ACTIONS:
        print(f"❌ 无效的动作: {action}")
        print(f"可用动作: {', '.join(VALID_ACTIONS)}")
        print()
        show_help()
        sys.exit(1)

    try:
        # 初始化 ROS 2
        print("🔧 初始化 ROS 2...")
        rclpy.init()

        # 创建节点并发送指令
        node = RobotActionPublisher()
        node.send_action(action, actiontype)

        # 等待消息发送
        rclpy.spin_once(node, timeout_sec=1.0)

        # 清理
        node.destroy_node()
        rclpy.shutdown()

        print("🎉 完成!")

    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
