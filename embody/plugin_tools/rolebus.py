"""Python 侧 rolebus 发件器 + 归档读取 —— 直接往共享 spool 写事件信封。

rolebus 的发件工具（`rolebus_send_<event_type>`）只挂在 dsh 进程里给模型用；云端任务
在 Python 侧到达（`Communicator/`），那里没有 cordis 上下文。但 spool 本身是纯文件协议
（`dsh-plugins/rolebus/src/transport.ts`）：

    <root>/inbox/<target>/<event_id>.json    收件方轮询、校验信封后归档并投递

收件侧只校验信封结构与 `target`、**不查事件目录**，所以 Python 按同一套规则写好文件即可
被 dsh 进程收到——不需要中间进程，也不需要在 Python 里重建 cordis。

本模块因此镜像 `transport.ts` 的两条语义，跨语言一致性靠两侧同步同一套规则
（含与 TS 事件目录的一致）：

1. `write_envelope()`：先写 `<event_id>.pending-<pid>-<uuid>` 再 `os.replace` 成
   `<event_id>.json`（POSIX 原子 rename，收件箱里只会出现完整 JSON）；同 `event_id`
   已存在则报错，不覆盖。
2. `validateEnvelope()`：`event_type` 合 snake_case、`event_id` 合 `[A-Za-z0-9_-]+`、
   `timestamp` 为 Unix 秒整数、`task_id` 在事件头。

信封组装分两层：`build_envelope()` 补 event_id/timestamp（信封有哪些字段、谁进头谁进
payload 只有这一处实现），`post_event()` 在它之上加**云端两条**的目录校验。角色自己的发件
方不走这一对函数：那类事件由 dsh 进程里的发件工具（`rolebus_send_<event_type>`）产出，
形状归 TS 目录管，本侧不镜像（理由见 `write_envelope` 的说明）。

反向也走同一套文件：`current_task()` / `task_report()` 从 `<root>/archive/<role>/` 读回
「本角色当前云端任务号」与它的描述、最新进展，供编码侧做参数注入与云端上报（不需要模型
传、也不需要新起一份状态文件）。

当前实现了云端两条事件（`source: cloud`），形状事实源仍是
`dsh-plugins/rolebus/src/event-catalog.ts`；`CLOUD_EVENTS` 是它在本侧的唯一副本。
两条分属两个并存版本：`cloud_update` 投给 interactor（旧版），`planner_cloud_update`
投给 planner（新版）；两个版本不同时启动，因此同一时刻只有一半的收件箱有角色在轮询。
「该投哪一半」由 `cloud_variant()` 现判（看哪个版本的角色在跑），发件方不必自己记版本。

端侧动作只有 `update` / `stop` 两个，两条事件都吃这两个动作（差别只在 `payload.action`）：
`update` 是任务内容有交代（下发 / 改写）、`stop` 是停下该任务。云端那套 `create / modify /
interrupt / answer / info` 是**云端词**，由 `Communicator/receiver.py` 的 `CLOUD_ACTION_MAP`
归一后才投到这里（answer / info 端侧不处理）。

命令行（免 PyYAML，只用标准库）：

    python -m plugin_tools.rolebus spool-root
    python -m plugin_tools.rolebus variant
    python -m plugin_tools.rolebus cloud-update --action update --description "倒杯水"
    python -m plugin_tools.rolebus cloud-update --action stop --task-id t-1 --description "用户叫停"
    python -m plugin_tools.rolebus planner-cloud-update --action update --description "改去 518"
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

#: 端侧云端事件的全部动作，只有两个；云端词（create / modify / interrupt / answer / info
#: + 旧网关 cancel / resume）到这两个词的映射在 Communicator/receiver.py 的 CLOUD_ACTION_MAP。
#: TS 侧同名常量在 dsh-plugins/rolebus/src/event-catalog.ts，各条目的 action.enum 用它们。
CLOUD_ACTIONS = ("update", "stop")

#: Python 侧的事件目录面：与 event-catalog.ts 里 source == "cloud" 的条目一一对应，
#: 顺序也一致。**两端是手抄、没有脚本比对**，改一处必须同步另一处；`actions` 就是 TS 侧
#: 同一条目 `action.enum` 的值域（两条事件都声明 update / stop），`parameters` 是它的键名（顺序一致），
#: `priority` 是同一条目的固定优先级——目录里定义的事件，信封上一定带这个字段。
CLOUD_EVENTS: Tuple[Dict[str, Any], ...] = (
    {
        "event_type": "cloud_update",
        "source": CLOUD_SOURCE,
        "target": "interactor",
        "parameters": ("task_id", "action", "description"),
        "actions": ("update", "stop"),
        "priority": "P1",
    },
    {
        "event_type": "planner_cloud_update",
        "source": CLOUD_SOURCE,
        "target": "planner",
        "parameters": ("task_id", "action", "description"),
        "actions": ("update", "stop"),
        "priority": "P1",
    },
)

#: 优先级取值，与 TS 侧 types.ts 的 PRIORITIES 同字面量（信封头字段 priority 的合法值）。
PRIORITIES = ("P1", "P2", "P3")


def _validate_cloud_events() -> None:
    """表自身的装配期校验：每个条目都要声明合法的 `priority`。

    目录里定义的事件，信封上一定带这个字段（收件侧只在目录外事件上才回落），漏一条就会
    静默退化成「发件方不表态」。

    @raises ValueError: 有条目漏了 priority，或取值不在 PRIORITIES 里。
    """
    for event in CLOUD_EVENTS:
        if event.get("priority") not in PRIORITIES:
            raise ValueError(
                f"rolebus: 事件 {event.get('event_type')!r} 的 priority 必须是 {PRIORITIES} 之一，"
                f"收到 {event.get('priority')!r}（与 event-catalog.ts 的同一条目对齐）"
            )


_validate_cloud_events()

#: 与 TS 侧 types.ts 同规则：snake_case 事件类型、路径安全的 event_id。
EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

#: TS 侧 HEADER_FIELDS 里**本侧会产出**的那几个：这些参数进事件头，不进 payload。
#: `priority` 也在里面：本侧只产云端两条事件，它们各自的 `CLOUD_EVENTS` 条目都声明了固定优先级，
#: 组装信封时写进头（见 post_event）。省略它的收件路径只剩目录外与手工 emit 的信封。
HEADER_FIELDS = ("task_id", "program_id", "correlation_id", "severity", "priority")

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


# ─────────────────────────────────────────────────────────────────────────────
# 版本判据 —— 云端事件该投分立版还是合并版
# ─────────────────────────────────────────────────────────────────────────────

#: 角色存活标记的后缀：`<DSH_HOME>/<role>.pid` 里是活 pid 才算该角色在跑。
#: 写者：starter.sh 写非 supervisor 的角色，scripts/role-daemon.sh 写 supervisor 的。
ROLE_PID_SUFFIX = ".pid"

#: 合并版角色的名字：它在跑就投 `planner_cloud_*`，否则投分立版（`cloud_*`）。
MERGED_ROLE = "planner"

SPLIT_VARIANT = "split"
MERGED_VARIANT = "planner"


def role_running(role: str, home: Optional[str] = None) -> bool:
    """该角色是否在本机这份 dsh home 下跑着。

    判据只看 `<home>/<role>.pid` 里的活 pid（与 starter.sh 的启动守卫、
    scripts/manual/cloud-emit.sh 的 role_running 同一口径）。读不到标记、内容不是数字、pid 已死
    都算没在跑——**宁可说没在跑**：调用方据此选版本，说错了事件就投给没人轮询的收件箱。

    `home` 缺省 `dsh_home()`，即本进程的 `$DSH_HOME`（**不看 `$ROLEBUS_SPOOL_ROOT`**：
    spool 根可以另指，但角色标记只在 dsh home 下）。

    @param role: 角色名。
    @param home: dsh home 覆盖；缺省取 `dsh_home()`。
    @returns: 在跑为 True。
    """
    marker = (Path(home).expanduser() if home is not None else dsh_home()) / f"{role}{ROLE_PID_SUFFIX}"
    try:
        pid = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程在，只是不归本用户——按在跑算（本侧只关心有没有人轮询收件箱）。
        return True
    except OSError:
        return False
    return True


def cloud_variant(home: Optional[str] = None) -> str:
    """云端事件该投哪个版本：`MERGED_ROLE` 在跑就是 "planner"，否则 "split"。

    云端两条事件分属两个并存版本（见 CLOUD_EVENTS），两个版本不同时启动，所以「投哪一半」
    由「哪一半的角色在跑」定。这是**唯一一处**判据：scripts/manual/cloud-emit.sh 与本模块的调用方
    （Communicator/receiver.py 的云端桥）都走这里，不留第二份。

    都判不出来（没有任何角色在跑）时返回 "split"（缺省三件套），与 cloud-emit.sh 一致。

    @param home: dsh home 覆盖；缺省取 `dsh_home()`。
    @returns: `SPLIT_VARIANT` 或 `MERGED_VARIANT`。
    """
    return MERGED_VARIANT if role_running(MERGED_ROLE, home) else SPLIT_VARIANT


# ─────────────────────────────────────────────────────────────────────────────
# 读归档 —— 角色在做什么，事实源是收到的信封本身
# ─────────────────────────────────────────────────────────────────────────────

#: 归档子目录：收件方认领信封时把收件箱文件挪到这里（`transport.ts` 的接收契约）。
#: **归档存在只表示已认领，不表示已处理**，所以这里读到的「当前任务」是角色收过什么，
#: 不是它执行到哪一步。
ARCHIVE_DIRNAME = "archive"

#: 云端下发「任务内容」的事件类型（`payload.description` 是任务本身的说法）。
#: 云端事件里「任务内容有交代」的那**一个动作**：只有它信封里的 `payload.description` 是任务
#: 本身的说法。另一个动作 `stop`（停下该任务）的同一字段是停下它的理由，不能当任务描述——
#: 两条动作都由同一条事件（`cloud_update`）承载，所以判据只能是动作，不是事件类型。
CLOUD_CONTENT_ACTION = "update"

#: 编排器回给交互器的任务进展事件（`payload.description` 是这次进展的人话说明，
#: `items` 里的状态改动落在待做任务清单上、不在信封里可读）。
TASK_PROGRESS_EVENT = "task_update_orc"


def archive_dir(role: str, root: Optional[str] = None) -> Path:
    """本角色已认领信封的归档目录。"""
    return spool_root(root) / ARCHIVE_DIRNAME / role


def _archived_envelopes(root: Optional[str] = None, role: Optional[str] = None) -> list:
    """归档信封，新的在前；`role` 省略表示所有角色。

    排序按信封头里的 `timestamp`（同一刻再用文件 mtime 打破平局）：投递先后与文件系统
    时间没有固定关系，而时间戳是发件方给出的既有事实。读不动的文件跳过——归档是整份
    `os.replace` 落下的，出现半截文件说明有别的进程正在动它。
    """
    base = spool_root(root) / ARCHIVE_DIRNAME
    dirs = [base / role] if role is not None else [d for d in base.glob("*") if d.is_dir()]

    found: list = []
    for directory in dirs:
        for path in directory.glob("*.json"):
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(envelope, dict):
                found.append(
                    (int(envelope.get("timestamp") or 0), path.stat().st_mtime, envelope)
                )
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [envelope for _, _, envelope in found]


def current_task(role: str, root: Optional[str] = None) -> str:
    """本角色「当前云端任务号」，取不到返回空串。

    事实源是本角色收到的信封归档，不是模型填的参数、也没有第二份状态文件：任务号随信封头
    下发，角色此刻在处理哪个云端任务，就看归档里最后一条 `source: cloud` 的信封带了哪个
    任务号。

    只认云端事件：编排器自己发的事件（task_update_orc / interaction_request）也带任务号，
    但那是「这个任务进展如何」，不是「云端下发了什么」；它们的文本由 task_report 取。
    """
    for envelope in _archived_envelopes(root, role):
        if envelope.get("source") != CLOUD_SOURCE:
            continue
        task_id = str(envelope.get("task_id") or "").strip()
        if task_id:
            return task_id
    return ""


def task_report(task_id: str, root: Optional[str] = None) -> Dict[str, str]:
    """按任务号取云端上报快照里的两项文本，取不到就是空串。

    这个任务号下：`task_description` 取云端最近一次 `action=update` 那封的
    `payload.description`（任务本身的说法；`action=stop` 的同一字段是停下它的理由，不算），
    `current_step_desc` 取编排器最近一次
    `task_update_orc` 的 `payload.description`（原样透传，编排器怎么说就怎么报）。上游没发过
    就是空串——空串怎么处理由调用方决定，本函数不替它编。

    同一任务号的信封可能落在不同角色的归档里（云端发给谁、编排器又回给谁），所以这里扫所有
    角色的归档：调用方因此不必知道自己是哪个角色。
    """
    report = {"task_description": "", "current_step_desc": ""}
    wanted = str(task_id or "").strip()
    if not wanted:
        return report

    for envelope in _archived_envelopes(root):
        if str(envelope.get("task_id") or "").strip() != wanted:
            continue
        payload = envelope.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if (not report["task_description"]
                and envelope.get("source") == CLOUD_SOURCE
                and payload.get("action") == CLOUD_CONTENT_ACTION):
            report["task_description"] = str(payload.get("description") or "")
        if not report["current_step_desc"] and envelope.get("event_type") == TASK_PROGRESS_EVENT:
            report["current_step_desc"] = str(payload.get("description") or "")
        if report["task_description"] and report["current_step_desc"]:
            break
    return report


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


def build_envelope(
    source: str,
    target: str,
    event_type: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
    priority: Optional[str] = None,
    event_id: Optional[str] = None,
) -> Dict[str, Any]:
    """组装一封**尚未校验**的信封：event_id 与 timestamp 在这里补齐，其余照搬入参。

    信封形状（哪些字段在头、哪些进 payload）只有这一处实现：`post_event`（云端两条）经它组装，
    与 `transport.ts` 那边的信封规则逐字段一致。

    @param source: 发送方身份（`cloud` 或某个角色名）。
    @param target: 目标角色。
    @param event_type: snake_case 事件类型。
    @param payload: 领域负载；无内容时**不写这个键**（不是写一个空对象）。
    @param task_id: 信封头 task_id。
    @param priority: 信封头 priority（P1/P2/P3）；目录里定义的事件必给（见各条目）。
    @param event_id: 显式事件 id（去重键）；缺省自动生成 UUID。
    @returns: 信封字典（字段顺序：event_id / event_type / source / target / timestamp /
        task_id / priority / payload）。
    """
    envelope: Dict[str, Any] = {
        "event_id": event_id if event_id is not None else str(uuid.uuid4()),
        "event_type": event_type,
        "source": source,
        "target": target,
        "timestamp": int(time.time()),
    }
    if task_id is not None:
        envelope["task_id"] = task_id
    if priority is not None:
        envelope["priority"] = priority
    if payload:
        envelope["payload"] = payload
    return envelope


def write_envelope(envelope: Dict[str, Any], *, root: Optional[str] = None) -> Dict[str, Any]:
    """把一封组装好的信封投进 `target` 角色的收件箱：校验 + 原子落盘 + 同 id 不覆盖。

    **不查事件目录**：目录的事实源是 TS 模块（dsh-plugins/rolebus/src/event-catalog.ts 的
    `source == 本角色` 那些条目），Python 侧不镜像它（镜像一份就要同步一份，而现在没有脚本
    能比对两者，见模块注释）。调用方负责自己那条事件与目录条目一致；收件侧同样不查目录，
    只校验信封结构与 `target`。

    @param envelope: 完整信封（含 event_id / timestamp，见 build_envelope）。
    @param root: spool 根覆盖；缺省见 `spool_root()`。
    @returns: 投出的信封（原样返回，便于链式使用）。
    @raises ValueError: 信封不合规（见 validate_envelope）。
    @raises FileExistsError: 同 event_id 已在收件箱里（同 id 重投是歧义，不覆盖）。
    """
    validate_envelope(envelope)
    inbox = spool_root(root) / "inbox" / envelope["target"]
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
    @returns: 已投出的信封（含补全的 event_id / timestamp；`priority` 取条目声明的固定值）。
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

    envelope = build_envelope(source, target, event_type,
                              payload=payload, task_id=task_id,
                              priority=spec["priority"], event_id=event_id)
    return write_envelope(envelope, root=root)


def _timestamp_task_id() -> str:
    """云端未给 task_id 时的占位：`cloud-<Unix 秒>`（唯一性只保证同一秒内不重）。"""
    return f"cloud-{int(time.time())}"


def _actions_of(event_type: str) -> Tuple[str, ...]:
    """该事件允许的 action 取值域（登记在 `CLOUD_EVENTS` 里，与 TS 目录条目的 enum 一致）。"""
    return next(event["actions"] for event in CLOUD_EVENTS if event["event_type"] == event_type)


def _cloud_event(event_type: str, action: str, description: str,
                 task_id: Optional[str], root: Optional[str],
                 event_id: Optional[str]) -> Dict[str, Any]:
    """两条 cloud 事件的共同实现：校验 action/description，按目录表定 target 与 task_id。

    action 按**本条事件**的值域校验（见各条目的 `actions`，两条事件都吃 update / stop）：
    不在值域里的动作会被挡在这里，而不是让收件角色收到一条语义对不上的事件。
    """
    _require_text(description, "description")
    spec = next(event for event in CLOUD_EVENTS if event["event_type"] == event_type)
    if action not in spec["actions"]:
        raise ValueError(
            f"rolebus: {event_type} 的 action 必须是 {spec['actions']} 之一，收到 {action!r}"
        )
    return post_event(
        event_type,
        spec["target"],
        payload={"action": action, "description": description},
        task_id=task_id if task_id is not None else _timestamp_task_id(),
        root=root,
        event_id=event_id,
    )


def cloud_event_type(action: str, variant: Optional[str] = None,
                     home: Optional[str] = None) -> str:
    """一个**端侧**动作该投哪条事件：只剩「按版本选前缀」一件事。

    云端两条动作（`update` / `stop`）由同一条事件承载，所以动作不再区分事件名，只有
    `planner_` 前缀随 `variant` 变。命中的条目仍从 `CLOUD_EVENTS` 的 `actions` 反查、不在
    本函数里写字面量——动作值域与事件目录仍是形状事实源。

    @param action: 端侧动作（`CLOUD_ACTIONS` = update / stop）；云端词要先过
        `Communicator/receiver.py` 的 `CLOUD_ACTION_MAP`。
    @param variant: `SPLIT_VARIANT` 或 `MERGED_VARIANT`；缺省按 `cloud_variant(home)` 现判。
    @param home: 判版本用的 dsh home 覆盖（只在 `variant` 缺省时用得上）。
    @returns: 事件类型名（已登记在 `CLOUD_EVENTS` 里）。
    @raises ValueError: 该 action 不在 `CLOUD_ACTIONS` 里（没有任何条目声明它）。
    """
    merged = (variant if variant is not None else cloud_variant(home)) == MERGED_VARIANT
    for event in CLOUD_EVENTS:
        if action in event["actions"] and event["event_type"].startswith("planner_") == merged:
            return event["event_type"]
    raise ValueError(f"rolebus: 未知的云端动作 {action!r}（已登记：{', '.join(CLOUD_ACTIONS)}）")


def send_cloud(action: str, description: str, *, variant: Optional[str] = None,
               task_id: Optional[str] = None, root: Optional[str] = None,
               event_id: Optional[str] = None, home: Optional[str] = None) -> Dict[str, Any]:
    """按版本选目标角色，投一封云端事件。

    这是两个 `send_*` 之上的按版本投，给**手上只有端侧动作、又不知道哪个版本在跑**的调用方用
    （Communicator/receiver.py 的云端桥：它把云端词归一成端侧词后，不该自己判"这封事件投
    interactor 还是 planner"——那是 `CLOUD_EVENTS` 的事）。

    @param action: 端侧动作（`CLOUD_ACTIONS`）；合法性见 `cloud_event_type`。
    @param description: 该动作的自然语言原文（任务描述 / 干涉理由）。
    @param variant: 版本覆盖；缺省 `cloud_variant(home)` 现判（哪个版本的角色在跑）。
    @param task_id: 云端任务 id；缺省用 `cloud-<Unix 秒>` 占位。
    @param root: spool 根覆盖。
    @param event_id: 显式事件 id；缺省自动生成 UUID。
    @param home: 判版本用的 dsh home 覆盖。
    @returns: 已投出的信封。
    """
    return _cloud_event(cloud_event_type(action, variant, home), action, description,
                        task_id, root, event_id)


def send_cloud_update(action: str, description: str, *, task_id: Optional[str] = None,
                      root: Optional[str] = None,
                      event_id: Optional[str] = None) -> Dict[str, Any]:
    """把云端下发的动作投给 interactor（`cloud_update`）。

    @param action: `CLOUD_ACTIONS` 里的 `update`（任务下发 / 改写）或 `stop`（停下该任务）。
    @param description: 任务内容（`stop` 时是停下它的理由）的自然语言原文。
    @param task_id: 云端任务 id；缺省用 `cloud-<Unix 秒>` 占位。
    @param root: spool 根覆盖。
    @param event_id: 显式事件 id；缺省自动生成 UUID。
    @returns: 已投出的信封。
    """
    return _cloud_event("cloud_update", action, description, task_id, root, event_id)


def send_planner_cloud_update(action: str, description: str, *, task_id: Optional[str] = None,
                              root: Optional[str] = None,
                              event_id: Optional[str] = None) -> Dict[str, Any]:
    """把云端下发的动作投给 planner（`planner_cloud_update`，新版）。

    与 `send_cloud_update` 同义，只是投给合并版角色 planner。两个版本不同时启动，
    投错了版本的事件没人收（收件方各自只轮询自己的收件箱），故二选一由调用方决定。

    @param action: `CLOUD_ACTIONS` 里的 `update`（任务下发 / 改写）或 `stop`（停下该任务）。
    @param description: 任务内容（`stop` 时是停下它的理由）的自然语言原文。
    @param task_id: 云端任务 id；缺省用 `cloud-<Unix 秒>` 占位。
    @param root: spool 根覆盖。
    @param event_id: 显式事件 id；缺省自动生成 UUID。
    @returns: 已投出的信封。
    """
    return _cloud_event("planner_cloud_update", action, description, task_id, root, event_id)


def main(argv: Optional[list] = None) -> int:
    """命令行入口：打印 spool 根，或发一条 cloud 事件（信封 JSON 打到 stdout）。"""
    parser = argparse.ArgumentParser(prog="plugin_tools.rolebus", description=__doc__)
    parser.add_argument("--root", default=None, help="spool 根覆盖（缺省见 spool_root()）")
    # 子命令名 = 事件类型下划线换连字符（cloud_update → cloud-update），
    # 故不再手抄一份子命令名表；--action 的取值域也按事件从 CLOUD_EVENTS 现取。
    senders = {
        "cloud_update": send_cloud_update,
        "planner_cloud_update": send_planner_cloud_update,
    }
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("spool-root", help="打印解析出的 spool 根路径")
    # variant 只看 DSH_HOME 下的角色标记（见 cloud_variant），故不吃 --root。
    sub.add_parser("variant", help="打印云端事件该投哪个版本（split / planner）")
    for event_type, sender in senders.items():
        child = sub.add_parser(event_type.replace("_", "-"), help=f"发 {sender.__name__}")
        child.add_argument("--action", required=True, choices=list(_actions_of(event_type)))
        child.add_argument("--description", required=True)
        child.add_argument("--task-id", default=None, help="缺省用 cloud-<Unix 秒> 占位")

    args = parser.parse_args(argv)

    if args.command == "spool-root":
        print(spool_root(args.root))
        return 0

    if args.command == "variant":
        print(cloud_variant())
        return 0

    envelope = senders[args.command.replace("-", "_")](args.action, args.description,
                                                       task_id=args.task_id, root=args.root)
    json.dump(envelope, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
