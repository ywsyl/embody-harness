"""语音播报与收听 —— 五个语音工具的实现（工具入口留在 plugin_tools/tool_executor.py）。

tool_executor 的 speak / listen / speak_listen / audio_interact / speak_input 各只做
「取参数 → 调本模块 → 拼模型可见的回执文本」，真实语音 I/O 与整段流程都在这里：

    播报  speak_text()          一次调用交给 TTS（常驻服务，或退回脚本）
    收音  collect_speech()      拉一次本地 ASR 收音
          read_tty_line()       从控制终端读一行（本地联调）
    整段  speak_and_listen()    speak_listen 的流程
          run_audio_interact()  audio_interact 的流程
    辅助  bool_arg()            工具参数 → 脚本期望的小写 true/false
          ended_by_text()       audio_interact 的结束原因 → 一句人话
    历史  _append_interaction_history()（现无调用方，触发点都注释着）

播报调的是机器人上的服务，不是命令行替身：走 TTS_Manager/tts_client.py（常驻服务在 →
socket；不在 → 退回 skills/tts-speak/scripts/run_tts_speak.sh），两边都是一次调用交给
/audio_center/play_tts。**客户端与设备都没有队列**：设备每收到一条新请求都先停掉在播的那条，
要连着说得等前一句播完（见 skills/tts-speak/SKILL.md 的「排队」一节）。

"听"分两条路，取决于这一轮用户的话从哪来：
    collect_speech  → 拉一次本地 ASR 收音（skills/asr-listener/scripts/run_asr.sh）：
                      唤醒设备、收设备侧 ASR 认出来的用户语音。listen / speak_listen 共用。
    read_tty_line   → 从控制终端读一行。本地联调用：没有云端接线时顶替上面那条。
云端还有一条 answer 事件的路（落盘单槽 Communicator/pending_answer.py），listen /
speak_listen 曾走它；2026-10-08 改回本地 ASR 后**没有消费方**（写侧自 2026-09-28 停用），
只留下注释掉的 _await_cloud_answer，恢复那条链路时放开它。

子进程的 stdin/stdout 都不占用：`plugin_tools.bridge call` 的 arguments 走 stdin、
结果 JSON 走 stdout，占了会让协议读到 EOF。读终端因此走 /dev/tty 而不是 stdin。
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 与 tool_executor/bridge 一致：把仓库根（embody/）放进搜索路径，
# 这样直接执行本文件与 `-m plugin_tools.audio_tools` 都能 import configs.*。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml

from configs.configs import (
    ASR_TOOL_TIMEOUT,
    AUDIO_INTERACT_TOOL_TIMEOUT,
    SPEECH_ITEM_STALE_SEC,
    SPEECH_QUEUE_DRAIN_MAX_WAIT,
    TTS_TOOL_TIMEOUT,
)
from configs.paths import (
    ASR_SCRIPT,
    AUDIO_INTERACT_SCRIPT,
    RESTORE_WAKEUP_REPLY_SCRIPT,
    RUNTIME_INTERACTION_HISTORY_FILE,
    RUNTIME_SPOKEN_STREAM_FILE,
    WAIT_TTS_DONE_SCRIPT,
)

# [2026-10-08 停用] 云端 answer 单槽：原 listen / speak_listen 从这儿取用户回话，两者改回本地
# ASR 后没有消费方（写侧也自 2026-09-28 停用，见 Communicator/receiver.py）。恢复云端 answer
# 链路时连同下面的 _await_cloud_answer 一起放开。
# from Communicator import pending_answer
from Communicator.sender import report_waiting_state

# 播报唯一入口：常驻 TTS 服务可达就走 socket，连不上自己退回 skills/tts-speak 的脚本
# （回退逻辑在那边，本模块不需要模式分支）。
from TTS_Manager import tts_client


# ─────────────────────────────────────────────────────────────────────────────
# 交互历史记录（interaction_history.yaml 读写）
# ─────────────────────────────────────────────────────────────────────────────

def _append_interaction_history(role: str, content: str) -> None:
    """
    往 runtime_files/interaction_history.yaml 追加一条记录：
        {"role": ..., "content": ..., "timestamp": "YYYY-mm-dd HH:MM:SS"}

    由语音工具在收工前触发（**现无调用方，触发点都注释着**）：
        speak / tts / speak_listen / speak_input 的播报部分
                                     → _append_interaction_history("assistant", text)
        listen / speak_listen 收下的设备侧 ASR 文本、audio_interact 的同名结果、
        speak_input 读到的终端输入    → _append_interaction_history("user", text)

    并发安全：
        多个角色的工具进程（tools-bridge 各起一个 python）可能同时写入同一文件。
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
# 工具参数辅助
# ─────────────────────────────────────────────────────────────────────────────

def _to_bool(value: Any) -> bool:
    """将传入的布尔值规范化（兼容字符串 "true"/"1"/"yes"）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def bool_arg(value: Any, default: bool = True) -> str:
    """把工具参数转成脚本期望的小写 true/false。

    schema 里声明的是 boolean，但 LLM 偶尔会传字符串（"false" / "0" / "否"），直接 bool()
    会把 "false" 判成 True——归一化只走 _to_bool 一处，本函数只负责补缺省值与转字符串。
    """
    return "true" if _to_bool(default if value is None else value) else "false"


# [2026-10-08 停用] 等云端 answer 事件——listen / speak_listen 曾用它，现两者改走本地 ASR
# （见下面的 collect_speech）。上面 `from Communicator import pending_answer` 也已注释。恢复
# 时一并放开这三处（含 configs.configs.CLOUD_ANSWER_WAIT），并确认 receiver 那两条腿已恢复。
# def _await_cloud_answer() -> str:
#     """等一条云端 answer 事件，返回用户这一轮的话（超时返回空串）。
#
#     base 取**进本函数时**的 seq 而不是 0：wait_for_answer 只认"比 base 新"的那一条，
#     否则上一次调用留在槽里的旧回答会被当成这一轮的回答取走。
#
#     取到就记一条 user 交互历史（超时不算一轮，不记）。
#     """
#     text = pending_answer.wait_for_answer(pending_answer.latest_seq(), CLOUD_ANSWER_WAIT)
#     if text:
#         _append_interaction_history("user", text)
#     return text


# ─────────────────────────────────────────────────────────────────────────────
# 收音（"听"）
# ─────────────────────────────────────────────────────────────────────────────

def _restore_wakeup_reply() -> None:
    """兜底恢复唤醒回应音频（wakeup04~10.wav，清单见 configs.paths.WAKEUP_REPLY_FILES）。

    run_asr.sh 的 asr_monitor 会在收尾的 finally 里把静音掉的唤醒回应还原，但它被 SIGKILL
    时 finally 来不及跑，那几个 wav 会**永久停在静音态** —— 之后真实唤醒就没有回应音了。
    所以父进程在超时后必须补调一次（脚本幂等，重跑只是再 copy 一遍）。

    备份缺失时脚本会 exit 1，故 check=False：恢复失败不该盖掉原本的超时错误。
    """
    try:
        subprocess.run(
            ["bash", str(RESTORE_WAKEUP_REPLY_SCRIPT)],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception as exc:
        print(f"    [asr] 恢复唤醒回应失败（不影响收音结果）: {exc}")


def _run_asr() -> subprocess.CompletedProcess:
    """拉起一次 ASR 收音，只负责封顶，不干预脚本内部的收音窗口。

    **不传 --timeout**：收音窗口由脚本自己定义（asr_monitor.py 的默认值，取自
    configs.ASR_LISTEN_WINDOW），工具层只把子进程总时长封在 ASR_TOOL_TIMEOUT 内，避免两处
    各写一个超时值互相漂移。

    单独起进程组（start_new_session=True）：run_asr.sh 下面还有 python 孙进程
    （asr_monitor.py），超时只杀 bash 会把孙进程留成孤儿继续占着 ASR 会话，导致之后真实唤醒
    唤不动。故超时杀整个进程组，再补一次唤醒回应音频恢复。

    超时仍以 subprocess.TimeoutExpired 抛出，由调用方决定怎么处理。

    已知遗留：tools-bridge 被硬取消时只对桥进程发 SIGTERM（dsh-plugins/tools-bridge），
    本函数所在的 python 桥先死了，那记 killpg 就跑不到，ASR 孙进程可能成孤儿；兜底是
    asr_monitor 自己的收音窗口到点自退。
    """
    proc = subprocess.Popen(
        ["bash", str(ASR_SCRIPT)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=ASR_TOOL_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception as exc:
                print(f"    [asr] 超时后终止子进程失败: {exc}")
        # 收尸。带上限是必要的：万一上面两种杀法都没成功，无上限的 communicate() 会在这里
        # 永久挂死，把"兜底"变成新的故障点。
        try:
            proc.communicate(timeout=5)
        except Exception as exc:
            print(f"    [asr] 超时后回收子进程失败（已忽略）: {exc}")
        # 无论收尸成败都要恢复唤醒回应，再把超时抛出去
        _restore_wakeup_reply()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def collect_speech() -> str:
    """拉一次本地 ASR 收音，返回用户这一轮说的话（没收到返回空串）。

    listen 与 speak_listen 的"听"都走这里；两者只管各自的措辞，收音逻辑只有这一份。

    超时**不算错误**：人可能没说话、设备可能没醒过来，返回空串让调用方按"未收到"收场，
    与老 listen 的语义一致（脚本内部的收音窗口到点自退才是常规路径，这里的超时是兜底）。
    识别到的文本照旧记一条 user 交互历史（原先记在 _await_cloud_answer 里）。
    """
    try:
        result = _run_asr()
    except subprocess.TimeoutExpired:
        print(f"    [asr] 子进程 {ASR_TOOL_TIMEOUT}s 未返回，按未收到处理")
        return ""
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"asr 执行失败 (code={result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    # code=0 识别成功（stdout 是 `[ASR] xxx`）；code=1 未识别到语音（也是正常结果）
    out = (result.stdout or "").strip()
    if result.returncode != 0 or not out:
        return ""
    # Python 3.8 无 str.removeprefix，用切片等价实现
    text = out[len("[ASR] "):].strip() if out.startswith("[ASR] ") else out.strip()
    # if text:
    #     _append_interaction_history("user", text)
    return text


def read_tty_line(timeout: float) -> Tuple[bool, str]:
    """从控制终端读一行，返回 (是否读得动, 文本)。

    **读 /dev/tty 而不是 stdin**：调用方是 tools-bridge 的子进程，stdin 是 JSON-RPC
    管道（arguments 从那里进来），读它会吃掉协议数据。打开 /dev/tty 需要本进程还挂在
    一个控制终端上 —— 角色被 setsid / nohup 之类摘掉终端时打不开，返回 False，
    由调用方转成错误文本（不抛异常：那只是"这个入口在这儿用不了"）。

    超时（窗口内没敲下回车）返回 (True, "")；读到 EOF 也按此收场。

    超时窗口由调用方给（speak_input 传 configs.INPUT_WAIT），本函数不自己取配置。
    """
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError as exc:
        print(f"    [speak_input] 打不开 /dev/tty: {exc}")
        return False, ""
    try:
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return True, ""
            if not select.select([fd], [], [], remain)[0]:
                return True, ""
            # 一次读 1 字节：终端是逐字符给的，读完这一行就停，不越过它去读下一行
            chunk = os.read(fd, 1)
            if not chunk or chunk == b"\n":
                # _append_interaction_history("user", buf.decode("utf-8", "replace").strip())
                return True, buf.decode("utf-8", "replace").strip()
            buf += chunk
    finally:
        os.close(fd)


# ─────────────────────────────────────────────────────────────────────────────
# 播报（"说"）
# ─────────────────────────────────────────────────────────────────────────────

def _spoken_payload() -> Optional[Dict[str, Any]]:
    """读一次插件的播报记录文件（只有插件写、这里只读）；没有 / 读不动 / 不是对象都返回 None。

    坏文件按「没有」处理：少播一次不算错，重复播才是（见 _spoken_record）。
    """
    try:
        raw = RUNTIME_SPOKEN_STREAM_FILE.read_text(encoding="utf-8")
    except OSError:
        return None                   # 没有记录文件 = 插件没装载 = 老路
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _spoken_record(text: str) -> Optional[Tuple[int, float]]:
    """读 dsh 插件留下的播报记录，返回这段 text 的 (已交给 TTS 的字数, 记录时刻)；
    没有匹配的记录返回 None（= 一个字都没播，或记录不可用）。

    交互器的插件（dsh-plugins/speech-stream）在 speak / speak_listen / speak_input 的 text
    参数**还在逐片到达**时就把已经成句的部分送进了 TTS（首句出声从「整段生成完 + 派发 +
    调服务」提前到「第一句生成完」）。工具被派发时因此可能晚于出声：整段都播过就只等播完，
    播了一半就只补剩下的那截（见 speak_text）。

    记录文件只有插件写、这里只读（configs/paths.py 的 RUNTIME_SPOKEN_STREAM_FILE）。插件没
    装载 / 刚重启 / 记录过期（SPEECH_ITEM_STALE_SEC）都返回 None，即**照今天的老路整段播** ——
    开关插件等于开关流式播报，本模块不需要模式分支。

    匹配规则：取 text **完全相同**里最新的那条。同一次调用的 text 会随分片逐次变长（插件
    每交接一句覆盖一次记录），最新的那条就是最终答案。**不做认领**：桥是「一次工具调用一个
    python 进程」（dsh-plugins/tools-bridge 每次 spawn 一个），进程内记不住；写回认领标记又要
    打破「记录文件只有一个写者」。代价是同一轮里用完全相同的文本说两遍时，第二遍会跟着第一遍
    一起被认成已播 —— 用户听到一次而不是两次，取舍记在这里。

    返回的时刻是**这段文字最后一次被交给 TTS 的时刻**，等队列播完时拿它和 `drained_at` 比
    （见 _wait_streamed_done）。
    """
    payload = _spoken_payload()
    if payload is None:
        return None
    items = payload.get("items") or []
    if not isinstance(items, list):
        return None
    newest: Optional[int] = None
    newest_at = float("-inf")
    now = time.time()
    for item in items:
        if not isinstance(item, dict) or item.get("text") != text:
            continue
        at = item.get("at")
        spoken = item.get("spoken")
        if not isinstance(at, (int, float)) or now - at > SPEECH_ITEM_STALE_SEC:
            continue
        if not isinstance(spoken, int) or spoken < 0:
            continue
        if at > newest_at:
            newest_at = float(at)
            newest = min(spoken, len(text))
    return None if newest is None else (newest, newest_at)


def _wait_streamed_done(text: str, at: float) -> None:
    """等插件那条逐句队列把这段文字播完：记录文件里的 `drained_at` 越过 `at` 就返回。

    播报是插件逐句送出去的，工具层拿不到 `section_id`，所以**不能用状态话题上的一次
    `status=0 Idle` 判播完**（那是 `_wait_tts_done` 干的事）：两句之间本来就有一段空档
    （等播完的静默窗口 + 设备的 stop/runInquiry，约 0.8s），那段时间话题上就是 Idle，按它判
    会返回到**两句中间** —— 后面几句会被
    随后的唤醒掐掉，而 speak_listen 还会把没播完的自己人声收进麦克风。`drained_at` 是队列见底
    （每句都等到播完才退；某句起不来/非 0 退出也算它到此为止）的时刻，是唯一能回答「这几句
    都不会再出声了」的事实。

    记录文件里没有 `drained_at`（插件还是老版本、daemon 没重起）时不在这里空等到上限，
    退回 `_wait_tts_done` 的老判定。
    """
    deadline = time.monotonic() + SPEECH_QUEUE_DRAIN_MAX_WAIT
    while True:
        payload = _spoken_payload()
        if payload is None or "drained_at" not in payload:
            _wait_tts_done(text)
            return
        drained = payload.get("drained_at")
        if isinstance(drained, (int, float)) and drained >= at:
            return
        if time.monotonic() >= deadline:
            print(f'[TTS] ⚠ 等逐句队列播完超过 {SPEECH_QUEUE_DRAIN_MAX_WAIT}s'
                  f'（drained_at={drained!r}，本条记录 at={at:.2f}），按上限返回', file=sys.stderr)
            return
        time.sleep(0.2)               # 轮询间隔：只读一份 JSON，与出声时长比可以忽略


def _wait_tts_done(text: str) -> None:
    """等一次**已经在放**的播报播完（播报是插件送出去的，拿不到它的 section_id）。

    现在是**老版本插件的兜底**（记录文件里没有 `drained_at`）：只剩这一条路时才用得上，见
    _wait_streamed_done。插件整段没装载的话根本走不到这里（那是 speak_text 的直接播报路，
    由 speak_and_wait 带着自己的 section_id 等）。

    **这条路不走常驻服务的 drained**：那些句子是插件逐句起脚本送出去的，服务没经手、没有
    交接记账，它的队列永远是空的、永远不报见底。所以这里仍旧跑
    skills/tts-speak/scripts/wait_tts_done.sh —— source ROS 环境后订阅状态话题等
    status=0 Idle（判定在 TTS_Manager/speech_status.py，全仓一份）。脚本恒退出 0，故这里
    不看返回码；服务在不在都不影响这条路。
    """
    subprocess.run(
        [str(WAIT_TTS_DONE_SCRIPT), text],
        capture_output=True, text=True, timeout=TTS_TOOL_TIMEOUT,
    )


def speak_text(text: str, play_now: str, wait_done: str) -> None:
    """播报一段话：插件已经送出去的部分不重发，只补没播过的、并按需等播完。

    speak / speak_listen / speak_input 共用的播报入口（原先三处各写一遍 subprocess.run）。
    拆分规则见 _spoken_record。

    真的要说的时候走 `tts_client`（常驻服务 socket；连不上它自己退回 run_tts_speak.sh），
    所以这里不再直接起脚本。

    play_now / wait_done 是 bool_arg() 转出的小写 "true"/"false"（本函数的判定按这个拼写比）。
    """
    # 播报后记一条 assistant 交互历史（现停用）。speak / speak_listen / speak_input 三处
    # 调用都汇到这里，恢复时放这一处即可（两条路径都要记，别只放在末尾）：
    # _append_interaction_history("assistant", text)
    record = _spoken_record(text)
    if record is not None and record[0] >= len(text):
        # 整段都已经由插件送出去：不再调 TTS，但 wait_done 的语义必须保住 ——
        # speak_listen 不等播完就开始收音，会把机器自己的声音收进去
        # （ASR 的 Wakeup 还会给在播的 TTS 下发 UserCancel）。等的是插件那条逐句队列见底，
        # 不是话题上的一次 Idle（两句之间本来就是 Idle，见 _wait_streamed_done）。
        if wait_done == "true":
            _wait_streamed_done(text, record[1])
        return
    spoken = 0 if record is None else record[0]
    # 播到一半：只补剩下的那截，且**一律不抢麦**（play_now=false）—— 插件送出去的句子还在播，
    # 这里再打断一次会把没说完的那段切断（设备没有队列，新请求会停掉在播的）。补送的是参数
    # **原文**（与今天整段送一样、不做净化），插件送出的是净化后的文本；接缝处可能多念一两个
    # markdown 标记符，可接受。真的插队风险在 wait_done=false 那条路上：那时不等 drained 就
    # 送，正好赶上插件在播就会有截断——要接着说话就传 wait_done=true。
    # 等播完（wait_done=true）时用 speak_and_wait：服务在就等它广播的 drained，服务不在就
    # 退回脚本的 wait_done=true（脚本自己订阅状态话题等 status=0 Idle）。
    speak_now = play_now == "true" and spoken == 0
    if wait_done == "true":
        tts_client.speak_and_wait(text[spoken:], play_now=speak_now)
    else:
        tts_client.speak(text[spoken:], play_now=speak_now)


# ─────────────────────────────────────────────────────────────────────────────
# 整段流程（播报 + 收音）
# ─────────────────────────────────────────────────────────────────────────────

def ended_by_text(payload: Dict[str, Any]) -> str:
    """把脚本报回的结束原因渲染成一句人话（模型读的是这句）。

    注意 `waited_sec` 是整个工具的耗时（含播报），不是"等用户回复"那一段的时长。
    """
    ended_by = payload.get("ended_by")
    waited = payload.get("waited_sec")
    if ended_by == 11:
        return f"唤醒后设备进入静默等待，全程 {waited}s 内没人再说话"
    if ended_by == 0:
        return f"唤醒后设备回到空闲，全程 {waited}s 内没人再说话"
    return f"等满 {waited}s 未见『这一轮动过（1/2/3/9）→ 静默等待/空闲』"


# audio_interact 的"正在跑"标记（脚本自己 touch/rm，本模块只兜 SIGKILL 那条路，见下）。
_AUDIO_INTERACT_FLAG = Path("/tmp/.fdx")


def speak_and_listen(text: str, wait_done: str, task_id: str) -> str:
    """一轮"说一句、听一句"：播报 text，再拉一次本地 ASR 收音，返回用户的回答（没收到空串）。

    speak_listen 的整段流程。**播报必须排在收音前面**：本地 TTS 开口会先拆掉当前语音会话、
    另起一个不收音的 TTS 会话（2026-09-28 实测，见 skills/audio_interact 的模块 docstring），
    先收音再播报等于自己把刚拉起来的会话拆了，用户说什么都收不到。播报**不主动打断在播的**
    （play_now 写死 false）——要抢麦是 audio_interact 的事。

    云端"等待用户回答"的上报成对：**播报之前**置 waiting_for_user=true
    （wait_done="true" 时播报调用会阻塞到播完，上报排在它会晚几十秒，可能错过用户的回答
    窗口），拿到回答（或超时）后再置 false 复位；播报失败那条路径单独复位一次，
    不留悬空。复位放在 finally：万一收音那段出意外，同样不能把云端留在"等待用户回答"。
    """
    # ---- 上报"正在等待用户"，pending_question=播报内容（播报即上报，不等播完）----
    report_waiting_state(task_id, waiting_for_user=True, pending_question=text)

    # ---- speak 部分：TTS 播报（缺省等这段播完再听、不主动打断在播的）----
    try:
        speak_text(text, "false", wait_done)
    except Exception:
        # 播报没成功：复位"等待回答"，否则云端会一直按"等待用户回答"处理
        report_waiting_state(task_id, waiting_for_user=False)
        raise

    # ---- listen 部分：本地 ASR 收音，没收到按"未收到用户语音"收场，不把这轮丢掉 ----
    try:
        return collect_speech()
    finally:
        # 收到或超时都复位（与上方成对）
        report_waiting_state(task_id, waiting_for_user=False)


def run_audio_interact(text: str, task_id: str) -> Any:
    """跑一次 audio_interact 的整段流程，返回技能脚本那一行 JSON（dict）。

    整段流程在 skills/audio_interact/（播报复用 tts-speak、唤醒复用 asr-listener），
    **播报排在唤醒前面**：设备的播报服务会先拆掉当前语音会话、另起一个不收音的 TTS 会话，
    先唤醒再播报等于自己把收音会话拆了（2026-09-28 实测，见该技能 docstring/SKILL.md）。
    判定不在本模块：脚本自己订阅状态话题，**收到唤醒之后这一轮的最终 ASR（status=2 且
    finish=true）就立刻收工**，不等设备的 11/0 —— 收音会话开着时设备根本不发 11/0
    （2026-10-09 实测：干等 21s、到期还播一句错误提示，见该技能 docstring）。一直没人说话
    才走静默窗口：唤醒之后这一轮真的动过（1/2/3/9 任一）且首次收到 11（静默等待）或 0（空闲）
    时转入「等用户回复」的窗口（configs.AUDIO_INTERACT_REPLY_WAIT），静默满那么多秒才退出；
    整体另有 configs.AUDIO_INTERACT_MAX_WAIT 兜底。把窗口内最后一条
    最终 ASR 文本（status=2 且 finish=true）带回。播报段的播完判定直接借 speak 那条老规则
    （run_tts_speak.sh 的 wait_done=true，等 status=0），只有收尾那段是本工具自己的。

    云端"等待用户回答"的上报与 speak_and_listen 同一套、也成对：进本函数先置
    waiting_for_user=true（不能等脚本报回来再报 —— 脚本自己就要跑几十秒，那会儿用户的回答
    窗口早过去了），跑完（含超时那条路径，复位在 finally 里）再置 false 复位。task_id 为空
    就不上报（本工具 schema 里没有 task_id，模型也填不了）：report_waiting_state 自己会空转。

    **超时、以及脚本输出不是预期 JSON，都返回一句说明文本**（不是异常）：模型看到的是
    「脚本没返回」，与工具报错分开。脚本非 0 退出抛 RuntimeError。

    `/tmp/.fdx`（"本工具正在跑"的标记）由**被调的脚本**自己管：见
    skills/audio_interact/audio_interact.sh（头 touch、EXIT trap 清）。本函数只在一处补刀
    —— 超时会 SIGKILL 掉那个 shell，trap 根本来不及跑，标记得由调用方清掉。
    """
    # ---- 上报"正在等待用户"，pending_question=播报内容（开口即上报，不等脚本跑完）----
    report_waiting_state(task_id, waiting_for_user=True, pending_question=text)

    try:
        try:
            result = subprocess.run(
                [str(AUDIO_INTERACT_SCRIPT), text],
                capture_output=True, text=True, timeout=AUDIO_INTERACT_TOOL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return (f"audio_interact: 子进程 {AUDIO_INTERACT_TOOL_TIMEOUT}s 未返回"
                    "（脚本内部的等待上限应小于它，见 configs）")
    finally:
        _AUDIO_INTERACT_FLAG.unlink(missing_ok=True)
        # 收工（或超时、或抛异常）都复位，与上方成对，不留悬空
        report_waiting_state(task_id, waiting_for_user=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"audio_interact 执行失败 (code={result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()}"
        )

    stdout = (result.stdout or "").strip()
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except (ValueError, IndexError):
        return f"audio_interact: 结果不是预期的 JSON：{stdout[:200]!r}"

    # 两半都记交互历史（没人说话时只记 assistant 那条）：
    # _append_interaction_history("assistant", text)
    # _append_interaction_history("user", str(payload.get("answer") or "").strip())
    return payload
