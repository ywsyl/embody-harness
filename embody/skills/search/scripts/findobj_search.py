#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语义寻物（search / 工具名 search_and_approach）端侧客户端。

对接端侧寻物算法服务（topic 交互，参照 skills/search/SKILL.md 与信令文档）：

    发布  /findobj_ctl              智能体 → 算法：控制信令
    订阅  /findobj/search_status    算法 → 智能体：寻物状态
    订阅  /findobj/search_poll      算法 → 智能体：轮询回报状态

三路 topic 均为 std_msgs/msg/String，payload 为 JSON 字符串
{"SN": <机器狗编号>, "prompt": <英文目标描述>, "status": "<两位码>"}。

本脚本是【自包含阻塞式】落地（当前任务循环形态）：一次调用内走完
  发 ctl 00 开启 → 订阅状态 → 发现目标(10/23)则先取消导航、再发 ctl 03 移交运控
  → 算法执行靠近(13) → 本轮结束(19) → 输出机器可读 JSON。
探测期间的跨区移动由上层用 waypoint_nav 分段驱动（非本脚本职责）。

约定（SKILL.md 7.1 新增语义）：
    ctl status 00=开始寻物  01=结束寻物  02=查询寻物
                03=已结束探索运控（已找到目标，停止智能体自身探索运控，移交算法接管执行靠近）
    ctl 信令不再携带 scene（上层串接智能体后已剔除寻物自带导航逻辑）。
    03 勿再当轮询用；需核对当前阶段时用 02（可能触发 /findobj/search_poll 回报）。

运行方式：由 run_findobj_search.sh 包装（source ROS 环境后调用本脚本），
或直接执行并自行保证 ROS_DOMAIN_ID=2 / RMW_IMPLEMENTATION=rmw_cyclonedds_cpp。
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

# 仓库根目录（skills/search/scripts → 上三级），仅用于读设备 SN 等配置，非必需
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import rclpy
from std_msgs.msg import String

# ---- 话题常量 ----------------------------------------------------------------
TOPIC_CTL = "/findobj_ctl"                # 智能体 → 算法（控制）
TOPIC_STATUS = "/findobj/search_status"   # 算法 → 智能体（状态）
TOPIC_POLL = "/findobj/search_poll"       # 算法 → 智能体（轮询回报）

# ---- 状态码含义（SKILL.md 7.5 扬帆总演示版，含播报文案由算法端自行说出）----
CODE_TEXT = {
    "00": "端侧收到开启信令",
    "01": "初始化异常_云端通信失败",
    "02": "初始化异常_云端接流失败",
    "07": "初始化成功，云端开始推理",
    "08": "端侧收到结束信令",
    "09": "轮询状态_后台常驻，等待开启信令",
    "10": "发现目标，开始执行靠近流程",
    "11": "执行完搜索策略，但始终没有发现目标",
    "12": "发现目标后，在靠近过程中丢失目标",
    "13": "成功到达目标旁边",
    "14": "推理过程中拉流异常（超过2s没有有效视频帧）",
    "19": "本轮任务结束，可下发下一次开启命令",
    "21": "重定位成功、要开始导航",
    "22": "成功到达导航点位，360°仔细查看",
    "23": "找到目标",
    "24": "未找到目标",
    "25": "scene 与现有 scene 列表不匹配（直接终止本次寻物）",
    "26": "第一个旋转90°期间",
    "27": "初始化原地找物（无 scene）",
    "28": "当前点位没有、前往下一个点位去找",
    "29": "含义待补充",
    "30": "开启本次寻找",
    "31": "结束本次寻找",
    "32": "暂停本次寻找",
    "33": "继续本次寻找",
}

# 作为“已找到目标”的信号（发现即可移交运控）
_FOUND_CODES = {"10", "23"}
# “未找到”信号（本轮搜索策略耗尽 / 判定无目标）
_NOT_FOUND_CODES = {"11", "24"}
# 初始化失败信号（仅在进入推理(07)前视为致命；07 之后再报同码忽略）
_INIT_FAIL_CODES = {"01", "02"}
# 致命异常信号
_ERROR_CODES = {"14", "25"}
# 到达目标旁
_ARRIVED_CODES = {"13"}
# 靠近中丢失目标
_LOST_CODES = {"12"}
# 进入“推理中”的成功初始化
_RUNNING_CODES = {"07", "30"}
# 本轮结束（唯一“一轮结束”标志，收到后才可下发下一次开启）
_END_CODES = {"19"}


def _normalize_code(raw) -> str:
    """把算法回报的 status 归一化为两位码字符串（"00"…"33"）。"""
    if isinstance(raw, bool):
        return str(raw)
    if isinstance(raw, int):
        return f"{raw:02d}" if 0 <= raw < 100 else str(raw)
    s = str(raw).strip()
    if s.isdigit() and len(s) < 2:
        s = f"{int(s):02d}"
    return s


class FindObjSession:
    """寻物一轮会话：订阅状态 + 按状态机推进，直到本轮结束(19)或超时。

    rclpy 节点由外部创建，本类只负责话题收发与状态机，不关心 rclpy 生命周期。
    """

    def __init__(self, node, sn: str, prompt: str, logger) -> None:
        self.node = node
        self.sn = sn
        self.prompt = prompt
        self.log = logger

        # 回调线程写入、主循环读取的共享区
        self._lock = threading.Lock()
        self._pending: list = []          # 尚未被主循环消费的状态码（按到达顺序）

        # 状态机结果
        self.searching = False            # 是否已进入推理(07/30)
        self.found = False                # 本轮是否发现过目标(10/23)
        self.handed_over = False          # 是否已发 ctl 03 移交运控
        self.arrived = False              # 是否成功到达目标旁(13)
        self.lost = False                 # 靠近过程是否丢失过目标(12)
        self.not_found = False            # 本轮是否判定未找到(11/24)
        self.init_fail = None             # 致命初始化失败码(01/02，未进入推理前)
        self.fatal_error = None           # 致命异常码(14/25)
        self.round_ended = False          # 是否收到本轮结束(19)
        self.codes: list = []             # 收到的全部状态码（消费顺序）
        self.first_code_ts = None

        self.pub = node.create_publisher(String, TOPIC_CTL, 10)
        node.create_subscription(String, TOPIC_STATUS, self._on_status, 10)
        node.create_subscription(String, TOPIC_POLL, self._on_status, 10)

    # ------------------------------------------------------------------ 收发
    def send_ctl(self, status: str) -> None:
        """发布一条控制信令到 /findobj_ctl。"""
        payload = {
            "SN": self.sn,
            "prompt": self.prompt,
            "status": status,
        }
        msg = String()
        msg.data = json.dumps(payload, ensure_ascii=False)
        self.pub.publish(msg)
        self.log(f"[findobj] → ctl status={status} (prompt={self.prompt!r}, SN={self.sn!r})")

    def _on_status(self, msg) -> None:
        """/findobj/search_status 与 /findobj/search_poll 的共用回调。"""
        try:
            body = json.loads(msg.data)
            code = _normalize_code(body.get("status"))
        except Exception as exc:
            self.log(f"[findobj] 状态报文解析失败: {exc} | {msg.data[:200]}")
            return
        with self._lock:
            self._pending.append(code)

    def _drain(self) -> list:
        with self._lock:
            codes = list(self._pending)
            self._pending.clear()
        return codes

    # ------------------------------------------------------------------ 状态机
    def consume(self, code: str) -> None:
        """按序消费一个回报状态，推进一轮结果。"""
        if self.first_code_ts is None:
            self.first_code_ts = time.time()
        self.codes.append(code)
        text = CODE_TEXT.get(code, "未知状态")
        self.log(f"[findobj] ← status={code}（{text}）")

        if code in _FOUND_CODES:
            self.found = True
        if code in _NOT_FOUND_CODES:
            self.not_found = True
        if code in _ARRIVED_CODES:
            self.arrived = True
        if code in _LOST_CODES:
            self.lost = True
        if code in _RUNNING_CODES and not self.searching:
            self.searching = True
            self.init_fail = None   # 已进入推理，早期 01/02 不再视为本轮失败
        if code in _END_CODES:
            self.round_ended = True
        # 进入推理(07)前的初始化失败才算致命；推理中再出现同码视为噪声忽略
        if code in _INIT_FAIL_CODES and not self.searching and self.init_fail is None:
            self.init_fail = code
        if code in _ERROR_CODES and self.fatal_error is None:
            self.fatal_error = code


def _default_sn() -> str:
    """SN 解析优先级：环境变量 FIND_OBJ_SN → 设备配置 DEVICE_ID → 空。"""
    env = os.environ.get("FIND_OBJ_SN", "").strip()
    if env:
        return env
    try:
        from configs.paths import DEVICE_ID
        # DEVICE_ID 在配置缺失时为 "unknown"，当 SN 用无意义，按无 SN 处理
        return "" if DEVICE_ID == "unknown" else DEVICE_ID.strip()
    except Exception:
        return ""


def _best_effort_cancel_navigation(log) -> None:
    """尽力取消当前导航任务（探测途中发现目标、先停自身探索运控）。

    通过导航后端 HTTP（POST /api/v1/navigation/cancel）取消；导航空闲时该调用
    返回失败也无妨（无需取消）。永不抛异常。
    """
    try:
        from configs.configs import NAVIGATION_CONTROL_TIMEOUT
        from plugin_tools.nav_tools import cancel_navigation
        # nav_tools 原样返回应答（dict / 非 JSON 原文 / ""），这里只记一笔，不判成败
        ret = cancel_navigation(NAVIGATION_CONTROL_TIMEOUT)
        log(f"[findobj] 取消导航（移交运控前停探索运控）: {ret}")
    except Exception as exc:
        log(f"[findobj] 取消导航失败（忽略，继续移交）: {exc}")


def _build_result(session, args, reason: str, now: float) -> dict:
    """把一轮会话结果汇总成给上层/LLM 的机器可读 JSON。"""
    found = session.found
    arrived = session.arrived
    not_found = session.not_found
    fatal = session.fatal_error or session.init_fail

    # 判定终态（优先级：致命异常 > 找到并到达 > 找到但丢失/未到达 > 未找到 > 超时）
    if fatal is not None and not found:
        state = "error"
        message = (
            f"寻物失败（初始化失败 status={fatal}）：{CODE_TEXT.get(fatal, fatal)}"
            if session.init_fail else
            f"寻物异常（status={fatal}）：{CODE_TEXT.get(fatal, fatal)}，已结束本轮"
        )
    elif found and arrived:
        state = "found_and_arrived"
        message = f"已找到目标（{args.prompt}）并已靠近到它旁边。"
    elif found and session.lost:
        state = "found_lost"
        message = f"找到过目标（{args.prompt}），但靠近过程中丢失，未能到达目标旁。"
    elif found:
        state = "found"
        message = f"已找到目标（{args.prompt}），正在靠近。"
    elif not_found:
        state = "not_found"
        message = f"在当前位置附近未找到目标（{args.prompt}）。"
    elif reason == "timeout":
        state = "timeout"
        message = "寻物超时仍未结束，已主动结束本轮。"
    else:
        state = "unresolved"
        message = "寻物未收到明确结束信号。"

    return {
        "found": bool(found),
        "state": state,
        "prompt": args.prompt,
        "sn": session.sn,
        "message": message,
        "last_status": session.codes[-1] if session.codes else "",
        "round_ended": session.round_ended,
        "received_codes": session.codes,
        "elapsed_sec": round(now - (session.first_code_ts or now), 1),
    }


def _out(json_obj) -> None:
    """stdout 只输出最终 JSON 一行（供上层解析）；诊断一律走 stderr。"""
    sys.stdout.write(json.dumps(json_obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_search(args, log) -> int:
    """执行一轮寻物，输出结果 JSON，返回进程退出码。"""
    if args.dry_run:
        log(f"[findobj] DRY-RUN: sn={args.sn!r} prompt={args.prompt!r} "
            f"topics={TOPIC_CTL},{TOPIC_STATUS},{TOPIC_POLL}")
        _out({
            "found": False,
            "state": "dry_run",
            "prompt": args.prompt,
            "sn": args.sn,
            "message": "dry-run 模式未与端侧寻物服务交互",
            "last_status": "",
            "round_ended": False,
            "received_codes": [],
            "elapsed_sec": 0.0,
        })
        return 0

    rclpy.init(args=[])
    node = rclpy.create_node("findobj_search_client")
    session = FindObjSession(node, args.sn, args.prompt, log)

    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start()

    reason = ""
    deadline = time.time() + args.timeout
    query_deadline = time.time() + args.no_new_timeout  # 长时间无新码时发 02 核对
    settle_deadline = None                              # 得出可收尾结果后再等 19 的窗口
    interrupted = False

    def _interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)

    try:
        # ---- 1) 开启探测 ----
        session.send_ctl("00")

        # ---- 2) 状态推进循环 ----
        while True:
            if interrupted:
                reason = "interrupted"
                break

            now = time.time()
            if now >= deadline:
                reason = "timeout"
                break
            # 得出可收尾结果后再等 19 的窗口到期：按已得结果收尾
            if settle_deadline is not None and now >= settle_deadline:
                reason = "settled"
                break

            # 消费所有已到达的状态码
            for code in session._drain():
                session.consume(code)
                # 发现目标：先取消导航（停自身探索运控），再发 ctl 03 移交算法接管靠近
                if code in _FOUND_CODES and not session.handed_over:
                    _best_effort_cancel_navigation(log)
                    session.send_ctl("03")
                    session.handed_over = True

            if session.round_ended:
                reason = "round_ended"
                break

            # 已经得出可收尾结果（到达/未找到/致命异常/初始化失败）：进入 settle 窗口等 19，
            # 窗口内没等来 19 也按已得结果收尾（不再占满整个总超时）。
            terminal = (
                session.arrived
                or session.not_found
                or session.fatal_error is not None
                or session.init_fail is not None
            )
            if terminal and settle_deadline is None:
                settle_deadline = min(deadline, time.time() + args.settle)

            # 长时间没有任何新状态：发 ctl 02 查询核对（03 已改语义，勿当轮询用）；
            # 总超时兜底，绝不把查询窗口拉出总超时之外。
            if not terminal and time.time() >= query_deadline and not session.round_ended:
                session.send_ctl("02")
                query_deadline = time.time() + args.no_new_timeout

            time.sleep(0.2)

        # ---- 3) 收尾：本轮未观察到 19 结束时，主动发 01 收口并等 08/19 ----
        # 只有收到 19 才说明算法侧本轮真正结束、可下发下一次开启命令；
        # 若因 settle/超时/被打断而退出但未见 19，主动 ctl 01 让端侧关闭本轮，
        # 避免占用会话导致后续开启(00)不生效。已到 19 则无需再发。
        if not session.round_ended:
            session.send_ctl("01")
            cleanup_deadline = time.time() + args.end_grace
            while time.time() < cleanup_deadline and not session.round_ended:
                for code in session._drain():
                    session.consume(code)
                if not session.round_ended:
                    time.sleep(0.3)
            if session.round_ended:
                reason = "round_ended"
            else:
                log("[findobj] 主动结束后仍未等到 19，按当前结果收尾")

    finally:
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            pass

    result = _build_result(session, args, reason, time.time())
    _out(result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="语义寻物（search_and_approach）端侧客户端：一次调用完成 开启→探测→移交→靠近→收尾",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--prompt", required=True,
                        help="目标物体的英文简单描述（如 sofa / red cup / man in red clothes）")
    parser.add_argument("--sn", default=None,
                        help="机器狗编号 SN；缺省取环境变量 FIND_OBJ_SN 或设备配置 factory/devSn")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="本轮总超时（秒）：超时仍未到 19 则主动 ctl 01 结束")
    parser.add_argument("--no-new-timeout", type=float, default=60.0,
                        help="持续多少秒无任何新状态时发 ctl 02 查询核对")
    parser.add_argument("--settle", type=float, default=20.0,
                        help="到达/未找到/致命异常等可收尾结果后再等 19 的最长秒数")
    parser.add_argument("--end-grace", type=float, default=10.0,
                        help="主动结束/超时后等待 19 收尾的最长秒数")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印参数与话题，不初始化 ROS（调试/自检用）")
    args = parser.parse_args()

    prompt = (args.prompt or "").strip()
    if not prompt:
        print("[findobj] 错误: --prompt 不能为空", file=sys.stderr)
        return 2
    if args.sn is None or not str(args.sn).strip():
        args.sn = _default_sn()
    else:
        args.sn = str(args.sn).strip()

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        return run_search(args, log)
    except Exception as exc:
        # 本地/环境异常（ROS 不可用等）：进程级失败，非正常寻物结果
        print(f"[findobj] 本地执行异常: {exc}", file=sys.stderr)
        _out({
            "found": False,
            "state": "error",
            "prompt": prompt,
            "sn": getattr(args, "sn", ""),
            "message": f"寻物调用本地失败: {exc}",
            "last_status": "",
            "round_ended": False,
            "received_codes": [],
            "elapsed_sec": 0.0,
        })
        return 1


if __name__ == "__main__":
    sys.exit(main())
