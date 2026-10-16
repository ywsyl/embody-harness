#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agent 统一启动入口（start_agent）

先按开关起好并列组件（receiver / 取帧服务 / 常驻 TTS 服务 / 视觉观测器 / 状态观测器 /
运动状态观测器），再进 agent 主循环；退出时把组件一起收掉。

模式
----
cloud  云端通信模式：进 Communicator/receiver.py（ROS SIGCEvent → rolebus 云端事件的薄桥），
       长驻到被中断。组件的 receiver 只管收发；云端事件交给角色系统处理（见 starter.sh）。

本文件只有这一个模式：旧代的 local 单次模式（入口 Agents/task/task_loop.py）随整棵
Agents/ 一起删了，那条路连同它的入口常量一并从这里移走。

本脚本**不认识的参数一律原样透传给入口**，所以入口自己的参数（receiver 的等）不用在这里
再声明一份，避免与入口的参数默认值漂移。

六个并列组件：谁起、以什么形态起
--------------------------------
组件各自独立，由本脚本按参数决定起哪些（默认全起，--no-xxx 关掉）：

  组件              形态          取哪些开关           职责
  receiver          本进程主循环   cloud 模式 / --no-receiver   云端通信（认领任务、上报）
  frame_producer    独立子进程     --no-frame-producer          **取帧服务**：相机固定高频
                     (--serve)                                  抓拍 → 内存帧环 → 供全体
                                                                消费者经 unix socket 取帧
  tts_service       独立子进程     --no-tts-service             **常驻 TTS 服务**：唯一播报
                     (--serve)                                  入口 + 语音状态话题唯一长期
                                                                订阅者，经 unix socket 收播报
                                                                与状态查询
  vision_observer   本进程内线程   --no-vision-observer         按可调周期看帧环、最新 N 帧
                                                                 拼接 → VQA → vision_obs.yaml
  robot_status      独立子进程     --no-status-observer         订阅 ROS → robot_status.yaml
  motion_status     本进程内线程   --no-motion-status           订阅 ROS + 读 manager 的
                                                                 内存记录 → motion_status.yaml
                                                                 （没起它时，MotionManager
                                                                 会懒起一份**只读**订阅，见下）

receiver 只管云端收发，不再涉及帧与观测（原先它在 main() 里 start 生产者 + 起观测
线程，已删除）。

⚠️ 帧环在取帧服务子进程里 —— 谁取帧、怎么取
帧环是「帧生产者所在进程」的内存（skills/camera/frame_producer.py 模块头），所以取帧权
归**一个进程**：本脚本拉起的 frame_producer 子进程（`--serve`）。它抢
FRAME_PRODUCER_LOCK_FILE、开抓拍、bind FRAME_PRODUCER_SOCKET；**本进程与其它所有消费者
都是它的客户端**：
  - frame_producer 子进程：帧环所有者（唯一）；
  - vision_observer 线程：客户端（frame_producer.py 的模块级取帧函数自动经 socket 走 RPC）；
  - 角色侧工具进程（plugin_tools/tool_executor.py 的 _look / search_old、bull_chase 等）：
    同样是客户端，**不再报「已被另一进程持有」**——这正是本组件从「进程内线程」改成
    「子进程」的原因（详见 frame_producer.py 模块头的三条分派规则）。
  - 独立 CLI（`python3 start_agent.py cloud --no-receiver` 之外的手工自检）没有服务时仍可
    自拉兜底：抢到锁就自己抓拍，抢不到才报错。
状态观测器没有这个约束（只订阅 ROS 话题、与帧环无关），按设计就是独立 CLI，故拉成子进程。

⚠️ 常驻 TTS 服务同形（独立子进程 + unix socket），理由不同
播报原先由消费者**各自起进程**送出去（每句一次 bash 前导 + ros2 CLI 启动 + DDS 发现，
近 2 秒整段变成句间静默，见 TTS_Manager/tts_service.py 模块头）。常驻它之后一句就是一条
socket 命令，而消费者**分布在好几个进程里**（交互器 daemon 的 dsh 插件、工具桥的 Python
进程、audio_interact 的一次性脚本），所以只能做成「一个常驻进程 + socket」而不是进程内
线程。**语音状态话题的长期订阅者也收在它这一处**：观察者那一格改为经 socket 向它要事实
（见 Observer/status_observer.probe_audio）——一个话题一个长期订阅者。

⚠️ 为什么 motion_status 必须与主循环**同进程**
「最近一笔运动是谁下发的」这份记录（Motion_Manager/motion_record.py）2026-09-20 起只在
MotionManager 的**进程内存**里，不再落盘：拉成子进程读到的永远是「没有这一笔」，而且不会报错。
所以它只能在本进程内起线程（与 vision_observer 同样的形态，理由不同；帧生产者不在本进程
内，它的理由见上一条）。
**本树里 MotionManager 几乎没有调用方**：运动/动作都由角色侧工具经脚本下发
（`motion.sh` / `robot_action.sh`），不经过 manager；唯一进 manager 的是本进程启动前的停运动
（startup_cleanup → `task_exit.stop_all_motion`）。所以那张「最近一笔运动是谁下发的」记录
在本树基本停在初值 —— `motion_status.yaml` 的动作/运动状态字段靠它自己的 ROS 订阅填，不靠
这份记录。manager 若将来重新由工具层驱动，仍必须与本组件同进程。
MotionManager 要判动作状态时，若本进程没有常驻订阅，会经 motion_status.ensure_reader()
懒起一份**只读**订阅（订阅 ROS、攒帧，**不写 motion_status.yaml、不发 _latest**）：写盘的
生产者仍然只有本进程这一个，别把只读那份当成第二个生产者看。

视觉观测依赖取帧服务：--no-frame-producer 与 vision_observer 不能同时成立 ——
本脚本直接拒绝启动（而不是让观测器偷偷去自拉生产者，那会绕过开关）。只关取帧服务
时明确警告：任务里的视觉调用会报「取帧失败」（不静默返空）。

各组件的节奏（robot_status 写盘周期、运动状态心跳）**一律是 configs 里的常量**
（configs.STATUS_WRITE_INTERVAL / MOTION_STATUS_*），本脚本只决定「起不起」，
不提供周期开关 —— 想改节奏就改那个常量，不必两次启动各调一遍参数。
**例外**：视觉观测周期是**运行时可调**的（Orchestrator/vision_tunables.py 的覆盖层，
`python3 -m Orchestrator.vision_tunables`，改完下一轮生效，不用重启）；它的出厂默认仍是
configs.DEFAULT_INTERVAL（= 档位表里的一档，按档位名取）。抓拍节奏不在此列：帧生产者一直固定高频抓帧。

启动顺序（有意为之，别调换）
--------------------------
  1. 校验开关组合（_validate_combo）—— 非法组合直接拒，不留副作用
  2. 拒绝判定（startup_cleanup.refuse_if_running）—— 已有另一套 agent 主循环在跑就拒。
     **零副作用**（只读 /proc，不碰硬件、不导入入口），所以放在最前面 fail fast
  3. 导入主循环入口模块（_import_entry）—— 缺依赖 / 入口内部导入路径写错，
     到此为止。**先导入、后清理与起组件**：反过来的话，组件（含相机）全起好了才崩，
     白占一次相机、还可能打断兄弟部署
  4. 启动前清理（startup_cleanup.cleanup）—— 停运动 → 收掉旧那套的整棵进程树 →
     收残留组件 → 复核。放在导入之后：它会碰机器人（下发停止指令、杀进程），
     不属于「不做任何事」的那类检查
  5. 起组件 → 等首帧 → 进主循环

与入口的衔接（取帧服务）
----------------------
取帧服务由本脚本作为**子进程**拉起（`frame_producer.py --serve`），帧环在那个子进程的内存
里；本进程拿到的是它的客户端句柄，把它交给 _wait_first_frame 等首帧用。cloud 模式下 entry
是 receiver（它不再跑任何任务循环），消费者只有同进程的 vision_observer 线程与**别的进程**
里的角色侧工具 —— 它们都经 FRAME_PRODUCER_SOCKET 取帧，调用点不必知道帧在谁的内存里。
旧代那条「local 模式把 producer 注入 task_loop.main(frame_source=...)」的路随 task_loop
一起没了，本树里 `frame_source` 已无人接收。
视觉观测的产物不用注入：观测器写 vision_obs.yaml，要读它的按路径直接读（路径登记在
configs/paths.py 的 RUNTIME_VISION_OBSERVATION，角色侧的监督器经 SUPERVISOR_VISION_OBS 拿它）。

输出与日志
----------
  robot_status（子进程）      → runtime_files/logs/status_observer.log（终端只留启停摘要）
  frame_producer（子进程）    → runtime_files/logs/frame_producer.log（同上；取帧服务是长驻
                               组件，输出有 socket 就绪那行、抓帧失败告警、信号收尾）
  tts_service（子进程）       → runtime_files/logs/tts_service.log（同上；常驻 TTS 服务，
                               输出有「已交一句」「队列见底」「播报失败」与信号收尾）
  vision_observer / motion_status（本进程内）
                             → 随主终端。两者输出量都很小：观测器每轮的模型回复那行
                               本来就被注释掉了（vision_observer._record_and_report），
                               motion_status 只在启动时打一行（状态改变不打印），
                               这类信息恰好是跑任务时要当场看到的，故不另落文件。

已在运行的旧实例 / 启动前清理
-------------------------
判定与收树**全部在 Communicator/startup_cleanup.py**（本脚本只调它，不再自己扫 /proc）：

  - 「另一套系统」= 主循环入口 start_agent.py / receiver.py。**含本脚本
    自己**：cloud 模式下 receiver 是被本脚本导入后在**同一进程**里跑 main 的，那套系统的
    根进程命令行是 `python3 start_agent.py cloud`、根本不含 receiver.py —— 只扫 receiver
    会漏掉最常见的重复启动方式。
  - 没有 --force：直接拒绝（零副作用，一个硬件都不碰）。
  - 有 --force：**先停运动**（指令是发给本体执行的，控制系统死掉不会让本体停下来），再
    收掉旧那套的**整棵进程树**（含技能脚本），收不干净就拒绝启动 —— 两套并跑等于重复
    认领云端事件、抢帧环。
  - 残留组件（status_observer / vision_observer / frame_producer）与技能脚本孤儿残骸
    无条件收掉：帧锁与两个状态 yaml 都是全仓唯一写手，两方并存不是合法状态。
    这里提到的 frame_producer 是**上个部署留下的取帧服务子进程**（正常退出由 _stop_children
    收；被 kill -9 才需要这步）—— 收它会连带释放帧锁，陈 socket 由下一个服务自己删。
  - 进程匹配用「入口路径 + 进程 cwd」双锚定，相对/绝对两种起法都认得出，且不会误伤
    别的部署（例如 /root/cmcc_harness_agent_receiver 下那份 receiver）。

只查不杀（不碰硬件）：`python3 Communicator/startup_cleanup.py --inspect`。

用法（环境要自己备齐：ROS 三件套 + LLM key；starter.sh 就是备好之后直接跑本文件）：
    python3 start_agent.py cloud                            # 云端 + 全部组件（默认全起）
    python3 start_agent.py cloud --no-vision-observer       # 不跑视觉观测
    python3 start_agent.py cloud --no-receiver              # 只跑组件，不接云端
    python3 start_agent.py cloud --force                    # 收掉上一套（整棵树）后重起

本脚本自己的选项（与入口同名参数会打架，别混用）：
    --no-receiver          不起 receiver 主循环（只把组件拉起来长驻）
    --no-frame-producer    不起取帧服务（视觉观测/视觉工具随之不可用）
    --no-vision-observer   不起视觉观测器（vision_obs.yaml 停更）
    --no-status-observer   不起状态观测器（robot_status.yaml 停更）
    --no-tts-service       不起常驻 TTS 服务（播报退回逐句脚本；robot_status.yaml 的
                           tools.audio 一格变成 unknown —— 状态话题的订阅者也随它一起没了）
    --no-motion-status     不起运动状态观测器（motion_status.yaml 停更）
    --force                默认拒绝重复启动；加了它会先停运动，再收掉旧那套的整棵
                           进程树后重起（见上「已在运行的旧实例」）
"""

import argparse
import importlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# 确保项目根目录在 Python 搜索路径中（用于导入 configs 包）
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import (
    PROC_KILL_WAIT_SEC,
    PROC_TERM_WAIT_SEC,
    STATUS_WRITE_INTERVAL,
)
from configs.paths import (
    FRAME_PRODUCER_MODULE,
    RECEIVER_MODULE,
    RUNTIME_LOGS_DIR,
    RUNTIME_MOTION_STATUS,
    RUNTIME_ROBOT_STATUS,
    STATUS_OBSERVER_MODULE,
    TTS_SERVICE_MODULE,
    TTS_SERVICE_SOCKET,
)
# 启动前清理（拒绝重复启动 / 收旧那套的整棵进程树 / 停运动）全在这个模块里
from Communicator import startup_cleanup

_PREFIX = '[start_agent]'

# 子进程起来后先观察这么久：立刻退出说明导入/环境有问题（最常见是没 source ROS），
# 直接把日志尾部打出来，免得变成「启动了但什么都没干」的静默失败。
_CHILD_BOOT_GRACE_SEC = 0.8

# 常驻 TTS 服务起来后等它应答 socket 的上限（秒）。子进程活着不等于已经在服务：
# 它要先 import rclpy、init、建节点，然后才 bind socket（见 _start_tts_service）。
_TTS_ATTACH_WAIT_SEC = 8.0

# 进主循环前等首帧的上限（秒）。相机可能被别的进程占着，等不到只告警不拦：
# 后台抓拍会持续重试，任务里第一次视觉调用失败时会给出明确报错。
_FIRST_FRAME_WAIT_SEC = 10.0

# 子进程日志尾部回显行数
_TAIL_LINES = 12

# 子进程日志文件句柄：留到收摊时统一关（子进程继承了同一 fd，父进程提前关不影响它）
_CHILD_LOG_HANDLES = []

# 收摊信号：进程内的运动状态观测线程靠它退出（daemon 线程随进程结束也会没，但
# 「起完组件就返回」的路径上进程还要活一会，明确收一下更干净）
_MOTION_STOP = threading.Event()


def _say(msg):
    """本脚本的统一输出前缀（子进程的日志走文件，不混进来）。"""
    print(f'{_PREFIX} {msg}', flush=True)


# =============================================================================
# 后台组件子进程
# =============================================================================

def _tail(path, lines=_TAIL_LINES):
    """读日志尾部若干行，用于启动失败时给出可诊断的现场。"""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        return f'    | (读日志失败: {exc})'
    tail = text.splitlines()[-lines:]
    return '\n'.join('    | ' + line for line in tail) or '    | (日志为空)'


def _start_child(name, module_path, extra_args=()):
    """拉起后台组件子进程，stdout/stderr 落 runtime_files/logs/<name>.log。

    上一次的日志留一份 .prev，便于对照「这次启动之前」的现场。
    """
    RUNTIME_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUNTIME_LOGS_DIR / f'{name}.log'
    if log_path.exists():
        log_path.replace(RUNTIME_LOGS_DIR / f'{name}.log.prev')

    handle = log_path.open('w', encoding='utf-8')
    cmd = [sys.executable, str(module_path)] + [str(a) for a in extra_args]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )

    time.sleep(_CHILD_BOOT_GRACE_SEC)
    if proc.poll() is not None:
        handle.close()
        raise RuntimeError(
            f'{name} 启动后立刻退出（code={proc.returncode}），日志 {log_path}:\n'
            f'{_tail(log_path)}'
        )
    _CHILD_LOG_HANDLES.append(handle)
    _say(f'{name} 已启动（pid={proc.pid}，日志 {log_path}）')
    return proc


def _stop_children(children):
    """收掉本脚本拉起的子进程：SIGTERM → 等 → SIGKILL，然后关日志句柄。"""
    for name, proc in children:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    for name, proc in children:
        if proc.poll() is None:
            try:
                proc.wait(timeout=PROC_TERM_WAIT_SEC)
            except subprocess.TimeoutExpired:
                _say(f'{name}(pid={proc.pid}) 未按时退出，强杀')
                try:
                    proc.kill()
                    proc.wait(timeout=PROC_KILL_WAIT_SEC)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        _say(f'{name}(pid={proc.pid}) 已停')
    for handle in _CHILD_LOG_HANDLES:
        try:
            handle.close()
        except OSError:
            pass
    del _CHILD_LOG_HANDLES[:]


def _bring_up_components(args):
    """按开关起后台组件；返回 (子进程列表, 取帧句柄或 None)。

    形态分两类（见文件头）：
      - 独立子进程：状态观测器（与帧环无关、与智能体只靠文件交互，天然可独立）
        + **取帧服务**（帧环在它自己的内存里，对外只开一个 unix socket）
        + **常驻 TTS 服务**（播报客户端常驻下来，对外也只开一个 unix socket）；
      - 本进程内：视觉观测器线程（读帧环，现在是取帧服务的客户端）
        + 运动状态观测器线程（要读 MotionManager 的内存记录）。

    顺序：先常驻 TTS 服务（失败就早退，还没碰相机），再状态观测器，再运动状态观测器，
    再取帧服务，最后视觉观测器线程 —— 观测器要以客户端身份取帧，服务必须先起来
    （_start_frame_service 里已等到 socket 可用才返回）。TTS 服务排在状态观测器**之前**也是
    这个道理：观测器一上来就问它要 tools.audio 的事实，服务还没起就只能报 unknown，而那一份
    快照要冻 60s。残留组件不在这里收 —— 启动前清理（startup_cleanup.cleanup）已经无条件把
    五个组件与技能孤儿都收掉了，这里再收一遍就是同一件事两处判据。
    """
    children = []

    if args.tts_service:
        proc = _start_tts_service()
        # 已有实例在服务时返回 None（那不是本脚本拉起的，也就轮不到本脚本收它）。
        if proc is not None:
            children.append(('tts_service', proc))
    else:
        _say('按 --no-tts-service 跳过常驻 TTS 服务'
             '（播报退回逐句脚本；robot_status.yaml 的 tools.audio 变成 unknown）')

    if args.status_observer:
        proc = _start_child('status_observer', STATUS_OBSERVER_MODULE)
        children.append(('status_observer', proc))
    else:
        _say('按 --no-status-observer 跳过状态观测器（robot_status.yaml 停更）')

    if args.motion_status:
        _start_motion_status_thread()
    else:
        _say('按 --no-motion-status 跳过运动状态观测器（motion_status.yaml 停更）')

    producer = None
    if args.frame_producer:
        proc, producer = _start_frame_service()
        # 加进 children：本进程退出时它跟着收（SIGTERM → 服务 unlink socket + 停抓拍），
        # --no-receiver 那条长驻路径也会盯它（意外死亡即报错，与状态观测器同款）。
        children.append(('frame_producer', proc))
    else:
        _say('按 --no-frame-producer 跳过取帧服务'
             '（视觉工具取帧会报「取帧失败…」，不静默返空）')

    if args.vision_observer:
        _start_vision_observer_thread()
    else:
        _say('按 --no-vision-observer 跳过视觉观测器（vision_obs.yaml 停更）')

    return children, producer


def _start_tts_service():
    """拉起常驻 TTS 服务子进程（tts_service.py --serve），返回子进程；已有实例在服务时返回 None。

    单实例的常态是「锁没人持、socket 不在」。这里先问一次 socket：**已有实例在服务就完全
    不动它**（不抢锁、不拉第二个）—— 那不是错误，正是单实例要的结果（上一个部署没收干净，
    或有人手工先起了，消费者照常能用那一个）。真拉起了新的，还要等它应答再往下走（子进程
    活着不等于已经在服务，它得先 init rclpy、建节点、bind socket）：等不到当场报错，不带一个
    「谁也用不了」的服务往下跑 —— 那会让所有播报静默退回逐句脚本，而没人知道为什么。
    """
    from TTS_Manager import tts_client

    if tts_client.service_available():
        _say('常驻 TTS 服务已在运行（socket 可用），不拉起第二个')
        return None
    proc = _start_child('tts_service', TTS_SERVICE_MODULE, extra_args=('--serve',))
    deadline = time.monotonic() + _TTS_ATTACH_WAIT_SEC
    while time.monotonic() < deadline:
        if tts_client.service_available():
            _say(f'常驻 TTS 服务已就绪（子进程 pid={proc.pid}，{TTS_SERVICE_SOCKET}；'
                 f'插件与各工具都经它播报）')
            return proc
        time.sleep(0.2)
    raise RuntimeError(
        f'常驻 TTS 服务起来了但连不上（{TTS_SERVICE_SOCKET} 等不到应答）：'
        f'见 {RUNTIME_LOGS_DIR / "tts_service.log"}'
    )


def _start_frame_service():
    """拉起取帧服务子进程（frame_producer.py --serve），返回它的取帧句柄。

    取帧服务是**帧环的唯一所有者**：它抢 FRAME_PRODUCER_LOCK_FILE、开抓拍、bind
    FRAME_PRODUCER_SOCKET；本进程与其它所有消费者（视觉观测器、角色侧工具进程）都是
    它的客户端，经那个 socket 取帧（见 skills/camera/frame_producer.py 模块头）。
    因此它可以是独立子进程 —— 帧环不再需要与消费者同进程，这正是本组件从「进程内线程」
    改成「子进程」的原因。

    子进程活着不等于能取帧：socket 要等它 import 完、抢到锁、bind 上才存在，所以拉起后
    用 attach() 等一个有限窗口（FRAME_PRODUCER_ATTACH_TIMEOUT，盖过 _CHILD_BOOT_GRACE_SEC
    与 import 时间）。等不到 = 有人占着锁并跑了别的生产者，或服务自身起不来 —— 两种都
    当场报错，不带瞎眼的视觉往下跑。
    """
    from skills.camera import frame_producer

    proc = _start_child('frame_producer', FRAME_PRODUCER_MODULE, extra_args=('--serve',))
    client = frame_producer.attach()
    if client is None:
        raise RuntimeError(
            f'取帧服务起来了但连不上（{frame_producer.FRAME_PRODUCER_SOCKET} 等不到应答）：'
            f'可能是别的进程持有帧生产者锁（另一个 agent / 独立 observer 在跑），'
            f'或服务自身没 serve 起来。见 {RUNTIME_LOGS_DIR / "frame_producer.log"}'
        )
    state = frame_producer.describe_source()
    _say(f'取帧服务已启动（子进程 pid={proc.pid}，{state.detail}；'
         f'视觉观测器与视觉工具都经这个 socket 取帧）')
    return proc, client


def _start_vision_observer_thread():
    """在本进程内起视觉观测器线程（纯消费者：不 stop 生产者，daemon 随进程退出）。

    观测器不接收任何帧句柄：它内部的模块级取帧函数会自己找**取帧服务**
    （frame_producer.py 的三条分派规则），所以这里只管起线程。
    观测周期**不在这里转传**（interval 传 None）：它由 Observer/vision_observer 每轮现读
    Orchestrator.vision_tunables 的运行时可调覆盖层 —— 出厂默认才是 configs.DEFAULT_INTERVAL。
    本脚本只把「启动时的生效值」念一遍，真正生效的以观测器那条启动行为准。
    """
    from Observer.vision_observer import run as run_vision_observer
    from Orchestrator import vision_tunables

    tunables = vision_tunables.current()
    threading.Thread(
        target=run_vision_observer,
        name='vision-observer',
        daemon=True,
    ).start()
    _say(
        f'视觉观测器线程已在本进程内启动'
        f'（周期 {tunables.observe_interval:g}s，来源 {tunables.source}，daemon；'
        f'可用 python3 -m Orchestrator.vision_tunables 改，不用重启）'
    )


def _start_motion_status_thread():
    """在本进程内起运动状态观测器线程（读 MotionManager 的内存记录，见文件头）。

    import 与 rclpy 可用性检查都放在**起线程之前**：那条路出错（没 source ROS）要让
    本脚本当场报出来，而不是让线程在后台静默死掉 —— 静默死的症状是 motion_status.yaml
    冻住，看起来和「狗一直没动」一模一样。
    """
    from Motion_Manager import motion_status

    if motion_status.IMPORT_ERROR is not None:
        raise RuntimeError(
            f'运动状态观测器不可用：无法导入 rclpy / 消息包'
            f'（{motion_status.IMPORT_ERROR}）。检查 ROS 环境是否已 source，'
            f'或加 --no-motion-status 明确跳过'
        )
    threading.Thread(
        target=motion_status.run,
        kwargs={'stop_event': _MOTION_STOP},
        name='motion-status',
        daemon=True,
    ).start()
    _say(f'运动状态观测器线程已在本进程内启动（状态改变时写 {RUNTIME_MOTION_STATUS.name}，daemon）')


# =============================================================================
# 主循环入口调用
# =============================================================================

def _call_entry_main(entry_main, entry_argv, entry_name, entry_kwargs=None):
    """把剩余参数原样交给入口模块的 main()（参数透传）。

    sys.argv 临时改写成入口自己的调用形态：入口的 argparse（含 --help、必填校验、
    默认值）就是唯一事实来源，本脚本不再抄一份。命令行参数之外的**对象**无法走 argv，
    经 entry_kwargs 传（本树已无人用这条，留着给还要注入对象的入口）。
    """
    saved_argv = sys.argv
    sys.argv = [entry_name] + list(entry_argv)
    try:
        entry_main(**(entry_kwargs or {}))
    finally:
        sys.argv = saved_argv


def _import_entry(module_path, hint):
    """起组件**之前**先把主循环入口模块导入解析掉，返回它的 main。

    放在清理与起组件之前是有意的：入口导入失败（缺依赖、内部导入路径写错）在真机上
    表现为「组件全起好、相机都抓了一帧，然后崩」，白占一次相机还可能打断兄弟部署。
    先导入 → 失败就到此为止，一个硬件都不碰（拒绝判定更要更早，见文件头启动顺序）。
    """
    name = '.'.join(module_path.relative_to(_ROOT).with_suffix('').parts)
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 — 缺依赖/导入错都要给出可读原因
        raise RuntimeError(
            f'入口模块 {name} 导入失败：{type(exc).__name__}: {exc}\n'
            f'  → {hint}'
        ) from exc
    return getattr(module, 'main')


def _wait_first_frame(producer):
    """进主循环前等首帧：等到说明相机通路正常；等不到只告警不拦。

    等不到不该拦：相机可能被别的进程短暂占用，后台抓拍会持续重试，任务里第一次
    视觉调用若是空帧会给出明确报错。
    """
    if producer is None:
        return
    if producer.wait_for_first_frame(timeout=_FIRST_FRAME_WAIT_SEC):
        _say('已取到首帧，相机通路正常')
    else:
        _say(f'⚠️ {_FIRST_FRAME_WAIT_SEC:g}s 内没等到首帧（相机可能被占用），'
             f'继续进主循环，后台抓拍会持续重试')


def _teardown(children, producer):
    """收摊：先前台说明，再收子进程与进程内线程，最后关掉取帧服务的客户端连接。

    `producer` 是取帧服务的**客户端句柄**（帧服务是子进程，已随 _stop_children 收到
    SIGTERM）：它的 stop() 只关本进程这条连接（可能已经随服务断开而作废），**不停服务**。
    """
    if children:
        _say('开始收摊…')
    _MOTION_STOP.set()          # 运动状态观测线程（daemon，随进程退出也会没，这里让它先收干净）
    _stop_children(children)
    if producer is not None:
        try:
            producer.stop()
            _say('取帧服务已停止')
        except Exception as exc:  # 收摊失败不该盖住主循环的退出原因
            _say(f'停止取帧服务失败: {exc}')


def run_cloud(args, entry_argv):
    """云端通信模式：起组件 → 进 receiver 主循环（--no-receiver 则只跑组件）。"""
    _say('模式：云端通信（Communicator/receiver.py）')
    # 拒绝判定在最前：纯查询、零副作用，已有另一套就当场拒绝（不碰硬件）
    startup_cleanup.refuse_if_running(args.force, _say)
    if not args.receiver:
        _say('按 --no-receiver：不进 receiver，只把组件拉起来长驻（Ctrl+C 退出）')

    # 先解析入口模块，再碰任何硬件（详见 _import_entry）
    receiver_main = None
    if args.receiver:
        receiver_main = _import_entry(
            RECEIVER_MODULE,
            '检查 ROS 环境是否已 source（缺 homi_speech_interface / claw_client 等运行时包）'
            '，以及 receiver.py 内部的导入路径。')

    # 入口没起来就不动硬件；到这里才停运动、收旧那套的整棵树、收残留组件
    startup_cleanup.cleanup(force=args.force, logger=_say)

    children = []
    producer = None
    try:
        children, producer = _bring_up_components(args)
        _say(f'状态快照：{_status_path_hint()}')

        if not args.receiver:
            _wait_until_interrupt(children)
            return 0

        _wait_first_frame(producer)

        _call_entry_main(receiver_main, entry_argv, str(RECEIVER_MODULE))
    finally:
        _teardown(children, producer)
    return 0


def _wait_until_interrupt(children):
    """只跑组件模式（cloud --no-receiver）：长驻到 Ctrl+C / SIGTERM。

    顺带盯一眼子进程：状态观测器、**取帧服务**或**常驻 TTS 服务**意外死掉时当场报错退出，
    而不是让本进程假装还在工作（取帧服务是帧环唯一所有者，它死了就等于视觉全瞎；常驻 TTS
    服务死了播报会静默退回逐句脚本，见文件头；视觉观测器与运动状态观测器是进程内线程，
    死了会直接体现在主进程）。
    """
    _say('只跑组件，未进 agent 主循环；Ctrl+C 退出')
    while True:
        for name, proc in children:
            if proc.poll() is not None:
                raise RuntimeError(
                    f'组件 {name} 意外退出（code={proc.returncode}），'
                    f'见 {RUNTIME_LOGS_DIR / (name + ".log")}'
                )
        time.sleep(1.0)


def _status_path_hint():
    """两份状态快照的落盘路径与节奏（给启动横幅用，避免脚本里再硬编码路径）。"""
    return (f'{RUNTIME_ROBOT_STATUS}（每 {STATUS_WRITE_INTERVAL:g}s 整份覆盖写）'
            f'｜运动状态 {RUNTIME_MOTION_STATUS}（状态改变即写 + 心跳）')


# =============================================================================
# CLI 入口
# =============================================================================

def _build_parser():
    parser = argparse.ArgumentParser(
        prog='start_agent.py',
        description='Agent 统一启动入口：按开关起好并列组件（receiver / 取帧服务 / '
                    '常驻 TTS 服务 / 视觉观测器 / 状态观测器 / 运动状态观测器）'
                    '后进 receiver 主循环',
        epilog=(
            '示例:\n'
            '  python3 start_agent.py cloud\n'
            '  python3 start_agent.py cloud --no-receiver   # 只跑组件，不接云端事件\n'
            '\n'
            '模式名之后的参数原样透传给入口模块；两端都不认的参数会被入口拒绝，\n'
            '所以这里不列举它们的参数（不抄一份它们的默认值）。\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('mode', choices=['cloud'], help='运行模式')
    parser.add_argument(
        '--no-receiver', dest='receiver', action='store_false',
        help='不进 receiver，只把组件拉起来长驻',
    )
    parser.add_argument(
        '--no-frame-producer', dest='frame_producer', action='store_false',
        help='不起取帧服务（视觉观测与视觉工具随之不可用）',
    )
    parser.add_argument(
        '--no-vision-observer', dest='vision_observer', action='store_false',
        help='不起视觉观测器（vision_obs.yaml 停更）',
    )
    parser.add_argument(
        '--no-status-observer', dest='status_observer', action='store_false',
        help='不起状态观测器（默认起，写 robot_status.yaml）',
    )
    parser.add_argument(
        '--no-tts-service', dest='tts_service', action='store_false',
        help='不起常驻 TTS 服务（播报退回逐句脚本；tools.audio 随之变成 unknown）',
    )
    parser.add_argument(
        '--no-motion-status', dest='motion_status', action='store_false',
        help='不起运动状态观测器（默认起，写 motion_status.yaml）',
    )
    parser.add_argument(
        '--force', action='store_true',
        help='已有另一套 agent 在跑时也继续：先停运动，再收掉它那套的整棵进程树后重起'
             '（默认直接拒绝）',
    )
    return parser


def _install_sigterm_handler():
    """SIGTERM 转 KeyboardInterrupt：让主循环的 finally 有机会收摊。

    receiver 内部也装了自己的同名处理器（行为一致），两层都转成同一个异常。
    """
    def _handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handler)


def _validate_combo(args):
    """开关组合校验：在任何副作用（收旧进程、起组件）之前就把非法组合挡掉。

    唯一硬约束是「视觉观测必须有取帧服务」：观测器每轮向帧环取帧，关了服务就没有帧。
    这里直接拒绝，而不是放它去自拉生产者（vision_observer.run() 内部有 start() 兜底，
    那条路会绕过 --no-frame-producer，让开关失效）。
    """
    if args.vision_observer and not args.frame_producer:
        raise RuntimeError(
            '视觉观测器必须有取帧服务：观测器每轮从帧环取帧，没有它就没有帧。'
            '要么去掉 --no-frame-producer，要么同时加 --no-vision-observer'
        )
    if not args.receiver and not (
        args.frame_producer or args.vision_observer or args.status_observer
        or args.motion_status or args.tts_service
    ):
        raise RuntimeError(
            '--no-receiver 且五个组件全关（status/motion/取帧服务/视觉观测/TTS 服务）：'
            '没有任何事情要做，去掉 --no-receiver 或至少开一个组件'
        )


def main():
    parser = _build_parser()
    # 入口参数（--user-prompt 等）不在本脚本声明，用 parse_known_args 收剩下来的
    args, entry_argv = parser.parse_known_args()

    _install_sigterm_handler()
    _say(f'仓库根目录 {_ROOT}')
    try:
        _validate_combo(args)
        return run_cloud(args, entry_argv)
    except KeyboardInterrupt:
        _say('收到中断，已退出')
        return 130
    except RuntimeError as exc:
        _say(f'❌ {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
