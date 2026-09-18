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
"""

from __future__ import annotations

import ast
import fcntl
import json
import math
import select
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保项目根目录在 Python 搜索路径中（支持 python Loop/tool_executor.py 直接运行）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml

from configs.paths import (
    ACTION_SCRIPT,
    FIND_OBJ_SCRIPT,
    NAVIGATION_LANDMARKS_FILE,
    RUNTIME_INTERACTION_HISTORY_FILE,
    TURN2SUBJECT_SCRIPT,
    VISION_DISPATCH_SCRIPT,
    VLN_SCRIPT,
)

from configs.configs import REPORT_TOOLS

from Communicator.sender import send_task_progress


# ─────────────────────────────────────────────────────────────────────────────
# 交互历史记录（interaction_history.yaml 读写）
# ─────────────────────────────────────────────────────────────────────────────

def _append_interaction_history(role: str, content: str) -> None:
    """
    往 runtime_files/interaction_history.yaml 追加一条记录：
        {"role": ..., "content": ..., "timestamp": "YYYY-mm-dd HH:MM:SS"}

    由执行器在调用工具后触发：
        speak/tts 执行器 → _append_interaction_history("assistant", text)
        listen 执行器    → _append_interaction_history("user", text)

    并发安全：
        TTS 播报与 ASR 收音可能在不同线程 / 进程同时完成并写入同一文件。
        这里对独立锁文件做 flock，串行化“读-改-写”，
        覆盖同进程多线程与跨进程的并发。

    写入失败只打印告警，不影响工具执行结果。
    """
    try:
        path = RUNTIME_INTERACTION_HISTORY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

        # 锁文件独立于目标文件：
        # 写入走 tmp + rename，会替换目标 inode，
        # 等锁在旧 inode 上的进程会读到旧内容，导致丢记录。
        lock_path = path.with_name(path.name + ".lock")

        with open(lock_path, "a", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)

            history: list = []

            if path.exists():
                try:
                    data = yaml.safe_load(
                        path.read_text(encoding="utf-8")
                    )
                except Exception:
                    data = None

                if (
                    isinstance(data, dict)
                    and isinstance(data.get("history"), list)
                ):
                    history = data["history"]

            history.append(
                {
                    "role": role,
                    "content": content,
                    "timestamp": datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            )

            tmp = path.with_suffix(".yaml.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    {"history": history},
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
            tmp.replace(path)

            # with 块退出时关闭 fd，flock 自动释放

    except Exception as e:
        print(f"    [interaction_history] 记录失败: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 内置工具执行函数（{tool_name}_exe）
# 每个内置工具一个独立函数，_build_executor 按工具名分发到这里。
# ─────────────────────────────────────────────────────────────────────────────

# 命令行语音 I/O：speak / listen / speak_listen 用控制终端代替真实 TTS 与 ASR。
# 读走 /dev/tty，不碰 stdin/stdout —— `plugin_tools.bridge call` 的 arguments 走
# stdin、结果 JSON 走 stdout，占用它们会让协议读到 EOF。没有控制终端（后台进程）
# 时降级到 stdin。写不再往任何地方落：原因见 _say_text。


def _open_tty(mode: str) -> Any:
    """按 mode（"r" / "w"）打开控制终端（/dev/tty）；无控制终端时返回 None。

    只按单向模式打开：/dev/tty 是字符设备，文本层可读写的 "r+" 走
    BufferedRandom、要求底层可 seek，会抛 UnsupportedOperation。
    """
    try:
        return open("/dev/tty", mode, encoding="utf-8")
    except OSError:
        return None


def _say_text(text: str) -> None:
    """播报文本不在这里打印 —— 它已经作为本次工具调用的参数进了记录。

    原来往控制终端（无终端时 stderr）打一行 `[speak] <文本>`，与 role-watch 的
    `[工具] speak {…}` 行是同一句话（scripts/role-watch.sh），而 watcher 的每一行同时
    就是逐轮记录的落盘内容：多打这一行会让终端比记录多出一行，脱离终端跑时它又重复落进
    start-<role>.out。文本没有丢——会话日志里有这次调用的 arguments，逐轮记录里有同一行。

    保留函数与签名：调用点标出「这里本该播报」，也是本事实唯一写下来的地方。
    """


def _read_line(timeout: float) -> Optional[str]:
    """从命令行读取一行用户输入（替代 ASR 收音）；超时、EOF、空行均返回 None。"""
    prompt = _open_tty("w")
    stream = _open_tty("r")
    if stream is None:
        stream = sys.stdin
    if prompt is None:
        print("🎤 请输入：", file=sys.stderr, flush=True, end="")
    else:
        prompt.write("🎤 请输入：")
        prompt.flush()
    try:
        # 终端可读即说明用户已输入整行；超时则判定为没有回复
        ready, _, _ = select.select([stream], [], [], timeout)
        if not ready:
            return None
        line = stream.readline()
    finally:
        if stream is not sys.stdin:
            stream.close()
        if prompt is not None:
            prompt.close()
    text = line.strip()
    return text or None


def speak(arguments: Dict[str, Any]) -> Any:
    """speak/tts：把文本输出到命令行（1 次，非持续）"""
    text = arguments.get("text", "")
    if not text:
        return "tts: text 参数为空"
    _say_text(text)
    # 播报后记录一条 assistant 交互历史
    _append_interaction_history("assistant", text)
    return "✅ 播报完成"


def listen(arguments: Dict[str, Any]) -> Any:
    """listen：从命令行读取一行输入，作为识别到的语音文本"""
    timeout_arg = int(arguments.get("timeout") or 30)
    user_text = _read_line(timeout_arg)
    if not user_text:
        return "[ASR] 未识别到语音内容"
    # 只记录识别到的用户输入
    _append_interaction_history("user", user_text)
    return f"[ASR] {user_text}"


# 终端测试模式开关：由 chat_loop.interactor 按 terminal 参数置位，
# voice_interact 转发多轮交互时读取，保证终端模式下不真实 TTS/ASR。
_TERMINAL_MODE = False


def set_terminal_mode(enabled: bool) -> None:
    """设置终端测试模式开关（全局）。"""
    global _TERMINAL_MODE
    _TERMINAL_MODE = bool(enabled)


_VALID_VOICE_MODES = {"chat", "follow_up", "get_reply"}


def voice_interact(arguments: Dict[str, Any]) -> Any:
    """voice_interact：通过多轮语音交互达成目标（转发给 chat_loop 的 interactor）"""
    mode = arguments.get("mode", "")
    if mode not in _VALID_VOICE_MODES:
        return (
            f"voice_interact: 无效的 mode={mode!r}，"
            f"可用模式: {', '.join(sorted(_VALID_VOICE_MODES))}"
        )
    task_id = arguments.get("task_id", "")
    init_text = arguments.get("init_text", "")
    if not init_text:
        return "voice_interact: init_text 参数为空"
    if not task_id:
        return "voice_interact: task_id 参数为空（需由系统注入）"

    from Agents.chat.chat_loop import interactor

    data = interactor(
        mode=mode,
        init_text=init_text,
        task_id=task_id,
        terminal=_TERMINAL_MODE,
    )
    # interactor 现返回字典 data = {"status", "reason", "info", "task_id", "interaction_text"}；
    # follow_up 模式 info 为任务信息 dict（顶层含 task_id），chat/get_reply 模式为空 dict {}
    task_status = data.get("status")
    task_info = data.get("info") or {}
    # 顶层 task_id 优先（replan 时 interactor 已更新 tm.task_id）；兜底取 info 内 task_id
    new_task_id = (
        data.get("task_id")
        or (task_info.get("task_id") if isinstance(task_info, dict) else None)
        or task_id
    )
    # 返回结构化 JSON（含完整任务信息与本次交互对话），由调用方（task_loop 等）
    # 同步回主循环；interaction_text 随工具结果进入 progress，下一轮暴露给主循环 LLM
    return json.dumps(
        {
            "task_status": task_status,
            "task_id": new_task_id,
            "info": task_info,
            "interaction_text": data.get("interaction_text", ""),
        },
        ensure_ascii=False,
        default=str,
    )


def _report_waiting_state(task_id: str, waiting_for_user: bool,
                          pending_question: str = "") -> None:
    """向云端上报"是否正在等待用户回答"。

    快照默认 waiting_for_user=False（ProgressManager.report_status），
    需标记等待用户时改写成 True 并附 pending_question=播报内容；
    收听结束/任务终止一律上报 False 复位，否则云端会一直按"等待用户回答"处理。
    """
    if not task_id:
        return
    try:
        from ToolManager.progress_manager import ProgressManager
        pm = ProgressManager(task_id=task_id, agent_type="task")
        snapshot = pm.report_status()
        snapshot["waiting_for_user"] = bool(waiting_for_user)
        if waiting_for_user:
            snapshot["pending_question"] = pending_question
        send_task_progress(snapshot)
    except Exception as exc:
        print(f"    [speak_listen] 上报等待用户状态失败: {exc}")


def speak_listen(arguments: Dict[str, Any]) -> Any:
    """speak_listen：先向命令行输出文本，再读取一行用户回复，完成一轮交互。

    云端"等待用户回答"状态的上报保持不变：输出后上报
    send_task_progress(waiting_for_user=true, pending_question=播报内容)，
    随后从命令行读取用户回复；读数结束（拿到回复或超时）后再上报
    waiting_for_user=false，成对复位云端"等待回答"状态。
    """
    # 参数名兼容：工具 schema 定义为 text，历史实现用 init_text，两处都接受
    init_text = arguments.get("text") or arguments.get("init_text") or ""
    if not init_text:
        return "speak_listen: text 参数为空"
    listen_timeout = int(arguments.get("listen_timeout") or 10)
    task_id = str(arguments.get("task_id") or "").strip()

    # ---- speak 部分：把文本输出到命令行 ----
    _say_text(init_text)
    speak_out = "✅ 播报完成"
    # 播报后记录一条 assistant 交互历史
    _append_interaction_history("assistant", init_text)

    # ---- 上报"正在等待用户"，pending_question=播报内容（播报即上报）----
    _report_waiting_state(task_id, waiting_for_user=True, pending_question=init_text)

    # ---- listen 部分：从命令行读取用户回复 ----
    user_text = _read_line(listen_timeout)

    # ---- 读数结束（拿到回复或超时）：复位 waiting_for_user=False ----
    # 与上方播报后置 True 成对，否则云端会一直按"等待用户回答"处理。
    _report_waiting_state(task_id, waiting_for_user=False)

    if user_text:
        # 只记录读取到的用户输入
        _append_interaction_history("user", user_text)
        asr_out = f"[ASR] {user_text}"
    else:
        asr_out = "[ASR] 未识别到语音内容"

    return f"{speak_out}\n{asr_out}"


_VALID_ACTIONS = {
    "dance", "danceV2", "happy", "stretch",
    "standUp", "sitDown", "getDown",
    "greeting", "fingerHeart", "newYearCall",
    "leap", "jumpForward", "walkUpsideDown", "standErect",
}
def do_action(arguments: Dict[str, Any]) -> Any:
    """do_action：控制机器狗执行指定动作"""
    action_type = arguments.get("action_type", "")
    if not action_type:
        return "do_action: action_type 参数为空"
    # 与 robot-dog-actions 技能保持一致的动作白名单，防止把无效动作发给机器人
    if action_type not in _VALID_ACTIONS:
        return (
            f"do_action: 无效的 action_type={action_type!r}，"
            f"可用动作: {', '.join(sorted(_VALID_ACTIONS))}"
        )
    result = subprocess.run(
        ["bash", ACTION_SCRIPT, action_type],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        return f"✅ 动作执行完成: {result.stdout.strip()}"
    raise RuntimeError(
        f"do_action 执行失败 (code={result.returncode}): {result.stderr or result.stdout}"
    )


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
    "image": f"python {VISION_DISPATCH_SCRIPT} --mode image --image {{IMAGE_PATH}} --prompt {{PROMPT}}",
    "front": f"python {VISION_DISPATCH_SCRIPT} --mode front --prompt {{PROMPT}}",
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


def _to_bool(value: Any) -> bool:
    """将传入的布尔值规范化（兼容字符串 "true"/"1"/"yes"）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)



def look(arguments: Dict[str, Any]) -> Any:
    """look：视觉理解（image 单图分析 / front 前方感知）。

    原地环绕搜索（360° 找目标）已拆分为独立工具 search_old，不再走本工具。
    """
    mode = arguments.get("mode", "")
    prompt = arguments.get("prompt", "")
    image_path = arguments.get("image_path", "")
    cmd_template = _VISION_MODES.get(mode, "")
    if not cmd_template:
        return f"look: 未知 mode={mode}，可用: {list(_VISION_MODES.keys())}"
    if mode == "image" and not image_path:
        return "look [image]: image_path 参数必填"
    cmd_str = cmd_template.replace("{IMAGE_PATH}", image_path).replace("{PROMPT}", prompt)
    result = subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"look [{mode}] 执行失败 (code={result.returncode}): {result.stderr}")
    return result.stdout.strip()


def search_old(arguments: Dict[str, Any]) -> Any:
    """search_old：原地环绕搜索（原 vision 的 surround，360° 找目标）。

    机器狗原地旋转一周、拍摄并分析周围四视角照片，寻找 prompt 描述的目标。
    找到目标（found=true 且解析出 region/x_hirizon）后总是调用 turn2subject.sh 转向该目标；
    interact_with_target 参数当前不生效（沿用 2026-09-02 起的无条件转向行为），仅保留兼容接收。
    """
    prompt = arguments.get("prompt", "")
    if not prompt:
        return "search_old: prompt 参数不能为空"
    cmd = f"python {VISION_DISPATCH_SCRIPT} --mode surround --prompt {prompt}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
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
            out += f"\n[interact] 已转向目标:\n{turn.stdout.strip()}"
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


def bash(arguments: Dict[str, Any]) -> Any:
    """bash：直接调用 subprocess 执行命令"""
    cmd = arguments.get("command", "")
    if not cmd:
        return "bash: command 参数为空"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"bash 命令执行失败 (code={result.returncode}): {result.stderr}")
    out = result.stdout.strip()
    if result.stderr:
        out = f"{out}\n[stderr] {result.stderr.strip()}"
    return out or "(空输出)"


# 导航轮询参数
_NAVIGATION_POLL_INTERVAL = 1.0   # 秒，导航状态轮询间隔
_NAVIGATION_TIMEOUT = 300         # 秒，导航整体超时上限，防止无限等待


def _waypoint_nav_supported_targets() -> set:
    """返回导航支持的目标地标集合（target 的选择范围）。

    由两部分取并集：
        load_supported_locations()   — configs.configs 读取 SUPPORTED_LOCATIONS_FILE 的名单
        landmarks.json 的 name 字段  — 导航技能脚本目录下的地标清单
    以 landmarks.json 为动态来源，新增地标时无需再改代码。

    landmarks.json 缺失或解析失败时，静默退化为仅使用 SUPPORTED_LOCATIONS_FILE 名单。
    """
    from configs.configs import load_supported_locations

    targets = set(load_supported_locations())
    try:
        data = json.loads(
            NAVIGATION_LANDMARKS_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return targets
    if isinstance(data, list):
        targets |= {
            item.get("name")
            for item in data
            if isinstance(item, dict) and item.get("name")
        }
    return targets


def waypoint_nav_supported_targets() -> List[str]:
    """导航支持的目标地标列表（已排序）。

    供 tool_parser 注入到 waypoint_nav 工具说明中，让 LLM 选择合法 target，
    与 waypoint_nav() 的校验集合（SUPPORTED_LOCATIONS_FILE ∪ landmarks.json name）保持同步。
    """
    # print(sorted(_waypoint_nav_supported_targets()))
    return sorted(_waypoint_nav_supported_targets())


def waypoint_nav(arguments: Dict[str, Any]) -> Any:
    """waypoint_nav：定点导航至指定地标。

    调用导航后端 HTTP 接口发起导航（POST /api/v1/navigation/go），
    随后每隔 1 秒轮询导航状态（GET /api/v1/navigation/status）：
        SUCCESS            → 返回成功，进入下一步骤
        FAILED / CANCELLED → 结束工具调用，返回终止状态
        RUNNING / IDLE     → 继续等待导航完成
    """
    # return f"已经到达目的地{arguments}"
    import time
    import requests

    t0=time.time()

    from configs.configs import (
        NAVIGATION_GO_URL,
        NAVIGATION_STATUS_URL,
    )

    # 参数名兼容：YAML 定义为 target，同时兼容 destination
    landmark = str(arguments.get("target") or arguments.get("destination") or "").strip()
    if not landmark:
        raise RuntimeError("waypoint_nav: target 参数为空")

    # 校验目标地标是否在支持范围内（SUPPORTED_LOCATIONS_FILE ∪ landmarks.json name 字段），
    # 不在范围内直接失败
    supported_targets = _waypoint_nav_supported_targets()
    if landmark not in supported_targets:
        raise RuntimeError(
            f"waypoint_nav: 目标地标 {landmark!r} 不在支持范围内，"
            f"可用地点: {', '.join(sorted(supported_targets))}"
        )

    # ---- 发起导航 ----
    try:
        resp = requests.post(
            NAVIGATION_GO_URL,
            json={"destination": landmark},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        raise RuntimeError(f"waypoint_nav 发起导航失败: {e}")
    if body.get("code") != 0:
        raise RuntimeError(
            f"waypoint_nav 发起导航失败: {body.get('message', body)}"
        )
    # ---- 轮询导航状态，直至任务结束 ----
    deadline = time.time() + _NAVIGATION_TIMEOUT
    while time.time() < deadline:
        time.sleep(_NAVIGATION_POLL_INTERVAL)
        try:
            status_resp = requests.get(NAVIGATION_STATUS_URL, timeout=10)
            status_resp.raise_for_status()
            status_body = status_resp.json()
            status = (status_body.get("data") or {}).get("status", "")
        except Exception as e:
            raise RuntimeError(f"waypoint_nav 查询状态失败: {e}")

        if status == "SUCCESS":
            return "✅ 导航成功到达目的地"
        if status in ("FAILED", "CANCELLED"):
            destination = (status_body.get("data") or {}).get("destination")
            raise RuntimeError(
                f"navigation 导航终止（status={status}，destination={destination}）"
            )
        # IDLE / RUNNING / 未知状态：继续等待

    raise RuntimeError(f"navigation 导航超时（{_NAVIGATION_TIMEOUT}s 内未完成）")


def task_replan(arguments: Dict[str, Any]) -> Any:
    """task_replan：根据任务描述重新规划任务（转发给 InfoCompleter.formulator）"""
    task_description = arguments.get("task_description", "")
    if not task_description:
        return "task_replan: task_description 参数为空"

    from InfoCompleter.formulator import formulate_all

    result = formulate_all(task_description)

    return json.dumps(result, ensure_ascii=False, indent=2)


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


# 内置工具名 → 独立执行函数
_BUILTIN_EXECUTORS: Dict[str, callable] = {
    "speak": speak,
    "tts": speak,
    "listen": listen,
    "voice_interact": voice_interact,
    "speak_listen": speak_listen,
    "do_action": do_action,
    "move": move,
    "look": look,
    "search_old": search_old,
    "search_and_approach": search_and_approach,
    "bash": bash,
    "waypoint_nav": waypoint_nav,
    "vision_language_nav": vision_language_nav,
    "task_replan": task_replan,
}


# ─────────────────────────────────────────────────────────────────────────────
# ToolExecutor — 构造、注册并执行工具
# ─────────────────────────────────────────────────────────────────────────────

class ToolExecutor:
    """
    根据工具条目（extra 配置）构造执行函数并维护执行器注册表，
    提供 OpenAI tool_calls 的解析与分发执行。

    Attributes:
        _executors — 工具名 → 执行函数 的注册表
    """

    def __init__(
        self,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._executors: Dict[str, callable] = {}
        # REPORT_TOOLS 预播报文案（LLM user_reply）：由主循环经 set_pre_announce 注入，
        # parse_and_execute 一次性消费，在调用 REPORT_TOOLS 工具前先播报再执行技能。
        self._pre_announce_text: str = ""
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

        内置工具（speak/listen/voice_interact 等）映射到模块级的 {tool_name} 函数；
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

    def set_pre_announce(self, text: str) -> None:
        """设置下一次 parse_and_execute 在调用 REPORT_TOOLS 工具前要播报的文案。

        由主循环（task_loop）把 LLM 本轮的 user_reply 注入进来；
        文案为一次性，parse_and_execute 消费后即清空。
        """
        self._pre_announce_text = text or ""

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
            task_id:       当前任务 ID（透传给执行器，voice_interact 等需要）

        Returns:
            工具执行结果字典列表，每项包含 tool_name、params、result、status
        """
        # —— REPORT_TOOLS 预播报（一次性）——
        # task_loop 等主循环在调用前已把 LLM 的 user_reply 经 set_pre_announce 注入。
        # 取出后立即清空：本批次没用上就丢弃，避免残留播报到后续批次。
        pre_announce = self._pre_announce_text
        self._pre_announce_text = ""
        announced = False

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

            # 系统级参数注入：voice_interact 需要当前任务 ID，由主循环透传，始终覆盖 LLM 填的值
            if name == "voice_interact":
                arguments["task_id"] = task_id or ""
            # speak_listen 需要当前任务 ID 定位进度文件（task_progress.yaml）以构造上报快照，由主循环透传
            if name == "speak_listen":
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

            # —— REPORT_TOOLS 预播报：先播报，再执行技能 ——
            # 调用 configs.configs.REPORT_TOOLS（上报/播报类工具，如 look / waypoint_nav）
            # 中的工具前，先用 tts 播报大模型的 user_reply（告知用户任务进展），
            # 播完再执行技能。同一批只播报一次。
            if (
                pre_announce
                and not announced
                and name in REPORT_TOOLS
            ):
                announced = True
                try:
                    speak({"text": pre_announce})
                except Exception as e:
                    print(f"[ToolExecutor] 播报 user_reply 失败: {e}", file=sys.stderr)

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