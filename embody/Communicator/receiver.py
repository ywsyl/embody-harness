"""Communicator/receiver.py —— 云端 SIGCEvent → rolebus 事件 的薄桥（组件侧）

云端下发的**任务内容 / 用户回答 / 干涉**只有走到**角色系统**里才算数，而这个进程是
start_agent.py 起的组件进程，与角色进程之间唯一的现成通路是 rolebus 的共享 spool。所以本
模块只做下面四件事（另有一条不经角色系统的直接执行腿，见后）：

  1. 订阅 SpeechCore 的 SIGCEvent 话题（`ROS_SIGC_EVENT_TOPIC`），解析云端下发的事件；
  2. 去重（`eventId`）+ 接住父类的凭证请求（credential_get）；
  3. 按 `CLOUD_ACTION_MAP` 把 `body.action` 归一成**端侧动作**（云端 create / modify /
     resume → `update`，interrupt / cancel → `stop`；云端 answer / info 端侧不处理），再交给
     `plugin_tools/rolebus.py` 的 `send_cloud` 投成一条 rolebus 云事件（按版本定目标角色：
     `cloud_update` 投 interactor、`planner_cloud_update` 投 planner，由它现判；两个端侧动作
     都走这一条事件，差别只在 `payload.action`）。answer
     原先另把用户回答原文写进落盘单槽（Communicator/pending_answer.py），供角色侧的
     `listen` / `speak_listen` 取走；**那条腿 2026-09-28 起暂时注释掉了**，且 answer 自本次起
     端侧不再处理（见 `UNHANDLED_ACTIONS`）。读侧那两个工具 2026-10-08 也改走本地 ASR 收音，
     所以这条链路**两端都已停用**，恢复要连着 plugin_tools/audio_tools.py 里注释掉的
     `_await_cloud_answer` 一起放开；
  4. 写存活心跳（Observer/status_observer.py 判 `tools.communication` 活着的唯一依据）。

**另一条腿：`exe` 事件**（`EVENT_EXE`）—— 云端直接点名执行一个工具，**不经角色系统、不叫模型**：
body 里给 `tool_name` + `parameters`，本模块当场按 `plugin_tools/available_tools_cloud.yaml`
（`AVAILABLE_TOOLS_CLOUD_FILE`）那份清单执行（执行器复用 plugin_tools/tool_executor.py）。
上面第 3 条是「把云端的**意图**交给人角色去理解」，这一条是「让这台机器**照做**一个动作」，
两者不是一回事，别合并。工具在**独立线程**里跑，理由见 `_run_exe`。

每条云端下发的 `robot_agent_task` 与 `exe` 还会**照原文打一行**（正文前缀 = configs 的
`CLOUD_EVENT_LOG_TAG`）：端侧不处理的 info / answer、未知动作、清单外的工具名这几条路径都走不到
「已投 rolebus」/「执行结束」那条回执，只留结论的话「云端到底说了什么」就无处可查。这一行的格式与
其余行不同（只有时刻 + 正文，不带 logger 名与级别，见 `_CloudEventFormatter`），starter.sh 按那个
前缀把它从组件日志里筛到终端。原先「每条事件都打一行 `📋 事件: event=…`」的通用轨迹已删——
robot_agent_task / exe 就是上面那两条，其余几类只留 debug 级说明。

**不做**的事（这些在本树里已归角色系统，对照 cmcc 单智能体树逐条删掉的）：

  · 起 task_loop 线程跑任务、维护任务池、忙碌时 TTS 拒绝、终态上报 —— 任务是角色的，
    云端看到的进展由编排器上报（`Communicator.sender.send_task_progress`），桥不代答；
  · 「继续任务」spawn 子进程（`Agents/task/task_loop.py` 在本树不存在）；resume 现在与
    create / modify 一样归一成 `update` 投给角色，由角色决定怎么续；
  · info 事件落 `interaction_history.yaml`、受理/拒绝的 ACK（task_received 等）；
  · `cloud_service_response` 的 pending_requests（本树里没有发起方）；
  · 退出时补报终态、停运动：启动前的清理统一在 Communicator/startup_cleanup.py，退出时的
    收尾归各组件自己（原先这里顺手做的那些都是 task_loop 时代的残留）。

投递失败（spool 不可写、event_id 撞车、动作不在值域里）只记一条 error 并返回：ROS 回调
线程不能被一次投递异常带走，否则后面所有云端事件都进不来。

`--test-send` 已随上报通路一并删除；要模拟云端下发用 `Communicator/cloud_event_sim.py`
（走同一个 ROS 话题，见该脚本头部）。
"""

import sys
from pathlib import Path

# cmcc_agent 包根目录（reference/paths 等）
_CMCC_ROOT = Path(__file__).parent.parent.resolve()
if str(_CMCC_ROOT) not in sys.path:
    sys.path.insert(0, str(_CMCC_ROOT))

import json
import logging
import os
import signal
import threading
from datetime import datetime
from typing import Any, Dict

import rclpy

from claw_client.claw_client_node import SpeechCoreClientNode
from claw_client.logging_setup import setup_logging
from configs.configs import CLOUD_EVENT_LOG_TAG, RECEIVER_HEARTBEAT_SEC
from configs.paths import DEVICE_ID, RECEIVER_HEARTBEAT_FILE, ROS_SIGC_EVENT_TOPIC

# 必须用包路径导入：`from pending_answer import ...` 只在「以脚本方式运行」时成立
# （python3 Communicator/receiver.py → sys.path[0] 是脚本所在目录 Communicator/），
# 被 start_agent.py 以 `from Communicator.receiver import main` 导入时会 ImportError。
# 包路径两种起法都认（本文件顶部已把仓库根插进 sys.path）。
from Communicator.pending_answer import submit as submit_pending_answer
from plugin_tools.rolebus import send_cloud
from plugin_tools.tool_executor import ToolExecutor
from plugin_tools.tool_parser import load_cloud_entries

from homi_speech_interface.msg import SIGCEvent


# =============================================================================
# 通信协议常量
# =============================================================================

DOMAIN_AGENT = "AGENT"

# ---- 云端下发事件（cloud → robot）---------------------------------
EVENT_ROBOT_AGENT_TASK = "robot_agent_task"   # 端侧智能体任务指令（VSS 平台透传）
# 云端点名执行一个工具（见文件头「另一条腿」）。body 形状：
#   {"tool_name": "<available_tools_cloud.yaml 里的名字>", "parameters": {…}}
# 字段名是云端契约：tool_name 必填、parameters 可省（缺省空参数），改名要两边一起改。
EVENT_EXE = "exe"

# ---- 云端下发动作（robot_agent_task 的 body.action）= 云端词表 ------
# 见《端侧智能体接口文档》：create / modify / interrupt / answer / info（+ 旧网关
# cancel / resume）。这七个词是**云端**的，端侧不透传它们：`CLOUD_ACTION_MAP` 是唯一一处
# 「云端词 → 端侧词」的映射，云端改词只动这张表。
ACTION_CREATE    = "create"      # 创建新任务（云端下发任务内容）
ACTION_MODIFY    = "modify"      # 修改当前任务
ACTION_INTERRUPT = "interrupt"   # 干涉当前任务（暂停/停止/恢复）
ACTION_ANSWER    = "answer"      # 用户回答端侧主动发问（原文回传，不解析语义）
ACTION_INFO      = "info"        # 对话通知（云端接管本轮）

# ---- 旧网关兼容动作（新协议已并入 interrupt）-----------------------
ACTION_CANCEL    = "cancel"      # 兼容别名，等价 interrupt
ACTION_RESUME    = "resume"      # 兼容别名，继续历史任务

# ---- 端侧动作（投给角色系统的那一套，rolebus 云事件的值域）---------
# 只有两个：内容类的 update 与控制类的 stop。payload.action 与日志里出现的都是它们，
# 下游（角色、模型）看不到云端词。
ACTION_UPDATE    = "update"      # 任务内容有交代：新建 / 修改 / 继续
ACTION_STOP      = "stop"        # 停止当前任务

#: 云端词 → 端侧词。**唯一**的映射表；create / modify / resume 在端侧同义。
CLOUD_ACTION_MAP = {
    ACTION_CREATE:    ACTION_UPDATE,
    ACTION_MODIFY:    ACTION_UPDATE,
    ACTION_RESUME:    ACTION_UPDATE,
    ACTION_INFO:      ACTION_UPDATE,
    # ACTION_ANSWER:      ACTION_UPDATE,
    ACTION_INTERRUPT: ACTION_STOP,
    ACTION_CANCEL:    ACTION_STOP,
}

#: 认识但端侧不处理的云端动作：既不投事件、也不算「未知动作」，只记一条说明日志。
UNHANDLED_ACTIONS = (ACTION_ANSWER, ACTION_INFO)

#: 本模块认识的全部云端动作，其余一律按"未知动作"告警丢弃。
KNOWN_ACTIONS = tuple(CLOUD_ACTION_MAP) + UNHANDLED_ACTIONS

# ---- 云端服务响应（cloud → robot）---------------------------------
# 本树不再处理它（没有发起方），常量保留给云端事件模拟器与旧网关照抄。
EVENT_CLOUD_SERVICE_RESPONSE = "cloud_service_response"

# ---- 端侧事件（robot → cloud）：本模块不再上报，常量留给模拟器 ------
# 事实源是云端那套接口文档；这里只保证 cloud_event_sim.py 还能 import 到同一个域名字符串。
EVENT_LOCAL_TASK_RECEIVED   = "task_received"
EVENT_LOCAL_TASK_STARTED    = "task_started"
EVENT_LOCAL_TASK_PROGRESS   = "task_progress"
EVENT_LOCAL_TASK_COMPLETED  = "task_completed"
EVENT_LOCAL_TASK_FAILED     = "task_failed"
EVENT_LOCAL_TASK_CANCELLED  = "task_cancelled"
EVENT_LOCAL_CLOUD_SERVICE_REQUEST = "cloud_service_request"


def _body_task_id(body: Dict[str, Any]) -> str:
    """取云端下发的任务 id（全链路 task_id 的唯一来源）。

    见《端侧智能体接口文档》body.task_id：云端 create 时生成，端侧认领后沿用；
    modify / interrupt / answer 携带同一 id 用于定位任务，info 时为空字符串。
    **取不到就返回空串**：占位（`cloud-<Unix 秒>`）由 plugin_tools/rolebus.py 在投递时补，
    本模块不再自造一份时间戳兜底（两份兜底就会有两套任务号）。
    兼容旧字段名 cloud_id（旧网关）。
    """
    if not isinstance(body, dict):
        return ""
    value = body.get("task_id") or body.get("cloud_id") or ""
    return str(value).strip()


def _last_user_content(messages: Any) -> str:
    """从 messages 中取最后一条 role=user 且 content 非空的消息内容。

    与云端 answer 事件一致：按列表顺序迭代，后出现的 user 消息覆盖前面的，
    得到"最新/最后一条用户意图"。content 统一转 str（兼容多模态列表形式）。
    无 user 消息时返回空串。
    """
    content = ""
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                value = msg.get("content", "")
                if value:
                    content = str(value)
    return content


def _single_line(text: Any) -> str:
    """把云端下发的原文压成单行：连续空白（含换行）并成一个空格，两端去干净。

    这段文字要进事件 payload、日志与 pending_answer 单槽，带换行的话在日志里读不出边界、
    在单槽里也只是一行 JSON 里的转义。只压空白，**不删字、不改标点**（云端原文照传是本桥
    的约定）。
    """
    return " ".join(str(text or "").split())


def _description_of(action: str, body: Dict[str, Any]) -> str:
    """云端动作 → 投给角色的说明文字（`payload.description`，自然语言原文，不解析语义）。

    按**云端动作**取字段——body 的形状由云端动作定，与端侧把它归一到哪个动作无关（都先过
    `_single_line`）：

      · create / modify / resume —— 任务描述：body.task_description（文档里是 32B 二次验证
        的输出，权威），空则回退 messages 里最后一条 user 消息；
      · interrupt / cancel —— 干涉理由：body.reason，缺省给一句固定说法。

    answer / info 端侧不处理（见 `UNHANDLED_ACTIONS`），走不到这里。取不到就说清楚为什么没投
    （见 `_forward_action`），不替云端编内容。
    """
    if action in (ACTION_CREATE, ACTION_MODIFY, ACTION_RESUME):
        return _single_line(body.get("task_description")) or _single_line(
            _last_user_content(body.get("messages", []))
        )

    if action in (ACTION_INTERRUPT, ACTION_CANCEL):
        return _single_line(body.get("reason")) or "云端终止任务"

    return ""


# =============================================================================
# 日志渲染
# =============================================================================

class _CloudEventFormatter(logging.Formatter):
    """只给「云端下发」那几行换一种写法：`时刻 -  正文`，不带 logger 名与级别。

    setup_logging 给的两个 handler 都挂着「时刻 - 日志名 - 级别 - 正文」那套格式（file 那份还多一段
    `[文件名:行号]`），而 Formatter 是**按 handler** 生效的、不能按记录换，所以这里包一层：只有带
    `cloud_event` 标记的记录（`_log_cloud_event` 打的那些）换，其余原样交回给 setup_logging 那份——
    本模块的启停、告警、转发回执的行一个都不变。

    两个 handler 都包（终端 stdout 与 /var/log/cmcc_robot/agent_task_receiver.log），所以同一条云端
    下发在终端与日志文件里长得一模一样，复制粘贴出去能直接对上。
    """

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, 'cloud_event', False):
            return self._inner.format(record)
        # 时刻带毫秒（与其余行同一口径，datefmt 取内层那份的），后面直接接正文。
        stamp = self.formatTime(record, self._inner.datefmt)
        return f"{stamp}.{int(record.msecs):03d} -  {record.getMessage()}"


# =============================================================================
# 核心类
# =============================================================================

class AgentCloudBridge(SpeechCoreClientNode):
    """云端事件 → rolebus 的桥（跑在组件进程里的 ROS 节点）。

    只干文件头那几件事：订阅、去重与凭证应答、转发、云端点名执行、心跳。不持有任务、不认识
    task_loop，也没有任何跨进程状态 —— 角色系统那边唯一的接口就是 spool 与 pending_answer 单槽。

    唯一持有的重物是 `_cloud_executor`（那份云端工具清单的执行器，见 `_handle_exe`）。
    """

    def __init__(self, node_name: str = "agent_cloud_bridge") -> None:
        super().__init__(node_name)
        # 这里原先有一句 exit_tasks("手动终止")：起节点时顺手停一遍运动。
        # 2026-09-21 删除 —— 启动前的清理统一在 Communicator/startup_cleanup.py（由统一
        # 启动入口调用），同一件事不留两处；不经 start_agent 直接起本模块时不再自动停运动。

        console_level = self.get_parameter('console_log_level').value.upper()
        file_level    = self.get_parameter('file_log_level').value.upper()
        self.logger, self.file_logging_enabled, self.log_file_path = setup_logging(
            'agent_task_receiver', console_level, file_level
        )
        # 「云端下发」那几行换成短格式（只有时刻 + 正文，见 _CloudEventFormatter）：包在 setup_logging
        # 装好的 Formatter 外面，两个 handler 都包 —— 终端那份与日志文件那份因此长得一模一样。
        # 文件写不了时 setup_logging 只装 console 一个，这里按现有的收，不假设有两个。
        for handler in self.logger.handlers:
            if handler.formatter is not None:
                handler.setFormatter(_CloudEventFormatter(handler.formatter))

        self.device_id = DEVICE_ID

        # ---- 云端点名执行（exe 事件）的工具面 ---------------------------
        # 白名单 = available_tools_cloud.yaml 那份清单（configs/paths.py 的
        # AVAILABLE_TOOLS_CLOUD_FILE），与角色系统的白名单是两份、互不影响（见文件头）。
        # 执行器**在构造期一次性注册**：清单里出现「既不是内置工具、也没有 extra.execute」的
        # 条目时 register_from_yaml 当场抛 —— 配置错误在这里出声，而不是等一条 exe 事件进来
        # 才发现（清单读不到时 load_cloud_entries 只出声、返回空表，一并在这里按失败拦住：
        # 空注册表会让后面每一条 exe 都被当成"工具名不对"，那是误导）。
        cloud_entries = load_cloud_entries()
        if not cloud_entries:
            raise RuntimeError(
                "读不到 plugin_tools/available_tools_cloud.yaml（云端点名执行认的就是那份清单）"
                "，exe 事件这条腿起不来"
            )
        self._cloud_executor = ToolExecutor(cloud_entries)
        self.logger.info(
            f"[Receiver] 云端点名执行已就绪: {len(cloud_entries)} 个工具"
            f"（{'、'.join(e['function']['name'] for e in cloud_entries)}）"
        )

        # ---- 接收侧：订阅 SpeechCore 下发的平台事件 --------------------
        self.sigc_event_sub = self.create_subscription(
            SIGCEvent,
            ROS_SIGC_EVENT_TOPIC,
            self._on_sigc_event,
            10,
        )
        self.logger.info(f"[Receiver] 订阅话题: {ROS_SIGC_EVENT_TOPIC}")

        # ---- 已处理事件 ID（防重复） ------------------------------------
        self.processed_event_ids: set = set()
        self._processed_ids_max_size = 10000

        # ---- 存活心跳 ---------------------------------------------------
        # 周期写一个只含时刻+pid 的文件，供 Observer/status_observer.py 判
        # 「receiver 还在不在转」（robot_status.yaml 的 tools.communication）。
        # **必须走 rclpy 定时器，不要改成独立线程**：定时器回调与订阅回调同在一个 executor
        # 里被 spin 驱动，某个回调卡死时心跳会跟着停 —— 那正是「主循环不再转」的信号。
        # 独立线程的心跳独立于 executor，只证得了「进程还活着」，证不了 receiver 还在干活
        # （先前的判据是「本观测器的父进程血缘」，栽的就是这一点：agent 主进程活着但 spin
        # 卡住时它照样报 available）。
        # 定时器由 main() 里那个 rclpy.spin(node) 驱动；节点销毁时随节点一起清掉（见 destroy_node）。
        self._heartbeat_timer = self.create_timer(
            RECEIVER_HEARTBEAT_SEC, self._write_heartbeat
        )

        self.logger.info("[Receiver] AgentCloudBridge 初始化完成（云端事件 → rolebus 桥）")

    def _write_heartbeat(self) -> None:
        """写一次存活心跳（判据见上文那条定时器的说明）。

        内容只写「什么时候、哪个 pid」—— 给人排查用，判活只看 mtime。原子写（tmp+rename），
        免得观察者读到半截文件（虽然它只 stat）。
        **任何异常都吞掉、只记一行日志**：定时器回调抛出去会让 rclpy 把整个节点带走，
        心跳写不成绝不能反过来把 receiver 弄崩。
        """
        try:
            RECEIVER_HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = RECEIVER_HEARTBEAT_FILE.with_suffix('.heartbeat.tmp')
            tmp.write_text(
                f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} pid={os.getpid()}\n',
                encoding='utf-8',
            )
            tmp.replace(RECEIVER_HEARTBEAT_FILE)
        except Exception as exc:  # noqa: BLE001 —— 心跳是附加信号，绝不能连累主循环
            self.logger.warning(f"[Receiver] 心跳写失败（不影响云端收发）: {exc}")

    # -------------------------------------------------------------------------
    # SpeechCore 事件入口
    # -------------------------------------------------------------------------

    def _on_sigc_event(self, msg) -> None:
        """
        SpeechCore 事件订阅回调。
        1. 过滤 keep_alive / bind_status_response
        2. 防重复过滤
        3. 处理 credential_get（自行处理，避免父类冗余日志）
        4. 转发给 on_speech_event
        """
        try:
            # 解析事件
            try:
                parsed = json.loads(msg.event)
            except Exception:
                return

            event = parsed.get("event", "")

            # 过滤不记录的事件
            if event in ("keep_alive", "bind_status_response"):
                return

            event_id = parsed.get("eventId", "")

            # 防重复：eventId 为空时不去重（否则第一条空 id 事件会占据集合，
            # 之后所有空 id 事件被静默丢弃，而云端下发是 fire-and-forget 无重试）
            if event_id:
                if event_id in self.processed_event_ids:
                    self.logger.debug(f"[Receiver] 重复事件，跳过: eventId={event_id}")
                    return
                self.processed_event_ids.add(event_id)
                if len(self.processed_event_ids) > self._processed_ids_max_size:
                    self.processed_event_ids = set(
                        list(self.processed_event_ids)[-self._processed_ids_max_size // 2:]
                    )

            # 手动处理 credential_get，其余走 on_speech_event
            if event == "credential_get" and event_id in self._credential_pending:
                self._handle_credential_response(parsed)
                return

            self.on_speech_event(msg.event)
        except Exception as e:
            self.logger.error(f"[Receiver] 处理 SpeechCore 事件异常: {e}")

    def on_speech_event(self, event_json: str) -> None:
        """处理 SpeechCore 下发的非鉴权事件：`robot_agent_task` 按 body.action 转投 rolebus，
        `exe` 按 body 里的工具名直接执行（见 `_handle_exe`）。

        其余事件（含 `cloud_service_response`）只记一行 debug —— 本树里没有它们的接收方。
        """
        try:
            message = json.loads(event_json)
        except json.JSONDecodeError:
            self.logger.error("[Receiver] 收到的 SpeechCore 事件不是合法 JSON")
            return

        event_name = str(message.get("event", ""))
        event_id   = str(message.get("eventId", ""))
        body       = message.get("body", {})

        if event_name == EVENT_EXE:
            self._handle_exe(body, event_id)
            return

        if event_name != EVENT_ROBOT_AGENT_TASK:
            # 这几类（multimodal_get_url / hardware_state_report / …）不是下发给本 agent 的东西，
            # 只有 debug 一条；原先那条「每条事件都打一行 📋 事件: event=…」的通用轨迹已删。
            self.logger.debug(f"[Receiver] 未支持的云端事件: {event_name}")
            return

        if not isinstance(body, dict):
            self.logger.error(f"[Receiver] robot_agent_task 事件 body 格式错误: {message}")
            return

        # 云端下发的原文照打一行 —— 这条事件**只有这一行**（通用轨迹已删）。放在动作分发之前是有意
        # 的：info / answer 端侧不处理、未知 action 直接丢弃，那几条路径都走不到 _forward_action 里
        # 那行带原文的回执，只留结论的话就是「云端发了东西、机器人没反应」而没有任何内容可查。
        self._log_cloud_event(body)

        action = str(body.get("action", "")).strip()
        if action not in KNOWN_ACTIONS:
            # 云端词表之外的动作落这里：告警而不是静默丢弃（云端不会重发）。
            self.logger.warning(f"[Receiver] 未知的 action: {action!r}，忽略事件: eventId={event_id}")
            return

        self._forward_action(action, body)

    def _log_cloud_event(self, body: Dict[str, Any]) -> None:
        """把一条云端下发的原文照打一行（渲染成什么样见 `_CloudEventFormatter`）。

        正文是 `CLOUD_EVENT_LOG_TAG` + body 的 JSON（`ensure_ascii=False`：中文不转义；JSON 里字符串
        自带换行的转义，所以这一行天然是单行，不用再压空白）。这个前缀同时是 starter.sh 把那几行筛到
        终端的匹配串，两边取的是 configs 里的同一个常量。
        """
        self.logger.info(
            f"{CLOUD_EVENT_LOG_TAG}: {json.dumps(body, ensure_ascii=False)}",
            extra={'cloud_event': True},
        )

    # -------------------------------------------------------------------------
    # 转发：云端动作 → rolebus 事件
    # -------------------------------------------------------------------------

    def _forward_action(self, action: str, body: Dict[str, Any]) -> None:
        """把一条云端动作投成 rolebus 云事件。

        两件事：① `CLOUD_ACTION_MAP` 把云端词归一成端侧词（answer / info 端侧不处理，只记一条
        日志）；② 说明文字按**云端动作**从 body 里取（`_description_of`）。事件类型与目标角色
        由 plugin_tools/rolebus.py 说了算（`send_cloud` 按端侧动作投 `cloud_update`、按
        cloud_variant() 定版本），本模块不判"这个动作投哪条事件"；task_id 照抄云端下发的那一个，
        缺了让 rolebus 补 `cloud-<Unix 秒>` 占位。
        """
        if action in UNHANDLED_ACTIONS:
            self.logger.info(f"[Receiver] ⏭ 云端动作 {action!r} 端侧不处理，忽略事件")
            return

        description = _description_of(action, body)
        if not description:
            self.logger.warning(
                f"[Receiver] 云端动作 {action!r} 里取不到可投的原文，忽略: body={body}"
            )
            return

        task_id = _body_task_id(body) or None
        mapped = CLOUD_ACTION_MAP[action]

        try:
            envelope = send_cloud(mapped, description, task_id=task_id)
        except Exception as exc:  # noqa: BLE001 —— 一次投递失败不能让 ROS 回调线程断了
            self.logger.error(
                f"[Receiver] 云端事件转发失败（事件没能进角色系统）: "
                f"action={action!r}→{mapped}, task_id={task_id or '(缺，占位)'}, 原因={exc}"
            )
            return

        self.logger.info(
            f"[Receiver] ➡ 已投 rolebus: {envelope['event_type']} → {envelope['target']}，"
            f"task_id={envelope.get('task_id')}, action={action}→{mapped}, 原文={description!r}"
        )

        # [2026-09-28 暂时注释] 单槽这条腿已停；answer 现在端侧不处理（见 UNHANDLED_ACTIONS），
        # 走到这儿的不会是 answer。恢复时与文件头那句一起放开。
        # if action == ACTION_ANSWER:
        #     self._submit_answer(description)

    def _submit_answer(self, content: str) -> None:
        """把用户回答写进落盘单槽（Communicator/pending_answer.py）。

        与上面那封 rolebus 事件是**两条腿**：事件让交互器这一轮知道"用户回答了、原文在
        payload 里"，单槽让 `listen` / `speak_listen` 这个工具调用当场拿到回答。写失败只记
        日志：事件那条腿已经走通，模型仍能从事件里读到原文。
        """
        try:
            submit_pending_answer(content)
        except OSError as exc:
            self.logger.error(f"[Receiver] 写 pending_answer 单槽失败（工具侧会等不到回答）: {exc}")
            return
        self.logger.info(f"[Receiver] 📥 用户回答已写入 pending_answer 单槽: {content!r}")

    # -------------------------------------------------------------------------
    # 云端点名执行（exe 事件）
    # -------------------------------------------------------------------------

    def _handle_exe(self, body: Any, event_id: str) -> None:
        """处理一条 `exe` 事件：拆出工具名与参数，交给云端工具清单的执行器。

        body 形状（云端契约，见 `EVENT_EXE`）：

            {"tool_name": "speak", "parameters": {"text": "你好"}}

        `tool_name` 必须在 `available_tools_cloud.yaml` 里（那份清单就是这条腿的白名单，与
        角色系统的白名单是两份）——**表外一律拒绝**，云端点是点不动别的工具的；`parameters`
        可省（缺省空参数），也可以给 JSON 字符串，解析归执行器（`ToolExecutor.parse_and_execute`
        两种都收）。

        校验通过后**只把执行丢出去、当场返回**：工具跑在哪个线程、为什么不在这里跑，见 `_run_exe`。
        这里只做「参数对不对」这一层判断，工具自己还会校验它那套参数（无效动作名、缺参数等由
        执行器那边给出结果），不在这份重复一遍。
        """
        # 云端下发的原文照打一行，与 robot_agent_task 同一个位置、同一条理由：后面几条拒绝路径
        # 都走不到执行结果那行，不先记原文就是「云端发了东西、机器人没反应」且无从查起。
        self._log_cloud_event(body)

        if not isinstance(body, dict):
            self.logger.error(f"[Receiver] exe 事件 body 格式错误: {body!r}")
            return

        tool_name = str(body.get("tool_name", "")).strip()
        if not tool_name:
            self.logger.warning(
                f"[Receiver] exe 事件没给 tool_name，忽略: eventId={event_id}, body={body!r}"
            )
            return

        parameters = body.get("parameters", {})
        if not isinstance(parameters, (dict, str)):
            self.logger.warning(
                f"[Receiver] exe 事件 {tool_name} 的 parameters 既不是对象也不是 JSON 字符串，"
                f"忽略: eventId={event_id}, parameters={parameters!r}"
            )
            return

        if not self._cloud_executor.has(tool_name):
            self.logger.warning(
                f"[Receiver] exe 事件点名了 available_tools_cloud.yaml 里没有的工具 {tool_name!r}，"
                f"忽略: eventId={event_id}"
            )
            return

        shown = (
            json.dumps(parameters, ensure_ascii=False)
            if isinstance(parameters, dict) else parameters
        )
        self.logger.info(
            f"[Receiver] ▶ 云端点名执行: {tool_name}, eventId={event_id}, parameters={shown}"
        )
        threading.Thread(
            target=self._run_exe,
            args=(tool_name, parameters, event_id),
            name=f"cloud-exe-{tool_name}",
            daemon=True,
        ).start()

    def _run_exe(self, tool_name: str, parameters: Any, event_id: str) -> None:
        """在独立线程里跑一条云端点名的工具，收尾记一行结果。

        **为什么不就地跑**（这是本段最容易改错的一点）：订阅回调跑在 rclpy 的 executor 线程上，
        心跳定时器也在那上面（心跳为什么必须同线程，见 `_write_heartbeat` 那段）。而这条腿上的
        工具是**阻塞的整段流程**：speak 等说完、move 等到位、waypoint_nav 轮询到抵达，一条就能
        占住那个线程几十秒到几分钟 —— 期间心跳停写（观察者会把 tools.communication 判成不活）、
        后续云端事件一条都进不来。所以回调只负责把活丢出去。

        多条 exe 事件同时到达时各跑各的，本模块不做串行：真正的排队在工具层（运动归
        MotionManager、播报归常驻 TTS 服务），而在这里串行只会让后到的那条 `stop` 排在前面那条
        长任务后面 —— 正好是它该打断的东西。

        `parse_and_execute` 已经把工具抛异常那条路兜成一条 status=error 的结果（连同 traceback），
        所以这里不再逐条判异常；结果**只记日志**、不回云端：云端要的是「这台机器去做这个动作」，
        回执走的是云端自己的链路，本桥不代答（与文件头「不做的事」同一条口径）。
        """
        try:
            results = self._cloud_executor.parse_and_execute(
                [{"function": {"name": tool_name, "arguments": parameters}}]
            )
        except Exception as exc:  # noqa: BLE001 —— 线程里的异常没人接，这里记清是哪条事件弄的
            self.logger.error(
                f"[Receiver] exe 执行器异常: {tool_name}, eventId={event_id}, 原因={exc}"
            )
            return

        result = results[0] if results else {}
        status = result.get("status", "error")
        # 错误时 result['error'] 是短原因、result['result'] 是 traceback（见 parse_and_execute），
        # 成功时只有 result['result']。压成单行是为了结果里带换行（如 robot_state 的整段 YAML）
        # 时日志仍是「一条事件一行」。
        detail = result.get("error") or result.get("result") or ""
        self.logger.info(
            f"[Receiver] {'✅' if status == 'success' else '❌'} 云端点名执行结束: "
            f"{tool_name}, eventId={event_id}, status={status}, result={_single_line(detail)}"
        )

    # -------------------------------------------------------------------------
    # 生命周期
    # -------------------------------------------------------------------------

    def destroy_node(self) -> None:
        """清理节点资源（本桥没有在飞的请求，只是把关闭过程记一行）。"""
        self.logger.info("[Receiver] 节点正在关闭...")
        super().destroy_node()


# =============================================================================
# 入口
# =============================================================================

def _handle_termination_signal(signum, frame) -> None:
    """SIGTERM 也转成 KeyboardInterrupt：与 SIGINT 走同一条收摊路径（spin 正常退出）。"""
    raise KeyboardInterrupt


def main() -> None:
    import argparse
    # 本模块已无可选参数（--test-send 随上报通路删除）。保留一个空 parser 是有意的：
    # start_agent.py 把剩余参数原样透传进来，多给一个旗标要当场报错，而不是被静默忽略。
    parser = argparse.ArgumentParser(
        prog="Communicator.receiver",
        description="云端 SIGCEvent → rolebus 事件 的薄桥（见本文件头部）",
    )
    parser.parse_args()

    rclpy.init()
    node = AgentCloudBridge()

    # 终止信号统一转 KeyboardInterrupt（SIGINT 本就如此），走 finally 收摊
    signal.signal(signal.SIGTERM, _handle_termination_signal)

    # 后台组件（frame_producer / vision_observer / status_observer / motion_status）不归 Receiver：
    # 它们与 receiver 是并列组件，由统一启动入口 start_agent.py 按参数决定起哪些，
    # 其中帧生产者/视觉观测器/运动状态观测器与主循环同进程（帧环是进程内存；运动记录在
    # MotionManager 的模块内存里），只有状态观测器是独立子进程。Receiver 只做云端收发。
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.logger.info("[Receiver] 收到 KeyboardInterrupt，正在关闭...")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
