"""
Cloud Sender / 云端上报模块

端侧 → 云端的上报通路。所有云端上报统一走这里。

**通路**（2026-10-15 从 HTTP POST 换成**本体信令**）：把状态快照组进一条报文，经
`/homi_speech/sigc_data_service`（`homi_speech_interface/srv/SIGCData`，请求体是一个字符串）
下发 —— 那个字符串就是这条报文的 JSON。**成功 = 服务应答 `error_code == 0`**。云端收没收到、
有没有反应由它自己的链路负责，本模块不订阅 `/homi_speech/sigc_event_topic` 上的回包
（那条话题上跑的是云端**下发**事件，归 Communicator/receiver.py）。

**报文信封**（字段名是平台侧契约，别改名）：

    {"deviceId", "domain", "event", "eventId", "seq", "response",
     "params": {"route"}, "body": {"data": {…快照…, "announce"}}}

**云端什么时候读这条数据**由 `body.data.announce` 决定（与快照字段同层）：
  - 不填 / false —— 云端只把它存进状态缓存，等它自己按 read 来取时才看得到；
  - true         —— 云端立刻按这条数据通知大模型。
所以「这一条要不要马上叫醒云端」是**调用方**的决定（见 `send_task_progress` 的 `announce`），
本模块不替谁定；缺省 false（只保存）。

**自己一份 rclpy 上下文**（`Context()` + `SingleThreadedExecutor`），不碰进程的默认上下文：
组件进程里那份归 receiver，而 rclpy 上下文是进程级的、**不允许二次 init**；本模块的调用方
又是「一次工具调用一个新进程」的 plugin_tools 子进程。两种情形都得能起，做法与
Motion_Manager/motion_status.py 相同（实测理由写在那个文件头）。

**rclpy 与语音包不在导入期 import**：plugin_tools 每次 list/call 都要 import 本模块，而工具
子进程不一定有 ROS 环境；放在导入期会让整个工具面跟着起不来。取不到就是这一次上报失败（-2）。
"""

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 确保 reference.paths 可导入
_CMCC_ROOT = Path(__file__).parent.parent.resolve()
if str(_CMCC_ROOT) not in sys.path:
    sys.path.insert(0, str(_CMCC_ROOT))

from configs.agents import TASK_STATUS
from configs.configs import CLOUD_REPORT_TIMEOUT
from configs.paths import DEVICE_ID, ROS_SIGC_DATA_SERVICE
from plugin_tools.rolebus import task_report


# =============================================================================
# 报文协议常量
# =============================================================================

#: 上报所在的领域 / 事件名 / 路由。三个都是**平台侧给的固定值**（见文件头的信封说明）：
#: 端侧上报只做「写快照」这一件事，所以 route 恒为 write_state。
DOMAIN_ALGORITHM_DEVICE_INTERACTION = "ALGORITHM_DEVICE_INTERACTION"
EVENT_LLM_ROUTE_FORWARD = "llm_route_forward"
ROUTE_WRITE_STATE = "write_state"


# =============================================================================
# 本体信令通路（SIGCData 服务客户端）
# =============================================================================

#: 本进程的信令通路：(node, client, request_cls, executor, rclpy)。**一个进程只建一次** ——
#: 建节点含 DDS 发现（1~2s），而一次工具调用通常要上报两条（如"开口问用户"前后各一条），
#: 重建不值当。建不起来时不缓存失败：下次调用再试一次，失败的代价只是几行 import。
_SIGNAL_CHANNEL: Optional[Tuple[Any, ...]] = None
_CHANNEL_LOCK = threading.Lock()


def _open_channel() -> Tuple[Any, ...]:
    """建（或复用）本进程的信令通路，返回 (node, client, request_cls, executor, rclpy)。

    自己一份 rclpy 上下文，不碰进程的默认上下文：组件进程里那份归 receiver，而 rclpy 的
    上下文**不允许二次 init**（实测报 `rcl_init called on an already initialized context`）
    —— 与 Motion_Manager/motion_status.py 同一套做法、同一条理由。工具子进程里默认上下文
    没人用，走同一份照样成立。

    rclpy 与语音包在**这里**才 import（见文件头）。建不起来就抛，由 `_send_message` 兜住
    并回报 -2 —— 一次上报失败，不连累调用方的其余逻辑。
    """
    global _SIGNAL_CHANNEL
    with _CHANNEL_LOCK:
        if _SIGNAL_CHANNEL is not None:
            return _SIGNAL_CHANNEL

        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from homi_speech_interface.srv import SIGCData

        context = Context()
        rclpy.init(context=context)
        node = rclpy.create_node("cloud_sender", context=context)
        client = node.create_client(SIGCData, ROS_SIGC_DATA_SERVICE)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)

        _SIGNAL_CHANNEL = (node, client, SIGCData, executor, rclpy)
        return _SIGNAL_CHANNEL


def _send_message(message: str) -> Tuple[int, str]:
    """把一条报文经信令服务下发，返回 (状态码, 失败原因)；成功时原因是空串。

    状态码见 `send_task_progress` 的 Returns。**本函数只回报、不打印**：打印留给调用方，
    好把任务号之类的上下文一起带上（与改动前的日志形状一致）。
    """
    try:
        _node, client, request_cls, executor, _rclpy = _open_channel()
    except Exception as exc:  # noqa: BLE001 —— 环境缺失/建节点失败都归这一类，只影响本次上报
        return -2, f"信令通路起不来: {exc}"

    # 就绪等待与应答等待**共用** CLOUD_REPORT_TIMEOUT 这一个预算（别让它变成两倍）。
    deadline = time.monotonic() + CLOUD_REPORT_TIMEOUT

    # 客户端刚建好时 DDS 发现还没完成，盲发会丢（motion_status 实测：第一笔等满超时也没响应，
    # 紧接着的第二笔毫秒级就回来）。所以先等服务就绪，不发盲包。
    if not client.wait_for_service(timeout_sec=CLOUD_REPORT_TIMEOUT):
        return -2, f"{ROS_SIGC_DATA_SERVICE} 不可用"

    request = request_cls.Request()
    request.data = message
    try:
        future = client.call_async(request)
        executor.spin_until_future_complete(
            future, timeout_sec=max(0.0, deadline - time.monotonic())
        )
    except Exception as exc:  # noqa: BLE001 —— 服务端异常/被取消都归这一类
        return -4, f"调用 {ROS_SIGC_DATA_SERVICE} 异常: {exc}"

    if not future.done():
        return -3, f"调用 {ROS_SIGC_DATA_SERVICE} 超时（{CLOUD_REPORT_TIMEOUT:g}s）"
    try:
        error_code = future.result().error_code
    except Exception as exc:  # noqa: BLE001 —— 取应答本身出错，与调用异常同归一类
        return -4, f"取信令服务应答异常: {exc}"

    if error_code != 0:
        return -5, f"云端返回 error_code={error_code}"
    return 0, ""


# =============================================================================
# 报文组装
# =============================================================================

def build_message(state: Dict[str, Any], announce: bool) -> Dict[str, Any]:
    """组一条上报报文（信封 + `body.data` 快照）。

    `announce` 写在 `body.data` 里、与快照字段同层：不填或 false 时云端只保存，true 才立刻
    通知大模型（见文件头）。`eventId` 每次新生成（全局唯一，云端按它去重/关联），`seq` 用
    毫秒时间戳。
    """
    now_ms = int(time.time() * 1000)
    return {
        "deviceId": DEVICE_ID,
        "domain":   DOMAIN_ALGORITHM_DEVICE_INTERACTION,
        "event":    EVENT_LLM_ROUTE_FORWARD,
        "eventId":  str(uuid.uuid4()),
        "seq":      now_ms,
        "response": True,
        "params":   {"route": ROUTE_WRITE_STATE},
        "body":     {"data": {**state, "announce": bool(announce)}},
    }


# =============================================================================
# 任务进度上报
# =============================================================================

# 快照的 4 个必填字段（与云端对齐的 Schema）
TASK_STATE_FIELDS = (
    "task_id",             # 任务唯一标识（时间戳 + 任务名）
    "task_status",         # running / paused / completed / failed
    "task_description",    # 人类可读任务描述（直接用于 LLM prompt）
    "current_step_desc",   # 人类可读当前步骤描述
)


def send_task_progress(data: Dict[str, Any], announce: bool = False) -> int:
    """把一份任务状态快照上报云端（经本体信令，见文件头）。

    入参 data 为任务状态快照，需包含 4 个必填字段（与端侧同事对齐的 Schema）：
      - task_id            任务唯一标识（时间戳 + 任务名），用于关联端侧全量轨迹
      - task_status        任务状态枚举：running / paused / completed / failed
      - task_description   人类可读任务描述，直接用于 LLM prompt
      - current_step_desc  人类可读当前步骤描述

    任务进行中应高频周期调用以保持状态存活（云端侧 TTL 10s），
    任务完成时须写一次 task_status=completed 再停写。

    Args:
        data:     任务状态快照（含上面 4 个必填字段）
        announce: 是否让云端**立刻按这条数据通知大模型**。缺省 False = 云端只保存，
                  等它自己来读。要不要马上叫醒云端由调用方定（见文件头）。

    Returns:
        0   上报成功（信令服务应答 error_code == 0）
        -1  参数校验失败（缺少必填字段）
        -2  信令通路不可用（rclpy / 语音包导不进来，或服务没起来）
        -3  调用超时（CLOUD_REPORT_TIMEOUT）
        -4  调用异常
        -5  云端返回非 0 error_code

    可选字段（调用方可传入，未提供时自动补齐默认值，云端 schema 要求上报时始终携带）：
      - waiting_for_user   布尔值，是否正在等待用户输入/回复，默认 false
      - pending_question   字符串，等待用户时向用户提出的问题，默认 ""
    """
    missing = [field for field in TASK_STATE_FIELDS if field not in data]
    if missing:
        print(f"[上报失败] task_progress, 缺少必填字段: {missing}")
        return -1

    # 补齐可选字段默认值（云端 schema 要求始终携带，未提供时补默认）
    payload = dict(data)
    payload.setdefault("waiting_for_user", False)
    payload.setdefault("pending_question", "")

    message = json.dumps(build_message(payload, announce), ensure_ascii=False)
    ret, reason = _send_message(message)
    if ret != 0:
        print(f"[上报失败] task_progress task_id={payload.get('task_id')}, {reason}")
        return ret

    print(
        f"[上报成功] task_progress task_id={payload.get('task_id')}, "
        f"task_status={payload.get('task_status')}, "
        f"listen_event={payload.get('waiting_for_user')}, announce={announce}"
    )
    return 0


def report_waiting_state(task_id: str, waiting_for_user: bool,
                         pending_question: str = "", announce: bool = False) -> None:
    """向云端上报"是否正在等待用户回答"。

    task_id 空则不上报：云端按任务号归档这类上报，没有任务号就无处可归。

    task_status 一律 EXECUTING（configs/agents.py 的 TASK_STATUS）：任务还在执行，只是卡在
    等用户回话这一步——这一层语义由快照里的 waiting_for_user / pending_question 带上，
    不另占一个状态值。任务描述与最新进展取自归档里这个任务号的原始文本（rolebus.task_report），
    编排器没发过就是空串——不替上游编。

    announce 透传给 send_task_progress（要不要让云端立刻通知大模型，见文件头），缺省 False。

    调用方是「要等用户回话」的那几个工具（speak_listen / audio_interact），各自成对调用
    （开口前 true、收工后 false）。
    """
    if not task_id:
        return
    try:
        report = task_report(task_id)
        snapshot = {
            "task_id": task_id,
            "task_status": TASK_STATUS["EXECUTING"]["value"],
            "task_description": report["task_description"],
            "current_step_desc": report["current_step_desc"],
            "waiting_for_user": bool(waiting_for_user),
            "pending_question": pending_question if waiting_for_user else "",
        }
        send_task_progress(snapshot, announce=announce)
    except Exception as exc:
        print(f"    [等待用户] 上报等待用户状态失败: {exc}")


# =============================================================================
# 测试入口
# =============================================================================

def main() -> None:
    """打印（或 `--send` 真发）一条样例上报的报文。

    缺省只打印：核对报文字段不必真往云端写一条。加 `--send` 才经信令服务下发。
    """
    import argparse

    parser = argparse.ArgumentParser(prog="sender.py", description=__doc__.splitlines()[1])
    parser.add_argument("--send", action="store_true",
                        help="真经 /homi_speech/sigc_data_service 下发（缺省只打印报文）")
    parser.add_argument("--announce", action="store_true",
                        help="报文里 body.data.announce 置 true（让云端立刻通知大模型）")
    args = parser.parse_args()

    snapshot = {
        "task_id":           "local-261015143000",
        "task_status":       "running",
        "task_description":  "让机器人趴下",
        "current_step_desc": "正在执行趴下动作",
    }

    print("=" * 50)
    message = build_message(
        {**snapshot, "waiting_for_user": False, "pending_question": ""}, args.announce
    )
    print(f"报文（{ROS_SIGC_DATA_SERVICE}）:")
    print(json.dumps(message, ensure_ascii=False, indent=2))
    print("=" * 50)

    if not args.send:
        print("（只打印，未下发；要真发加 --send）")
        return
    ret = send_task_progress(snapshot, announce=args.announce)
    print(f"返回值: {ret}")


if __name__ == "__main__":
    main()
