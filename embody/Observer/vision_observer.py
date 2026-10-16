#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
视觉观测器（Vision Observer）

每 N 秒从共享帧环取最新 M 帧拼成一张图（M 帧自上而下、时间由远到近）
做识别，并把
  - 本轮启动时的时间戳
  - 大模型回复
追加记录到 runtime_files/vision_obs.yaml（仓库根下，路径见 configs/paths.py 的
RUNTIME_VISION_OBSERVATION）。

**观测周期 N 与每轮拼几帧 M 都是运行时可调**的，不重启就能改：见
Orchestrator/vision_tunables.py（文件 runtime_files/vision_tunables.yaml，人用 CLI、
LLM 用 vision_tunables 工具改周期档位）。N 的出厂默认 = configs.DEFAULT_INTERVAL，
它是**档位表 VISION_OBSERVE_RATE_LEVELS 里的一档**（按档位名取，不是写死的秒数）——
默认值与该表同源，就不会再出现「默认值对不上任何档位」那种脱钩。
抓拍节奏**不在这份覆盖层里**：帧生产者一直按固定的高频率抓帧攒环，本观测器只决定
「隔多久去看一眼帧环」，不去动相机。

帧来源：skills/camera/frame_producer.py —— 由统一启动入口 start_agent.py 按开关拉起的
**取帧服务**（独立子进程 `frame_producer.py --serve`：周期抓拍、滚动帧环，深度见
configs.FRAME_PRODUCER_MAX_FRAMES）。帧环在那个服务进程的内存里，「全系统只有一个」
由 frame_producer.start() 持 FRAME_PRODUCER_LOCK_FILE 独占锁保证。
本观测器**不接收任何帧句柄**：它经规范包路径调 frame_producer 的模块级取帧函数，由后者
按三条规则分派（本进程在抓拍 → 本地帧环；取帧服务可达 → unix socket 取帧；都不行 →
本地空环）—— 所以取帧不落盘、不各自 new、不在退出时 stop()（stop 只关本进程的连接，
不停服务；服务的生死归 start_agent）。
「观测器依赖取帧服务」：没有它就没有帧，start_agent 会拒绝
「--no-frame-producer 但留着视觉观测器」这种组合。
独立 CLI 调试时 start() 幂等兜底可自拉生产者（此时它自己就是生产者，走本地帧环）；
若取帧服务在跑（或另一进程已持有生产者锁）→ 本进程不重复抓拍，直接用服务取帧。
注意：生产者**不占用相机设备**——2026-09-22 起抓拍改成读共享内存
（skills/camera/scripts/capture.py 读 /tmp/neck_jpeg），不再走 capture.sh →
capture_direct 那条 `fuser -k` 抢占设备的老路，故运行期间不影响本体视频流；
抓不到帧只告警。

识别：进程内把拼接图 JPEG 转 Base64，直连 VQA 接口（configs 的
VISION_LOCAL_VQA_URL）返回回复文本，全程无临时文件、无子进程。
发送的图是帧环最新 M 帧连续拼接，自上而下时间由远到近。
⚠️ VISION_PROMPT 里写死了「几帧 / 过去几秒」时，它不会随 M 变：M ≠ prompt 里的帧数时，
模型会按 prompt 说的去理解这张图，而它的回复会被写进 vision_obs.yaml、被 task_loop 当事实读。

使用：
    python3 Observer/vision_observer.py --interval 5   # 钉死 5s（覆盖层不参与，调试用）
    python3 Observer/vision_observer.py                # 按运行时覆盖层/出厂周期的观测周期持续运行
    python3 -m Orchestrator.vision_tunables                 # 看/改「隔多久看一次」「每次拼几帧」（不用重启）
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml

# 确保项目根目录在 Python 搜索路径中（用于导入 configs 包）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import (
    DEFAULT_INTERVAL,
    VISION_TIMEOUT,
    VISION_MAX_ENTRIES,
    VISION_LOCAL_VQA_URL,
)
from configs.paths import (
    DEVICE_SN,
    RUNTIME_VISION_OBSERVATION,
    VISION_OBSERVER_HEARTBEAT_FILE,
)
from configs.prompts import VISION_PROMPT
# 视觉观测参数的**运行时可调**覆盖层：本观测器每轮取一次「观测周期」与「拼几帧」。
# 抓拍节奏不在这里（帧生产者一直固定高频抓帧），见 Orchestrator/vision_tunables.py 的模块头。
from Orchestrator import vision_tunables

# 取帧入口：由统一启动入口 start_agent.py 拉起**取帧服务子进程**（frame_producer.py --serve），
# 本模块只是它的客户端之一。必须经规范包路径导入，与 start_agent.py / 视觉工具共用同一
# sys.modules 实例 —— 进程内的连接缓存（_SERVICE_CLIENT）与分派规则都挂在这个模块上。
from skills.camera import frame_producer as _frame_source  # noqa: E402


# =============================================================================
# 常量
# =============================================================================

# 重试等待首帧的间隔（秒）
_RETRY_POLL_INTERVAL = 2.0


# =============================================================================
# 帧源：取帧服务（由 start_agent 拉起；独立 CLI 时自拉生产者兜底）
# =============================================================================
# 本模块顶部已通过规范包路径导入 _frame_source = skills.camera.frame_producer，
# 取帧一律走它的模块级函数（run() 里先调一次 start()）—— 由那个模块决定帧从哪来：
# 本进程在抓拍（独立 CLI 兜底）就用本地帧环，否则经 FRAME_PRODUCER_SOCKET 找取帧服务。
#
# 观测器只是消费者：
#   - 不 new 自己的 producer；start() 幂等：服务在跑就给个客户端句柄，绝无第二个抓拍线程。
#   - **不把句柄存下来**：就绪判定用一次 start()，之后的读取全走模块级函数。句柄是「那一张
#     连接」的凭证，一次失败就作废且对象自己不会重连（见 frame_producer.FrameServiceClient），
#     存着它等于一次网络抖动之后永久「连接已关闭」；模块级函数每次现取连接、失败不抛，
#     观测器正是 _call 那句「抛异常会打死观测器那条循环」所指的循环。
#   - 退出时不 stop()：句柄的 stop() 只关本进程那条连接（服务由 start_agent 收），
#     但本进程若自拉过生产者，收摊归它自己 —— 这里不主动 stop，避免误停别人依赖的东西。
#
# 读接口（服务端与本地帧环同名，换源时不必改调用点）：
#   _frame_source.latest_frame_id() / wait_for_first_frame() / get_stacked_frame(fmt)
#   / wait_for_new_frame(after_id, timeout)。


# =============================================================================
# VQA 识别（进程内直连，不落盘）
# =============================================================================

def query_vqa(jpeg_bytes: bytes, prompt: str) -> str:
    """把单帧 JPEG 字节直连 VQA 接口识别，返回应答体原文。

    payload 与 skills/vision/scripts/vision_local.py 的 request_vqa 保持一致
    （image_source=external + image_base64 + 设备 SN 当 session_id），HTTP 非 200
    抛错交由上层转为记录。session_id 用 configs/paths.py 的 DEVICE_SN（设备配置
    文件里的 SN，非硬编码）；SN 读不到时是空串，整个字段不传、交服务端自己生成。
    回复文本要从应答体里解出来，见 _decode_reply_text。
    """
    image_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    payload = {
        "image_source": "external",
        "image_base64": image_b64,
        "prompt": prompt,
    }
    if DEVICE_SN:
        payload["session_id"] = DEVICE_SN
    resp = requests.post(
        VISION_LOCAL_VQA_URL,
        json=payload,
        timeout=VISION_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"请求失败: {resp.status_code}\n{resp.text}")
    return resp.text


def _decode_reply_text(text: str) -> str:
    """从视觉服务应答体里取出回复文本。

    服务端把整段回复当 JSON 字符串编码（应答体两端带引号、内部引号转义），
    原样写进 vision_obs.yaml 会让每条 reply 都套一层引号；能解成字符串就取它，
    解不出（纯文本应答、错误页等）原样返回。解成对象/数组时同样原样返回 ——
    这份文件存的是给人读的观察描述，不是结构化结果。
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    return parsed if isinstance(parsed, str) else text


def observe_frame(jpeg_bytes: bytes) -> Dict[str, str]:
    """对一张 JPEG（多帧拼接图）做一次识别，返回一条记录。

    时间戳在发起识别之前记录，表示「本轮开始观测」的时刻。
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        reply = _decode_reply_text(query_vqa(jpeg_bytes, VISION_PROMPT)).strip()
    except requests.Timeout:
        reply = f"[错误] 视觉识别超时（>{VISION_TIMEOUT}s）"
    except requests.RequestException as exc:
        reply = f"[错误] {exc}"
    except Exception as exc:  # noqa: BLE001
        reply = f"[错误] {exc}"

    return {"timestamp": timestamp, "reply": reply}


# =============================================================================
# YAML 读写
# =============================================================================

def _trim_observations(
    observations: List[Dict[str, str]],
    max_entries: Optional[int],
) -> List[Dict[str, str]]:
    """只保留最近 max_entries 条；max_entries 为空或非正数时不裁剪。"""
    if max_entries and max_entries > 0:
        return observations[-max_entries:]
    return observations


def _load_observations(
    max_entries: Optional[int] = None,
) -> List[Dict[str, str]]:
    """读取已记录的观测数据，文件不存在或格式异常时返回空列表。"""
    obs: List[Dict[str, str]] = []
    if RUNTIME_VISION_OBSERVATION.exists():
        try:
            data = yaml.safe_load(
                RUNTIME_VISION_OBSERVATION.read_text(encoding="utf-8")
            )
        except Exception:
            data = None
        if isinstance(data, dict):
            loaded = data.get("observations")
            if isinstance(loaded, list):
                obs = [o for o in loaded if isinstance(o, dict)]
    return _trim_observations(obs, max_entries)


def _save_observations(
    observations: List[Dict[str, str]],
    max_entries: Optional[int] = None,
) -> None:
    """原子写入观测数据到 vision_obs.yaml（可选：只保留最近 max_entries 条）。"""
    observations = _trim_observations(observations, max_entries)
    RUNTIME_VISION_OBSERVATION.parent.mkdir(parents=True, exist_ok=True)
    data = {"observations": observations}
    tmp = RUNTIME_VISION_OBSERVATION.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
    tmp.replace(RUNTIME_VISION_OBSERVATION)


# =============================================================================
# 观测逻辑
# =============================================================================

def _record_and_report(
    observations: List[Dict[str, str]],
    jpeg_bytes: bytes,
    max_entries: int,
) -> List[Dict[str, str]]:
    """识别一张拼接图并写入 yaml，返回更新（裁剪后）的观测列表。"""
    record = observe_frame(jpeg_bytes)
    observations.append(record)
    observations = _trim_observations(observations, max_entries)
    _save_observations(observations, max_entries=max_entries)

    # print(
    #     f"[VisionObserver] {record['timestamp']} -> {record['reply'][:80]}",
    #     flush=True,
    # )
    return observations


# =============================================================================
# 主循环
# =============================================================================

def _write_heartbeat() -> None:
    """写一次存活心跳，供 Observer/status_observer.py 判本线程还在不在转（tools.vision）。

    **不搭车写进 vision_obs.yaml**：那份是观测账本，每轮无条件全量重写它（内含最近
    VISION_MAX_ENTRIES 条大模型原文）既浪费，又会让它的 mtime 从「最后一次真观测的时刻」
    变成「最后一次心跳」，把原有语义弄丢。心跳单独一个文件，只携带「我还活着」这一个位。

    内容只写「什么时候、哪个 pid」，判活只看 mtime。异常吞掉只记一行：
    心跳是附加信号，写不成不该把观测循环带走（写不成时 status_observer 那边会报
    unavailable，那也正是实情）。
    """
    try:
        VISION_OBSERVER_HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = VISION_OBSERVER_HEARTBEAT_FILE.with_suffix('.heartbeat.tmp')
        tmp.write_text(
            f'{time.strftime("%Y-%m-%d %H:%M:%S")} pid={os.getpid()}\n',
            encoding='utf-8',
        )
        tmp.replace(VISION_OBSERVER_HEARTBEAT_FILE)
    except Exception as exc:  # noqa: BLE001 —— 心跳是附加信号，绝不能连累观测循环
        print(f'[VisionObserver] 心跳写失败（不影响观测）：{exc}', flush=True)


def run(
    interval: Optional[int] = None,
    max_entries: int = VISION_MAX_ENTRIES,
) -> None:
    """消费 start_agent 拉起的取帧服务，周期观测，持续运行直到被中断。

    每轮送大模型的是 get_stacked_frame() 得到的 N 帧拼接图（帧环最新 N 帧，自上而下
    时间由远到近）。**N 和观测周期都是运行时可调值**：每轮现读 Orchestrator.vision_tunables
    （覆盖层 runtime_files/vision_tunables.yaml，不重启即生效）。抓拍节奏不在此列 ——
    帧生产者一直以固定的高频率抓帧攒环，本函数只决定「隔多久去看一眼帧环」，
    「看的时候取几帧」由 stack_frames 决定。

    interval 显式传值（or CLI --interval）＝钉死该周期，覆盖层不再参与（自检/调试用）；
    不传则每轮取覆盖层的 observe_interval，出厂默认 configs.DEFAULT_INTERVAL
    （= 档位表里的一档，当前取值见 Orchestrator.vision_tunables 的 CLI 回显）。

    ⚠️ 改 N 之前先想一下 configs.prompts.VISION_PROMPT：它把「几帧 / 过去几秒」写进了
    给模型的话里。N 与它不一致时，模型会按 prompt 说的帧数去理解那张图，而它的回复会被
    写进 vision_obs.yaml、被 task_loop 当事实读。

    本函数是纯消费者：不 new 生产者，退出时也不 stop()（本进程若自拉过生产者就是它的
    所有者 —— 一律不主动 stop，免得误停）。帧源生命周期归 start_agent（服务进程由它收）。
    _frame_source.start() 按三条规则给句柄：本进程在抓拍 → 本地实例；取帧服务可达 →
    它的客户端（正常部署走这条）；都不行 → 自拉兜底，抢不到 FRAME_PRODUCER_LOCK_FILE
    时才返回 None（帧在别的进程的内存里，本进程拿不到），本进程退出。
    **只拿它做这一次就绪判定**：之后的读取一律走模块级函数（见模块头「观测器只是消费者」）。
    """
    if _frame_source.start() is None:
        state = _frame_source.describe_source()
        print(
            f"[VisionObserver] 取不到帧源：取帧服务不可达（{state.detail}），"
            "也抢不到帧生产者锁（别的进程在抓拍）—— 帧环在对方内存里，本进程退出",
            flush=True,
        )
        return

    # 就绪：探询到首帧为止（后台线程按周期自重试抓拍）
    attempt = 0
    while not _frame_source.wait_for_first_frame(timeout=_RETRY_POLL_INTERVAL):
        attempt += 1
        if attempt == 1 or attempt % 10 == 0:
            print(
                "[VisionObserver] 等待首帧…（相机可能被占用，后台抓拍持续重试中）",
                flush=True,
            )

    observations = _load_observations(max_entries=max_entries)
    tunables = vision_tunables.current()
    # interval 显式传了就是钉死它；没传则每轮取覆盖层的 observe_interval（启动时先亮一次）
    pinned_interval = None if interval is None else float(interval)
    print(
        f"[VisionObserver] 启动，周期 "
        f"{pinned_interval if pinned_interval is not None else tunables.observe_interval:g}s"
        f"{'（命令行钉死，覆盖层不参与）' if pinned_interval is not None else f'（来源 {tunables.source}）'}，"
        f"帧源为取帧服务（独立 CLI 时自拉生产者），每轮取 {tunables.stack_frames} 帧拼接图识别"
        f"（可用 python3 -m Orchestrator.vision_tunables 改，改完下一轮生效），"
        f"记录文件 {RUNTIME_VISION_OBSERVATION}，最多保留 {max_entries} 条",
        flush=True,
    )
    for line in vision_tunables.warnings(tunables):
        print(f"[VisionObserver] ⚠️ 视觉观测参数覆盖层有问题：{line}", flush=True)

    # 只观测「相比上一次已分析帧更新」的帧，避免抓拍停摆时重复识别同一旧帧
    last_id = _frame_source.latest_frame_id()
    last_count = tunables.stack_frames
    last_interval = tunables.observe_interval
    last_warn = vision_tunables.warnings(tunables)   # 覆盖层告警去重（内容变了才再报）
    while True:
        loop_start = time.monotonic()

        # 每轮现读一次运行时可调值（一次 stat，见 Orchestrator/vision_tunables.current()）。
        # 放在循环里就是「改覆盖层 → 下一轮就按新值拼/按新周期走」的落点。
        tunables = vision_tunables.current()
        if tunables.stack_frames != last_count:
            print(
                f"[VisionObserver] 拼接帧数按运行时覆盖层改为 {tunables.stack_frames}"
                f"（原 {last_count}，来源 {tunables.source}）；"
                "⚠️ VISION_PROMPT 里若写了具体帧数，它不会跟着变——模型会按 prompt 说的理解这张图",
                flush=True,
            )
            last_count = tunables.stack_frames
        # 观测周期同理（显式 --interval 时钉死，覆盖层不参与）
        interval = (
            pinned_interval
            if pinned_interval is not None
            else float(tunables.observe_interval)
        )
        if pinned_interval is None and tunables.observe_interval != last_interval:
            print(
                f"[VisionObserver] 观测周期按运行时覆盖层改为 {tunables.observe_interval:g}s"
                f"（原 {last_interval:g}s，来源 {tunables.source}）",
                flush=True,
            )
            last_interval = tunables.observe_interval
        # 覆盖层本身有问题（手改越界、字段名拼错）要报出来 —— 否则它会静默生效，
        # 只有碰巧跑过 CLI 的人才知道。内容变了才再报，别刷屏。
        warn = vision_tunables.warnings(tunables)
        if warn and warn != last_warn:
            last_warn = warn
            for line in warn:
                print(f"[VisionObserver] ⚠️ 视觉观测参数覆盖层有问题：{line}", flush=True)

        new_id = _frame_source.wait_for_new_frame(after_id=last_id, timeout=interval)
        unreachable = False
        if new_id is None:
            # 门面把「服务断了」和「没有新帧」抹成同一个 None，而这两件事对下游含义相反：
            # 前者是取帧这条路断了（判活信号必须随之变脏），后者只是相机没在推新帧。
            # 只在空帧这一轮查一次实情（本地-自有生产者的部署 backend 是 local-owner，不会误判）。
            state = _frame_source.describe_source()
            if state.latest_frame_id is not None and state.latest_frame_id < last_id:
                # 取帧服务重起过：帧号从 1 重新数。比着旧号等只会等到它追平 —— 等待时长
                # ≈服务原先已经跑过的时长（0.2s 抓拍节奏下，跑过一小时就再瞎一小时）。
                print(
                    "[VisionObserver] 取帧服务重起过（帧号归零），改按新帧流继续",
                    flush=True,
                )
                last_id = 0
            elif state.backend == "service-unreachable":
                unreachable = True
                print(
                    "[VisionObserver] 取帧服务不可达，本轮没取到帧（判活信号随之停更）",
                    flush=True,
                )
            else:
                print(
                    "[VisionObserver] 本周期内无新帧（帧流可能暂停），本轮跳过",
                    flush=True,
                )
        else:
            # 最新 N 帧拼接；帧环不足 N 帧时自动退化为可用帧数（N 超过环深时由
            # frame_producer 夹到环深并告警）。
            # save=True 是本观测器的显式选择：观测是**周期性无人值守**的，出问题时
            # 只能靠留档回看「大模型当时到底看到了什么」；视觉工具那两次（vision /
            # search_old）默认不留图。目录与命名由 frame_producer 管，旧图的清理由监督器
            # 每轮按体积做（configs.RUNTIME_PHOTOS_TRIGGER_MB / _TARGET_MB，
            # 见 Observer/cleanup.py），落盘本身不再自己清。
            stacked = _frame_source.get_stacked_frame(
                "jpeg", count=tunables.stack_frames, save=True, suffix="observe"
            )
            if isinstance(stacked, bytes):
                observations = _record_and_report(
                    observations, stacked, max_entries
                )

        last_id = new_id if new_id is not None else last_id

        # 心跳：本轮**无论有没有新帧都写**（没有新帧才是它最该证明自己还活着的时候）。
        # 放在这里 = 一轮完整跑完的收尾，故正常两笔心跳的间隔最长是
        # 「等帧 interval + 一次 VQA 超时 VISION_TIMEOUT」，判活阈值按 3 倍留余量。
        # interval 现在是**运行时可调**的，判活阈值必须跟着走 —— 所以它不在这里写死，
        # 由 status_observer 现读 Orchestrator.vision_tunables.alive_max_age_sec()（见 configs.py）。
        # 就绪之前（上面等首帧那一段）**不写**：那时相机被占或坏掉，视觉一条观测也产不出来，
        # 这时候报「还活着」只会误导读方。同一条理由，取帧服务断了那一轮也不写 —— 那时本
        # 轮什么也没看到，写心跳会让 status_observer 把瞎着的观测器报成 available。
        if not unreachable:
            _write_heartbeat()

        # 精确控制周期：扣除本次执行耗时
        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, interval - elapsed))


# =============================================================================
# CLI 入口
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="视觉观测器（多帧拼接图 + 大模型识别）")
    # 出厂默认报成「档位 + 秒数」：默认值本身出自档位表，报档位名才不会与那张表分叉。
    default_level = vision_tunables.observe_rate_level(DEFAULT_INTERVAL)
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"观测周期（秒）。默认不传：每轮取运行时覆盖层的 observe_interval"
             f"（出厂默认 = 档位 {default_level} 的 {DEFAULT_INTERVAL:g}s，"
             "可 python3 -m Orchestrator.vision_tunables 改）；传了就钉死、覆盖层不再参与",
    )
    parser.add_argument(
        "--max-entries",
        type=int,
        default=VISION_MAX_ENTRIES,
        help=f"最多保留最近 N 条观测记录，默认 {VISION_MAX_ENTRIES}",
    )
    args = parser.parse_args()

    try:
        run(
            interval=args.interval,
            max_entries=args.max_entries,
        )
    except KeyboardInterrupt:
        print("\n[VisionObserver] 已停止", flush=True)


if __name__ == "__main__":
    main()
