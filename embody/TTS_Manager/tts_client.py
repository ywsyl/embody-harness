#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
全体工具用的 TTS 入口（模块级门面）：一句播报只经这几个函数，别处不要再直接跑脚本。

三条路，按顺序选（选路集中在 _get_client / 各函数的第一段，不散在调用点）：
  1. 常驻服务可达（TTS_Manager/tts_service.py --serve，经 TTS_SERVICE_SOCKET）→ 走它。
     一句就是一条命令，句间不再有「起进程 + CLI 启动 + DDS 发现」那两段开销（见该模块头）。
  2. 服务连不上 → 退回 `skills/tts-speak/scripts/run_tts_speak.sh`（**原路，一字不改**），
     行为与接服务之前逐字相同 —— 服务没起（或 --no-tts-service）时系统照常能说话。
  3. 两边都失败 → RuntimeError（与今天脚本非 0 退出时一致，工具层据此报错）。

**只有连不上才回退**：命令已经发出去、只是没等到应答时不回退 —— 那一句很可能已经交给设备
出声了，再跑一遍脚本会把它说第二遍。这种时候如实抛出（调用方/工具层会报工具失败），
下一次调用会重新拨号（连接失败即作废）。

长连接与并发（照 skills/camera/frame_producer.py 的 FrameServiceClient）：
  · 模块级一条连接，任何一次失败都作废它、下次调用重新连；
  · 一条连接同时只跑一条请求（_io_lock）：服务端按**到达顺序**应答，而 `drained` 是广播，
    可能夹在任何两条应答之间 —— 读应答时按 id 认自己那条，广播顺手记下（见 _Client._read_reply）。
  · 长活调用方（观察器）请用模块级函数，别存句柄：句柄在第一次失败之后就不再重连。
"""

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

# 确保仓库根在 sys.path（本文件 TTS_Manager/ 上一级 = 仓库根），以便 import configs
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import TTS_SPEAK_CALL_TIMEOUT, TTS_TOOL_TIMEOUT
from configs.paths import TTS_SCRIPT, TTS_SERVICE_SOCKET

# 连 unix socket 本身是瞬时的；这个窗口只用于「服务正在 bind」的极短暂态。
_CONNECT_TIMEOUT = 0.5
# 一问一答型命令（ping / speech_status）的等待上限：它只读内存里的几个字段。
_QUICK_TIMEOUT = 2.0
# 交一句的等待上限：服务侧自己对 play_tts 的应答等了 TTS_SPEAK_CALL_TIMEOUT 才判超时，
# 客户端多留 2s 好收到它那条 failed 而不是自己先断。
_SPEAK_REPLY_TIMEOUT = TTS_SPEAK_CALL_TIMEOUT + 2.0

# 「服务不可达」这类日志最少隔多久打一次（长活进程里每轮调一次，不能每轮刷屏）。
_LOG_THROTTLE_SEC = 10.0


def _log(msg: str) -> None:
    print(f'[TTS] {msg}', file=sys.stderr, flush=True)


class ServiceError(RuntimeError):
    """与常驻服务打交道失败（断开/协议错/超时/服务拒绝）。它同时是 RuntimeError，调用方可当错误处理。"""


class _Client:
    """到常驻服务的一条长连接（模块内部用；消费者只碰模块级函数）。"""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b''
        self._io_lock = threading.Lock()
        self._next_id = 0
        self._drained_seq = 0                  # 见过的 drained 广播条数（判「新的那条」用）
        self._drained_at: Optional[float] = None

    @classmethod
    def connect(cls) -> '_Client':
        """连服务；连不上抛 OSError（由模块级 _get_client 转成「回退」）。"""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT)
        try:
            sock.connect(str(TTS_SERVICE_SOCKET))
        except OSError:
            sock.close()
            raise
        return cls(sock)

    def close(self) -> None:
        """作废这条连接（socket 关掉；下次模块级调用重新拨号）。"""
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ 对外三个动作
    def speak(self, text: str, *, multi_wheel: bool = False, play_now: bool = False,
              wait_drain: bool = False, wait_timeout: Optional[float] = None) -> None:
        """交一句给服务；`wait_drain=True` 时再等这条连接上**新出现**的那条 drained。

        @param wait_timeout - 等 drained 的上限（秒）；缺省 TTS_TOOL_TIMEOUT。

        等见底失败（超时/断开）**不抛** —— 与 wait_tts_done.py「等不到就按上限返回」同一条
        口径：播报本身已经成功，等不到只影响「何时开始收音」，不该让调用方以为播报失败了。
        失败原因当场打一行日志（这里最清楚是谁等谁）。
        """
        with self._io_lock:
            request = {'op': 'speak', 'text': text, 'multi_wheel': bool(multi_wheel),
                       'play_now': bool(play_now)}
            req_id = self._send(request)
            reply = self._read_reply(req_id, time.monotonic() + _SPEAK_REPLY_TIMEOUT)
            event = reply.get('event')
            if event == 'failed':
                raise ServiceError(
                    f'常驻服务拒绝了这一句（error_code={reply.get("error_code")}）：'
                    f'{reply.get("detail")}')
            if event != 'accepted':
                raise ServiceError(f'交一句收到认不出的应答: {reply!r}')
            if not wait_drain:
                return
            # 等的是「这条 accepted 之后新出现的那条 drained」：drained 的语义是「到此刻为止
            # 交出去的都播完了」，而我们的句子在这条 accepted 之前就已交出（服务先 handoff
            # 再回应答），所以那之后广播的 drained 必定涵盖它；反之在这之前见到的那条可能是
            # 别人的句子收尾，拿它当答案会提前返回（后面几句还在放）。
            limit = TTS_TOOL_TIMEOUT if wait_timeout is None else wait_timeout
            base = self._drained_seq
            if not self._read_until_drained(base, time.monotonic() + limit):
                _log(f'⚠ 等服务报「播完」（drained）超过 {limit:g}s，按上限返回')

    def speech_status(self) -> Dict[str, Any]:
        """状态话题的事实快照（观察者的 tools.audio 吃它）。"""
        return self._request('speech_status')

    def ping(self) -> Dict[str, Any]:
        """服务摘要（pid / 队列 / 播放器就绪 / 状态话题事实）。"""
        return self._request('ping')

    # ------------------------------------------------------------------ 传输
    def _request(self, op: str, timeout: float = _QUICK_TIMEOUT) -> Dict[str, Any]:
        """一问一答（`op` 的答复在 `status` 字段里）。"""
        with self._io_lock:
            req_id = self._send({'op': op})
            reply = self._read_reply(req_id, time.monotonic() + timeout)
            event = reply.get('event')
            if event == 'result':
                return dict(reply.get('status') or {})
            raise ServiceError(f'{op} 被服务拒绝: {reply.get("detail") or reply.get("event")}')

    def _send(self, request: Dict[str, Any]) -> int:
        """写一条请求，返回它的 id（调用方保证持 _io_lock）。"""
        sock = self._sock
        if sock is None:
            raise ServiceError('与常驻服务的连接已关闭')
        self._next_id += 1
        request = dict(request, id=self._next_id)
        try:
            sock.sendall((json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8'))
        except OSError as exc:
            self.close()
            raise ServiceError(f'写请求失败（{exc!r}）') from exc
        return self._next_id

    def _read_reply(self, req_id: int, deadline: float) -> Dict[str, Any]:
        """读应答直到出现 `req_id` 的那条；途中的 drained 广播顺手记账。"""
        while True:
            message = self._read_message(deadline)
            if message.get('event') == 'drained':
                self._drained_seq += 1
                self._drained_at = message.get('at')
                continue
            if message.get('id') != req_id:
                self.close()
                raise ServiceError(f'应答对不上（等 id={req_id}，收到 {message!r}）')
            return message

    def _read_until_drained(self, base_seq: int, deadline: float) -> bool:
        """读到新的一条 drained 为止；超时/断开返回 False。"""
        while self._drained_seq <= base_seq:
            try:
                message = self._read_message(deadline)
            except ServiceError as exc:
                _log(f'⚠ 等 drained 时连接出问题（{exc}）')
                return False
            if message.get('event') == 'drained':
                self._drained_seq += 1
                self._drained_at = message.get('at')
                continue
            _log(f'⚠ 等 drained 时收到无关消息: {message!r}')
        return True

    def _read_message(self, deadline: float) -> Dict[str, Any]:
        """读一行 NDJSON（带缓冲：半行留在 _buf 里，超时重来时接着读）。"""
        while b'\n' not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServiceError('等应答超时')
            sock = self._sock
            if sock is None:
                raise ServiceError('与常驻服务的连接已关闭')
            try:
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                raise ServiceError('等应答超时') from exc
            except OSError as exc:
                self.close()
                raise ServiceError(f'读应答失败（{exc!r}）') from exc
            if not chunk:
                self.close()
                raise ServiceError('服务端已关闭连接')
            self._buf += chunk
        line, self._buf = self._buf.split(b'\n', 1)
        try:
            message = json.loads(line.decode('utf-8', 'replace'))
        except ValueError as exc:
            self.close()
            raise ServiceError(f'应答不是合法 JSON（{exc}）') from exc
        if not isinstance(message, dict):
            self.close()
            raise ServiceError(f'应答不是 JSON 对象: {message!r}')
        return message


# =============================================================================
# 模块级门面：进程内一条连接 + 回退到脚本
# =============================================================================

_lock = threading.Lock()
_client: Optional[_Client] = None
_log_lock = threading.Lock()      # 与 _lock 分开：_get_client 持 _lock 时也会调 _note_unreachable
_last_fail_log = 0.0


def _note_unreachable(exc: BaseException) -> None:
    """记一次「服务不可达」（按 _LOG_THROTTLE_SEC 节流）。"""
    global _last_fail_log
    now = time.monotonic()
    with _log_lock:
        if now - _last_fail_log < _LOG_THROTTLE_SEC:
            return
        _last_fail_log = now
    _log(f'⚠ 常驻 TTS 服务不可达（{exc}）：要走播报的调用退回脚本，'
         f'只问事实的调用（speech_status）如实报「取不到」')


def _get_client() -> Optional[_Client]:
    """取本进程到服务的连接；连不上返回 None（已按节流记日志）。"""
    global _client
    with _lock:
        if _client is not None:
            return _client
        try:
            _client = _Client.connect()
        except OSError as exc:
            _note_unreachable(exc)
            return None
        _log(f'已连上常驻 TTS 服务（{TTS_SERVICE_SOCKET}）')
        return _client


def _drop_client(client: _Client) -> None:
    """作废这条连接（失败之后；下次调用重新拨号）。只作废自己那条。"""
    global _client
    client.close()
    with _lock:
        if _client is client:
            _client = None


def _flag(value: bool) -> str:
    """布尔 → 脚本认识的字面量（脚本用字符串比较判断 true/false）。"""
    return 'true' if value else 'false'


def _speak_via_script(text: str, *, multi_wheel: bool, play_now: bool, wait_done: bool,
                      timeout: Optional[float] = None) -> None:
    """回退路径：原样调 run_tts_speak.sh（位置参数顺序：text / multi_wheel / play_now / wait_done）。

    失败时抛 RuntimeError，措辞与工具层原来那条一致（工具结果里 LLM 看到的就是它）。
    @param timeout - 子进程上限（秒）；缺省 TTS_TOOL_TIMEOUT（脚本内部等待上限小于它）。
    """
    try:
        result = subprocess.run(
            [str(TTS_SCRIPT), text, _flag(multi_wheel), _flag(play_now), _flag(wait_done)],
            capture_output=True, text=True,
            timeout=TTS_TOOL_TIMEOUT if timeout is None else timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'tts 执行超时（{timeout or TTS_TOOL_TIMEOUT:g}s 未返回）') from None
    if result.returncode != 0:
        raise RuntimeError(
            f'tts 执行失败 (code={result.returncode}): {result.stderr or result.stdout}'
        )


def service_available() -> bool:
    """常驻服务此刻是否可达（连接建立即算可达，不判 play_tts）。

    读者：start_agent 起组件前先问一次（已有服务在跑就不再抢锁、也不再拉子进程）。
    """
    return _get_client() is not None


def service_status() -> Optional[Dict[str, Any]]:
    """服务摘要（`tts_service.py --status` 读它）；不可达返回 None。"""
    client = _get_client()
    if client is None:
        return None
    try:
        return client.ping()
    except ServiceError as exc:
        _drop_client(client)
        _log(f'⚠ 常驻 TTS 服务查询失败（{exc}）')
        return None


def speech_status() -> Optional[Dict[str, Any]]:
    """状态话题的事实快照（`topic_present` / `code` / `code_at`）；不可达返回 None。

    读者：Observer/status_observer.probe_audio。**没有**退回订阅的兜底 —— 话题的唯一长期
    订阅者是服务本身，回退会在回退进程里再弄出一个订阅者（见该模块头）。
    """
    client = _get_client()
    if client is None:
        return None
    try:
        return client.speech_status()
    except ServiceError as exc:
        _drop_client(client)
        _log(f'⚠ 读语音状态失败（{exc}）')
        return None


def speak(text: str, *, multi_wheel: bool = False, play_now: bool = False) -> None:
    """播报一段话（**不等播完**）。服务可达 → 交给它；连不上 → 跑脚本。

    @param text - 要播的文本（原样送，不做净化）。
    @param multi_wheel - 多轮播报开关（默认关，与脚本默认一致）。
    @param play_now - True = 打断在播的那条立刻播；False = 不抢麦（默认，但**挡不住**下一次
        调用停掉它：设备没有队列，接着播要自己等前一句播完，见 speak_and_wait）。
    @raises RuntimeError - 服务拒绝这一句、命令发出去之后失败、或脚本非 0 退出。
    """
    client = _get_client()
    if client is None:
        _speak_via_script(text, multi_wheel=multi_wheel, play_now=play_now, wait_done=False)
        return
    try:
        client.speak(text, multi_wheel=multi_wheel, play_now=play_now)
    except ServiceError:
        _drop_client(client)
        raise


def speak_and_wait(text: str, *, multi_wheel: bool = False, play_now: bool = False,
                   timeout: Optional[float] = None) -> None:
    """播报一段话并等它**播完**再返回（要抢麦的调用方用：不等就会被自己后面的话/唤醒掐断）。

    服务可达 → 交一句再等服务广播 drained（没接到就没有 section_id 可用，见 TTS_Manager/
    speech_status.py）；连不上 → 脚本 wait_done=true（它自己订阅状态话题等 status=0 Idle）。
    两条路都**不因等不到而报错**（播报本身是成功的），等不到只打日志。

    @param timeout - 等播完的上限（秒）；缺省 TTS_TOOL_TIMEOUT。注意它是**上限**而不是
        「超了就算失败」—— 等不到只会打一行日志后返回。
    """
    client = _get_client()
    if client is None:
        _speak_via_script(text, multi_wheel=multi_wheel, play_now=play_now, wait_done=True,
                          timeout=timeout)
        return
    try:
        client.speak(text, multi_wheel=multi_wheel, play_now=play_now, wait_drain=True,
                     wait_timeout=timeout)
    except ServiceError:
        _drop_client(client)
        raise
