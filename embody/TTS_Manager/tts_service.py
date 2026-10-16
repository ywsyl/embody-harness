#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
常驻 TTS 服务（`--serve`）：全系统**唯一**的语音播报入口，也是语音状态话题**唯一的长期订阅者**。

为什么要常驻
------------
一次 `speak` 的三句话原先由交互器插件**逐句各起一个进程**送出去（`run_tts_speak.sh`：bash
前导 0.80s + `ros2 service call` 的 CLI 启动与 DDS 发现 1.3s），而下一句只有在上一句「播完」
之后才肯交 —— 两段固定开销整段变成句间静默（2026-10-12 实测 2.42s / 3.0s，而设备从收到请求
到出声只要 0.463s，见 /root/log/speechcore.20261012.log）。把客户端常驻下来，那两段就都不存在：
一句就是一条 socket 命令。

**但它不替我们排队**：设备也没有队列 —— 每来一条新 `play_tts` 请求，`speech_core` 都先
`speech app stop` 停掉在播的那条再 `runInquiry`（2026-10-12 15:37~15:39 实测三次，前一句分别
在 75ms / 96ms / 185ms 处被掐断）。所以本服务**照收到的顺序、一条一条发**，而「等这一句播完
再发下一条」由调用方按 `drained` 把握（交互器插件就是 `serial` 模式）。

形态（照 skills/camera/frame_producer.py 的取帧服务）
----------------------------------------------------
  · start_agent 作为**并列组件子进程**拉起（`tts_service.py --serve`）；ROS 三件套由 starter.sh
    备好，子进程继承。
  · 单实例：flock 独占 TTS_SERVICE_LOCK_FILE。抢到独占锁即证明全系统没有别的服务在跑，于是
    残留的 socket 文件（上次被 kill -9 留下）可以安全 unlink 后重 bind —— 陈 socket 因此不需要
    任何探测。
  · 对外只能是 socket，不能是进程内对象：消费者都在别的进程里（交互器 daemon 的 dsh 插件、
    工具桥的 Python 进程、audio_interact 的一次性脚本），这正是它必须常驻的理由。
  · 状态话题也由它一份订阅（原先是观察者自己订，见 Observer/status_observer.probe_audio）：
    一个话题一个长期订阅者，最后一条原始码经 `speech_status` 命令对外供给。

行协议（NDJSON；载荷全是短文本，不需要取帧服务那套二进制分帧）
-----------------------------------------------------------
  请求 →  {"id":N,"op":"speak","text":"…","multi_wheel":false,"play_now":false}
          {"id":N,"op":"speech_status"}     — 状态话题的事实快照（观察者读它）
          {"id":N,"op":"ping"}              — 服务摘要（`--status` 读它）
  应答 →  {"id":N,"event":"accepted","section_id":"…","error_code":0}
          {"id":N,"event":"failed","error_code":…,"detail":"…"}
          {"id":N,"event":"result","status":{…}}     — speech_status / ping 的答复
          {"id":N,"event":"error","detail":"…"}      — 协议错（认不出的行 / 未知的 op）
  广播 →  {"event":"drained","at":<epoch秒>}          — 队列见底（判据见下）

  同一条连接的请求按到达顺序应答（命令队列是 FIFO，同一连接的命令由它自己的连接线程顺序
  塞入）。`drained` 是广播，可能夹在任何两条应答之间，客户端读应答时必须容忍它并按 `id`
  认自己那条（见 TTS_Manager/tts_client.py 与 dsh-plugins/speech-stream/src/service.ts）。
  队列见底之前**不重复**广播：`drained_at` 是「这一刻起不会再有我们送出去的声音」的时刻，
  语义是状态而不是事件流。

线程模型
--------
  主线程：**只做 rclpy**（`spin_once` + 所有 service 调用与应答处理）**与 socket 写**。
  连接线程：只读请求行、把命令塞进队列、照常读下一行 —— 一律不碰 rclpy、不写 socket。
  于是「一条连接的字节流只有一个写者」与「rclpy 只在一个线程里」两条都由结构保证，不需要
  任何锁。代价是主循环每轮最多 50ms（spin_once 的超时），对句子量级毫无影响。

「播完」（`drained`）的判据
--------------------------
  判据本体在 TTS_Manager/speech_status.py 的 QueueDrain（与 wait_tts_done 的一次性等待同源，
  「播完了没有」只判这一处）。服务额外给两条：
    · play_tts 报错的那一句不算交出（它不会出声，撤掉交接，见 cancel_handoff）；
    · 看门狗：交出一句之后 TTS_WAIT_DONE_MAX_WAIT 秒还没见 Idle，就按上限放行并在日志里
      说明。没有它，一次丢应答会让「队列见底」永久不再上报，而 speak_listen 的下一轮收音
      正等着它 —— 那会变成静默的永久卡死，比晚一点上报坏得多。

回退路径
--------
  服务不在时消费者各自退回旧路（Python 侧 `run_tts_speak.sh` / `wait_tts_done.sh`，插件侧
  逐句 spawn），选路在 TTS_Manager/tts_client.py 与 dsh-plugins/speech-stream/src/tts.ts。
  本文件不提供回退：`--serve` 起不来就是起不来，缺消息定义时**报缺什么并退出**，不静默降级。

用法:
    python3 TTS_Manager/tts_service.py --serve      # 前台长驻（由 start_agent 拉为组件子进程）
    python3 TTS_Manager/tts_service.py --status     # 打印服务当前状态后退出
"""

import argparse
import fcntl
import json
import os
import queue as queue_mod
import signal
import socketserver
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保仓库根在 sys.path（本文件 TTS_Manager/ 上一级 = 仓库根），以便 import configs 与 TTS_Manager
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import (
    AUDIO_STATUS_NAMES,
    TTS_SPEAK_CALL_TIMEOUT,
    TTS_WAIT_DONE_MAX_WAIT,
)
from configs.paths import ROS_TTS_SERVICE, TTS_SERVICE_LOCK_FILE, TTS_SERVICE_SOCKET
from TTS_Manager.speech_status import STATUS_TOPIC, QueueDrain

# ROS 依赖**不在模块顶层 fail loud**：`--status` 要能在没 source ROS 的终端里跑（它只是查
# socket 与锁文件，不需要 ROS）。缺什么由 _serve 报出来并退出，见那里的提示。
try:
    import rclpy
    from rclpy.node import Node
    from homi_speech_interface.msg import AssistantEvent
    from homi_speech_interface.srv import AssistantSpeechText
    _ROS_IMPORT_ERROR: Optional[ImportError] = None
except ImportError as _exc:
    rclpy = None  # type: ignore[assignment]
    Node = None  # type: ignore[assignment]
    AssistantEvent = None  # type: ignore[assignment]
    AssistantSpeechText = None  # type: ignore[assignment]
    _ROS_IMPORT_ERROR = _exc

_PREFIX = '[TTS服务]'

# 一条请求行的上限：超出即视为协议错（客户端不会把整本书塞进一句播报）。
# 超过它 readline 会返回半行（没有结尾换行），按协议错断掉这条连接。
_MAX_LINE_BYTES = 64 * 1024

# 主循环每轮的 rclpy 等待上限（秒）—— 话题事件与 play_tts 应答都在其中被处理。
_POLL_INTERVAL_SEC = 0.05

# play_tts 未就绪时的一次等待上限（秒）。启动早期 audio_center 可能还没起来；
# 等不到就按失败回给调用方（见 _issue_outbox）。
_SERVICE_WAIT_SEC = 1.0


def _log(msg: str) -> None:
    print(f'{_PREFIX} {msg}', file=sys.stderr, flush=True)


class _Conn:
    """一条客户端连接的写端。**只由主线程写**（见模块头的线程模型）。

    对端已走不是错误：send 失败即把连接标记为死，后续写入直接丢弃 —— 服务不该因为
    某个客户端进程退出而受影响。
    """

    def __init__(self, sock) -> None:
        self._sock = sock
        self.alive = True

    def send(self, message: Dict[str, Any]) -> bool:
        """写一条 NDJSON；连接已死/写失败返回 False（不抛）。"""
        if not self.alive:
            return False
        data = (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8')
        try:
            self._sock.sendall(data)
        except OSError as exc:
            self.alive = False
            _log(f'客户端连接断开（{exc!r}），后续事件不再发给它')
            return False
        return True

    def close(self) -> None:
        """标记连接已死（socket 本身由 socketserver 在连接线程里关）。"""
        self.alive = False


class _Command:
    """一条待处理的请求（连接线程塞进队列，主线程取出来做）。"""

    __slots__ = ('conn', 'req_id', 'op', 'text', 'multi_wheel', 'play_now', 'detail')

    def __init__(self, conn: _Conn, req_id: Any, op: str, request: Optional[Dict[str, Any]] = None,
                 detail: str = '') -> None:
        self.conn = conn
        self.req_id = req_id
        self.op = op
        self.detail = detail
        request = request or {}
        self.text = str(request.get('text') or '')
        self.multi_wheel = bool(request.get('multi_wheel', False))
        self.play_now = bool(request.get('play_now', False))


class _InFlight:
    """一次已发出、等应答的 play_tts 调用。"""

    __slots__ = ('cmd', 'future', 'deadline')

    def __init__(self, cmd: _Command, future, deadline: float) -> None:
        self.cmd = cmd
        self.future = future
        self.deadline = deadline


class TtsService:
    """服务的内核：rclpy 侧 + 命令队列 + drained 判定（全部方法只在主线程调用）。

    连接线程只经 `submit()` / `register()` / `unregister()` 三个口子进来。
    """

    def __init__(self) -> None:
        self._node = None
        self._client = None
        self._started_at = time.time()
        self._commands: queue_mod.SimpleQueue = queue_mod.SimpleQueue()
        self._outbox: deque = deque()      # 已受理、还没发出 play_tts 的句子
        self._inflight: List[_InFlight] = []
        self._drain = QueueDrain()
        self._last_handoff = 0.0           # 最后一次交出的单调时刻（看门狗用）
        self._drained_at: Optional[float] = None
        self._code: Optional[int] = None   # 状态话题最后一条原始码（观察者要的事实）
        self._code_at: Optional[float] = None
        self._warned_codes: set = set()    # 码表外的码只报一次警
        self._conns: List[_Conn] = []
        self._conns_lock = threading.Lock()
        self._running = True

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """建节点、建 play_tts 客户端、订状态话题（import 由调用方保证已成功）。"""
        self._node = Node('tts_service')
        self._client = self._node.create_client(AssistantSpeechText, ROS_TTS_SERVICE)
        self._node.create_subscription(AssistantEvent, STATUS_TOPIC, self._on_event, 10)
        _log(f'已就绪：play_tts={ROS_TTS_SERVICE}，状态话题={STATUS_TOPIC}（本服务是它唯一的长期订阅者）')

    def run(self) -> None:
        """主循环：rclpy 推进 + 取命令 + 发句子 + 收应答 + 判见底。"""
        while self._running and rclpy.ok():
            rclpy.spin_once(self._node, timeout_sec=_POLL_INTERVAL_SEC)
            self._take_commands()
            self._issue_outbox()
            self._advance_inflight()
            self._check_drain()

    def request_stop(self) -> None:
        """请主循环退出（信号处理器只许做这个：它跑在主线程的 spin_once 里，不能碰 rclpy）。"""
        self._running = False

    def stop(self) -> None:
        """停止主循环并收掉 rclpy（socket 与锁由 _serve 的 finally 收）。"""
        self._running = False
        if self._node is not None:
            try:
                self._node.destroy_node()
            except Exception as exc:  # noqa: BLE001 —— 收尾失败不该挡住后续清理
                _log(f'销毁节点失败（{exc!r}）')
            self._node = None
            self._client = None
        if rclpy.ok():
            rclpy.shutdown()

    # ------------------------------------------------------- 连接线程的三个入口
    def submit(self, cmd: _Command) -> None:
        """连接线程塞一条命令（不碰 rclpy）。"""
        self._commands.put(cmd)

    def register(self, conn: _Conn) -> None:
        """登记一条连接的写端（广播 drained 用）。"""
        with self._conns_lock:
            self._conns.append(conn)

    def unregister(self, conn: _Conn) -> None:
        """注销一条连接（对端已走；顺带标记它不再接收事件）。"""
        conn.close()
        with self._conns_lock:
            if conn in self._conns:
                self._conns.remove(conn)

    # ------------------------------------------------------------------ 主循环各步
    def _on_event(self, m) -> None:
        """状态话题回调（在 spin_once 里执行，即主线程）。"""
        status = int(m.status)
        self._code = status
        self._code_at = time.time()
        # 码表外的码只报一次：它意味着接口漂移（/root/cmcc_robot 那份旧版消息定义就是如此），
        # 一条警告足够定位，不必每来一条刷一行。观察者的 tools.audio 会把这种码判成 unknown。
        if status not in AUDIO_STATUS_NAMES and status not in self._warned_codes:
            self._warned_codes.add(status)
            _log(f'⚠ 状态话题出现码表外的 status={status}（接口可能漂移）：{m.msg!r}')
        self._drain.on_event(status)

    def _take_commands(self) -> None:
        """取走此刻排队的全部命令（非阻塞，不拖主循环）。"""
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue_mod.Empty:
                return
            self._handle_command(cmd)

    def _handle_command(self, cmd: _Command) -> None:
        """执行一条命令：立即答的当场答，要出声的排进 outbox。"""
        if cmd.op == 'speak':
            if not cmd.text.strip():
                _log('speak 命令的文本为空，已拒')
                self._reject(cmd, None, '文本为空')
                return
            self._outbox.append(cmd)
            return
        if cmd.op == 'speech_status':
            cmd.conn.send({'id': cmd.req_id, 'event': 'result', 'status': self._speech_facts()})
            return
        if cmd.op == 'ping':
            cmd.conn.send({'id': cmd.req_id, 'event': 'result', 'status': self._summary()})
            return
        # 协议错（未知 op / 认不出的行）也由主线程答，保住「一条连接只有一个写者」
        cmd.conn.send({'id': cmd.req_id, 'event': 'error', 'detail': cmd.detail or f'未知的 op: {cmd.op!r}'})

    def _issue_outbox(self) -> None:
        """把排队的句子发出去（一句一条 play_tts 调用，不等应答）。

        **不等应答就发下一句**是有意的：应答只说明设备受理了，与「这句播完了没有」无关
        （那是 drained 判定的事）。设备没有队列 —— 所以本服务不替调用方串行，谁想连播谁自己
        按 drained 把握节奏：这里连发两句，设备会把前一句掐断。
        """
        if not self._outbox:
            return
        if not self._client.service_is_ready():
            if not self._client.wait_for_service(timeout_sec=_SERVICE_WAIT_SEC):
                detail = f'play_tts 服务未就绪（{ROS_TTS_SERVICE}）'
                _log(f'⚠ {detail}：{len(self._outbox)} 句按失败回给调用方')
                while self._outbox:
                    self._reject(self._outbox.popleft(), None, detail)
                return
        while self._outbox:
            cmd = self._outbox.popleft()
            request = AssistantSpeechText.Request()
            request.msg = cmd.text
            request.multi_wheel = cmd.multi_wheel
            request.play_now = cmd.play_now
            try:
                future = self._client.call_async(request)
            except Exception as exc:  # noqa: BLE001 —— 调用点异常按这一句失败处理，不拖垮服务
                self._reject(cmd, None, f'play_tts 调用异常: {exc!r}')
                continue
            self._inflight.append(
                _InFlight(cmd, future, time.monotonic() + TTS_SPEAK_CALL_TIMEOUT))
            # 交接在**发出**这一刻记：请求已经上路，设备随后就会出声（它收到就会先停掉在播的）
            self._drain.handoff()
            self._last_handoff = time.monotonic()

    def _advance_inflight(self) -> None:
        """收 play_tts 应答：成功答 accepted，失败撤交接并答 failed，超时同样按失败。"""
        if not self._inflight:
            return
        now = time.monotonic()
        for item in list(self._inflight):
            future = item.future
            if future.done():
                self._inflight.remove(item)
                if future.cancelled():
                    self._drain.cancel_handoff()
                    self._reject(item.cmd, None, 'play_tts 调用被取消')
                    continue
                try:
                    response = future.result()
                except Exception as exc:  # noqa: BLE001 —— 传输/服务异常都只是这一句失败
                    self._drain.cancel_handoff()
                    self._reject(item.cmd, None, f'play_tts 调用失败: {exc!r}')
                    continue
                code = int(response.error_code)
                if code != 0:
                    self._drain.cancel_handoff()
                    self._reject(item.cmd, code, f'play_tts 返回错误码 {code}')
                    continue
                item.cmd.conn.send({
                    'id': item.cmd.req_id, 'event': 'accepted',
                    'section_id': str(response.section_id), 'error_code': 0,
                })
                _log(f'已交一句（{len(item.cmd.text)} 字，section_id={response.section_id}）：'
                     f'{item.cmd.text}')
            elif now >= item.deadline:
                self._inflight.remove(item)
                self._drain.cancel_handoff()
                self._reject(item.cmd, None,
                             f'play_tts {TTS_SPEAK_CALL_TIMEOUT:g}s 内无应答')

    def _check_drain(self) -> None:
        """判「交出去的每一句都不会再出声了」并按需广播。"""
        idle_now = not self._outbox and not self._inflight
        if self._drain.poll(idle_now):
            self._broadcast_drain()
            _log('队列见底（drained）')
            return
        # 看门狗：见底判据永远要等一条 Idle，而 Idle 只由设备发 —— 丢一次应答或设备不回
        # Idle，判据就再也不成立。按调用方同样的等待上限放行，并在日志里说明这次是放行不是判出。
        if (idle_now and self._drain.pending > 0
                and time.monotonic() - self._last_handoff > TTS_WAIT_DONE_MAX_WAIT):
            self._drain.reset()
            self._broadcast_drain()
            _log(f'⚠ 交出后 {TTS_WAIT_DONE_MAX_WAIT:g}s 未收到 status=0 Idle，'
                 f'按上限放行并广播 drained（设备可能没回 Idle，或丢了应答）')

    def _broadcast_drain(self) -> None:
        """把 drained 发给所有活着的连接（时刻取服务自己的钟 = 同一台机器）。"""
        self._drained_at = time.time()
        message = {'event': 'drained', 'at': self._drained_at}
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            conn.send(message)

    def _reject(self, cmd: _Command, error_code: Optional[int], detail: str) -> None:
        """把一句播报判为失败回给调用方（连接已走就只是日志）。"""
        _log(f'⚠ 播报失败（{detail}）：{cmd.text[:40]!r}')
        cmd.conn.send({
            'id': cmd.req_id, 'event': 'failed', 'error_code': error_code, 'detail': detail,
        })

    # ------------------------------------------------------------------ 事实快照
    def _speech_facts(self) -> Dict[str, Any]:
        """状态话题的事实（观察者的 tools.audio 判据吃它，判据本身留在观察者那头）。

        `topic_present` = ROS 图里这个话题此刻有没有发布者（None = 问不出来）；
        `code` / `code_at` = 最后一条原始码与它的到达时刻（没出过声就是 None）。
        """
        present: Optional[bool] = None
        if self._node is not None:
            try:
                present = bool(self._node.count_publishers(STATUS_TOPIC))
            except Exception as exc:  # noqa: BLE001 —— 问不出来就如实报 None
                _log(f'⚠ 问不到状态话题的发布者（{exc!r}）')
        return {'topic_present': present, 'code': self._code, 'code_at': self._code_at}

    def _summary(self) -> Dict[str, Any]:
        """服务自身状态（`--status` 与 ping 用）。"""
        with self._conns_lock:
            clients = len(self._conns)
        return {
            'service': 'tts',
            'pid': os.getpid(),
            'uptime': round(time.time() - self._started_at, 1),
            'play_tts_ready': bool(self._client is not None and self._client.service_is_ready()),
            'clients': clients,
            'outbox': len(self._outbox),
            'inflight': len(self._inflight),
            'waiting_drain': self._drain.pending,
            'drained_at': self._drained_at,
            'speech': self._speech_facts(),
        }


class _TtsServiceHandler(socketserver.StreamRequestHandler):
    """一条连接的读端：逐行读请求 → 交给主线程 → 接着读下一行。

    **不写 socket、不碰 rclpy**（见模块头的线程模型）。请求按到达顺序塞进 FIFO，于是同一条
    连接上的应答顺序 = 请求顺序。
    """

    def setup(self) -> None:
        super().setup()
        self.conn = _Conn(self.request)
        self.server.service.register(self.conn)

    def finish(self) -> None:
        self.server.service.unregister(self.conn)
        try:
            super().finish()
        except OSError as exc:  # 对端已走；关连接失败没有别的后果
            _log(f'关闭客户端连接失败（{exc!r}）')

    def handle(self) -> None:
        while True:
            try:
                line = self.rfile.readline(_MAX_LINE_BYTES)
            except OSError as exc:
                _log(f'读客户端请求失败（{exc!r}），断开这条连接')
                return
            if not line:
                return                     # 对端关闭
            if not line.endswith(b'\n'):
                self._bad(None, f'请求行超过 {_MAX_LINE_BYTES} 字节（不完整的一行）')
                return                     # 字节流已经错位，断掉比继续猜安全
            text = line.decode('utf-8', 'replace').strip()
            if not text:
                continue                   # 空行：宽容跳过（不改变状态）
            try:
                request = json.loads(text)
            except ValueError as exc:
                self._bad(None, f'请求不是合法 JSON（{exc}）')
                continue
            if not isinstance(request, dict):
                self._bad(None, '请求必须是 JSON 对象')
                continue
            op = str(request.get('op') or '')
            self.server.service.submit(
                _Command(self.conn, request.get('id'), op, request))

    def _bad(self, req_id: Optional[int], detail: str) -> None:
        """协议错的应答也走命令队列 —— 写 socket 的只有主线程那一个。"""
        _log(f'⚠ {detail}')
        self.server.service.submit(_Command(self.conn, req_id, '__bad__', None, detail=detail))


class _TtsServiceServer(socketserver.ThreadingUnixStreamServer):
    """服务的服务端：unix socket + 一条连接一个线程。"""

    daemon_threads = True    # 退出不必等卡在 readline 上的连接线程（server_close 不 join）
    request_queue_size = 16  # 并发客户端：交互器插件 + 工具进程 + 观察器 + 一次性脚本

    def __init__(self, sock_path: str, service: TtsService) -> None:
        self.service = service
        super().__init__(sock_path, _TtsServiceHandler)


def _acquire_lock():
    """抢跨进程单实例锁（非阻塞独占 TTS_SERVICE_LOCK_FILE）。

    成功返回持锁的 fd（进程退出/close 自动释放 → 无陈旧锁问题）；已被别的进程持有返回 None。
    锁文件打不开是环境问题（不该出现：runtime_files 是服务自己的目录）—— 直接报错退出，
    不 fail-open：两个服务同时播报会互相打断，比不启动更坏。
    """
    path = TTS_SERVICE_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        owner = '?'
        try:
            owner = os.read(fd, 64).decode(errors='replace').strip() or '?'
        except OSError:
            pass
        os.close(fd)
        _log(f'❌ 已有一个常驻 TTS 服务在跑（pid={owner}），本进程不启动')
        return None
    try:  # 写入本进程 pid 供后续竞争方/诊断读
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    except OSError as exc:
        _log(f'⚠ 写锁文件 pid 失败（{exc!r}）')
    return fd


def _serve(args: argparse.Namespace) -> int:
    """`--serve`：前台长驻。顺序有意如此：**先抢锁、再 bind**（见模块头）。

    缺消息定义（rclpy 或运行中的安装没 source）在这里 fail loud：静默降级等于让所有消费者
    以为服务在、其实一句也送不出去。
    """
    if _ROS_IMPORT_ERROR is not None:
        print(f'{_PREFIX} ERROR: 无法导入 rclpy / homi_speech_interface'
              f'（{_ROS_IMPORT_ERROR}）', file=sys.stderr)
        print(f'{_PREFIX} 提示: 先 source /opt/ros/foxy/setup.bash 与 '
              f'/usr/bin/cmcc_robot/install/setup.bash', file=sys.stderr)
        return 3

    lock_fd = _acquire_lock()
    if lock_fd is None:
        return 1

    service = TtsService()
    sock_path = Path(TTS_SERVICE_SOCKET)
    if len(os.fsencode(str(sock_path))) >= 108:  # unix sun_path 上限，太长连 bind 都装不下
        print(f'{_PREFIX} socket 路径过长(>=108 字节)，无法监听: {sock_path}', file=sys.stderr)
        os.close(lock_fd)
        return 1
    server: Optional[_TtsServiceServer] = None
    try:
        rclpy.init()
        service.start()
        try:
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                sock_path.unlink()
            except FileNotFoundError:
                pass
            server = _TtsServiceServer(str(sock_path), service)
        except OSError as exc:
            print(f'{_PREFIX} 服务监听 {sock_path} 失败: {exc!r}', file=sys.stderr)
            service.stop()
            return 1
        try:
            os.chmod(sock_path, 0o600)  # stdlib 不设权限，默认受 umask 影响
        except OSError as exc:
            _log(f'⚠ socket 权限设置失败（{exc!r}）')

        def _on_signal(signum: int, _frame: object) -> None:
            # 信号处理器跑在主线程**正停在 spin_once 里**的那一刻，所以它只做两件不碰 rclpy 的
            # 事：请主循环退出（下一轮就跳出），以及另起一个线程去关 socket 服务
            # （socketserver 文档给的标准姿势 —— 在信号处理器里直接 shutdown() 会死锁）。
            # 默认 SIGTERM 不跑 atexit，所以收尾必须显式走 finally：unlink socket + 收 rclpy + 放锁。
            _log(f'收到信号 {signum}，停止中')
            service.request_stop()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        print(f'{_PREFIX} 常驻 TTS 服务已监听 {sock_path}（pid={os.getpid()}）', flush=True)
        # 主循环要同时跑 rclpy 与 socket 服务：serve_forever 会占住主线程，所以把它交给一个
        # 线程，主线程留给 rclpy（见模块头的线程模型）。
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2},
                         name='tts-service-socket', daemon=True).start()
        service.run()
    finally:
        if server is not None:
            server.server_close()
        try:
            sock_path.unlink()
        except OSError:
            pass
        service.stop()
        os.close(lock_fd)
    _log('常驻 TTS 服务已停止')
    return 0


def _lock_holder() -> Optional[str]:
    """单实例锁**此刻**的持有者 pid；无人持有时返回 None。

    不能只读锁文件：里面的 pid 是持有者自己写进去的，进程消失后 flock 随进程释放、文件却留着
    —— 只读内容会把一个死 pid 报成「持有者」（2026-10-12 实测：报出的 16349 早就不在了）。
    判据以 flock 本身为准：能抢到就说明没人持有（随即 close 释放，不改变现状）。
    """
    try:
        fd = os.open(str(TTS_SERVICE_LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                return os.read(fd, 64).decode(errors='replace').strip() or '?'
            except OSError:
                return '?'
        return None
    finally:
        os.close(fd)


def _print_status() -> int:
    """`--status`：先问服务（socket），再报本机的锁文件与路径。返回 0 = 服务在。"""
    from TTS_Manager import tts_client

    print(f'socket     : {TTS_SERVICE_SOCKET}')
    print(f'锁文件     : {TTS_SERVICE_LOCK_FILE}')
    holder = _lock_holder()
    print(f'锁持有者   : {holder or "（无人持有；文件里的 pid 是上一任留下的残留）"}')
    summary = tts_client.service_status()
    if summary is None:
        print('服务       : 不可达（socket 连不上）—— 消费者会各自退回旧路（脚本/逐句 spawn）')
        return 1
    print(f'服务       : 在（pid={summary.get("pid")}，已跑 {summary.get("uptime")}s，'
          f'客户端 {summary.get("clients")} 条）')
    facts = summary.get('speech') or {}
    present = facts.get('topic_present')
    print(f'状态话题   : 发布者 {"有" if present else "无" if present is False else "问不到"}'
          f'，最后一条码 {facts.get("code")}（{facts.get("code_at")}）')
    print(f'play_tts   : {"就绪" if summary.get("play_tts_ready") else "未就绪"}'
          f'，待发 {summary.get("outbox")} 句 / 在飞 {summary.get("inflight")} 句'
          f' / 等见底 {summary.get("waiting_drain")} 句')
    print(f'上次见底   : {summary.get("drained_at")}')
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """命令行：两个互斥的用途（长驻服务 / 查状态）。"""
    parser = argparse.ArgumentParser(
        description='常驻 TTS 服务（全系统唯一播报入口 + 状态话题唯一长期订阅者）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--serve', action='store_true',
                        help='前台长驻：抢单实例锁、bind socket、订状态话题（start_agent 拉它）')
    parser.add_argument('--status', action='store_true',
                        help='打印服务当前状态后退出（0 = 服务在）')
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """入口：`--serve` / `--status` 二选一（都不给时打印帮助）。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.serve:
        return _serve(args)
    if args.status:
        return _print_status()
    parser.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
