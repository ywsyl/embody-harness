#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端事件模拟下发 —— 手工驱动 receiver 的 robot_agent_task 链路，一条命令一次触发。

    python3 Communicator/cloud_event_sim.py create "去厨房看看冰箱里有什么"
    python3 Communicator/cloud_event_sim.py modify "改去厨房，别开冰箱了"
    python3 Communicator/cloud_event_sim.py answer "在519"      # 回答端侧的主动发问
    python3 Communicator/cloud_event_sim.py interrupt "用户改主意了，别去了"
    python3 Communicator/cloud_event_sim.py create "去门口" --task-id cloud_20260921153000
    python3 Communicator/cloud_event_sim.py create "去门口" --dry-run     # 只打印事件 JSON，不发
    python3 Communicator/cloud_event_sim.py interrupt "结束吧" --wait 10   # 多等一会儿订阅者

入参就是 event（create / modify / answer / interrupt）与 description，**发一次 = 触发一次**：
receiver 收到后按 `CLOUD_ACTION_MAP` 把云端动作归一成**端侧动作**再投出一封 rolebus 云端事件
（见 Communicator/receiver.py 的 _forward_action）——create / modify 在端侧是同一个动作
`update`，模拟器不替它归一，发出去的仍是云端原词。
`answer` **现在打不动任何工具**：它属 `UNHANDLED_ACTIONS`，receiver 只记一条「端侧不处理」
日志、既不投事件也不写落盘单槽（两条腿一起停用，见 Communicator/receiver.py），而原先等它的
`listen` / `speak_listen` 2026-10-08 也改成了本地 ASR 收音。留着本入口只为角色系统恢复云端
answer 链路时备用。

走的是真链路，不是旁路：本脚本把一条 SIGCEvent 发到 /homi_speech/sigc_event_topic
（configs.paths.ROS_SIGC_EVENT_TOPIC），正是 receiver 订阅的云端下发话题。所以事件会完整
经过 _on_sigc_event（去重/过滤/credential 分流）→ on_speech_event（按 body.action 路由）→
_forward_action（投 rolebus）。receiver 一行没改，也不在进程内直调转发函数 —— 直调会跳过
订阅与路由这两段，恰恰是「云端下发到端侧」最该验的部分。
（它验的是**组件侧**那半条链路：事件有没有进 spool，看 $DSH_HOME/rolebus/inbox/。
 answer 的另一条腿——写 runtime_files/pending_answer.json 单槽——已随 answer 事件一起停用，
 那条腿现在不动了。）

**跑之前先读这四条**：

  - receiver 必须在跑（`bash starter.sh` 起的组件里就有它，手工单跑的
    `python3 start_agent.py cloud` 同上），否则
    话题没人订阅，事件发出去就没了。脚本会把订阅者数量打出来：0 就是没接上（没起 / domain 不是
    同一个）。这条话题只是**组件侧**的半条链路 —— 事件到了 receiver 还要被投进 rolebus 才轮到
    角色系统；想跳过 ROS 直接给角色系统投一封，用 scripts/manual/cloud-emit.sh。
  - 角色系统在跑时，create / modify 会**真的执行任务**：LLM 规划、导航、动作、播报都真实发生
    （一条 create 与一条 modify 在端侧没有区别）；interrupt 会**真的终止任务**（含停运动）。
    不是空转的假事件，别在有人的场地随手 create。
    answer 端侧不处理，发出去只多一条「端侧不处理」日志，随便发。
    （角色系统没跑就只有 spool 里多一封没人读的事件。）
  - 连发两次就是两次真实触发：每次下发都带新的 eventId。receiver 的去重只挡「同一条事件被重复
    投递」，挡不住「我又发了一条」。
  - **多个订阅者 = 各执行一遍**：ROS2 同话题的订阅者是各收一份（不是队列）。这条话题上同时挂着
    别的 receiver 时，一条 create 会被每个大脑各跑一次（两次导航、两次播报）。脚本会把订阅者
    数量打出来，>1 就是这个情况。
  - 与云端真下发的差异：不带 session_id / turn_id 的真实业务值（只填一个可辨识的模拟值，
    便于在日志里认出这次是模拟的），也不经 VSS 平台的鉴权/网关那一层。其余 envelope 与 body
    字段与云端同构（字段名从 receiver 取，见下方 import）。

退出码: 0=已下发且当时有订阅者 / 1=已发布但无人订阅（=丢了）/ 2=用法错误 / 3=环境或依赖缺失
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict

# 仓库根（本文件在 Communicator/ 下）—— lib-shell-paths.sh 与 configs 都按它定位
_ROOT = Path(__file__).resolve().parent.parent

# ROS 环境自举：rclpy / homi_speech_interface 只有在 source 过 ROS 之后才可导入，而
# os.environ 必须在 python 进程启动前就位（CycloneDDS 的库路径、domain、CYCLONEDDS_URI
# 都靠环境变量），所以这里用 bash 重新执行一次自己，而不是在 python 里 setenv。
# 与 tests/motion_watch.py 同一套做法：setup 与参数都过 configs/lib-shell-paths.sh
# （cmcc_path / cmcc_conf）从 configs/ 取，**不写死路径** —— 写死搬部署树就指向另一棵树。
_ENV_GUARD = '_CLOUD_EVENT_SIM_ENV_READY'


def _bootstrap_ros_env() -> None:
    """未 source ROS 环境时，经 bash source 后重新执行本脚本（只递归一层）。"""
    if os.environ.get(_ENV_GUARD) == '1':
        return
    cmd = (
        'set -e; '
        f'ROOT="{_ROOT}"; '
        'source "$ROOT/configs/lib-shell-paths.sh"; '
        'source "$(cmcc_path ROS_SETUP)"; '
        'source "$(cmcc_path UNITREE_ROS_SETUP)"; '
        'source "$(cmcc_path CMCC_ROBOT_SETUP)"; '
        'export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"; '
        # 与 starter.sh 同一写法：已导出则沿用（跑到别的 domain 上的 receiver 也能对上），
        # 未导出才回退 configs 的值。setup.bash 自己就导出了 ROS_DOMAIN_ID，故这行基本取它。
        'export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$(cmcc_conf ROS_DOMAIN_ID)}"; '
        'export CYCLONEDDS_URI="$(cmcc_conf CYCLONEDDS_URI)"; '
        f'export {_ENV_GUARD}=1; '
        'exec python3 "$0" "$@"'
    )
    os.execvp('bash', ['bash', '-c', cmd, os.path.abspath(__file__), *sys.argv[1:]])


_bootstrap_ros_env()

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import rclpy
    from homi_speech_interface.msg import SIGCEvent
except ImportError as exc:
    print(f'[sim] ERROR: 无法导入 ROS 依赖（{exc}）')
    print('[sim] 提示: 本脚本通常能自己 source ROS（见文件头），失败时手动 '
          'source /opt/ros/foxy/setup.bash 与 /usr/bin/cmcc_robot/install/setup.bash')
    sys.exit(3)

try:
    # 协议常量（事件名 / 动作名 / 域）一律从 receiver 取，不在这里重抄一份字符串：
    # 抄一份就会出现「改了 receiver 忘了改模拟器」的静默漂移——模拟器发出的话题名或
    # action 拼错时，收到的只是「未支持的云端事件」debug 日志，不报错。
    from Communicator.receiver import (
        ACTION_ANSWER,
        ACTION_CREATE,
        ACTION_INTERRUPT,
        ACTION_MODIFY,
        DOMAIN_AGENT,
        EVENT_ROBOT_AGENT_TASK,
    )
except ImportError as exc:
    print(f'[sim] ERROR: 无法导入 Communicator.receiver 的协议常量（{exc}）')
    print('[sim] 提示: 必须在仓库根下跑（receiver 依赖 ROS 侧的 claw_client 等运行时包）')
    sys.exit(3)

from configs.paths import DEVICE_ID, ROS_SIGC_EVENT_TOPIC

# 模拟下发的标记：session_id 只进 receiver 的链路日志，用于把「这次是模拟的」认出来
SIM_SESSION_ID = 'sim_cloud_event'

# 等订阅者的最长时间（秒）：DDS 是异步发现，节点刚起来就 publish 有丢的可能
DEFAULT_DISCOVERY_TIMEOUT = 5.0
# 发布后留给 executor 把消息推出去的收尾时间（秒）。publish 只是入队，不 spin 不保证已发出
FLUSH_SEC = 0.5

EXIT_OK = 0
EXIT_UNSUBSCRIBED = 1
EXIT_ENV = 3


# =============================================================================
# 事件构造（与云端同构）
# =============================================================================

def _body_create(description: str, task_id: str) -> Dict[str, Any]:
    """create 的 body：description 给 task_description（权威字段，见 receiver）。

    另外补一条 role=user 的同一句话进 messages：receiver 取描述时以 task_description 为准、
    空则回退 messages 里的最后一条 user 消息（见 _description_of），且真实云端下发是带
    messages 的，这里照做。
    """
    now_ms = str(int(time.time() * 1000))
    body: Dict[str, Any] = {
        'action': ACTION_CREATE,
        'task_description': description,
        'messages': [{'role': 'user', 'content': description}],
        'session_id': SIM_SESSION_ID,
        'turn_id': now_ms,
        'request_id': now_ms,
    }
    # task_id 留空时不填：receiver 交给 plugin_tools/rolebus.py 补 cloud-<Unix 秒> 占位
    if task_id:
        body['task_id'] = task_id
    return body


def _body_modify(description: str, task_id: str) -> Dict[str, Any]:
    """modify 的 body：与 create **完全同形**（task_description 权威字段 + 同一条 messages）。

    云端把 modify 当「改写某个已有任务」，但端侧没有单独的改写动作——receiver 的映射表把它
    与 create 一起归一成 `update` 投出去，角色系统靠 task_id 在不在区分「新任务 / 对已有任务
    的新交代」，所以这里照 create 的样子拼 body：description 走 task_description。
    """
    body = _body_create(description, task_id)
    body['action'] = ACTION_MODIFY
    return body


def _body_answer(description: str, task_id: str) -> Dict[str, Any]:
    """answer 的 body：description 落到 user_answer（receiver 取它当用户回答原文）。

    这条事件**当前没有消费方**：answer 属 receiver 的 `UNHANDLED_ACTIONS`，_forward_action
    见到就只记一条「端侧不处理」日志、既不投 rolebus 也不写单槽（原来那两处一起停用——
    投 rolebus 的那封云事件与写落盘单槽 Communicator/pending_answer.py），而等单槽的
    speak_listen / listen 2026-10-08 也改走本地 ASR 了。本函数还照常拼 body，是为了恢复
    那条链路时事件是完整的。

    messages 与 create 同理由地带上（role=user 的同一句）：真实下发是带的。
    """
    body: Dict[str, Any] = {
        'action': ACTION_ANSWER,
        'user_answer': description,
        'messages': [{'role': 'user', 'content': description, 'intent': 'answer'}],
    }
    if task_id:
        body['task_id'] = task_id
    return body


def _body_interrupt(description: str, task_id: str) -> Dict[str, Any]:
    """interrupt 的 body：description 落到 reason（receiver 读它当取消原因）。

    task_id 留空时 receiver 不替它定位在册任务（那个回退已随任务池删除）：直接让
    plugin_tools/rolebus.py 补 `cloud-<Unix 秒>` 占位号，编排器按动作停止在跑的任务。
    reason 留空则 receiver 用自带的默认文案，这里不重抄一份默认值。
    """
    body: Dict[str, Any] = {'action': ACTION_INTERRUPT}
    if task_id:
        body['task_id'] = task_id
    if description:
        body['reason'] = description
    return body


# event 名 → body 构造器。这里放的是**云端词**（模拟器下发的是云端原话，归一由 receiver 做）；
# 要加 info / resume 就补一个构造器（各自的字段不同，取值见 receiver._description_of 的分支），
# CLI 的 choices 跟着这张表走。
ACTION_BUILDERS = {
    ACTION_CREATE:    _body_create,
    ACTION_MODIFY:    _body_modify,
    ACTION_ANSWER:    _body_answer,
    ACTION_INTERRUPT: _body_interrupt,
}


def build_event(action: str, description: str, task_id: str = '') -> Dict[str, Any]:
    """构造一条与云端同构的 robot_agent_task 事件（envelope + body）。"""
    now_ms = str(int(time.time() * 1000))
    return {
        'deviceId': DEVICE_ID,
        'domain':   DOMAIN_AGENT,
        'event':    EVENT_ROBOT_AGENT_TASK,
        # 每次下发都发新的 eventId：receiver 按它去重，重放同一条才会被挡，
        # 「我再发一条」不该被挡，所以不能复用 id。
        'eventId':  f'sim_{now_ms}_{uuid.uuid4().hex[:8]}',
        'seq':      now_ms,
        'body':     ACTION_BUILDERS[action](description, task_id),
    }


# =============================================================================
# 下发
# =============================================================================

def send_cloud_event(
    action: str,
    description: str,
    task_id: str = '',
    discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
) -> int:
    """把一条模拟云端事件发到 SIGC 事件话题（= receiver 订阅的那条云端下发话题）。

    先等订阅者出现再发**正好一条**：DDS 异步发现，订阅还没匹配上就 publish，
    消息会被静默丢掉（云端视角是「发了但端侧没动」，最难查的那种）。所以这里等
    pub 报告匹配到订阅者，再发一条 —— 本函数一次调用 = 一次触发，不做重发。

    Returns:
        EXIT_OK           已下发且当时有订阅者
        EXIT_UNSUBSCRIBED 没人订阅（照样发了，但等于丢包，调用方按失败处理）
    """
    event = build_event(action, description, task_id)
    print('[sim] 事件 JSON:')
    print(json.dumps(event, ensure_ascii=False, indent=2))

    rclpy.init()
    node = rclpy.create_node('cloud_event_sim')
    try:
        pub = node.create_publisher(SIGCEvent, ROS_SIGC_EVENT_TOPIC, 10)

        deadline = time.time() + max(0.0, discovery_timeout)
        subs = pub.get_subscription_count()
        while subs == 0 and time.time() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            subs = pub.get_subscription_count()

        msg = SIGCEvent()
        msg.event = json.dumps(event, ensure_ascii=False)
        pub.publish(msg)

        # publish 只是入队，还要把 executor 转一会儿才真的推出去（与上面同一条理由）
        end = time.time() + FLUSH_SEC
        while time.time() < end:
            rclpy.spin_once(node, timeout_sec=0.05)

        print(f'[sim] 已发布到 {ROS_SIGC_EVENT_TOPIC}（订阅者={subs}, action={action}）')
        if subs == 0:
            print('[sim] ⚠️ 当时无人订阅：事件等同丢包。请确认 receiver 正在跑'
                  '（starter.sh 起的，或手工单跑的 start_agent.py cloud），'
                  '且与本脚本在同一个 ROS domain')
            return EXIT_UNSUBSCRIBED

        if subs > 1:
            print(f'[sim] ⚠️ 有 {subs} 个订阅者：同话题各收一份，这条事件会被每个'
                  '大脑各执行一遍（见文件头「多个订阅者」那条）')

        print('[sim] 处理结果见 receiver 日志（on_speech_event 路由 + _handle_* 的处理记录）')
        return EXIT_OK
    finally:
        node.destroy_node()
        rclpy.shutdown()


# =============================================================================
# 入口
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        prog='cloud_event_sim.py',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='模拟云端下发 robot_agent_task 事件：发一次 = 触发一次 receiver 的处理函数',
        epilog=(
            '示例:\n'
            '  %(prog)s create "去厨房看看冰箱里有什么"\n'
            '  %(prog)s modify "改去厨房，别开冰箱了"\n'
            '  %(prog)s answer "在519"\n'
            '  %(prog)s interrupt "用户改主意了，别去了"\n'
            '  %(prog)s create "去门口" --task-id cloud_20260921153000\n'
            '  %(prog)s create "去门口" --dry-run\n'
        ),
    )
    parser.add_argument(
        'event', choices=sorted(ACTION_BUILDERS),
        help='create=下发新任务并真的执行；modify=改写任务（端侧与 create 同义）；'
             'answer=回答端侧的主动发问（端侧不处理，只记日志）；'
             'interrupt=干涉当前任务（端侧按停止处理）',
    )
    parser.add_argument(
        'description', nargs='?', default='',
        help='create / modify 的任务描述、answer 的回答原文（三者必填）/ '
             'interrupt 的取消原因（可省，省了用 receiver 的默认文案）',
    )
    parser.add_argument(
        '--task-id', default='',
        help='云端任务 id；省略时由 receiver 交给 plugin_tools/rolebus.py 补 '
             'cloud-<Unix 秒> 占位',
    )
    parser.add_argument(
        '--wait', type=float, default=DEFAULT_DISCOVERY_TIMEOUT,
        help=f'等订阅者出现的最长秒数，默认 {DEFAULT_DISCOVERY_TIMEOUT:g}',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='只打印将下发的事件 JSON，不发布（离线核对字段用）',
    )
    args = parser.parse_args()

    description = args.description.strip()
    if args.event in (ACTION_CREATE, ACTION_MODIFY, ACTION_ANSWER) and not description:
        # 三者的 description 都是必填：create / modify 缺 task_description、answer 缺 user_answer
        # 时 receiver 取不到 description 就**不投**（见 _forward_action 的空值分支），发出去等于白发。
        parser.error(f'{args.event} 必须给 description（receiver 取不到就不会投递）')

    if args.dry_run:
        print(json.dumps(build_event(args.event, description, args.task_id),
                         ensure_ascii=False, indent=2))
        return EXIT_OK

    return send_cloud_event(args.event, description, args.task_id, args.wait)


if __name__ == '__main__':
    sys.exit(main())
