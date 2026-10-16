"""
待做任务清单（task board）—— 交互器与编排器共享的一份。

清单是磁盘上的两份文件，都在 runtime_files/tasks/ 下（路径常量见 configs/paths.py）：
  * RUNTIME_PENDING_TASKS_FILE（pending_tasks.yaml）：只装**未终结**的任务（status 不在
    TERMINAL_TASK_STATUS 里），整份原子覆盖（独立锁文件 flock + tmp+rename）。
  * RUNTIME_TASKS_DIR/<task_id>.yaml：**已从清单里出去**的任务，一条一份，文件名即 task_id。
    出去有两条路：终结（取值见 TERMINAL_TASK_STATUS —— 其中 cancelled 是用户明确取消，写进来的
    同时就落档走人），或 stopped 停得太久被启动清理移出（见 expire_stopped_tasks）。出去的任务
    不再回到清单里，所以这些文件只增不减。**stopped / interrupted 本身都不算终结**
    （见 TERMINAL_TASK_STATUS 处的说明）：它们留在清单里，等着被接着做。

清单的**唯一写者**是 `plugin_tools/task_manager.py`（一次性 CLI，模型手里没有写工具）：两个
角色都只能把改动装进 rolebus 事件的 `items` 参数（交互器发 `task_update_int`、编排器发
`task_update_orc`），发件工具在**投递事件之前**同步调 task_manager 落盘——收件方被唤醒时读到
的清单必然已含这次改动。本模块的 write 是 task_manager 的下层实现，模型永远不直接调它。
**两边读的方式不同**：交互器经 `task_list` 工具按需读 —— 参数 task_range 选读哪一批（待做 =
清单一份、历史 = 已出清单的存档）、task_id 选读整批还是那一条：不带号时待做给 read_pending_text 的
**整份原文**、历史给 read_history_text 的**汇总**（这时 task_range 才起作用），带了号就是
read_task_text 给的**那一条**（先在清单里找、找不到再翻同目录的存档 —— 与 task_range 无关，故这个
工具也是读历史任务的那条路）；编排器手里没有
task_list 工具，由 `@embody/dsh-task-context` 每轮把清单**全文**注入模型输入（原样照搬、不筛；
谁该做哪条由条目上的 owner 交给模型判断）。另有一处**挑出来再注入**的读：交互器的开机自查那一
轮，注入的是 boot_check_text() 生成的块（先 interrupted、否则 pending / stopped）。后两处的
挑法都写在代码里，不随模型的心情漂。
本模块没有内存态：**一次调用就是一次「持锁 → 读 → 在副本上改 → 原子写 → 放锁」**，返回即
已落盘，没有需要调用方记得打检查点的缓冲。

除 write 外会改这份文件的只有两处，都是 starter.sh 拉起时的一次性动作、都经 task_manager 调用：
`interrupt_executing()` 把还停在 `executing` 的任务标成 `interrupted`（上一轮退出时没做完的），
只动 `status` 一个字段、**并且刻意不碰 `last_update_time`**；`expire_stopped_tasks()` 把停了太久的
`stopped` 任务移出清单、转成历史存档，那条任务本身一个字段不改（时间戳原样带走），两处都不算
「一次改动」。

**时间戳是本模块的账，调用方写不了也不作数**：`last_update_time`（任务级）与步骤的 `update_time`
都在这里盖成当前时刻，**清单每被改动一次就盖一次**（盖点见 `_stamp`，落盘出口见 `_save_pending`）。
调用方（模型、事件）传进来的时间值一律不算数：任务级的三个字段写它直接报错（`_TASK_RESERVED`），
步骤的 `update_time` 传了也会被盖掉（见 `_touch_step`）。唯一的例外就是上面那条
`interrupt_executing()` —— 那是系统在收上一轮的执行痕迹，不是一次改动；`expire_stopped_tasks()`
同理（它只是把一条没被改动的任务挪个地方）。两个例外的理由都写在各自的函数里。

格式（清单里是单层列表，执行记录是任务对象上的顶层键，编号就在键名里）：

    tasks:
      - task_id: local-260923154400
        description: 去客厅找一下遥控器
        status: executing
        owner: interactor
        create_time: '2026-09-23T15:44:00+08:00'
        last_update_time: '2026-09-23T15:46:12+08:00'
        step1:
          description: 走到客厅
          tool: waypoint_nav
          result: 已到达客厅
          attempts: 1
          update_time: '2026-09-23T15:45:02+08:00'

字段改动的唯一入口是 write(task_id, data)：data 是一组 {"field": …, "value": …}（点分字段
路径，逐段下钻字典，例如 description、status、step1、step1.result）。

write 不区分增和改：**字段已存在就改值，不存在就补出来**。只有叶子这样——路径中间那些段
必须已经存在，不会凭空造出嵌套层，所以 write(T, [{"field": "step99.status", …}]) 报错而不是
造一个空壳步骤。**新步骤要整段写进去**（field 写 `step`，value 给一个对象），写完之后才能按
`step1.result` 这样的路径改它。task_id 不在清单里时 write 就地建任务（create_time 由模块补）。

**任务号只出自两处，模型不编号**：云端下发的号随信封头进来、由调用方原样传下（改已有任务
时也照抄清单里读到的号）；云端没给号的新任务，调用方**不给 task_id**，由 write 自己发一个
`local-<年月日时分秒>`（清单里与已终结文件里都没用过的那个，见 _mint_task_id）。

模块自己记账、调用方写不了的字段（见 _TASK_RESERVED / _STEP_RESERVED）：
task_id / create_time / last_update_time，以及步骤的 attempts —— 写它们直接报错。步骤的
`update_time` 归的是同一个账、走的是另一条路：调用方可以传，**但传什么都不作数**，模块一律盖成
当前时刻（见 _touch_step）。**owner 也只在新建时给**：一条任务归谁做在建它的时候就定了，之后
谁都改不了（见 _reject_owner_field）。

**已有的任务只有 owner 能改**（见 _reject_owner_write）：非 owner 一个字段也写不进——唯一的窄口子
是交互器能把**归编排器的任务**的 status 改成 executing：那是它叫起那个角色的唯一手段（它手里没有
别的唤醒通道），别的字段、以及 status 的其它取值都写不了（要暂停 / 取消，它在 `task_update_int`
的 description 里说事，状态由编排器自己改）。此外还有**按角色的限制**（见 configs/agents.py
的 ROLE_TASK_BOARD_LIMITS）：编排器不能新建任务。调用方角色经 write 的 role 参数进来（rolebus
发件工具填的是事件的 source），不给角色的调用是系统通道，不受这些限制。

write 还会回报**这次改动有没有真的改变内容**：要写的值本来就等于文件里的值（按 JSON 值比，模块
自己记账的时间戳与 attempts 不算）时，清单一字不动、返回值里的 changed 为 False —— 调用方据此
不叫醒收件方（空叫醒一次是白花对方一轮模型）。

状态只有任务级一处：`status` 是任务上的字段（TASK_STATUS），**步骤没有状态**——一条执行
记录做到哪一步看它自己的 result 与 update_time。

依赖：PyYAML（见 plugin_tools/requirements.txt）。
"""

from __future__ import annotations

import fcntl
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import yaml

# 与 plugin_tools/bridge.py 一致：把仓库根（embody/）放进搜索路径，这样
# `python -m plugin_tools.task_board` 与 `python plugin_tools/task_board.py`
# 直接执行两种入口都能 import configs.*。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.agents import ROLE_TASK_BOARD_LIMITS, TASK_OWNER, TASK_STATUS  # noqa: E402
from configs.paths import (  # noqa: E402
    RUNTIME_PENDING_TASKS_FILE,
    RUNTIME_PENDING_TASKS_LOCK,
    RUNTIME_TASKS_DIR,
    runtime_task_file,
)

# 状态取值不在这里写死第二份：configs/agents.py 的 TASK_STATUS / TASK_OWNER
# 是事实源，这里只把它们的 value 抽出来。终结集合按字典里的键名取，也不重抄一遍取值。
TASK_STATUS_VALUES: Tuple[str, ...] = tuple(spec["value"] for spec in TASK_STATUS.values())
TASK_OWNER_VALUES: Tuple[str, ...] = tuple(spec["value"] for spec in TASK_OWNER.values())
# 系统专用状态（configs/agents.py 里标了 system_only 的那些）：只有本模块的初始化动作会写
# （interrupt_executing），模型经 items 写它会被 _check_status 拒掉。两个集合都由 TASK_STATUS
# 现算，不在这里另列一份名字。
SYSTEM_ONLY_STATUS_VALUES: Tuple[str, ...] = tuple(
    spec["value"] for spec in TASK_STATUS.values() if spec.get("system_only")
)
WRITABLE_STATUS_VALUES: Tuple[str, ...] = tuple(
    value for value in TASK_STATUS_VALUES if value not in SYSTEM_ONLY_STATUS_VALUES
)
# **终结的有三个：succeeded / failed / cancelled**，一写进来就落成同目录的单文件存档、从清单
# 移除（见 write 的 _is_terminal 分支）。cancelled 与 stopped 的分界全在「还会不会接着做」：
# cancelled 是用户明确取消 / 终止（明确不再需要继续执行），stopped 是用户暂停 / 急停——还可能
# 接着做。
# stopped / interrupted 都不算终结：前者是「用户暂停 / 急停」，后者是「系统退出时还没做完」
# （见 interrupt_executing），两者都可能被重新拾起（交互器提示词的「不做/以后再说的置 stopped」、
# 开机自查里的「要不要接着做」都依赖它还留在清单里），移出去就等于把「以后再说」变成「以后说不
# 成」——重启回来读不到，编号也已被存档文件占住、写不进新状态。**stopped 只是留得有时限**
# （TASK_STOPPED_EXPIRE_HOURS，见 expire_stopped_tasks），那是「先不做」的时限到了，不是把它判
# 成终结——搬进存档时 status 仍是 stopped。
# 留下的四个状态（pending / executing / stopped / interrupted）是清单里
# **还在场**的全部活任务——不等于每轮都摆到每个角色眼前：交互器的 task_list 与开机自查看得到
# 它们，编排器每轮拿到的是清单全文（@embody/dsh-task-context）。
TERMINAL_TASK_STATUS: Tuple[str, ...] = tuple(
    TASK_STATUS[key]["value"] for key in ("SUCCEEDED", "FAILED", "CANCELLED")
)

# 「读哪一批」（task_list 工具的 task_range）：`recent` = 清单里还没终结的（TERMINAL_TASK_STATUS
# 之外的那四个状态），`history` = 已经出清单、单独落盘的。取值是**模型可见**的，plugin_tools/
# available_tools.yaml 里 task_list 的 enum 与这里的字面量必须逐字一致（那份是给模型的 JSON Schema，
# 搬不进来、也没法引用这里）。**省略 / 空串 = recent**，即这个工具从前的行为。
TASK_RANGE_PENDING = "recent"
TASK_RANGE_HISTORY = "history"
TASK_RANGE_VALUES: Tuple[str, ...] = (TASK_RANGE_PENDING, TASK_RANGE_HISTORY)

# 自动编号用的步骤键：step1 / step2 / …（编号嵌在键名里，不是列表）
_STEP_KEY_RE = re.compile(r"^step(\d+)$")
# 自动编号的写法：字段写裸 step（step / step.<子路径>）
_AUTO_STEP = "step"
# 字段名
_STATUS = "status"
_OWNER = "owner"
_ATTEMPTS = "attempts"

# 交互器的角色名（取值即它自己那部分任务的 owner 值，见 configs/agents.py 的 TASK_OWNER，不另抄
# 一份字面量），以及它唯一能写进**别人**任务的取值（TASK_STATUS 的 EXECUTING，同样现取）：清单
# 的分配者只在「叫起归编排器的任务」这一件事上越过 owner 边界，见 _reject_owner_write。
_INTERACTOR = TASK_OWNER["INTERACTOR"]["value"]
_INTERACTOR_WAKE = TASK_STATUS["EXECUTING"]["value"]

# 任务字段与步骤字段在文件里的排列顺序（其余键跟在后面，保持写入顺序）。YAML 映射本身
# 无序，但这份文件是模型要读的，每次落盘同一个顺序更省事。
_TASK_FIELD_ORDER = ("task_id", "description", "status", "owner", "create_time", "last_update_time")
_STEP_FIELD_ORDER = ("description", "tool", "result", "attempts", "update_time")

# 调用方不能经 data 写的字段：模块自己记账。深度 1 的这三个是任务级，attempts 是步骤级。
_TASK_RESERVED = ("task_id", "create_time", "last_update_time")
_STEP_RESERVED = (_ATTEMPTS,)

# task_id 会直接当文件名用，调用方传来的号来自模型输出 —— 必须限定在白名单字符内，否则是一
# 条路径穿越。首字符必须是字母数字，所以 `.` / `..` / 隐藏文件名都进不来；`..` 另判一次是防
# `a..b`。（自己发的号 `local-<年月日时分秒>` 也在白名单内。）
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# 自己发号（_mint_task_id）的格式：local-<年月日时分秒>，本地时间。已终结过的号不能再用，所以
# 号被占用时往后推一秒重试；上限只是把病态情况变成一条明确的报错，不是长度限制。
_MINT_FORMAT = "local-%y%m%d%H%M%S"
_MINT_MAX_TRIES = 24 * 60 * 60


class TaskBoardError(Exception):
    """任务清单的领域错误基类。调用方按它一类捕获即可。"""


class TaskBoardCorruptError(TaskBoardError):
    """清单文件无法解析或结构非法。绝不当作空清单继续写。"""


class FieldNotFoundError(TaskBoardError):
    """字段路径的中间段指不到：不存在，或不是映射、不能再往下钻。"""


class InvalidStatusError(TaskBoardError):
    """状态取值不在 TASK_STATUS 里。"""


class InvalidOwnerError(TaskBoardError):
    """owner 取值不在 TASK_OWNER 里，或新建任务时没给 owner。"""


class InvalidTaskIdError(TaskBoardError):
    """task_id 不能当文件名用（空、含路径分隔符或非法字符）。"""


class ReservedFieldError(TaskBoardError):
    """字段由模块记账，调用方不能经 data 写。"""


class RoleNotAllowedError(TaskBoardError):
    """本角色不许做这次写入（编排器不能新建任务，也不能改 owner）。"""


class TaskTerminalError(TaskBoardError):
    """任务已终结（移入单文件），不再接受写操作。"""


class InvalidTaskRangeError(TaskBoardError):
    """task_range 取值不在 TASK_RANGE_VALUES 里（参数错，不是「没找到」）。"""


def _now() -> str:
    """当前本地时间，带时区偏移的 ISO 8601（秒级）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp(task: Dict[str, Any]) -> None:
    """盖上「最后更新时间」—— 任务级时间戳（`last_update_time`）唯一的写入点。

    **每一次改动落盘都盖**，所以这个字段永远等于「这条任务最后一次被改动的时刻」；调用方
    写不了它（`_TASK_RESERVED` 直接拒），也不接受调用方传值。唯一的例外是启动收尾把 status
    改成 interrupted —— 那次调用不经过这里（见 interrupt_executing）。
    """
    task["last_update_time"] = _now()


def _copy(value: Any) -> Any:
    """深拷贝一份纯 JSON 值，避免调用方拿到的对象与清单里的对象共享引用。"""
    return json.loads(json.dumps(value, ensure_ascii=False))


def _parse_time(value: Any) -> Optional[datetime]:
    """解析模块自己写的时间戳（`_now()` 的带时区 ISO 8601），读不出来的返回 None。

    没有时区的老写法按本机时区解释（`astimezone`），不从别处猜。

    Returns:
        带时区的 datetime；值不是字符串或解析不了时是 None —— 调用方（expire_stopped_tasks）
        把 None 当作「判不出去什么时候更新的」，因而不会搬走那条任务。
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def is_blank_task_id(task_id: Any) -> bool:
    """调用方是不是「没给任务号」（未提供、或给了空串/空白串）。

    Args:
        task_id: 调用方给的 task_id；None 表示没给这个参数。

    Returns:
        True 表示这个号由模块自己发（见 _mint_task_id）。
    """
    return task_id is None or (isinstance(task_id, str) and not task_id.strip())


def _mint_task_id(tasks: Sequence[Dict[str, Any]]) -> str:
    """发一个没用过的任务号：`local-<年月日时分秒>`（本地时间），被占用就往后推一秒。

    占用包括两处：清单里还在的任务、以及同目录已终结的 `<task_id>.yaml` —— 后者也必须绕开，
    因为「已终结过的任务号不能再用」（复用会让 write 直接抛 TaskTerminalError，等于这次新建
    失败）。调用方已持写锁，所以这里看到的是一份不会变的清单。

    Args:
        tasks: 当前清单里的任务（已持锁读到的那一份）。

    Returns:
        可用的新任务号。

    Raises:
        TaskBoardError: 连着一个整天的时间戳都被占用（只可能是清单被灌满了人写的号）。
    """
    taken = {str(task.get("task_id") or "") for task in tasks}
    moment = datetime.now()
    for _ in range(_MINT_MAX_TRIES):
        candidate = moment.strftime(_MINT_FORMAT)
        if candidate not in taken and not runtime_task_file(candidate).exists():
            return candidate
        moment += timedelta(seconds=1)
    raise TaskBoardError(f"发不出任务号：{_MINT_MAX_TRIES} 个连续时间戳都被占用")


def check_task_id(task_id: Any) -> str:
    """校验 task_id 能当文件名用，并原样返回它。

    Args:
        task_id: 模型给的（或事件带来的）任务编号。

    Returns:
        校验通过的 task_id。

    Raises:
        InvalidTaskIdError: 不是非空字符串，或含白名单外的字符。
    """
    if not isinstance(task_id, str) or not task_id:
        raise InvalidTaskIdError(f"task_id 必须是非空字符串，收到 {task_id!r}")
    if not _TASK_ID_RE.match(task_id) or ".." in task_id:
        raise InvalidTaskIdError(
            f"task_id {task_id!r} 含非法字符（只允许字母、数字、下划线、点、连字符，"
            f"且首字符必须是字母或数字）"
        )
    return task_id


def check_task_range(task_range: Any) -> Optional[str]:
    """校验「读哪一批」的取值，返回规范化后的结果。

    Args:
        task_range: 模型给的 task_range；None、空串、空白串表示没给（不限定哪一批）。

    Returns:
        None（没给），或 TASK_RANGE_VALUES 里的那一个。

    Raises:
        InvalidTaskRangeError: 给了值但不在 TASK_RANGE_VALUES 里。
    """
    if task_range is None or (isinstance(task_range, str) and not task_range.strip()):
        return None
    if task_range not in TASK_RANGE_VALUES:
        raise InvalidTaskRangeError(
            f"task_range {task_range!r} 不是可选值之一：{' / '.join(TASK_RANGE_VALUES)}"
        )
    return task_range


def _split_field(field: Any) -> List[str]:
    """把点分字段路径拆成段，拒绝空串与空段。"""
    if not isinstance(field, str) or not field.strip():
        raise FieldNotFoundError(f"字段路径必须是非空字符串，收到 {field!r}")
    segments = field.split(".")
    if any(segment == "" for segment in segments):
        raise FieldNotFoundError(f"字段路径 {field!r} 有空段")
    return segments


def _iter_data(data: Any) -> Iterator[Tuple[str, Any]]:
    """把调用方给的 data 收成 (field, value) 序列。

    data 的形状是工具参数与 rolebus 事件共用的那一份（事件里这个参数叫 `items`，形状同款）：
    一个数组，每项
    ``{"field": <点分路径>, "value": <任意 JSON>}``。value 必须显式给出（可以是 null，
    但不能缺键）—— 缺键与「写一个 null」是两件事，前者是调用方写错了。

    Args:
        data: 调用方给的 data。

    Yields:
        (field, value)。

    Raises:
        TaskBoardError: data 不是数组，或某一项不合形状。
    """
    if not isinstance(data, list):
        raise TaskBoardError(f'data 必须是数组，收到 {type(data).__name__}')
    for index, item in enumerate(data):
        if not isinstance(item, dict) or "field" not in item or "value" not in item:
            raise TaskBoardError(f'data[{index}] 必须是 {{"field": …, "value": …}}，收到 {item!r}')
        field = item["field"]
        if not isinstance(field, str):
            raise TaskBoardError(f"data[{index}].field 必须是字符串，收到 {field!r}")
        yield field, item["value"]


def _reject_reserved(segments: List[str], value: Any) -> None:
    """挡住由模块记账的字段：任务级三个，以及步骤的 attempts。

    Args:
        segments: 字段路径的段。
        value: 要写入的值。

    Raises:
        ReservedFieldError: 路径落在保留字段上（整段写步骤时 value 里带了 attempts 也算）。
    """
    if len(segments) == 1:
        if segments[0] in _TASK_RESERVED:
            raise ReservedFieldError(
                f"字段 {segments[0]!r} 由模块记账，不能直接写（改它没有任何效果）"
            )
        if _STEP_KEY_RE.match(segments[0]) and isinstance(value, dict):
            for field in _STEP_RESERVED:
                if field in value:
                    raise ReservedFieldError(
                        f"步骤里的 {field!r} 由模块记账，不能直接写（{segments[0]}）"
                    )
    elif (len(segments) == 2 and _STEP_KEY_RE.match(segments[0])
          and segments[1] in _STEP_RESERVED):
        raise ReservedFieldError(
            f"字段 {'.'.join(segments)!r} 由模块记账，不能直接写"
        )


def _role_limits(role: Any) -> Dict[str, Any]:
    """取这次写入的调用方角色的限制（表与理由见 configs/agents.py 的 ROLE_TASK_BOARD_LIMITS）。

    Args:
        role: 调用方角色名；None / 空串表示系统通道。

    Returns:
        该角色的限制（现在只有 `no_create` 一项）；没有角色的调用、以及表里没有的角色都返回
        空字典——不受额外限制。
    """
    if not role:
        return {}
    return ROLE_TASK_BOARD_LIMITS.get(str(role), {})


def _reject_role_create(task_id: Any, role: Any, limits: Dict[str, Any]) -> None:
    """本角色不许新建时拦住新建——**空号与清单里没有的号都是新建**。

    号不在清单里就建一条，是 write 的默认语义；对不许新建的角色（编排器）它正是「照抄一个
    事件头带来的号、结果建出一条重复任务」那条路，所以两种情形都拦。

    Args:
        task_id: 目标号；空号表示调用方没给号（要发新号）。
        role: 调用方角色名，用在报错里。
        limits: `_role_limits` 取到的本角色限制。

    Raises:
        RoleNotAllowedError: 本角色不许新建任务。
    """
    detail = "没给号就是要发一个新号" if is_blank_task_id(task_id) else f"清单里没有 {task_id}"
    raise RoleNotAllowedError(f"角色 {role!r} 不能新建任务：{limits['no_create']}（{detail}）")


def _reject_owner_write(
    segments: List[str], value: Any, task_owner: Any, role: Any, task_id: str
) -> None:
    """挡住对**别人的任务**的写入：已有的任务归谁做，就只有谁能改。

    新建任务不受这条限制（那时还没有 owner 这回事，见 write 的 is_new 分支）。编排器**一个
    字段也写不了**别人的任务：它手里只有 `task_update_orc`，而那条事件的收件方是交互器。
    交互器对归编排器的任务只有**一条窄口子**：把它的 status 改成 executing（见
    `_INTERACTOR_WAKE`）——它手里没有别的唤醒通道，这是它叫起那个角色的唯一手段。**只有这一个
    字段、这一个取值**：别的字段（description / stepN …）与 status 的其它取值（含 stopped）都
    写不了，暂停 / 取消靠发 `task_update_int` 的 description 说事，状态仍归 owner 维护。报错里
    把越权的理由说全，让发件方能直接回给模型。

    Args:
        segments: 字段路径的段。
        value: 要写入的值（判「改的是不是 executing」要看它）。
        task_owner: 目标任务的 owner（清单里那条的 owner 值）。
        role: 调用方角色名。
        task_id: 目标任务号，用在报错里。

    Raises:
        RoleNotAllowedError: 这次写入超出了调用方对这条任务的权限。
    """
    owner = str(task_owner or "")
    if owner == str(role or ""):
        return
    field = segments[0] if segments else ""
    who = f"归 {owner} 做" if owner else "没有标 owner"
    if role == _INTERACTOR and segments == [_STATUS] and value == _INTERACTOR_WAKE:
        return
    if role == _INTERACTOR:
        raise RoleNotAllowedError(
            f"越权：任务 {task_id} {who}，只有 owner 能改它 —— 你唯一能写的是把它的 status 改成"
            f" {_INTERACTOR_WAKE!r}（叫起它），不能写 {field!r}"
            f" —— 这次改动整条被拒、也没有通知收件方"
        )
    raise RoleNotAllowedError(
        f"越权：任务 {task_id} {who}，只有 owner 能改它，你一个字段也不能写 {field!r}"
        f" —— 这次改动整条被拒、也没有通知收件方"
    )


def _reject_owner_field(segments: List[str], is_new: bool) -> None:
    """挡住对已有任务 owner 字段的写入：owner **只在新建时给**。

    Args:
        segments: 字段路径的段。
        is_new: 这次写入是不是在建任务（建任务时 owner 必给）。

    Raises:
        RoleNotAllowedError: 想改一条已有任务的 owner。
    """
    if not is_new and len(segments) == 1 and segments[0] == _OWNER:
        raise RoleNotAllowedError(
            "owner 只在新建任务时给：这条任务归谁做在建它的时候就定了，之后谁都改不了"
            "（要换人只有停掉这条、另建一条给那个人）"
        )


# 清单里没有这个键（区分「值是 None」与「字段还不存在」）。
_MISSING = object()


def _changes_content(segments: List[str], previous: Any, value: Any) -> bool:
    """这次赋值会不会真的改变清单内容（模块自己记账的时间戳与 attempts 不算）。

    比的是**要写的值**与当前值：相等就是「已经是要修改的值」，调用方据此不必叫醒收件方。
    两个刻意的判断：

    - 整段写一步（`step1`，或经 _normalize 编号的裸 `step`）**一律算变化**：attempts 会 +1，
      值一样也是「又启动一次」，那正是要通知出去的事；
    - 比不了就当变了（清单里手工编辑进去的裸日期之类不是 JSON 值）：宁可多叫醒一次，也不要
      漏掉一次真实改动。

    Args:
        segments: 字段路径的段（已 _normalize）。
        previous: 赋值前该叶子位置的值；`_MISSING` 表示还没有这个字段。
        value: 这次要写入的值。

    Returns:
        True 表示这次写入确实改变了任务内容。
    """
    if len(segments) == 1 and _STEP_KEY_RE.match(segments[0]):
        return True
    if previous is _MISSING:
        return True
    try:
        return (json.dumps(previous, sort_keys=True, ensure_ascii=False)
                != json.dumps(value, sort_keys=True, ensure_ascii=False))
    except TypeError:
        return True


def _normalize(segments: List[str], task: Dict[str, Any]) -> List[str]:
    """把裸 step 换成自动编号的 step<N>。"""
    if segments and segments[0] == _AUTO_STEP:
        return [_next_step_key(task)] + segments[1:]
    return segments


def _next_step_key(task: Dict[str, Any]) -> str:
    """下一个自动编号的步骤键：现有最大编号 + 1（删掉的编号留空洞、不复用）。"""
    numbers = [int(_STEP_KEY_RE.match(key).group(1)) for key in task if _STEP_KEY_RE.match(key)]
    return f"step{max(numbers, default=0) + 1}"


def _descend(
    task: Dict[str, Any],
    segments: List[str],
    task_id: str,
) -> Tuple[Dict[str, Any], str]:
    """走到路径的父节点，返回 (父字典, 叶子键)。叶子不做存在性检查，由调用方决定增还是改。

    Args:
        task: 任务对象。
        segments: 字段路径的段。
        task_id: 任务编号，只用于报错。

    Returns:
        (父字典, 叶子键)。

    Raises:
        FieldNotFoundError: 中间段指不到，或中间段不是映射。
    """
    parent: Dict[str, Any] = task
    for segment in segments[:-1]:
        child = parent.get(segment)
        if child is None:
            raise FieldNotFoundError(
                f"字段 {'.'.join(segments)!r} 指不到：{segment!r} 不存在（task {task_id}）。"
                f"新步骤要整段写：field 写 `step`、value 给一个对象。"
            )
        if not isinstance(child, dict):
            raise FieldNotFoundError(
                f"字段 {'.'.join(segments)!r} 指不到：{segment!r} 不是映射，不能再往下钻"
                f"（task {task_id}）"
            )
        parent = child
    return parent, segments[-1]


def _touch_step(
    task: Dict[str, Any],
    segments: List[str],
    *,
    carried_attempts: Optional[int] = None,
) -> None:
    """路径落在某一步上时，盖该步的 update_time、记 attempts。

    步骤是开放字典，但这两项由模块保证：没有 update_time 则渲染不出时间；attempts 是
    「这一步启动了几次」，模型自己数不可靠。

    **update_time 一律盖成当前时刻**：调用方（模型）写整段步骤时常常把读过的那份原样抄回来，
    尊重那次抄来的值会让一条步骤的时间戳停在过去——于是「这一步最后一次被改是什么时候」失真，
    还会出现任务级 `last_update_time` 早于它自己步骤的情况。时间戳是本模块的账，调用方传进来
    的值不作数。

    Args:
        task: 任务对象。
        segments: 字段路径的段；首段是 stepN 时才动。
        carried_attempts: 整段重写某一步时，由 _carry_attempts 在赋值前数出来的新计数；
            None 表示这次不是整段重写（新步骤按「已启动一次」算，已有计数不动）。
    """
    if not segments or not _STEP_KEY_RE.match(segments[0]):
        return
    step = task.get(segments[0])
    if not isinstance(step, dict):
        return
    step["update_time"] = _now()
    if carried_attempts is None:
        step.setdefault(_ATTEMPTS, 1)
    else:
        step[_ATTEMPTS] = carried_attempts


def _carry_attempts(task: Dict[str, Any], segments: List[str], value: Any) -> Optional[int]:
    """整段重写一条**已有**步骤（路径就落在 stepN 上、value 给对象）= 这一步又启动了一次。

    必须在赋值**之前**调：赋值会把这一步整个换成调用方给的新对象，旧计数随之消失
    （调用方写不了 attempts，新对象里没有它），数出来的值由 _touch_step 装回去。

    Args:
        task: 任务对象。
        segments: 字段路径的段。
        value: 要写入的值。

    Returns:
        这一步重写后的 attempts；不是「重写已有步骤」时返回 None。
    """
    if len(segments) != 1 or not _STEP_KEY_RE.match(segments[0]) or not isinstance(value, dict):
        return None
    step = task.get(segments[0])
    if not isinstance(step, dict):
        return None
    return int(step.get(_ATTEMPTS) or 0) + 1


def _check_status(segments: List[str], value: Any) -> None:
    """校验任务级 status 取值（查 TASK_STATUS）。

    状态只有任务级一处：路径就是 `status` 时才校验；步骤里没有状态，更深的路径
    （step1.status 之类）不以状态论处，不校验。

    系统专用取值（SYSTEM_ONLY_STATUS_VALUES）单独报错：它是**合法取值但模型写不了**，与
    写错一个词的报错必须分得清（前者是「这件事不该由你做」，后者是「拼错了」）。

    Args:
        segments: 字段路径的段。
        value: 要写入的值。

    Raises:
        InvalidStatusError: 取值不在 TASK_STATUS 里，或它是系统专用的取值。
    """
    if segments != [_STATUS]:
        return
    if value in SYSTEM_ONLY_STATUS_VALUES:
        raise InvalidStatusError(
            f"任务状态 {value!r} 由系统在启动时标（上一轮没做完的任务），模型不能写；"
            f"能写的是: {' / '.join(WRITABLE_STATUS_VALUES)}"
        )
    if value not in TASK_STATUS_VALUES:
        raise InvalidStatusError(
            f"任务状态 {value!r} 非法，合法取值: {' / '.join(WRITABLE_STATUS_VALUES)}"
        )


def _check_owner(segments: List[str], value: Any) -> None:
    """校验 owner 取值。

    Args:
        segments: 字段路径的段。
        value: 要写入的值。

    Raises:
        InvalidOwnerError: 取值不在 TASK_OWNER 里。
    """
    if segments != [_OWNER] or value in TASK_OWNER_VALUES:
        return
    raise InvalidOwnerError(f"owner {value!r} 非法，合法取值: {' / '.join(TASK_OWNER_VALUES)}")


def _is_terminal(task: Dict[str, Any]) -> bool:
    """任务是否已终结。"""
    return task.get(_STATUS) in TERMINAL_TASK_STATUS


def _reorder(mapping: Dict[str, Any], order: Sequence[str]) -> Dict[str, Any]:
    """按 order 重排映射的键，order 里没提到的键保持原有顺序跟在后面。"""
    ordered: Dict[str, Any] = {key: mapping[key] for key in order if key in mapping}
    ordered.update((key, value) for key, value in mapping.items() if key not in ordered)
    return ordered


def _ordered(task: Dict[str, Any]) -> Dict[str, Any]:
    """按固定顺序重排一条任务的字段与各步骤的字段。"""
    steps = {key: value for key, value in task.items()
             if _STEP_KEY_RE.match(key) and isinstance(value, dict)}
    ordered: Dict[str, Any] = {key: task[key] for key in _TASK_FIELD_ORDER if key in task}
    ordered.update((key, value) for key, value in task.items()
                   if key not in ordered and key not in steps)
    for key, step in steps.items():
        ordered[key] = _reorder(step, _STEP_FIELD_ORDER)
    return ordered


# ─────────────────────────────────────────────────────────────────────────────
# 读写
# ─────────────────────────────────────────────────────────────────────────────


def read_pending_text() -> str:
    """读待做任务清单的**原文**，供 task_list 工具原样返回（编排器侧读同一份文件，但走的是
    @embody/dsh-task-context 的每轮注入，不经本函数）。

    **不加锁**：写盘走 tmp+rename 原子换，读到的必然是一份完整的文件（要么是改写前的、
    要么是改写后的），不会读到半截。

    Returns:
        清单文件的原文；文件不存在或为空时返回一句提示，不抛错
        —— 「还没有任何待做任务」是正常状态，不是异常。
    """
    try:
        text = RUNTIME_PENDING_TASKS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (f"待做任务清单还不存在（还没有任何待做任务）：{RUNTIME_PENDING_TASKS_FILE}\n"
                f"要建第一条任务，发一条 rolebus_send_task_update_int（items 里给 description 与 "
                f"owner，不带 task_id —— 云端没给号的任务由系统发号）。")
    except OSError as exc:
        return f"待做任务清单读取失败：{RUNTIME_PENDING_TASKS_FILE}：{exc}"
    if not text.strip():
        return f"待做任务清单是空的（没有任何待做任务）：{RUNTIME_PENDING_TASKS_FILE}"
    return text


def read_task_text(task_id: str) -> str:
    """读**一条**任务的内容：先在近期待做清单里找，找不到再翻同目录的**历史完结存档**。

    这就是 `task_list` 填了 task_id 时走的那条路（**填了号就跟 task_range 无关**：那两批是递进的
    两站，不是二选一。不带号才由 task_range 决定给哪一批，见 read_pending_text / read_history_text）。
    两处都是只读：不加锁、不写盘，读到的必然是一份完整文件（写盘走 tmp+rename）。

    清单优先，且只给那一条：清单里装的是**活任务**，
    存档是任务移出清单那一刻落下的那一份（见 write）。两种命中读到的都是**同一种东西**：顶层映射、
    写者的字段序，与存档文件的写法一致。**别拿存档反推它「终结」了**：判定按 status（见
    TERMINAL_TASK_STATUS），而目录里留着的旧存档可能带着早已不用的写法。

    存档按文件名精确匹配：号即 `<task_id>.yaml`（runtime_task_file），不扫目录、不认 `.yaml`
    后缀。所以 task_id 必须先过 check_task_id —— 号直接当文件名用，带 `/` 或 `..` 的就是路径穿越。

    Args:
        task_id: 任务编号，取值受 check_task_id 约束。

    Returns:
        清单命中时是该条任务的 YAML 内容；存档命中时是「在历史任务中找到该任务」加存档原文；
        两站都没有时是「在近期待做与历史完结任务库中都未找到该任务（…）」加一句原因（**不是**异常：
        号抄错了是正常情况）。那句原因里的过期删除是真的 —— 存档会被 Observer/cleanup.py 的
        prune_task_archives 按体积清掉（超 RUNTIME_TASKS_TRIGGER_MB 就删最旧的）。

    Raises:
        InvalidTaskIdError: 号不是非空字符串，或含白名单外的字符（参数错，不是「没找到」）。
        TaskBoardCorruptError: 清单文件损坏（此刻判不出「清单里有没有它」，不能去翻存档充数）。
        OSError: 存档存在但读不出来（真实故障，绝不降级成「没找到」）。
    """
    check_task_id(task_id)
    task = next((item for item in _load_pending() if item.get("task_id") == task_id), None)
    if task is not None:
        return yaml.safe_dump(
            _ordered(task), allow_unicode=True, default_flow_style=False, sort_keys=False
        )
    path = runtime_task_file(task_id)
    try:
        # 竞态：读到之前被清理掉也算这一支 —— 与「本来就没有」对调用方是同一件事。
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (f"在近期待做（task_range={TASK_RANGE_PENDING}）与历史完结（task_range="
                f"{TASK_RANGE_HISTORY}）任务库中都未找到该任务（{task_id}）：可能是任务号错误，"
                f"或者该任务文件已被过期删除。")
    return f"在历史任务中找到该任务（已不在待做清单里，存于 {path.name}）：\n\n{text.rstrip(chr(10))}\n"


def read_history_text() -> str:
    """读**全部**历史任务（已从清单里出去、单独落盘的那些）的汇总，供 task_list 的 task_range=history 用。

    汇总而不是原文：待做清单是有头有尾的一份、装的全是活任务，整份原文交出去正合适；历史存档却
    只增不减（一条一个文件，只受 Observer/cleanup.py 的体积清理约束），几千条各自的执行记录一次
    交出去没人看得下。这里每条只给任务自己的字段（_TASK_FIELD_ORDER 那几个，其余非步骤键原样跟
    在后面），要看哪一条的全文（含 stepN）就带它的号再调一次本工具。

    不按 status 过滤：目录里的存档来自两条路 —— write 落下的终结任务，与 expire_stopped_tasks
    搬出来的（那条仍是 stopped），旧存档还可能带着早已不用的写法。这里一律原样列出，由读方自己看
    status。顺序按文件名（即任务号）—— local-… 的号里就带着年月日时分秒，同一前缀下即时间顺序。

    只读：不加锁、不写盘。

    Returns:
        汇总的 YAML 文本（开头一段说明与条数）；一条存档都没有（或目录还不存在）时返回一句提示。
        个别存档读不出来时那一条只给 task_id 与 error，不牵连其余条目。

    Raises:
        OSError: 目录存在但枚举不了（真实故障，绝不降级成「没有历史任务」）。
    """
    try:
        paths = sorted(path for path in RUNTIME_TASKS_DIR.iterdir()
                       if path.suffix == ".yaml" and path.name != RUNTIME_PENDING_TASKS_FILE.name)
    except FileNotFoundError:
        return f"还没有任何历史任务（目录还不存在）：{RUNTIME_TASKS_DIR}"
    if not paths:
        return f"还没有任何历史任务：{RUNTIME_TASKS_DIR}"
    header = (f"# 历史任务（task_range={TASK_RANGE_HISTORY}）—— 已经不在待做清单里的任务（写完的 "
              f"{' / '.join(TERMINAL_TASK_STATUS)}，以及\n"
              f"# 停了太久被移出来的 {TASK_STATUS['STOPPED']['value']}），一条一份落在同目录的\n"
              f"# <task_id>.yaml。本文件是它们的汇总：每条只列任务自己的字段，不含执行记录（stepN）。\n"
              f"# 要看某一条的全文就再调一次 task_list：task_range 填 {TASK_RANGE_HISTORY}、带上它的 task_id。\n"
              f"# 顺序按任务号；共 {len(paths)} 条；个别存档坏了的那条只给 task_id 与 error。\n")
    return header + yaml.safe_dump(
        {"tasks": [_history_entry(path) for path in paths]},
        allow_unicode=True, default_flow_style=False, sort_keys=False,
    )


def _history_entry(path: Path) -> Dict[str, Any]:
    """历史汇总里的一条：任务自己的字段（去掉执行记录 stepN）；读不出来时只给号与原因。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return {"task_id": path.stem, "error": f"存档读取失败：{exc}"}
    if not isinstance(data, dict):
        return {"task_id": path.stem, "error": "存档内容不是任务映射"}
    entry = _reorder({key: value for key, value in data.items() if not _STEP_KEY_RE.match(key)},
                     _TASK_FIELD_ORDER)
    entry.setdefault("task_id", path.stem)
    return entry


def _load_pending() -> List[Dict[str, Any]]:
    """读清单文件，返回任务列表。

    Returns:
        任务列表；文件不存在或只有注释（safe_load 得 None）时为空列表。

    Raises:
        TaskBoardCorruptError: 文件无法解析、顶层不是映射、tasks 不是列表、
            或某个条目不是带 task_id 的映射。**绝不降级成空清单**——那会让下一次写
            把别人的任务整份抹掉。
    """
    if not RUNTIME_PENDING_TASKS_FILE.exists():
        return []
    try:
        data = yaml.safe_load(RUNTIME_PENDING_TASKS_FILE.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TaskBoardCorruptError(f"待做任务清单无法解析: {RUNTIME_PENDING_TASKS_FILE}: {exc}") from exc
    # 空文件 / 只有注释按空清单处理：整份写入从不产生这种文件，只可能来自手工编辑。
    # 真正的损坏（半截 YAML）会在上面解析失败。
    if data is None:
        return []
    if not isinstance(data, dict):
        raise TaskBoardCorruptError(f"待做任务清单顶层必须是映射: {RUNTIME_PENDING_TASKS_FILE}")
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        raise TaskBoardCorruptError(f"待做任务清单的 tasks 必须是列表: {RUNTIME_PENDING_TASKS_FILE}")
    for task in tasks:
        if not isinstance(task, dict) or not task.get("task_id"):
            raise TaskBoardCorruptError(
                f"待做任务清单里有不是映射、或没有 task_id 的条目: {RUNTIME_PENDING_TASKS_FILE}"
            )
    return tasks


# 开机自查该问用户哪批任务：**先看中断的**——有 interrupted 就只给这些（上一轮退出时没做完的，
# 用户最需要先定「接着做还是算了」）；一条都没有才给 pending / stopped（还没开始的、用户之前
# 暂停的）。两批都不含 executing：启动时的初始化会把还停在 executing 的标成 interrupted
# （interrupt_executing），此刻清单里不该再有 executing。挑法由代码定，模型不读整份清单。
BOOT_CHECK_FIRST_STATUS: Tuple[str, ...] = (TASK_STATUS["INTERRUPTED"]["value"],)
BOOT_CHECK_THEN_STATUS: Tuple[str, ...] = (
    TASK_STATUS["PENDING"]["value"],
    TASK_STATUS["STOPPED"]["value"],
)


def boot_check_tasks() -> List[Dict[str, Any]]:
    """挑出开机自查要问用户的任务（BOOT_CHECK_FIRST_STATUS 优先，没有才看
    BOOT_CHECK_THEN_STATUS）。

    这是一次**读**：不加锁、不写盘（写盘只有 write 与 interrupt_executing 两处）。

    Returns:
        挑中的任务（原样，未改字段），按清单顺序；一类都没有时是空列表。

    Raises:
        TaskBoardCorruptError: 清单损坏（同 _load_pending，绝不降级成空清单）。
    """
    tasks = _load_pending()
    for statuses in (BOOT_CHECK_FIRST_STATUS, BOOT_CHECK_THEN_STATUS):
        picked = [task for task in tasks if task.get(_STATUS) in statuses]
        if picked:
            return picked
    return []


def boot_check_text() -> str:
    """把 boot_check_tasks 挑出的任务渲染成开机自查那一轮**注入**的文本块。

    这一段由代码写：`scripts/role-daemon.sh` 取值进进程环境，Interactor/resident.patch.yml 的
    `!!js` 把它拼进 `startupWake`（那一轮的输入就是这个文本）。于是「开机先问哪一批任务」不随
    模型的心情漂，模型也不必自己去读整份清单——块里只有任务事实，怎么问用户仍写在人设的
    【开机自查】一节。

    Returns:
        多行文本。一条要问的都没有时返回一个说清「两类都空」的说明块——模型据此判定本轮没有
        要问的，不必去猜「是没任务还是注入坏了」。
    """
    rule = (f"系统按「有 {' / '.join(BOOT_CHECK_FIRST_STATUS)} 只列它，"
            f"否则列 {' / '.join(BOOT_CHECK_THEN_STATUS)}」挑过")
    picked = boot_check_tasks()
    if not picked:
        return f"本轮没有要问的任务（{rule}，两类都空）。"
    lines = [f"本轮要问用户的任务（{rule}）："]
    for task in picked:
        lines.append(f"- 任务号={task.get('task_id', '?')} 状态={task.get(_STATUS, '?')} "
                     f"归属={task.get(_OWNER, '?')} 说明={task.get('description', '')}")
    return "\n".join(lines)


def _dump_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """整份原子写：先写同目录的 .tmp，再 replace 换过去（换 inode，故锁不在本文件上）。

    Args:
        path: 目标文件。
        payload: 要 dump 成 YAML 的顶层映射。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, allow_unicode=True, default_flow_style=False, sort_keys=False)
    tmp.replace(path)


def _save_pending(tasks: List[Dict[str, Any]]) -> None:
    """落盘待做清单 —— `RUNTIME_PENDING_TASKS_FILE` 的**唯一出口**。

    任何改清单的路径都走它（`write` / `interrupt_executing` / `expire_stopped_tasks`），这样
    「写这份文件」这件事只有一处：锁与原子换都由这里统一，将来加一条写路径也不会
    自己造一个绕过模块规矩的第二出口。

    时间戳**不在这里盖**：哪条任务被这次改动碰过，只有改它的那条路径知道，所以 `_stamp` 由
    它们在改完、dump 之前调用（见 `write` 与 `interrupt_executing` 里各自的说明）。
    """
    _dump_atomic(RUNTIME_PENDING_TASKS_FILE, {"tasks": tasks})


def write(task_id: Any = None, data: Any = None, role: Any = None) -> Dict[str, Any]:
    """写清单：新建任务、补字段、追加步骤、改值——增和改是同一件事。

    字段已存在就改值，不存在就补出来，调用方不用先查。只有叶子这样：路径中间的段必须
    已经存在，不存在或不是映射都抛 FieldNotFoundError（不会凭空造出中间层）——所以**新步骤
    要整段写**（field 写 `step`、value 给对象），之后才能按 `step1.result` 改它。

    task_id 不在清单里时就地建任务（create_time 由本函数补，此时 data 里必须给 owner）。
    已经终结（同目录已有 <task_id>.yaml）则抛 TaskTerminalError，不会复活它。
    data 逐条应用到副本上，中途有一条抛错时原文件**一点没动**。

    **写的是已有任务时分权**（见 _reject_owner_write / _reject_owner_field）：只有 owner 能改
    它（唯一例外是交互器把归编排器任务的 status 置成 executing），owner 字段本身只在新建时给。
    越权时整条改动被拒（抛 RoleNotAllowedError），一条都不落盘——调用方据此不投递事件。

    **省略 task_id（或给空串）= 新建一条没有云端号的任务**，号由本函数发（_mint_task_id）；
    给了号就是改那一条（号来自云端信封头，或调用方从清单里读到的）。**两种都是新建**，因此都
    要过调用方角色的限制：本角色不许新建时抛 RoleNotAllowedError——省略 task_id 时它取代发号，
    给了个清单里没有的号时它取代「就地建任务」。

    落盘：该任务已终结就写单文件并从清单移除，否则整份覆盖清单。两步都在同一把锁内，
    **顺序是先写单文件、后写清单**：中途崩只会让任务在两处都出现（重放幂等），反过来
    （先清清单再落单文件）会整个丢掉它。

    Args:
        task_id: 任务编号，取值受 check_task_id 约束；省略或空串表示新建、由本函数发号。
        data: 一组 {"field": …, "value": …}，形状见 _iter_data。
        role: 发起这次改动的角色，查 configs/agents.py 的 ROLE_TASK_BOARD_LIMITS 得它的写入
            限制；省略表示系统通道，没有角色限制。

    Returns:
        `(改动后的任务深拷贝, 这次改动是否真的改变了内容)`。新建时号就是发出来的那个；changed
        为 False 表示要写的值本来就在文件里（清单一字未动、时间戳也没动，见 _changes_content），
        调用方据此不必叫醒收件方。

    Raises:
        InvalidTaskIdError: 给了 task_id 但不能当文件名用（不是字符串，或含白名单外字符）。
        RoleNotAllowedError: 本角色不许新建任务，或越权改别人的任务、改已有任务的 owner。
        TaskTerminalError: 该 task_id 已终结（不能复活）。
        ReservedFieldError: 写到了模块记账的字段。
        FieldNotFoundError: 字段路径的中间段指不到。
        InvalidStatusError / InvalidOwnerError: 取值不在词表里，或新建时没给 owner。
        TaskBoardCorruptError: 清单文件损坏。
    """
    if not is_blank_task_id(task_id):
        # 先校验再进锁区：调用方给的号不合法是它自己的问题，不该占着写锁才发现。
        check_task_id(task_id)
    # 角色限制也在锁外判：它只看调用方是谁和它要写什么，不依赖清单当前的内容。
    limits = _role_limits(role)
    if is_blank_task_id(task_id) and limits.get("no_create"):
        _reject_role_create(task_id, role, limits)
    items = list(_iter_data(data))
    RUNTIME_TASKS_DIR.mkdir(parents=True, exist_ok=True)

    with open(RUNTIME_PENDING_TASKS_LOCK, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        tasks = _load_pending()
        # 发号要在锁内、且要拿到当前清单：占用判定（清单 + 已终结文件）不能靠自己读一遍。
        if is_blank_task_id(task_id):
            task_id = _mint_task_id(tasks)
        terminal_path = runtime_task_file(task_id)
        if terminal_path.exists():
            raise TaskTerminalError(
                f"任务已终结并落在 {terminal_path.name}，不能再写（同编号也不能重建）: {task_id}"
            )
        existing = next((task for task in tasks if task.get("task_id") == task_id), None)
        is_new = existing is None
        if is_new and limits.get("no_create"):
            # 号不在清单里就是新建（见 _reject_role_create）：对不许新建的角色来说，这正是
            # 「照抄事件头带来的号、结果多出一条重复任务」那条路。
            _reject_role_create(task_id, role, limits)
        working: Dict[str, Any] = {"task_id": task_id, "create_time": _now()} if is_new else _copy(existing)
        task_owner = None if is_new else (existing or {}).get(_OWNER)
        # 新建任务本身就是一次变化（清单里多一条），所以从这里起步。
        changed = is_new

        for field, value in items:
            segments = _normalize(_split_field(field), working)
            _reject_reserved(segments, value)
            _reject_owner_field(segments, is_new)
            if not is_new:
                _reject_owner_write(segments, value, task_owner, role, task_id)
            _check_status(segments, value)
            _check_owner(segments, value)
            parent, leaf = _descend(working, segments, task_id)
            previous = parent.get(leaf, _MISSING)
            # 必须在赋值前数：赋值会把这一步整个换成调用方给的新对象，旧计数就没了。
            carried_attempts = _carry_attempts(working, segments, value)
            parent[leaf] = value
            changed = changed or _changes_content(segments, previous, value)
            # 落在这条路径上的步骤：时间戳盖成当前时刻（调用方传的 update_time 不作数），
            # attempts 按 _carry_attempts 数出来的值装回去。
            _touch_step(working, segments, carried_attempts=carried_attempts)

        if is_new:
            # 新建的任务必须说清归谁做，否则「只有 owner 该执行它」这条就落空了。
            if not working.get(_OWNER):
                raise InvalidOwnerError(
                    f'新建任务必须在 data 里给 owner（{" / ".join(TASK_OWNER_VALUES)}）: {task_id}'
                )
            # 新建的任务必须有个状态，否则既终结不了、清单里也没得显示。调用方没给才补。
            working.setdefault(_STATUS, TASK_STATUS["PENDING"]["value"])
        if not changed:
            # 要写的值本来就等于文件里的值：**清单一字不动**（连时间戳都不动），回报 changed
            # 为 False，调用方据此不叫醒收件方。existing 一定不是 None——新建时 changed 已是 True。
            return _copy(existing), False
        # 改动要落盘了才盖时间戳：先盖后 _ordered（字段顺序按 _TASK_FIELD_ORDER）。终结的那条
        # 下面会被移出清单，所以它的这一份由紧接着的单文件存档带走——两处是同一个值。
        _stamp(working)
        working = _ordered(working)

        if _is_terminal(working):
            _dump_atomic(terminal_path, working)
            tasks = [task for task in tasks if task.get("task_id") != task_id]
        elif is_new:
            tasks.append(working)
        else:
            # 就地替换，保住「清单顺序即创建顺序」。
            for index, candidate in enumerate(tasks):
                if candidate.get("task_id") == task_id:
                    tasks[index] = working
                    break
        _save_pending(tasks)

    return _copy(working), True


def interrupt_executing() -> List[str]:
    """把清单里所有还在 `executing` 的任务标成 `interrupted` —— 启动时的一次性收尾。

    上一轮角色系统退出（被收掉、掉电、Ctrl-C）时，正做到一半的任务会停在 `executing`：那是
    上一轮的进度，本轮没有任何人在执行它们。启动时统一标成 `interrupted`（取值与含义见
    configs/agents.py 的 TASK_STATUS），交互器的开机自查于是照常问用户要不要接着做——
    `interrupted` 不是终结状态，任务留在清单里。

    **只改 status 这一个字段**：`last_update_time` 与其它字段原样不动 —— 这是全模块唯一一处
    「改了文件却不盖时间戳」的地方（其余落盘路径都经 `_stamp`）。理由：这不是模型的一次改动，
    是系统在收上一轮的场；时间戳留着才看得出这条任务是什么时候被搁下的——盖成现在（每轮拉起
    都盖一次）就再也看不出它搁了多久，开机自查里那批任务也就分不出新旧了。

    与 write 同一把锁、同一套原子写；清单不存在、没有 executing 的任务、或清单损坏（抛
    TaskBoardCorruptError）时都不改文件。

    Returns:
        被标成 interrupted 的任务号，按清单顺序；一条都没有时是空列表。
    """
    RUNTIME_TASKS_DIR.mkdir(parents=True, exist_ok=True)
    changed: List[str] = []
    with open(RUNTIME_PENDING_TASKS_LOCK, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        tasks = _load_pending()
        for task in tasks:
            if task.get(_STATUS) != TASK_STATUS["EXECUTING"]["value"]:
                continue
            task[_STATUS] = TASK_STATUS["INTERRUPTED"]["value"]
            changed.append(str(task["task_id"]))
        if changed:
            # 刻意不 _stamp：这是全模块唯一一处「改了文件却不盖时间戳」的地方，理由见函数文档。
            _save_pending(tasks)
    return changed


def expire_stopped_tasks(max_age_hours: float) -> List[str]:
    """把停了太久的 `stopped` 任务移出清单、转成历史存档 —— starter.sh 拉起时的一次性清理。

    用户说的「先不做」（交互器把 status 置成 stopped）留在清单里等着被接着做，但**不该永远等**：
    清单正是交互器开机自查要问的那批（boot_check_tasks），一条几个月前暂停的任务会让以后每一轮
    重启都再问一次。stopped 且 `last_update_time` 早于 `max_age_hours` 的那些，这里按 task_id 落成
    同目录的历史存档（runtime_task_file）并从清单移除——**只搬不删**：存档照旧能被 task_list 的
    task_range=history 读到，编号也仍被 _mint_task_id 当作已占用，不会重号。

    搬走的副本**原样**落盘：status 仍是 stopped、`last_update_time` 仍是用户搁下它的那一刻。与
    interrupt_executing 同一理由——这是系统的清理动作，不是模型的一次改动；那个时间戳是「什么时候
    停的」唯一的记录，不该被这次搬运刷掉。

    判据只认能解析出来的 `last_update_time`：字段缺失或写法解析不了（老存档里的写法）的一律留着，
    宁可留着也不会因为读不懂时间就把任务搬走。

    落盘顺序与 write 一致——**先写单文件、后写清单**：中途崩只会让任务在两处都出现，下次重放幂等
    （清单里那份较新的会覆盖已存在的存档）；反过来会整个丢掉它。

    与 write / interrupt_executing 同一把锁、同一套原子写；清单不存在或没有到期的任务时不动文件。

    Args:
        max_age_hours: 过期时限（小时），取自 configs/configs.py 的 TASK_STOPPED_EXPIRE_HOURS。

    Returns:
        被移出的任务号，按清单顺序；一条都没有时是空列表。

    Raises:
        TaskBoardError: max_age_hours 不是正数（配置写错了，不能当成「全部过期」）。
        TaskBoardCorruptError: 清单损坏（见 _load_pending，绝不降级成空清单）。
    """
    if max_age_hours <= 0:
        raise TaskBoardError(f"过期时限必须是正数（小时），收到 {max_age_hours!r}")
    cutoff = datetime.now().astimezone() - timedelta(hours=max_age_hours)
    RUNTIME_TASKS_DIR.mkdir(parents=True, exist_ok=True)
    expired: List[str] = []
    with open(RUNTIME_PENDING_TASKS_LOCK, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        staying: List[Dict[str, Any]] = []
        for task in _load_pending():
            stamp = _parse_time(task.get("last_update_time"))
            if (task.get(_STATUS) == TASK_STATUS["STOPPED"]["value"]
                    and stamp is not None and stamp < cutoff):
                # 搬走的这一份原样带走时间戳（不 _stamp）：这条任务没有*被改动*，只是挪了个地方。
                _dump_atomic(runtime_task_file(str(task["task_id"])), _ordered(task))
                expired.append(str(task["task_id"]))
            else:
                staying.append(task)
        if expired:
            # 留下来的那些一个字段都没动，因此也不 _stamp。
            _save_pending(staying)
    return expired
