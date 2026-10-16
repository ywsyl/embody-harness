#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
斗牛（bull_chase）端侧客户端 —— **进程内模块**，由 plugin_tools/tool_executor.py 直接调用。

对接端侧斗牛算法服务（topic 交互，见 skills/bull-chase/要求.md 与《智能体斗牛_信令文档》）：

    发布  /bullfight_ctl        智能体 → 算法：控制信令（SN / objpos / status）
    订阅  /bullfight_status     算法 → 智能体：状态
    （/bullfight_poll 轮询回报：要求.md 1.1b 标注「暂不实现」，故本模块不订阅、不发 02 轮询）

两路 topic 均为 std_msgs/msg/String，payload 为 JSON 字符串。

一场斗牛（要求.md §1 / §2）：
    §1.2 发 ctl 00 开启搜寻 → 只等算法状态，**不先搜一轮**
    收到 12（冲刺完成）→ 先播一遍 BULL_CHASE_VISION_AUDIO → 做一次视觉搜索
    → 发 ctl 03（带 objpos）→ 继续等状态
    每收到一次 12 就再搜一轮（轮数 turns +1，turns 只数 12 触发的搜索）
    收到 01/02/08/11/14 → 退出工具并汇报

停一场斗牛：stop_bull_chase() 只往 /bullfight_ctl 发一条 ctl 01 就返回，不等算法回执——
正在跑的那一场订阅到 08（端侧收到结束信令）会自己收场。它不碰帧环、不调视觉，且跑在
**另一个进程**（角色侧每调一次工具就是一个 plugin_tools.bridge 子进程），所以斗牛进行中
也调得动。

⚠️ 为什么是「进程内模块」而不是像 skills/search 那样的独立子进程脚本：
§2 的视觉搜索用 vision 工具（mode=front）。取帧本身**已经跨进程**了 —— frame_producer 的
模块级函数会经 FRAME_PRODUCER_SOCKET 向取帧服务要帧（见 frame_producer 模块头），所以
「子进程读不到帧」这条理由不再成立。本模块仍按进程内加载是**历史与遗留耦合**：它还牵着
ROS init/复用与按路径加载，改回子进程形态要单独验证。由 tool_executor.bull_chase() 进程内
调用这件事本身不用改。
（目录名 bull-chase 带连字符，不能按包路径 import，tool_executor 按文件路径加载本模块。）

ROS 生命周期：本模块可能跑在已经有 rclpy 上下文（receiver 主循环）的进程里，也可能跑在
没人初始化过的进程（local 模式）里。故：只在 rclpy 未就绪时自己 init（并记住，收尾时自己
shutdown），已就绪则复用；spin 用自带的 SingleThreadedExecutor + 独立线程，不碰全局 executor，
不与主循环抢 node。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 仓库根（本文件 skills/bull-chase/scripts/ 上三级）入 sys.path，以便 import configs / skills
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import (  # noqa: E402
    BULL_CHASE_PUBLISHER_TIMEOUT,
    BULL_CHASE_STATUS_TIMEOUT,
    BULL_CHASE_VISION_AUDIO,
    BULL_CHASE_VISION_RESIZE,
)
from configs.prompts import BULL_CHASE_PROMPT  # noqa: E402

# ---- 话题常量（要求.md / 信令文档；注意文档正文里的 "/bullfight‌_status" 混入了
#      零宽非连接符 U+200C，真实话题名不带该字符，命令示例也是干净的）----------------
TOPIC_CTL = "/bullfight_ctl"        # 智能体 → 算法（控制）
TOPIC_STATUS = "/bullfight_status"  # 算法 → 智能体（状态）

# ---- 控制信令（智能体 → 算法）------------------------------------------------
CTL_START = "00"    # 开始斗牛
CTL_STOP = "01"     # 结束斗牛
CTL_TARGET = "03"   # 上报目标位置（带 objpos），交算法接管冲刺

# ---- 状态码含义（信令文档 §2.2 + 要求.md）------------------------------------
CODE_TEXT = {
    "00": "端侧收到开启信令",
    "01": "初始化异常_云端通信失败",
    "02": "初始化异常_云端接流失败",
    "07": "初始化成功，云端开始推理",
    "08": "端侧收到结束信令",
    "09": "轮询状态_后台常驻，等待开启信令",
    "10": "目标初始化匹配成功",
    "11": "连续3次无法识别到目标（红色斗牛旗）",
    "12": "冲刺完成（本轮斗牛结束）",
    "13": "成功到达目标旁边",
    "14": "斗牛过程中视频流异常（超过2s没有有效视频帧）",
    "19": "本轮任务结束，可下发下一次开启命令",
}

# 退出工具的状态码 → 汇报文案（要求.md §3 的取值，逐字对齐）
EXIT_RESULT_TEXT = {
    "01": "初始化异常_云端通信失败",
    "02": "初始化异常_云端接流失败",
    "08": "用户下发结束指令",
    "11": "连续3次无法识别到目标（红色斗牛旗）",
    "14": "连续视频流异常",
}

_EXIT_CODES = frozenset(EXIT_RESULT_TEXT)   # 收到即退出工具
_ROUND_DONE_CODES = frozenset({"12"})       # 收到即开下一轮（要求.md §2）

# 发完结束信令后等 DDS 把报文送出去的时间（秒）：publish 只是交给中间件，紧接着
# destroy_node / rclpy.shutdown 会掐断连接，报文可能还没出门。
_STOP_FLUSH_SEC = 0.5

_MISSING = object()   # 在视觉输出里没找到 objpos 字段的哨兵


def _default_log(msg: str) -> None:
    """默认日志输出：随主终端打印（同 skills/*/scripts 的进展打印习惯）。"""
    print(msg, flush=True)


def _normalize_code(raw: Any) -> str:
    """把算法回报的 status 归一化为两位码字符串（"00"…"19"）。"""
    if isinstance(raw, bool):
        return str(raw)
    if isinstance(raw, int):
        return f"{raw:02d}" if 0 <= raw < 100 else str(raw)
    s = str(raw).strip()
    if s.isdigit() and len(s) < 2:
        s = f"{int(s):02d}"
    return s


def _default_sn() -> str:
    """SN 解析优先级：环境变量 BULL_CHASE_SN → 设备配置 factory/devSn → 空。

    与 skills/search 的 findobj 客户端同一套口径（同一只狗、同一个编号）。
    """
    import os

    env = os.environ.get("BULL_CHASE_SN", "").strip()
    if env:
        return env
    try:
        from configs.paths import DEVICE_ID

        # 配置缺失时为 "unknown"，当 SN 用无意义，按无 SN 处理
        return "" if DEVICE_ID == "unknown" else DEVICE_ID.strip()
    except Exception:
        return ""


# =============================================================================
# 视觉输出解析（要求.md §2）
# =============================================================================

def _balanced_json(text: str, start: int) -> Optional[str]:
    """返回 text[start]（'{' 或 '['）起到配对括号的片段，字符串内的括号不计数。

    不成对（被截断）时返回 None。比按 {} 计数更严格：模型输出里常带引号包裹的
    内层 JSON（二次编码），纯计数法会被内层括号带偏。
    """
    if start >= len(text) or text[start] not in "{[":
        return None
    closer = "}" if text[start] == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == text[start]:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _find_objpos(node: Any, depth: int = 0) -> Any:
    """在已解析的 JSON 结构里递归找 "objpos" 字段，返回其值（找不到返回 _MISSING）。

    顶层直接是 {"objpos": ...} 最理想；同时容忍接口把模型文本包一层（如
    {"choices": [{"message": {"content": "{...}"}}]}）或整段被二次编码成字符串——
    字符串会再 json.loads 一次，递归层数设上限防死循环。
    """
    if depth > 5:
        return _MISSING
    if isinstance(node, dict):
        if "objpos" in node:
            return node["objpos"]
        for value in node.values():
            found = _find_objpos(value, depth + 1)
            if found is not _MISSING:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_objpos(value, depth + 1)
            if found is not _MISSING:
                return found
    elif isinstance(node, str):
        try:
            parsed = json.loads(node)
        except ValueError:
            return _MISSING
        return _find_objpos(parsed, depth + 1)
    return _MISSING


def _quoted_literal(text: str, start: int) -> Optional[str]:
    """按转义感知扫描，返回 text[start]=='"' 起到配对引号的字符串字面量（含两端引号）。"""
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return text[start:i + 1]
        i += 1
    return None


def _normalize_objpos(raw: Any, log: Optional[Callable[[str], None]] = None) -> Tuple[bool, Optional[List[float]]]:
    """objpos 取值归一化：null → 未找到；[x, y] → 找到（越界只告警，不擅自换算）。

    提示词要求 x/y 为 0~1 之间的小数（左下角为原点），本模块原样转发，不做像素换算：
    坐标语义由算法侧按同一份提示词约定解释，这里替它"猜"单位反而会引入偏差。
    """
    log = log or _default_log
    if raw is None:
        return False, None
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            x, y = float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            raise RuntimeError(f"斗牛视觉搜索：objpos 不是数值对: {raw!r}")
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            log(f"[bull_chase] 警告: objpos 超出 0~1 范围 {raw!r}，原样转发给算法")
        return True, [round(x, 4), round(y, 4)]
    raise RuntimeError(f"斗牛视觉搜索：objpos 格式非法（应为 [x, y] 或 null）: {raw!r}")


def _parse_vision_output(
    output: str, log: Optional[Callable[[str], None]] = None
) -> Tuple[bool, Optional[List[float]]]:
    """从 vision（BULL_CHASE_PROMPT）输出里取出 objpos。

    返回 (found, objpos)：
        (True,  [x, y])  找到目标，坐标为 0~1 的归一化值（提示词约定：左下角为原点）
        (False, None)    模型明确回 objpos=null —— 本帧没找到目标

    输出里压根没有带 objpos 的 JSON 时抛 RuntimeError：那属于视觉工具调用失败，
    必须退出工具报错，**不能**把「没解析出来」当成「没找到」（否则会拿 null 去骗算法）。
    """
    candidates: List[Any] = []

    def _try(text: str) -> None:
        if not text:
            return
        try:
            candidates.append(json.loads(text))
        except ValueError:
            pass

    _try(output.strip())
    for i, ch in enumerate(output):
        if ch in "{[":
            block = _balanced_json(output, i)
            if block and "objpos" in block:
                _try(block)
        elif ch == '"':
            # 二次编码：整段 JSON 被当成字符串打印（外层引号、内部引号转义）
            lit = _quoted_literal(output, i)
            if lit and "objpos" in lit:
                _try(lit)

    for cand in candidates:
        raw = _find_objpos(cand)
        if raw is not _MISSING:
            return _normalize_objpos(raw, log)

    raise RuntimeError(
        "斗牛视觉搜索：未能从视觉输出中解析出 objpos 字段（视觉工具调用失败）。"
        f"原始输出片段: {output.strip()[:300]!r}"
    )


def _announce_vision(log: Callable[[str], None]) -> None:
    """§2 视觉搜索前播一遍 BULL_CHASE_VISION_AUDIO（提醒在场的人：马上要冲刺了）。

    走 TTS_Manager 的门面（常驻服务 socket；连不上它自己退回 run_tts_speak.sh），与全仓别的
    播报同一个入口。**等它播完再往下走**（沿用原先 REPORT_TOOLS 预播报「播完再执行技能」的口径）：
    下一轮冲刺紧跟在这次视觉搜索与 ctl 03 之后，警告没说完就冲出去便失去意义。
    播报失败不改斗牛流程（与 _close_session 发信令失败同一口径）：只打一行日志继续搜。
    """
    audio = str(BULL_CHASE_VISION_AUDIO or "").strip()
    if not audio:
        return
    try:
        # from TTS_Manager import tts_client
        # tts_client.speak_and_wait(audio)
        from plugin_tools.tool_executor import speak
        speak({"text": audio, "play_now": True, "wait_done": False})
        log(f"[bull_chase] 冲刺前播报：{audio}")
    except Exception as exc:
        log(f"[bull_chase] 视觉前播报失败（忽略，继续斗牛）: {exc}")


def _vision_search(prompt: str, log: Callable[[str], None]) -> Tuple[bool, Optional[List[float]]]:
    """要求.md §2：用 vision 工具（mode=front）搜索斗牛旗，返回 (found, objpos)。

    调视觉前先播一遍 BULL_CHASE_VISION_AUDIO（见 _announce_vision）。

    进程内调用 skills/vision/scripts/vision_dispatch.run_front：front 取帧走共享帧环，
    只能在本进程内做（见模块头）。取帧失败、视觉接口报错、输出解析不出 objpos —— 三种
    情况都抛 RuntimeError 交给工具层：按约定「视觉工具调用失败即退出工具并报错」。

    取帧按 BULL_CHASE_VISION_RESIZE 缩到 1280×720 再送视觉模型（帧环原帧是 1920×1080）：
    缩放只改画面尺寸，objpos 是 0~1 归一化坐标，语义不变。
    """
    from skills.vision.scripts import vision_dispatch

    _announce_vision(log)
    log("[bull_chase] §2 视觉搜索（vision mode=front）…")
    output = vision_dispatch.run_front(prompt, resize=BULL_CHASE_VISION_RESIZE)
    found, objpos = _parse_vision_output(output or "", log)
    if found:
        log(f"[bull_chase] 视觉找到目标 objpos={objpos}")
    else:
        log("[bull_chase] 视觉本帧未找到目标（objpos=null）")
    return found, objpos


# =============================================================================
# ROS 会话
# =============================================================================

class BullChaseSession:
    """斗牛一轮会话的收发器：发布 ctl、收集 /bullfight_status 状态码。

    rclpy 节点由外部创建/销毁，本类只管话题收发与状态缓存。
    回调在 spin 线程里跑，主循环读 _pending，两者经 _lock 互斥。
    """

    def __init__(self, node: Any, sn: str, log: Callable[[str], None]) -> None:
        from std_msgs.msg import String

        self.node = node
        self.sn = sn
        self.log = log

        self._lock = threading.Lock()
        self._pending: List[str] = []   # 尚未被主循环消费的状态码（按到达顺序）
        self.codes: List[str] = []      # 已消费的全部状态码（随结果一并汇报）

        self.pub = node.create_publisher(String, TOPIC_CTL, 10)
        node.create_subscription(String, TOPIC_STATUS, self._on_status, 10)
        self._msg_type = String

    # ------------------------------------------------------------------ 收发
    def send_ctl(self, status: str, objpos: Any = _MISSING) -> None:
        """发布一条控制信令到 /bullfight_ctl。

        objpos 只在需要时报（03 上报目标位置）：00/01 按信令文档的停止示例
        只带 SN + status，不塞空字段。
        """
        payload: Dict[str, Any] = {"SN": self.sn}
        if objpos is not _MISSING:
            payload["objpos"] = objpos
        payload["status"] = status

        msg = self._msg_type()
        msg.data = json.dumps(payload, ensure_ascii=False)
        self.pub.publish(msg)
        self.log(f"[bull_chase] → ctl {msg.data}")

    def _on_status(self, msg: Any) -> None:
        """ /bullfight_status 回调：只归一化入队，不在回调里做判断。"""
        try:
            body = json.loads(msg.data)
            code = _normalize_code(body.get("status"))
        except Exception as exc:
            self.log(f"[bull_chase] 状态报文解析失败: {exc} | {str(msg.data)[:200]}")
            return
        with self._lock:
            self._pending.append(code)

    def drain(self) -> List[str]:
        with self._lock:
            codes = list(self._pending)
            self._pending.clear()
        return codes

    def record(self, code: str) -> None:
        """登记并打印一个状态码。"""
        self.codes.append(code)
        self.log(f"[bull_chase] ← status={code}（{CODE_TEXT.get(code, '未知状态')}）")

    def wait_for_subscriber(self, timeout: float) -> bool:
        """等算法侧订阅者上线再发首条信令。

        DDS 发现是异步的：发布者刚建好就 publish，报文可能在发现完成前被丢掉
        （ros2 topic pub --once 也是等匹配上订阅者才发）。等不到也照样发（超时只告警），
        不能让算法没起来时工具卡死在这里。
        """
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            try:
                if self.pub.get_subscription_count() > 0:
                    return True
            except Exception:
                return False
            time.sleep(0.1)
        return False


def _wait_round_signal(
    session: BullChaseSession,
    timeout: float,
    cancel_event: Optional[threading.Event],
) -> Tuple[str, str]:
    """等本轮的收尾信号，返回 (结果, 状态码)。

    结果取值：
        "exit"       收到退出码（01/02/08/11/14），状态码一并返回
        "round_done" 收到 12（冲刺完成），可开下一轮
        "cancelled"  任务被取消（cancel_event 置位）
        "timeout"    timeout 秒内既没退出码也没等到 12

    同一批到达的码里退出码优先于 12（退出码是终态，不能被 12 顶掉）。
    其余码（07/10/13/19/00/09…）只登记打印，继续等下一个信号。
    """
    deadline = time.time() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled", ""
        for code in session.drain():
            session.record(code)
            if code in _EXIT_CODES:
                return "exit", code
            if code in _ROUND_DONE_CODES:
                return "round_done", code
        if time.time() >= deadline:
            return "timeout", ""
        time.sleep(0.2)


def _close_session(session: BullChaseSession, log: Callable[[str], None]) -> None:
    """主动结束本次斗牛（发 ctl 01）。

    只在**我们自己**要收场时发（任务取消 / 等状态超时）：算法自己报的退出码
    （01/02/08/11/14）说明它那边本轮已经结束，再补一条 01 是多余噪声。
    发完即返回，不等算法回执（08/19）：我们这边已经没有下一步要等它，等回执只会让
    这几条码出现在返回的 received_codes 里——不值得在退出路径上多阻塞几秒。
    永不抛异常。
    """
    try:
        session.send_ctl(CTL_STOP)
    except Exception as exc:
        log(f"[bull_chase] 发送结束信令失败（忽略）: {exc}")


# =============================================================================
# 主流程
# =============================================================================

def _ensure_rclpy() -> Tuple[Any, bool]:
    """确保 rclpy 可用，返回 (rclpy 模块, 是否由本模块 init)。"""
    try:
        import rclpy
    except Exception as exc:
        raise RuntimeError(f"rclpy 不可用（ROS 环境未就绪）: {exc}")
    if rclpy.ok():
        # 同进程已有上下文（receiver 主循环）：复用，收尾时也不许我们 shutdown
        return rclpy, False
    rclpy.init()
    return rclpy, True


def _build_result(
    *,
    result_text: str,
    turns: int,
    code: str,
    sn: str,
    session: Optional[BullChaseSession],
) -> Dict[str, Any]:
    """汇总工具返回：result（退出情况说明）+ turns（轮数）+ 诊断字段。"""
    return {
        "result": f"{code}：{result_text}" if code else result_text,
        "turns": turns,        # 已进行的轮数 = 收到 12 后触发的视觉搜索次数（要求.md §3）
        "sn": sn,
        "last_status": (session.codes[-1] if session and session.codes else ""),
        "received_codes": list(session.codes) if session else [],
    }


def run_bull_chase(
    prompt: Optional[str] = None,
    sn: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    log: Optional[Callable[[str], None]] = None,
    status_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """跑完整场斗牛，返回 {result, turns, ...}。

    流程（要求.md §1.2 / §2）：
        发 ctl 00 开启 → 只等算法状态 → 每收到一次 12 就做一轮视觉搜索并上报 ctl 03(objpos)
        → 继续等状态；收到 01/02/08/11/14 即收场退出。
    发 00 之后**不先搜一轮**：要求.md §1 只要求「监听状态」与「开启搜寻」，视觉搜索
    属 §2 的 12 分支（收到 12 才搜）。故首轮也由 12 触发，`turns` 只数这类搜索。
    本函数只负责流程；视觉调用失败（取帧失败 / 接口报错 / 解析不出 objpos）直接抛
    RuntimeError，由工具层按「调用失败」上报为错误。

    Args:
        prompt:         视觉提示词，缺省取 configs.prompts.BULL_CHASE_PROMPT
        sn:             机器狗编号，缺省按 BULL_CHASE_SN / 设备配置解析
        cancel_event:   任务取消事件（receiver 的云端 interrupt 信号）；置位后立即收场
        log:            日志输出函数，缺省打印到 stdout
        status_timeout: 单次等状态上限（秒），缺省取 configs.BULL_CHASE_STATUS_TIMEOUT
    """
    log = log or _default_log
    prompt = (prompt if prompt is not None else BULL_CHASE_PROMPT).strip()
    if not prompt:
        raise RuntimeError(
            "bull_chase: 目标 prompt 未配置（configs.prompts.BULL_CHASE_PROMPT 为空占位），"
            "填写该配置后本工具才能执行"
        )
    sn = _default_sn() if sn is None else str(sn).strip()
    wait_timeout = float(status_timeout if status_timeout is not None else BULL_CHASE_STATUS_TIMEOUT)

    rclpy, inited_here = _ensure_rclpy()
    from rclpy.executors import SingleThreadedExecutor

    node = rclpy.create_node("bull_chase_client")
    session = BullChaseSession(node, sn, log)
    # 自带 executor + 独立 spin 线程：不动全局 executor，不与主循环（receiver）抢 node
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True, name="bull-chase-ros")
    spin_thread.start()

    turns = 0      # 已进行的轮数：收到 12 后触发的视觉搜索次数（要求.md §3）
    code = ""
    result_text = ""
    try:
        # ---- §1.1 / §1.2 监听状态 + 开启搜寻 ----
        if not session.wait_for_subscriber(BULL_CHASE_PUBLISHER_TIMEOUT):
            log(f"[bull_chase] 警告: {BULL_CHASE_PUBLISHER_TIMEOUT:g}s 内未见 {TOPIC_CTL} 订阅者，"
                "仍按流程下发开启信令（算法侧可能未启动）")
        session.send_ctl(CTL_START)
        log(f"[bull_chase] 已开启斗牛（sn={sn!r}），等算法上报 12 触发第一轮视觉搜索")

        # ---- §2 循环：等本轮状态 → 收到 12 就搜一轮并 03 上报 → 再等 ----
        while True:
            verdict, code = _wait_round_signal(session, wait_timeout, cancel_event)

            if verdict == "exit":
                result_text = EXIT_RESULT_TEXT.get(code, CODE_TEXT.get(code, "未知退出状态"))
                break
            if verdict == "cancelled":
                result_text = "用户下发结束指令（任务被取消，已结束斗牛）"
                _close_session(session, log)
                break
            if verdict == "timeout":
                # 算法那边既没报退出码也没报 12，本轮没法再推进
                result_text = (
                    f"等待算法状态超时（{wait_timeout:g}s 内既无 12 也无退出码），已主动结束斗牛"
                )
                _close_session(session, log)
                break

            # verdict == "round_done"：收到 12 → 视觉搜索一轮（轮数 +1）→ 03 上报目标
            turns += 1
            log(f"[bull_chase] 收到 12，开始第 {turns} 轮视觉搜索")
            found, objpos = _vision_search(prompt, log)
            # 未找到也要按规则发 03（objpos=null），让算法侧决定后续
            session.send_ctl(CTL_TARGET, objpos=objpos if found else None)
    finally:
        try:
            executor.remove_node(node)
        except Exception:
            pass
        try:
            executor.shutdown(timeout_sec=2.0)
        except Exception:
            pass
        try:
            node.destroy_node()
        except Exception:
            pass
        if inited_here:
            try:
                rclpy.shutdown()
            except Exception:
                pass

    result = _build_result(
        result_text=result_text,
        turns=turns,
        code=code,
        sn=sn,
        session=session,
    )
    log(f"[bull_chase] 结束：{result['result']}；共进行 {turns} 轮")
    return result


def stop_bull_chase(
    sn: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
    publisher_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """下发结束信令（ctl 01），让算法收场，返回 {result, sn, subscriber_seen}。

    只发一条 01 就返回，不等算法回执（08/19）：收场结论由**正在跑的那一场**汇报——
    run_bull_chase 订阅 /bullfight_status，收到 08 会以「08：用户下发结束指令」结束。
    本函数不碰共享帧环、不调视觉，也不读任务池，所以既能在斗牛进行中由另一个进程调用，
    也能独立于 agent 主进程跑（见 main --stop）。
    没见到算法订阅者时照样发（只告警）：算法没起来不该让调用方拿不到结果，信令是否落地
    由 subscriber_seen 说明。永不抛异常，除了 rclpy 不可用（进程环境问题，调用方无解）。

    Args:
        sn:                机器狗编号，缺省按 BULL_CHASE_SN / 设备配置解析
        log:               日志输出函数，缺省打印到 stdout
        publisher_timeout: 发信令前等订阅者上线的上限（秒），缺省取 BULL_CHASE_PUBLISHER_TIMEOUT
    """
    log = log or _default_log
    sn = _default_sn() if sn is None else str(sn).strip()
    wait_timeout = float(
        publisher_timeout if publisher_timeout is not None else BULL_CHASE_PUBLISHER_TIMEOUT
    )

    rclpy, inited_here = _ensure_rclpy()
    from rclpy.executors import SingleThreadedExecutor

    node = rclpy.create_node("bull_chase_stop_client")
    session = BullChaseSession(node, sn, log)
    # 与 run_bull_chase 同一口径：自带 executor，不动全局 executor（本进程可能已有主循环）
    executor = SingleThreadedExecutor()
    executor.add_node(node)

    matched = False
    try:
        matched = session.wait_for_subscriber(wait_timeout)
        if not matched:
            log(f"[bull_chase] 警告: {wait_timeout:g}s 内未见 {TOPIC_CTL} 订阅者，"
                "仍下发结束信令（算法侧可能未启动）")
        session.send_ctl(CTL_STOP)
        # 这一等也让订阅到的状态报文（算法收场时的 08/19）走完回调，不影响本函数的结论
        executor.spin_once(timeout_sec=_STOP_FLUSH_SEC)
    finally:
        try:
            executor.remove_node(node)
        except Exception:
            pass
        try:
            executor.shutdown(timeout_sec=2.0)
        except Exception:
            pass
        try:
            node.destroy_node()
        except Exception:
            pass
        if inited_here:
            try:
                rclpy.shutdown()
            except Exception:
                pass

    result = {
        "result": "已下发结束信令（/bullfight_ctl status=01）。若当时正有一场斗牛在进行，"
                  "bull_chase 会收到 08（用户下发结束指令）并结束。",
        "sn": sn,
        "subscriber_seen": matched,
    }
    log(f"[bull_chase] 已下发结束信令（sn={sn!r}，订阅者{'已见到' if matched else '未见到'}）")
    return result


def main() -> int:
    """命令行自检入口（非工具调用路径）。

    --stop 只发结束信令，不碰帧环，可独立运行；不带 --stop 时是完整斗牛流程，注意独立跑
    本脚本时**没有共享帧环**（帧生产者归 agent 主进程所有），§2 视觉搜索会以「取帧失败」
    报错退出——这是进程边界的必然结果，不是 bug。要看完整流程请在 agent 进程内经
    bull_chase 工具调用。
    """
    import argparse

    parser = argparse.ArgumentParser(description="斗牛（bull_chase）端侧客户端（进程内模块）")
    parser.add_argument("--sn", default=None, help="机器狗编号；缺省取 BULL_CHASE_SN / 设备配置 devSn")
    parser.add_argument("--status-timeout", type=float, default=None,
                        help="单次等算法状态的上限（秒）")
    parser.add_argument("--stop", action="store_true",
                        help="只下发结束信令（ctl 01）并退出，不跑斗牛流程")
    args = parser.parse_args()

    if args.stop:
        result = stop_bull_chase(sn=args.sn)
    else:
        result = run_bull_chase(sn=args.sn, status_timeout=args.status_timeout)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
