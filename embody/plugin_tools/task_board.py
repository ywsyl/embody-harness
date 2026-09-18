"""
待做任务清单（task board）。

清单常驻内存：只有任务级 / 步骤级状态变更才落盘，逐步骤的文件 I/O 因此省掉。
代价是崩溃恢复时，上次落盘之后新增的步骤会丢，**允许重跑**；某一步不能重跑时，
调用方显式调 flush() 打检查点。

两个文件：
  * 活跃清单 TASK_BOARD_FILE：只装**未终结**的任务（status 不是 succeeded / failed /
    stopped），整份原子覆盖（独立锁文件 flock + tmp+rename）。
  * 归档 TASK_BOARD_ARCHIVE_FILE：**已终结**的任务，一行一条 JSON，只追加（O_APPEND，
    一次 write）。归档只增不减，整份重写会随历史线性变慢，所以不写成 YAML 列表。
    终结的任务不留在内存里。

格式（活跃清单是单层列表，步骤是任务对象上的顶层键，编号就在键名里）：

    version: 1
    tasks:
      - task_id: t-20260915-101530
        description: 去客厅找一下遥控器
        status: executing
        create_time: '2026-09-15T10:15:30+08:00'
        step1:
          description: 走到客厅
          tool: waypoint_nav
          result: 已到达客厅
          status: succeeded
          update_time: '2026-09-15T10:16:02+08:00'

对外只有两个写函数 update / delete，签名一律 (task_id, data)：data 是一组 (field, value)
（update）或 (field,)（delete）。field 是点分字段路径，逐段下钻字典，例如 description、
status、step3、step3.result。

update 不区分增和改：**字段已存在就改值，不存在就补出来**。只有叶子这样——路径中间那些段
必须已经存在，不会凭空造出嵌套层，所以 update(T, [("step99.status", "running")]) 报错而不是
造一个空壳步骤。task_id 不在内存里时 update 就地建任务（create_time 由模块补）。字段写裸
step 时自动编号为 step<N>（N = 现有最大编号 + 1，删掉的编号留空洞、不复用、不重排）。

落盘收敛在 flush() 一个函数里，update / delete 按需调用它。

单写者契约：flush() 整份覆盖活跃文件，只支持一个进程持实例写（当前即 orchestrator）。
只读方（interactor）用 reload() 取最新。两个写者各持内存态会互相覆盖，不在支持范围内。

CLI（python -m plugin_tools.task_board）：每次调用都是新进程，所以写子命令一律在退出前
flush()。省 I/O 的收益只在进程内长驻调用（持有一个 TaskBoard 实例）时才成立。

依赖：PyYAML（见 plugin_tools/requirements.txt）。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

# 与 plugin_tools/bridge.py 一致：把仓库根（embody/）放进搜索路径，这样
# `python -m plugin_tools.task_board` 与 `python plugin_tools/task_board.py`
# 直接执行两种入口都能 import configs.*。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import STEP_STATUS, TASK_STATUS  # noqa: E402
from configs.paths import (  # noqa: E402
    TASK_BOARD_ARCHIVE_FILE,
    TASK_BOARD_FILE,
)

BOARD_VERSION = 1

# 状态取值不在这里写死第二份：configs/configs.py 的 TASK_STATUS / STEP_STATUS 是事实源，
# 这里只把它们的 value 抽出来。终结集合按字典里的键名取，也不重抄一遍取值。
TASK_STATUS_VALUES: Tuple[str, ...] = tuple(spec["value"] for spec in TASK_STATUS.values())
STEP_STATUS_VALUES: Tuple[str, ...] = tuple(spec["value"] for spec in STEP_STATUS.values())
TERMINAL_TASK_STATUS: Tuple[str, ...] = tuple(
    TASK_STATUS[key]["value"] for key in ("SUCCEEDED", "FAILED", "STOPPED")
)

# 自动编号用的步骤键：step1 / step2 / …（编号嵌在键名里，不是列表）
_STEP_KEY_RE = re.compile(r"^step(\d+)$")
# 自动编号的写法：字段写裸 step（step 或 step.<子路径>）
_AUTO_STEP = "step"
# 保留字段名：delete(T, [("task",)]) 删整个任务
_WHOLE_TASK = "task"
# 触发落盘的字段名
_STATUS = "status"


class TaskBoardError(Exception):
    """任务清单的领域错误基类。"""


class TaskNotFoundError(TaskBoardError):
    """task_id 在内存与归档里都找不到。"""


class FieldNotFoundError(TaskBoardError):
    """字段路径的中间段指不到：不存在，或不是映射、不能再往下钻。"""


class TaskBoardCorruptError(TaskBoardError):
    """清单文件无法解析或结构非法。绝不当作空清单继续写。"""


class InvalidStatusError(TaskBoardError):
    """状态取值不在 TASK_STATUS / STEP_STATUS 里。"""


class TaskTerminalError(TaskBoardError):
    """任务已终结（移入归档），不再接受写操作。"""


def _now() -> str:
    """当前本地时间，带时区偏移的 ISO 8601（秒级）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _env_path(key: str, default: Path) -> Path:
    """取环境变量 key 指定的路径，缺省用 default。空字符串视为未设置。"""
    return Path(os.environ.get(key) or default)


def _lock_path(path: Path) -> Path:
    """锁文件与目标同目录同名加 .lock 后缀（与 tools/runtime_state.py 同约定）。"""
    return path.with_name(path.name + ".lock")


def _split_field(field: str) -> List[str]:
    """把点分字段路径拆成段，拒绝空串与空段。"""
    if not isinstance(field, str) or not field.strip():
        raise FieldNotFoundError(f"字段路径必须是非空字符串，收到 {field!r}")
    segments = field.split(".")
    if any(segment == "" for segment in segments):
        raise FieldNotFoundError(f"字段路径 {field!r} 有空段")
    return segments


def _is_terminal(task: Dict[str, Any]) -> bool:
    """任务是否已终结。"""
    return task.get(_STATUS) in TERMINAL_TASK_STATUS


def _copy(value: Any) -> Any:
    """深拷贝一份纯 JSON 值，避免调用方拿到的对象与内存态共享引用。"""
    return json.loads(json.dumps(value, ensure_ascii=False))


class TaskBoard:
    """内存态的待做任务清单，按需落盘。单写者（见模块 docstring）。

    Attributes:
        active_path: 活跃清单文件路径。
        archive_path: 归档文件路径。
    """

    def __init__(
        self,
        active_path: Optional[Path] = None,
        archive_path: Optional[Path] = None,
    ) -> None:
        """
        Args:
            active_path: 活跃清单路径，缺省 EMBODY_TASK_BOARD_FILE 或 configs.paths
                的 TASK_BOARD_FILE。
            archive_path: 归档路径，缺省 EMBODY_TASK_BOARD_ARCHIVE_FILE 或
                configs.paths 的 TASK_BOARD_ARCHIVE_FILE。
        """
        self.active_path = Path(active_path or _env_path("EMBODY_TASK_BOARD_FILE", TASK_BOARD_FILE))
        self.archive_path = Path(
            archive_path or _env_path("EMBODY_TASK_BOARD_ARCHIVE_FILE", TASK_BOARD_ARCHIVE_FILE)
        )
        # 内存态只装未终结的任务，顺序即写入顺序。
        self._tasks: List[Dict[str, Any]] = []
        self._dirty = False
        # 归档读一次就缓存：写新任务时要查 id 有没有被归档占着，不能每次都整份重读。
        self._archive_cache: Optional[Dict[str, Dict[str, Any]]] = None

    # ---- 加载与只读查询 ------------------------------------------------------

    @classmethod
    def load(
        cls,
        active_path: Optional[Path] = None,
        archive_path: Optional[Path] = None,
    ) -> "TaskBoard":
        """读一次盘，返回实例。

        Args:
            active_path: 活跃清单路径，缺省同 __init__。
            archive_path: 归档路径，缺省同 __init__。

        Returns:
            已装好内存态的任务清单。

        Raises:
            TaskBoardCorruptError: 活跃文件无法解析或结构非法。
        """
        board = cls(active_path=active_path, archive_path=archive_path)
        board._read_active()
        return board

    def _read_active(self) -> None:
        """把活跃文件读进内存。

        活跃文件里若混进了终结状态的任务（手工编辑、或上一版遗留），就地归档并移出内存，
        让「终结任务不进内存」这条不变量在加载后也成立。正常写入的清单不会触发这条路径。

        Raises:
            TaskBoardCorruptError: 文件无法解析、顶层不是映射、tasks 不是列表、
                或某个条目不是带 task_id 的映射。
        """
        # reload() 走这里：归档缓存一并作废，否则只读方看不到别的进程新归档的任务。
        self._archive_cache = None
        if not self.active_path.exists():
            self._tasks = []
            self._dirty = False
            return
        try:
            data = yaml.safe_load(self.active_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise TaskBoardCorruptError(f"活跃清单无法解析: {self.active_path}: {exc}") from exc
        # 空文件按空清单处理：整份写入从不产生零字节文件，safy_load 的 None 只可能来自
        # 手工 touch。真正的损坏（半截 YAML）会在上面解析失败。
        if data is None:
            self._tasks = []
            self._dirty = False
            return
        if not isinstance(data, dict):
            raise TaskBoardCorruptError(f"活跃清单顶层必须是映射: {self.active_path}")
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            raise TaskBoardCorruptError(f"活跃清单的 tasks 必须是列表: {self.active_path}")

        in_memory: List[Dict[str, Any]] = []
        strays: List[Dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, dict) or not task.get("task_id"):
                raise TaskBoardCorruptError(
                    f"活跃清单里有不是映射、或没有 task_id 的条目: {self.active_path}"
                )
            (strays if _is_terminal(task) else in_memory).append(task)

        self._tasks = in_memory
        self._dirty = False
        if strays:
            self._append_archive(strays)

    def reload(self) -> None:
        """丢弃内存里未落盘的改动，重新读盘。供只读方取最新用。"""
        self._read_active()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """按 task_id 查任务，先查内存、再查归档。

        Args:
            task_id: 任务编号。

        Returns:
            任务的深拷贝；内存与归档都没有则返回 None。
        """
        task = self._find(task_id)
        if task is not None:
            return task
        archived = self._read_archive().get(task_id)
        return _copy(archived) if archived is not None else None

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """列出任务。

        Args:
            status: 只要这个状态的任务；None 表示不过滤。
            include_archived: 是否把归档里的任务一并列出（同 task_id 以内存态为准）。

        Returns:
            任务深拷贝的列表；内存里的在前，归档里的在后。

        Raises:
            InvalidStatusError: status 不在 TASK_STATUS 里。
        """
        if status is not None and status not in TASK_STATUS_VALUES:
            raise InvalidStatusError(
                f"任务状态 {status!r} 非法，合法取值: {' / '.join(TASK_STATUS_VALUES)}"
            )
        tasks = self._tasks
        if include_archived:
            known = {task["task_id"] for task in self._tasks}
            archived = self._read_archive()
            tasks = tasks + [_copy(archived[k]) for k in archived if k not in known]
        selected = tasks if status is None else [t for t in tasks if t.get(_STATUS) == status]
        return [_copy(task) for task in selected]

    def render(
        self,
        task_id: Optional[str] = None,
        *,
        status: Optional[str] = None,
    ) -> str:
        """渲染成给人看的文本。

        Args:
            task_id: 只渲染这一条；None 表示渲染内存里的全部任务。
            status: 只渲染这个状态的任务；仅在不指定 task_id 时生效。

        Returns:
            多行文本（末尾带换行）。

        Raises:
            TaskNotFoundError: 指定了 task_id 但查不到。
            InvalidStatusError: status 不在 TASK_STATUS 里。
        """
        if task_id is not None:
            task = self.get_task(task_id)
            if task is None:
                raise TaskNotFoundError(f"任务不存在: {task_id}")
            tasks = [task]
            header = f"任务 {task_id}"
        else:
            tasks = self.list_tasks(status=status)
            header = f"待做任务清单：未终结 {len(tasks)} 条"

        lines = [header, "─" * 60]
        if not tasks:
            lines.append("（空）")
        for task in tasks:
            lines.append(
                f"{task.get('task_id', '?')}  [{task.get(_STATUS, '?')}]  {task.get('description', '')}"
            )
            lines.append(f"    create_time: {task.get('create_time', '')}")
            for key in self._step_keys(task):
                step = task[key]
                detail = " | ".join(
                    str(step.get(field, ""))
                    for field in ("description", "tool", "result", "update_time")
                )
                lines.append(f"    {key}  [{step.get(_STATUS, '?')}]  {detail}")
        return "\n".join(lines) + "\n"

    def _find(self, task_id: str) -> Optional[Dict[str, Any]]:
        """在内存里找任务（内部用，不拷贝）。"""
        for task in self._tasks:
            if task.get("task_id") == task_id:
                return task
        return None

    def _require_writable(self, task_id: str) -> Dict[str, Any]:
        """取一个可写的任务（在内存里，且未终结）。找不到就按原因分别报错。

        已终结的任务会移出内存，所以「内存里没有」有两种原因，报错要分得清：
        归档里有 → 已终结；两边都没有 → 压根不存在。查归档只在报错路径上发生。

        Returns:
            内存里的任务对象（内部用，不拷贝）。

        Raises:
            TaskTerminalError: 任务已终结并移入归档。
            TaskNotFoundError: 任务在内存与归档里都不存在。
        """
        task = self._find(task_id)
        if task is not None:
            if _is_terminal(task):
                raise TaskTerminalError(f"任务已终结，不能再写: {task_id}")
            return task
        if task_id in self._read_archive():
            raise TaskTerminalError(f"任务已终结并移入归档，不能再写: {task_id}")
        raise TaskNotFoundError(f"任务不存在: {task_id}")

    def _require_free_id(self, task_id: str) -> None:
        """新建任务前确认 task_id 没被归档里的任务占着。

        Raises:
            TaskTerminalError: 该 task_id 已终结并移入归档（复用编号会造出重复 id）。
        """
        if task_id in self._read_archive():
            raise TaskTerminalError(f"task_id 已被归档里的任务占用，不能重建: {task_id}")

    @staticmethod
    def _step_keys(task: Dict[str, Any]) -> List[str]:
        """任务上的步骤键，按编号升序。"""
        keys = [key for key in task if _STEP_KEY_RE.match(key)]
        return sorted(keys, key=lambda key: int(_STEP_KEY_RE.match(key).group(1)))

    @staticmethod
    def _next_step_key(task: Dict[str, Any]) -> str:
        """下一个自动编号的步骤键：现有最大编号 + 1（删掉的编号留空洞、不复用）。"""
        numbers = [int(_STEP_KEY_RE.match(key).group(1)) for key in task if _STEP_KEY_RE.match(key)]
        return f"step{max(numbers, default=0) + 1}"

    # ---- 两个写函数 ----------------------------------------------------------

    def update(self, task_id: str, data: Sequence[Any]) -> Dict[str, Any]:
        """写：新建任务、补字段、追加步骤、改值——增和改是同一件事。

        字段已存在就改值，不存在就补出来，调用方不用先查。只有叶子这样：路径中间的段必须
        已经存在，不存在或不是映射都抛 FieldNotFoundError（不会凭空造出中间层）。

        task_id 不在内存里时就地建任务（create_time 由本方法补）；已经终结（含编号被归档
        占用）则抛 TaskTerminalError。data 逐条应用到副本上，中途有一条抛错时内存态仍原样。

        Args:
            task_id: 任务编号。
            data: 一组 (field, value)。field 写法见模块 docstring；写裸 step 时自动编号。

        Returns:
            改动后任务的深拷贝。

        Raises:
            TaskTerminalError: 任务已终结（含 task_id 已被归档占用）。
            FieldNotFoundError: 字段路径的中间段指不到。
            InvalidStatusError: 状态取值非法。
        """
        existing = self._find(task_id)
        if existing is None:
            self._require_free_id(task_id)
            working: Dict[str, Any] = {"task_id": task_id, "create_time": _now()}
            needs_flush = True
        else:
            if _is_terminal(existing):
                raise TaskTerminalError(f"任务已终结，不能再写: {task_id}")
            working = _copy(existing)
            needs_flush = False

        for field, value in data:
            segments = self._normalize(_split_field(field), working)
            needs_flush |= self._check_status(segments, value)
            parent, leaf = self._descend(working, segments, task_id)
            parent[leaf] = value
            # 整段写入（step1）尊重调用方给的 update_time；往步骤里写字段（step1.result）算
            # 改动，刷新时间戳。
            self._touch_step(working, segments, overwrite=len(segments) > 1)
        # 新建的任务必须有个状态，否则既终结不了、render 也没得显示。调用方没给才补。
        if _STATUS not in working:
            working[_STATUS] = TASK_STATUS["PENDING"]["value"]

        if existing is None:
            self._tasks.append(working)
        else:
            existing.clear()
            existing.update(working)
        self._dirty = True
        if needs_flush:
            self.flush()
        return _copy(working)

    def delete(self, task_id: str, data: Sequence[Any]) -> Optional[Dict[str, Any]]:
        """删：删整个任务、删一整步、或删某个字段。

        Args:
            task_id: 任务编号。
            data: 一组 (field,) 或裸字符串 field。field 写 task 时删掉整个任务。

        Returns:
            改动后任务的深拷贝；整条任务被删掉时返回 None。

        Raises:
            TaskNotFoundError: 任务在内存与归档里都不存在。
            TaskTerminalError: 任务已终结。
            FieldNotFoundError: 字段路径指不到。
        """
        task = self._require_writable(task_id)
        working = _copy(task)
        for item in data:
            field = item[0] if isinstance(item, (tuple, list)) else item
            segments = self._normalize(_split_field(field), working)
            if segments == [_WHOLE_TASK]:
                # 按身份删，不按相等：dict 的 == 是逐字段比较，这里要精确摘掉这一条。
                for index, candidate in enumerate(self._tasks):
                    if candidate is task:
                        del self._tasks[index]
                        break
                self._dirty = True
                return None
            parent, leaf = self._descend(working, segments, task_id)
            if leaf not in parent:
                raise FieldNotFoundError(f"字段 {field!r} 指不到：{leaf!r} 不存在（task {task_id}）")
            del parent[leaf]
        task.clear()
        task.update(working)
        self._dirty = True
        return _copy(working)

    # ---- 落盘 ----------------------------------------------------------------

    def flush(self) -> None:
        """落盘：内存里终结的任务追加到归档并移出内存，其余整份原子写活跃文件。

        这是唯一的落盘函数，update / delete 需要落盘时都调它。内存无改动时直接返回。

        Raises:
            TaskBoardError: 写入失败（目录建不出来、锁拿不到等）。
        """
        if not self._dirty:
            return
        archived = [task for task in self._tasks if _is_terminal(task)]
        active = [task for task in self._tasks if not _is_terminal(task)]
        # 先归档再写活跃清单：中途崩了只会让任务在两边都出现（归档按 task_id 去重，
        # 重放无害），反过来会整个丢掉。
        if archived:
            self._append_archive(archived)
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.active_path)
        with open(lock, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            tmp = self.active_path.with_name(self.active_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                yaml.safe_dump(
                    {"version": BOARD_VERSION, "tasks": active},
                    handle,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
            tmp.replace(self.active_path)
        self._tasks = active
        self._dirty = False

    def _append_archive(self, tasks: Iterable[Dict[str, Any]]) -> None:
        """把终结的任务追加到归档文件（O_APPEND，一次 write）。"""
        payload = "".join(
            json.dumps(task, ensure_ascii=False, sort_keys=False) + "\n" for task in tasks
        )
        if not payload:
            return
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        lock = _lock_path(self.archive_path)
        with open(lock, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            with open(self.archive_path, "a", encoding="utf-8") as handle:
                handle.write(payload)
        self._archive_cache = None

    def _read_archive(self) -> Dict[str, Dict[str, Any]]:
        """读归档文件，返回 task_id → 任务。结果缓存，直到本实例追加归档才失效。

        同一 task_id 出现多次时以最后一行为准：flush 先归档后写活跃清单，中途崩掉会留下
        重复行，重放读到的仍应是同一条任务。

        Raises:
            TaskBoardCorruptError: 某一行不是合法 JSON，或不是带 task_id 的映射。
        """
        if self._archive_cache is not None:
            return self._archive_cache
        if not self.archive_path.exists():
            self._archive_cache = {}
            return self._archive_cache
        result: Dict[str, Dict[str, Any]] = {}
        with open(self.archive_path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    task = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TaskBoardCorruptError(
                        f"归档第 {lineno} 行不是合法 JSON: {self.archive_path}: {exc}"
                    ) from exc
                if not isinstance(task, dict) or not task.get("task_id"):
                    raise TaskBoardCorruptError(
                        f"归档第 {lineno} 行不是带 task_id 的映射: {self.archive_path}"
                    )
                result[task["task_id"]] = task
        self._archive_cache = result
        return result

    # ---- 字段路径 ------------------------------------------------------------

    def _normalize(self, segments: List[str], task: Dict[str, Any]) -> List[str]:
        """把裸 step 换成自动编号的 step<N>。"""
        if segments and segments[0] == _AUTO_STEP:
            return [self._next_step_key(task)] + segments[1:]
        return segments

    @staticmethod
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
                    f"字段 {'.'.join(segments)!r} 指不到：{segment!r} 不存在（task {task_id}）"
                )
            if not isinstance(child, dict):
                raise FieldNotFoundError(
                    f"字段 {'.'.join(segments)!r} 指不到：{segment!r} 不是映射，不能再往下钻"
                    f"（task {task_id}）"
                )
            parent = child
        return parent, segments[-1]

    @staticmethod
    def _touch_step(task: Dict[str, Any], segments: List[str], *, overwrite: bool) -> None:
        """路径落在某一步上时，补齐 / 刷新该步的 update_time 与 status。

        步骤是开放字典，但这两项由模块保证：没有 status 就没法做「步骤级状态」变更，
        也就永远触发不了落盘；没有 update_time 则渲染不出时间。调用方自己给了就不覆盖
        （overwrite 为 False 时）。

        Args:
            task: 任务对象。
            segments: 字段路径的段；首段是 stepN 时才动。
            overwrite: True 直接盖成当前时刻；False 只在缺失时补。
        """
        if not segments or not _STEP_KEY_RE.match(segments[0]):
            return
        step = task.get(segments[0])
        if not isinstance(step, dict):
            return
        if overwrite or "update_time" not in step:
            step["update_time"] = _now()
        step.setdefault(_STATUS, STEP_STATUS["RUNNING"]["value"])

    @staticmethod
    def _check_status(segments: List[str], value: Any) -> bool:
        """校验状态取值，并回答「这次改动是否需要落盘」。

        只认两个层级：任务级 status 查 TASK_STATUS，stepN.status 查 STEP_STATUS。
        更深的路径不以状态论处（不校验、不落盘）。

        Args:
            segments: 字段路径的段。
            value: 要写入的值。

        Returns:
            是否属于状态变更（需要落盘）。

        Raises:
            InvalidStatusError: 取值不在对应的状态集合里。
        """
        if not segments or segments[-1] != _STATUS:
            return False
        if len(segments) == 1:
            allowed, level = TASK_STATUS_VALUES, "任务"
        elif len(segments) == 2 and _STEP_KEY_RE.match(segments[0]):
            allowed, level = STEP_STATUS_VALUES, "步骤"
        else:
            return False
        if value not in allowed:
            raise InvalidStatusError(
                f"{level}状态 {value!r} 非法，合法取值: {' / '.join(allowed)}"
            )
        return True


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _parse_value(raw: str) -> Any:
    """把命令行上的值按 JSON 解析，解析不出来就当字符串。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parse_set(pairs: Sequence[str]) -> List[Tuple[str, Any]]:
    """把 --set field=value 解析成 (field, value) 列表，顺序即参数顺序。"""
    data: List[Tuple[str, Any]] = []
    for pair in pairs:
        field, sep, raw = pair.partition("=")
        if not sep or not field.strip():
            raise TaskBoardError(f"--set 需要 field=value 形式，收到 {pair!r}")
        data.append((field, _parse_value(raw)))
    return data


def _parse_del(fields: Sequence[str]) -> List[Tuple[str]]:
    """把 --del field 解析成 (field,) 列表。"""
    return [(field,) for field in fields]


def _auto_task_id(board: TaskBoard) -> str:
    """生成不与现有任务撞名的 task_id：t-<YYYYmmdd-HHMMSS>，撞名则追加 -2、-3。"""
    base = datetime.now().astimezone().strftime("t-%Y%m%d-%H%M%S")
    candidate = base
    suffix = 1
    while board.get_task(candidate) is not None:
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def _build_parser() -> argparse.ArgumentParser:
    """构造 CLI 解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m plugin_tools.task_board",
        description="待做任务清单：内存态维护，只有状态变更才落盘。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_update = sub.add_parser("update", help="写：建任务 / 补字段 / 追加步骤 / 改值")
    p_update.add_argument("task_id", nargs="?", help="任务编号，缺省自动生成")
    p_update.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    p_update.add_argument("--json", action="store_true", help="输出 JSON")

    p_delete = sub.add_parser("delete", help="删：删任务 / 删步骤 / 删字段")
    p_delete.add_argument("task_id")
    p_delete.add_argument("--del", dest="del_fields", action="append", default=[], metavar="FIELD")
    p_delete.add_argument("--json", action="store_true", help="输出 JSON")

    p_show = sub.add_parser("show", help="读：列出任务（不带 TASK_ID 即全量）")
    p_show.add_argument("task_id", nargs="?")
    p_show.add_argument("--status", help="只列这个状态")
    p_show.add_argument("--json", action="store_true", help="输出 JSON")

    sub.add_parser("flush", help="把内存态落盘（检查点）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表，缺省取 sys.argv[1:]。

    Returns:
        进程退出码：0 成功，1 领域错误。
    """
    args = _build_parser().parse_args(argv)
    try:
        board = TaskBoard.load()
        if args.command == "update":
            task_id = args.task_id or _auto_task_id(board)
            task = board.update(task_id, _parse_set(args.set))
            board.flush()  # CLI 是新进程，不落盘就等于没做
            _emit(task, json_out=args.json)
        elif args.command == "delete":
            task = board.delete(args.task_id, _parse_del(args.del_fields))
            board.flush()
            if task is not None:
                _emit(task, json_out=args.json)
        elif args.command == "show":
            if args.task_id:
                task = board.get_task(args.task_id)
                if task is None:
                    raise TaskNotFoundError(f"任务不存在: {args.task_id}")
                _emit(task, json_out=args.json)
            elif args.json:
                print(json.dumps(board.list_tasks(status=args.status), ensure_ascii=False, indent=2))
            else:
                print(board.render(status=args.status), end="")
        elif args.command == "flush":
            board.flush()
            print(board.render(), end="")
    except TaskBoardError as exc:
        print(f"task_board: {exc}", file=sys.stderr)
        return 1
    return 0


def _emit(value: Any, *, json_out: bool) -> None:
    """输出一条结果：任务对象按 JSON 打印更好读。"""
    if json_out:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, dict) else value)


if __name__ == "__main__":
    raise SystemExit(main())
