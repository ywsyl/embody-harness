"""Python 侧 rolebus 发件器 —— 直接往共享 spool 写事件信封。

rolebus 的发件工具（`rolebus_send_<event_type>`）只挂在 dsh 进程里给模型用；云端任务
在 Python 侧到达（`Communicator/`），那里没有 cordis 上下文。但 spool 本身是纯文件协议
（`dsh-plugins/rolebus/src/transport.ts`）：

    <root>/inbox/<target>/<event_id>.json    收件方轮询、校验信封后归档并投递

收件侧只校验信封结构与 `target`、**不查事件目录**，所以 Python 按同一套规则写好文件即可
被 dsh 进程收到——不需要中间进程，也不需要在 Python 里重建 cordis。

本模块因此镜像 `transport.ts` 的两条语义，跨语言一致性靠两侧同步同一套规则
（含与 TS 事件目录的一致）：

1. `post()`：先写 `<event_id>.pending-<pid>-<uuid>` 再 `os.replace` 成 `<event_id>.json`
   （POSIX 原子 rename，收件箱里只会出现完整 JSON）；同 `event_id` 已存在则报错，不覆盖。
2. `validateEnvelope()`：`event_type` 合 snake_case、`event_id` 合 `[A-Za-z0-9_-]+`、
   `timestamp` 为 Unix 秒整数、`task_id` 在事件头。

当前只实现了云端四条事件（`source: cloud`），形状事实源仍是
`dsh-plugins/rolebus/src/event-catalog.ts`；`CLOUD_EVENTS` 是它在本侧的唯一副本。
四条分属两个并存版本：`cloud_*` 投给 interactor / orchestrator（旧版），`planner_cloud_*`
投给 planner（新版）；两个版本不同时启动，因此同一时刻只有一半的收件箱有角色在轮询。

命令行（免 PyYAML，只用标准库）：

    python -m plugin_tools.rolebus spool-root
    python -m plugin_tools.rolebus cloud-content-update --action create --description "倒杯水"
    python -m plugin_tools.rolebus cloud-status-update --action interrupt --task-id t-1 --description "用户叫停"
    python -m plugin_tools.rolebus planner-cloud-content-update --action create --description "倒杯水"
    python -m plugin_tools.rolebus planner-cloud-status-update --action info --description "已到达客厅"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 与 bridge/tool_parser 一致：把仓库根（embody/）放进搜索路径，`python plugin_tools/rolebus.py`
# （直接执行）与 `-m plugin_tools.rolebus` 两种入口都能用。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 角色身份不可能是 unset（bundle 中性默认）——与 @embody/dsh-rolebus 同约定。
UNSET_ROLE = "unset"

#: 云端在本项目里的发送方身份（事件目录里是唯一的非 dsh-profile source）。
CLOUD_SOURCE = "cloud"

#: 云端下发的动作词表，逐字取自 Communicator/文档/端侧智能体接口文档.md；
#: TS 侧的同名导出是 dsh-plugins/rolebus/src/event-catalog.ts 的 CLOUD_ACTIONS。
CLOUD_ACTIONS = ("create", "modify", "interrupt", "answer", "info")

#: Python 侧的事件目录面：与 event-catalog.ts 里 source == "cloud" 的条目一一对应，
#: 顺序也一致（门禁逐条比对，不一致即 FAIL）。
CLOUD_EVENTS: Tuple[Dict[str, Any], ...] = (
    {
        "event_type": "cloud_content_update",
        "source": CLOUD_SOURCE,
        "target": "interactor",
        "parameters": ("task_id", "action", "description"),
    },
    {
        "event_type": "cloud_status_update",
        "source": CLOUD_SOURCE,
        "target": "orchestrator",
        "parameters": ("task_id", "action", "description"),
    },
    {
        "event_type": "planner_cloud_content_update",
        "source": CLOUD_SOURCE,
        "target": "planner",
        "parameters": ("task_id", "action", "description"),
    },
    {
        "event_type": "planner_cloud_status_update",
        "source": CLOUD_SOURCE,
        "target": "planner",
        "parameters": ("task_id", "action", "description"),
    },
)

#: 与 TS 侧 types.ts 同规则：snake_case 事件类型、路径安全的 event_id。
EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

#: 与 TS 侧 HEADER_FIELDS 同名：这些参数进事件头，不进 payload。
HEADER_FIELDS = ("task_id", "program_id", "correlation_id", "severity")

#: 显式 root 之后、DSH_HOME 之前的 spool 根覆盖（对应 rolebus 行的 transportConfig.root）。
ROOT_ENV = "ROLEBUS_SPOOL_ROOT"

_PENDING_SUFFIX = ".pending"


def dsh_home() -> Path:
    """本机 dsh home：`$DSH_HOME` 优先，否则 `~/.dsh`。

    逐字镜像引擎 `resolveDshHome()` 的一处细节：**纯空白的 DSH_HOME 视同未设置**。
    """
    home = os.environ.get("DSH_HOME")
    if home is not None and home.strip() != "":
        return Path(home).expanduser()
    return Path.home() / ".dsh"


def spool_root(root: Optional[str] = None) -> Path:
    """共享 spool 根，优先级：显式参数 > `$ROLEBUS_SPOOL_ROOT` > `$DSH_HOME/rolebus`。

    与 TS 侧 `defaultSpoolRoot()` 必须落到同一处，否则发出去的事件收件方永远看不到。
    """
    if root is not None:
        return Path(root).expanduser()
    override = os.environ.get(ROOT_ENV)
    if override is not None and override.strip() != "":
        return Path(override).expanduser()
    return dsh_home() / "rolebus"


def _require_text(value: Any, field: str) -> str:
    """非空字符串校验：缺字段与错类型都报同一个可读错误。"""
    if not isinstance(value, str) or value == "":
        raise ValueError(f"rolebus: {field} 必须是非空字符串，收到 {value!r}")
    return value


def validate_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """校验一封待投递的信封，规则同 TS 侧 `validateEnvelope()`。

    @param envelope: 待校验的信封。
    @returns: 原信封（便于链式使用）。
    @raises ValueError: 必填字段缺失/类型不符、event_type 或 event_id 不合规、
        timestamp 不是有限数、task_id 等头字段不是字符串、payload 不是对象。
    """
    for field in ("event_type", "source", "target"):
        _require_text(envelope.get(field), field)
    if not EVENT_TYPE_PATTERN.fullmatch(envelope["event_type"]):
        raise ValueError(f"rolebus: event_type 必须是 snake_case，收到 {envelope['event_type']!r}")
    if envelope["source"] == UNSET_ROLE:
        raise ValueError(f"rolebus: source 不能是 {UNSET_ROLE!r}（bundle 中性默认，表示未赋予身份）")
    event_id = envelope.get("event_id")
    if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(event_id):
        raise ValueError(f"rolebus: event_id 必须是 [A-Za-z0-9_-] 非空字符串，收到 {event_id!r}")
    timestamp = envelope.get("timestamp")
    # bool 是 int 的子类，但不是合法时间戳。
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise ValueError(f"rolebus: timestamp 必须是 Unix 秒整数，收到 {timestamp!r}")
    for field in HEADER_FIELDS:
        value = envelope.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"rolebus: {field} 必须是字符串或省略，收到 {value!r}")
    payload = envelope.get("payload")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError(f"rolebus: payload 必须是对象，收到 {payload!r}")
    return envelope


def post_event(
    event_type: str,
    target: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
    source: str = CLOUD_SOURCE,
    event_id: Optional[str] = None,
    root: Optional[str] = None,
) -> Dict[str, Any]:
    """把一条 `CLOUD_EVENTS` 里登记的事件投到 `target` 角色的收件箱。

    身份与路由校验通过后写文件；失败一律抛错，不静默丢弃（收件侧对非法文件只会加
    `.bad-` 前缀，人不会注意到）。目录外的 event_type 一律拒绝：TS 收件侧不查目录，
    放行等于让拼错的类型名被投递给 agent。

    @param event_type: 事件类型（必须已登记在 `CLOUD_EVENTS`）。
    @param target: 目标角色，必须与目录里该事件的 target 一致。
    @param payload: 领域负载；不得含 `HEADER_FIELDS` 里的名字（那些进事件头）。
    @param task_id: 事件头字段；必要时由调用方给（见两个 `send_cloud_*` 的占位规则）。
    @param source: 发送方身份，缺省 `cloud`。
    @param event_id: 显式事件 id（去重键）；缺省自动生成 UUID。
    @param root: spool 根覆盖；缺省见 `spool_root()`。
    @returns: 已投出的信封（含补全的 event_id / timestamp）。
    @raises ValueError: 身份/路由/命名不合规、事件不在目录里、payload 占用头字段名。
    @raises FileExistsError: 同 event_id 已在收件箱里（重投是歧义，不覆盖）。
    """
    _require_text(source, "source")
    if source == UNSET_ROLE:
        raise ValueError(f"rolebus: source 不能是 {UNSET_ROLE!r}")
    if not EVENT_TYPE_PATTERN.fullmatch(event_type or ""):
        raise ValueError(f"rolebus: event_type {event_type!r} 不是 snake_case")
    _require_text(target, "target")
    spec = next((event for event in CLOUD_EVENTS if event["event_type"] == event_type), None)
    if spec is None:
        known = ", ".join(event["event_type"] for event in CLOUD_EVENTS)
        raise ValueError(f"rolebus: 事件 {event_type!r} 不在 CLOUD_EVENTS 里（已登记：{known}）")
    if spec["target"] != target:
        raise ValueError(
            f"rolebus: 事件 {event_type!r} 的 target 是 {spec['target']!r}，不能投给 {target!r}"
        )
    payload = dict(payload or {})
    for field in HEADER_FIELDS:
        if field in payload:
            raise ValueError(
                f"rolebus: payload 里的 {field!r} 应作为事件头传参（事件头与载荷的分区见 event-catalog.ts）"
            )

    envelope: Dict[str, Any] = {
        "event_id": event_id if event_id is not None else str(uuid.uuid4()),
        "event_type": event_type,
        "source": source,
        "target": target,
        "timestamp": int(time.time()),
    }
    if task_id is not None:
        envelope["task_id"] = task_id
    if payload:
        envelope["payload"] = payload
    validate_envelope(envelope)

    inbox = spool_root(root) / "inbox" / target
    inbox.mkdir(parents=True, exist_ok=True)
    final_path = inbox / f"{envelope['event_id']}.json"
    if final_path.exists():
        raise FileExistsError(
            f"rolebus: event_id {envelope['event_id']!r} 已存在于 {inbox}（同 id 重投是歧义，不覆盖）"
        )
    # 先写临时文件再 os.replace：收件方轮询到的一律是完整 JSON。
    pending_path = inbox / f"{envelope['event_id']}{_PENDING_SUFFIX}-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        pending_path.write_text(json.dumps(envelope, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(pending_path, final_path)
    except BaseException:
        pending_path.unlink(missing_ok=True)
        raise
    return envelope


def _timestamp_task_id() -> str:
    """云端未给 task_id 时的占位：`cloud-<Unix 秒>`（唯一性只保证同一秒内不重）。"""
    return f"cloud-{int(time.time())}"


def _cloud_event(event_type: str, action: str, description: str,
                 task_id: Optional[str], root: Optional[str],
                 event_id: Optional[str]) -> Dict[str, Any]:
    """四条 cloud 事件的共同实现：校验 action/description，按目录表定 target 与 task_id。"""
    if action not in CLOUD_ACTIONS:
        raise ValueError(f"rolebus: action 必须是 {CLOUD_ACTIONS} 之一，收到 {action!r}")
    _require_text(description, "description")
    spec = next(event for event in CLOUD_EVENTS if event["event_type"] == event_type)
    return post_event(
        event_type,
        spec["target"],
        payload={"action": action, "description": description},
        task_id=task_id if task_id is not None else _timestamp_task_id(),
        root=root,
        event_id=event_id,
    )


def send_cloud_content_update(action: str, description: str, *, task_id: Optional[str] = None,
                              root: Optional[str] = None,
                              event_id: Optional[str] = None) -> Dict[str, Any]:
    """把云端任务内容变化投给 interactor（`cloud_content_update`）。

    @param action: `CLOUD_ACTIONS` 之一。
    @param description: 任务内容的自然语言描述。
    @param task_id: 云端任务 id；缺省用 `cloud-<Unix 秒>` 占位。
    @param root: spool 根覆盖。
    @param event_id: 显式事件 id；缺省自动生成 UUID。
    @returns: 已投出的信封。
    """
    return _cloud_event("cloud_content_update", action, description, task_id, root, event_id)


def send_cloud_status_update(action: str, description: str, *, task_id: Optional[str] = None,
                             root: Optional[str] = None,
                             event_id: Optional[str] = None) -> Dict[str, Any]:
    """把云端任务状态变化投给 orchestrator（`cloud_status_update`）。

    @param action: `CLOUD_ACTIONS` 之一。
    @param description: 任务内容的自然语言描述。
    @param task_id: 云端任务 id；缺省用 `cloud-<Unix 秒>` 占位。
    @param root: spool 根覆盖。
    @param event_id: 显式事件 id；缺省自动生成 UUID。
    @returns: 已投出的信封。
    """
    return _cloud_event("cloud_status_update", action, description, task_id, root, event_id)


def send_planner_cloud_content_update(action: str, description: str, *, task_id: Optional[str] = None,
                                      root: Optional[str] = None,
                                      event_id: Optional[str] = None) -> Dict[str, Any]:
    """把云端任务内容变化投给 planner（`planner_cloud_content_update`，新版）。

    与 `send_cloud_content_update` 同义，只是投给合并版角色 planner。两个版本不同时启动，
    投错了版本的事件没人收（收件方各自只轮询自己的收件箱），故二选一由调用方决定。

    @param action: `CLOUD_ACTIONS` 之一。
    @param description: 任务内容的自然语言描述。
    @param task_id: 云端任务 id；缺省用 `cloud-<Unix 秒>` 占位。
    @param root: spool 根覆盖。
    @param event_id: 显式事件 id；缺省自动生成 UUID。
    @returns: 已投出的信封。
    """
    return _cloud_event("planner_cloud_content_update", action, description, task_id, root, event_id)


def send_planner_cloud_status_update(action: str, description: str, *, task_id: Optional[str] = None,
                                     root: Optional[str] = None,
                                     event_id: Optional[str] = None) -> Dict[str, Any]:
    """把云端任务状态变化投给 planner（`planner_cloud_status_update`，新版）。

    @param action: `CLOUD_ACTIONS` 之一。
    @param description: 任务内容的自然语言描述。
    @param task_id: 云端任务 id；缺省用 `cloud-<Unix 秒>` 占位。
    @param root: spool 根覆盖。
    @param event_id: 显式事件 id；缺省自动生成 UUID。
    @returns: 已投出的信封。
    """
    return _cloud_event("planner_cloud_status_update", action, description, task_id, root, event_id)


def main(argv: Optional[list] = None) -> int:
    """命令行入口：打印 spool 根，或发一条 cloud 事件（信封 JSON 打到 stdout）。"""
    parser = argparse.ArgumentParser(prog="plugin_tools.rolebus", description=__doc__)
    parser.add_argument("--root", default=None, help="spool 根覆盖（缺省见 spool_root()）")
    senders = {
        "cloud-content-update": send_cloud_content_update,
        "cloud-status-update": send_cloud_status_update,
        "planner-cloud-content-update": send_planner_cloud_content_update,
        "planner-cloud-status-update": send_planner_cloud_status_update,
    }
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("spool-root", help="打印解析出的 spool 根路径")
    for name, sender in senders.items():
        child = sub.add_parser(name, help=f"发 {sender.__name__}")
        child.add_argument("--action", required=True, choices=list(CLOUD_ACTIONS))
        child.add_argument("--description", required=True)
        child.add_argument("--task-id", default=None, help="缺省用 cloud-<Unix 秒> 占位")

    args = parser.parse_args(argv)

    if args.command == "spool-root":
        print(spool_root(args.root))
        return 0

    envelope = senders[args.command](args.action, args.description,
                                     task_id=args.task_id, root=args.root)
    json.dump(envelope, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
