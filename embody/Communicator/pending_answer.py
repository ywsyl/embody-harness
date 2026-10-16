"""
pending_answer — 云端 action="answer" 事件用户回复的**落盘单槽**（同机跨进程）

**当前整条链路停用，本模块没有任何调用方**：写侧 Communicator/receiver.py 自 2026-09-28 起
把 answer 事件连同这里的一起拦下（见 _forward_action 里的注释），读侧 plugin_tools/
audio_tools.py 的 `_await_cloud_answer` 也随 listen / speak_listen 改回本地 ASR 于
2026-10-08 注释掉。两边都只注释、没删，恢复答案链路时一起放开。

原本的用法（恢复后仍是这套）：receiver 收到云端 answer 事件后把用户回答原文写进这里；
角色侧的 listen / speak_listen 工具在"问完一句"之后等这个槽里的 seq 往前走，取到的就是本轮
用户回复。

为什么是文件而不是进程内 Condition
----------------------------------
receiver 在**组件进程**（start_agent.py 起的那套），工具是**角色 dsh 进程**的子进程
（dsh 的 tools-bridge 插件按调用起一次 python 桥），两者不同进程。原先那版用模块级
threading.Condition 做线程间通知，成立的前提是"receiver 的工作线程与工具同进程"——
那是 cmcc 单智能体树的形态，本树里已经不成立（跨进程读到的永远是空槽，工具只能等到超时）。

落盘口径
--------
- 路径登记在 `configs/paths.py` 的 RUNTIME_PENDING_ANSWER，写用 tmp + os.replace（与
  receiver 心跳、各份 robot_status 同一套写法）：读侧看到的永远是完整 JSON。
- **写者只有一个**（receiver 的 ROS 回调线程），读者是若干工具进程，故不需要锁；
  seq 只增不减，是"这槽里当前是第几条回答"的单调计数。
- 文件不存在、或正被替换（读到半截/读不动）一律当 seq=0、内容为空：没云端在跑时就是这条路。

接口：
    submit(content)        写入最新用户回复（seq 自增后整份覆盖）
    latest_seq()           当前最新 seq（工具在"问"之前记下基准）
    wait_for_answer(...)   等 seq 超过基准，返回最新 content，超时返回空串
"""

from __future__ import annotations

import json
import os
import time
from typing import Tuple

from configs.paths import RUNTIME_PENDING_ANSWER

# 轮询间隔：写侧是事件驱动的一次性覆盖，读侧只在这一个文件上 stat+读；0.2s 与
# rolebus 收件轮询（transport.ts 的 pollIntervalMs 缺省 200）同一个量级，新回答的
# 感知延迟不超过一档。窗口（等多久）由调用方给，见 configs.CLOUD_ANSWER_WAIT。
_POLL_INTERVAL = 0.2


def _read_slot() -> Tuple[int, str]:
    """读当前槽；文件缺失/半截/字段不合型都按空槽（seq=0, 内容空串）处理。"""
    try:
        data = json.loads(RUNTIME_PENDING_ANSWER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, ""
    if not isinstance(data, dict):
        return 0, ""
    seq = data.get("seq")
    content = data.get("content")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return 0, ""
    return seq, content if isinstance(content, str) else ""


def submit(content: str) -> None:
    """写入最新用户回复，seq 自增后整份原子覆盖。

    @raises OSError: 落盘失败（调用方负责记录，别让写失败静默：写不进去等于工具收不到回答）。
    """
    seq, _ = _read_slot()
    path = RUNTIME_PENDING_ANSWER
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"seq": seq + 1, "content": content}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def latest_seq() -> int:
    """当前最新 seq（空槽为 0）。"""
    return _read_slot()[0]


def wait_for_answer(base_seq: int, timeout: float) -> str:
    """等待 seq 超过 base_seq（新的 answer 事件到达），返回最新 content。

    超时返回空串 —— 与"用户没回答"同一个结果，由调用方决定怎么向模型描述。
    """
    deadline = time.monotonic() + timeout
    while True:
        seq, content = _read_slot()
        if seq > base_seq:
            return content
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        time.sleep(min(_POLL_INTERVAL, remaining))
