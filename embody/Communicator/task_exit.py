"""
任务终止 / 退出统一流程。

正常完成、取消、终止、异常中断、手动中断的任务，退出时统一走本模块的
exit_tasks()：

    1. 停止所有运动 —— Motion_Manager/motion_manager.py 的 stop_all()，**不含急停**
       （move 停止指令 + 导航取消 + VLN 进程清理；动作没有非急停停止接口）
    2. send_task_progress(waiting_for_user=False)  复位云端"活跃任务/回答模式"
    3. 清空任务池（不触发已有任务、拒绝新任务）

**清任务池 ≠ 放行新任务**：同时只允许一个 task_loop 由下面的并发闸门（_RUNNING 单槽）
把守，它只认「载体真的退出了」，清池动作动不了它（见 _running_task_id_locked）。

停止指令一律由 Motion_Manager 下发，本模块不再自己拼（原先这里直接调导航 cancel 与
pkill）——退出路径上「什么算运动、怎么停」只有一份实现。

任务池 _POOL 是模块级登记处，进程退出场景调用 exit_tasks(reject_new=True) 一次性收尾。
**本树目前没有登记方**：原先登记的是 receiver 的 create/resume 路径（与任务载体同进程），
而 receiver 已改成只转投 rolebus 事件的薄桥，任务归角色系统，登记这件事没人做了。于是
本模块实际只有两处在用 —— `stop_all_motion()`（start_agent 启动前的停运动、以及角色侧工具
进程调用的退出停运动都走它）与空池下的 exit_tasks。登记方是新设计的一部分，恢复时别照抄
旧形态（跨进程的 threading.Event 传不过去）。

任务池登记结构（task_id → dict）：
    {
        "description":   任务描述（人类可读，上报 task_description），
        "worker":        任务载体（threading.Thread / multiprocessing.Process，可空），
        "cancel_event":  协作取消事件 threading.Event（可空），
    }

本模块不依赖 ROS 节点，可被任何退出/终止机制独立调用；所有函数尽力而为、
不抛异常（除 register_task 的正常返回外），调用方无需 try/except。
"""

import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# 确保 cmcc_agent 包根目录在 Python 搜索路径中（与 sender/receiver 一致）
_ROOT = Path(__file__).parent.parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.agents import TASK_STATUS
from Communicator.sender import send_task_progress


# =============================================================================
# 模块级任务池（唯一登记处）
# =============================================================================

_POOL: Dict[str, Dict[str, Any]] = {}
_POOL_LOCK = threading.Lock()
_ACCEPTING_NEW = True   # 是否接受新任务（退出流程中置 False，register_task 一律拒绝）

# ---- 并发闸门：同时只允许一个 task_loop -------------------------------
# 单槽登记"当前占着闸门的那个任务"（task_id + 载体）。它与 _POOL 的分工：
#   _POOL  —— 记账用（谁在册、上报终态、定位 interrupt/answer 的目标），exit_tasks 会清它；
#   _RUNNING —— 闸门用（还能不能再起一局），**谁也不清它**，只在载体真的退出后惰性算空。
# 两者分开是因为清池时机与"任务真的不跑了"不是一回事：取消时 join 超时、或旧任务刚
# clear_pool 但线程还在收尾（Visualizer 还占着 8765），池里已经空了而旧 loop 还在跑。
# 只看池的话这些窗口会放第二个 task_loop 进来。代价是：真卡死的僵尸线程（long tool call
# 里出不来）会一直占着闸门，新任务一律被拒 —— 这是有意的，宁可按忙拒绝也不并起两局。
_RUNNING: Dict[str, Any] = {"task_id": "", "worker": None}


def _carrier_alive(worker: Any) -> bool:
    """载体是否还在跑。Thread / Process 共用 is_alive()；未起载体或已退出都算没在跑。"""
    if worker is None:
        return False
    try:
        return bool(worker.is_alive())
    except Exception:
        return False


def _running_task_id_locked() -> str:
    """（须持锁）当前占着闸门的 task_id；没有任务在跑时返回空串。

    载体还活着 → 占着闸门（无论池里有没有它）；载体没起或已退出 → 只有池里还留着登记
    才算占着（register 后还没 start、或还没清池的那一瞬）。
    """
    task_id = _RUNNING["task_id"]
    if _carrier_alive(_RUNNING["worker"]):
        return task_id
    return task_id if _POOL else ""


def running_task_id() -> str:
    """当前占用并发闸门的 task_id（没有任务在跑时为空串）。

    与 active_tasks()/get_task() 的区别：池被清过之后它照样能答出"那局还在跑"。
    闲置的槽不需要谁记得释放，载体退出后自动算空。
    """
    with _POOL_LOCK:
        return _running_task_id_locked()


def register_task(
    task_id: str,
    description: str,
    worker: Any = None,
    cancel_event: Any = None,
) -> bool:
    """登记任务到任务池，并占用并发闸门（同时只允许一个 task_loop）。

    未开放接收新任务（_ACCEPTING_NEW=False，即系统退出/手动中断中）、或闸门已被另一个
    还在跑的任务占用时返回 False，调用方应据此拒绝该任务（create/resume 的忙碌拒绝
    统一从这里取）。调用方进来之前都会先用 is_busy() 挡一道，这里的检查是入口处的兜底：
    「同时只跑一个」这条不变量只在这一处成立，不靠调用方自觉。
    """
    with _POOL_LOCK:
        if not _ACCEPTING_NEW or _running_task_id_locked():
            return False
        _POOL[task_id] = {
            "description": description,
            "worker": worker,
            "cancel_event": cancel_event,
        }
        _RUNNING["task_id"] = task_id
        _RUNNING["worker"] = worker
        return True


def update_task(task_id: str, **fields: Any) -> bool:
    """补填任务池中已登记任务的字段（如 worker / cancel_event，登记后再就绪）。

    闸门槽里那份载体同步更新（resume 是"先登记、起了进程再补载体"），否则闸门
    认不出载体已退出/仍在跑。
    """
    with _POOL_LOCK:
        if task_id not in _POOL:
            return False
        _POOL[task_id].update(fields)
        if task_id == _RUNNING["task_id"] and "worker" in fields:
            _RUNNING["worker"] = fields["worker"]
        return True


def unregister_task(task_id: str) -> Optional[Dict[str, Any]]:
    """从任务池移除任务登记（幂等：不存在时返回 None，不抛异常）。

    "清空任务池（不触发已有任务）"即通过本函数/clear_task_pool 完成：
    仅移除登记，不置位 cancel_event、不终止 worker，任务按自身节奏退出。
    **不动并发闸门** —— 取消路径上正是靠这一点，被取消任务的僵尸线程还在跑时
    新任务进不来（见 _RUNNING 的说明）。
    """
    with _POOL_LOCK:
        return _POOL.pop(task_id, None)


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """读取任务池中单条登记（副本；不存在返回 None）。"""
    with _POOL_LOCK:
        entry = _POOL.get(task_id)
        return dict(entry) if entry else None


def active_tasks() -> Dict[str, Dict[str, Any]]:
    """任务池全部登记的快照副本（供遍历 / 忙碌判断 / 终态补报）。"""
    with _POOL_LOCK:
        return {cid: dict(e) for cid, e in _POOL.items()}


def is_busy() -> bool:
    """是否已有任务在跑（并发闸门被占用）—— 同时只允许一个 task_loop。

    判据是 running_task_id() 而非"池非空"：光看池会被 clear_pool 骗（见 _RUNNING）。
    """
    return bool(running_task_id())


def set_accepting_new(accept: bool) -> None:
    """开关"接收新任务"：退出流程置 False 后，register_task 一律返回 False。"""
    global _ACCEPTING_NEW
    with _POOL_LOCK:
        _ACCEPTING_NEW = accept


def is_accepting_new() -> bool:
    with _POOL_LOCK:
        return _ACCEPTING_NEW


def clear_task_pool() -> int:
    """清空任务池全部登记（不触发已有任务，仅移除登记），返回移除条数。

    **不放行新任务**：并发闸门不受影响，载体还在跑就照样算忙（见 _RUNNING）。
    """
    with _POOL_LOCK:
        n = len(_POOL)
        _POOL.clear()
        return n


# =============================================================================
# 停止所有运动（薄壳，实现全在 Motion_Manager）
# =============================================================================

def stop_all_motion(reason: Optional[str] = None, logger: Optional[Any] = None,
                    wait: bool = True) -> str:
    """停掉所有运动：move 停止指令 + 导航取消 + VLN 进程清理（**不含急停**）。

    实际动作全在 Motion_Manager/motion_manager.py 的 stop_all()——本函数只负责取单例、
    记日志、吞异常（退出路径不能被它绊住，重复调用也安全：那些停止都幂等）。

    Args:
        reason: 写进中止文案与日志（如「任务已取消: 用户终止」）。
        logger: 可选日志器；缺省用 print。
        wait:   是否等执行权释放（让新任务的第一条 move 不必排在旧运动后面）。

    Returns:
        stop_all() 的一行摘要（异常时是一句错误说明），便于调用方再记一笔。
    """
    try:
        from Motion_Manager.motion_manager import get_motion_manager
        ret = get_motion_manager().stop_all(reason=reason, wait=wait)
    except Exception as exc:
        ret = f"停止运动异常: {exc}"
        _log(logger, f"[任务退出] {ret}", "error")
        return ret
    _log(logger, f"[任务退出] 停止所有运动: {ret}")
    return ret



# =============================================================================
# 统一退出流程
# =============================================================================

# 可上报给云端的任务终态；其余状态（executing/interacting/replan 等）一律收敛为 stopped
_TERMINAL_STATUSES = frozenset({
    TASK_STATUS["SUCCEEDED"]["value"],
    TASK_STATUS["STOPPED"]["value"],
    TASK_STATUS["FAILED"]["value"],
})


def exit_tasks(
    reason: str,
    *,
    task_id: Optional[str] = None,
    task_status: Optional[str] = None,
    report_progress: bool = True,
    clear_pool: bool = True,
    reject_new: bool = False,
    stop_motion: bool = True,
    stop_wait: bool = True,
    logger: Optional[Any] = None,
) -> None:
    """统一任务终止流程（正常完成 / 取消 / 终止 / 异常中断 / 手动中断共用）。

    Args:
        reason:          终止原因，写入 current_step_desc（如 "Receiver 退出，任务被中断"）。
        task_id:        指定只终止该任务；None 表示终止任务池内全部任务。
        task_status:     终态（succeeded / stopped / failed）；缺省或非法时用 stopped 兜底。
        report_progress: 是否 send_task_progress(waiting_for_user=False) 复位云端。
        clear_pool:      是否清空任务池登记（不触发已有任务，仅移除登记）。
        reject_new:      是否拒绝新任务（进程退出 / 手动中断场景置 True，之后 register 一律拒绝）。
        stop_motion:     是否停止所有运动（move + 两类导航；**不含急停**）。
        stop_wait:       停止时是否等执行权释放（False = 只下发不等，进程已在退出的场合用）。
        logger:          可选日志器（.info/.error）；缺省用 print。
    """
    # 1. 停止所有运动（放最前面：停是安全动作，上报是记账；越早停，被取消任务在飞的
    #    运动越早收手，新任务的第一条 move 也越早能下发）。实现全在 Motion_Manager，
    #    本模块只调这一处——原先这里自己拼导航 cancel + pkill，现收口到那边。
    if stop_motion:
        stop_all_motion(reason, logger, wait=stop_wait)

    # 2. 终态进度上报：waiting_for_user=False，复位云端活跃任务/回答模式
    if report_progress:
        _report_terminal_progress(reason, task_id, task_status, logger)

    # 3. 清空任务池（不触发已有任务：仅移除登记，不动 cancel_event / worker）
    if clear_pool:
        if task_id:
            if unregister_task(task_id) is not None:
                _log(logger, f"[任务退出] 已从任务池移除登记: task_id={task_id}")
        else:
            cleared = clear_task_pool()
            if cleared:
                _log(logger, f"[任务退出] 已清空任务池: 移除 {cleared} 条任务登记")

    # 4. 拒绝新任务（进程退出/手动中断场景）
    if reject_new:
        set_accepting_new(False)
        _log(logger, "[任务退出] 已关闭接收新任务")


def _report_terminal_progress(
    reason: str,
    task_id: Optional[str],
    task_status: Optional[str],
    logger: Optional[Any],
) -> None:
    """对目标任务补发一条终态进度快照（waiting_for_user=False）。

    指定 task_id 时只补该任务（即使已不在池中也补报，用于任务取消但登记丢失的兜底）；
    为空时对任务池内全部任务补报。
    """
    status = task_status if task_status in _TERMINAL_STATUSES else TASK_STATUS["STOPPED"]["value"]

    if task_id:
        entries = {task_id: get_task(task_id) or {"description": ""}}
    else:
        entries = active_tasks()
        if not entries:
            return

    for cid, entry in entries.items():
        snapshot = {
            "task_id": cid,
            "task_status": status,
            "task_description": (entry or {}).get("description", "") or "",
            "current_step_desc": reason,
            "waiting_for_user": False,
            "pending_question": "",
        }
        try:
            ret = send_task_progress(snapshot)
            _log(
                logger,
                f"[任务退出] 补报终态进度: task_id={cid}, task_status={status}, ret={ret}",
            )
        except Exception as exc:
            _log(logger, f"[任务退出] 补报终态进度失败: task_id={cid}, exc={exc}", "error")


def _log(logger: Optional[Any], msg: str, level: str = "info") -> None:
    """优先用调用方日志器，缺省回退 print（task_loop 等无 logger 的场景）。"""
    if logger is not None:
        getattr(logger, level, print)(msg)
    else:
        print(msg)


if __name__ == "__main__":
    # 冒烟测试：停运动（move 停止 + 导航取消）→ 补报 → 清池（不真正拒绝新任务）
    import time
    exit_tasks("手动终止")
    
    # print("register:", register_task("test_exit_001", "退出流程冒烟测试"))
    # print("is_busy:", is_busy(), "| active:", list(active_tasks()))
    # exit_tasks("冒烟测试退出", task_id="test_exit_001", reject_new=False)
    # print("is_busy:", is_busy(), "| accepting:", is_accepting_new())
    # time.sleep(0.2)
