#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
视觉观测参数的**运行时可调**覆盖层 —— 读写 runtime_files/vision_tunables.yaml。

为什么要有它
------------
视觉观测每轮送给大模型的图是「帧环最新 N 帧纵向拼接」，而 N 原本硬编码在
frame_producer.get_stacked_frame 里（`[-3:]`）；观测周期原本也只有 configs.DEFAULT_INTERVAL
一个来源。想换档位就得改代码 + 重启 agent —— 而重启会打断正在跑的任务。本模块给这两个数
开一个**改完下一轮即生效**的口子。

两个数（**都是观测器自己的**）：
  stack_frames      每轮拼接多少帧（消费方：Observer/vision_observer.py 的观测循环）
  observe_interval  多久观测一次，秒（同一消费方：等下一帧的超时 + 本轮间隔）

**frame_producer 的抓拍节奏不在这里，也不该在这里**：生产者是全体消费者共享的常驻取帧服务
（观测器、视觉工具、任务主循环都从它的帧环取帧），就该一直以高频率抓帧把帧环攒满
（见 configs.FRAME_PRODUCER_INTERVAL 那行）。「调频率」调的是观测器多久来看一次，
不是相机抓得多快 —— 后者一调慢，受害的是所有从帧环取帧的人，而调它的人（LLM）看不见。

读侧怎么做到「实时」
--------------------
消费方在**主循环里每轮调一次 current()**：它只做一次 stat，mtime 没变就直接返回上一次
解析好的对象（每轮重新解析 YAML 是白烧 CPU）。mtime 变了才重读。所以「改文件 → 下一轮生效」
这句话的落点就是观测循环里那一行 current()。

定位：这是**覆盖层**，不是第二份配置
------------------------------------
出厂默认与可调区间只登记在 configs/configs.py（VISION_STACK_FRAMES /
VISION_OBSERVE_RATE_LEVELS；观测周期的出厂默认 `DEFAULT_INTERVAL` **就是那张档位表里的一档**
（按档位名取，不写秒数 —— 2026-09-24 起），可调区间也**不立常量**：它就是那张档位表的跨度，
由本文件的 `_observe_interval_bounds()` 现算。2026-09-24 之前那对独立的 INTERVAL_MIN/MAX 与档位表互相漂移过，见该函数 docstring），
本文件不另抄一份边界。文件不存在、缺字段、值非法，一律回到 configs 里的出厂默认，所以
`rm runtime_files/vision_tunables.yaml` 就是「一键回默认」。

**这不是把 2026-09-21 删掉的那两个命令行周期开关又加回来**：那时删的是
--status-interval / --vision-interval（**启动期**参数的第二个来源）。本层是运行期覆盖层，
且只有观测器这一个消费者；`--interval` 仍能把它钉死（自检用）。

写侧（谁改、怎么改）
--------------------
  人：本模块的 CLI（库与 CLI 同一个文件，照 Observer/cleanup.py 的先例）
        python3 -m Orchestrator.vision_tunables                        # 看当前生效值
        python3 -m Orchestrator.vision_tunables --stack-frames 5       # 只改这一项
        python3 -m Orchestrator.vision_tunables --observe-interval 6   # 只改这一项（秒）
        python3 -m Orchestrator.vision_tunables --reset                # 删掉覆盖层 = 回出厂默认
      （`python3 Orchestrator/vision_tunables.py` 同义 —— 上面把仓库根插进 sys.path 就是为这条路）
  LLM：plugin_tools/tool_executor.py 的 vision_tunables 工具（只收**观测频率**的三档档位名，
       秒数由 configs.VISION_OBSERVE_RATE_LEVELS 查表，工具不另抄；拼接帧数**只能看不能改**
       —— 那是给画面内容定尺度的，人用上面的 CLI 改）。

两处写入口共用本模块的 update()：独立锁文件 flock + 锁内读-改-写 + tmp+rename（照
plugin_tools/audio_tools.py 的 _append_interaction_history 那一路）。写法是「只覆盖调用方传了的字段」，
所以「CLI 改 A」与「工具改 B」并发不会互相丢 —— 不是最后写的那个赢。

**读侧夹取、写侧拒绝**（两套都要有，别只做一边）：读方是观测线程，不能让一个手滑的值把它
带走 —— 遇到越界/类型不对就夹到区间内并在回显里告警；写侧则直接抛 TunablesError，
消息里必须带合法区间，让人（或 LLM）当场知道能填什么。
"""

from __future__ import annotations

import argparse
import fcntl
import sys
import threading
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional, Tuple

# 确保仓库根在 Python 搜索路径中（本文件 Orchestrator/ 的上一级 = 仓库根），供直接跑本文件时用
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402

from configs.configs import (  # noqa: E402
    DEFAULT_INTERVAL,
    FRAME_PRODUCER_MAX_FRAMES,
    VISION_OBSERVE_RATE_LEVELS,
    VISION_STACK_FRAMES,
    vision_alive_max_age_sec,
)
from configs.paths import (  # noqa: E402
    RUNTIME_VISION_TUNABLES,
    RUNTIME_VISION_TUNABLES_LOCK,
)

# 本文件里两个字段的名字。读/写/回显/渲染模板都用这两个常量，
# 免得某处写成 stack_frame（少了 s）而静默无效 —— 那正是本仓最恨的故障。
FIELD_STACK_FRAMES = "stack_frames"
FIELD_OBSERVE_INTERVAL = "observe_interval"

# 拼接帧数的下界。上界**不立常量**：就是帧环深度（环里没有的帧拼不出来）。
# 这里写 1 是自明值 —— 一帧＝只看当下一张，不再有「帧间差异」这个判据。
_STACK_FRAMES_MIN = 1


class TunablesError(ValueError):
    """写侧取值非法（越界 / 类型不对）。消息里必须带合法区间，别只说「参数错误」。"""


class Tunables(NamedTuple):
    """一次读到的生效值。

    **必须不可变**：current() 返回的这份对象会被观测线程（以及 future 的判活方、
    CLI 回显）同时持有，可变对象迟早被谁顺手改掉，然后各人看到的值都不一样了。
    """

    stack_frames: int
    observe_interval: float
    # "file" = 覆盖层里读到的；"default" = 没有覆盖层（或该项缺失）用的出厂默认；
    # "error" = 覆盖层存在但读不成（YAML 坏 / 不是映射），已回落到出厂默认
    source: str
    clamped: Tuple[str, ...] = ()       # 被夹到区间内的字段名（回显要告警）
    unknown_keys: Tuple[str, ...] = ()  # 覆盖层里不认识、**未生效**的键（回显要告警）


# =============================================================================
# 区间（唯一出处是 configs/configs.py，这里只做取用，不另抄一份数字）
# =============================================================================

def _stack_frames_bounds() -> Tuple[int, int]:
    """拼接帧数的可调区间：1 ~ 帧环深度。"""
    return (_STACK_FRAMES_MIN, max(_STACK_FRAMES_MIN, int(FRAME_PRODUCER_MAX_FRAMES)))


def _observe_interval_bounds() -> Tuple[float, float]:
    """观测周期的可调区间（秒）= **档位表的最小/最大值**。

    区间不另立常量（2026-09-24 删掉了那对 `VISION_OBSERVE_INTERVAL_MIN/MAX`）：两张表并存时
    没人管「档位 ⊆ 区间」这条，`low`=60 漂到区间外，写侧把这一档整个拒掉，而回显与文档还在
    宣称它可用。现在区间就是这张表的跨度 —— 改档位值＝改区间，漂不动。
    """
    values = [float(v) for v in VISION_OBSERVE_RATE_LEVELS.values()]
    return (min(values), max(values))


def observe_rate_level(interval: float) -> Optional[str]:
    """把生效的观测周期对回档位名（低/中/高）；对不上任何一档返回 None。

    给 LLM 那一侧用：人用 CLI 设过自定义值（如 4.5s）时，LLM 必须看得见现实，
    不能让它以为当前处在「中」—— 所以回执里报的是「档位 + 生效秒数」。
    """
    for name, seconds in VISION_OBSERVE_RATE_LEVELS.items():
        if abs(float(seconds) - float(interval)) < 1e-9:
            return name
    return None


def describe_levels() -> str:
    """档位清单的可读文案（秒数查 VISION_OBSERVE_RATE_LEVELS，不在这里重抄），供工具与 CLI 回显共用。"""
    order = {"low": 0, "medium": 1, "high": 2}
    items = sorted(VISION_OBSERVE_RATE_LEVELS.items(), key=lambda kv: order.get(kv[0], 99))
    return "、".join(f"{name}={seconds:g}s" for name, seconds in items)


def alive_max_age_sec(values: Optional[Tunables] = None) -> float:
    """vision 心跳的判活阈值（秒），按**当前生效**的观测周期算。

    算法（3 × 一轮最长耗时）是 configs.vision_alive_max_age_sec() 那一条，这里只负责
    把「生效值」喂给它 —— 判活方（Observer/status_observer.py 的 probe_vision）是另一个
    **进程**，它 import 本模块现读，所以覆盖层一改，它下一轮判活就跟着变。

    观测周期被 --interval 钉死时（观测器自检 CLI），判活方看不到那个数 —— 这是已知的窄缝，
    与改动前那条常量注释承认的是同一个：那种跑法只在单独调试时用，不起 status_observer。
    """
    return vision_alive_max_age_sec(
        current().observe_interval if values is None else values.observe_interval
    )


# =============================================================================
# 读侧
# =============================================================================

def _coerce(stack_raw: Any) -> Tuple[Optional[int], bool]:
    """把覆盖层里的 stack_frames 归成合法整数。返回 (值, 是否被夹取/纠正)。

    非法（类型不对/越界）时返回 None + False 交给调用方决定怎么回落 —— 但这里选择
    直接夹到位并标记，因为读侧的原则是「不能让一个手滑的值把抓拍线程带走」。
    """
    lo, hi = _stack_frames_bounds()
    try:
        value = int(stack_raw)
    except (TypeError, ValueError):
        return None, True
    if value < lo or value > hi:
        return max(lo, min(hi, value)), True
    return value, False


def _coerce_interval(raw: Any) -> Tuple[Optional[float], bool]:
    """把覆盖层里的 observe_interval 归成合法浮点数（语义同 _coerce）。"""
    lo, hi = _observe_interval_bounds()
    if isinstance(raw, bool):  # bool 是 int 的子类，别让 True 变成 1.0
        return None, True
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, True
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None, True
    if value < lo or value > hi:
        return max(lo, min(hi, value)), True
    return value, False


def load() -> Tunables:
    """现读现解析（不走缓存）：返回当前的生效值。

    覆盖层不存在 → 两项都用出厂默认（source="default"），**不报错、也不自动创建它**：
    没有覆盖层是完全正常的状态，创建该由人来决定（CLI 的 --stack-frames 等）。
    文件存在但读不成（YAML 坏 / 顶层不是映射）→ 同样回默认，source="error"。

    注意这里**不打印任何东西**：调用方是观测循环（每轮一次，常驻），刷屏就没人看日志了；
    要告警由调用方按需做。回显（CLI / 工具）直接看返回值里的 clamped / unknown_keys。
    """
    values: Dict[str, Any] = {}
    source = "default"
    if RUNTIME_VISION_TUNABLES.exists():
        try:
            data = yaml.safe_load(RUNTIME_VISION_TUNABLES.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict):
            values = data
            source = "file"
        else:
            source = "error"

    clamped: list = []
    unknown = tuple(
        sorted(str(k) for k in values if k not in (FIELD_STACK_FRAMES, FIELD_OBSERVE_INTERVAL))
    )

    stack_frames, stack_bad = _coerce(values.get(FIELD_STACK_FRAMES))
    if stack_frames is None:
        stack_frames = int(VISION_STACK_FRAMES)
        if FIELD_STACK_FRAMES in values:
            clamped.append(FIELD_STACK_FRAMES)
    elif stack_bad:
        clamped.append(FIELD_STACK_FRAMES)

    interval, interval_bad = _coerce_interval(values.get(FIELD_OBSERVE_INTERVAL))
    if interval is None:
        interval = float(DEFAULT_INTERVAL)
        if FIELD_OBSERVE_INTERVAL in values:
            clamped.append(FIELD_OBSERVE_INTERVAL)
    elif interval_bad:
        clamped.append(FIELD_OBSERVE_INTERVAL)

    return Tunables(
        stack_frames=int(stack_frames),
        observe_interval=float(interval),
        source=source,
        clamped=tuple(clamped),
        unknown_keys=unknown,
    )


# mtime 门控缓存：观测循环每轮解析一次 YAML 是白烧 CPU（常驻进程里更是如此）。
# 键取 (mtime_ns, size, ino)：写盘走 tmp+rename，每次都是新 inode，所以「改了但 mtime 没变」
# 这种情况不存在；三件套只是把边界情况也堵掉。
# 用一把锁护住读写：可能被多个线程（观测线程 + 工具线程 + 判活方）同时调。
_CACHE_LOCK = threading.Lock()
_CACHE_KEY: Optional[Tuple[int, int, int]] = None
_CACHE_VALUE: Optional[Tunables] = None


def current() -> Tunables:
    """热路径版：mtime 门控缓存，开销是**一次 stat**（没变就返回上次那份）。

    消费方在每轮主循环里调它 —— 这就是「改文件下一轮即生效」的落点。

    文件**不存在时不缓存「不存在」这件事**：必须每轮 stat 才能发现它被创建出来
    （`rm 覆盖层` → 回默认、再 `--stack-frames 5` → 又生效，中间不能有粘住的状态）。

    竞态：stat 之后文件被换掉的唯一可能是「stat 到旧、open 到新」，那会以旧键缓存新值，
    下一轮 mtime 不等 → 再解析一次；不会读到过期值（反过来「stat 新、open 旧」在 rename
    下不可能）。
    """
    global _CACHE_KEY, _CACHE_VALUE
    try:
        st = RUNTIME_VISION_TUNABLES.stat()
        key: Optional[Tuple[int, int, int]] = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        key = None  # 文件不存在：每轮都得重新看，落到下面不走缓存
    if key is not None:
        with _CACHE_LOCK:
            if _CACHE_KEY == key and _CACHE_VALUE is not None:
                return _CACHE_VALUE
    value = load()
    if key is not None:
        with _CACHE_LOCK:
            _CACHE_KEY, _CACHE_VALUE = key, value
    return value


# =============================================================================
# 写侧
# =============================================================================

_FILE_TEMPLATE = """\
# runtime_files/vision_tunables.yaml —— **视觉观测器**参数的运行时可调覆盖层
#
# 谁读：Observer/vision_observer.py 的观测循环，**每轮**取一次 —— 所以改完这个文件
#       **不用重启 agent**，下一轮就生效。
# 谁写：本文件由 `python3 -m Orchestrator.vision_tunables`（人）或 vision_tunables 工具（LLM）写入。
#
# 两个数都是**观测器自己的**（多久看一次、每轮拼几帧）。帧生产者的抓拍节奏**不在这里，
# 也不该在这里**：它一直以高频率抓帧把帧环攒满，供所有消费者（观测器、视觉工具、任务主循环）
# 共享；把它调慢，受害的是所有从帧环取帧的人。
#
# 这只是**覆盖层**，不是第二份配置：删掉本文件（或删掉其中一项）= 该项回到
# configs/configs.py 里的出厂默认。出厂默认与可调区间只登记在那边（VISION_STACK_FRAMES /
# VISION_OBSERVE_RATE_LEVELS —— 观测周期的可调区间就是那张档位表的跨度），**别在这里再抄
# 一份边界** —— 两份边界一定会分叉。手改请只改下面两个值，并保持字段名不变（拼错会被当作
# 「不认识的字段」回显告警、且不生效）。
#
# 写入是原子的（独立锁文件 + tmp+rename），本文件由写入方整份重渲染。

%(stack_frames)s: %(stack_value)s          # 观测每轮拼接帧数（出厂 %(stack_default)s；范围 %(stack_lo)s~%(stack_hi)s = 帧环深度）
%(observe_interval)s: %(interval_value)s       # 观测周期秒（出厂 = 档位 %(interval_default_level)s 的 %(interval_default)s；范围 %(interval_lo)s~%(interval_hi)s）。调大＝看得更稀，vision 心跳与判活阈值随之变
"""


def _render_file(values: Tunables) -> str:
    """渲染覆盖层文件的**完整内容**（含注释）。

    手写模板而不是 yaml.safe_dump：这个文件的价值有一半在注释里（「为什么这两个数能改、
    别在这里抄边界」），safe_dump 会把注释全丢掉。代价是写入方整份重渲染 —— 手加的
    注释会在下次写入时被冲掉，所以说明一律写进模板本身。
    """
    stack_lo, stack_hi = _stack_frames_bounds()
    interval_lo, interval_hi = _observe_interval_bounds()
    return _FILE_TEMPLATE % {
        "stack_frames": FIELD_STACK_FRAMES,
        "stack_value": values.stack_frames,
        "stack_default": int(VISION_STACK_FRAMES),
        "stack_lo": stack_lo,
        "stack_hi": stack_hi,
        "observe_interval": FIELD_OBSERVE_INTERVAL,
        "interval_value": f"{values.observe_interval:g}",
        # 出厂默认是档位表里的一档，所以连档位名一起报 —— 光报秒数让人对不回是哪一档
        "interval_default_level": observe_rate_level(DEFAULT_INTERVAL),
        "interval_default": f"{float(DEFAULT_INTERVAL):g}",
        "interval_lo": f"{interval_lo:g}",
        "interval_hi": f"{interval_hi:g}",
    }


def _validate_stack_frames(raw: Any) -> int:
    """写侧校验：非法直接抛（消息里必须带合法区间），不夹取 —— 夹取是读侧的事。"""
    lo, hi = _stack_frames_bounds()
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TunablesError(f"{FIELD_STACK_FRAMES} 必须是整数，可调 {lo}~{hi}（收到 {raw!r}）")
    if raw < lo or raw > hi:
        raise TunablesError(
            f"{FIELD_STACK_FRAMES}={raw} 超出范围，可调 {lo}~{hi}"
            f"（上界＝帧环深度 FRAME_PRODUCER_MAX_FRAMES={FRAME_PRODUCER_MAX_FRAMES}）"
        )
    return int(raw)


def _validate_observe_interval(raw: Any) -> float:
    """写侧校验：非法直接抛（消息里必须带合法区间），不夹取。"""
    lo, hi = _observe_interval_bounds()
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TunablesError(
            f"{FIELD_OBSERVE_INTERVAL} 必须是数字（秒），可调 {lo:g}~{hi:g}（收到 {raw!r}）"
        )
    value = float(raw)
    if value != value or value < lo or value > hi:
        raise TunablesError(
            f"{FIELD_OBSERVE_INTERVAL}={raw} 超出范围，可调 {lo:g}~{hi:g} 秒"
        )
    return value


def update(
    *,
    stack_frames: Optional[int] = None,
    observe_interval: Optional[float] = None,
) -> Tunables:
    """按需覆盖其中一项或两项，返回**写入后**的生效值；非法取值抛 TunablesError。

    只覆盖传进来的字段（None = 这一项不动），所以「CLI 改周期」与「工具改档位」并发时
    两项都在，不是最后写的那个赢。

    并发安全：照 plugin_tools/audio_tools.py 的 _append_interaction_history 那一路 —— 独立锁文件
    flock + **锁内读-改-写** + tmp+rename。锁在另一个文件上是因为 tmp+rename 会换 inode，
    锁在被替换的那个文件上等于没锁。flock 同时覆盖「另一个进程的 CLI」与「同进程的另一个
    线程」（各自 open 拿到的是不同的 open file description，flock 同样串得住）。
    """
    if stack_frames is None and observe_interval is None:
        raise TunablesError("没有要改的项：至少给出 stack_frames 或 observe_interval 之一")

    new_stack = None if stack_frames is None else _validate_stack_frames(stack_frames)
    new_interval = (
        None if observe_interval is None else _validate_observe_interval(observe_interval)
    )

    RUNTIME_VISION_TUNABLES.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNTIME_VISION_TUNABLES_LOCK, "a", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        # 锁内读现值：读-改-写必须整体串行，否则并发两次写入会丢字段
        existing = load()
        merged = Tunables(
            stack_frames=existing.stack_frames if new_stack is None else new_stack,
            observe_interval=(
                existing.observe_interval if new_interval is None else new_interval
            ),
            source="file",
        )
        tmp = RUNTIME_VISION_TUNABLES.with_suffix(".yaml.tmp")
        tmp.write_text(_render_file(merged), encoding="utf-8")
        tmp.replace(RUNTIME_VISION_TUNABLES)

    # 写后立即刷新本进程缓存：同进程 write→read 之间不留一个「读到旧值」的窗口
    _refresh_cache(merged)
    return merged


def reset() -> bool:
    """删掉覆盖层 = 两项都回出厂默认。返回「是否真的删掉了」（本来就没有则 False）。

    只删覆盖层文件，**不动锁文件**（它只是个 flock 靶子，留着不影响任何语义）。
    """
    global _CACHE_KEY, _CACHE_VALUE
    try:
        RUNTIME_VISION_TUNABLES.unlink()
    except FileNotFoundError:
        removed = False
    else:
        removed = True
    with _CACHE_LOCK:
        _CACHE_KEY, _CACHE_VALUE = None, None
    return removed


def _refresh_cache(values: Tunables) -> None:
    """把刚写入的值直接灌进缓存（不等下一轮 mtime 生效）。"""
    global _CACHE_KEY, _CACHE_VALUE
    try:
        st = RUNTIME_VISION_TUNABLES.stat()
        key: Optional[Tuple[int, int, int]] = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        key = None
    with _CACHE_LOCK:
        _CACHE_KEY, _CACHE_VALUE = key, values if key is not None else None


# =============================================================================
# 回显（CLI 与 LLM 工具共用同一份文案）
# =============================================================================

def warnings(values: Tunables) -> Tuple[str, ...]:
    """把这次读到的异常状态（被夹取 / 有未知字段）转成可读告警行。

    为什么要单独一个函数：**运行期的消费方也要能把这些报出来**（观测循环每轮拿一份
    Tunables）。否则一份手改坏的覆盖层只会静默生效 —— 只有碰巧跑过 CLI 的那个人才看得到
    「已夹到区间内」。文案只写一处，免得两边说法不一致。
    """
    lines = []
    for name in values.clamped:
        lines.append(f"{name} 的值非法或越界，已夹到区间内 —— 请改覆盖层那一行。")
    for name in values.unknown_keys:
        lines.append(f"覆盖层里有不认识的字段 `{name}`，**未生效**（是不是拼错了？）。")
    return tuple(lines)


def render(values: Tunables) -> str:
    """把生效值渲染成可读文案（CLI 与 vision_tunables 工具共用）。"""
    stack_lo, stack_hi = _stack_frames_bounds()
    interval_lo, interval_hi = _observe_interval_bounds()
    level = observe_rate_level(values.observe_interval)
    level_text = f"，对应档位 {level}" if level else "（对不上任何档位，是自定义值）"
    where = {
        "file": f"来源 {RUNTIME_VISION_TUNABLES.name}（覆盖层）",
        "default": f"来源 出厂默认（没有覆盖层文件 {RUNTIME_VISION_TUNABLES.name}）",
        "error": f"覆盖层 {RUNTIME_VISION_TUNABLES.name} 读不成（不是合法 YAML？），已回出厂默认",
    }[values.source]

    lines = [
        f"当前视觉观测参数（都是观测器的，不含相机抓拍节奏）：{where}",
        f"  观测周期  {values.observe_interval:g}s{level_text}（可调 {interval_lo:g}~{interval_hi:g} 秒）",
        f"  拼接帧数  {values.stack_frames}    （可调 {stack_lo}~{stack_hi}）",
        # 判活阈值是观测周期的函数，改周期必须让人当场看见它跟着变了
        f"  └ vision 心跳判活阈值随之 = 3×({values.observe_interval:g}+VISION_TIMEOUT) "
        f"= {alive_max_age_sec(values):g}s（status_observer 现读，不用同步改任何常量）",
        f"  改完下一轮观测即生效，不用重启 agent。删掉覆盖层文件即回出厂默认。",
        f"  档位（仅 vision_tunables 工具可用，LLM 只认档位名）：{describe_levels()}",
    ]
    for line in warnings(values):
        lines.append(f"  ⚠️ {line}")
    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

def _build_parser() -> argparse.ArgumentParser:
    interval_lo, interval_hi = _observe_interval_bounds()
    stack_lo, stack_hi = _stack_frames_bounds()
    parser = argparse.ArgumentParser(
        description="视觉观测参数（runtime_files/vision_tunables.yaml）：查看/修改，改完下一轮即生效",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python3 -m Orchestrator.vision_tunables                        # 看当前生效值\n"
            f"  python3 -m Orchestrator.vision_tunables --observe-interval 6   # 每 6 秒观测一次（{interval_lo:g}~{interval_hi:g}）\n"
            f"  python3 -m Orchestrator.vision_tunables --stack-frames 5       # 每轮拼 5 帧（{stack_lo}~{stack_hi}）\n"
            "  python3 -m Orchestrator.vision_tunables --reset                # 删掉覆盖层 = 回出厂默认\n"
            "\n注意：相机抓拍节奏（frame_producer）不在这里，它一直是固定的高频率。\n"
        ),
    )
    parser.add_argument("--observe-interval", type=float, default=None,
                        help=f"观测周期秒（{interval_lo:g}~{interval_hi:g}）")
    parser.add_argument("--stack-frames", type=int, default=None,
                        help=f"每轮拼接帧数（{stack_lo}~{stack_hi}，上界＝帧环深度）")
    parser.add_argument("--reset", action="store_true",
                        help="删掉覆盖层文件，两项都回出厂默认")
    return parser


def _main() -> int:
    args = _build_parser().parse_args()

    if args.reset:
        if args.stack_frames is not None or args.observe_interval is not None:
            print("--reset 不能与其它修改项同时给", file=sys.stderr)
            return 1
        print("覆盖层已删除，回出厂默认。" if reset() else "本来就没有覆盖层文件，无需删除。")
        print(render(current()))
        return 0

    if args.stack_frames is not None or args.observe_interval is not None:
        try:
            values = update(
                stack_frames=args.stack_frames,
                observe_interval=args.observe_interval,
            )
        except TunablesError as exc:
            print(f"未修改：{exc}", file=sys.stderr)
            return 1
        print("已写入覆盖层（下一轮生效）。")
        print(render(values))
        return 0

    print(render(current()))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
