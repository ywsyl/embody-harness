#!/usr/bin/env python3
"""
「最近一次下发的本体运动」的记录 —— **进程内存**里的一小份状态（2026-09-20 起不落盘）。

谁写：Motion_Manager/motion_manager.py 每次下发运动前开一条新记录，收到停止信号时改一笔。
谁读：Motion_Manager/motion_status.py 渲染 motion_status.yaml 的 last_motion 块与 motion.idle。

为什么在内存里：这份记录只有 manager 自己写、同进程的 motion_status 线程读，两边共享同一个
Python 模块对象，没有跨进程需求。原先它落盘成 runtime_files/motion_record.yaml，是为了跨进程
交接给 Observer/status_observer.py —— 那个进程 2026-09-20 已不再管运动状态（见
motion_status.yaml），文件随之取消。**因此本模块的前提是读写双方在同一个进程里**：把
MotionManager 挪到别的进程去的话，记录会静默地不再更新（读方只会看到一个空记录，不会报错）。

为什么是「记录」而不是「占用锁」：本体是不是空闲**不看这份记录**，看 /catch_turtle/action_state
（2 静止站立 / 3 静止趴下 = 空闲，见 resolve_occupancy）。**例外只有一个：导航**——导航中途会
暂停，狗站着不动、action_state 短暂回到 2 静止站立，那不是导航结束，所以「最近一笔是
navigation 且 action_state 已空闲」时得再问一句导航后端还在不在飞（探针由读端提供，见
resolve_occupancy 的 nav_in_flight）。除此之外这里没有「加锁 / 解锁」，也不靠时间戳判活——
一次跑 60s 的舞蹈、300s 的导航都不会因为记录没刷新而被误判成「读不到」。这份记录只回答三件事：
最近这次是谁在动、动的是什么、它有没有被停止信号掐停。运动结束后记录留在内存里，下次下发时被
整条覆盖。

记录的内容（read_record 的返回，也是 motion_status.yaml 里 last_motion 块的四个字段）：

  source         占用来源，原样透传
  since          开这条记录的时刻，仅供人工排查，不参与任何判定
  stop_received  本次运动是否收到过停止信号
  motion         本次运动的参数本身，原样透传

stop_received 是一位状态位，三态：

  默认（没有记录）          true    没有正在跑的运动，按「已停止」处理
  下发运动时                false   这一笔刚发下去，还没收到停止信号
  收到停止信号              true    被 stop() / task_exit / 导航抢占掐停

motion 是**这一笔运动的参数**，由下发方原样透传：

  action      → 动作名（如 dance）
  move        → 运动量描述（如 forward 1m / turn 90°）
  navigation  → 目标点名称

取值与读取约定见 read_record() / resolve_occupancy()。
"""

import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 仓库根（本文件 Motion_Manager/ 的上一级）入 sys.path，以便 import configs
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import IDLE_ACTION_STATES, SNAPSHOT_TS_FMT

# 写入端允许的占用来源。读端**不**拿这份白名单卡（见 read_record）：来源名是原样透传的，
# 以后加新来源只要改这里（或由调用方传新名字），不必动读端。
# 与 configs 里各处工具名一致：move 工具 / action 工具 / 导航（工具名是 waypoint_nav，
# 占用来源统一记 navigation）。
MOTION_ACTION = "action"
MOTION_MOVE = "move"
MOTION_NAVIGATION = "navigation"

MOTION_SOURCES = (MOTION_ACTION, MOTION_MOVE, MOTION_NAVIGATION)

# 两个非来源取值
MOTION_IDLE = "idle"        # 空闲：action_state 为 2 静止站立 / 3 静止趴下
MOTION_UNKNOWN = "unknown"  # 读不到来源

# stop_received 的默认值：没有记录 = 没有正在跑的运动 = 已停止。见模块头部的三态说明。
STOP_RECEIVED_DEFAULT = True

# 时间戳格式取 configs（SNAPSHOT_TS_FMT）：与 motion_status.yaml / robot_status.yaml 必须同格式，
# 监督器按原文比先后。

# 当前记录 + 它的代次。**代次**是给读端的缓存键：每次 begin_motion 自增一次，代表「换了一笔
# 运动」。motion_status.py 的导航探针靠它认「还是同一笔导航吗」——旧实现用记录文件的
# inode+mtime 做同一件事，文件没了就换成这个（语义更直接：就是「换没换笔」）。
_lock = threading.Lock()
_record: Dict[str, Any] = {
    'source': None,
    'since': None,
    'stop_received': STOP_RECEIVED_DEFAULT,
    'motion': None,
}
_generation = 0


def _now_str() -> str:
    return datetime.now().strftime(SNAPSHOT_TS_FMT)


def begin_motion(source: str, motion: str) -> None:
    """开一条新记录：本次运动由 source 发起、参数是 motion，stop_received 重置为 false。

    source 必须是 MOTION_SOURCES 之一。非法取值直接抛 ValueError：宁可在这里炸，
    也不要让一个没人认识的来源流到文件里（读端是透传的，写错了它会照着渲染出去）。

    motion 是这一笔运动的参数（action=动作名 / move=运动量 / navigation=目标点名称），
    同样原样透传。**不给默认值**：让每个新增的下发点都必须显式想一次「这一笔的参数是什么」，
    忘了传就会在调用处直接报错，而不是静默留空。
    """
    if source not in MOTION_SOURCES:
        raise ValueError(f"未知的占用来源: {source!r}，可用: {', '.join(MOTION_SOURCES)}")
    global _generation
    with _lock:
        _record.update({
            'source': source,
            'since': _now_str(),
            'stop_received': False,
            'motion': motion,
        })
        _generation += 1


def read_record() -> Dict[str, Any]:
    """读最近一次运动的记录，返回 {'source', 'since', 'stop_received', 'motion'}。

    source：写进来时**原样透传**，不拿白名单卡——这样以后加新来源不必动读端。
            从来没有下发过运动（开机以来的默认记录）→ None。
    since： 开这条记录的时刻；没有记录 → None（渲染成 null：**已确认没有**）。
    stop_received：永远是个 bool，没有第三种取值；没有记录 → 默认值 true
            （见 STOP_RECEIVED_DEFAULT：没有正在跑的运动）。
    motion：同样原样透传。没有记录 / 没写这一行 → None，表示「开机以来没下发过运动」，
            由调用方渲染成 null（是**已确认没有**，不是读不到）。

    source 的 None 表示「没有这一笔」，渲染成什么由调用方定。
    """
    with _lock:
        return dict(_record)


def generation() -> int:
    """当前记录代次：每 begin_motion 自增。给读端的缓存键用（见模块头部的说明）。"""
    with _lock:
        return _generation


def note_stop_received(source: Optional[str] = None) -> None:
    """把当前这条记录标成「收到过停止信号」（stop_received 置 true）。

    Args:
        source: 给出时先比对记录里的来源，不一致就**不动**（避免把上一条无关的记录标脏）；
                传 None 表示不比对来源（急停是全局的，本来就与发起方无关）。

    没有记录、或记录里读不出来源时原样返回——没有那一笔可观测量，本来就取默认值 true；
    标记已经是 true 时同样直接返回（幂等，重复调用安全）。
    since 与 motion 原样保留：它们记的是「这条记录是何时、为何开的」，不是被停的时刻。
    """
    with _lock:
        recorded = _record['source']
        if recorded is None:
            return                  # 没有可标记的记录（此时状态位已经是默认的 true）
        if source is not None and recorded != source:
            return                  # 记录不是这个来源发起的，不是这一笔要标的东西
        if _record['stop_received']:
            return                  # 已经标过
        _record['stop_received'] = True


def resolve_occupancy(action_state: Optional[int],
                      nav_in_flight=None) -> Tuple[str, bool, Optional[str]]:
    """把「动作状态」与「运动记录」合成 motion_status.yaml 要的占用三值。

    Args:
        action_state: 本体的动作状态（configs.ACTION_STATES 的码值）
        nav_in_flight: **可调用的探针**，返回「导航后端此刻是否仍在执行导航」
            （True 在飞 / False 不在飞 / None 查不到）。
            只在「最近一笔是 navigation 且 action_state 已空闲」这一种情况下被调用一次
            —— 那正是「导航中途暂停」与「导航真的结束」长得一模一样的情形，只有导航
            自己的状态接口能把它俩分开。缺省 None = 没有探针（如命令行手工读），按旧规则判。

    Returns:
        (action_source, stop_received, last_motion)
        - action_state 为 2 静止站立 / 3 静止趴下 → 空闲（`idle`）；本体静止在场时
          不管最近一条记录是什么，都不是「正在被谁占用」。此时后两项仍从记录里读
          —— 超时收场后状态回到空闲，但那一笔「动的是什么、有没有收到停止信号」还得看得见。
          **但导航在飞时不算空闲**：导航中途暂停就是这幅样子（狗站着不动、
          action_state=2），此时仍算 navigation 占用。
        - 其余状态（含读不到状态 None）→ 用记录里的来源；读不出就算 `unknown`。
    """
    record = read_record()
    if action_state in IDLE_ACTION_STATES:
        if (record['source'] == MOTION_NAVIGATION and nav_in_flight is not None
                and nav_in_flight()):
            return MOTION_NAVIGATION, record['stop_received'], record['motion']
        return MOTION_IDLE, record['stop_received'], record['motion']
    return (record['source'] or MOTION_UNKNOWN, record['stop_received'], record['motion'])
