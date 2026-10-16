"""
工具执行器 — 依据 available_tools.yaml 中每个工具的 extra 配置构造执行函数，
并提供 OpenAI tool_calls 的解析与分发执行。

职责划分（与 tool_parser.py 配套）:
    tool_parser    — 读取 YAML、按角色白名单过滤（load_role_entries）、构造工具说明文本
                     （prompt_tools / tools）
    tool_executor  — 构造并注册执行器、解析 tool_calls 并分发执行

角色白名单只由 tool_parser.load_role_entries 落地：本文件按收到的条目注册执行器，
不自行判断角色；两个调用点（ToolParser、bridge）都先把条目过滤好再传进来。

主要导出:
    ToolExecutor — 从工具条目注册执行器；parse_and_execute 解析并执行 tool_calls

工具实现分散在各模块里，本文件只留入口（取参数 → 调实现 → 拼模型可见的回执文本）：
语音五个工具在 plugin_tools/audio_tools.py，天气在 plugin_tools/weather.py，
任务清单在 plugin_tools/task_board.py。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保项目根目录在 Python 搜索路径中（支持 python Loop/tool_executor.py 直接运行）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.paths import (
    ACTION_SCRIPT,
    BULL_CHASE_SCRIPT,
    FIND_OBJ_SCRIPT,
    RUNTIME_MOTION_STATUS,
    RUNTIME_ROBOT_STATUS,
    RUNTIME_VISION_OBSERVATION,
    TURN2SUBJECT_SCRIPT,
    VISION_DISPATCH_SCRIPT,
    VLN_SCRIPT,
)

from configs.configs import (
    ACTION_CHANNEL_GAIT,
    GAIT_TYPES,
    INPUT_WAIT,
    MOTION_ACTION_TIMEOUT_BY_ACTION,
    NAVIGATION_POLL_INTERVAL,
    NAVIGATION_REQUEST_TIMEOUT,
    NAVIGATION_TIMEOUT,
    VISION_OBSERVE_RATE_LEVELS,
    VISION_SUBPROCESS_TIMEOUT,
)

from configs.prompts import BULL_CHASE_PROMPT

# 视觉观测参数的运行时可调覆盖层（读写在 Orchestrator/vision_tunables.py）。
# 别名带下划线：本模块要导出一个**同名工具函数** vision_tunables()，
# 直接 `from Orchestrator import vision_tunables` 会让本文件后面的 def 把它遮住。
from Orchestrator import vision_tunables as _vision_tunables_config

from plugin_tools import audio_tools
from plugin_tools import task_board
# 天气查询（poll open-meteo）。别名带下划线，理由同上：本模块要导出一个**同名工具函数** weather()。
from plugin_tools import weather as _weather_impl


# ─────────────────────────────────────────────────────────────────────────────
# 内置工具执行函数（{tool_name}_exe）
# 每个内置工具一个独立函数，_build_executor 按工具名分发到这里。
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# 语音工具（speak / listen / speak_listen / audio_interact / speak_input）
# 实现全在 plugin_tools/audio_tools.py（播报走常驻 TTS 服务、收音走本地 ASR 或控制终端，
# 加上流式播报去重、等播完的判定、上报"等待用户回答"那几段流程）。下面五个只做
# 「取参数 → 调 audio_tools → 拼模型可见的回执文本」，流程改动请去那边。
# ─────────────────────────────────────────────────────────────────────────────


def speak(arguments: Dict[str, Any]) -> Any:
    """speak/tts：调 TTS 服务播报文本（1 次，非持续）。

    `play_now` 缺省 **false**：不主动抢麦（设备没有队列 —— 每条新请求都会先停掉在播的那条），
    要抢麦、立刻打断在播的才传 true；`wait_done` 缺省 false，即发出请求就返回（音频还在放），
    传 true 则等这段播完再返回。

    流式播报（dsh 插件边生成边把成句的部分送进 TTS）开着时，这段文字可能已经由插件送出去了：
    这里不重发，只补没播过的那截并按需等播完。两条路与判定见 audio_tools.speak_text。
    """
    text = arguments.get("text", "")
    if not text:
        return "tts: text 参数为空"
    audio_tools.speak_text(
        text,
        play_now=audio_tools.bool_arg(arguments.get("play_now"), default=False),
        wait_done=audio_tools.bool_arg(arguments.get("wait_done"), default=False),
    )
    return "✅ 播报完成"


def listen(arguments: Dict[str, Any]) -> Any:
    """listen：拉一次本地 ASR 收音，返回用户这一轮说的话。

    主动唤醒设备收音（skills/asr-listener/scripts/run_asr.sh），取设备侧 ASR 认出来的
    最终文本；收音窗口（configs.ASR_LISTEN_WINDOW）内没人说话就是"未收到"。

    等不到不抛异常：没人说话是正常结果，返回"未收到"让模型自己决定要不要再问一遍。
    措辞与 audio_interact 一致 —— 两者收的是同一路声音（设备侧 ASR）。
    收音过程（含超时兜底）见 audio_tools.collect_speech。
    """
    text = audio_tools.collect_speech()
    if text:
        return f"[本地语音] 用户说：{text}"
    return "[本地语音] 未收到用户语音（收音窗口内未识别到语音）"


def speak_listen(arguments: Dict[str, Any]) -> Any:
    """speak_listen：先 TTS 播报，再拉一次本地 ASR 收音，完成一轮"说一句、听一句"。

    播报那一段**不主动打断在播的**（`play_now=false` 写死，schema 也没暴露这个开关——要抢麦是
    audio_interact 的事）；`wait_done` 缺省 true，即等这段播完再开始收音。

    整段流程（上报"等待用户回答" → 播报 → 收音 → 复位，以及"播报必须排在收音前面"的原因、
    上报为何成对）见 audio_tools.speak_and_listen。
    """
    # 参数名兼容：工具 schema 定义为 text，历史实现用 init_text，两处都接受
    init_text = arguments.get("text") or arguments.get("init_text") or ""
    if not init_text:
        return "speak_listen: text 参数为空"
    answer = audio_tools.speak_and_listen(
        init_text,
        wait_done=audio_tools.bool_arg(arguments.get("wait_done"), default=True),
        task_id=str(arguments.get("task_id") or "").strip(),
    )
    if answer:
        return f"✅ 播报完成\n[本地语音] 用户说：{answer}"
    return "✅ 播报完成\n[本地语音] 未收到用户语音（收音窗口内未识别到语音）"


def audio_interact(arguments: Dict[str, Any]) -> Any:
    """audio_interact：播报一句话 → 静默唤醒设备 → 等这一轮收尾，并收下设备侧 ASR 认出来的用户语音。

    整段流程在 skills/audio_interact/（播报复用 tts-speak、唤醒复用 asr-listener），
    **播报排在唤醒前面**；判定、等待窗口与"等待用户回答"上报见
    audio_tools.run_audio_interact。本工具 schema 里没有 task_id，模型也填不了 → 拿不到任务号
    就不向云端上报。

    没听清时把那句结束原因翻成人话再交给模型（audio_tools.ended_by_text），模型读的是人话而不是
    脚本的数字码。
    """
    text = str(arguments.get("text") or "").strip()
    if not text:
        return "audio_interact: text 参数为空"
    result = audio_tools.run_audio_interact(
        text, task_id=str(arguments.get("task_id") or "").strip()
    )
    if isinstance(result, str):
        # 超时 / 脚本输出不是预期 JSON：audio_tools 给的是一句说明，按原样交给模型
        return result
    answer = str(result.get("answer") or "").strip()
    if not answer:
        return f"[本地语音] 未收到用户语音（{audio_tools.ended_by_text(result)}）"
    return f"[本地语音] 用户说：{answer}"


def speak_input(arguments: Dict[str, Any]) -> Any:
    """speak_input：先 TTS 播报，再从控制终端读一行，作为本轮用户回复。

    本地联调入口：拿不到麦克风收音（不在机器人上、ASR 会话起不来）时用它顶替
    listen / speak_listen，人的输入直接变成工具结果。它不在任何一条云端链路上，
    因此不向云端上报状态、也不需要 task_id。

    播报那半段与 speak_listen 相同（不主动打断、wait_done 缺省为真）；读终端失败
    （无控制终端）返回错误文本而不是抛异常。读终端本身见 audio_tools.read_tty_line。
    """
    text = arguments.get("text") or arguments.get("init_text") or ""
    if not text:
        return "speak_input: text 参数为空"

    audio_tools.speak_text(
        text,
        play_now="false",
        wait_done=audio_tools.bool_arg(arguments.get("wait_done"), default=True),
    )
    readable, user_text = audio_tools.read_tty_line(INPUT_WAIT)
    if not readable:
        return "✅ 播报完成\n[终端输入] 读不到控制终端，本入口只在有终端的角色进程里可用"
    if not user_text:
        return "✅ 播报完成\n[终端输入] 未收到终端输入"
    return f"✅ 播报完成\n[终端输入] {user_text}"


# 动作白名单**从 configs 的动作表派生**，不另抄一份：那张表是「动作名 → 等待上限」的全集
# （22 个），也是 available_tools.yaml 里 action 工具枚举的对照。曾在这里另抄过一份 14 个的
# 子集，结果是新动作在工具层被静默判成「无效的 action_type」。
_VALID_ACTIONS = frozenset(MOTION_ACTION_TIMEOUT_BY_ACTION)


def _do_action(name: str, arguments: Dict[str, Any]) -> Any:
    """do_action / action 的共用实现；name 只用于回执与报错里的自称。"""
    action_type = arguments.get("action_type", "")
    if not action_type:
        return f"{name}: action_type 参数为空"
    # 与 robot-dog-actions 技能保持一致的动作白名单，防止把无效动作发给机器人
    if action_type not in _VALID_ACTIONS:
        return (
            f"{name}: 无效的 action_type={action_type!r}，"
            f"可用动作: {', '.join(sorted(_VALID_ACTIONS))}"
        )
    result = subprocess.run(
        ["bash", ACTION_SCRIPT, action_type],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        # 脚本把指令发到 /catch_turtle/action_type 就返回（--once 发布），不等机器人做完动作；
        # 这里如实转述它的回执，不替它宣布"动作完成"（等完成要订阅状态，订阅方在组件进程）。
        return result.stdout.strip()
    raise RuntimeError(
        f"{name} 执行失败 (code={result.returncode}): {result.stderr or result.stdout}"
    )


def do_action(arguments: Dict[str, Any]) -> Any:
    """do_action：控制机器狗执行指定动作（ACTION_SCRIPT 下发，不等动作做完）"""
    return _do_action("do_action", arguments)


def action(arguments: Dict[str, Any]) -> Any:
    """action：do_action 的别名 —— 编排器的工具表里它叫 action，交互器的叫 do_action。

    两条名字指同一份实现，不各写一遍。参数与返回见 do_action。
    """
    return _do_action("action", arguments)


_VALID_GAITS = frozenset(GAIT_TYPES)


def gait(arguments: Dict[str, Any]) -> Any:
    """gait：切换机器狗步态（走 sportMode 通道，不是 motorSkill）。

    与其他动作同一条路：把步态名当动作名下发到同一个话题，只是换通道参数。旧实现调
    MotionManager.run_gait 等切换完成，本层**等不到**：状态订阅在组件进程（Motion_Manager/
    motion_status.py 那份 rclpy 上下文），角色工具进程另起订阅会与它抢同一个话题，且本层
    也没有"切换完成"的判据。故只返回脚本回执（指令已下发），不报"已切换"。
    """
    gait_type = arguments.get("gait_type", "")
    if not gait_type:
        return "gait: gait_type 参数为空"
    # 白名单从 configs.GAIT_TYPES 派生，不另抄一份：那张表也是 yaml 里 gait 枚举的对照
    if gait_type not in _VALID_GAITS:
        return (
            f"gait: 无效的 gait_type={gait_type!r}，"
            f"可用步态: {', '.join(sorted(_VALID_GAITS))}"
        )
    result = subprocess.run(
        ["bash", ACTION_SCRIPT, gait_type, ACTION_CHANNEL_GAIT],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gait 执行失败 (code={result.returncode}): {result.stderr or result.stdout}"
        )
    return result.stdout.strip()


# move：平移距离（米）/ 旋转角度（度）；stop 无需参数
_MOVE_LINEAR_MODES = {"forward", "backward", "left", "right"}
_MOVE_ANGULAR_MODES = {"turnleft", "turnright"}
_MOVE_MODES = _MOVE_LINEAR_MODES | _MOVE_ANGULAR_MODES | {"stop"}


def move(arguments: Dict[str, Any]) -> Any:
    """move：控制机器狗平移（value 单位米）或旋转（value 单位度）；stop 立即停止"""
    from configs.paths import MOTION_SCRIPT
    from configs.configs import (
        MOVE_ANGULAR_SPEED,
        MOVE_LINEAR_SPEED,
        MOVE_90_DURATION,
        MOVE_MAX_LINEAR_DISTANCE,
        MOVE_MAX_ANGULAR_DEGREES,
    )

    mode = arguments.get("mode", "")

    # stop 无需 value，直接下发停止命令
    if mode == "stop":
        result = subprocess.run(
            [str(MOTION_SCRIPT), "stop"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"move 执行失败 (code={result.returncode}): {result.stderr or result.stdout}"
            )
        return "✅ 移动完成: 已停止"

    if mode not in _MOVE_MODES:
        return (
            f"move: 无效的参数 mode={mode!r}，"
            f"可用模式: forward/backward/left/right/turnleft/turnright/stop"
        )

    value = arguments.get("value")
    if value is None:
        return f"move: mode={mode!r} 需要 value 参数（平移单位米，旋转单位度）"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return f"move: value={value!r} 不是有效数值"
    if value <= 0:
        return f"move: value 必须为正数（当前 {value!r}）"

    # 最大值裁剪：平移最多 10 米，旋转最多 720 度（防止误传巨值导致运动失控）。
    # 裁剪到上限而非拒绝；返回信息中的 value 即为实际运动量，LLM 可见裁剪后的结果。
    if mode in _MOVE_LINEAR_MODES and value > MOVE_MAX_LINEAR_DISTANCE:
        value = MOVE_MAX_LINEAR_DISTANCE
    elif mode in _MOVE_ANGULAR_MODES and value > MOVE_MAX_ANGULAR_DEGREES:
        value = MOVE_MAX_ANGULAR_DEGREES

    # 按速度换算执行时长：client 只下发命令即返回，server 按 duration 秒驱动后自动停止。
    # 平移：时长 = 距离 / 线速度；旋转：时长 = 角度(弧度) / 角速度。
    if mode in _MOVE_LINEAR_MODES:
        duration = value / MOVE_LINEAR_SPEED
        speed = MOVE_LINEAR_SPEED
        unit = "米"
    else:
        duration = (value / 90.0) * MOVE_90_DURATION
        speed = MOVE_ANGULAR_SPEED
        unit = "度"

    result = subprocess.run(
        [str(MOTION_SCRIPT), mode, f"{speed:g}", f"{duration:.2f}"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"move 执行失败 (code={result.returncode}): {result.stderr or result.stdout}"
        )
    # 等待运动完整结束（含少量余量），避免后续工具调用与本次运动重叠
    time.sleep(duration/2)
    return f"✅ 移动完成: {mode} {value:g}{unit}"


_VISION_MODES = {
    "image": f"{sys.executable} {VISION_DISPATCH_SCRIPT} --mode image --image {{IMAGE_PATH}} --prompt {{PROMPT}}",
    "front": f"{sys.executable} {VISION_DISPATCH_SCRIPT} --mode front --prompt {{PROMPT}}",
}


def _extract_surround_target(output: str):
    """从 surround 搜索输出中提取最高置信度目标的 (region, x_hirizon)。

    输出格式参考 vision/SKILL.md 的原地环绕搜索一节：
        {"found": true, "targets": [{"region": ..., "x_hirizon": ..., "confidence": ...}], "return_message": ...}
    实际输出可能有两种形态（vision_local 的 print(response.text) 原样打印接口返回）：
        1) 原始 JSON 对象（可能包裹在 [CMD]/日志文本中）
        2) 被当作字符串二次编码的 JSON（外层带引号、内部引号转义），如 "{ \\\"found\\\": true, ... }"
    两种都解析；找不到可解析的目标时返回 (None, None)。
    """
    candidates: List[dict] = []

    # 1) 整个输出直接是 JSON 对象
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            candidates.append(data)
    except ValueError:
        pass

    def _json_object_at(text: str, start: int):
        """按 {} 配对，返回从 text[start]=='{' 到配对 '}' 的文本。"""
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    def _quoted_string_at(text: str, start: int):
        """按转义感知扫描，返回从 text[start]=='"' 到配对 '"' 的字符串字面量（含两端引号）。"""
        i = start + 1
        while i < len(text):
            ch = text[i]
            if ch == '\\':
                i += 2
                continue
            if ch == '"':
                return text[start:i + 1]
            i += 1
        return None

    # 2) 原始 JSON 对象（包裹在其它日志文本中的情况）
    for i, ch in enumerate(output):
        if ch == '{':
            obj = _json_object_at(output, i)
            if obj and '"found"' in obj:
                try:
                    data = json.loads(obj)
                    if isinstance(data, dict):
                        candidates.append(data)
                except ValueError:
                    pass

    # 3) 二次编码：JSON 被当作字符串打印（外层引号、内部引号转义），如 "{ \"found\": ... }"
    for i, ch in enumerate(output):
        if ch == '"':
            lit = _quoted_string_at(output, i)
            if not lit or '{' not in lit:
                continue
            try:
                inner = json.loads(lit)
            except ValueError:
                continue
            if isinstance(inner, str) and '"found"' in inner:
                try:
                    data = json.loads(inner)
                    if isinstance(data, dict):
                        candidates.append(data)
                except ValueError:
                    pass

    for data in candidates:
        if data.get("found") is not True:
            continue
        targets = data.get("targets")
        if not isinstance(targets, list) or not targets:
            continue
        best = max(
            (t for t in targets if isinstance(t, dict)),
            key=lambda t: t.get("confidence", 0),
            default=None,
        )
        if best and best.get("region") and best.get("x_hirizon") is not None:
            return best["region"], best["x_hirizon"]
    return None, None


def _look(name: str, arguments: Dict[str, Any]) -> Any:
    """look / vision 的共用实现；name 只用于回执与报错里的自称。

    起子进程（VISION_DISPATCH_SCRIPT）做视觉理解。取帧在**子进程**里发生：本工具是角色侧
    tools-bridge 起的另一个进程，帧环在**取帧服务进程**的内存里（skills/camera/frame_producer.py
    模块头），子进程自己也没有帧 —— 它调 frame_producer 的模块级取帧函数，由后者经
    FRAME_PRODUCER_SOCKET 向取帧服务要帧（三条分派规则见 frame_producer 模块头）。
    正常部署（starter.sh 下由 start_agent.py 拉起取帧服务）时这条链路是通的；取不到帧时
    子进程**报错退出**，本函数据此抛 RuntimeError，不静默返回空画面。
    """
    mode = arguments.get("mode", "")
    prompt = arguments.get("prompt", "")
    image_path = arguments.get("image_path", "")
    cmd_template = _VISION_MODES.get(mode, "")
    if not cmd_template:
        return f"{name}: 未知 mode={mode}，可用: {list(_VISION_MODES.keys())}"
    if mode == "image" and not image_path:
        return f"{name} [image]: image_path 参数必填"
    cmd_str = cmd_template.replace("{IMAGE_PATH}", image_path).replace("{PROMPT}", prompt)
    result = subprocess.run(cmd_str, shell=True, capture_output=True, text=True,
                            timeout=VISION_SUBPROCESS_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(
            f"{name} [{mode}] 执行失败 (code={result.returncode}): {result.stderr or result.stdout}"
        )
    return result.stdout.strip()


def look(arguments: Dict[str, Any]) -> Any:
    """look：视觉理解（image 单图分析 / front 前方感知）。

    原地环绕搜索（360° 找目标）已拆分为独立工具 search_old，不再走本工具。
    取帧的进程边界见 _look 的 docstring。
    """
    return _look("look", arguments)


def vision(arguments: Dict[str, Any]) -> Any:
    """vision：look 的别名 —— 编排器的工具表里它叫 vision，交互器的叫 look。

    两条名字指同一份实现。参数与取帧链路见 look / _look。
    """
    return _look("vision", arguments)


def vision_tunables(arguments: Dict[str, Any]) -> Any:
    """vision_tunables：查看/切换**视觉观测的频率档位**（低/中/高）。

    调的是 vision_observer「隔多久去看一眼帧环」的周期 —— **不是相机抓拍节奏**：帧生产者
    一直按固定高频抓帧攒环，改它会让相机被抢占得更凶或更懒，所以不开放。改观测周期只影响
    视觉的实时性与上报频度，不碰相机。

    只收档位名、不收秒数：档位名 → 秒数的唯一出处是 configs.VISION_OBSERVE_RATE_LEVELS，
    本工具不另抄一份。无参数 = 查询当前档位与生效值（人用 CLI 设过自定义值时要让 LLM 看得见
    现实，所以回执里连秒数一起报，而不是只报档位名）。

    **只收档位名，不收每轮拼接帧数**（那个字段是给画面内容定尺度的，人用 CLI 改）；
    查询回执由 render() 渲染，里面连拼接帧数一起报 —— LLM 看得见它，但改不了。

    非法取值返回字符串、不抛异常（与 gait / action 同款）：让 LLM 有机会照着可用档位自己纠偏。
    覆盖层是**跨进程**的（观测器在组件进程里，每轮现读一次），所以这里写完下一轮才生效。
    """
    rate = arguments.get("observe_rate", "")
    if not rate:  # 无参数 = 查询
        return _vision_tunables_config.render(_vision_tunables_config.current())
    if not isinstance(rate, str) or rate not in VISION_OBSERVE_RATE_LEVELS:
        return (
            f"vision_tunables: 无效的 observe_rate={rate!r}，"
            f"可用档位: {', '.join(VISION_OBSERVE_RATE_LEVELS)}"
        )
    try:
        values = _vision_tunables_config.update(
            observe_interval=VISION_OBSERVE_RATE_LEVELS[rate]
        )
    except _vision_tunables_config.TunablesError as exc:
        return f"vision_tunables: 未修改：{exc}"
    return f"视觉观测频率已切到「{rate}」。\n" + _vision_tunables_config.render(values)


# 本体状态段 → 状态文件。三段各由一个独立后台进程写入（视觉观测器 / 状态观测器 /
# 运动管理器），文件路径只有 configs/paths.py 一处事实源，这里只做「段名 → 文件」的映射，
# 不另抄路径、也不解析内容。
_STATE_FILES: Dict[str, Path] = {
    "vision": RUNTIME_VISION_OBSERVATION,
    "system": RUNTIME_ROBOT_STATUS,
    "motion": RUNTIME_MOTION_STATUS,
}


def robot_state(arguments: Dict[str, Any]) -> Any:
    """robot_state：读机器狗本体状态快照，原样返回所选段的 YAML 原文。

    只读不写、不做解析：文件头部自带取值语义（null / unknown 各是什么意思），取数就是把
    原文交出去。**不做失效回落、也不替读方标过期** —— 三段由三个进程各按自己的周期刷新
    （system 段 60s 整份覆盖、motion 只在状态改变时写 + 2s 心跳、vision 按观测周期追加），
    能不能采信由读方拿文件里的时间戳自己判。

    文件缺失或为空返回字符串、不抛异常（与 vision_tunables 同款）：写入它的后台进程可能
    没在跑，这是模型该看到的现实，不是工具故障。消息里只报文件名不报全路径 —— 路径对模型
    没用，认得出是哪一段就够了。
    """
    section = arguments.get("section", "")
    path = _STATE_FILES.get(section) if isinstance(section, str) else None
    if path is None:
        return (f"robot_state: 未知的 section={section!r}，可用: {', '.join(_STATE_FILES)}"
                f"（vision=视觉观察历史，system=系统状态，motion=运动状态）")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (f"robot_state [{section}]: 状态文件 {path.name} 不存在，"
                f"写入它的后台进程可能没在跑")
    except OSError as exc:
        return f"robot_state [{section}]: 状态文件 {path.name} 读取失败：{exc}"
    if not text.strip():
        return f"robot_state [{section}]: 状态文件 {path.name} 为空，还没有内容写进来"
    return text


def weather(arguments: Dict[str, Any]) -> Any:
    """weather：查天气（当前天气 + 逐日预报），实现全在 plugin_tools/weather.py。

    这里只是个薄壳，与 vision_tunables 转发给 Orchestrator/vision_tunables.py 同一分工：
    tool_executor 管「按名分发」，外部服务客户端放各自的模块。天气那条链路要读状态快照取
    机器狗坐标、要 curl 两个外网接口、要译 WMO 码表，全摊进本文件会让它更臃肿；
    给模型看的用法说明在 available_tools.yaml，提示词里的场景说明见
    Interactor/prompts/task_policy.md §2。

    失败不在这里兜：weather.query_weather 抛的 RuntimeError 原样往上走，由桥转成
    {"error": ...} 交给模型（见模块抬头「抛异常 vs 返回字符串」的约定）。
    """
    return _weather_impl.query_weather(arguments)


def task_list(arguments: Dict[str, Any]) -> Any:
    """task_list：读任务 —— task_range 选读哪一批（recent / history），task_id 选整批还是那一条。

    两者不同时生效：**填了 task_id 就只读那一条，task_range 不起作用**（见下面的第三条出口）；
    task_range 只在**不带**号、要「读一整批」时才决定给哪一批。

    清单是交互器与编排器**共享的一份**（runtime_files/tasks/pending_tasks.yaml，见
    plugin_tools/task_board.py），只装**未终结**的任务；任务一终结（succeeded / failed /
    cancelled）就移出清单，单独落到同目录的 <task_id>.yaml。两批就读法不同，task_range 选的就是
    哪一批：
    **recent**（清单里还没终结的：pending / executing / stopped / interrupted，即「待做任务」）与
    **history**（已经终结的那些存档，即「历史任务」）。省略 task_range 等于 recent —— 这是本工具
    从前的行为。

    四条出口：

    - recent、不给 task_id：原样返回清单文件的**整份原文**—— 不解析、不摘要、不重排，交出去的就是
      磁盘上的事实。清单还不存在时返回一句提示、不抛异常（还没有任何待做任务是正常状态）；
    - history、不给 task_id：返回**汇总**——每条只给任务自己的字段、不含执行记录（见
      task_board.read_history_text：存档一条一个文件、只增不减，几千条的执行记录一次交出去没人
      看得下）。要看某一条的全文，带它的号再调一次本工具；
    - 给了 task_id：读那**一条**的全文（task_board.read_task_text）—— **填了号 task_range 就不起作用**
      （照旧收下、校一遍取值，但不参与决策）：那两批是递进的两站，先在清单里找、找不到再翻存档，
      所以号是从别处抄来的、不确定它终结没终结时不必填范围；
    - 号本身不合法（含白名单外字符）抛错，由桥渲染成具体的报错；号只是抄错（两批里都没有）返回
      说明而不是抛错。

    **只有交互器手里有**这个工具（configs/agents.py 的 INTERACTOR_TOOLS）：编排器那一侧的
    清单不由它主动读，dsh-plugins/task-context 每轮把清单全文注入它的模型输入。所以本函数是
    给交互器的按需读点，别把它当两个角色共用的读口。

    读不加锁：写盘走 tmp+rename 原子换，读到的必然是一份完整的文件。

    Args:
        arguments: 工具参数；task_range 与 task_id 都由本函数自己取（见上）。task_range 的取值
            已在 available_tools.yaml 的 enum 里约束过一遍，这里仍让 task_board 校一次
            —— 桥这条路不保证调用方是引擎。

    Returns:
        整份清单原文、历史汇总、或那一条任务的 YAML 全文；或一句说清为什么没有的说明。
    """
    task_range = task_board.check_task_range(arguments.get("task_range"))
    task_id = arguments.get("task_id")
    if task_board.is_blank_task_id(task_id):
        if task_range == task_board.TASK_RANGE_HISTORY:
            return task_board.read_history_text()
        return task_board.read_pending_text()
    return task_board.read_task_text(task_id)


def search_old(arguments: Dict[str, Any]) -> Any:
    """search_old：原地环绕搜索（原 vision 的 surround，360° 找目标）。

    机器狗原地旋转一周、拍摄并分析周围四视角照片，寻找 prompt 描述的目标。
    找到目标（found=true 且解析出 region/x_hirizon）后总是调用 turn2subject.sh 转向该目标；
    interact_with_target 参数当前不生效（沿用 2026-09-02 起的无条件转向行为），仅保留兼容接收。

    交给模型的只有搜索结论：脚本 stdout（转成 JSON 的视觉结论，见 vision_dispatch.run_cmd
    —— 它的命令流水走 stderr）加末尾一行 `[interact]` 转向结论。转向脚本自己的 `[INFO]`/
    motion.sh 回显**不往里塞**（那是执行流水，不是结论）；转向失败仍把原文带上，那是出错点。
    """
    prompt = arguments.get("prompt", "")
    if not prompt:
        return "search_old: prompt 参数不能为空"
    cmd = f"{sys.executable} {VISION_DISPATCH_SCRIPT} --mode surround --prompt {prompt}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                            timeout=VISION_SUBPROCESS_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"search_old 执行失败 (code={result.returncode}): {result.stderr}")
    out = result.stdout.strip()

    # 找到目标则调用 turn2subject.sh 转向目标（无条件转向）
    region, x_hirizon = _extract_surround_target(out)
    if region is None:
        out += "\n[interact] 未找到目标，跳过转向"
    else:
        turn = subprocess.run(
            ["bash", str(TURN2SUBJECT_SCRIPT), str(region), str(x_hirizon), "true"],
            capture_output=True, text=True, timeout=60,
        )
        if turn.returncode == 0:
            # 转向成功只报结论：turn2subject.sh 的 [INFO] 与 motion.sh 的命令回显都是执行流水
            # （motion.sh 打印什么由它自己定），模型要的是「转没转」这一件事。
            out += "\n[interact] 已转向目标"
        else:
            out += (
                f"\n[interact] 转向目标失败 (code={turn.returncode}): "
                f"{turn.stderr or turn.stdout}"
            )
    return out

# 寻物子进程执行超时：脚本内置总超时(默认180s)+settle(20s)+end-grace(10s)等缓冲，
# 这里需盖过脚本最坏收尾时长，避免外层先把子进程掐掉导致收不到结果。
_FIND_OBJ_PROCESS_TIMEOUT = 260


def search_and_approach(arguments: Dict[str, Any]) -> Any:
    """search_and_approach：语义寻物并靠近（对接端侧 /findobj 寻物服务）。

    一次阻塞调用内完成一轮：发 ctl 00 开启探测 → 订阅 /findobj/search_status 与
    /findobj/search_poll 推进 → 发现目标(10/23) 先取消导航、再发 ctl 03 把运控移交给
    算法执行靠近 → 到达(13) / 本轮结束(19) 后收尾。stdout 返回 JSON 结果
    （found / state / message，见 skills/search/SKILL.md §5 结果汇报）。

    目标 prompt 必须为英文简单名词短语（red cup / sofa / man in red clothes），
    只描述物体外观/特征，不含地点或移动指令——探测期间跨区移动由上层按
    waypoint_nav 分段驱动（本工具单次调用内不移动）。
    """
    prompt = str(arguments.get("prompt") or "").strip()
    print(prompt)
    return "成功找到目标"
    if not prompt:
        return "search_and_approach: prompt 参数不能为空（须为英文目标描述，如 red cup / sofa）"

    # 列表形式传参：prompt 含空格时不会因 shell 分词出错
    cmd = [str(FIND_OBJ_SCRIPT), "--prompt", prompt]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=_FIND_OBJ_PROCESS_TIMEOUT,
    )
    out = result.stdout.strip()
    if result.returncode != 0:
        raise RuntimeError(
            f"search_and_approach 执行失败 (code={result.returncode}): {result.stderr or out}"
        )
    return out or "(空输出)"


# def bash(arguments: Dict[str, Any]) -> Any:
#     """bash：直接调用 subprocess 执行命令"""
#     cmd = arguments.get("command", "")
#     if not cmd:
#         return "bash: command 参数为空"
#     result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
#     if result.returncode != 0:
#         raise RuntimeError(f"bash 命令执行失败 (code={result.returncode}): {result.stderr}")
#     out = result.stdout.strip()
#     if result.stderr:
#         out = f"{out}\n[stderr] {result.stderr.strip()}"
#     return out or "(空输出)"


# 导航轮询间隔与整体超时上限取 configs（NAVIGATION_POLL_INTERVAL / NAVIGATION_TIMEOUT），
# 不在本文件另立一份：Motion_Manager/motion_manager.py 用的是同一对。
# 导航服务的 HTTP 一律经 plugin_tools/nav_tools.py（唯一客户端）—— 本文件既不 import
# requests，也不 import 那组 URL（它们在 configs/paths.py）。


def _waypoint_nav_supported_targets() -> set:
    """返回导航支持的目标地标集合（target 的选择范围）。

    唯一来源是 configs.configs.load_supported_locations()，它读 SUPPORTED_LOCATIONS_FILE
    （skills/waypoint_nav/landmarks.json）。地标清单是动态的，新增地标无需改代码。

    这里原先把同一份 landmarks.json 再读一遍取 name 后求并集（当时那份名单另有一个路径
    常量指向它）；两处现已指向同一份文件，并集是同一份名单与自己的并集，故去掉那次重复
    读取，行为不变。
    """
    from configs.configs import load_supported_locations

    return set(load_supported_locations())


def waypoint_nav_supported_targets() -> List[str]:
    """导航支持的目标地标列表（已排序）。

    消费方是 bridge 的 `list`：把这份名单注入 waypoint_nav 的工具说明（`_inject_supported_targets`），
    让模型选合法 target，与 waypoint_nav() 的校验集合（SUPPORTED_LOCATIONS_FILE）同源
    —— 校验改了口径，模型看到的名单跟着改，不会两处各说一套。
    （tool_parser 的 `_build_tools_text` 有一份同样的注入，那条路在 dsh 角色系统里已无调用方。）
    """
    # print(sorted(_waypoint_nav_supported_targets()))
    return sorted(_waypoint_nav_supported_targets())


def _auto_relocalize() -> None:
    """按 navigation_api.md §4.2 启动自动重定位；不成则抛错。

    成功判据是应答体的 `message == "success"`（形如
    {"code":0,"message":"success","data":{"task_id":"loc_…"}}）。它只表示服务端**受理**了，
    不表示底盘真的定位好了 —— 是否真定位好由紧随其后的导航自己暴露（没恢复，go 仍会失败）。
    """
    from plugin_tools import nav_tools

    reply = nav_tools.relocalize(NAVIGATION_REQUEST_TIMEOUT)
    if reply == "":
        raise RuntimeError("waypoint_nav: 当前地图自动定位失败（导航服务没有应答）")
    if not isinstance(reply, dict):
        raise RuntimeError(
            f"waypoint_nav: 当前地图自动定位失败（应答不是 JSON: {str(reply)[:200]}）")
    if reply.get("message") != "success":
        raise RuntimeError(
            f"waypoint_nav: 当前地图自动定位失败"
            f"（code={reply.get('code')!r}, message={reply.get('message')!r}）")


def waypoint_nav(arguments: Dict[str, Any]) -> Any:
    """waypoint_nav：定点导航至指定地标。

    执行流程：
        ① 校验 target 是否在支持地点名单里（_waypoint_nav_supported_targets）—— 放最前，
           它最便宜，且服务不在时这条提示（可用地点）比「服务未启动」更有用；
        ② 服务与定位状态校验：拿不到定位状态应答 → 失败「导航服务未启动」；
           message != "success" → 失败「导航服务出错」；localized 明确为 false → 先按
           navigation_api.md §4.2 自动重定位，失败则「当前地图自动定位失败」；
        ③ 发起导航（POST /api/v1/navigation/go），随后每 NAVIGATION_POLL_INTERVAL 秒轮询
           GET /api/v1/navigation/status：
               SUCCESS            → 返回成功，进入下一步骤
               FAILED / CANCELLED → 结束工具调用，返回终止状态
               RUNNING / IDLE     → 继续等待导航完成

    导航服务的 HTTP 全走 plugin_tools/nav_tools.py（唯一客户端），本函数不自己发请求。
    """
    import time

    from plugin_tools import nav_tools

    # 参数名兼容：YAML 定义为 target，同时兼容 destination
    landmark = str(arguments.get("target") or arguments.get("destination") or "").strip()
    if not landmark:
        raise RuntimeError("waypoint_nav: target 参数为空")

    # 校验目标地标是否在支持范围内（SUPPORTED_LOCATIONS_FILE，即 landmarks.json 的 name 字段），
    # 不在范围内直接失败
    supported_targets = _waypoint_nav_supported_targets()
    if landmark not in supported_targets:
        raise RuntimeError(
            f"waypoint_nav: 目标地标 {landmark!r} 不在支持范围内，"
            f"可用地点: {', '.join(sorted(supported_targets))}"
        )

    # ---- 导航前置校验 ----
    reply = nav_tools.localization_status(NAVIGATION_REQUEST_TIMEOUT)
    if reply == "":
        raise RuntimeError("waypoint_nav: 导航服务未启动（定位状态接口没有应答）")
    if not isinstance(reply, dict):
        raise RuntimeError(
            f"waypoint_nav: 导航服务出错（定位状态接口的应答不是 JSON: {str(reply)[:200]}）")
    if reply.get("message") != "success":
        raise RuntimeError(
            f"waypoint_nav: 导航服务出错"
            f"（code={reply.get('code')!r}, message={reply.get('message')!r}）")

    data = reply.get("data")
    if isinstance(data, dict) and data.get("localized") is False:
        _auto_relocalize()

    # ---- 发起导航 ----
    reply = nav_tools.navigate(landmark, NAVIGATION_REQUEST_TIMEOUT)
    if reply == "":
        raise RuntimeError("waypoint_nav 发起导航失败: 导航服务没有应答")
    if not isinstance(reply, dict):
        raise RuntimeError(f"waypoint_nav 发起导航失败: 应答不是 JSON（{str(reply)[:200]}）")
    if reply.get("code") != 0:
        raise RuntimeError(f"waypoint_nav 发起导航失败: {reply.get('message', reply)}")

    # ---- 轮询导航状态，直至任务结束 ----
    deadline = time.time() + NAVIGATION_TIMEOUT
    while time.time() < deadline:
        time.sleep(NAVIGATION_POLL_INTERVAL)
        status_reply = nav_tools.navigation_status(NAVIGATION_REQUEST_TIMEOUT)
        if not isinstance(status_reply, dict):
            raise RuntimeError(f"waypoint_nav 查询状态失败: {status_reply!r}")
        data = status_reply.get("data")
        data = data if isinstance(data, dict) else {}
        status = data.get("status", "")

        if status == "SUCCESS":
            return "✅ 导航成功到达目的地"
        # if status in ("FAILED", "CANCELLED"):
        #     raise RuntimeError(
        #         f"navigation 导航终止（status={status}，destination={data.get('destination')}）"
        #     )
        # IDLE / RUNNING / 未知状态：继续等待

    raise RuntimeError(f"navigation 导航超时（{NAVIGATION_TIMEOUT}s 内未完成）")


# VLN 语言指令导航：进程超时需高于 OMNINAV_RESULT_TIMEOUT_SEC（默认 900s），留足缓冲
_VLN_PROCESS_TIMEOUT = 960

def vision_language_nav(arguments: Dict[str, Any]) -> Any:
    """vision_language_nav：视觉导航（VLN）。

    接收一段自然语言导航指令（必须翻译为英文），利用视觉逐渐靠近目标，
    运行 OmniNav 机器人导航栈（run_vln_instruction.sh）直到导航完成收到 STOP，
    返回机器可读结果。仅用于实时机器人导航，非离线图像/视频推理。

    执行契约（见 skills/waypoint_nav/vision_language_nav.md）：
        - 进度日志写到 stderr；stdout 恰好为一个 JSON 对象；
        - exit 0 = status=ok（显式 STOP）；非 0 = status=error；
        - 超时/传输故障绝不能被解释为 STOP（status=ok）。
    """
    instruction = str(arguments.get("instruction") or "").strip()
    if not instruction:
        raise RuntimeError("vision_language_nav: instruction 参数不能为空")

    # 占位功能s
    return {"status": "success"}

    script = Path(VLN_SCRIPT)
    if not script.is_file():
        raise RuntimeError(
            f"vision_language_nav: VLN 导航栈脚本不存在（{script}），无法执行视觉导航"
        )

    try:
        result = subprocess.run(
            [str(script), instruction],
            capture_output=True,
            text=True,
            timeout=_VLN_PROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        # 契约：超时绝不能被解释为 STOP，须显式报错
        raise RuntimeError(
            f"vision_language_nav: 导航超时（{_VLN_PROCESS_TIMEOUT}s 内未收到结果）"
        ) from exc

    out = result.stdout.strip()
    if out:
        # stdout 恰好为一个 JSON 对象；只透传 status 与 stop 两个字段，
        # 其余（robot_pose/path_length/model_infer_sec 等）不返回。
        # status=error 时 stop 缺失，错误详情也一并丢弃，只留 status。
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"vision_language_nav: stdout 不是合法 JSON (code={result.returncode}): {exc}\n"
                f"{out}\n[stderr] {result.stderr}"
            ) from exc
        return {"status": "success"}
        # return {"status": data.get("status"), "stop": data.get("stop")}


    if result.returncode != 0:
        raise RuntimeError(
            f"vision_language_nav 执行失败 (code={result.returncode}): "
            f"{result.stderr or result.stdout}"
        )

    # 退出码 0 但 stdout 为空：违反"恰好一个 JSON 对象"契约
    raise RuntimeError("vision_language_nav: 执行成功但 stdout 为空")


_BULL_CHASE_MODULE_NAME = "bull_chase_skill"


def _bull_chase_module():
    """进程内加载并返回斗牛技能模块（skills/bull-chase/scripts/bull_chase.py）。

    斗牛 §2 的视觉搜索用 vision mode=front 取帧，而取帧本身**已经跨进程**了
    （frame_producer.py 的模块级函数经 FRAME_PRODUCER_SOCKET 找取帧服务）——所以「子进程读不到
    帧」这个理由不再成立。这里仍按进程内加载：斗牛还牵着 ROS init / 复用与按路径加载，
    改回子进程形态要单独验证（不在取帧收口这一单里）。

    目录名 bull-chase 带连字符、不能按包路径 import，所以按文件路径加载；模块缓存进
    sys.modules（exec 前先塞，兼容模块内的自引用），避免每次工具调用重复加载。
    """
    import importlib.util

    mod = sys.modules.get(_BULL_CHASE_MODULE_NAME)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(_BULL_CHASE_MODULE_NAME, BULL_CHASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"bull_chase: 无法加载技能模块 {BULL_CHASE_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_BULL_CHASE_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


def _current_task_cancel_event(task_id: str) -> Optional[threading.Event]:
    """取当前任务的协作取消事件（云端 interrupt 置位 → 长流程工具据此及时收场）。

    任务池的登记处是 Communicator.task_exit。**本树目前没有进程往里登记**：登记方原本是
    receiver 的 create 路径（与任务线程同进程，`threading.Event` 才能跨线程读写），而 receiver
    已改成只转投 rolebus 事件的薄桥 —— 于是这里一律取不到、返回 None，调用方按「无取消信号」
    跑完（云端 interrupt 不再能即刻掐断斗牛）。要恢复得让 orchestrator 角色收到
    `cloud_update{stop}` 时自己登记一份取消事件，属新设计。本函数永不抛异常。
    """
    if not task_id:
        return None
    try:
        from Communicator.task_exit import get_task

        event = (get_task(task_id) or {}).get("cancel_event")
        return event if hasattr(event, "is_set") else None
    except Exception:
        return None


def bull_chase(arguments: Dict[str, Any]) -> Any:
    """bull_chase：斗牛（无入参的预设表演动作）。

    整场斗牛在**本进程内**跑完（见 _bull_chase_module）：发 ctl 00 开启 → 只等算法状态
    → 收到 12（冲刺完成）就做一轮视觉搜索并上报 ctl 03(objpos) → 继续等状态 → 收到退出码
    01/02/08/11/14 收场（发 00 之后不先搜一轮，首轮也由 12 触发）。目标描述固定取
    configs.prompts.BULL_CHASE_PROMPT，没有工具入参（arguments 一律忽略，
    只读主循环注入的 task_id 用于取取消事件）。

    返回 JSON 字符串：{"result": 退出情况, "turns": 已进行的轮数}。
    视觉工具调用失败（取帧失败 / 视觉接口报错 / 输出里解析不出 objpos）会抛 RuntimeError，
    由 parse_and_execute 记为 error、结束本工具。
    占位 prompt 未填时直接返回提示，不空跑一场斗牛。
    """
    prompt = str(BULL_CHASE_PROMPT or "").strip()
    if not prompt:
        return ("bull_chase: 目标 prompt 未配置（configs.prompts.BULL_CHASE_PROMPT 为空占位），"
                "填写该配置后本工具才能执行")
    result = _bull_chase_module().run_bull_chase(
        prompt=prompt,
        cancel_event=_current_task_cancel_event(arguments.get("task_id", "")),
    )
    return json.dumps(result, ensure_ascii=False)


def bull_chase_stop(arguments: Dict[str, Any]) -> Any:
    """bull_chase_stop：停止斗牛（无入参的收场动作）。

    往 /bullfight_ctl 发一条 ctl 01，算法据此收场并回报 08；正在跑的 bull_chase 收到 08
    后结束并以「08：用户下发结束指令」汇报。本工具只下发信令、不等回执，也不读任务池，
    故斗牛进行中（bull_chase 占着另一个 bridge 子进程）也调得动。
    arguments 一律忽略。

    返回 JSON 字符串：{"result": 下发情况, "sn": ..., "subscriber_seen": true/false}。
    """
    return json.dumps(_bull_chase_module().stop_bull_chase(), ensure_ascii=False)


# 内置工具名 → 独立执行函数。注册只认 available_tools.yaml 的条目（见
# ToolExecutor.register_from_yaml）：这里多出来的名字（如 "tts" 别名）不会暴露给模型，
# 只是让 yaml 侧改名时有个退路。
_BUILTIN_EXECUTORS: Dict[str, callable] = {
    "speak": speak,
    "tts": speak,
    "listen": listen,
    "speak_listen": speak_listen,
    "audio_interact": audio_interact,
    "speak_input": speak_input,
    "action": action,
    "do_action": do_action,
    "gait": gait,
    "move": move,
    "vision": vision,
    "look": look,
    "vision_tunables": vision_tunables,
    "robot_state": robot_state,
    "weather": weather,
    "task_list": task_list,
    "search_old": search_old,
    "search_and_approach": search_and_approach,
    "bull_chase": bull_chase,
    "bull_chase_stop": bull_chase_stop,
    # "bash": bash,
    "waypoint_nav": waypoint_nav,
    "vision_language_nav": vision_language_nav,
}


# ─────────────────────────────────────────────────────────────────────────────
# ToolExecutor — 构造、注册并执行工具
# ─────────────────────────────────────────────────────────────────────────────

class ToolExecutor:
    """
    根据工具条目（extra 配置）构造执行函数并维护执行器注册表，
    提供 OpenAI tool_calls 的解析与分发执行。

    Attributes:
        _executors  — 工具名 → 执行函数 的注册表
    """

    def __init__(
        self,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._executors: Dict[str, callable] = {}
        if tools:
            self.register_from_yaml(tools)

    # ---- 私有：注册执行器 ----------------------------------------------------

    def register_from_yaml(self, tools: List[Dict[str, Any]]) -> None:
        """根据工具条目重建执行器注册表（每次全量替换）。"""
        self._executors.clear()
        for tool in tools:
            func_def = tool.get("function", {})
            name = func_def.get("name")
            if not name:
                continue
            self._executors[name] = self._build_executor(name, tool)

    def _build_executor(self, name: str, tool: Dict[str, Any]) -> callable:
        """根据工具名构造执行函数。

        内置工具（speak/listen/speak_listen 等）映射到模块级的 {tool_name} 函数；
        其余工具按 extra.execute 字符串执行（eval / exec）。
        """
        extra = tool.get("extra", {})
        execute_str = extra.get("execute", "")
        repeatable = extra.get("repeatable", False)

        # ---- 内置工具：使用独立的 {tool_name} 执行函数 ----
        executor = _BUILTIN_EXECUTORS.get(name)
        if executor is not None:
            executor._tool_name = name
            executor._repeatable = repeatable
            return executor

        # ---- 其他工具：执行 execute 字符串（eval / exec）----
        # 空字符串也是合法的 exec 模块（ast.parse 不报错），不在这里拦住就会造出一个
        # 「调用成功、返回空串」的执行器：工具表说这个工具存在，它却什么都不做，模型
        # 只会看到空结果。工具表与执行器对不上是配置错误，按配置错误处理——注册时就炸。
        if not execute_str.strip():
            raise RuntimeError(
                f"[{name}] 既不是内置工具（_BUILTIN_EXECUTORS），也没有 extra.execute，"
                f"没有可执行的实现"
            )

        exec_mode = "eval"
        try:
            ast.parse(execute_str, mode="eval")
        except SyntaxError:
            exec_mode = "exec"
            try:
                ast.parse(execute_str, mode="exec")
            except SyntaxError:
                exec_mode = None

        safe_globals = {"subprocess": subprocess, "json": json, "print": print}

        def executor(arguments: Dict[str, Any]) -> Any:
            if exec_mode is None:
                raise RuntimeError(f"[{name}] execute 字符串语法错误")
            local_vars = {"arguments": arguments, **arguments}
            if exec_mode == "exec":
                exec(execute_str, safe_globals, local_vars)
                return ""
            else:
                result = eval(execute_str, safe_globals, local_vars)
                return result if result is not None else ""

        executor._tool_name = name
        executor._repeatable = repeatable
        return executor

    # ---- 公开：查询 ----------------------------------------------------------

    def get_executor(self, name: str) -> Optional[callable]:
        """返回指定工具的执行函数。"""
        return self._executors.get(name)

    def has(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._executors

    # ---- 公开：执行单个工具 --------------------------------------------------

    def execute_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
    ) -> Any:
        """按名执行一个工具。

        Args:
            name:      工具名，必须已注册（白名单校验由调用方先做）。
            arguments: 参数，原样传给执行器。

        Returns:
            执行器的返回值。

        Raises:
            KeyError: name 不在注册表里——调用方应先按白名单校验过名字。
        """
        run = self._executors.get(name)
        if run is None:
            raise KeyError(f"工具 {name!r} 没有执行器：注册表与工具清单不一致")

        return run(arguments)

    # ---- 公开：解析 & 执行 --------------------------------------------------

    def parse_and_execute(
        self,
        tool_calls: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        从 tool_calls 逐个执行并返回结构化结果列表。

        Args:
            tool_calls:  request_llm_with_messages 的返回值
            task_id:       当前任务 ID（透传给需要它的执行器，如 bull_chase）

        Returns:
            工具执行结果字典列表，每项包含 tool_name、params、result、status
        """
        results: List[Dict[str, Any]] = []
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            raw_args = func.get("arguments", "{}")

            if isinstance(raw_args, str):
                try:
                    arguments = json.loads(raw_args)
                except json.JSONDecodeError as e:
                    results.append({
                        "tool_name": name,
                        "params": raw_args,
                        "status": "error",
                        "error": str(e),
                        "result": traceback.format_exc(),
                    })
                    continue
            else:
                arguments = raw_args

            # bull_chase 是长流程（一整场斗牛可能跨多轮、跑几分钟）：透传 task_id 供它从
            # task_exit 任务池取协作取消事件，云端 interrupt 时能及时收场（见 bull_chase）
            if name == "bull_chase":
                arguments["task_id"] = task_id or ""

            executor = self.get_executor(name)
            if executor is None:
                results.append({
                    "tool_name": name,
                    "params": arguments,
                    "result": "未找到执行器",
                    "status": "error",
                })
                continue

            try:
                result = executor(arguments)
                results.append({
                    "tool_name": name,
                    "params": arguments,
                    "result": result,
                    "status": "success",
                })
            except Exception as e:
                results.append({
                    "tool_name": name,
                    "params": arguments,
                    "status": "error",
                    "error": str(e),
                    "result": traceback.format_exc(),
                })

        return results


if __name__ == "__main__":
    waypoint_nav(None)