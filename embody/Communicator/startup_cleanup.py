#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
启动前清理 —— 保证**同时只有一套**智能体系统在跑，且启动时本体是静止的。

由统一启动入口 start_agent.py 在「导入主循环入口之后、起组件之前」调用，也可单独当
自检工具跑（`python3 Communicator/startup_cleanup.py --inspect`，只查不杀）。

职责与边界
----------
谁起的谁收：

  - 本模块管**别的终端 / 别的入口起的**那套（只能按 pid 收）：判「有没有另一套 agent
    在跑」、`--force` 时收掉它的整棵进程树、回收残留组件与技能残骸、启动前把本体停下来。
  - start_agent.py 保留「按开关起组件 + 收它自己 Popen 出来的子进程（有句柄、能拿退出
    码）+ 收自己的进程内线程」——那些是本进程范围内的活，不需要按 pid 去找。

「任务终止 / 退出清理」走同目录的 task_exit.py（exit_tasks），那是**运行中**的一条线；
本模块是**启动前**的一条线。两条线都收口在 Communicator/ 下，但不要互相调用。

什么算「另一套系统」：主循环入口
------------------------------
    start_agent.py / Communicator/receiver.py

**必须带上 start_agent.py**：cloud 模式下 receiver 是被 start_agent 导入后**在同一进程里**
调 main 的，于是那套系统的根进程命令行是 `python3 start_agent.py cloud`，
**根本不含 receiver.py**——只扫 receiver 会漏掉最常见的重复启动方式。
（本模块的前身就长这个毛病：它只扫 receiver 那一个入口。）
start_agent.py 一律算主循环，含 `--no-receiver`（只跑组件也是长驻的一套）。

组件（status_observer / vision_observer / frame_producer / tts_service）**不算另一套系统**：它们只是
上一轮留下的残骸，无条件收掉后继续启动。理由是它们写的都是全仓唯一的产出——帧锁
（FRAME_PRODUCER_LOCK_FILE）与两个状态 yaml——两个写手并存本身就不是合法状态。
技能脚本的孤儿残骸同理（谁都没爹了，只能按 cmdline 认）。

为什么扫 /proc 而不是 pgrep
--------------------------
pgrep 只认一种写法（相对或绝对），漏一种就漏掉一套系统；它还会命中「命令行里含该路径」
的非 agent 进程（正在编辑 receiver.py 的 vim、grep、tail -f 日志），而我们会对命中的 pid
发 SIGKILL。所以逐进程读 /proc，并且：

  - `argv[0]` 的 basename 必须以 python 开头（只有解释器起的才算；技能兜底那条放宽到
    python/bash/sh）；
  - 路径匹配双锚定：**绝对路径**直配，**相对路径**要求该进程 `cwd` 就在本仓库根
    （这样既认得出两种起法，又不会误伤别的部署树，例如
    /root/cmcc_harness_agent_receiver 下那份 receiver）。

为什么不能按进程组收
------------------
本树里所有进程都与父同组（曾经自起进程组的只有工具层的 ASR 调用，它要超时连
asr_monitor.py 一起收，那段已随工具层不再拉 ASR 一并删了），而入口的进程组由**起它的
那个终端或脚本**决定 —— 对它 killpg 会连带打到用户的 shell 和同终端里别的命令。
所以只能按 pid 逐个收，并靠 ppid 链把子孙找出来。

为什么必须靠 ppid 链
------------------
用 Popen 自起的子孙，命令行写的是**它们自己的**路径（如 status_observer 组件、技能脚本），
不是入口的路径 —— 按入口路径扫只能认出根进程，收树必须从入口 pid 沿 ppid 链枚举子孙。
（旧代还有一条更硬的例子：云端「继续任务」是 `multiprocessing spawn` 起的子进程，命令行是
`python3 -c "from multiprocessing.spawn import spawn_main..." --multiprocessing-fork`，
根本不含任何文件路径；那条路已随 receiver 改桥去掉。）

线程跨进程杀不掉
--------------
`vision-observer` / `robotdog-frame-producer` / `motion-status` / `motion-reader`（在起组件的
那一个进程内）以及角色侧工具进程里的 `bull-chase-ros`，全部是 daemon 线程（receiver 改桥后
不再长线程：旧代的 `task-exec-*` / `task-resume-watcher` 已随任务池一起删了）。所以
「清理所有线程」= 收掉承载它们的进程，没有第二条路。

停运动的口径
----------
只走 Communicator/task_exit.py 的 stop_all_motion() 一个入口 → Motion_Manager 的
`stop_all()`：move 停止指令 + 导航 cancel + VLN 进程清理，**绝不含急停**（急停只在
`stop(emergency=True)` 那条路径上；退出路径都不急停，启动路径更不该）。

  - 顺序：**先停运动，再收进程**。`motion.sh move` 这类指令是发给本体执行的，控制系统
    死掉不会让本体停下来——反过来做就是「杀完指挥者，本体照旧在走」。
    stop_all_motion 由 MotionManager 直接向服务端下发，不依赖那些进程还活着。
  - 判据不用 motion_status.snapshot()：它在 start_agent 进程里必然是 None（写 yaml 的线程
    还没起），而它的 idle 判定依赖 MotionManager 的**进程内记录**，换个进程读就失真。
    能用的是无状态的外部通路：runtime_files/motion_status.yaml（只当现场证据）、
    plugin_tools/nav_tools.navigation_status()（HTTP）、check_action_state.sh（读一帧就退）。
  - 停不干净**只告警、不拒绝启动**：动作类没有非急停停止接口（正在跳舞时停不下来），
    为这个拦住启动得不偿失。

明确收不到什么（写在这里，不只在 README 里）
----------------------------------------
  - **孤儿 / ppid=1 的残骸**：父被 SIGKILL 后重挂到 init 的孙子（典型是垂死 agent 在
    finally 里新起的那些）。轮 2 用幸存者当根重拉一遍能捞回大多数（父还活着的那部分），
    父链一断就再也无法归属。剩余风险：还占着 ASR 会话的 asr_monitor.py、还在抓图的
    capture_direct.py。
  - **别的部署树**（/root/cmcc_harness_agent_receiver、/root/RobotAlgorithmGen1 的节点）：
    路径 + cwd 双锚定就是为了不误伤。代价是那棵树占着相机/帧锁时本进程会启动失败，
    只能人工处理。
  - **harness 角色系统那一棵**（缺省 interactor / orchestrator / supervisor，可加 planner；外加
    覆盖全部角色的 role-watch）：不在本仓、也不在 start_agent 树下，本模块与 `start_agent.py` 的
    `--force` 都不收。走 `starter.sh --force` 时那一棵由 starter.sh 自己按角色标记收
    （scripts/lib-starter.sh 的 starter_stop_running_role），两边各收各的那一半。
  - **systemd 与共享设施**：robot-dog-motion.service 的 server、导航 app、ros2 daemon、
    ROS 域内其它节点——一律不碰。
  - **云端活跃任务不会复位**：强收时旧 receiver 的 finally 补报可能来不及（SIGKILL 必然
    来不及），云端可能挂着一个活跃任务直到超时，需人工取消或接受这个窗口。

不探 FRAME_PRODUCER_LOCK_FILE（后人一定会想加，所以写明为什么不加）
--------------------------------------------------------------
  1. flock 的所有者 fd 随进程退出由内核释放，「树上的进程都不在了」与「锁已释放」是
     同一件事——再确认一次就是同一判据的第二份实现，而那份实现已经存在于
     Observer/status_observer.py 的 probe_camera（那里还记着「绝不能自己抢一次锁来
     探测」的教训）。
  2. 权威判据由消费者在需要时给出：紧随其后的取帧服务拉起（start_agent._start_frame_service）
     连不上 socket 时会当场报错，而抢锁失败的持有者 pid 由取帧服务自己写进
     runtime_files/logs/frame_producer.log（frame_producer._acquire_process_lock）。
  3. 锁文件本身 fail-open（打不开就放行），不存在陈旧锁。

自检（不碰硬件）
--------------
    python3 Communicator/startup_cleanup.py --inspect                    # 只查不杀
    python3 Communicator/startup_cleanup.py --cleanup --no-motion        # 无机器人时测收树链路
    python3 Communicator/startup_cleanup.py --cleanup --force            # 真清理

所有对外函数只抛 RuntimeError（start_agent.main 会把 RuntimeError 转成 `❌ …` + 退出码 1）；
/proc 读失败、YAML 解析失败、HTTP 失败一律内部吞掉降级——启动前清理不该因为读不到一份
证据就把启动拦下来。
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

# 确保项目根目录在 Python 搜索路径中（用于导入 configs 包）
_ROOT = Path(__file__).parent.parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import (
    MOTION_STOP_ALL_WAIT,
    MOVE_STATE_TOOL_TIMEOUT,
    PROC_KILL_WAIT_SEC,
    PROC_TERM_WAIT_SEC,
)
from configs.paths import (
    FRAME_PRODUCER_MODULE,
    MOTION_STATE_SCRIPT,
    RECEIVER_MODULE,
    ROOT_DIR,
    RUNTIME_MOTION_STATUS,
    START_AGENT_MODULE,
    STATUS_OBSERVER_MODULE,
    TTS_SERVICE_MODULE,
    VISION_OBSERVER_MODULE,
)

# 主循环入口：命中即「另一套系统在跑」（见文件头，start_agent 那一条不能少）。
_MAIN_ENTRIES = (
    (START_AGENT_MODULE, 'start_agent'),
    (RECEIVER_MODULE, 'receiver'),
)

# 并列组件：命中只当残骸，无条件收掉（不构成「另一套系统」）
_COMPONENT_ENTRIES = (
    (STATUS_OBSERVER_MODULE, 'status_observer'),
    (VISION_OBSERVER_MODULE, 'vision_observer'),
    (FRAME_PRODUCER_MODULE, 'frame_producer'),
    # 常驻 TTS 服务：正常退出由 start_agent._stop_children 收（SIGTERM → 服务 unlink socket
    # + 放锁），被 kill -9 才需要这一步。收它会连带释放单实例锁，陈 socket 由下一个服务自己删。
    (TTS_SERVICE_MODULE, 'tts_service'),
)

# 技能脚本兜底的锚点（决策 6：收孤儿残骸，但不按 cwd 判——避免误杀用户手动跑的 tests/ 工具）
_SKILLS_ABS = str(ROOT_DIR / 'skills')
_SKILLS_REL = 'skills'
# 能起技能脚本的解释器（argv[0] 的 basename 前缀）
_SHELLISH = ('python', 'bash', 'sh')

# 证据/复核的轮询步长（秒）：实现细节，不进 configs
_POLL_STEP_SEC = 0.5


# =============================================================================
# 日志出口
# =============================================================================

def _log(logger: Optional[Any], msg: str, level: str = 'info') -> None:
    """写一行日志：可调用对象（start_agent 直接传它的 _say，保住 [start_agent] 前缀）
    → 有 .info/.error 的 logger 对象 → print。"""
    if logger is None:
        print(msg)
        return
    if callable(logger):
        logger(msg)
        return
    getattr(logger, level, print)(msg)


# =============================================================================
# 数据结构
# =============================================================================

@dataclass(frozen=True)
class ProcInfo:
    """一个进程的快照记录（argv 是收集时读到的命令行，用于 pid 复用防护）。"""

    pid: int
    ppid: int
    name: str                  # 显示名，如 'start_agent cloud' / 'status_observer'
    argv: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Inventory:
    """一次全仓进程盘点：主循环（另起的那套）+ 残留组件/技能残骸 + 主循环的整棵进程树。"""

    main: Tuple[ProcInfo, ...] = ()
    components: Tuple[ProcInfo, ...] = ()
    tree: Tuple[ProcInfo, ...] = ()

    @property
    def has_other_system(self) -> bool:
        """是否有另一套 agent 主循环在跑（组件不算，它们只是残骸）。"""
        return bool(self.main)

    def describe(self) -> str:
        """多行可读摘要（--inspect 与启动日志共用）。"""
        lines = [
            f'主循环: {_fmt_list(self.main) or "无"}',
            f'残留组件/技能残骸: {_fmt_list(self.components) or "无"}',
        ]
        if self.tree:
            lines.append(f'主循环进程树（{len(self.tree)} 个）:')
            lines += [f'    - {p.name}(pid={p.pid}, ppid={p.ppid})' for p in self.tree]
        return '\n'.join(lines)


@dataclass(frozen=True)
class CleanupReport:
    """一次启动前清理的结果（给调用方记一笔/上报用）。"""

    killed: Tuple[ProcInfo, ...] = ()          # 收掉的主循环整棵树
    components: Tuple[ProcInfo, ...] = ()      # 收掉的残留组件/技能残骸
    motion_summary: str = ''                   # 停运动那行摘要
    elapsed_sec: float = 0.0


def _fmt_list(procs: Iterable[ProcInfo]) -> str:
    """把进程列表压成一行：'receiver(pid=1)、bash capture.sh(pid=2)'。"""
    return '、'.join(f'{p.name}(pid={p.pid})' for p in procs)


# =============================================================================
# /proc 快照（只读）
# =============================================================================

def _read_bytes(path: str) -> Optional[bytes]:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _cmdline(pid: int) -> Optional[Tuple[str, ...]]:
    """读一个进程的 cmdline（读不到返回 None）。

    **不因为读不到就把进程从快照里丢掉**：它可能是 ppid 链上的中间节点
    （入口 → 中间脚本 → 技能进程），漏掉就把这棵树截断了。只是它永远不会被
    「入口命中」，也不会被发信号（见 _still_same）。
    """
    raw = _read_bytes(f'/proc/{pid}/cmdline')
    if raw is None:
        return None
    return tuple(a.decode('utf-8', 'replace') for a in raw.split(b'\0') if a)


def _stat_of(pid: int) -> Optional[Tuple[str, int]]:
    """读 /proc/<pid>/stat 的 (state, ppid)。

    comm 字段是括号包起来的进程名，里面**可能有空格和括号**，所以只能以最后一个 ')'
    切分（不能 split() 整行）——这正是 comm 允许的字符集决定的。
    """
    raw = _read_bytes(f'/proc/{pid}/stat')
    if raw is None:
        return None
    text = raw.decode('utf-8', 'replace')
    idx = text.rfind(')')
    if idx == -1:
        return None
    parts = text[idx + 1:].split()
    if len(parts) < 2:
        return None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return None


def _snapshot() -> Dict[int, ProcInfo]:
    """一次遍历 /proc，返回 pid → ProcInfo（name 先按命令行压个短标签，命中入口的由调用方覆盖）。

    僵尸（Z）不进快照：它已经退出了，只是父进程还没回收，既不该被发信号，也不会
    再有活着的子进程（子进程早已被 reparent 到 init）。
    """
    snap: Dict[int, ProcInfo] = {}
    try:
        entries = os.listdir('/proc')
    except OSError:
        return snap
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        stat = _stat_of(pid)
        if stat is None or stat[0] == 'Z':
            continue          # 进程刚退出 / 已是僵尸，跳过
        argv = _cmdline(pid) or ()
        snap[pid] = ProcInfo(pid=pid, ppid=stat[1], argv=argv,
                             name=_short_argv(argv) if argv else '(命令行不可读)')
    return snap


def _alive(pid: int) -> bool:
    """进程是否还活着。

    不用 os.kill(pid, 0)：僵尸进程（已退出、父进程还没回收）对 kill 0 仍然「存在」，
    会让等待白等到超时、再误报「收不掉，请人工处理」，把其实已经退掉的进程变成启动阻塞。
    所以直接读 /proc：状态 Z 算已死。
    """
    stat = _stat_of(pid)
    return stat is not None and stat[0] != 'Z'


def _still_same(info: ProcInfo) -> bool:
    """pid 复用防护：命令行与收集时一致，才算还是那个进程。

    读不到命令行（进程已退出/权限不足）一律当「不是它」——**宁漏杀不误杀**。
    """
    cur = _cmdline(info.pid)
    return cur is not None and bool(info.argv) and cur == info.argv


def _wait_gone(pids: Iterable[int], timeout: float) -> Tuple[int, ...]:
    """等到这些 pid 都不在了；返回超时后仍活着的（按 pid 排序）。"""
    wanted = list(pids)
    deadline = time.time() + timeout
    while True:
        left = tuple(p for p in wanted if _alive(p))
        if not left or time.time() >= deadline:
            return left
        time.sleep(0.2)


# =============================================================================
# 进程匹配（双锚定：入口路径 + 进程 cwd）
# =============================================================================

def _cwd_getter(pid: int) -> Callable[[], str]:
    """迟到的 cwd 读取器（只在相对路径那一分支才真正 readlink，且只读一次）。"""
    cache: Dict[str, str] = {}

    def get() -> str:
        if 'v' not in cache:
            try:
                cache['v'] = os.readlink(f'/proc/{pid}/cwd')
            except OSError:
                cache['v'] = ''
        return cache['v']

    return get


def _entry_hit(argv: Tuple[str, ...], cwd: Callable[[], str],
               entries: Tuple[Tuple[Path, str], ...]) -> Optional[Tuple[str, int]]:
    """argv 命中了 entries 里的哪个入口 → (名字, 该参数下标)；未命中返回 None。

    匹配规则（原样承自 start_agent._pids_of，别简化）：解释器过滤 → 绝对路径直配 /
    相对路径 + `cwd == 仓库根` 二次确认。
    """
    if not argv or not os.path.basename(argv[0]).startswith('python'):
        return None
    for path, name in entries:
        target = str(path)
        try:
            rel = str(path.relative_to(ROOT_DIR))
        except ValueError:
            continue
        for idx, arg in enumerate(argv):
            if arg == target:
                return name, idx
            if arg == rel or arg.endswith('/' + rel):
                if cwd() == str(ROOT_DIR):
                    return name, idx
    return None


def _skill_script(argv: Tuple[str, ...], cwd: Callable[[], str]) -> Optional[str]:
    """cmdline 里出现本仓 skills/ 下脚本路径的解释器进程（孤儿残骸兜底）。

    只按**命令行**认，不按 cwd 判：用户手动跑的 tests/ 工具与后台遗留的
    capture_direct.py / asr_monitor.py 的 cwd 可能都是仓库根，按 cwd 会一起误杀。
    """
    if not argv or not os.path.basename(argv[0]).startswith(_SHELLISH):
        return None
    for arg in argv[1:]:
        a = arg[2:] if arg.startswith('./') else arg
        if a == _SKILLS_ABS or a.startswith(_SKILLS_ABS + '/'):
            return arg
        if (a == _SKILLS_REL or a.startswith(_SKILLS_REL + '/')) and cwd() == str(ROOT_DIR):
            return arg
    return None


def _short_argv(argv: Tuple[str, ...]) -> str:
    """把命令行压成一行短标签（树里非目标进程的显示名）。

    只认**像脚本路径**的参数：带空白/引号的（`python3 -c '...'` 的那段代码）一律跳过 ——
    否则标签会变成半截代码，还是多行的。
    """
    if not argv:
        return '(命令行不可读)'
    label = os.path.basename(argv[0])
    for arg in argv[1:]:
        if arg.startswith('-') or any(c.isspace() or c in '"\'`' for c in arg):
            continue
        base = os.path.basename(arg)
        return f'{label} {base}' if ('/' in arg or base.endswith(('.py', '.sh'))) else label
    return label


def _excluded_pids() -> set:
    """自身 + 自己的祖先链（沿 ppid 上行，遇 0/1 或成环即停）。

    **必须排除祖先**：收树是按「入口路径命中 + ppid 链」扫的，祖先也在扫描范围内——任何一层祖先
    命中入口路径（例如它就是本入口的另一个实例）都不该被算成「另一套系统」，漏掉它的话 `--force`
    收树就是把自己（连带自己的终端）收掉。
    """
    out = {os.getpid()}
    pid = os.getpid()
    for _ in range(64):
        stat = _stat_of(pid)
        ppid = stat[1] if stat else 0
        if ppid <= 1 or ppid in out:
            break
        out.add(ppid)
        pid = ppid
    return out


# =============================================================================
# 纯查询：盘点全仓进程
# =============================================================================

def find_descendants(roots: Iterable[ProcInfo],
                     snapshot: Optional[Dict[int, ProcInfo]] = None) -> Tuple[ProcInfo, ...]:
    """返回这批进程**自身 + 全部子孙**（BFS，按 ppid 链）。

    只从**已确认的入口**当根，永不从 pid 1 往下走（init 的子孙是全机进程）。
    """
    snap = snapshot if snapshot is not None else _snapshot()
    children: Dict[int, list] = {}
    for proc in snap.values():
        children.setdefault(proc.ppid, []).append(proc.pid)

    seen: set = set()
    queue: deque = deque()
    for root in roots:
        if root.pid not in seen:
            seen.add(root.pid)
            queue.append(root.pid)

    out = []
    while queue:
        pid = queue.popleft()
        info = snap.get(pid)
        if info is None:
            continue          # 中间节点已退出：链到此为止（它的子进程已成孤儿，收不到）
        out.append(info)
        for child in children.get(pid, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return tuple(out)


def find_agent_processes() -> Inventory:
    """盘点本仓库的 agent 进程（**只读 /proc**：不发信号、不写文件）。

    返回：
      main       —— 另一套 agent 主循环（start_agent.py / receiver.py）
      components —— 残留组件 + 技能脚本孤儿残骸（不在 main 的树里）
      tree       —— main 那套的整棵进程树（含全部子孙；收树就收它）
    """
    snap = _snapshot()
    excluded = _excluded_pids()

    main: list = []
    comps: list = []
    for pid, info in sorted(snap.items()):
        if pid in excluded or not info.argv:
            continue
        cwd = _cwd_getter(pid)

        hit = _entry_hit(info.argv, cwd, _MAIN_ENTRIES)
        if hit:
            name, idx = hit
            if name == 'start_agent':
                # 把模式名带上（start_agent cloud），报错时一眼看出是哪套
                mode = next((a for a in info.argv[idx + 1:] if not a.startswith('-')), '')
                name = f'start_agent {mode}'.rstrip()
            main.append(ProcInfo(pid, info.ppid, name, info.argv))
            continue

        hit = _entry_hit(info.argv, cwd, _COMPONENT_ENTRIES)
        if hit:
            comps.append(ProcInfo(pid, info.ppid, hit[0], info.argv))
            continue

        if _skill_script(info.argv, cwd):
            comps.append(ProcInfo(pid, info.ppid, _short_argv(info.argv), info.argv))

    tree = find_descendants(main, snapshot=snap)
    tree_pids = {p.pid for p in tree}
    components = tuple(p for p in comps if p.pid not in tree_pids)
    return Inventory(main=tuple(main), components=components, tree=tree)


# =============================================================================
# 拒绝重复启动（零副作用）
# =============================================================================

def _main_detail(inv: Inventory) -> str:
    return _fmt_list(inv.main)


def _refuse_msg(detail: str) -> str:
    return (f'本仓库已有 agent 在跑（{detail}）：两个 agent 会重复认领云端任务、'
            f'抢占帧环。先停掉它，或加 --force 强行继续')


def refuse_if_running(force: bool, logger: Optional[Any] = None) -> None:
    """本仓库已有 agent 主循环在跑时拒绝启动（`--force` 可越过）。

    **零副作用**：只读 /proc，不发信号、不写文件、不导入入口模块。所以它可以放在
    start_agent 流程的最前面（在碰任何硬件之前 fail fast）；真正动手的清理在
    cleanup()，那要等入口模块导入成功之后才做。
    """
    inv = find_agent_processes()
    if not inv.main:
        return
    detail = _main_detail(inv)
    if force:
        _log(logger, f'⚠️ 本仓库已有 agent 在跑（{detail}），--force：稍后收掉它的整棵进程树'
                     f'（含组件子进程与技能脚本）再启动')
        return
    raise RuntimeError(_refuse_msg(detail))


# =============================================================================
# 停运动（需求 2：保证智能体启动时是静止的）
# =============================================================================

def _motion_evidence() -> str:
    """读 motion_status.yaml 的一行现场证据（读不到就返回一句说明，不拦启动）。

    它只是**证据**：说明「为什么本体可能没停」，以及留给人工排查。判据不靠它——
    这份文件是上一个 agent 进程的线程写的，可能已经陈旧。
    """
    path = Path(RUNTIME_MOTION_STATUS)
    try:
        import yaml            # 延迟到这里：缺 PyYAML 也不该拦下启动前的清理
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        age = time.time() - path.stat().st_mtime
        motion = data.get('motion') or {}
        last = data.get('last_motion') or {}
        return (f'idle={motion.get("idle")}、action_state={motion.get("action_state_name")}、'
                f'最近一笔={last.get("action_source")}({last.get("params")})、'
                f'文件 {age:.0f}s 前更新')
    except Exception as exc:   # noqa: BLE001 — 证据读不到只降级
        return f'（读不到 {path.name}: {exc}）'


def _nav_status() -> str:
    """查导航状态（HTTP，不需要 ROS）；查不到返回空串。

    nav_tools 的三态（见它模块文档）：""=没应答、str=应答不是 JSON、dict=解析后的应答。
    """
    try:
        from configs.configs import NAVIGATION_CONTROL_TIMEOUT
        from plugin_tools.nav_tools import navigation_status
        ret = navigation_status(NAVIGATION_CONTROL_TIMEOUT)
    except Exception:          # noqa: BLE001 — 导航服务没起/包缺失都只当查不到
        return ''
    if not isinstance(ret, dict) or ret.get('code') != 0:
        return ''
    data = ret.get('data')
    return str(data.get('status') or '') if isinstance(data, dict) else ''


def _body_state() -> Tuple[int, str]:
    """读一帧本体动作态：返回 (退出码, 脚本 stderr 原文最后一行)。

    check_action_state.sh 非等待模式**读一帧即退**：0=静止站立、1=非站立、
    2=窗口内没收到状态、3=环境错误。**不解析状态数字**——脚本只打印 _describe(state)
    的文案，解析它既脆弱又没必要：退出码 + 转发原文已经够读。
    """
    try:
        proc = subprocess.run(
            ['bash', str(MOTION_STATE_SCRIPT), '--timeout', f'{MOTION_STOP_ALL_WAIT:g}'],
            capture_output=True, text=True, timeout=MOVE_STATE_TOOL_TIMEOUT,
        )
    except Exception as exc:   # noqa: BLE001 — 脚本起不来/超时都只当读不到
        return -1, f'（调用 {Path(MOTION_STATE_SCRIPT).name} 失败: {exc}）'
    lines = [ln for ln in (proc.stderr or '').splitlines() if ln.strip()]
    return proc.returncode, (lines[-1] if lines else '')


def ensure_static(logger: Optional[Any] = None) -> str:
    """把本体停下来，保证启动时是静止的；返回一行摘要（**一律不拦启动**）。

    三步：读现场证据（只读）→ 经 task_exit.stop_all_motion 下发停止 → 复核。
    停在**收进程之前**：指令是发给本体执行的，控制系统死掉不会让本体停下来。
    复核不通过只打 ⚠️ 继续（动作类没有非急停停止接口，正在跳舞就是停不下来）。
    """
    _log(logger, f'运动状态证据: {_motion_evidence()}')

    nav_before = _nav_status()
    if nav_before == 'RUNNING':
        _log(logger, '导航正在运行，将一并取消')

    # 唯一入口：task_exit.stop_all_motion → MotionManager.stop_all（**不含急停**）
    try:
        from Communicator.task_exit import stop_all_motion
    except Exception as exc:   # noqa: BLE001 — 导入不了就没法停，如实说明但不当成拦启动的理由
        summary = f'停止运动不可用（task_exit 导入失败: {exc}）'
        _log(logger, f'⚠️ {summary}', 'error')
        return f'{summary}；复核：跳过'
    summary = stop_all_motion('start_agent 启动前停止运动', logger)

    # ---- 复核：导航 ----
    nav_after = _nav_status()
    if nav_after == 'RUNNING':
        deadline = time.time() + MOTION_STOP_ALL_WAIT
        while nav_after == 'RUNNING' and time.time() < deadline:
            time.sleep(_POLL_STEP_SEC)
            nav_after = _nav_status()
    nav_note = ('导航已停' if nav_after != 'RUNNING' else
                '⚠️ 导航仍报 RUNNING（取消未生效）') if nav_after else '导航状态查不到'

    # ---- 复核：本体动作态 ----
    code, raw = _body_state()
    if code == 0:
        body_note = '✅ 本体静止站立'
    elif code == 1:
        body_note = f'⚠️ 本体不在静止状态：{raw}'
    else:
        # 2=窗口内没收到状态消息、3=环境错误（没 source ROS / 没有本体）
        body_note = f'本体状态未知，按「不是静止」对待但不拦启动：{raw}'

    out = f'{summary} ｜ 复核：{nav_note}；{body_note}'
    _log(logger, f'停止后复核：{nav_note}；{body_note}')
    return out


# =============================================================================
# 收进程树
# =============================================================================

def _signal_all(procs: Dict[int, ProcInfo], sig: int, logger: Optional[Any] = None) -> None:
    """给这批进程发同一个信号（发前做 pid 复用防护）。"""
    for pid in sorted(procs):
        info = procs[pid]
        if not _alive(pid):
            continue
        if not _still_same(info):
            _log(logger, f'⚠️ 跳过 pid={pid}（{info.name}）：命令行已变或读不到，'
                         f'可能已被复用 —— 宁漏杀不误杀')
            continue
        try:
            os.kill(pid, sig)
        except OSError as exc:
            _log(logger, f'⚠️ 杀不动 pid={pid}（{info.name}）：{exc}；'
                         f'若是别的用户起的旧实例，需 sudo 处理', 'error')


def _kill_tree(targets: Iterable[ProcInfo],
               logger: Optional[Any] = None) -> Tuple[ProcInfo, ...]:
    """收掉这批进程**及其子孙**：SIGTERM 广播 → 等 → 重拉子孙再 SIGTERM → SIGKILL → 复核。

    为什么是**一次性广播**而不是「根先/子孙先」：本仓没有 setsid/killpg 那种收树手段
    （唯一自起过进程组的是工具层的 ASR 子进程，收的是它自己那个组，不是本要收的这棵树，
    那处已随工具层不再拉 ASR 一并删了），
    而根自己收摊时只认它 Popen 的句柄——技能脚本、capture.sh、asr_monitor.py、motion.sh
    都不在其内。一次性发完，唯一实质好处是**垂死的 supervisor 没有时间窗去重新拉起
    刚被收掉的子进程**。

    为什么轮 2 要重拉一遍子孙：父进程的 finally 会新起 motion.sh stop / pkill /
    check_action_state.sh，这些不在轮 1 名单里，父一死就成孤儿。

    收不干净 → RuntimeError（宁可拒绝启动，也不两套并跑）。
    """
    targets = tuple(targets)
    if not targets:
        return ()

    signalled: Dict[int, ProcInfo] = {p.pid: p for p in targets}
    _signal_all(signalled, signal.SIGTERM, logger)
    _wait_gone(signalled, PROC_TERM_WAIT_SEC)

    survivors = [p for pid, p in signalled.items() if _alive(pid)]
    if survivors:
        for proc in find_descendants(survivors):
            signalled.setdefault(proc.pid, proc)
        alive = {pid: p for pid, p in signalled.items() if _alive(pid)}
        _signal_all(alive, signal.SIGTERM, logger)
        _wait_gone(signalled, PROC_TERM_WAIT_SEC)

    left = {pid: p for pid, p in signalled.items() if _alive(pid)}
    if left:
        _log(logger, f'{len(left)} 个进程未在 {PROC_TERM_WAIT_SEC:g}s 内退出，强杀：'
                     f'{_fmt_list(left.values())}')
        _signal_all(left, signal.SIGKILL, logger)
        _wait_gone(left, PROC_KILL_WAIT_SEC)

    left = {pid: p for pid, p in signalled.items() if _alive(pid)}
    if left:
        raise RuntimeError(
            f'这些进程收不掉（{_fmt_list(left.values())}）：请手动 kill -9 后重试。'
            f'收不干净不启动 —— 两套 agent 会重复认领云端任务、抢占帧环'
        )
    return tuple(signalled.values())


# =============================================================================
# 总编排
# =============================================================================

def cleanup(force: bool = False, logger: Optional[Any] = None, *,
            stop_motion: bool = True) -> CleanupReport:
    """启动前清理：停运动 → 收掉旧那套的整棵进程树 → 收残留组件 → 复核。

    调用方应先过 refuse_if_running()（本函数开头也会再判一次，保证 --force 语义不被绕过）。

    Args:
        force:       False 时若发现主循环在跑就抛 RuntimeError；True 时收掉它。
        stop_motion: 是否执行停运动（默认做；--no-motion 只为在没有机器人的机器上
                     验证收树链路而存在）。

    幂等：第二次调用时 main/components 都空，只剩两条幂等的停止指令。
    """
    started = time.time()
    inv = find_agent_processes()
    if inv.main:
        if not force:
            raise RuntimeError(_refuse_msg(_main_detail(inv)))
        _log(logger, f'--force：收掉旧那套 agent（{_main_detail(inv)}）')
    else:
        _log(logger, '没有检测到别的 agent 主循环在跑')

    motion_summary = ensure_static(logger) if stop_motion else '（按 --no-motion 跳过停运动）'

    killed: Dict[int, ProcInfo] = {}
    comps: Dict[int, ProcInfo] = {}
    for attempt in (1, 2):
        inv = find_agent_processes()
        # 轮 2 是重扫：收树过程中垂死的进程可能又拉起一两个（技能脚本的重试周期）
        if not inv.tree and not inv.components:
            break
        if inv.components:
            _log(logger, f'收掉残留组件/技能残骸：{_fmt_list(inv.components)}')
        for proc in _kill_tree(inv.tree, logger):
            killed[proc.pid] = proc
        for proc in _kill_tree(inv.components, logger):
            comps[proc.pid] = proc
        if attempt == 1 and (inv.tree or inv.components):
            _log(logger, '清理完成，重扫一遍确认没有新冒出来的进程')

    final = find_agent_processes()
    if final.tree or final.components:
        raise RuntimeError(
            f'清理后仍有本仓库进程在跑（{_fmt_list(final.tree + final.components)}）：'
            f'请手动 kill -9 后重试'
        )

    elapsed = time.time() - started
    _log(logger, f'启动前清理完成：收掉 {len(killed)} 个进程（残留组件 {len(comps)} 个），'
                 f'耗时 {elapsed:.1f}s')
    return CleanupReport(killed=tuple(killed.values()), components=tuple(comps.values()),
                         motion_summary=motion_summary, elapsed_sec=elapsed)


# =============================================================================
# CLI 自检入口（`--inspect` 只查不杀，是删掉 start_agent --dry-run 之后的替代手段）
# =============================================================================

def _cli(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='startup_cleanup.py',
        description='启动前清理：保证同时只有一套 agent 在跑，且启动时本体是静止的',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            '示例:\n'
            '  python3 Communicator/startup_cleanup.py --inspect                  # 只查不杀\n'
            '  python3 Communicator/startup_cleanup.py --cleanup --no-motion      # 无机器人时测收树\n'
            '  python3 Communicator/startup_cleanup.py --cleanup --force          # 真清理\n'
        ),
    )
    parser.add_argument('--inspect', action='store_true',
                        help='只盘点进程（默认动作），不发信号、不碰机器人')
    parser.add_argument('--cleanup', action='store_true',
                        help='执行清理：停运动 → 收掉旧那套的整棵树 → 收残留组件 → 复核')
    parser.add_argument('--force', action='store_true',
                        help='--cleanup 时收掉正在跑的主循环（默认拒绝）')
    parser.add_argument('--no-motion', dest='motion', action='store_false',
                        help='--cleanup 时跳过停运动（没有机器人/ROS 的机器上验证收树链路用）')
    args = parser.parse_args(argv)

    if not args.cleanup:
        print(find_agent_processes().describe())
        print('（--inspect 只查不杀；要执行清理加 --cleanup）')
        return 0

    try:
        report = cleanup(force=args.force, logger=print, stop_motion=args.motion)
    except RuntimeError as exc:
        print(f'❌ {exc}')
        return 1
    print(f'✅ 已收掉 {len(report.killed)} 个进程（残留组件 {len(report.components)} 个），'
          f'耗时 {report.elapsed_sec:.1f}s')
    return 0


if __name__ == '__main__':
    sys.exit(_cli())
