#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
机器人状态观测器（Status Observer）

订阅机器狗上已在跑的 ROS 话题，按固定周期把一份**实时状态快照**覆盖写入
/root/cmcc_agent/runtime_files/robot_status.yaml，
供监督类消费方读（判断低电量、过温、掉线、收音/播报卡住等）。

快照分三段：robot（ROS 话题订阅来的）+ system（本机现读）+ tools（六个组件各自在不在/什么状态），
整份按 configs.STATUS_WRITE_INTERVAL 覆盖写。
**tools 段的规矩：谁拥有的进程由谁监督、记录**（2026-09-21 用户定）—— 六个 tool 的判据来源
各不相同，但一律是**该组件自己的拥有者留下的痕**：它自己写的心跳 / 它自己发的话题 / 它自己持的锁 /
它自己写的文件的 mtime / 它自己的 HTTP 接口。本观测器**不 pgrep 别人的进程、不抢别人的锁，
也不重启任何人**（各 tool 注释里写的「若否则尝试重启」归拥有者 start_agent 那条链路）。
逐条判据见 TOOL_PROBES 与渲染出来的注释列。
**运动相关的状态也不在这里**：2026-09-20 拆出去给了 Motion_Manager/motion_status.py
（写 runtime_files/motion_status.yaml，只在状态改变时写）——那边的数据源是 20Hz 的运动话题
加 MotionManager 的进程内记录，与这里的慢状态不是一套节奏，混在一个文件里只会互相拖累。
（tools.motion 判的只是那位写手还活没活，运动真值在 motion_status.yaml。）

**不做失效回落**（2026-09-21 按用户要求去掉）：任何字段的值一律是「最后一次读到的」，数据源
静默也不把它改写成 unknown —— 有效性由**各源自己的时间戳**表达，读方拿「现在 − 它」判断有
多旧，多久算不可用**由读方定**（本文件不代替读方设阈值）。这是刻意的：本文件是**转储**不是
**重读**，一份快照里本来就可能装着一个几小时前的缓存读数，与其把值丢掉，不如把年龄如实写出来。
  - robot：ROS 话题是**推的**，没有「读一次」这个动作，只能一直订阅收，写盘时倒出缓存里的最后
    一条。两个源频率差 150 倍（1Hz vs 约 12s 一帧），故**各给一个时间戳**：
      battery_updated_at ← /batteryStatus；device_updated_at ← /catch_turtle/device_state。
    别拿其中一个去衡量另一个的值——某一路断流时，另一路的时间戳照常前进，看不出来。
    该源从未收到过消息 → 字段 unknown + 它自己的时间戳 unknown。
  - system：本机现读的（拉的，采集时现读一次），updated_at 就是采集时刻，观测器在跑它就前进。
  - tools：**每个 tool 各带一条 updated_at**（不设段级时间戳——六个 tool 的判据来源各不相同，
    合一条会互相盖住，与 robot 段两个源同理），分两个家族：
      现读判据（camera / navigation）：= **采集时刻**，观测器在跑它就前进；
        这里的 unknown 表示**此刻判据取不到**（下一次采集可能就好）。
      痕迹判据（communication / audio / vision / motion）：= **拥有者最后一次留下痕迹的时刻**，
        可以很旧；这里的 unknown 表示**没有痕迹**（四格里只有 audio 可能走到：
        它事件驱动、静默是常态，一条事件都没来过时 updated_at 就是 unknown，
        而它的 status 那时报的是 unavailable —— 见下条）。
        另三格的心跳都是无条件周期写的，「没有痕迹」就等于「没在跑」，
        故它们都报 unavailable 而不是 unknown。
        communication 2026-09-21 从现读挪进了这一族：判据由「本观测器的父进程血缘」换成
        receiver 自己写的心跳 —— 连它一起想清楚的一件事是，父血缘报的是「我怎么被启动的」，
        不是「receiver 还在不在干活」。
    两个家族逐条写在渲染出来的注释列里。
时间戳只会是带引号的时刻或裸 unknown，**永不出现 null**（null 专表「已确认没有这个值」）。

与 vision_observer.py 的分工：那个看外部世界（相机 + 大模型），这个看机器狗自身。
与 motion_status.py 的分工：那个看运动/位姿/最近一笔运动（快、事件驱动），这个看慢状态。

数据源（2026-09-16 在真机上逐个实测确认，非文档推断）：

  /catch_turtle/device_state      DeviceState   ~0.08Hz  电池百分比/是否充电/CPU 温度
  /batteryStatus                  String(JSON)  1Hz      {"batteryLevel":87,"isCharging":0}
  /homi_speech/speech_assistant_status_topic  AssistantEvent  事件驱动  收音/播报状态机（→ tools.audio）

上面全是 ROS 话题；另有一个**本机来源**（非话题，每轮现读）：

  iw dev <网卡> link              本机子进程     每轮     当前无线网络名 + 信号强度

tools 段另外五项**不订阅任何话题**，判据是各组件自己的痕（误判场景逐条写在 TOOL_PROBES 里
每个 probe 的 docstring）：receiver / vision_observer 各自写的心跳文件（communication /
vision）、frame_producer 的跨进程 flock（camera）、motion_status.yaml 的 mtime（motion）、
导航后端的 HTTP 接口（navigation）。

运动相关的数据源（/catch_turtle/action_state、/leg_odom）不在这里，见 motion_status.py。

网络（system.network）：本地现读，不走 ROS。2026-09-20 在真机上实测过三条候选通路——
  nmcli 不可用（NetworkManager 未运行）；/network_status 无发布者（这正是当初把它标成
  「无实测通路」的原因），/internet_connect_status、/andlink_status、/network_recovery_location
  虽有发布者，但用匹配 QoS 的订阅者监听 12s 一条消息都收不到，故都不接；只有
  `iw dev wlan0 link` 稳定可读（实测输出 SSID: _vi / signal: -39 dBm），网卡名取
  /proc/net/wireless。`iwgetid -r` 也拿得到网名（实测 → _vi），但 iw 同一次输出还带信号
  强度，且「未连接」有明确文本 Not connected.，不必靠返回码猜，故取 iw。

speech_assistant_status 取值（tools.audio）：**三态，判据是最后一条事件的原始码**（2026-09-22 用户定）——
  一条事件都没来过 → unavailable；码在 configs.AUDIO_STATUS_NAMES 里（0~9，**整张码表**）→ available；
  码表外的码 → unknown（收到了消息，但不知道它是什么 = 接口漂移）。**边界就是码表本身**，别再
  引入第二个数字。快照里**不出码名、也不出码值**：码 → 名称只登记在 configs.AUDIO_STATUS_NAMES
  （别在这里再抄一份，这里曾手抄过、已经跟 configs 漂移过），它在这条路上只剩日志告警一个用途。
  **事件驱动：静默是常态** —— 最后一条事件一直有效到下一事件，值旧不等于故障（旧到多久算问题
  由读方拿 updated_at 自己定）；不做失效回落。

写什么、不写什么：
  只写**判据取得到**的状态。tools 的每一项都有实测判据（见 TOOL_PROBES），取不到就写 unknown，
  不拿别的来源凑一个看着像真的值，也不留占位值。
  unknown 的意思看它在哪段：robot 段（订阅来的，值会冻住）**只剩一个意思 —— 开机以来从没读到过**；
  system 段与 tools 段的现读判据是「此刻判据取不到」；tools 段的痕迹判据是「没有痕迹」。
  本观测器**不重启任何组件**：某个 tool 不是正常状态时它只报告，不会自愈。
  robot.battery_percent 只认 /batteryStatus，**不跨源回落到 device_state**：两个源各有自己的
  时间戳，拿另一路的值顶上会让值和它头上的时间戳来源不一致（读方以为看的是 1Hz 那路，实际是
  12s 那路的值）。两路各自如实记账，读方自己权衡。
  system.network 是每轮现读的本地源：读失败即 unknown，已确认未连接写 null ——
  null 和 unknown 不是一回事，别混。

原子写：临时文件 + rename，读方任何时刻读到的都是一份完整快照。

使用（必须先 source 运行中的 ROS 安装，否则 homi_speech_interface 的消息定义不对）：
    source /opt/ros/foxy/setup.bash
    source /usr/bin/cmcc_robot/install/setup.bash   # 运行中的安装；/root/cmcc_robot 是陈旧副本
    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp ROS_DOMAIN_ID=2
    python3 Observer/status_observer.py                 # 持续运行，模式同 vision_observer
    python3 Observer/status_observer.py --interval 300  # 换写盘周期（默认值见 configs.STATUS_WRITE_INTERVAL）
    python3 Observer/status_observer.py --once          # 写一次快照就退出（调试/定时任务）
    python3 Observer/status_observer.py --verbose       # 每轮把快照打到 stdout

一次采集的耗时：tools 探针最坏多花一个导航 HTTP 超时（configs.NAVIGATION_PROBE_TIMEOUT），
所以 --once 会因此多等最多那么多秒；主循环那边照旧容忍慢采集（写盘周期以 60s 计，无所谓）。

2026-09-21 起 tools.communication / tools.vision 改读两个**心跳文件**（configs.paths 的
RECEIVER_HEARTBEAT_FILE / VISION_OBSERVER_HEARTBEAT_FILE）：由 receiver（rclpy 定时器）和
vision_observer（主循环每轮）各自周期写，观测器只 stat 它们的 mtime。两个好处：
  ① **手动拉起本观测器也能正确报出这两格的死活**——旧判据读的是「谁把我拉起来的」，手动跑时
     一律 unknown，等于这两格在调试场景下完全失效；
  ② **「主循环卡住」抓得住了**——旧判据只证得了「那个进程还在」，证不了它还在干活。
这两格因此都没有 unknown（心跳无条件周期写，「没有心跳」就是「没在跑」）；tools.motion 那格
（账本兼心跳）2026-09-21 起也用同一条判词，于是三格口径一致：**心跳停住 = 写手不在了**。
详见 TOOL_PROBES 里各 probe 的 docstring。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set

# 确保项目根目录在 Python 搜索路径中（用于导入 configs 包）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String as RosString
    # DeviceState 是温度/电池的唯一来源，缺了本模块就活不了（与 rclpy 同一个包），故留在硬依赖里。
    from homi_speech_interface.msg import DeviceState
except ImportError as exc:
    print(f'[StatusObserver] ERROR: 无法导入 rclpy / homi_speech_interface（{exc}）')
    print('[StatusObserver] 提示: 先 source /opt/ros/foxy/setup.bash 与 '
          '/usr/bin/cmcc_robot/install/setup.bash')
    sys.exit(3)

# AssistantEvent 只服务 tools.audio，**单独一件 try**：包在位而消息定义版本漂移是真实存在的
# （/root/cmcc_robot 那份就是旧版），那种情况下只该让 audio 这一项退化成 unknown，不能把整份
# 快照（含 robot/system）一起带走。
try:
    from homi_speech_interface.msg import AssistantEvent
    _AUDIO_MSG_OK = True
except ImportError as _audio_exc:
    print(f'[StatusObserver] WARN: 无法导入 AssistantEvent（{_audio_exc}）'
          f'—— tools.audio 恒 unknown，其余段不受影响')
    AssistantEvent = None  # type: ignore[assignment]
    _AUDIO_MSG_OK = False

try:
    import psutil
except ImportError:  # 板载计算机的系统指标属于「有则记、无则 unknown」，不该拖垮整个观测器
    psutil = None

# 不设字面量兜底：周期与失效阈值只允许有一个来源，宁可在这里明确报错，
# 也不要再造一个会和 configs 漂移的"默认值"。
from configs.configs import (
    AUDIO_STATUS_NAMES,
    MOTION_STATUS_ALIVE_MAX_AGE_SEC,
    NAVIGATION_PROBE_TIMEOUT,
    NAVIGATION_STATUS_STATES,
    NAVIGATION_STATUS_URL,
    NETWORK_READ_TIMEOUT,
    RECEIVER_ALIVE_MAX_AGE_SEC,
    STATUS_TICK_SEC,
    STATUS_WRITE_INTERVAL,
)
# vision 判活阈值随观测周期走，而周期是**运行时可调**的（configs/vision_tunables.py），
# 所以阈值不能在这里固化成一个 import 来的常量 —— 每次判活现算（见 probe_vision）。
from configs import vision_tunables
from configs.paths import (
    FRAME_PRODUCER_LOCK_FILE,
    IW_BIN,
    RECEIVER_HEARTBEAT_FILE,
    RUNTIME_MOTION_STATUS,
    RUNTIME_ROBOT_STATUS,
    VISION_OBSERVER_HEARTBEAT_FILE,
    WIRELESS_IFACES_FILE,
)


# =============================================================================
# 常量
# =============================================================================

TOPIC_DEVICE_STATE = '/catch_turtle/device_state'
TOPIC_BATTERY_STATUS = '/batteryStatus'
TOPIC_SPEECH_STATUS = '/homi_speech/speech_assistant_status_topic'

# 快照里代表「读不到」的字面量：统一一个写法，消费方好判。
UNKNOWN = 'unknown'

# tools 段「在不在」的三态里另外两个词（第三个就是上面的 UNKNOWN = 判据本身取不到）。
# 这两个词只有本文件一个消费者（读方是人/LLM），与 UNKNOWN/NULL 同级，故不登记进 configs。
TOOL_AVAILABLE = 'available'      # 判据明确说「在」= 该组件的拥有者留下了踪迹
TOOL_UNAVAILABLE = 'unavailable'  # 判据明确说「不在」= 这条路曾经有过，现在没了

# 与 UNKNOWN 并列的另一种「没有值」：**已确认没有**（如没连上无线网，没有网名可报）。
# 两者必须分开——unknown 是「坏了/读不到」，null 是「确实没有」，混起来监督方就分不清
# 「网卡读不出来」和「机器狗没联网」。
NULL = 'null'

# 渲染时注释列的起始列（值短的行补空格对齐，读起来是一张表）
_COMMENT_COL = 40

# --once 模式下收消息的等待时长（秒）：够等到 1Hz 的 /batteryStatus
# （/catch_turtle/device_state 实测约 12s 一帧，单次运行收不到属正常，不是 bug）
_ONCE_SPIN_SEC = 1.5

# 时间戳格式（快照里各源的时间戳 / seen_wall 都用它）。
# **固定零填充的 %F %T，字典序就等于时间序** —— 读方直接比大小就对了，别改成 12 小时制或
# 去掉零填充，那样会**静默**比错先后。
_TS_FMT = '%Y-%m-%d %H:%M:%S'


# =============================================================================
# 辅助
# =============================================================================

def _now_str() -> str:
    return datetime.now().strftime(_TS_FMT)


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
    """时间戳等自由文本：带引号，None 仍是裸的 unknown。"""
    if value is None:
        return UNKNOWN
    return f'"{value}"'


def _num(value: Any) -> Optional[float]:
    """把消息里的数字转成 float；转不动就返回 None（不猜）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    """解析 JSON 字段的话题（batteryStatus）。解析不了返回 None。"""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# -----------------------------------------------------------------------------
# 网络（本机现读，非 ROS 话题；通路的实测依据见模块头部「网络」一节）
# -----------------------------------------------------------------------------

def _wireless_iface() -> Optional[str]:
    """/proc/net/wireless 里第一块无线网卡的名字；没文件/没网卡则 None。

    纯文件读，不起子进程。格式是内核固定的：两行表头 + 每行 `<网卡>: 状态 质量 电平 ...`
    （2026-09-20 实测本机为 `wlan0: 0000 70. -39. ...`）。多网卡时只取第一块——本机只有
    一块无线网卡，够用；真出现多块再定挑哪块的规则，现在不猜。
    """
    try:
        lines = WIRELESS_IFACES_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    for line in lines[2:]:  # 前两行是表头，跳过
        name = line.split(':', 1)[0].strip()
        if name:
            return name
    return None


def read_network() -> Optional[Dict[str, Any]]:
    """读当前无线联网状态。返回 None = **读不到**（渲染成 unknown），不是「没连上」。

    返回 dict 时一定有 connected；ssid / signal_dbm 允许为 None（连上了却没解析出名字）。

    判据（iw 5.4 的 `iw dev <网卡> link` 输出，2026-09-20 实测格式）：
      有 `SSID: <名>` 行 → 连上了，名字即该行冒号后的内容
      `Not connected.`   → 明确没连上，没有名字可报
      两者都没有        → 读不到
    失败一律回 None，判据取得宽：命令不存在、超时、非零返回码、输出不认识——都算读不到。
    宁可写 unknown，也不拿半条输出凑一个看着像真的状态。
    """
    iface = _wireless_iface()
    if not iface:
        return None
    try:
        proc = subprocess.run(
            [str(IW_BIN), 'dev', iface, 'link'],
            capture_output=True, text=True, timeout=NETWORK_READ_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):  # 命令不存在 / 超时 / 起不来
        return None
    if proc.returncode != 0:
        return None

    connected: Optional[bool] = None
    ssid: Optional[str] = None
    signal_dbm: Optional[float] = None
    for line in proc.stdout.splitlines():
        text = line.strip()
        if text.startswith('SSID:'):
            connected = True
            ssid = text.split(':', 1)[1].strip() or None
        elif text.startswith('Not connected'):
            connected = False
        elif text.startswith('signal:'):
            signal_dbm = _num(text.split(':', 1)[1].replace('dBm', '').strip())

    if connected is None:
        return None
    return {
        'connected': connected,
        'ssid': ssid,
        'interface': iface,
        'signal_dbm': signal_dbm,
    }


def _ssid_text(net: Optional[Dict[str, Any]]) -> str:
    """network 子块 → ssid 的渲染文本。三态分别对应三种写法：

      读到名字      → 带引号（SSID 可能是纯数字、含 #/: 或正好是 YAML 的关键字）
      已确认未连接  → null（确实没有名字）
      连上了却没名字 / 整块读不到 → unknown
    """
    if net is None:
        return UNKNOWN
    if net['ssid'] is not None:
        return _quoted(net['ssid'])
    return UNKNOWN if net['connected'] else NULL


# =============================================================================
# 订阅：只留最新值 + 最后一次收到消息的时刻
# =============================================================================

class StatusReader(Node):
    """订阅各状态话题，缓存最新值 + 记住**每个源**最后收到消息的时刻。

    这里**不判失效**（旧值不回落 unknown，见文件头）：回调只负责把值和时刻记下来，快照把它们
    如实写出去，新旧由读方拿时间戳自己算。故不存 monotonic 时刻——没有判据要用它。
    """

    def __init__(self) -> None:
        super().__init__('status_observer')
        self.device_state: Optional[DeviceState] = None
        self.battery: Optional[Dict[str, Any]] = None
        # 语音事件（tools.audio）：最后一条事件的**原始码**，不做任何过滤 —— 三态的判据在
        # probe_audio 里现算（码在码表里就算认得）。None = 事件驱动的话题一条都没来过
        # （静默是常态），那时报 unavailable。事件带的文本这次不进快照，故不缓存。
        self.speech: Optional[int] = None
        # 码表外的码见过哪些（只报警一次/每个码，避免每来一条就刷日志）：它们照旧参与三态判据
        self._speech_unknown_codes: Set[int] = set()

        # 每个源「最后收到消息」的时刻（墙钟文本）：随值一起进快照，一条源一条
        self.seen_wall: Dict[str, str] = {}

        self.create_subscription(DeviceState, TOPIC_DEVICE_STATE, self._on_device_state, 10)
        self.create_subscription(RosString, TOPIC_BATTERY_STATUS, self._on_battery, 10)
        # AssistantEvent 导入失败（版本漂移）就不订阅：tools.audio 那一支等于「一条都没来过」→
        # unavailable（与真静默同解，因为观测器确实无从知道语音链在不在），别的段不受影响
        if _AUDIO_MSG_OK:
            self.create_subscription(
                AssistantEvent, TOPIC_SPEECH_STATUS, self._on_speech_status, 10
            )

    # ---------- 记账 ----------

    def _mark(self, key: str) -> None:
        self.seen_wall[key] = _now_str()

    def last_seen_wall(self, key: str) -> Optional[str]:
        """该源最后收到消息的时刻（墙钟文本）；从没收到过 → None（渲染成 unknown）。

        一次只问一个源：robot 段两个源各记各的时刻，合并成一个会让断流的那一路看起来还活着。
        直接读 seen_wall，不另存一份（快照里写的就是这里记的）。
        """
        return self.seen_wall.get(key)

    # ---------- 回调 ----------

    def _on_device_state(self, msg: DeviceState) -> None:
        self.device_state = msg
        self._mark('device_state')

    def _on_battery(self, msg: RosString) -> None:
        data = _parse_json(msg.data)
        if data is not None:
            self.battery = data
            self._mark('battery_status')

    def _on_speech_status(self, msg: AssistantEvent) -> None:
        # 事件驱动话题：只在唤醒/识别/播报时才有消息，静默是常态，
        # 因此不做失效回落——最后一条事件一直有效到下一事件（新旧看它自己的时间戳）。
        # 这里**只记原始码**，不判三态（判在 probe_audio，那才是读快照的人要看的口径）。
        # 码表外的码照记不误（它就是 unknown 的来源），只额外报一次警 —— 真出现就是接口
        # 漂移了，值得进日志，但不该反复刷。
        code = int(msg.status)
        if code not in AUDIO_STATUS_NAMES and code not in self._speech_unknown_codes:
            self._speech_unknown_codes.add(code)
            print(f'[StatusObserver] WARN: 语音状态码 {code} 不在 AUDIO_STATUS_NAMES 里'
                  f'（接口漂移？）—— tools.audio 报 unknown', flush=True)
        self.speech = code
        self._mark('speech_status')


# =============================================================================
# tools 段：一 tool 一探针（**只读拥有者自己的痕**，不管别人的进程）
# =============================================================================
# 用户定的规矩：**谁拥有的进程由谁来监督、记录**。所以这一段里没有一次「找出别人的进程」、
# 没有一次抢锁、没有一次 open(别人的文件, 'w')，更没有重启任何东西 —— 六个探针读的全是该组件
# 自己留下的东西：
#
#   communication  receiver 自己写的 receiver.heartbeat（rclpy 定时器，每 RECEIVER_HEARTBEAT_SEC）
#   audio          语音助手自己发的那条状态话题
#   camera         frame_producer 自己持的那把跨进程 flock
#   vision         vision_observer 自己写的 vision_observer.heartbeat（主循环每轮无条件）
#   motion         motion_status 那个写手自己写的 motion_status.yaml 的 mtime
#   navigation     导航后端自己的 HTTP 接口
#
# 探针统一签名 probe_xxx(node)，返回值统一形状（见 _tool）：status 三态 + updated_at + 各自字段。
# **探针不该抛异常**（build_tools 那个兜底是防漏，不是许可）。status 与 updated_at 的含义见文件头。


def _tool(status: str, updated_at: Optional[str], **extra: Any) -> Dict[str, Any]:
    """探针的统一返回形状。**键序即渲染顺序**（status → 该 tool 自己的字段 → updated_at）。

    updated_at 由调用处给：现读判据给 _now_str()（= 采集时刻），痕迹判据给 _stamp_of(mtime)。
    """
    out: Dict[str, Any] = {'status': status}
    out.update(extra)
    out['updated_at'] = updated_at
    return out


def _mtime_of(path: Path) -> Optional[float]:
    """文件的 mtime（epoch 秒）；不存在/读不到 → None。

    **只 stat**：绝不 touch/重写别人的文件 —— 替别人写账本等于伪造别人的痕，会把真故障盖住。
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _age_sec(mtime: Optional[float]) -> Optional[float]:
    """mtime（epoch）距现在多少秒；None 原样返回 None（调用处渲染成 unknown）。"""
    if mtime is None:
        return None
    return max(0.0, time.time() - mtime)


def _stamp_of(mtime: Optional[float]) -> Optional[str]:
    """epoch → 快照里的时间戳文本（_TS_FMT）；None 原样返回 None。"""
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime).strftime(_TS_FMT)


def probe_communication(node: StatusReader) -> Dict[str, Any]:
    """communication：receiver 那个主循环还在不在转。

    判据是 **receiver 自己写的心跳**（configs.paths.RECEIVER_HEARTBEAT_FILE）——由
    Communicator/receiver.py 的 `_write_heartbeat` 通过 **rclpy 定时器**每
    RECEIVER_HEARTBEAT_SEC 写一次（为什么必须是定时器而不是独立线程，见 configs 里那条注释）。
      available   = mtime 年龄 ≤ configs.RECEIVER_ALIVE_MAX_AGE_SEC
      unavailable = 更旧，或文件不存在（从没写过 = receiver 从没跑起来过）

    **这一格没有 unknown**（2026-09-21 用户定）：心跳是无条件周期写的，所以「没有心跳」就是
    「没在跑」，不必再分「说不清」；`_mtime_of` 只在 OSError 时给 None（文件不存在 / stat 失败），
    两者在这里同归 unavailable。

    **判的是「主循环还在不在转」，不是「云端链路通不通」**：后者要发消息才知道，观测器不该发。
    也不要用 ROS 图里有没有 /agent_cloud_bridge 来替代：两棵部署并存时那个节点名两棵树都有，
    按图判会在「本仓 agent 根本没跑」时报 available。

    **不要退回去用「本观测器自己的父进程血缘」**（原实现，2026-09-21 用户定删除）：
    那条判据测的是「我是怎么被启动的」，不是「receiver 在不在干活」——agent 主进程活着而
    `rclpy.spin` 卡住时它照样报 available，而那恰是本项最该抓住的故障。心跳由定时器写、
    与订阅回调同在一个 executor 里被 spin 驱动，卡死会一起停，所以抓得住。
    父血缘还有第二个盲区：receiver 也可能被 `python3 Communicator/receiver.py` 独立拉起
    （那时 ppid 是 shell 而非 start_agent.py），心跳判据对两种起法一视同仁。
    连带删掉的 `_proc_cmdline` 是它唯一的消费者（本观测器**一次都不需要读别人的进程**）。
    """
    mtime = _mtime_of(RECEIVER_HEARTBEAT_FILE)
    age = _age_sec(mtime)
    if age is not None and age <= RECEIVER_ALIVE_MAX_AGE_SEC:
        return _tool(TOOL_AVAILABLE, _stamp_of(mtime))
    return _tool(TOOL_UNAVAILABLE, _stamp_of(mtime))


def probe_audio(node: StatusReader) -> Dict[str, Any]:
    """audio：语音助手那条状态话题**在不在报状态**（2026-09-22 用户定的三态）。

    拥有者是仓库外那条语音助手节点链，它的痕就是 /homi_speech/speech_assistant_status_topic
    上发的事件，故这里只读订阅缓存（不另找通路）。判据是**最后一条事件的原始码**：
      unavailable = 一条事件都没来过（静默是常态，但「从没说过话」就是没在干活）
      available   = 码在 configs.AUDIO_STATUS_NAMES 里（0~9 整张码表）：认得这条事件
      unknown     = 码表外的码：收到了消息，但不知道它是什么（接口漂移），故按读不到算
    **判据就是那张码表本身**（`in AUDIO_STATUS_NAMES`），没有第二套取值范围、也没有第二处边界
    数字 —— 码表加一条，这里自动跟着走。
    **不做失效回落**：最后一条事件一直有效到下一事件，值旧不等于故障，旧到多久算问题由读方拿
    updated_at 自己定。也**不记事件名**（这一段只要三态 + updated_at）；要查具体是哪个状态看
    ROS 话题本身。码 → 名称在这里只服务日志告警，不进快照。
    """
    if node.speech is None:
        return _tool(TOOL_UNAVAILABLE, None)
    status = TOOL_AVAILABLE if node.speech in AUDIO_STATUS_NAMES else UNKNOWN
    return _tool(status, node.last_seen_wall('speech_status'))


def probe_camera(node: StatusReader) -> Dict[str, Any]:
    """camera：帧生产者（skills/camera/frame_producer.py）那把跨进程单实例锁有没有被持有。

    拥有者是 agent 进程内的 frame_producer 单例，它的痕就是 runtime_files/frame_producer.lock
    上的一把 flock（进程一死由内核释放，故不存在「陈旧锁」）。这里只判「有没有人持锁」：
      available   = /proc/locks 里有一条 FLOCK+WRITE 落在同一个设备+inode 上
      unavailable = 锁文件在但无人持锁，或锁文件不存在（从没有生产者走到持锁那一步）
      unknown     = /proc/locks 读不到（容器/内核限制）

    **绝不「自己抢一次锁来探测」**：抢不到会让 frame_producer 的 start() 返回 None，start_agent
    随即判「帧生产者已被另一进程持有」并让整条 agent 启动链失败 —— 探针持锁只有几微秒，代价却是
    一次启动失败。所以只读 /proc/locks。
    锁文件里写的那个 pid **不读**（实测是陈旧值）；真持锁的 pid 从 /proc/locks 那一行现取
    （本探针不需要它，不留字段）。
    「有人持锁」= 生产者活着，**不代表帧在流**（相机被占也照样持锁）——帧的问题看 tools.vision。
    """
    now = _now_str()
    try:
        st = FRAME_PRODUCER_LOCK_FILE.stat()
    except FileNotFoundError:
        return _tool(TOOL_UNAVAILABLE, now)
    except OSError:
        return _tool(UNKNOWN, now)
    # /proc/locks 的设备是十六进制 major:minor，inode 是十进制 —— 两个都比（本机同时有别的
    # 进程的 flock，只比设备或只比 pid 会串味）。
    want = f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
    try:
        lines = Path('/proc/locks').read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return _tool(UNKNOWN, now)
    for line in lines:
        # 格式：`<id>: FLOCK  ADVISORY  WRITE <pid> <dev>:<ino> <start> <end> [EOF]`
        parts = line.split()
        if (len(parts) >= 6 and parts[1] == 'FLOCK' and parts[2] == 'ADVISORY'
                and parts[3] == 'WRITE' and parts[5] == want):
            return _tool(TOOL_AVAILABLE, now)
    return _tool(TOOL_UNAVAILABLE, now)


def probe_vision(node: StatusReader) -> Dict[str, Any]:
    """vision：视觉观测线程还在不在转。

    判据是 **vision_observer 自己写的心跳**（configs.paths.VISION_OBSERVER_HEARTBEAT_FILE）——
    它的主循环**每轮无条件写一次**（`_write_heartbeat`），不管那轮有没有新帧。
      available   = mtime 年龄 ≤ 判活阈值
      unavailable = 更旧，或文件不存在（线程从没进过观测循环）

    阈值**每次现算**：`vision_tunables.alive_max_age_sec()` = 3×(观测周期 + VISION_TIMEOUT)，
    规则本体在 configs.configs.vision_alive_max_age_sec()。观测周期是运行时可调的
    （configs/vision_tunables.py），所以这里**不能**固化一个 import 来的常量 —— 否则把人
    调的慢周期（如 6s）误判成 unavailable。现算的成本是一次 os.stat（mtime 门控缓存）。

    **这一格没有 unknown**：心跳无条件，所以「没有心跳」=「没在转」，不必再分「说不清」。

    2026-09-21 之前这里读的是 `vision_obs.yaml` 的 mtime，**只能报 available / unknown**：
    那份账本只在拿到新帧时才重写，mtime 停住分不出「线程死了」「没有新帧（相机被占或坏）」
    「大模型那一路卡住」，报 unavailable 是替它下结论。现在有了独立心跳，三种故障在判活这一
    层不必再分——它们都等于「视觉不在产出」，诊断细节去 `vision_obs.yaml` 的内容和自己的日志里找。
    **别再改回读账本的 mtime**：那就把「最后一次真观测」和「线程还活着」两件事又混成一个数。
    也**不要**拿「相机持锁」当判据（持锁证不了帧在流，而且相机那格已经有自己的判据了）。
    """
    mtime = _mtime_of(VISION_OBSERVER_HEARTBEAT_FILE)
    age = _age_sec(mtime)
    if age is not None and age <= vision_tunables.alive_max_age_sec():
        return _tool(TOOL_AVAILABLE, _stamp_of(mtime))
    return _tool(TOOL_UNAVAILABLE, _stamp_of(mtime))


def probe_motion(node: StatusReader) -> Dict[str, Any]:
    """motion：运动状态的写手（Motion_Manager/motion_status.py 那个线程）还在不在。

    拥有者的痕是 runtime_files/motion_status.yaml 的 mtime。那个写手是**状态改变即写 + 无条件
    心跳**（configs.MOTION_STATUS_HEARTBEAT_SEC），所以 mtime 停住确实等于它不在了：
      available   = 年龄 ≤ configs.MOTION_STATUS_ALIVE_MAX_AGE_SEC（= 3 个心跳，留两次余量）
      unavailable = 更旧，或文件不存在（那个线程从没写过：没起，或缺 ROS 包在 run() 开头就退出了）

    **没有 unknown**（2026-09-21 用户定，与 tools.communication / tools.vision 对齐）：心跳是无条件
    周期写的，所以「没有心跳」就是「没在跑」，不必再分「说不清」。改之前这里报 unknown，理由是
    「分不出没启动、和启动了但还没写下第一笔」—— 但心跳既然无条件，启动后最迟一个
    MOTION_STATUS_HEARTBEAT_SEC 内必然有第一笔，那个窗口小到不值得为它单留一个状态。
    三格现在同款判词：**心跳停住 = 那个写手不在了**。

    判的是**那位写手**，不是 MotionManager 单例本身（惰性导入、零痕迹，判不了）。
    运动真值在 motion_status.yaml 的 motion.idle / action_state_name，这里不复述。
    本机 CPU 常态高负载下线程可能迟一拍 → 偶发 unavailable，读方别据此立刻动作。
    """
    mtime = _mtime_of(RUNTIME_MOTION_STATUS)
    age = _age_sec(mtime)
    if age is not None and age <= MOTION_STATUS_ALIVE_MAX_AGE_SEC:
        return _tool(TOOL_AVAILABLE, _stamp_of(mtime))
    return _tool(TOOL_UNAVAILABLE, _stamp_of(mtime))


def probe_navigation(node: StatusReader) -> Dict[str, Any]:
    """navigation：导航后端此刻在什么状态（拥有者自己的 HTTP 接口）。

    解析口径复用 motion_status 那一套（问的是同一个 URL、同一件事）：
      status      = 枚举原样（IDLE / RUNNING / SUCCESS / FAILED / CANCELLED）
      unavailable = 连不上 / 超时（服务没在跑）
      unknown     = HTTP 非 200、code!=0、返回体不认识、枚举外的状态、requests 缺失

    **判的是导航服务在不在，不是导航能力可用**（定位丢了服务照答）。SUCCESS/FAILED/CANCELLED
    是**上一笔**的终态，不代表此刻有任务在跑 —— 只有 RUNNING 是在飞。
    requests 在**函数内** import：它缺失只该让这一项 unknown，不能把整份快照（含 robot/system）
    带走（模块顶部已经为 rclpy 付过一次退出的代价，不付第二次）。
    """
    now = _now_str()
    try:
        import requests
    except ImportError:
        return _tool(UNKNOWN, now)
    try:
        resp = requests.get(NAVIGATION_STATUS_URL, timeout=NAVIGATION_PROBE_TIMEOUT)
    except Exception:  # 连不上/超时/被拒 —— 服务不在，这就是 unavailable
        return _tool(TOOL_UNAVAILABLE, now)
    if resp.status_code != 200:
        return _tool(UNKNOWN, now)
    try:
        body = resp.json()
    except ValueError:
        return _tool(UNKNOWN, now)
    if not isinstance(body, dict) or body.get('code') != 0:
        return _tool(UNKNOWN, now)
    data = body.get('data')
    state = data.get('status') if isinstance(data, dict) else None
    state = str(state).strip().upper() if state is not None else ''
    if state not in NAVIGATION_STATUS_STATES:
        return _tool(UNKNOWN, now)
    return _tool(state, now)


# 六个 tool 的探针登记处。**键序 = 渲染顺序 = 用户手写的顺序**，也是快照里 tools 的键序；
# 加一个 tool 只在这里登记一次（采集、渲染、时间戳自检都按这份名单走）。
TOOL_PROBES = {
    'communication': probe_communication,
    'audio': probe_audio,
    'camera': probe_camera,
    'vision': probe_vision,
    'motion': probe_motion,
    'navigation': probe_navigation,
}

TOOL_NAMES = tuple(TOOL_PROBES)


# =============================================================================
# 快照组装
# =============================================================================

def build_robot(node: StatusReader) -> Dict[str, Any]:
    """robot 段：从 ROS 话题订阅来的本体状态（话题是推的，写盘时倒出缓存里的最后一条）。

    **不做失效回落**（见文件头）：值原样透传，源静默也照写，旧到什么程度由各自的时间戳说。
    两个源**各给一个时间戳**——它们频率差 150 倍，合用一个的话，断流的那一路会被另一路
    照常前进的时间戳盖住，读方看不出来。
      battery_percent / charging ← /batteryStatus（1Hz，JSON 文本）
      temperature_c              ← /catch_turtle/device_state（约 12s 一帧，温度的唯一来源）
    该源从没发过消息 → 值与该源时间戳一起 unknown。
    """
    # ---- 电池：只认 1Hz 的 /batteryStatus ------------------------------------
    # 不回落到 device_state 的同名值：那会让值与它头上的时间戳来源不一致（读方以为看的是
    # 1Hz 那路，实际是 12s 那路的值）。两路各自如实记账。
    battery_percent: Optional[float] = None
    charging: Optional[bool] = None
    if node.battery is not None:
        battery_percent = _num(node.battery.get('batteryLevel'))
        is_charging = node.battery.get('isCharging')
        if is_charging is not None:
            charging = bool(_num(is_charging) or 0)

    # ---- 温度：唯一来源 device_state -----------------------------------------
    temperature = None
    if node.device_state is not None:
        raw_temp = _num(node.device_state.cpu_temp)
        temperature = None if raw_temp is None else round(raw_temp, 1)

    return {
        'battery_percent': None if battery_percent is None else round(battery_percent),
        'charging': charging,
        'battery_updated_at': node.last_seen_wall('battery_status'),
        'temperature_c': temperature,
        'device_updated_at': node.last_seen_wall('device_state'),
    }


def build_system(node: StatusReader) -> Dict[str, Any]:
    """system 段：板载计算机自身——**全是本机现读**（psutil / iw），采集时读一次就是当下事实。

    与 robot 段相反：这里没有跨轮的缓存值，采到什么就是什么，所以不需要「最后一次读到的」
    那一套，也没有旧值可透传（掉线了就该反映出来）。
    """
    # ---- 本机系统指标 --------------------------------------------------------
    # cpu_percent 是「距上次调用」这段窗口的均值，窗口长度 = 写盘周期（见渲染里的注释）
    cpu_percent = memory_percent = None
    if psutil is not None:
        cpu_percent = round(psutil.cpu_percent(interval=None), 1)
        memory_percent = round(psutil.virtual_memory().percent, 1)

    # ---- 网络：采集时现读，不缓存 --------------------------------------------
    # 「此刻问一次系统」，读到什么算什么：读失败回 None → 渲染成 unknown。
    network = read_network()

    # 只要有一路出了值，这一份就是有效的，故 updated_at = 采集时刻（观测器在跑它就前进；
    # 本段不含 ROS 源，所以它的年龄就是「这份文件的本机部分有多新」）。三路全砸 → unknown。
    sampled = cpu_percent is not None or memory_percent is not None or network is not None

    return {
        'cpu_percent': cpu_percent,
        'memory_percent': memory_percent,
        'network': network,
        'updated_at': _now_str() if sampled else None,
    }


def build_tools(node: StatusReader) -> Dict[str, Dict[str, Any]]:
    """tools 段：六个 tool 各采一次（顺序 = TOOL_PROBES 的键序 = 渲染顺序）。

    一段一套判据 —— **谁拥有的进程由谁监督、记录**（见文件头与各 probe 的 docstring）。
    单个探针炸掉**不能把整份快照带走**（本文件的 mtime 是「观测器还活着」的唯一信号），
    故逐个 try：炸掉的那个 tool 全字段按 unknown 写 + 一行警告进 logs/status_observer.log。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for name, probe in TOOL_PROBES.items():
        try:
            out[name] = probe(node)
        except Exception as exc:  # 探针的锅不能连累快照
            print(f'[StatusObserver] WARN: tools.{name} 探针异常'
                  f'（{type(exc).__name__}: {exc}）—— 该项按 unknown 写', flush=True)
            out[name] = _tool(UNKNOWN, _now_str())
    return out


# 段名 → 段构建函数，**顺序即快照里的段序**（render 的模板按这个顺序写）。新增一段只在这里
# 登记一次：采集照这份名单走，渲染那边的时间戳对不对由 TIMESTAMP_KEYS 自检。
# （audio 不是一个段：它降级成了 tools.audio 一项，见 TOOL_PROBES。）
SECTION_BUILDERS = {
    'robot': build_robot,
    'system': build_system,
}

# 快照里各时间戳字段**应有**的条数（不是「各出现一次」了）：加段/加源/加 tool 时在这里登记
# 一次，漏写就由 render() 末尾的自检报错，不靠人眼。
# robot 段有两个源、频率差 150 倍，故两个时间戳各一条；system 一条；tools 段**每个 tool 各一条**
# （不设段级时间戳：六个 tool 的判据来源各不相同，合一条会互相盖住，与 robot 段两个源同理）。
TIMESTAMP_KEYS = {
    'battery_updated_at': 1,
    'device_updated_at': 1,
    'updated_at': 1 + len(TOOL_NAMES),
}


def _sample(node: StatusReader) -> Dict[str, Any]:
    """采一份完整快照：每段各采一次，外加 tools 段（六个探针每次现算，不缓存）。

    探针**只在这一步跑**（每写盘周期一次），不在 tick/spin 那条路径上 —— 其中导航那个是 HTTP
    请求（最坏等满 NAVIGATION_PROBE_TIMEOUT），不能挂到 0.2s 的 tick 上。
    """
    snap = {name: build(node) for name, build in SECTION_BUILDERS.items()}
    snap['tools'] = build_tools(node)
    return snap


# =============================================================================
# 渲染（带注释，覆盖写掉示例快照后字段含义不丢）
# =============================================================================

def render(snap: Dict[str, Any]) -> str:
    """快照 dict → 带注释的 YAML 文本。

    注释列按 _COMMENT_COL 对齐；值里含中文的行因终端宽度
    与字符数不等，会略有偏移，属预期。
    """
    r = snap['robot']
    s = snap['system']
    t = snap['tools']

    # 无线联网状态（system.network）：每轮现读的本地源，三种写法要分清——读到名字 /
    # 已确认未连接(null) / 读不到(unknown)。ssid 的引号在 _ssid_text 里就带上，kv 对
    # 字符串原样透传，故这里直接给渲染好的文本。
    net = s['network']
    net_connected = UNKNOWN if net is None else _scalar(net['connected'])
    net_ssid = _ssid_text(net)
    net_iface = UNKNOWN if net is None else _scalar(net['interface'])
    net_signal = UNKNOWN if net is None else _scalar(net['signal_dbm'])

    def kv(key: Any, value: Any, comment: str = '', indent: str = '  ') -> str:
        left = f'{indent}{key}: {_scalar(value)}'
        # 无注释的行不留尾空格（写入文件后才干净）
        if not comment:
            return left
        # 值比注释列还长时（如带中文的描述）也要**留一个空格**：YAML 里 `#` 前没有空白就
        # 不算注释，会被并进标量值里，读方拿到的就不是那个值了。
        return left + ' ' * max(_COMMENT_COL - len(left), 1) + comment

    def ts(key: str, stamp: Optional[str], note: str, indent: str = '  ') -> str:
        """时间戳行：**每个数据源各一条**，key 由调用处给（robot 段两个源 = 两条，
        tools 段六个 tool = 六条，各在自己那一块里）。

        取值格式统一（带引号的时间戳 / 裸 unknown，**永不 null**）；注释由调用处给——
        每条时间戳的含义不同，见文件头。
        """
        return kv(key, _quoted(stamp), f'# {note}', indent=indent)

    text = f"""\
# 机器狗状态快照 —— 由 Observer/status_observer.py 周期覆盖写入（临时文件 + 原子替换）。
# 取值 null 表示：已确认没有这个值（如没连上无线网，没有网名可报）—— 时间戳里永不出现 null。
# 取值 unknown 有两种意思，看它在哪一段：
#   robot 段（订阅来的，值会一直冻在最后一次）→ **开机以来从没读到过**；
#   system / tools 段的现读判据 → **此刻判据取不到**（下一次采集可能就好了）。
#
# **值一律是「最后一次读到的」，源静默也不会变成 unknown**（本文件不做失效回落）。判断一个值还
#   能不能用，看它自己那条时间戳：年龄 = 现在 − 它。**多久算过期由读方定**，本文件不设阈值。
#   robot 段是**订阅**来的（话题是推的，没有「读一次」这个动作）：断了值就冻在最后一次。
#     battery_updated_at / device_updated_at 是那两个话题**各自**最后收到消息的时刻 —— 两者频率
#     差 150 倍（1Hz vs 约 12s 一帧），**必须分开看**：某一路断了，另一路的时间戳照常前进，
#     所以别拿其中一个的时间去衡量另一个的值。
#   system 段是**每轮现读**的（拉的）：updated_at 就是采集时刻，观测器在跑它就前进。
#   tools 段**每个 tool 各带一条** updated_at（它们判据来源各不相同，合一条会互相盖住），
#     含义分两个家族，逐条写在下面：
#       现读判据（camera / navigation）= 采集时刻，观测器在跑它就前进；
#       痕迹判据（communication / audio / vision / motion）= 该组件的拥有者最后一次留下痕迹的
#         时刻，可以很旧（communication 2026-09-21 从现读挪进来：改读 receiver 的心跳文件）。
#   时间戳只会是带引号的时间戳或裸 unknown，不会是 null。
# 判观测器本身还活着看**文件 mtime**：本文件每 {STATUS_WRITE_INTERVAL:g}s 整份覆盖写一次，内容没变也照写，
#   所以判活的阈值必须 ≥ 2×{STATUS_WRITE_INTERVAL:g}s，否则会把正常的文件误判成「冻住」。
# **运动相关的状态不在这里** —— 见 runtime_files/motion_status.yaml（Motion_Manager/motion_status.py，
#   状态改变时才写），那是 motion.idle / action_state_name / last_motion / navigation.pose 的地方。

robot:                                 # 机器狗本体：从 ROS 话题**订阅**来的（推的，不是读的）
{kv('battery_percent', r['battery_percent'], '# 电池百分比；来自 /batteryStatus（1Hz）；低电量是典型告警')}
{kv('charging', r['charging'], '# 是否在充电；同上来源')}
{ts('battery_updated_at', r['battery_updated_at'], '↑ 这两个字段的数据时刻 = /batteryStatus 最后收到消息的时刻；从没收到过 → unknown')}
{kv('temperature_c', r['temperature_c'], '# CPU 温度；唯一来源 /catch_turtle/device_state（约 12s 一帧）；过温同样是告警')}
{ts('device_updated_at', r['device_updated_at'], '↑ 温度的数据时刻 = device_state 最后收到消息的时刻；与上面那条**各算各的**，别混')}

system:                                # 板载计算机自身：**每轮现读**的（拉的，非 ROS 话题）
{kv('cpu_percent', s['cpu_percent'], '# CPU 占用（psutil）；是「距上次采集」这段窗口的均值，窗口长度 = 本文件写盘周期（configs.STATUS_WRITE_INTERVAL）')}
{kv('memory_percent', s['memory_percent'], '# 内存占用')}
  network:                             # 本机无线联网状态；**采集时现读**（非 ROS 话题），命令 iw dev <网卡> link
{kv('connected', net_connected, '# 是否已关联无线网络；false = 明确没连上', indent='    ')}
{kv('ssid', net_ssid, '# 当前网络名；null = 已确认未连接，unknown = 读不到', indent='    ')}
{kv('interface', net_iface, '# 网卡名，取 /proc/net/wireless 里的第一块', indent='    ')}
{kv('signal_dbm', net_signal, '# 信号强度（负值，越接近 0 越强）；弱信号是掉线前兆', indent='    ')}
{ts('updated_at', s['updated_at'], '本段数据时刻 = 采集时刻（本机现读，观测器在跑它就前进）——本段无缓存值，无所谓陈旧')}

tools:                                 # 六个组件各自「在不在 / 什么状态」：**一段一套判据，谁拥有的进程由谁监督、记录**
  communication:                       # 判据：receiver 自己写的心跳 receiver.heartbeat（rclpy 定时器每 RECEIVER_HEARTBEAT_SEC 一次）
{kv('status', t['communication']['status'], '# available=心跳新鲜(≤3 个心跳周期)；unavailable=更旧或从没写过 —— 心跳走定时器，executor 被回调卡死会一起停，所以停住就是主循环不转了', indent='    ')}
{ts('updated_at', t['communication']['updated_at'], '↑ 痕迹判据：receiver 最后一次写心跳的时刻；unknown = 从没有过心跳', indent='    ')}
  audio:                               # 判据：语音助手自己发的那条话题 /homi_speech/speech_assistant_status_topic（AssistantEvent）
{kv('status', t['audio']['status'], '# unavailable=一条事件都没来过；available=最后一条事件的码认得(在 configs.AUDIO_STATUS_NAMES 里，0~9)；unknown=码表外的码(接口漂移)', indent='    ')}
{ts('updated_at', t['audio']['updated_at'], '↑ 痕迹判据：最后一次事件的时刻。**事件驱动：静默是常态，旧不等于故障**（旧到多久算问题由读方定）', indent='    ')}
  camera:                              # 判据：frame_producer 自己持的那把跨进程 flock（读 /proc/locks，不抢锁、不 open 锁文件）
{kv('status', t['camera']['status'], '# available=有人在持锁=抓拍在跑；unavailable=没人持锁或锁文件不存在；unknown=/proc/locks 读不到', indent='    ')}
{ts('updated_at', t['camera']['updated_at'], '↑ 现读判据：本行 = 采集时刻；unknown = 此刻判据取不到', indent='    ')}
  vision:                              # 判据：vision_observer 自己写的心跳 vision_observer.heartbeat（主循环每轮无条件写）
{kv('status', t['vision']['status'], '# available=心跳新鲜(≤3 轮)；unavailable=更旧或从没写过 —— 心跳有没有新帧都写，停住就是线程不在转(死了/卡在取帧或大模型那一路)', indent='    ')}
{ts('updated_at', t['vision']['updated_at'], '↑ 痕迹判据：vision_observer 最后一次写心跳的时刻；unknown = 从没有过心跳', indent='    ')}
  motion:                              # 判据：motion_status 那位写手自己写的 motion_status.yaml 的 mtime（状态改变即写 + 无条件心跳）
{kv('status', t['motion']['status'], '# available=刚写过(≤3 个心跳)；unavailable=更旧或文件不存在 —— 心跳无条件，停住就是写手不在了，故与 communication/vision 同款：没有 unknown', indent='    ')}
{ts('updated_at', t['motion']['updated_at'], '↑ 痕迹判据：最后一次写盘的时刻。运动真值**不在这里**，见 motion_status.yaml 的 motion.idle / action_state_name', indent='    ')}
  navigation:                          # 判据：导航后端自己的 HTTP 接口（GET configs.NAVIGATION_STATUS_URL，超时 configs.NAVIGATION_PROBE_TIMEOUT）
{kv('status', t['navigation']['status'], '# 枚举原样：IDLE/RUNNING/SUCCESS/FAILED/CANCELLED；unavailable=连不上或超时(服务没在跑)；unknown=非200/code!=0/枚举外的值/requests 缺失', indent='    ')}
{ts('updated_at', t['navigation']['updated_at'], '↑ 现读判据：本行 = 采集时刻。SUCCESS/FAILED/CANCELLED 是**上一笔**的终态，只有 RUNNING 是在飞', indent='    ')}
"""

    # 时间戳行漏一条，读方就少一个判陈旧的依据 —— 值已经不靠 unknown 兜底了，全靠它们。这个
    # f-string 是手写的，这种漏最不容易肉眼看出来，所以照 TIMESTAMP_KEYS 逐条数一遍：不靠人眼。
    lines = [line.strip() for line in text.splitlines()]
    for key, expected in TIMESTAMP_KEYS.items():
        n = sum(1 for line in lines if line.startswith(f'{key}:'))
        if n != expected:
            raise AssertionError(
                f'render() 里 {key} 出现了 {n} 次（应为 {expected} 次，见 TIMESTAMP_KEYS）—— '
                f'检查是不是漏了某条时间戳，或把它写进了注释行'
            )
    return text


def write_snapshot(text: str) -> None:
    """原子写入快照（临时文件 + rename，读方读到的永远是完整的一份）。

    **内容与上一份相同也照写**：文件 mtime 是「观测器还活着」的唯一信号。别加「内容没变就
    跳过」的优化 —— 那会让文件看起来停在上一次真的变化时，判活的消费方（tests/motion_watch.py
    早先就是这么判的）会把它当成观测器死了。代价是判活的时间粒度就是这个写盘周期：消费方的
    失效阈值必须 ≥ 2×configs.STATUS_WRITE_INTERVAL。
    """
    RUNTIME_ROBOT_STATUS.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_ROBOT_STATUS.with_suffix('.yaml.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(RUNTIME_ROBOT_STATUS)


# =============================================================================
# 主循环
# =============================================================================

def _summarize(snap: Dict[str, Any]) -> str:
    r = snap['robot']
    s = snap['system']
    # 值后面带上它自己的时刻：不做失效回落之后，不带时刻就看不出这条日志里的值有多旧
    text = (
        f"电量 {_scalar(r['battery_percent'])} @{_quoted(r['battery_updated_at'])} "
        f"温度 {_scalar(r['temperature_c'])} @{_quoted(r['device_updated_at'])} "
        f"网络 {_ssid_text(s['network'])}"
    )
    # tools 段只列**明确不好**的那两个状态：unavailable（判据说不在）与 unknown（判据取不到），
    # 全好时这一句不出现，日志里一眼看出哪条断了。
    # **不要写成「列出所有不是 available 的」**：navigation 的 status 是枚举（IDLE/RUNNING/… 里
    # 有些是「在位且正常」的信息性取值），按白名单挑会把它们每轮都当成坏的报出来。
    bad = [
        f'{name}={snap["tools"][name]["status"]}'
        for name in TOOL_NAMES
        if snap['tools'][name]['status'] in (TOOL_UNAVAILABLE, UNKNOWN)
    ]
    if bad:
        text += ' | tools: ' + ' '.join(bad)
    return text


def _spin_for(node: StatusReader, seconds: float, step: float = 0.05) -> None:
    """在 seconds 秒内持续 spin_once 收消息（主循环与 --once 共用）。

    订阅是异步的：各段的数据都从回调这儿进缓存，不 spin 就会积压。step 是单次 spin_once 的
    上限——收不到消息时它空转一次就回来，好让外层能看着表往下走。
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return
        rclpy.spin_once(node, timeout_sec=min(step, remaining))


def run(interval: float = STATUS_WRITE_INTERVAL, once: bool = False,
        verbose: bool = False) -> None:
    """按 interval 秒周期采集并覆盖写快照，直到被中断。

    主循环**必须**一直 spin（哪怕写盘周期是几分钟）：剩下的两个 ROS 源也都是**订阅**话题
    （/batteryStatus 1Hz、/catch_turtle/device_state 约 12s 一帧），消息只在回调里进缓存，
    等到该写盘了再收一轮是收不到的。所以 STATUS_TICK_SEC 不是「写盘周期的步进」而已，
    它是**收消息**的循环——audio 停用与它无关，这个循环照样不能省。
    """
    rclpy.init()
    node = StatusReader()

    # psutil 首次 cpu_percent() 返回的是自进程启动以来的均值（无意义），先预热一次
    if psutil is not None:
        psutil.cpu_percent(interval=None)

    print(f'[StatusObserver] 启动，写盘 {interval:g}s，tick {STATUS_TICK_SEC:g}s，'
          f'写入 {RUNTIME_ROBOT_STATUS}', flush=True)
    try:
        if once:
            # --once 是调试/定时用：先多等一会让 1Hz 的 /batteryStatus 有机会进来
            # （/catch_turtle/device_state 实测约 12s 一帧，单次运行收不到属正常，不是 bug），
            # 然后采一份写掉就退。
            _spin_for(node, _ONCE_SPIN_SEC)
            snap = _sample(node)
            write_snapshot(render(snap))
            print(f'[StatusObserver] {_now_str()} -> {_summarize(snap)}', flush=True)
            return

        next_write = time.monotonic()
        while True:
            # 一个 tick：先收一轮消息（各段的数据都从订阅回调这儿进缓存），到点就采一份写掉。
            _spin_for(node, STATUS_TICK_SEC)
            now = time.monotonic()
            if now >= next_write:
                snap = _sample(node)
                write_snapshot(render(snap))
                if verbose:
                    print(f'[StatusObserver] {_now_str()} -> {_summarize(snap)}', flush=True)
                next_write += interval
                if next_write <= now:
                    # 落后太多就重新对齐，不补写。慢的来源在采集那一步：system 的 iw 子进程
                    # （最坏等满 NETWORK_READ_TIMEOUT），以及 DDS 本身。
                    next_write = now + interval
    finally:
        node.destroy_node()
        rclpy.shutdown()


# =============================================================================
# CLI 入口
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description='机器人状态观测器（写 robot_status.yaml）')
    parser.add_argument(
        '--interval',
        type=float,
        default=STATUS_WRITE_INTERVAL,
        help=(f'快照写盘周期（秒），默认 {STATUS_WRITE_INTERVAL}（configs.STATUS_WRITE_INTERVAL）。'
              f'它同时是判活的时间粒度：消费方的失效阈值须 ≥ 2×它。'
              f'运动状态不在这里，见 motion_status.yaml'),
    )
    parser.add_argument('--once', action='store_true', help='写一次快照就退出')
    parser.add_argument('--verbose', '-v', action='store_true', help='每轮把摘要打到 stdout')
    args = parser.parse_args()
    # 0 或负数会让主循环的 next_write 永不到期（0）或倒着跑，症状是「跑着但不写文件」，
    # 而消费方只看文件 mtime——那种失败最难查，所以在入口就拦掉。
    if args.interval <= 0:
        parser.error(f'--interval 必须大于 0，收到 {args.interval:g}')

    try:
        run(interval=args.interval, once=args.once, verbose=args.verbose)
    except KeyboardInterrupt:
        print('\n[StatusObserver] 已停止', flush=True)


if __name__ == '__main__':
    main()
