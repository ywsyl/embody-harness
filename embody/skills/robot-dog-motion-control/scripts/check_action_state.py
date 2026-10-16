#!/usr/bin/env python3
"""
动作状态查询脚本 — 读 /catch_turtle/action_state（std_msgs/msg/Int32）

用途：工具层下发运动指令前的「是否站立」前置检查。

  默认：           读一帧当前状态就退出
  --wait-standing： 轮询等到 action_state == 2（静止站立）
  --wait-state 2,3：轮询等到 action_state 落在给定取值集合内

等待类模式现在是**给人手动排查用**的（例如盯着一次下发看它落到哪个态）。

**等待类模式不是生产判据**：Motion_Manager 判「动作结束没」读的是**进程内那份常驻订阅**
（Motion_Manager/motion_status.py，20Hz，与 manager 同进程，判据是按时间保持
configs.MOTION_IDLE_HOLD_SEC 秒静止 —— 见 motion_manager._wait_state）。
**2026-10-08 已删掉「取不到帧就现起本脚本来等」那条兜底**：本脚本从启动到收到第一帧要
2~3s（bash + 读 configs + rclpy init + DDS 发现），而 getDown 这类短动作本体只要 2.2s
（真机实测），等它张开眼动作早做完了 —— 「已经成功却报未观察到动作启动」。
本脚本仍在服役的两处，都是**非等待**的：开机清理复核读一帧
（Communicator/startup_cleanup.py）、_ensure_standing 读不到帧时读一帧当闸门
（Motion_Manager/motion_manager.py）。参数与**退出码语义一概不变**（下面这套仍是对外契约）。

--consecutive N：等待类模式下的**消抖**：目标状态要连续收到 N 帧才算达成，中间任何一帧
落到集合外就清零重来（默认取 configs.MOTION_IDLE_HITS）。注意这里的单位是**帧**、
按本进程**收到的条数**数（不按检测循环的轮次——话题静默时循环在空转，按轮次会把同一个
旧值重复计数），与主路径「按时间保持」不是一个口径，别拿两者互相推。为什么需要消抖：
动作通道没有完成信号，判结束只能看 action_state，而动作执行**中途会短暂闪回 2/3**，
只看一帧就会把「还没做完」判成「已完成」。N=1 即不消抖。

状态取值（原始语义见《机器人功能接口规格.md》第 3/4 节；本脚本按 `configs.ACTION_STATES`
校验，措辞以那边为准）：

  0 初始化中，未执行动作 | 1 正在执行动作 | 2 静止站立 | 3 静止趴下
  4 运动中               | 5 切换步态中   | 6 急停状态

退出码：

  0  达成：等到了目标状态，或（默认模式下）当前就是静止站立
  1  读到了状态，但不是静止站立（仅默认模式会返回）
  2  超时：等待期间没等到目标状态，或压根没收到状态消息
  3  环境错误（无法导入 rclpy / 读不到 configs / 参数非法）

stdout 保持为空，诊断信息一律走 stderr（与本目录其它脚本、以及
skills/tts-speak、skills/asr-listener 的输出约定一致）。
"""

import sys
import time
import argparse
from pathlib import Path
from typing import Optional, Set

# 仓库根（本文件 skills/robot-dog-motion-control/scripts 上四级）入 sys.path，
# 以便 import configs —— 站立判据与等待上限的唯一来源都在那边
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import Int32
except ImportError as exc:
    print(f'[MOTION] ERROR: 无法导入 rclpy / std_msgs（{exc}）', file=sys.stderr)
    print('[MOTION] 提示: source /opt/ros/foxy/setup.bash 后再运行'
          '（或直接用同目录的 check_action_state.sh）', file=sys.stderr)
    sys.exit(3)

try:
    from configs.configs import (ACTION_STATES, ACTION_STATE_STANDING,
                                 MOTION_IDLE_HITS, MOVE_STAND_TIMEOUT)
except ImportError as exc:
    # 不设字面量兜底：站立判据只允许有一个来源，宁可在这里明确报错，
    # 也不要再造一个会和 configs 漂移的"默认值"。
    print(f'[MOTION] ERROR: 无法从 configs.configs 读取 ACTION_STATES / '
          f'ACTION_STATE_STANDING（{exc}）', file=sys.stderr)
    print('[MOTION] 提示: 脚本需位于 <仓库根>/skills/robot-dog-motion-control/scripts/ 下运行',
          file=sys.stderr)
    sys.exit(3)

ACTION_STATE_TOPIC = '/catch_turtle/action_state'

EXIT_STANDING = 0
EXIT_NOT_STANDING = 1
EXIT_TIMEOUT = 2
EXIT_ENV_ERROR = 3

STATE_NAMES = ACTION_STATES   # 「数值 → 描述」的表在 configs.configs，本脚本不另留一份


def _describe(state):
    if state is None:
        return '未收到状态消息'
    return f'{state}({STATE_NAMES.get(state, "未知")})'


def _wait_desc(states):
    return ' / '.join(_describe(s) for s in sorted(states))


def _parse_wait_state(raw):
    """解析 --wait-state 的取值集合；非法（非整数、全空、或超出状态域）返回 None。

    超出状态域（非 0~6）的值直接判非法：等一个永远不会出现的状态只会白等到超时，
    调用方拿到的还是 code=2「超时」，看不出是自己参数写错了。
    """
    states = set()
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            states.add(int(part))
        except ValueError:
            return None
    if not states or any(s not in STATE_NAMES for s in states):
        return None
    return states


class ActionStateReader(Node):
    """订阅 /catch_turtle/action_state，保留最新一帧，并**按收到的帧**数连续命中数。

    streak（连续命中目标集合的帧数）必须在**回调里**数，不能在检测循环里数：循环是靠
    `spin_once` 空转的，话题静默时它照样一轮一轮地跑，拿循环轮次当帧数会把同一个旧值
    重复计数——「状态冻在 2」于是被数成「连续空闲」，正好把消抖要挡的假完成放进来。
    回调只在**真收到一帧**时被调用，帧数就是帧数。

    非等待模式（只读一帧）时 target 为空集，streak 恒为 0，不影响原有语义。
    """

    def __init__(self, target: Set[int]):
        super().__init__('action_state_reader')
        self.state = None
        self.target = target          # 命中即累加 streak 的取值集合；空集 = 不作等待
        self.streak = 0               # 连续命中 target 的帧数
        self.arrivals = []            # 最近若干帧的到达时刻，用来估帧间隔（收尾边界用）
        self.create_subscription(Int32, ACTION_STATE_TOPIC, self._on_state, 10)

    def _on_state(self, msg):
        self.state = int(msg.data)
        self.arrivals.append(time.monotonic())
        del self.arrivals[:-8]        # 只留最近 8 帧，够估间隔就行
        self.streak = (self.streak + 1) if self.state in self.target else 0


def main():
    parser = argparse.ArgumentParser(description='查询机器狗动作状态（是否站立）')
    parser.add_argument('--wait-standing', action='store_true',
                        help='轮询等到静止站立（action_state=2）为止，而不是读一帧就退出')
    parser.add_argument('--wait-state', default=None, metavar='N[,N...]',
                        help='轮询等到 action_state 落在给定取值集合内（如 2 或 1,4,5），'
                             '而不是读一帧就退出')
    parser.add_argument('--timeout', type=float, default=MOVE_STAND_TIMEOUT,
                        help=f'等待上限秒数，默认 {MOVE_STAND_TIMEOUT}'
                             '（configs.MOVE_STAND_TIMEOUT；要改就改那边）')
    parser.add_argument('--consecutive', type=int, default=MOTION_IDLE_HITS,
                        metavar='N',
                        help=f'消抖：目标状态连续收到 N 帧才算达成，中间落到集合外就清零重来'
                             f'（默认 {MOTION_IDLE_HITS}，configs.MOTION_IDLE_HITS —— 那边是'
                             f'按时间算出来的，两个口径别混；1 = 见到一帧就算）。'
                             f'按收到的帧计数，话题静默时不会把同一个旧值重复计数')
    parser.add_argument('--verbose', '-v', action='store_true', help='打印每次状态变化')
    args = parser.parse_args()

    if args.consecutive < 1:
        print(f'[MOTION] ERROR: --consecutive 必须 >= 1，收到 {args.consecutive}'
              '（1 = 见到一帧目标状态就算达成）', file=sys.stderr)
        return EXIT_ENV_ERROR

    # 等待模式：--wait-standing 与 --wait-state 都是「等到某个状态集合」，只是来源不同。
    # --wait-standing 的判据仍然只能来自 configs.ACTION_STATE_STANDING，不设字面量兜底。
    waiting = args.wait_standing or args.wait_state is not None
    if args.wait_state is not None:
        wait_for = _parse_wait_state(args.wait_state)
        if wait_for is None:
            print(f'[MOTION] ERROR: --wait-state 取值非法: {args.wait_state!r}'
                  f'（应为逗号分隔的整数，取值 {",".join(str(s) for s in sorted(STATE_NAMES))}，'
                  '如 2 或 1,4,5）', file=sys.stderr)
            return EXIT_ENV_ERROR
    else:
        wait_for = {ACTION_STATE_STANDING}

    try:
        rclpy.init(args=None)
    except Exception as exc:
        print(f'[MOTION] ERROR: rclpy 初始化失败（{exc}）', file=sys.stderr)
        sys.exit(EXIT_ENV_ERROR)

    node = ActionStateReader(wait_for if waiting else set())
    deadline = time.time() + args.timeout
    grace_used = False        # 收尾宽限只给一次，见下面 deadline 那段
    last_seen = None
    try:
        while True:
            rclpy.spin_once(node, timeout_sec=0.1)

            state = node.state
            if state != last_seen:
                last_seen = state
                if args.verbose:
                    streak = f'  连续 {node.streak}/{args.consecutive}' if waiting else ''
                    print(f'[MOTION] action_state={_describe(state)}{streak}', file=sys.stderr)

            if waiting:
                # 达成 = 连续命中 N 帧（N=1 时就是「见到一帧就算」，与旧行为一致）。
                # streak 由回调按**收到的帧**累加，循环这边只读，不自己数轮次。
                if node.streak >= args.consecutive:
                    print(f'[MOTION] ✅ 已等到 {_describe(state)}'
                          f'（连续 {node.streak} 帧）', file=sys.stderr)
                    return EXIT_STANDING
            elif state is not None:
                # 非等待模式：拿到任何一帧就按它判，不做等待、也不消抖
                # （wait_for 在这个模式下就是 {ACTION_STATE_STANDING}，见上面参数解析）
                if state in wait_for:
                    print(f'[MOTION] ✅ 已等到 {_describe(state)}', file=sys.stderr)
                    return EXIT_STANDING
                print(f'[MOTION] ⚠️ 当前不在静止站立: {_describe(state)}', file=sys.stderr)
                return EXIT_NOT_STANDING

            if time.time() >= deadline:
                # 到点了，但目标状态**已经出现**、只差连续帧没凑齐：把这一串看完再判超时。
                # 宽限按实测帧间隔估（不再引第二个常量），只给一次；帧停了就自然到点退出。
                if waiting and not grace_used and 0 < node.streak < args.consecutive \
                        and len(node.arrivals) >= 2:
                    gap = (node.arrivals[-1] - node.arrivals[0]) / (len(node.arrivals) - 1)
                    deadline = time.time() + (args.consecutive - node.streak) * gap
                    grace_used = True
                    if args.verbose:
                        print(f'[MOTION] 到点但已连续 {node.streak}/{args.consecutive} 帧，'
                              f'再给 {(args.consecutive - node.streak) * gap:.2f}s 凑齐这一串',
                              file=sys.stderr)
                    continue
                if waiting:
                    print(f'[MOTION] ⏰ {args.timeout:g}s 内没等到 {_wait_desc(wait_for)}'
                          f'连续 {args.consecutive} 帧，最后状态: {_describe(state)}'
                          f'（连续 {node.streak} 帧）', file=sys.stderr)
                else:
                    print(f'[MOTION] ⏰ {args.timeout:g}s 内未收到 {ACTION_STATE_TOPIC} 消息',
                          file=sys.stderr)
                return EXIT_TIMEOUT
    except KeyboardInterrupt:
        return EXIT_TIMEOUT
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    sys.exit(main())
