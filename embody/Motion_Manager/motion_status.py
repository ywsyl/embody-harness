#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
运动状态观测器（Motion Status Observer）

订阅机器狗的运动相关 ROS 话题，把一份**运动状态快照**覆盖写入
runtime_files/motion_status.yaml，并在进程内提供 snapshot() 取数。

**同时它是本进程唯一的动作状态判据来源**：MotionManager 的「动作结束没 / 现在是什么姿态」
读的是这里的 action_frame()（值 + 消息序号 + 变化次数），不再是每次现起一个
check_action_state.sh 子进程 —— 那个子进程从启动到收到第一帧要 2~3s（bash + 读 configs +
rclpy init + DDS 发现），比 getDown 这种短动作（实测 2.2s）还长，等它张开眼动作早做完了
（2026-09-21 的「getDown 假超时」就是这两个数撞出来的）。20Hz 的常驻订阅帧数只多不少。
（那条子进程兜底 2026-10-08 已删——它慢且判据更弱；check_action_state.sh 本身还在，
给开机清理与人工排查用。）

本进程没有快照线程、却要跑 MotionManager 时（第三方进程里判动作状态），由 ensure_reader()
懒起一份**只读**订阅（不起线程写文件）。

与 Observer/status_observer.py 的分工（2026-09-20 拆分）：那个管慢状态（system + 派生的 tools），
按固定周期整份写 robot_status.yaml，只靠文件与智能体交互；这个管运动状态，**只在状态改变时写**。

**跑在 agent 进程内的线程里，不是独立子进程**（start_agent.py 起，与 frame_producer / receiver
并列）。理由有二：
  1. 「最近一笔运动是谁下发的」这份记录现在只在 MotionManager 的**进程内存**里
     （Motion_Manager/motion_record.py，不再落盘），换个进程就读不到了；
  2. 它是并列组件里唯一订阅本体运动话题的那个，进程内起线程即可，无需另拉进程。
**前提写死在这里**：MotionManager 必须与它同进程 —— 记录是 manager 的进程内存，换个进程读
只会读到初值且不报错。本树里 manager 的调用方只剩启动前的停运动（startup_cleanup 起的本进程
`task_exit.stop_all_motion`），角色侧的运动/动作走脚本、不经过 manager，所以那份记录在本树
基本停在初值；快照里的动作/运动字段靠本线程自己的 ROS 订阅填。谁要把 manager 挪去别的进程，
先回来改这里。

**用自己的一份 rclpy 上下文（Context），不碰进程的默认上下文**：rclpy 的上下文是进程级的，
而且**不允许二次 init**（实测：第二次 `rclpy.init()` 报 `rcl_init called on an already
initialized context`）。本进程里默认上下文归主循环（Communicator/receiver.py 里
`rclpy.init()` + `rclpy.spin`），而我们这个线程比它先起来 —— 用默认上下文就必然要么把它顶掉、
要么被它顶掉。所以这里 `Context()` + `rclpy.init(context=...)` + 自己的
SingleThreadedExecutor，退出时只关自己这一份（实测两份上下文可共存、互不干扰）。
**别改成 rclpy.init() / rclpy.spin_once() 的全局写法**，那会连带把 receiver 弄崩。

节奏（都在 configs，不在命令行）：
  - MOTION_STATUS_TICK_SEC = 0.2s：主循环步进，每 tick 收一轮 ROS 消息。
  - 写盘判据 = 状态变了（见 _status_key）**或**心跳到期（MOTION_STATUS_HEARTBEAT_SEC = 2s）。
    空闲时内容逐字不变，只有心跳在写 —— **心跳不能删**：各段的 updated_at 只在写盘时才落进
    文件，没有心跳就分不出「状态没变」与「生产者死了」。所以判活看 mtime（最迟 2s 动过一次），
    判某个源的新鲜度看段里的 updated_at。内容没变 ≠ 生产者死了。
  - MOTION_LOCALIZATION_PROBE_SEC = 60s：定位状态**按时间节流**去问导航后端（HTTP，见
    _localization_probe）。navigation.localized 是快照里唯一「拉来」的字段（其余字段都来自
    话题订阅；判空闲时那个 _navigation_in_flight 只是借问一句，不产生字段）。
  - MOTION_GAIT_PROBE_SEC = 5s：步态同样**按时间节流**现读，走的是 DDS 只读服务
    （SERVICE_GAIT_STATUS，见 poll_gait）。比定位那条勤：步态会在任务中途被换掉，而这一问
    是进程内的服务调用、不是 HTTP。两条「拉来」的字段都**不阻塞** tick 循环。

快照三段（段序即文件里的顺序，2026-09-20 定的）：
  - last_motion：MotionManager 的进程内记录（谁在动 / 动的是什么 / 有没有被停止信号掐停）。
    action_source 是**原样透传**的记录来源，不在这里判空闲。
  - navigation：**两个源** —— 定位是否就绪（HTTP 现读，每 60s 一次）+ 底盘位姿（/leg_odom 20Hz）。
    localized 参与写盘判据（低频离散量，翻转就该写盘）；pose 不参与（连续量，跟着写盘搭车刷新，
    狗静止时最多滞后一个心跳，比旧的「1s 采样 + 2s 写盘」还快）。段级 updated_at 只代表
    /leg_odom（只有一个时间戳而下述「各段 updated_at」的规矩要求它点名自己那个源）。
  - motion：**两个源** —— 动作状态（订阅 /catch_turtle/action_state 20Hz）+ 步态（只读服务现读，
    每 5s 一次）。本体是否空闲（派生）、动作状态名与 gait 参与写盘判据；段级 updated_at 只代表
    action_state（同 navigation 段的规矩：点名自己那个源，另一个源靠「值是不是 unknown」表态）。
    「空闲与否」的合成规则只有一份：Motion_Manager.motion_record.resolve_occupancy
    —— 判据是 action_state 落在 2 静止站立 / 3 静止趴下，**唯一的例外是导航在飞时不算空闲**
    （导航中途会暂停：狗站着不动、action_state 短暂回到 2，那不是导航结束），那时要问一句
    导航后端（_navigation_in_flight），问的是此刻，不来自记录。

各段 updated_at = **该源最后一次真正收到消息的时刻**（不是写盘时刻）：源静默时它不再前进，
它很旧而值字段是 unknown 就是那个源断了。从未收到过 → unknown，**永不 null**。
null 只表「已确认没有这个值」（如开机以来没下发过运动，没有参数可报）—— null 与 unknown
不是一回事。
**唯一的例外是 navigation.localized**：它读不到时报 false 而不是 unknown（口径与理由写在
_localization_probe 的 docstring 与渲染注释里）。它是后加的一个布尔判断，别拿它当「unknown
口径可以有第二份」的先例 —— 比它更晚加的 motion.gait 就没走这条路（读不到报 unknown）。

数据源（2026-09-16 在真机上逐个实测确认，非文档推断；2026-09-21 补最后一条）：
  /catch_turtle/action_state      Int32         20Hz     姿态与运动状态（取值见 configs.ACTION_STATES）
  /leg_odom                       Odometry      20Hz     底盘位姿（位置 + 四元数）
  nav_tools.localization_status()  HTTP  定位是否就绪（data.localized，取不到即 false）
  SERVICE_GAIT_STATUS             srv 只读      现读    步态（结果的 gait_type，取不到即 unknown）

原子写：临时文件 + rename，读方任何时刻读到的都是一份完整快照。

使用（必须先 source 运行中的 ROS 安装，否则消息包对不上）：
    source /opt/ros/foxy/setup.bash
    source /usr/bin/cmcc_robot/install/setup.bash   # 运行中的安装；/root/cmcc_robot 是陈旧副本
    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp ROS_DOMAIN_ID=2
    python3 Motion_Manager/motion_status.py             # 前台跑（排障用；正常由 start_agent 起线程）
"""

import json
import math
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional

# 确保项目根目录在 Python 搜索路径中（用于导入 configs 与 Motion_Manager 包）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import (
    ACTION_STATES,
    MOTION_GAIT_PROBE_SEC,
    MOTION_LOCALIZATION_PROBE_SEC,
    MOTION_SOURCE_STALE_SEC,
    MOTION_STATUS_HEARTBEAT_SEC,
    MOTION_STATUS_TICK_SEC,
    NAVIGATION_PROBE_TIMEOUT,
    SNAPSHOT_COMMENT_COL,
    SNAPSHOT_TS_FMT,
)
from configs.paths import RUNTIME_MOTION_STATUS
from Motion_Manager.motion_record import (
    MOTION_IDLE,
    MOTION_UNKNOWN,
    generation as record_generation,
    read_record,
    resolve_occupancy,
)

# 本模块**刻意不 import Observer.status_observer** 来复用它的渲染辅助：那个模块在 import 期就
# `from homi_speech_interface.msg import ...` 且失败即 sys.exit(3)，复用等于把本组件的生死绑到
# 语音包上。多出来的这几十行渲染辅助是有意的重复；但**渲染格式不重复**——时间戳格式与注释列
# 取 configs 的 SNAPSHOT_TS_FMT / SNAPSHOT_COMMENT_COL，两个快照的写法必须一致。

# 快照里代表「读不到」的字面量：统一一个写法，消费方好判。
UNKNOWN = 'unknown'

# 与 UNKNOWN 并列的另一种「没有值」：**已确认没有**（如开机以来没下发过运动）。
NULL = 'null'

# rclpy / 消息包的 import 结果。**在模块导入期就试**：这样 start_agent.py 在起线程之前就能
# 看到失败并当场报出来，而不是让线程在后台静默死掉（见 start_agent 的 _start_motion_status_thread）。
IMPORT_ERROR: Optional[str] = None
try:
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from std_msgs.msg import Int32
    from nav_msgs.msg import Odometry
except ImportError as _exc:  # pragma: no cover - 只在没 source ROS 环境的机器上出现
    rclpy = None
    Context = None  # type: ignore[assignment]
    SingleThreadedExecutor = None  # type: ignore[assignment]
    Node = object  # type: ignore[assignment]
    IMPORT_ERROR = str(_exc)

# 步态服务的消息类型**单独**试导入，**不并进上面那把 IMPORT_ERROR**：那个一旦非空，整个组件
# 就不跑了；而语音包缺失只该让 motion.gait 这一格变成 unknown（正是上面「不 import
# status_observer 的渲染辅助」同一条理由：别把本组件的生死绑到语音包上）。拿不到类型就不建
# 客户端，那个字段恒 unknown，其余两段照常。
GAIT_MSG_ERROR: Optional[str] = None
try:
    from homi_speech_interface.srv import PeripheralsStatus
except ImportError as _exc:  # pragma: no cover - 只在没有该语音包的机器上出现
    PeripheralsStatus = None  # type: ignore[assignment]
    GAIT_MSG_ERROR = str(_exc)


# =============================================================================
# 辅助
# =============================================================================

def _now_str() -> str:
    return datetime.now().strftime(SNAPSHOT_TS_FMT)


def _scalar(value: Any) -> str:
    """把值渲染成 YAML 标量；None 一律是 unknown。"""
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float):
        return f'{value:g}'
    return str(value)


def _quoted(value: Optional[str]) -> str:
    """时间戳 / 自由文本：带引号，None 仍是裸的 unknown。"""
    if value is None:
        return UNKNOWN
    return f'"{value}"'


def _nullable(value: Any) -> str:
    """「有则给值、没有就是 null」的字段（区别于 _scalar 的 None → unknown）。

    null = **已确认没有这个值**（与 unknown 的「有通路但读不到」不是一回事，见文件头）。
    last_motion 那一块用它：没有记录 = 开机以来没下发过运动，是确确实实的「没有」，
    写成 unknown 会让人以为读不到。
    """
    if value is None or value == '':
        return NULL
    return _scalar(value)


def _nullable_quoted(value: Optional[str]) -> str:
    """_nullable 的带引号版（自由文本：目标点名称可能含空格、# 或 :）。"""
    if value is None or value == '':
        return NULL
    return f'"{value}"'


def _yaw_from_quaternion(qx: float, qy: float, qz: float, qw: float) -> float:
    """四元数 → 偏航角（弧度），只取绕 Z 的分量。"""
    siny_cosp = 2.0 * (qw * qz + qx * qy)
    cosy_cosp = 1.0 - 2.0 * (qy * qy + qz * qz)
    return math.atan2(siny_cosp, cosy_cosp)


# =============================================================================
# 导航探针：只在本体空闲而最近一笔是导航时问一次
# =============================================================================

# 「导航不在飞」这个终态答案的缓存：{记录代次: True}。见 _navigation_in_flight。
_nav_probe_terminal: Dict[int, bool] = {}


def _navigation_in_flight() -> Optional[bool]:
    """问导航后端：它此刻是否仍在执行导航。True 在飞 / False 不在飞 / None 查不到。

    为什么要问：导航中途会暂停——狗站着不动、action_state 短暂回到 2 静止站立，
    那不是导航结束。而「结束」只有导航自己的状态接口说了算（SUCCESS / FAILED /
    CANCELLED），所以「最近一笔是 navigation 且 action_state 已空闲」时得问这一句。
    平时不产生任何请求：只被 resolve_occupancy 的那一个分支调用（见其 nav_in_flight）。

    RUNNING = 在飞；IDLE / SUCCESS / FAILED / CANCELLED = 不在飞。请求失败、超时、
    返回体不认识 → None（读不出来就退回旧行为：按 action_state 判空闲，不拿猜的结论覆盖）。

    缓存：同一笔导航的答案不会变——导航结束后记录里仍留着 navigation，若不缓存，
    狗每站着不动一帧就白问一次，而且后端僵住时这一问的 NAVIGATION_PROBE_TIMEOUT
    会把渲染循环拖住。所以「不在飞」这个**终态答案**按记录代次记下来，同一笔不再问；
    新一笔导航会 begin_motion（代次自增，见 motion_record.generation），代次变了缓存自然
    失效，不必手工清理。在飞 / 查不到的答案**不缓存**：前者要接着看它什么时候结束，
    后者下一帧该重试。
    """
    gen = record_generation()
    if _nav_probe_terminal.get(gen):
        return False
    from plugin_tools import nav_tools

    body = nav_tools.navigation_status(NAVIGATION_PROBE_TIMEOUT)
    if not isinstance(body, dict) or body.get('code') != 0:   # ""/str/非 0：读不出来
        return None
    try:
        status = str((body.get('data') or {}).get('status') or '').strip().upper()
    except AttributeError:      # 返回体不是对象（如一个 list）：读不出来，退回旧行为
        return None
    if not status:
        return None
    in_flight = status == 'RUNNING'
    if not in_flight:
        _nav_probe_terminal.clear()
        _nav_probe_terminal[gen] = True
    return in_flight


# =============================================================================
# 定位探针：按时间节流、无条件问（与上面那条按需问的探针分工不同，别合并）
# =============================================================================

# 定位状态的节流缓存：{'value': 上次的结论, 'next_at': 下一次允许探测的单调时刻}。
# **不加锁**：只有写快照的这一个线程与测试碰它（_latest 那把锁是因为它要被别的线程读，别跟着加）。
_loc_cache: Dict[str, Any] = {'value': False, 'next_at': 0.0}


def _localization_probe() -> bool:
    """问导航后端：底盘此刻是否已完成定位。True / False。

    为什么要问：底盘能不能导航，前提是定位已完成，而这件事两个话题里都没有 —— 只有导航后端知道。

    与 _navigation_in_flight 的分工（都是问同一个后端的 HTTP，但问的理由和节奏不一样）：
      那个**按需**问：只在「最近一笔是 navigation 且 action_state 已空闲」这一种情形下被调用
        （导航暂停与导航结束长得一样），终态答案按记录代次缓存。
      这个**无条件**问：开机起一直问、狗动不动都问，所以必须按时间节流
        （MOTION_LOCALIZATION_PROBE_SEC，60s），否则 0.2s 的 tick 会变成 5 请求/秒。

    **读不到一律报 False**（nav_tools 给回 ""、应答不是 JSON、code!=0、字段不认识）——
    这是本文件 unknown 口径的**唯一例外**：本字段的需方要的是「此刻能不能导航」这一个布尔判断，
    读不到时按「没定位」偏保守处理。代价已写进渲染的注释：False 不区分「未定位」与「读不到」；
    想知道导航服务本身在不在，看 robot_status.yaml 的 tools.navigation。

    节流**从请求返回之后起算**：若从开始起算，后端僵住（单次探测耗时 ≥ 周期）时每一拍都会再探，
    循环就有一半以上时间耗在这一句上。
    """
    now = time.monotonic()
    if now < _loc_cache['next_at']:
        return _loc_cache['value']

    localized = False
    from plugin_tools import nav_tools

    body = nav_tools.localization_status(NAVIGATION_PROBE_TIMEOUT)
    if isinstance(body, dict) and body.get('code') == 0:
        data = body.get('data')
        raw = data.get('localized') if isinstance(data, dict) else None
        # 只认真正的布尔：`'false'` 这种字符串走 bool() 会变成 True（读反了语义还不报错），
        # 0/1 也不认 —— 与上面那条探针「枚举外的值一律不采信」同款。
        if isinstance(raw, bool):
            localized = raw
    # 拿不到（'' / 应答不是 JSON / code!=0 / 字段不认识）→ 保持 False，见上面的口径说明

    _loc_cache['value'] = localized
    _loc_cache['next_at'] = time.monotonic() + MOTION_LOCALIZATION_PROBE_SEC
    return localized


# =============================================================================
# 订阅：只留最新值 + 最后一次收到消息的时刻
# =============================================================================

TOPIC_ACTION_STATE = '/catch_turtle/action_state'
TOPIC_LEG_ODOM = '/leg_odom'
# 步态**只有这一条通路**：只读服务（回一个 JSON 串，gait_type 在里面）。
# /robdog_control/robdog_state 话题是事件驱动的，实测 70s 一条不吐，不能当源（2026-09-21）。
SERVICE_GAIT_STATUS = '/robdog_control/get_gait_status'


class ActionFrame(NamedTuple):
    """一帧动作状态 + 两个计数器（不可变：读到的一定是配好对的一帧）。

    value:   最新取值（configs.ACTION_STATES 的码值，码表外的原始码原样带出）。
    seq:     本进程收到的第几条 action_state 消息（从 1 起、单调递增）。
             用途只有一个：**让两条取值相同的消息也彼此不同**。判据那边拿「这一帧跟上次
             采样取到的那帧是不是同一条」当「有没有新消息」用（motion_manager._wait_state），
             没有它，话题静默时反复读到同一个旧值就会被数成「连续静止」。
    changes: 取值发生变化的累计次数（单调递增）。下发前后各取一次、相减，就是「这期间
             状态到底动过没有」——比「必须逮到 1/4/5 那一瞬」稳，短动作也不会漏。
             Motion_Manager 的等状态判据还用它做第二件事：**保持窗口内它必须一直不动**。

    seq 的前身是 streak（该取值连续收到了几帧）：判据从「连续 N 帧同值」改成「按时间保持」
    之后没人再关心连续同值帧数，但那点「逐条消息彼此可区分」的作用还得留着（2026-10-08 改）。
    """

    value: int
    seq: int
    changes: int


class MotionStatusReader(Node):
    """订阅运动相关话题，缓存最新值。时间戳用于判失效——旧值不冒充现状。

    context 是**本组件自己那份**（见文件头）：节点必须建在自己的上下文里，才谈得上
    用自己的 executor 单独 spin。
    """

    def __init__(self, context) -> None:
        super().__init__('motion_status', context=context)
        # 动作状态只存这一份 frame（值 + 消息序号 + 变化次数）：build_status 读 action_state（下面的
        # property），MotionManager 的判据读 frame，两处取的是同一个值，不会各自跑偏。
        # **整份替换一个不可变元组**：MotionManager 在别的线程里读它，单次引用赋值就够了。
        self.frame: Optional[ActionFrame] = None
        self.pose: Optional[Dict[str, float]] = None

        # 步态：最后一个**认下来的**取值（原样字符串），以及那一笔探测的在飞状态。
        # 它与 self.frame 是**两个源**（一个订阅 + 一个只读服务），别把两者搅在一起。
        self.gait: Optional[str] = None
        self._gait_client = None
        if PeripheralsStatus is not None:
            self._gait_client = self.create_client(PeripheralsStatus, SERVICE_GAIT_STATUS)
        self._gait_fut = None            # 在飞的那笔探测（None = 没有）
        self._gait_sent_at = 0.0         # 它是什么时候发出去的（判「丢了没」用）
        self._gait_next_at = 0.0         # 下一次允许探测的单调时刻（节流）

        # 每个源「最后收到消息」的时刻：monotonic 判失效，墙钟进快照各段的 updated_at
        self.seen: Dict[str, float] = {}
        self.seen_wall: Dict[str, str] = {}

        self.create_subscription(Int32, TOPIC_ACTION_STATE, self._on_action_state, 10)
        self.create_subscription(Odometry, TOPIC_LEG_ODOM, self._on_leg_odom, 10)

    # ---------- 记账 ----------

    def _mark(self, key: str) -> None:
        self.seen[key] = time.monotonic()
        self.seen_wall[key] = _now_str()

    def fresh(self, key: str) -> bool:
        """该源是否还在正常发：静默超过 MOTION_SOURCE_STALE_SEC 即视为读不到。

        **这是业务判据不是报告新鲜度**：读不到就不能编一个值出来（action_state 读不到时
        按不空闲报，见 motion.idle）。robot_status.yaml 那边不做失效回落，是另一回事。
        """
        last = self.seen.get(key)
        return last is not None and (time.monotonic() - last) <= MOTION_SOURCE_STALE_SEC

    # ---------- 回调 ----------

    @property
    def action_state(self) -> Optional[int]:
        """最新动作状态取值（快照组装用；判据走 action_frame()）。"""
        return self.frame.value if self.frame is not None else None

    def _on_action_state(self, msg: Int32) -> None:
        """每收到一帧就换掉整条 frame ——**取值不变也要换**（seq 逐条递增）：等状态那条判据拿
        「frame 有没有变」当「有没有新消息」用（见 motion_manager._wait_state）。"""
        value = int(msg.data)
        prev = self.frame
        if prev is None:
            seq, changes = 1, 0
        else:
            seq = prev.seq + 1
            changes = prev.changes + (0 if value == prev.value else 1)
        self.frame = ActionFrame(value, seq, changes)
        self._mark('action_state')

    def _on_leg_odom(self, msg: Odometry) -> None:
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        self.pose = {
            'x': round(float(p.x), 3),
            'y': round(float(p.y), 3),
            'yaw': round(_yaw_from_quaternion(float(q.x), float(q.y), float(q.z), float(q.w)), 3),
        }
        self._mark('leg_odom')

    # ---------- 步态：只读服务 + 按时间节流 ----------

    def _gait_value(self) -> Optional[str]:
        """当前可用的步态值；源静默（超过 MOTION_SOURCE_STALE_SEC 没收到应答）就是 None → unknown。

        与其余字段同口径，**不沿用 navigation.localized 的 false 例外**：步态没有
        「读不到 = 某个确定值」这回事，读不到就是读不到。
        """
        return self.gait if self.fresh('gait') else None

    def poll_gait(self) -> Optional[str]:
        """取当前步态；必要时顺手发一笔探测。读不到 → None（快照里渲染成 unknown）。

        为什么用服务：步态只在 /robdog_control/get_gait_status 这个**只读服务**里（result 是个
        JSON 串，gait_type 在里面）；robdog_state 那条话题是事件驱动的，实测 70s 一条不吐，不能当源。

        为什么 call_async + 回调、不同步等结果：本组件是 0.2s 的 tick 循环，阻塞等一次服务会把这一拍
        （连同这一拍的订阅收取）按住；而响应本来就由本节点自己的 executor 在下一拍 spin 时送进回调
        —— 异步才是不欠账的写法。

        节奏 MOTION_GAIT_PROBE_SEC，**从发出那一刻起算**：这一笔不阻塞循环、丢了也不拖着谁，
        按发出起算才等价于「最多每 N 秒一笔」。发之前三道闸：
          - 没有消息类型 / 没建客户端 → 恒 None（语音包缺失只是这一格空着，其余两段照常）；
          - 上一笔还在飞且还在 stale 窗口内 → 等它，不叠加第二笔；超出窗口就当它丢了
            （那时值本来就已经报 unknown），丢掉重来；
          - 节流窗口（`_gait_next_at`，与上面那道**并列**、顺序在外）：没到点就这一拍不问。
            **别把它写成「上一笔有结论就直接往下走」**：那等于每收到一次应答就立刻再问一次
            （0.2s 的 tick 下就是 5 次/秒），节流形同虚设 —— 这条是测试 B4 逮出来的。
            两道窗口默认一样长（都是 5s），所以丢笔后的重来也是 ~5s 一次，不会更密；
          - `service_is_ready()` 是 False → 这一拍不发。**实测（2026-09-21）：客户端建好后第一笔会在
            DDS 发现完成前丢掉**（盲发的那笔等满 3s 也没响应，紧接着的第二笔 5ms 就回来），
            所以这道闸不是保险，是必需。

        只读模式（_serve(with_snapshot=False)，给没有快照线程的进程用）里本函数不会被调用：那条路
        只取动作状态帧，不组装快照，也就不该额外发 DDS 请求。
        """
        if self._gait_client is None:
            return None
        now = time.monotonic()
        if self._gait_fut is not None:
            if self._gait_fut.done():
                self._gait_fut = None               # 这一笔有结论了（应答由回调消化），记下就行
            elif now - self._gait_sent_at >= MOTION_SOURCE_STALE_SEC:
                self._gait_fut.cancel()             # 超窗口：当它丢了
                self._gait_fut = None
            else:
                return self._gait_value()           # 还在这笔的预算内，等它
        if now < self._gait_next_at:
            return self._gait_value()               # 节流命中

        if not self._gait_client.service_is_ready():
            return self._gait_value()
        self._gait_fut = self._gait_client.call_async(PeripheralsStatus.Request(data=''))
        self._gait_fut.add_done_callback(self._on_gait_response)
        self._gait_sent_at = now
        self._gait_next_at = now + MOTION_GAIT_PROBE_SEC
        return self._gait_value()

    def _on_gait_response(self, fut) -> None:
        """服务应答 → 认下这一步态。**读不出来就什么都不做**（值不变、时间戳不前进 → 到点报 unknown）。

        认的只有一种：error_code == 0、result 能解析成 JSON 对象、gait_type 是非空字符串。

        gait_type **原样透传、不查白名单**：那是驱动器自己的名字（工具支持 4 个，端上还有
        jumpRun / walk / runSide 之类），按工具那 4 个过滤会把 APP / 语音切出来的步态报成 unknown，
        等于把信息抹掉。
        """
        try:
            resp = fut.result()
        except Exception:               # 被取消 / 服务侧异常：当这一笔没收到
            return
        if getattr(resp, 'error_code', None) != 0:
            return
        try:
            body = json.loads(resp.result)
        except Exception:
            return
        gait = body.get('gait_type') if isinstance(body, dict) else None
        if not isinstance(gait, str) or not gait.strip():
            return
        self.gait = gait.strip()
        self._mark('gait')


# =============================================================================
# 快照组装
# =============================================================================

def build_status(node: 'MotionStatusReader') -> Dict[str, Any]:
    """采一份运动状态快照：last_motion（记录）+ navigation（定位 + 位姿）+ motion（派生 + 动作状态）。

    navigation 段是**两个源**：定位状态现问导航后端（HTTP，节流见 _localization_probe），
    位姿订 /leg_odom。段级 updated_at 只代表后者（见 render）。
    """
    action_state = node.action_state if node.fresh('action_state') else None

    # ---- 最近一笔运动的记录（进程内存，见 motion_record）----
    record = read_record()
    # 合成规则全在 motion_record.resolve_occupancy（那边是唯一一份）：本体空闲与否只看
    # action_state，跑得久的动作/导航不会因为记录没刷新而被误判；例外只有导航在飞。
    # 兜底只护探针那一条路（导航后端的返回体畸形等）：出错时报 unknown 而**不是** idle ——
    # 读不出来时并不知道本体空不空着，报「空闲」是编出来的结论。
    try:
        occupancy, _stop_received, _params = resolve_occupancy(
            action_state, nav_in_flight=_navigation_in_flight
        )
    except Exception as exc:
        print(f'[MotionStatus] 判空闲失败，按 unknown 处理: {exc}', flush=True)
        occupancy = MOTION_UNKNOWN

    return {
        'last_motion': {
            # 原样透传的记录来源（action/move/navigation）；没有记录 → None → null（= 没有这一笔）
            'action_source': record['source'],
            'params': record['motion'],
            'stop_received': record['stop_received'],
            # 仅供人工排查，不参与任何判定
            'start_at': record['since'],
        },
        'navigation': {
            # 定位是否就绪（HTTP，60s 一次）：读不到就是 False，见 _localization_probe
            'localized': _localization_probe(),
            'pose': node.pose if node.fresh('leg_odom') else None,
            # 源静默时它不再前进 —— 它很旧而 pose 是 unknown，就是位姿数据断了。
            'updated_at': node.seen_wall.get('leg_odom'),
        },
        'motion': {
            # 是否空闲：由 resolve_occupancy 判（导航在飞时即使 action_state 是 2 也不算空闲）
            'idle': occupancy == MOTION_IDLE,
            'action_state_name': ACTION_STATES.get(action_state, UNKNOWN)
            if action_state is not None else UNKNOWN,
            # 当前步态（只读服务现读，节流见 poll_gait）：读不到就是 unknown，与其余字段同口径
            'gait': node.poll_gait(),
            'updated_at': node.seen_wall.get('action_state'),
        },
    }


def _status_key(snap: Dict[str, Any]) -> Any:
    """写盘判据：状态本身有没有变。

    **必须剔除两个 updated_at**：它们分别是 action_state / leg_odom 两个 20Hz 源的收信时刻，
    源活着时每 tick 都变，算进判据等于每 MOTION_STATUS_TICK_SEC 写一次盘（1s 五次），
    那就不是「状态改变时才写」了。
    **navigation 段里只有 localized 参与判据**：它是低频离散量（定位好了没），翻转就该立刻写盘；
    同段的 pose 是连续量，狗站着不动时它也在抖，它跟着写盘搭车刷新（见 build_status 的调用点），
    最多滞后一个心跳。**「立刻」是指写盘那一侧 —— 定位值本身最迟要到下一个探测点才被看到，
    观测延迟 ≤ MOTION_LOCALIZATION_PROBE_SEC**，别把这里读成实时。
    **gait 与 localized 同款**：也是低频离散量（换了步态就该写一盘），也参与判据 —— 而它一样要按
    探测周期打折看：写盘「立刻」是指这一侧，观测延迟 ≤ MOTION_GAIT_PROBE_SEC。
    剩下的就是「人看得懂的状态」：哪一笔运动 + 是否空闲 + 动作状态名 + 定位就绪没 + 什么步态。
    """
    m = snap['motion']
    return (
        tuple(sorted(snap['last_motion'].items())),
        m['idle'],
        m['action_state_name'],
        snap['navigation']['localized'],
        m['gait'],
    )


# =============================================================================
# 渲染（带注释，覆盖写掉示例快照后字段含义不丢）
# =============================================================================

def render(snap: Dict[str, Any]) -> str:
    """快照 dict → 带注释的 YAML 文本。注释列按 configs 的 SNAPSHOT_COMMENT_COL 对齐。"""
    lm = snap['last_motion']
    n = snap['navigation']
    m = snap['motion']
    pose = n['pose']

    def kv(key: Any, value: Any, comment: str = '', indent: str = '  ') -> str:
        left = f'{indent}{key}: {_scalar(value)}'
        # 无注释的行不留尾空格（写入文件后才干净）
        if not comment:
            return left
        # 值比注释列还长时（如带中文的描述）也要**留一个空格**：YAML 里 `#` 前没有空白就
        # 不算注释，会被并进标量值里，读方拿到的就不是那个值了。
        return left + ' ' * max(SNAPSHOT_COMMENT_COL - len(left), 1) + comment

    def comment(text: str, indent: str = '  ') -> str:
        """没有实测通路的字段写成注释行：不留取值，免得读方把它当成一个真实值。"""
        return f'{indent}# {text}'

    def ts(seg: Dict[str, Any], note: str) -> str:
        """段末尾的 updated_at 行。取值格式统一（带引号的时间戳 / 裸 unknown，**永不 null**）。"""
        return kv('updated_at', _quoted(seg['updated_at']), f'# {note}')

    return f"""\
last_motion:                           # 最近一笔下发的本体运动；来源 MotionManager 的**进程内记录**（不落盘，见 Motion_Manager/motion_record.py）
{kv('action_source', _nullable(lm['action_source']), '# 这一笔的发起来源：action | move | navigation；null = 开机以来没下发过运动。**原样透传，不在这里判空闲**——空闲与否看下面 motion.idle')}
{kv('params', _nullable(lm['params']), '# 这一笔的参数：action=动作名 | move=运动量（如 forward 1m）| navigation=目标点名称；null = 没有这一笔')}
{kv('stop_received', lm['stop_received'], '# 这一笔是否收到过停止信号：true = 收到过（或没有记录 = 已停止，默认值）；false = 下发后没被停过（可能还在跑，也可能已跑完/超时）')}
{kv('start_at', _nullable_quoted(lm['start_at']), '# 这一笔下发的时刻，仅供人工排查，不参与任何判定；null = 没有这一笔')}

navigation:                            # 底盘导航；本段两个源：定位状态（现问后端，HTTP）+ 位姿（/leg_odom）。到没到目的地的判据在导航后端，不在本快照里
{kv('localized', n['localized'], f'# 底盘是否已完成定位（导航 app 的 HTTP 现读：configs.paths.NAVIGATION_LOCALIZATION_STATUS_URL 的 data.localized，经 plugin_tools/nav_tools.localization_status，每 {MOTION_LOCALIZATION_PROBE_SEC:g}s 问一次）。**读不到一律报 false**（连不上/超时/code!=0/返回体不认识）—— 这是本文件里唯一不写 unknown 的字段：本字段要的就是「此刻能不能导航」这一个判断。代价是 false 不区分「未定位」与「读不到」；想知道导航服务本身在不在，看 robot_status.yaml 的 tools.navigation')}
{comment('map: 无实测通路（未接入地图状态话题）')}
  pose:                                # 底盘位姿（/leg_odom）；执行卡死判定看它是否长时间不变
    x: {_scalar(pose['x']) if pose else UNKNOWN}
    y: {_scalar(pose['y']) if pose else UNKNOWN}
    yaw: {_scalar(pose['yaw']) if pose else UNKNOWN}
{ts(n, '/leg_odom 最后一次收到消息的时刻；本段源静默时它不再前进 —— 它很旧而 pose 是 unknown，就是位姿数据断了')}

motion:                                # 运动/姿态；本段两个源：动作状态（订阅 /catch_turtle/action_state 20Hz）+ 步态（只读服务现读）
{kv('idle', m['idle'], '# 本体是否空闲：action_state 落在 2 静止站立/3 静止趴下**且**最近一笔不是仍在飞的导航（导航中途会暂停，狗站着不动、状态回到 2，那不是结束）—— 见 last_motion.action_source')}
{kv('action_state_name', m['action_state_name'], '# /catch_turtle/action_state 的描述：初始化中，未执行动作|正在执行动作|静止站立|静止趴下|运动中|切换步态中|急停状态（表在 configs.ACTION_STATES）；码表里没有的原始码与读不到一律 unknown')}
{kv('gait', m['gait'], f'# 当前步态（robdog_control 的只读服务 {SERVICE_GAIT_STATUS} 现读其结果的 gait_type，每 {MOTION_GAIT_PROBE_SEC:g}s 问一次）：全地形 AINimble | 经典 AIClassic | 跑步 run | 牵引 traction（工具能切的就这 4 个，见 configs.GAIT_TYPES），别处切出来的（APP/语音的 jumpRun、walk 等）也**原样报**；读不到一律 unknown。切换中 action_state 会是「切换步态中」(5)')}
{ts(m, '只代表 /catch_turtle/action_state（20Hz，在跑就一直是此刻）——**本段的另一个源（步态）没有独立时间戳**：它靠「值是不是 unknown」表态，新鲜度按探测周期 MOTION_GAIT_PROBE_SEC 打折看。**上面 last_motion 那一块是「最近一笔记录」，可能比这个时间戳旧得多** ——「最近一次」是它的语义，不是陈旧（记录不会过期，只在下一次下发运动时被整条覆盖）')}
"""


def write_snapshot(text: str) -> None:
    """原子写入快照（临时文件 + rename，读方读到的永远是完整的一份）。

    与 robot_status.yaml 那边不同，**本文件不需要「内容相同也照写」**：这里的判据是「状态变了
    或心跳到期」，心跳已经保证了活着这个信号会按期落到 mtime 上（见 run）。
    """
    RUNTIME_MOTION_STATUS.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_MOTION_STATUS.with_suffix('.yaml.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(RUNTIME_MOTION_STATUS)


# =============================================================================
# 取数（进程内）
# =============================================================================

# 最近一次写出去的那份快照：给**同进程**消费方（智能体侧的工具/循环）直接取数用，
# 不必再读文件、也就不会读到半份。线程写、别人读，故整体替换而非原地改。
_latest: Optional[Dict[str, Any]] = None
_latest_lock = threading.Lock()


def snapshot() -> Optional[Dict[str, Any]]:
    """取最近一份运动状态快照（还没有写过 → None）。**只在 agent 进程内有效**。"""
    with _latest_lock:
        return _latest


# 本进程那份常驻订阅：run()/只读线程建好节点后写进来、退出时清掉。
# 读方（MotionManager 的判据）不走锁：单次引用读，清掉之后拿到的一定是 None。
_reader: Optional[MotionStatusReader] = None
_reader_lock = threading.Lock()
# off = 没起；starting = 有人在建（别起第二份）；on = 已就绪；failed = 建不起来（不再重试）
_reader_state = 'off'


def action_frame() -> Optional[ActionFrame]:
    """取常驻订阅里最新的一帧动作状态（值 + 消息序号 + 变化次数）。

    状态判据（MotionManager）用这个，**不是**给快照看的报表：本进程里只有这一份
    motion 订阅，判「动作结束没」直接读它就没有 2~3s 的起进程冷启动。

    下列任一情形返回 None，调用方自己决定怎么办（MotionManager 是接着等到 deadline、
    最后报「读不到动作状态」，见 _wait_state）：
      - 本进程没起常驻订阅（--no-motion-status、独立跑脚本）；
      - 从没收到过该话题的消息；
      - 静默超过 MOTION_SOURCE_STALE_SEC（复用 fresh() 那条既有业务判据，不新增阈值）
        —— 读不到就是读不到，不能拿一个旧值冒充现状。
    """
    reader = _reader
    if reader is None or not reader.fresh('action_state'):
        return None
    return reader.frame


def ensure_reader() -> None:
    """本进程还没有常驻订阅就**懒起一份只读的**（不写 motion_status.yaml、不发布 _latest）。

    给「没有快照线程、却要跑 MotionManager」的进程用（今天只有一条：Communicator/receiver.py
    起的 resume 子进程，见 start_agent.py 的说明）。幂等：已就绪或正在建都直接返回。

    只读的边界是硬的：motion_status.yaml 的生产者只能有一个（agent 主进程，它读的是**本进程**
    内存里的运动记录），别的进程再写一份会把 last_motion 翻花。

    起不来（ROS 环境缺、建节点失败）→ 记 failed 后不再重试，调用方随后每次都读到 None。
    """
    global _reader_state
    if IMPORT_ERROR is not None:
        return
    with _reader_lock:
        if _reader_state != 'off':
            return
        _reader_state = 'starting'
    threading.Thread(
        target=_serve,
        kwargs={'stop_event': None, 'with_snapshot': False},
        name='motion-reader',
        daemon=True,
    ).start()


# =============================================================================
# 主循环
# =============================================================================

def _spin_for(executor, seconds: float, step: float = 0.05) -> None:
    """在 seconds 秒内持续 spin_once 收消息。

    订阅是异步的：数据都从回调这儿进缓存，不 spin 就会积压。step 是单次 spin_once 的上限
    ——收不到消息时它空转一次就回来，好让外层能看着表往下走。

    用 executor 而不是 rclpy.spin_once(node)：那个走的是**默认上下文**的全局 executor，
    与本组件自己那份上下文对不上（见文件头）。
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return
        executor.spin_once(timeout_sec=min(step, remaining))


def _serve(stop_event: Optional[threading.Event], with_snapshot: bool) -> None:
    """常驻订阅的躯干：建自己那份上下文/节点/executor，spin 收帧 → 更新 reader.frame。

    with_snapshot=True  —— agent 主进程那份（start_agent.py 起）：另按「状态变了或心跳到期」
                            覆盖写 motion_status.yaml，并把快照发布到 _latest。
    with_snapshot=False —— 只读订阅（本进程没有快照线程时由 ensure_reader() 懒起）：
                            **不写文件、不发布 _latest**，只把帧攒在 reader 里供判据读。
                            写盘的生产者只能有一个，理由见 ensure_reader()。

    本函数不自我引导 ROS 环境：与 status_observer / vision_observer 一致，环境由拉起它的
    那一层提供（starter.sh 已 source foxy + unitree + 运行版安装）。
    """
    global _reader, _reader_state, _latest
    # 建节点要 1~2s（rclpy init + DDS 发现）。这段窗口里状态位就得是 starting：否则
    # MotionManager 在这期间懒起第二份订阅，本进程就有两个 motion 订阅了（见 ensure_reader）。
    with _reader_lock:
        _reader_state = 'starting'
    if IMPORT_ERROR is not None:
        with _reader_lock:
            _reader_state = 'failed'
        print(f'[MotionStatus] ERROR: 无法导入 rclpy / 消息包（{IMPORT_ERROR}）', flush=True)
        print('[MotionStatus] 提示: 先 source /opt/ros/foxy/setup.bash 与 '
              '/usr/bin/cmcc_robot/install/setup.bash', flush=True)
        return

    node = None
    executor = None
    context = None
    try:
        # 自己一份上下文（见文件头：默认上下文归本进程的主循环，且 rclpy 不允许二次 init）
        context = Context()
        rclpy.init(context=context)
        node = MotionStatusReader(context)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        with _reader_lock:
            _reader = node
            _reader_state = 'on'
    except Exception as exc:
        with _reader_lock:
            _reader, _reader_state = None, 'failed'
        print(f'[MotionStatus] ERROR: 常驻订阅起不来（{exc}）', flush=True)
        return

    if with_snapshot:
        print(f'[MotionStatus] 启动，tick {MOTION_STATUS_TICK_SEC:g}s，心跳 '
              f'{MOTION_STATUS_HEARTBEAT_SEC:g}s，写入 {RUNTIME_MOTION_STATUS}', flush=True)
    else:
        print('[MotionStatus] 只读订阅已在本进程内启动（不写 '
              f'{RUNTIME_MOTION_STATUS.name}，只供本进程判据取数）', flush=True)

    last_key: Any = None
    next_beat = 0.0
    try:
        while not (stop_event is not None and stop_event.is_set()):
            _spin_for(executor, MOTION_STATUS_TICK_SEC)
            if not with_snapshot:
                continue
            snap = build_status(node)
            now = time.monotonic()
            key = _status_key(snap)
            # 判据变了就写（重启后 key 一定不同，第一拍必写）；没变就等心跳到期再写，
            # 好让 mtime 一直能证明生产者还活着。
            if key != last_key or now >= next_beat:
                text = render(snap)
                write_snapshot(text)
                with _latest_lock:
                    _latest = snap
                last_key = key
                next_beat = now + MOTION_STATUS_HEARTBEAT_SEC
    finally:
        # 只收自己这一份：executor → node → context，别去动进程的默认上下文
        with _reader_lock:
            if _reader is node:
                _reader = None
                _reader_state = 'off'
        try:
            executor.remove_node(node)
        except Exception:
            pass
        node.destroy_node()
        rclpy.shutdown(context=context)


def run(stop_event: Optional[threading.Event] = None) -> None:
    """周期检查运动状态并（在状态改变或心跳到期时）覆盖写 motion_status.yaml。

    作为 agent 进程内的 daemon 线程运行（见 start_agent.py），stop_event 置位即退出；
    前台手工跑时靠 Ctrl-C。
    """
    _serve(stop_event, with_snapshot=True)


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        print('\n[MotionStatus] 已停止', flush=True)
