"""
待做任务清单的**唯一写者** —— 一次性调用，不常驻。

清单是交互器与编排器共享的一份（`runtime_files/tasks/pending_tasks.yaml`，格式与全部语义在
`plugin_tools/task_board.py`）。两个角色都不再直接写它：各自把改动写成 rolebus 事件
（`task_update_int` / `task_update_orc`）的 `items` 参数，**发件工具在投递事件之前**同步调本模块
落盘——收件方被唤醒时读到的清单因此一定已经含这次改动，不存在「先收到事件、后看到文件」的
竞态。

用法：

    python3 -m plugin_tools.task_manager --apply
    stdin : {"task_id": <str|null>, "items": [{"field": …, "value": …}, …],
             "source": "<角色>", "event_type": "<事件类型>"}
    stdout: {"result": {"task_id": …, "summary": "✅ 已写入 …", "changed": true}}
            或 {"error": "<异常文本>"}

    python3 -m plugin_tools.task_manager --interrupt-executing
    stdout: {"result": {"task_ids": […], "summary": "…"}}
            或 {"error": "<异常文本>"}

    python3 -m plugin_tools.task_manager --expire-stopped
    stdout: 同上（这一支也是 {"result": {"task_ids": […], "summary": "…"}}）

`--interrupt-executing` 不是模型的一条改动，是 **starter.sh 拉起时的一次性收尾**：上一轮角色
系统退出（被收掉、掉电、Ctrl-C）时停在 `executing` 的任务，本轮没有任何人在执行，一律标成
`interrupted`（只改 status，`last_update_time` 原样不动，理由见 task_board.interrupt_executing）。

`--expire-stopped` 同样不是模型的改动，也是 **starter.sh 拉起时的一次性清理**：用户早就说了
「先不做」（status 为 `stopped`）、并且 `last_update_time` 已经超过
configs/configs.py 的 `TASK_STOPPED_EXPIRE_HOURS` 的任务，移出清单、按 task_id 落成同目录的
历史存档（字段不改，理由见 task_board.expire_stopped_tasks）。只搬不删：存档照旧读得到。

**stdout 恰好一个 JSON 值**，与 `plugin_tools/bridge.py` 是同一条协议（dsh 侧对整个 stdout 做
一次 JSON.parse）。工具自身报错不是通道故障：出错也以**退出码 0** + `{"error"}` 返回，让调用方
能区分「写不进去」与「通道坏了」。

互斥就是 task_board 自己的 flock（`LOCK_EX`，**阻塞等待**）：前一次写还没放锁，这里就等着，
不等同于失败、也不重试。本模块因此**不加自己的锁**。

`source` 是**发出这次改动的角色**：除了写 stderr 上的诊断行，它还决定这次改动受哪些限制
（configs/agents.py 的 ROLE_TASK_BOARD_LIMITS，例如编排器不能新建任务；已有任务只有 owner 能改，
见 task_board._reject_owner_write）。因此请求体缺它、或给了个空串都是错，不会按「没有角色」放行。
`event_type` 只用于诊断行。

回执里的 `changed` = 这次改动有没有真的改变清单内容：要写的值本来就是文件里的值时它是 false，
清单**一字未动**——调用方（rolebus 发件工具）据此不投递那次事件：没有值得收件方醒的事，叫醒
一次就是白花它一轮模型。越权与其它校验失败一样，走 `{"error"}` 那一支。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

# 两种入口（`python -m plugin_tools.task_manager` 与 `python plugin_tools/task_manager.py`）
# 都要能 import configs.* / plugin_tools.*：`-m` 会把 cwd 放进 sys.path，直接跑则要靠这里
# 自己找仓库根（与 bridge.py 的引导同款）。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import TASK_STOPPED_EXPIRE_HOURS  # noqa: E402
from plugin_tools import task_board  # noqa: E402  （放在 sys.path 引导之后）


@contextmanager
def _protocol_stdout() -> Iterator[Any]:
    """执行期间把本进程的 stdout 借走，yield 出真实 stdout 供协议 JSON 使用。

    协议是「stdout 恰好一个 JSON 值」，而写盘是**就地执行**的（同一进程、同一 sys.stdout）：
    期间任何一句 print（本模块的、task_board 的、它 import 的库的）都会混进协议。期间
    sys.stdout 指向 sys.stderr，照常 print、日志一行不丢（调用方已把子进程 stderr 透传出来）。
    同款守卫与那次的起因见 bridge.py 的 `_protocol_stdout`。
    """
    real = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield real
    finally:
        sys.stdout = real


def _summary(task: Dict[str, Any], changed: bool) -> str:
    """落盘确认（模型看到的那一句话）。取值与措辞沿用原 `task_write` 工具的返回，不变。"""
    if not changed:
        return (f"⏸ 无变化 {task['task_id']}：要写的值本来就是清单里的值，清单未改动，"
                f"这次也没有通知收件方（没有值得它醒的事）。")
    return (f"✅ 已写入 {task['task_id']}：status={task.get('status', '?')}，"
            f"owner={task.get('owner', '?')}，last_update_time={task.get('last_update_time', '?')}")


def apply_change(request: Dict[str, Any]) -> Dict[str, Any]:
    """把一次 `items` 改动落进清单，返回协议响应体。

    只做两件事：判请求体的形状、把改动交给 `task_board.write`。字段路径、保留字段、取值词表、
    角色限制、终结任务等一切校验都归 task_board，这里**不复制**它们——复制一份就是第二个事实源。

    Args:
        request: `task_id`（字符串或 null，空串视同没给）、`items`（非空的字段改动数组）与
            `source`（发出这次改动的角色，必给）。

    Returns:
        `{"task_id": <最终任务号>, "summary": <模型可见的确认句>, "changed": <bool>}`。changed 为
        False 表示这次改动没有真的改变内容（清单未动），调用方据此不投递事件。

    Raises:
        task_board.TaskBoardError: 请求体形状不对（调用方写错了），或 task_board 拒绝了这次改动
            （取值不在词表里、越权改别人的任务、改已有任务的 owner、任务已终结…）。
    """
    task_id = request.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        raise task_board.TaskBoardError(
            f"task_id 必须是字符串或 null，收到 {type(task_id).__name__}")
    source = request.get("source")
    if not isinstance(source, str) or not source:
        raise task_board.TaskBoardError(
            "source 必须是非空字符串：它是发出这次改动的角色，决定这次改动受哪些限制")
    items = request.get("items")
    if not isinstance(items, list) or not items:
        raise task_board.TaskBoardError("items 必须是非空数组：没有要改的字段就别调本模块")
    # 空串与 None 同样表示「调用方没给号」：task_board 按时间戳发一个新号（本角色不许新建时
    # 由它抛 RoleNotAllowedError）。
    task, changed = task_board.write(task_id or None, items, role=source)
    return {"task_id": task["task_id"], "summary": _summary(task, changed), "changed": changed}


def interrupt_executing() -> Dict[str, Any]:
    """启动收尾：把清单里还在 `executing` 的任务标成 `interrupted`。

    薄薄一层，校验与改文件都在 `task_board.interrupt_executing`（措辞与写盘策略见那里）。

    Returns:
        `{"task_ids": [...], "summary": <一行确认>}`。
    """
    task_ids = task_board.interrupt_executing()
    if not task_ids:
        summary = "清单里没有 executing 的任务，未改动。"
    else:
        summary = f"⚠️ 已把 {len(task_ids)} 条 executing 任务标为 interrupted：{', '.join(task_ids)}"
    return {"task_ids": task_ids, "summary": summary}


def expire_stopped() -> Dict[str, Any]:
    """启动清理：把停了太久的 stopped 任务移出清单、转成历史存档。

    薄薄一层，时限取自 configs/configs.py，校验与改文件都在 `task_board.expire_stopped_tasks`。

    Returns:
        `{"task_ids": [...], "summary": <一行确认>}`。
    """
    task_ids = task_board.expire_stopped_tasks(TASK_STOPPED_EXPIRE_HOURS)
    if not task_ids:
        summary = f"清单里没有停了超过 {TASK_STOPPED_EXPIRE_HOURS} 小时的 stopped 任务，未改动。"
    else:
        summary = (f"🗄 已把 {len(task_ids)} 条停了超过 {TASK_STOPPED_EXPIRE_HOURS} 小时的 stopped "
                   f"任务移入历史存档：{', '.join(task_ids)}")
    return {"task_ids": task_ids, "summary": summary}


def main(argv: Any = None) -> int:
    """命令行入口：一次调用做完一件事，结果按协议写到 stdout。

    Args:
        argv: 参数表；省略时取 `sys.argv[1:]`。

    Returns:
        进程退出码。落盘失败也返回 0（工具自身报错不是通道故障，见模块文档）。
    """
    parser = argparse.ArgumentParser(
        prog="task_manager",
        description="待做任务清单的唯一写者：落盘一次改动（--apply）或做一次启动收尾"
                    "（--interrupt-executing / --expire-stopped），按协议回一条 JSON。",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true",
                      help="把 stdin 的请求 JSON 落进待做任务清单。")
    mode.add_argument("--interrupt-executing", action="store_true",
                      help="把清单里还在 executing 的任务标成 interrupted（只改 status；"
                           "starter.sh 拉起时用）。")
    mode.add_argument("--expire-stopped", action="store_true",
                      help=f"把停了超过 {TASK_STOPPED_EXPIRE_HOURS} 小时仍停在 stopped 的任务"
                           f"移出清单、转成历史存档（starter.sh 拉起时用）。")
    args = parser.parse_args(argv)

    with _protocol_stdout() as out:
        try:
            if args.interrupt_executing or args.expire_stopped:
                result = interrupt_executing() if args.interrupt_executing else expire_stopped()
                # 每个 case 都往 stderr 打一行：调用方（starter.sh）把 stdout 当协议丢掉，
                # 这一行就是它在终端上留下的全部痕迹——改没改都该看得见。
                print(f"[task_manager] 启动收尾：{result['summary']}", file=sys.stderr)
            else:
                request = json.loads(sys.stdin.read() or "")
                if not isinstance(request, dict):
                    raise task_board.TaskBoardError(
                        f"请求体必须是对象，收到 {type(request).__name__}")
                result = apply_change(request)
                print(f"[task_manager] {request.get('source', '?')} → "
                      f"{request.get('event_type', '?')}：{result['summary']}", file=sys.stderr)
            print(json.dumps({"result": result}, ensure_ascii=False), file=out)
        except Exception as exc:  # noqa: BLE001 —— 协议要求把任何失败都变成一条 {"error"}
            traceback.print_exc(file=sys.stderr)
            error = f"{type(exc).__name__}: {exc}"
            # 调用方不一定读 stdout（starter.sh 就把它丢掉），所以失败也在 stderr 上留一行。
            print(f"[task_manager] 失败：{error}", file=sys.stderr)
            print(json.dumps({"error": error}, ensure_ascii=False), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
