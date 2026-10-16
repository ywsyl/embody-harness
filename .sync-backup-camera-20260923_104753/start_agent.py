#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agent 统一启动入口（start_agent）

先按开关起好并列组件（receiver / 帧生产者 / 视觉观测器 / 状态观测器 / 运动状态观测器），
再进 agent 主循环；退出时把组件一起收掉。

两种模式
--------
cloud  云端通信模式：进 Communicator/receiver.py，等云端下发任务，长驻到被中断。
local  本地单次模式：进 Agents/task/task_loop.py 跑一局任务（--user-prompt 新建 /
       --task-id 续跑），跑完即收摊退出。

两种模式下本脚本**不认识的参数一律原样透传**给入口（沿用 run_task_loop.sh 的
「参数透传」约定），所以 --user-prompt / --task-id / --max-iter 以及 receiver 的
--test-send 都不用在这里再声明一份，避免与入口的参数默认值漂移。

五个并列组件：谁起、以什么形态起
--------------------------------
组件各自独立，由本脚本按参数决定起哪些（默认全起，--no-xxx 关掉）：

  组件              形态          取哪些开关           职责
  receiver          本进程主循环   cloud 模式 / --no-receiver   云端通信（认领任务、上报）
  frame_producer    本进程内       --no-frame-producer          相机固定高频抓拍 → 内存帧环
  vision_observer   本进程内线程   --no-vision-observer         按可调周期看帧环、最新 N 帧
                                                                 拼接 → VQA → vision_obs.yaml
  robot_status      独立子进程     --no-status-observer         订阅 ROS → robot_status.yaml
  motion_status     本进程内线程   --no-motion-status           订阅 ROS + 读 manager 的
                                                                 内存记录 → motion_status.yaml
                                                                 （没起它时，MotionManager
                                                                 会懒起一份**只读**订阅，见下）

receiver 只管云端收发，不再涉及帧与观测（原先它在 main() 里 start 生产者 + 起观测
线程，已删除）。

⚠️ 为什么 frame_producer / vision_observer 必须与主循环**同进程**
帧环是「帧生产者所在进程」的内存：跨进程只有一把排他锁、没有共享读通路
（skills/camera/frame_producer.py 模块头；ToolManager/tool_executor.py 的
_vision_dispatch 也是因此必须进程内 import 才能取到帧）。若把它们拉成独立子进程：
  - 子进程先抢到 FRAME_PRODUCER_LOCK_FILE → 主循环进程里 start() 返回 None，
    视觉工具取帧直接 RuntimeError「共享帧环无可用帧」，vision_obs.yaml 也停更；
  - vision_observer 在第三个进程里同样抢不到锁 → 立刻退出，不产任何观测。
所以「独立」只在**单独跑**时成立（那时它自己就是生产者，各自的 CLI 都能跑）；
一旦要在跑 agent 主循环的同时做视觉，就必须与主循环同进程。状态观测器没有这个
约束（只订阅 ROS 话题、与帧环无关），按设计就是独立 CLI，故拉成子进程。

⚠️ 为什么 motion_status 必须与主循环**同进程**
「最近一笔运动是谁下发的」这份记录（Motion_Manager/motion_record.py）2026-09-20 起只在
MotionManager 的**进程内存**里，不再落盘。它由接收任务/跑工具的那一层惰性导入 —— 也就是
本进程。把 motion_status 拉成子进程的话，它读到的永远是「没有这一笔」，而不会报错。所以
它只能在本进程内起线程（与 frame_producer / vision_observer 同样的形态，理由不同）。
**但「跑 MotionManager 的进程」不止本进程**：receiver 收「继续任务」时会 spawn 一个子进程跑
task_loop（Communicator/receiver.py），那里没有这条线程。所以 MotionManager 要判动作状态时，
若本进程没有常驻订阅，会经 motion_status.ensure_reader() 懒起一份**只读**订阅（订阅 ROS、攒帧，
**不写 motion_status.yaml、不发 _latest**）：写盘的生产者仍然只有本进程这一个，别把只读那份
当成第二个生产者看。

视觉观测依赖帧生产者：--no-frame-producer 与 vision_observer 不能同时成立 ——
本脚本直接拒绝启动（而不是让观测器偷偷去自拉生产者，那会绕过开关）。只关生产者
时明确警告：任务里的视觉调用会报「共享帧环无可用帧」。

各组件的节奏（robot_status 写盘周期、运动状态心跳）**一律是 configs 里的常量**
（configs.STATUS_WRITE_INTERVAL / MOTION_STATUS_*），本脚本只决定「起不起」，
不提供周期开关 —— 想改节奏就改那个常量，不必两次启动各调一遍参数。
**例外**：视觉观测周期是**运行时可调**的（configs/vision_tunables.py 的覆盖层，
`python3 -m configs.vision_tunables`，改完下一轮生效，不用重启）；它的出厂默认仍是
configs.DEFAULT_INTERVAL。抓拍节奏不在此列：帧生产者一直固定高频抓帧。

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

与 task_loop 的衔接
------------------
帧生产者对象由本脚本注入 task_loop（local 模式走 task_loop.main(frame_source=...)；
cloud 模式下 receiver 内部那层 run() 调用拿不到注入，task_loop 自己取
frame_producer.get_default()，**是同一个进程内单例对象**，两条路径等价）。
视觉观测的产物不用注入：观测器写 vision_obs.yaml，task_loop / chat_loop 每轮经
tools/runtime_state.py 读它。

输出与日志
----------
  robot_status（子进程）      → runtime_files/logs/status_observer.log（终端只留启停摘要）
  frame_producer / vision_observer / motion_status（本进程内）
                             → 随主终端。三者输出量都很小：观测器每轮的模型回复那行
                               本来就被注释掉了（vision_observer._record_and_report），
                               frame_producer 只在异常/抢占相机时告警，motion_status
                               只在启动时打一行（状态改变不打印），这类信息恰好是
                               跑任务时要当场看到的，故不另落文件。

已在运行的旧实例 / 启动前清理
-------------------------
判定与收树**全部在 Communicator/startup_cleanup.py**（本脚本只调它，不再自己扫 /proc）：

  - 「另一套系统」= 主循环三入口 start_agent.py / receiver.py / task_loop.py。**含本脚本
    自己**：cloud 模式下 receiver 是被本脚本导入后在**同一进程**里跑 main 的，那套系统的
    根进程命令行是 `python3 start_agent.py cloud`、根本不含 receiver.py —— 只扫 receiver
    会漏掉最常见的重复启动方式。
  - 没有 --force：直接拒绝（零副作用，一个硬件都不碰）。
  - 有 --force：**先停运动**（指令是发给本体执行的，控制系统死掉不会让本体停下来），再
    收掉旧那套的**整棵进程树**（含 multiprocessing spawn 起的 task-resume 子进程与
    技能脚本），收不干净就拒绝启动 —— 两套并跑等于重复认领云端任务、抢帧环。
  - 残留组件（status_observer / vision_observer / frame_producer）与技能脚本孤儿残骸
    无条件收掉：帧锁与两个状态 yaml 都是全仓唯一写手，两方并存不是合法状态。
  - 进程匹配用「入口路径 + 进程 cwd」双锚定，相对/绝对两种起法都认得出，且不会误伤
    别的部署（例如 /root/cmcc_harness_agent_receiver 下那份 receiver）。

只查不杀（不碰硬件）：`python3 Communicator/startup_cleanup.py --inspect`。

用法（一律经 start_agent.sh 走，它负责 source ROS/LLM 环境）：
    ./start_agent.sh cloud                                  # 云端 + 全部组件（默认全起）
    ./start_agent.sh cloud --no-vision-observer             # 不跑视觉观测
    ./start_agent.sh cloud --no-receiver                    # 只跑组件，不接云端
    ./start_agent.sh local --user-prompt "去工位1"
    ./start_agent.sh local --task-id 20260909102107_SearchForZhangBo
    ./start_agent.sh local --user-prompt "去找红色水杯" --max-iter 50
    ./start_agent.sh cloud --force                          # 收掉上一套（整棵树）后重起

本脚本自己的选项（与入口同名参数会打架，别混用）：
    --no-receiver          不起 receiver 主循环（cloud 模式只把组件拉起来长驻；
                           local 模式本就不涉及 receiver，该开关被忽略）
    --no-frame-producer    不起帧生产者（视觉观测/视觉工具随之不可用）
    --no-vision-observer   不起视觉观测器（vision_obs.yaml 停更）
    --no-status-observer   不起状态观测器（robot_status.yaml 停更）
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
    RECEIVER_MODULE,
    RUNTIME_LOGS_DIR,
    RUNTIME_MOTION_STATUS,
    RUNTIME_ROBOT_STATUS,
    STATUS_OBSERVER_MODULE,
    TASK_LOOP_MODULE,
)
# 启动前清理（拒绝重复启动 / 收旧那套的整棵进程树 / 停运动）全在这个模块里
from Communicator import startup_cleanup

_PREFIX = '[start_agent]'

# 子进程起来后先观察这么久：立刻退出说明导入/环境有问题（最常见是没 source ROS），
# 直接把日志尾部打出来，免得变成「启动了但什么都没干」的静默失败。
_CHILD_BOOT_GRACE_SEC = 0.8

# local 模式进主循环前等首帧的上限（秒）。相机可能被别的进程占着，等不到只告警不拦：
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
    """按开关起后台组件；返回 (子进程列表, 进程内生产者或 None)。

    形态分两类（见文件头）：
      - 独立子进程：状态观测器（与帧环无关、与智能体只靠文件交互，天然可独立）；
      - 本进程内：帧生产者（帧环是本进程内存）+ 视觉观测器线程（要读这个帧环）
        + 运动状态观测器线程（要读 MotionManager 的内存记录）。

    顺序：先状态观测器（失败就早退，还没碰相机），再运动状态观测器，再生产者，
    最后视觉观测器线程。残留组件不在这里收 —— 启动前清理（startup_cleanup.cleanup）
    已经无条件把三个组件与技能孤儿都收掉了，这里再收一遍就是同一件事两处判据。
    """
    children = []

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
        producer = _start_producer_inprocess()
    else:
        _say('按 --no-frame-producer 跳过帧生产者'
             '（视觉工具取帧会报「共享帧环无可用帧」）')

    if args.vision_observer:
        _start_vision_observer_thread()
    else:
        _say('按 --no-vision-observer 跳过视觉观测器（vision_obs.yaml 停更）')

    return children, producer


def _start_producer_inprocess():
    """在本进程内启动帧生产者（幂等）；被别人持锁时明确报错。

    帧生产者必须与视觉观测器、视觉工具同进程（帧环是进程内存，见文件头）。
    跨进程单实例锁被别人持有（另一个 receiver / 独立 observer 在跑）时 start()
    返回 None —— 此时本进程没有帧，与其带着瞎眼的视觉往下跑，不如当场说清楚。
    """
    from skills.camera import frame_producer

    producer = frame_producer.start()
    if producer is None:
        raise RuntimeError(
            '帧生产者已被另一进程持有（另一个 agent / 独立 observer 在跑）。'
            '帧环在对方进程里，本进程取不到帧 —— 先停掉它，或改用 --no-frame-producer。'
        )
    _say('帧生产者已在本进程内启动（周期抓拍，供视觉观测器与视觉工具取帧）')
    return producer


def _start_vision_observer_thread():
    """在本进程内起视觉观测器线程（纯消费者：不 stop 生产者，daemon 随进程退出）。

    观测周期**不在这里转传**（interval 传 None）：它由 Observer/vision_observer 每轮现读
    configs.vision_tunables 的运行时可调覆盖层 —— 出厂默认才是 configs.DEFAULT_INTERVAL。
    本脚本只把「启动时的生效值」念一遍，真正生效的以观测器那条启动行为准。
    """
    from Observer.vision_observer import run as run_vision_observer
    from configs import vision_tunables

    tunables = vision_tunables.current()
    threading.Thread(
        target=run_vision_observer,
        name='vision-observer',
        daemon=True,
    ).start()
    _say(
        f'视觉观测器线程已在本进程内启动'
        f'（周期 {tunables.observe_interval:g}s，来源 {tunables.source}，daemon；'
        f'可用 python3 -m configs.vision_tunables 改，不用重启）'
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
    """把剩余参数原样交给入口模块的 main()（参数透传，同 run_task_loop.sh）。

    sys.argv 临时改写成入口自己的调用形态：入口的 argparse（含 --help、必填校验、
    默认值）就是唯一事实来源，本脚本不再抄一份。命令行参数之外的**对象**（如注入
    task_loop 的帧生产者）无法走 argv，经 entry_kwargs 传。
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
    """收摊：先前台说明，再收子进程与进程内线程，最后停本进程内起的帧生产者。"""
    if children:
        _say('开始收摊…')
    _MOTION_STOP.set()          # 运动状态观测线程（daemon，随进程退出也会没，这里让它先收干净）
    _stop_children(children)
    if producer is not None:
        try:
            producer.stop()
            _say('帧生产者已停止')
        except Exception as exc:  # 收摊失败不该盖住主循环的退出原因
            _say(f'停止帧生产者失败: {exc}')


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


def run_local(args, entry_argv):
    """本地单次模式：起组件 → 跑一局 task_loop（跑完收摊）。"""
    _say('模式：本地单次（Agents/task/task_loop.py，跑完即收摊）')
    # 拒绝判定在最前：纯查询、零副作用，已有另一套就当场拒绝（不碰硬件）
    startup_cleanup.refuse_if_running(args.force, _say)
    if not args.receiver:
        _say('local 模式本就不涉及 receiver，--no-receiver 忽略')

    # 先解析入口模块，再碰任何硬件（详见 _import_entry）
    task_loop_main = _import_entry(
        TASK_LOOP_MODULE,
        '检查 ROS 环境是否已 source（缺 homi_speech_interface / claw_client 等运行时包）'
        '，以及 task_loop.py 内部的导入路径。')

    # 入口没起来就不动硬件；到这里才停运动、收旧那套的整棵树、收残留组件
    startup_cleanup.cleanup(force=args.force, logger=_say)

    children = []
    producer = None
    try:
        children, producer = _bring_up_components(args)
        _say(f'状态快照：{_status_path_hint()}')

        _wait_first_frame(producer)

        # ---- 进 agent 主循环：本进程内跑，并把帧生产者注入 task_loop ----
        # 注入的是本进程内的单例对象（帧环是本进程内存）；task_loop 不传时自己取
        # frame_producer.get_default()，与本行等价，注入是为了让依赖显式可见。
        _say('进入 task_loop 主循环')
        _call_entry_main(
            task_loop_main, entry_argv, str(TASK_LOOP_MODULE),
            entry_kwargs={'frame_source': producer},
        )
    finally:
        _teardown(children, producer)
    return 0


def _wait_until_interrupt(children):
    """只跑组件模式（cloud --no-receiver）：长驻到 Ctrl+C / SIGTERM。

    顺带盯一眼子进程：状态观测器意外死掉时当场报错退出，而不是让本进程假装还在工作
    （另一个后台组件——帧生产者/视觉观测器——是进程内线程，死了会直接体现在主进程）。
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
        prog='start_agent.sh',
        description='Agent 统一启动入口：按开关起好并列组件（receiver / 帧生产者 / '
                    '视觉观测器 / 状态观测器 / 运动状态观测器）后进 agent 循环'
                    '（cloud / local）',
        epilog=(
            '示例:\n'
            '  ./start_agent.sh cloud\n'
            '  ./start_agent.sh cloud --no-receiver        # 只跑组件，不接云端\n'
            '  ./start_agent.sh local --user-prompt "去工位1"\n'
            '  ./start_agent.sh local --task-id 20260909102107_SearchForZhangBo\n'
            '\n'
            '模式名之后的参数原样透传给入口脚本（task_loop 的 --user-prompt/--task-id/\n'
            '--max-iter、receiver 的 --test-send 都可用）。\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('mode', choices=['cloud', 'local'], help='运行模式')
    parser.add_argument(
        '--no-receiver', dest='receiver', action='store_false',
        help='cloud 模式：不进 receiver，只把组件拉起来长驻（local 模式忽略）',
    )
    parser.add_argument(
        '--no-frame-producer', dest='frame_producer', action='store_false',
        help='不起帧生产者（视觉观测与视觉工具随之不可用）',
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

    receiver 内部也会装自己的同名处理器（行为一致）；task_loop 不装，所以这里统一装。
    """
    def _handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handler)


def _validate_combo(args):
    """开关组合校验：在任何副作用（收旧进程、起组件）之前就把非法组合挡掉。

    唯一硬约束是「视觉观测必须有帧生产者」：观测器每轮向帧环取帧，关了生产者就没有
    帧。这里直接拒绝，而不是放它去自拉生产者（vision_observer.run() 内部有
    start() 兜底，那条路会绕过 --no-frame-producer，让开关失效）。
    """
    if args.vision_observer and not args.frame_producer:
        raise RuntimeError(
            '视觉观测器必须有帧生产者：观测器每轮从帧环取帧，没有生产者就没有帧。'
            '要么去掉 --no-frame-producer，要么同时加 --no-vision-observer'
        )
    if not args.receiver and args.mode == 'cloud' and not (
        args.frame_producer or args.vision_observer or args.status_observer
        or args.motion_status
    ):
        raise RuntimeError(
            '--no-receiver 且四个组件全关（status/motion/帧生产者/视觉观测）：没有任何事情要做，'
            '去掉 --no-receiver 或至少开一个组件'
        )


def main():
    parser = _build_parser()
    # 入口参数（--user-prompt 等）不在本脚本声明，用 parse_known_args 收剩下来的
    args, entry_argv = parser.parse_known_args()

    _install_sigterm_handler()
    _say(f'仓库根目录 {_ROOT}')
    try:
        _validate_combo(args)
        if args.mode == 'cloud':
            return run_cloud(args, entry_argv)
        return run_local(args, entry_argv)
    except KeyboardInterrupt:
        _say('收到中断，已退出')
        return 130
    except RuntimeError as exc:
        _say(f'❌ {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
