#!/usr/bin/env python3
"""
统一运动管理 —— action / move / navigation 三个工具的下发、占用、结束判定都走这里。

为什么要有它：三个工具原先各管各的——`action` 发完就返回「执行完成」（其实动作才刚开始），
`move` 靠 client 阻塞到自己结束，`waypoint_nav` 靠轮询 HTTP；「能不能动」的前置检查只在
move 里做。没有一个地方能回答「此刻本体被谁占用」。这里把这三件事收敛到一处：

  1. 前置状态检查（三者共用）：静止站立直接动；不是就先 standUp 再动；读不到状态不下发。
  2. 结束判定：action / move 听 /catch_turtle/action_state **回到静止**（连续
     configs.MOTION_IDLE_HOLD_SEC 秒都落在目标集合里才算数，见 _wait_state），navigation
     走它自己的状态接口；等不到就是超时。
  3. 占用与排队：同一时刻只有一个来源能动（见下方表格）。
  4. 最近一次运动的记录（来源 + 参数 + 是否收到过停止信号）留在**进程内存**里，由
     Motion_Manager/motion_status.py 那个线程渲染成 motion_status.yaml 的
     last_motion.action_source / last_motion.params / last_motion.stop_received。
     （2026-09-20 前这份记录落盘成 motion_record.yaml、由 Observer/status_observer.py 渲染进
     robot_status.yaml 的 motion 段；记录改进程内存后那条路已取消，见 motion_record.py。）

调用方保留参数校验、白名单、裁剪与返回文案，下发与等待一律到这里。本树（embody）的调用方
只剩**启动前的停运动**：`Communicator/startup_cleanup.py` → `task_exit.stop_all_motion()` →
`stop_all()`（其他进程都不进 manager，本树也没有进程往 task_exit 的任务池里登记任务）。
**角色侧的工具层不在这里**——它经 skills/ 下的 motion.sh /
robot_action.sh 直接向机器人说话（`plugin_tools/tool_executor.py`），因此那一层的
action / gait 只拿到「指令已下发」，等状态与排队都够不着（见各自的 docstring）。
motion_status.py 是同进程的**读**方：经 motion_record 渲染 last_motion，不调用 manager。

占用规则（当前占用 → 新任务）：

  | 当前占用        | move / action                  | navigation                    |
  | 无              | 执行                           | 执行                          |
  | move / action   | 排队，等前面那单真实做完再下发 | 拒绝「本体正在进行 {source}」 |
  | navigation      | 拒绝「正在导航中」             | 停掉原导航，改发新地点        |

  stop() 是例外：直通，不排队、不等待，见 stop() 的说明。

并发：manager 是进程内单例，调用方是**谁就是谁那个进程**（本树 = 起组件的那个进程，见上）。
用一把 threading.Lock 串行化执行，阻塞拿锁即「排队」——不再引入 worker 线程，避免多一套
生命周期要在 Communicator/task_exit.py 里收。占用判定用的是进程内的实时状态
self._holder，不是那份运动记录；记录只给**同进程**的 motion_status 线程渲染用，允许它滞后
（本体是不是空闲看 /catch_turtle/action_state，不看那份记录——导航在飞是唯一例外，见
Motion_Manager/motion_record.py 的 resolve_occupancy）。

停止与退出：

  stop(source, emergency=False)   工具层用：**单个来源**直通停止（不排队、不等结束）。
  stop_all(reason)                退出路径用（Communicator/task_exit.py 的 exit_tasks
                                  → stop_all_motion）：一次停掉所有能停的运动。

  两者分工不能混：stop() 是「让这次运动停下来」，只有它带急停开关；stop_all() 是
  「任务终止 / 进程退出，把本体停下来」，**它连 emergency 参数都没有**，方法体里也
  走不到急停分支（急停命令只由 _emergency_stop() 拼，唯一调用点在 stop()）。

  退出时各来源各做什么：

  | 来源       | 停止手段                                              |
  | move       | MOTION_SCRIPT stop（服务端收到会清队列并唤醒阻塞中的 client，见下） |
  | navigation | 导航 cancel 接口 + pkill VLN 进程（后者见 _cancel_vln 的说明） |
  | action     | **没有非急停停止接口**，只记一笔，本体把该动作自己跑完（规格限制） |

  不归它管的三条（退出时也不额外兜）：斗牛 /bullfight_ctl（靠自己的 cancel_event 收尾）、
  环绕搜索与转向目标（直接 bash motion.sh，靠时长到达）、跟随 /follow_me/start。

  在飞的那单怎么尽快收手：每笔运动在**排队之前**领一个单调递增的世代号（_new_gen），
  stop_all() 把「已作废世代」的水位抬到当前已发放的号；此后那笔运动在每个检查点上
  （领号后、等状态前后、导航轮询每轮）都会发现自己作废而中止，finally 里的 _leave()
  随即释放运动锁——否则新任务的第一条 move 要排在旧运动后面，最坏 120s/300s。
  最长的那次阻塞是「等动作回到静止」，它按 MOTION_STATE_CHECK_HZ 采样、**每采一次查一遍
  中止**，所以最多滞后一个采样间隔就被收手（2026-10-08 之前它是一条现起的等待子进程，
  要靠 stop_all() terminate 它才醒得过来；那条路已删，见 _wait_state）。
"""

import sys
import time
import threading
import subprocess
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

# 仓库根（本文件 Motion_Manager/ 的上一级）入 sys.path，以便 import configs
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import (
    ACTION_CHANNEL_GAIT,
    ACTION_CHANNEL_SKILL,
    ACTION_STATES,
    ACTION_STATE_LYING,
    ACTION_STATE_STANDING,
    EMERGENCY_ACTION,
    IDLE_ACTION_STATES,
    MOTION_GAIT_TIMEOUT,
    MOTION_IDLE_HITS,
    MOTION_IDLE_HOLD_SEC,
    MOTION_LYING_ACTIONS,
    MOTION_MOVE_END_TIMEOUT,
    MOTION_MOVE_SCRIPT_TIMEOUT,
    MOTION_SCRIPT_TIMEOUT,
    MOTION_STAND_ACTIONS,
    MOTION_STATE_CHECK_HZ,
    MOTION_STOP_ALL_WAIT,
    MOTION_TOOL_TIMEOUT_SLACK,
    MOVE_STAND_TIMEOUT,
    MOVE_STATE_TOOL_TIMEOUT,
    NAVIGATION_POLL_INTERVAL,
    NAVIGATION_TIMEOUT,
    action_timeout,
)
from configs.paths import ACTION_SCRIPT, MOTION_SCRIPT, MOTION_STATE_SCRIPT, VLN_SCRIPT
from Motion_Manager import motion_status
# 占用来源名由 motion_record 定义（它按 MOTION_SOURCES 卡合法性），本文件取它的名字用，
# 不再另立一份同值常量——两份同值常量会让「加一个来源」变成两处要改。
from Motion_Manager.motion_record import (
    MOTION_ACTION,
    MOTION_MOVE,
    MOTION_NAVIGATION,
    begin_motion,
    note_stop_received,
)


class MotionAborted(Exception):
    """本笔运动在排队 / 下发 / 等待过程中被 stop_all() 作废（任务终止、进程退出）而中止。

    这不是错误，是「收到停止信号」的正常结局：run_* 把它收成一句返回文案（不往上抛），
    调用方（工具层）据此知道本次没做完。
    """


class MotionManager:
    """运动下发与占用的唯一入口。进程内单例，用 get_motion_manager() 取。"""

    def __init__(self) -> None:
        # _motion_lock 串行化「下发 + 等结束」这一整段；阻塞拿锁即排队
        self._motion_lock = threading.Lock()
        # _state_lock 护住下面这几个小字段（占用 / 世代）
        self._state_lock = threading.Lock()
        # _holder 是占用来源的**实时**值（进程内）
        self._holder: Optional[str] = None
        # 世代号：_gen_seq 是已发放的号（每笔运动在排队前领一个），
        # _gen_abort 是「已作废世代」的水位（<= 它的都作废），只由 stop_all() 抬高、永不回退
        self._gen_seq: int = 0
        self._gen_abort: int = 0
        self._stop_reason: str = ""

    # -- 占用 ---------------------------------------------------------------

    @property
    def holder(self) -> Optional[str]:
        """当前占用来源；None 表示没有运动在执行。"""
        with self._state_lock:
            return self._holder

    def _reject_if_held_by_navigation(self, source: str) -> Optional[str]:
        """move / action 撞上 navigation 时拒绝；其余情况返回 None（继续走排队）。"""
        holder = self.holder
        if holder == MOTION_NAVIGATION:
            return (f"{source}: 本体正在导航中，暂不接受{source}指令"
                    "（请先停止导航，或等导航结束）")
        return None

    # -- 世代号与中止（退出 / 终止用） ---------------------------------------

    def _new_gen(self) -> int:
        """给一笔即将发起的运动领世代号。**必须在排队（_enter）之前调**。

        发号在排队之前才能表达「这一笔属于哪个年代」：排队期间被 stop_all() 作废的，
        拿到执行权后第一件事就是发现自己已作废、于是不下发；反过来（拿到锁才发号）
        排队中的那单会在退出之后「复活」，把本体重新驱动起来。
        """
        with self._state_lock:
            self._gen_seq += 1
            return self._gen_seq

    def _check_aborted(self, gen: int, source: str) -> None:
        """本笔运动是否已被 stop_all() 作废；作废就抛 MotionAborted。

        判据只有一个：世代号 <= 已作废水位。stop_all() 只抬一次水位，之后新领的号一定更大，
        所以「上一轮的 stop_all 掐掉新运动」不成立（前提：发号在排队之前）。
        """
        with self._state_lock:
            aborted, reason = gen <= self._gen_abort, self._stop_reason
        if aborted:
            raise MotionAborted(f"{source}: 已收到停止信号（{reason}），本次指令已中止")

    # -- 占用 ---------------------------------------------------------------

    def _enter(self, source: str, gen: int) -> Optional[str]:
        """取执行权。返回错误提示表示本次不执行（占用冲突 / 已被终止）；返回 None 表示已持锁。

        成功拿到执行权时 self._holder 已是 source，调用方收尾时必须调 _leave()。
        gen 是本笔运动的世代号（_new_gen 领的），拿锁之后要再比一次——见 _claim。
        """
        if source == MOTION_NAVIGATION:
            # 导航不排队：要么立刻执行，要么被挡住；占用者也是导航时抢占（停掉旧的）
            if self._motion_lock.acquire(blocking=False):
                return self._claim(source, gen)
            holder = self.holder
            if holder != MOTION_NAVIGATION:
                return self._reject_if_held_by_navigation(source) or (
                    f"{source}: 本体正在进行{holder or '其它运动'}，暂不接受导航指令")
            # 停掉原导航，改发新地点：先取消，再等那单收尾（它会看到 CANCELLED 而返回）。
            # 这里是**有上限**的等待（不是靠调用方超时兜底的无限等）：原导航万一对取消
            # 没反应，得让本次调用拿到一句明确提示，而不是一直挂在锁上。
            self._cancel_navigation()
            if not self._motion_lock.acquire(timeout=NAVIGATION_TIMEOUT + MOTION_TOOL_TIMEOUT_SLACK):
                return (f"{source}: 已取消原导航，但它 {NAVIGATION_TIMEOUT:g}s 内仍未退出，"
                        f"本次导航指令未下发")
            return self._claim(source, gen)

        # move / action：可排队
        if self._motion_lock.acquire(blocking=False):
            return self._claim(source, gen)
        reject = self._reject_if_held_by_navigation(source)
        if reject:
            return reject
        self._motion_lock.acquire()          # 排队点：等前面那单真实做完
        return self._claim(source, gen)

    def _claim(self, source: str, gen: int) -> Optional[str]:
        """持锁之后登记占用。返回错误提示表示本笔不能执行（已自行释放锁）。

        两件事都在这里判：本笔是否已被 stop_all() 作废（退出/终止后排队的那单绝不能
        再把运动发出去），以及等锁期间是否来了一单导航并占住了。
        """
        with self._state_lock:
            if gen <= self._gen_abort:
                reason = self._stop_reason
                self._motion_lock.release()
                return (f"{source}: 已收到停止信号（{reason}），本次指令未下发")
            if source != MOTION_NAVIGATION and self._holder == MOTION_NAVIGATION:
                self._motion_lock.release()      # 等锁期间来了一单导航并占住了
                return self._reject_if_held_by_navigation(source)
            self._holder = source
        return None

    def _leave(self) -> None:
        """释放执行权。先清占用再放锁：下一个拿到锁的看到的一定是「无占用」。"""
        with self._state_lock:
            self._holder = None
        self._motion_lock.release()

    def note_stop(self, source: Optional[str] = None) -> None:
        """把「本次运动收到过停止信号」记进最近一笔运动的记录（motion_status.yaml 的 last_motion.stop_received）。

        为什么要记：超时停止与正常结束在快照里长得一样——两者本体都回到空闲。有了这一位，
        才能区分「走完了」和「被掐停了」。记录里已带本次的来源，所以这里只是补一笔。

        Args:
            source: move / navigation；给出时只标记记录里同来源的那笔（防止标记串到别的
                    运动上）。传 None 表示不比对来源——急停是全局的，跟发起方无关。

        只在本进程确实有运动在执行（self._holder 非空）时标记：没有运动在跑时收到 stop，
        那是发给空气的，不该把上一条已经跑完的记录标成「被停止」。重复调用安全（幂等）。
        """
        if self.holder is None:
            return
        note_stop_received(source)

    # -- 子进程 -------------------------------------------------------------

    def _run(self, cmd: Sequence[str], timeout: float, source: str, *,
             allow_emergency: bool = False) -> subprocess.CompletedProcess:
        """跑一个子进程；超时抛 RuntimeError（与既有契约一致）。

        Args:
            allow_emergency: 是否允许命令里夹带急停。默认 False：**唯一**能开它的地方是
                   _emergency_stop()。「退出路径不得急停」因此有代码层面的一道闸门，
                   不只是一句注释。

        用 Popen 而不是 subprocess.run：run 一阻塞就是几十上百秒，中途没法从别的线程
        打断；Popen 拿到句柄后 stop_all() 能 terminate。

        这里跑的全是**下发类**子进程（动作指令、move 的 client、停止命令），一次几十秒封顶，
        没有任何一个长等到需要从别的线程打断的：等状态那次最长（等动作回静止，上限见
        configs.MOTION_ACTION_TIMEOUT_*）走的是 _wait_state 的纯 Python 采样循环。
        （2026-10-08 前有一条「登记持锁者正在等的子进程，供 stop_all() terminate」的
        track 机制，它只服务于那条已被删掉的等待子进程，故一并删除。）
        """
        argv = [str(x) for x in cmd]
        if not allow_emergency and any(EMERGENCY_ACTION in x for x in argv):
            raise RuntimeError(
                f"{source}: 该路径不允许下发急停（只有 stop(emergency=True) 可以）")
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError(
                f"{source}: 子进程 {Path(argv[0]).name} 超过 {timeout:g}s 未返回")
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def _read_action_frame(self) -> Optional['motion_status.ActionFrame']:
        """取一帧动作状态（取值 + 变化次数）。取不到 → None。

        取的是**进程内那份常驻订阅**（Motion_Manager/motion_status.py 的线程，与 manager
        同进程：start_agent.py 头两段写了为什么必须同进程）。本进程没有它（--no-motion-status、
        单独跑脚本、receiver 起的 resume 子进程）时，顺手懒起一份**只读**的，下次起就有；
        **这一次仍是 None**。调用方按「读不到」处理：等状态那边会继续等到 deadline，前置检查
        那边会退到脚本读一帧（见 _ensure_standing）。
        """
        frame = motion_status.action_frame()
        if frame is None:
            motion_status.ensure_reader()
            frame = motion_status.action_frame()
        return frame

    def _wait_state(self, states: set, timeout: float, source: str, gen: int,
                    min_changes: int = 0) -> Tuple[bool, str]:
        """等 action_state 落进 states，且**连续 MOTION_IDLE_HITS 次检测都是它**。
        返回 (是否达成, 未达成时的原因)。

        读的是**进程内那份常驻订阅**（motion_status 的 20Hz 订阅，与 manager 同进程）：它是本
        进程唯一的 motion 订阅，取一帧就是内存里读一下，没有起进程那 2~3s 的冷启动。每
        1/MOTION_STATE_CHECK_HZ 秒采一次，连续命中 MOTION_IDLE_HITS 次才算达成 ——
        也就是**连续 MOTION_IDLE_HOLD_SEC 秒静止**（两个数都在 configs，命中次数由它们算出来）。

        一次「命中」要同时满足三条，缺一不可：
          - 取值在 states 里；
          - **这一帧是新到的**（与上一次采样取到的那帧不同）。同一个 frame 只可能是话题静默、
            缓存没更新 —— 拿它重复计数就把「状态冻住」数成了「连续静止」，正好是消抖要挡的
            假完成（motion_status.ActionFrame 的注释里记着同一个坑）；
          - **窗口内一次都没变过**：第一次命中时记下 changes，后续要求相等。changes 是**每一帧**
            级的单调计数，这条等于「这一串里收到的每一帧都是静止」——只在采样瞬间看一眼会漏掉
            「中间闪一下又回来」（2→1→2）。

        min_changes: 变化次数的下限（含）。0 = 只认值本身；>0 = 还要求这期间状态至少变过这么多下
        —— 用它替代「必须逮到 1/4/5 那一瞬」，做完得比判据起得还快的短动作不会被漏掉。

        每步查一次中止：这里是纯 Python 等待，**没有子进程句柄可供 stop_all() terminate**，中止
        只能靠采样步长（1/MOTION_STATE_CHECK_HZ）及时发现，所以它必须远小于 MOTION_STOP_ALL_WAIT。

        取不到帧**不立刻放弃**（懒起的只读订阅可能刚起来、话题也可能恢复），继续等到 deadline；
        整段一帧都没取到 → 报「读不到动作状态」。**2026-10-08 删掉了原来那条现起
        check_action_state.sh 的子进程兜底**：它冷启动 2~3s、比 getDown 这类 2.2s 的短动作还慢，
        判据反而更弱（2026-09-21「动作已成功却报未观察到启动」就是它造成的），留着只是第二条
        单位还对不上的消抖。脚本本体仍在：开机清理复核读一帧、人工排查都用它。
        """
        deadline = time.monotonic() + timeout
        hits = 0                    # 这一串连续命中的次数
        hit_changes = 0             # 这一串从哪一次变化开始（窗口内要求它一直不动）
        prev: Optional['motion_status.ActionFrame'] = None
        seen_any = False

        while True:
            self._check_aborted(gen, source)
            frame = self._read_action_frame()
            now = time.monotonic()

            if frame is None:
                hits = 0            # 帧都没了，谈不上「连续」
            else:
                seen_any = True
                if frame != prev and frame.value in states:
                    if hits == 0 or frame.changes != hit_changes:
                        # 新开一串：要么上一串刚断，要么**窗口里动过**（采样点看着都是静止，
                        # 中间却闪过别的取值 → changes 涨了，见上面第二条判据）。必须重开而不是
                        # 接着数：接着数的话 hit_changes 这个起点永远对不上，这条判据就再也
                        # 等不到了。
                        hit_changes = frame.changes
                        hits = 1
                    else:
                        hits += 1
                    if hits >= MOTION_IDLE_HITS and frame.changes >= min_changes:
                        return True, ""
                else:
                    hits = 0
                prev = frame

            if now >= deadline:
                return False, self._wait_timeout_reason(states, min_changes, seen_any)
            time.sleep(1.0 / MOTION_STATE_CHECK_HZ)

    def _wait_timeout_reason(self, states: set, min_changes: int, seen_any: bool) -> str:
        """等到超时了，说清楚是哪一种没等到（接在「已下发，但 Ns 内」后面）。"""
        if not seen_any:
            return "读不到动作状态（常驻订阅未就绪或话题静默），无法确认它是否执行"
        last = self._read_action_frame()
        if last is None:
            return "读不到动作状态（常驻订阅中途断了），无法确认它是否执行"
        if min_changes and last.changes < min_changes:
            return ("未观察到动作启动（action_state 这期间一次都没变），"
                    "无法确认它是否执行")
        return (f"未等到 {_describe_states(states)} 连续 {MOTION_IDLE_HOLD_SEC:g}s"
                f"（最后看到 {_describe_state(last.value)}），可能仍在进行或已异常中止")

    @staticmethod
    def _action_end_state(action_type: str) -> set:
        """动作的终态集合：以趴下收尾的（configs.MOTION_LYING_ACTIONS）等 3，其余等 2。

        与 run_action 里判幂等用的是同一份映射（那边问的是「当前姿态是不是就已经是它要的」），
        所以只有这一处说「哪个动作收在哪个姿态」。

        ⚠️ MOTION_STAND_ACTIONS / MOTION_LYING_ACTIONS 是**按动作名分类**的第二套集合：仓库里
        「动作名 → 参数/时长」的表在 configs 那边有好几处同源，这两个集合不在那份清单里，
        新增动作时要单独想一次「它收在站还是趴」，别只改时长表。
        """
        if action_type in MOTION_LYING_ACTIONS:
            return {ACTION_STATE_LYING}
        return {ACTION_STATE_STANDING}

    def _wait_action_end(self, end_states: set, timeout: float, source: str, gen: int,
                         base: Optional['motion_status.ActionFrame']) -> Tuple[bool, str]:
        """等动作回到终态（静止）。返回 (是否等到, 没等到时接在「已下发，但 Ns 内」后面的原因)。

        判据就是 `_wait_state`：连续 MOTION_IDLE_HITS 次检测都落在 end_states 里
        （= 连续 MOTION_IDLE_HOLD_SEC 秒静止）。动作执行**中途会短暂闪回静止态**，只看一帧就会
        把「还没做完」判成「已完成」；注意消抖只挡得住**短于这个窗口**的闪：中途若有一段真实
        静止造型（如直立、跳舞里的定点）比它还长，本判据区分不了 —— 那是动作通道没有完成信号
        导致的，不是这里能修的。

        base 是**下发前**读的那一帧（取不到就是 None），只用来定「这期间到底动过没有」：
          - base 的取值**不在**终态里（趴着站起、站着趴下）：等到的终态**本身就是效果**，
            不再要求先「观察到启动」；
          - base 的取值**就在**终态里（舞蹈等从静止回到静止）：还要求这期间 changes 有增量 ——
            它得真动过。这就是旧代码里「先确认启动」那一步的作用，只是判据从「必须逮到
            1/4/5 那一瞬」放宽成「状态至少变过一次」，不再依赖那一瞬被采到。
        取不到帧（base 为 None）→ 不做「必须动过」这条要求：读不到状态时不该凭空编一个
        「它没动」出来；真的一直读不到，超时文案里会说明白。
        """
        min_changes = base.changes + 1 if (base is not None and base.value in end_states) else 0
        return self._wait_state(end_states, timeout, source, gen, min_changes=min_changes)

    # -- 前置状态检查（三者共用） -------------------------------------------

    def _ensure_standing(self, source: str, gen: int,
                         action_type: Optional[str] = None) -> tuple:
        """运动前置检查：确保处于静止站立，否则先让它站起来。

        判据是 /catch_turtle/action_state == 2（静止站立）——比 /robot_idle_state 快得多。
        不是 2 时先下发 standUp，再轮询等它站起（上限 configs.MOVE_STAND_TIMEOUT）。

        例外：要执行的动作本身就是站立动作（MOTION_STAND_ACTIONS，如 standUp）时跳过
        前置的 standUp——它自己就是站起来，先补一次会连站两次。

        退出/终止时不该再补站立，所以每一步前后都查一次中止（gen）。

        Returns:
            (错误提示, 是否由本次检查自动站立)。错误提示为 None 表示可以继续下发。
        """
        if action_type in MOTION_STAND_ACTIONS:
            return None, False

        self._check_aborted(gen, source)

        # 读一帧当前状态。读不到（超时/环境错误）时**不下发运动**：姿态未知的情况下
        # 让四条腿动起来风险太大，宁可让调用方看到明确提示后重试。
        # 进程内那一帧与脚本的退出码逐值对齐：2 静止站立 = rc 0；其余任何值（含码表外的原始
        # 码）= 读了状态但不在站立 = rc 1，都先补 standUp。rc 2/3（读不到 / 环境错）在进程内
        # 不存在：帧要么在，要么 _read_action_frame() 给 None，那正是下面兜底子进程的活。
        frame = self._read_action_frame()
        if frame is not None:
            if frame.value == ACTION_STATE_STANDING:
                return None, False      # 已站立，直接运动
        else:
            try:
                check = self._run([str(MOTION_STATE_SCRIPT)], MOVE_STATE_TOOL_TIMEOUT, source)
            except RuntimeError:
                return (f"{source}: 前置检查超时，未能读到机器狗动作状态"
                        "（/catch_turtle/action_state），已取消本次动作"), False

            if check.returncode == 0:
                return None, False      # 已站立，直接运动
            if check.returncode in (2, 3):
                return (f"{source}: 无法确认机器狗是否站立"
                        f"（{_tail(check.stderr) or _tail(check.stdout) or 'code=' + str(check.returncode)}），"
                        f"已取消本次动作"), False

        # 明确不是静止站立（趴下/运动中/急停/切换步态），先补一个 standUp
        self._check_aborted(gen, source)
        stand = self._run(["bash", str(ACTION_SCRIPT), "standUp"], MOTION_SCRIPT_TIMEOUT, source)
        if stand.returncode != 0:
            return (f"{source}: 机器狗当前未站立，自动站立失败"
                    f"（{_tail(stand.stderr) or _tail(stand.stdout) or 'code=' + str(stand.returncode)}），"
                    f"已取消本次动作"), False

        # 等它站起来：判据同动作终态那一套（连续 MOTION_IDLE_HITS 次检测都是 2 = 连续
        # MOTION_IDLE_HOLD_SEC 秒静止站立）。
        ok, reason = self._wait_state({ACTION_STATE_STANDING}, MOVE_STAND_TIMEOUT, source, gen)
        if ok:
            return None, True           # 站好了，继续原运动
        return (f"{source}: 机器狗当前未站立，已下发 standUp 但 {MOVE_STAND_TIMEOUT:g}s 内未站起"
                f"（{reason}），已取消本次动作"), False

    # -- 下发 ---------------------------------------------------------------

    def _dispatch_action(self, action_type: str, source: str,
                         channel: str = ACTION_CHANNEL_SKILL) -> None:
        """下发一条动作通道指令：robot_action.sh <参数> <通道>（通道缺省 motorSkill）。

        通道就是 RobdogAction 的 actiontype（见 skills/robot-dog-actions/SKILL.md）：
        预设动作用 motorSkill，步态用 sportMode。脚本本来就认第二个位置参数，这里只是把它显式化。
        """
        result = self._run(["bash", str(ACTION_SCRIPT), action_type, channel],
                           MOTION_SCRIPT_TIMEOUT, source)
        if result.returncode != 0:
            raise RuntimeError(
                f"{source} 执行失败 (code={result.returncode}): "
                f"{result.stderr or result.stdout}")

    def _dispatch_move(self, mode: str, speed: float, duration: float, source: str) -> None:
        result = self._run([str(MOTION_SCRIPT), mode, f"{speed:g}", f"{duration:.2f}"],
                           MOTION_MOVE_SCRIPT_TIMEOUT, source)
        if result.returncode != 0:
            raise RuntimeError(
                f"{source} 执行失败 (code={result.returncode}): "
                f"{result.stderr or result.stdout}")

    @staticmethod
    def _cancel_navigation() -> None:
        """取消导航后端正在执行的定点导航。尽力而为、永不抛异常（与 task_exit 同约定）。"""
        try:
            from configs.configs import NAVIGATION_CONTROL_TIMEOUT
            from plugin_tools.nav_tools import cancel_navigation
            cancel_navigation(NAVIGATION_CONTROL_TIMEOUT)
        except Exception:
            pass

    def _dispatch_navigation(self, landmark: str, source: str, gen: int) -> None:
        """发起导航并轮询到结束（SUCCESS / FAILED / CANCELLED / 超时）。

        HTTP 走 plugin_tools/nav_tools.py（唯一客户端，应答三态见它的模块文档）。
        """
        from configs.configs import NAVIGATION_REQUEST_TIMEOUT
        from plugin_tools import nav_tools

        body = nav_tools.navigate(landmark, NAVIGATION_REQUEST_TIMEOUT)
        if not isinstance(body, dict) or body.get("code") != 0:
            detail = "导航服务没有应答" if body == "" else str(body)[:200]
            raise RuntimeError(f"waypoint_nav 发起导航失败: {detail}")

        deadline = time.time() + NAVIGATION_TIMEOUT
        while time.time() < deadline:
            time.sleep(NAVIGATION_POLL_INTERVAL)
            # 这是唯一的纯 Python 等待循环（最长 NAVIGATION_TIMEOUT），退出/终止后
            # 必须在这里收手：单轮的阻塞上限是轮询间隔 + 请求超时，够快。
            self._check_aborted(gen, source)
            status_body = nav_tools.navigation_status(NAVIGATION_REQUEST_TIMEOUT)
            if not isinstance(status_body, dict):
                raise RuntimeError(f"waypoint_nav 查询状态失败: {status_body!r}")
            data = status_body.get("data")
            data = data if isinstance(data, dict) else {}
            status = data.get("status", "")

            if status == "SUCCESS":
                return
            if status in ("FAILED", "CANCELLED"):
                # CANCELLED 就是「这一单收到了停止信号」——取消它的可能是 stop()、
                # task_exit 收尾，或另一单导航抢占。记录里此刻还是本单（begin_motion
                # 只在下发前调用），补一笔后再抛，抢占方的 begin_motion 会整条覆盖掉。
                if status == "CANCELLED":
                    self.note_stop(source)
                raise RuntimeError(
                    f"navigation 导航终止（status={status}，destination={data.get('destination')}）")
            # IDLE / RUNNING / 未知状态：继续等待

        raise RuntimeError(f"navigation 导航超时（{NAVIGATION_TIMEOUT}s 内未完成）")

    # -- 对外接口 -----------------------------------------------------------

    def run_action(self, action_type: str) -> str:
        """下发一个动作，并等到它结束。

        动作没有自己的完成信号，所以结束判定是「action_state 回到静止」：连续
        configs.MOTION_IDLE_HOLD_SEC 秒都落在 IDLE_ACTION_STATES（= 静止站立或静止趴下）里，
        即算做完——不区分它收在站还是趴（那是幂等短路才关心的事）。判据与超时上限的细节都在
        _wait_action_end 里。

        **幂等**：要下发的姿态就是当前姿态时（standUp 时已经站着、getDown 时已经趴着）直接
        返回成功，不下发、不补站立、不写运动记录。「当前姿态」取自进程内那份常驻订阅；取不到
        就不短路，照旧下发（宁可多动一次，也不要凭一个读不到的姿态把动作吞掉）。
        """
        source = MOTION_ACTION
        # 结束判据 = 回到**任一静止态**（IDLE_ACTION_STATES = 静止站立/静止趴下）。动作通道没有
        # 完成信号，只能拿状态反推；「它到底该收在站还是趴」那是**幂等短路**才需要区分的，
        # 见下面 action_end_state()（2026-10-08 改：结束判定不再按动作名查它收在哪个姿态）。
        end_states = IDLE_ACTION_STATES

        # 幂等：它要的就是当前这个姿态时，别再下发一遍 —— 趴着再说「趴下」不该先站起来，
        # 站着再说「站立」也不该再发一条指令。**只对这两个姿态动作集合生效**：别的动作
        # （舞蹈/打招呼…）没有「当前状态就已经是它的效果」这回事，在那里短路会把狗空闲时
        # 的每一次调用都判成「已完成」。
        # 放在 _enter 之前：既不必排队等前面的运动做完，也不写运动记录（记录记的是
        # 「这一笔下发的运动」，这一笔压根没下发；而 stop_all 标「被掐停」是按来源比对的，
        # 把 begin_motion 往后挪会把那条标记的窗口从微秒拉到秒级）。取不到帧 → 不短路。
        if action_type in (MOTION_STAND_ACTIONS | MOTION_LYING_ACTIONS):
            frame = self._read_action_frame()
            if frame is not None and frame.value in self._action_end_state(action_type):
                return (f"✅ 动作执行完成: {action_type}"
                        f"（当前已是{_describe_state(frame.value)}，未重复下发）")

        gen = self._new_gen()               # 世代号必须排在 _enter（排队）之前
        reject = self._enter(source, gen)
        if reject:
            return reject
        try:
            # 记录里的「这一笔是什么」就是动作名本身（快照的 last_motion.params）
            begin_motion(source, action_type)
            pre_error, _ = self._ensure_standing(source, gen, action_type=action_type)
            if pre_error:
                return pre_error

            self._check_aborted(gen, source)
            # 基线必须在**下发前**读：_ensure_standing 可能刚补过一次 standUp，那次也会让
            # 状态变一次，把「这期间动过没」的起点顶掉。读不到 → base 为 None（不要求动过）。
            base = self._read_action_frame()
            self._dispatch_action(action_type, source)

            timeout = action_timeout(action_type)
            ok, note = self._wait_action_end(end_states, timeout, source, gen, base)
            if not ok:
                return f"⚠️ 动作 {action_type} 已下发，但 {timeout:g}s 内{note}"
            return f"✅ 动作执行完成: {action_type}"
        except MotionAborted as exc:
            # 动作没有停止接口，中终止的是「我们这边的等待」——本体可能还在动，
            # 文案里要说清楚，别让调用方以为动作被停住了（见 stop_all 的说明）。
            return f"⏹️ {exc}（动作无停止接口，本体可能仍在执行）"
        finally:
            self._leave()

    def run_gait(self, gait_type: str) -> str:
        """切换步态（/catch_turtle/action_type 的 sportMode 通道），并等它切完。

        与 run_action 是同一套形状——同一话题、同一「回到终态」判据（_wait_action_end）、
        同一套占用与排队。差别只有三处：

          - 通道是 sportMode 而不是 motorSkill（gait_type 是 `actionargument`，取值见
            configs.GAIT_TYPES）；
          - **没有幂等短路**（不像 run_action 对 standUp/getDown 那样先看当前姿态）：manager 刻意
            不读 ROS（见 _wait_state 的说明），判不了「当前是不是这个步态」；切换步态本来就是
            「再下发一条指令」，重发同一条是安全的。此刻真是什么步态，看 motion_status 的 motion.gait；
          - 终态固定 2 静止站立：切换步态必然收在站立，与 MOTION_LYING_ACTIONS 那套无关。

        记录里的 params 写 `gait <gait_type>`：action_source 仍是 action（同一条通道、同一份
        占用规则，见 motion_record.MOTION_SOURCES），只写个 "run" 会被读成「有个叫 run 的动作」。
        """
        source = MOTION_ACTION
        gen = self._new_gen()               # 世代号必须排在 _enter（排队）之前
        reject = self._enter(source, gen)
        if reject:
            return reject
        try:
            begin_motion(source, f"gait {gait_type}")
            # 步态不是站立动作，别走 _ensure_standing 里那个「它自己就是站起来」的例外；
            # 切换步态同样要求先站稳。
            pre_error, _ = self._ensure_standing(source, gen)
            if pre_error:
                return pre_error

            self._check_aborted(gen, source)
            # 基线必须在**下发前**读：_ensure_standing 可能刚补过一次 standUp。切换前是 2 静止
            # 站立、而 2 正是终态，所以这一读决定了判据要不要「这期间至少变过一次」——
            # 切换步态必然要让 action_state 走一趟 5（切换步态中），这一读正是用来证明它走过。
            base = self._read_action_frame()
            self._dispatch_action(gait_type, source, ACTION_CHANNEL_GAIT)

            ok, note = self._wait_action_end({ACTION_STATE_STANDING}, MOTION_GAIT_TIMEOUT,
                                             source, gen, base)
            if not ok:
                return f"⚠️ 步态切换 {gait_type} 已下发，但 {MOTION_GAIT_TIMEOUT:g}s 内{note}"
            return f"✅ 步态已切换: {gait_type}"
        except MotionAborted as exc:
            # 与动作同理：步态切换没有停止接口，中终止的是「我们这边的等待」
            return f"⏹️ {exc}（步态切换无停止接口，本体可能仍在执行）"
        finally:
            self._leave()

    def run_move(self, mode: str, speed: float, duration: float, label: str) -> str:
        """下发一次平移/旋转，并等到它结束。label 是给调用方拼返回文案用的运动量描述。"""
        source = MOTION_MOVE
        gen = self._new_gen()
        reject = self._enter(source, gen)
        if reject:
            return reject
        try:
            # label 是调用方拼好的运动量描述（如 "forward 1m"），正是要记进快照的那笔参数
            begin_motion(source, label)
            pre_error, auto_stood = self._ensure_standing(source, gen)
            if pre_error:
                return pre_error

            self._check_aborted(gen, source)
            self._dispatch_move(mode, speed, duration, source)

            # client 是「收到停止信号才返回」的，返回即运动结束；这里再确认本体确实
            # 回到了静止站立（同样是「连续 MOTION_IDLE_HOLD_SEC 秒都是 2」），避免后续
            # 工具调用与残余运动重叠。
            ok, reason = self._wait_state({ACTION_STATE_STANDING}, MOTION_MOVE_END_TIMEOUT,
                                          source, gen)
            if not ok:
                return (f"⚠️ 移动指令 {label} 已下发，但 {MOTION_MOVE_END_TIMEOUT:g}s 内"
                        f"未确认本体回到静止站立（{reason}）")
            stood_note = "（执行前已自动站立）" if auto_stood else ""
            return f"✅ 移动完成: {label}{stood_note}"
        except MotionAborted as exc:
            return f"⏹️ {exc}"
        finally:
            self._leave()

    def run_navigation(self, landmark: str) -> str:
        """发起定点导航，并等到导航后端给出终态。"""
        source = MOTION_NAVIGATION
        gen = self._new_gen()
        reject = self._enter(source, gen)
        if reject:
            return reject
        try:
            # 记录里的「这一笔是什么」就是目标点名称（快照的 last_motion.params）
            begin_motion(source, landmark)
            pre_error, _ = self._ensure_standing(source, gen)
            if pre_error:
                return pre_error

            # 结束判定走导航自己的接口（SUCCESS / FAILED / CANCELLED），不听 action_state：
            # 到没到目的地只有导航后端知道，本体「不动了」既可能是到达也可能是卡住。
            self._dispatch_navigation(landmark, source, gen)
            return "✅ 导航成功到达目的地"
        except MotionAborted as exc:
            return f"⏹️ {exc}"
        finally:
            self._leave()

    def stop(self, source: str, emergency: bool = False) -> str:
        """停止运动。直通：不排队、不等结束，但会记一笔「本次收到停止信号」。

        Args:
            source:    move / navigation；action 目前没有停止能力。
            emergency: 是否急停。True 时直接发全局急停动作 emergencyStop，忽略 source。
                       **默认 False，且不向智能体暴露**——它只是本模块的 Python 接口参数，
                       不进任何工具的参数表，LLM 无法通过工具调用把它打开。

        急停是全局的（规格 §4 急停一栏：actiontype=motorSkill、actionargument 留空），
        下发后本体进入 action_state=6，需要重新下发运动指令才能恢复。

        记的那一笔只改内存记录里的 stop_received 位，不动进程内占用（self._holder）：谁在跑
        还是谁在跑，收尾照旧由执行中的那单自己 _leave。快照里因此能区分「跑完了」与
        「被掐停了」——两者本体都回到空闲。

        **不动世代号**（不中止在飞的等待）：stop("move") 靠「client 收到 stop 后返回 +
        再确认本体回到静止站立」的既有语义收场，提前放弃会让调用方拿不到那句确认。
        要连等待一起收掉的场合是退出/终止——那条路径走 stop_all()。
        """
        if emergency:
            return self._emergency_stop(source)

        if source == MOTION_MOVE:
            ok, detail = self._stop_move_command()
            if not ok:
                raise RuntimeError(detail)
            self.note_stop(MOTION_MOVE)
            return "✅ 移动完成: 已停止"

        if source == MOTION_NAVIGATION:
            self._cancel_navigation()
            # 在跑的那单会在轮询里看到 CANCELLED 并自己补一笔（见 _dispatch_navigation）；
            # 这里再补一次是兜底：它可能已经返回、也可能来不及看到就被收尾了。幂等，重复无害。
            self.note_stop(MOTION_NAVIGATION)
            return "✅ 导航已取消"

        if source == MOTION_ACTION:
            return "action: 动作暂不支持停止"

        return f"stop: 未知的停止来源 {source!r}，可用: move / navigation / action"

    def stop_all(self, reason: Optional[str] = None, wait: bool = True) -> str:
        """退出 / 终止时一次停掉所有能停的运动（task_exit.exit_tasks 的唯一出口）。

        **绝不急停**：本方法没有 emergency 参数，方法体里也只调用非急停的
        _stop_move_command() / _cancel_navigation() / _cancel_vln()——急停命令只由
        _emergency_stop() 拼，而它只被 stop(emergency=True) 调用。

        步骤（尽力而为：任何一步失败都只落进返回的摘要，不抛异常——退出路径不能被它绊住）：
          1. 取占用快照，把「已作废世代」抬到当前已发放的号 → 在飞/排队的那单收手；
          2. 对本体下发非急停停止：move 的 stop 指令 + 导航 cancel + VLN 进程清理；
          3. 在记录里标一笔「这次运动是被终止掐停的」；
          4. 等一下执行权真的释放（上限 MOTION_STOP_ALL_WAIT），免得新任务的第一条
             move 还排在旧运动后面。

        为什么只靠世代号就够：那笔运动在每个检查点上都查一次作废，而最长的阻塞「等动作回到
        静止」是按 MOTION_STATE_CHECK_HZ 采样、每采一次查一遍，最多滞后一个采样间隔就
        _leave() 了。（2026-10-08 前还有一步 terminate 掉持锁者正在等的子进程——那条等待
        子进程已删，见 _wait_state，这一步随之删掉。）

        Args:
            reason: 写进中止文案与日志（如「任务已取消: 用户终止」），缺省「任务终止」。
            wait:   是否等执行权释放；等不到只记进摘要，不影响退出。

        Returns: 一行可直接进日志的中文摘要。
        """
        reason = reason or "任务终止"

        # 1) 先取快照再作废：作废后在飞的那单很快会 _leave()（holder 变 None），
        #    那时候再读就漏标「被终止掐停」了。
        with self._state_lock:
            self._gen_abort = self._gen_seq
            self._stop_reason = reason
            holder = self._holder

        # 2) 对本体下发非急停停止（都幂等，空闲时发也无害）
        parts = []
        _, move_detail = self._stop_move_command()
        parts.append(move_detail)
        self._cancel_navigation()
        parts.append("navigation=已取消")
        if holder == MOTION_ACTION:
            parts.append("action=无停止接口（规格没有停止动作，退出也不用急停），"
                         "本体将把该动作跑完")
        elif holder is None:
            parts.append("无运动在执行")
        parts.append(self._cancel_vln())

        # 3) 记录里那一位：用第 1 步的快照判断，不用 note_stop() 的「此刻 holder 非空」守卫
        #    ——那时它可能已经被清空了。action 也照标：任务终止对它就是停止信号；
        #    本体到底停没停看 action_state，别看这一位。
        if holder is not None:
            note_stop_received(holder)

        # 4) 等执行权释放（带上限；被持锁线程自己调用也只是超时返回）
        if wait and not self._wait_idle(MOTION_STOP_ALL_WAIT):
            parts.append(f"执行权 {MOTION_STOP_ALL_WAIT:g}s 内未释放")

        return f"[{reason}] " + "；".join(parts)

    # -- 停止手段（非急停；stop() 与 stop_all() 共用） -----------------------

    def _stop_move_command(self) -> Tuple[bool, str]:
        """发 move 自己的停止指令，返回 (是否成功, 说明)；不抛异常。

        服务端收到 stop 会清空队列并 settleMotion 唤醒阻塞中的 client（src/server.cpp），
        所以正卡在下发里的那单会立刻返回——这是退出时最快的收手方式。
        """
        try:
            result = self._run([str(MOTION_SCRIPT), "stop"], MOTION_MOVE_SCRIPT_TIMEOUT, MOTION_MOVE)
        except Exception as exc:
            return False, f"move 停止指令异常: {exc}"
        if result.returncode != 0:
            return False, (f"move 停止指令失败 (code={result.returncode}): "
                           f"{_tail(result.stderr) or _tail(result.stdout)}")
        return True, "move=已停止"

    @staticmethod
    def _cancel_vln() -> str:
        """pkill 视觉导航（VLN）进程树，返回一行说明；不抛异常。

        从 Communicator/task_exit.py 搬来，行为不变：VLN 是 subprocess 起的、没有句柄
        留存，只能按脚本路径兜底清理；无匹配（rc=1）属正常。
        ⚠️ 本树没有走这条路的调用方：角色侧的工具层经脚本下发（不经 MotionManager），
        而 vision_language_nav 的工具实现还是个提前 return 的桩（plugin_tools/
        tool_executor.py），它既没起过子进程、也没在白名单里。这段是按「停止所有运动」
        的完整性保留的，真删 VLN（或把它接上）时一并处理。
        """
        notes = []
        for pattern in (str(VLN_SCRIPT), "run_vln_instruction.sh"):
            try:
                proc = subprocess.run(["pkill", "-f", pattern], capture_output=True,
                                      text=True, timeout=5)
                notes.append(f"VLN[{pattern}] rc={proc.returncode}")
            except Exception as exc:
                notes.append(f"VLN[{pattern}] 异常: {exc}")
        return "；".join(notes)

    def _emergency_stop(self, source: str) -> str:
        """唯一的急停下发点：只被 stop(emergency=True) 调用，stop_all() 无路径可达它。"""
        result = self._run(["bash", str(ACTION_SCRIPT), EMERGENCY_ACTION],
                           MOTION_SCRIPT_TIMEOUT, source, allow_emergency=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"急停下发失败 (code={result.returncode}): "
                f"{result.stderr or result.stdout}")
        # 急停不认来源：不发 source，直接标记最近这笔
        self.note_stop()
        return "🛑 已下发急停"

    def _wait_idle(self, timeout: float) -> bool:
        """等执行权空闲（带上限）。带超时的 acquire：万一被持锁线程自己调也只是超时返回。"""
        got = self._motion_lock.acquire(timeout=timeout)
        if got:
            self._motion_lock.release()
        return got


def _tail(text: Optional[str]) -> str:
    """取子进程输出的最后一行，用作错误提示（脚本的诊断都在 stderr）。"""
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else ""


def _describe_state(value: Optional[int]) -> str:
    """动作状态取值的可读名（码表就是 configs.ACTION_STATES 那一份）。

    码表外的原始码与「读不到」都给一句人话：判据超时的时候，调用方（与日志）要能看出
    「卡在哪一个状态上」，而不是只拿到一句「没等到」。
    """
    if value is None:
        return "未读到状态"
    return ACTION_STATES.get(value, f"未知取值({value})")


# =============================================================================
# 进程内单例
# =============================================================================

_MANAGER: Optional[MotionManager] = None
_MANAGER_LOCK = threading.Lock()


def get_motion_manager() -> MotionManager:
    """取本进程的 MotionManager 单例。

    单例是必要的而不是图方便：占用状态（谁在动）必须全进程一份，多个实例会让
    「同一时刻只有一个来源能动」这条规则失效。
    """
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = MotionManager()
    return _MANAGER


def _describe_states(states: set) -> str:
    """一个取值集合的可读名（`{2} → 静止站立`、`{2, 3} → 静止站立 / 静止趴下`）。"""
    return " / ".join(_describe_state(s) for s in sorted(states))
