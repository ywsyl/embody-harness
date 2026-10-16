#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
视觉任务调度脚本

mode:
    front   : 前方视角，取共享帧环最新一帧做视觉分析
    surround: 环绕视角，原地转一圈、每 90° 取一帧，拼四宫格后搜索分析
    image   : 单张图片模式，使用指定图片进行视觉分析

取帧（front / surround）：一律来自 skills/camera/frame_producer.py 的共享帧环
（get_latest_frame），不再调用 capture.py 拍图。
front 另支持 resize=(宽, 高)：默认不缩放（抓拍原分辨率 1920×1080），斗牛按约定传
(1280, 720)（见 configs.configs.BULL_CHASE_VISION_RESIZE）；缩放在本层做，帧环原帧与
别的调用方都不受影响。

图像全程在内存里流转（默认不落盘）
----------------------------------
帧环取出的 JPEG 字节 → base64 → 进程内直调视觉服务（vision_local.request_vqa 的
image_base64），不再「写临时文件 → 交给子进程 → 子进程读回来编码」——视觉链路默认
**一个图像文件都不产生**（环绕搜索的四宫格同理，在内存里拼好直接送模型）。

需要留档时由调用方传 save=True（工具层默认 False），落盘由
skills/camera/frame_producer.py 独占完成：目录、命名、滚动清理都在它那儿，本模块
只透传 save，自己不拼路径、不建目录。要单独存一张图也调它，别在本模块加第二条写路径。

⚠️ 进程边界：帧环在**取帧服务进程**（skills/camera/frame_producer.py --serve，由
start_agent.py 作为组件子进程拉起）的内存里。本模块跑在哪个进程都不影响取帧：它调的都是
frame_producer 的模块级函数，由后者按三条规则分派 —— 本进程在抓拍 → 本地帧环；取帧服务
可达 → unix socket 取帧（configs.paths.FRAME_PRODUCER_SOCKET）；都不行 → 自拉生产者兜底
（独立 CLI 那条路：抢到 FRAME_PRODUCER_LOCK_FILE 才成，抢不到则**报错，不静默返空**）。

角色侧的工具层走的就是「起子进程跑本模块」这条路（plugin_tools/tool_executor.py 的 _look），
子进程自己没有帧环，所以走的是上述第 2 条（取帧服务）—— 服务的正常存在使这条链路可用。
取不到帧时的文案由 _no_frame_reason() 按实际来源给（人在抓拍但没帧 / 服务不可达），
不再一律说「请经工具调用而非独立 CLI 运行」。
"""
import argparse
import base64
import subprocess
import sys
from pathlib import Path

# 仓库根（本文件 skills/vision/scripts/ 上三级）入 sys.path，以便 import configs / skills
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.paths import MOTION_SCRIPT  # noqa: E402
from configs.prompts import SURROUND_SEARCH_PROMPT  # noqa: E402
from skills.camera import frame_producer  # noqa: E402
from skills.vision.scripts import vision_local  # noqa: E402

# 取帧相关
_FIRST_FRAME_TIMEOUT = 5.0      # CLI 自拉生产者后等首帧的超时（秒）
_NEW_FRAME_TIMEOUT = 2.0        # 环绕搜索里等「转向之后」的新帧超时（秒）
_TURN_ARGS = ("turnright", "1.2", "1.6")   # 原地右转 90°（沿用原环绕搜索流程参数）
# [2026-09-15 注释] motion.sh 改为阻塞返回后，转向结束即返回，无需额外停稳等待。
# _TURN_SETTLE_SECONDS = 0.2      # 转完等机身停稳再取帧（秒）


def run_cmd(cmd):
    """
    执行 shell 命令，返回它的 stdout。

    `[CMD]` 标记、命令回显与它的输出一律打 **stderr**：stdout 是调用方（角色侧工具）原样
    取走给模型的结果，只该有视觉结论（request_vqa 打的 JSON），不该有转向的执行流水——那是
    每一轮都一样的命令回显，模型看了没有信息量。留在 stderr 上是为了人/CLI 调试时仍能看到
    跑到哪一步；子进程失败时那段流水也会随报错一起冒出来（tool_executor 用的是 result.stderr）。
    """
    print("[CMD]", file=sys.stderr)
    print(" ".join(cmd), file=sys.stderr)

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    print(result.stdout, file=sys.stderr)

    if result.returncode != 0:
        raise RuntimeError(
            f"命令执行失败: {' '.join(cmd)}"
        )

    return result.stdout


def _to_base64(jpeg: bytes) -> str:
    """JPEG 字节 → base64 字符串（视觉服务的 image_base64 字段）。

    传 bytes 直接编码，不再经文件读回——图只在内存里存在。
    """
    return base64.b64encode(jpeg).decode("ascii")


def _no_frame_reason() -> str:
    """取不到帧时的报错文案，按 frame_producer.describe_source() 的实际来源分两种。

    这两种对人有不同的下一步（相机通路 vs 取帧服务没起），所以分开说；「服务在跑但锁被
    别人占了」这种细分不做（见 frame_producer.describe_source）。
    """
    state = frame_producer.describe_source()
    if state.backend == "service-unreachable":
        return (
            f"取帧失败: 取不到帧 —— 帧环在取帧服务进程的内存里，本进程读不到，而取帧服务"
            f"不可达（{state.detail}）。正常部署由 start_agent.py 拉起取帧服务"
            "（frame_producer.py --serve）；本进程若独立运行，则要能抢到 "
            "FRAME_PRODUCER_LOCK_FILE 才能自拉生产者。"
        )
    return (
        f"取帧失败: 有人在抓拍但帧环里还没有帧（{state.detail}）—— 相机共享内存没人推流"
        "（video_gst 节点没起、或 socket 路径不对）？见 configs.paths.CAMERA_SHM_SOCKET。"
    )


def grab_frame(after_id=None, resize=None, save=False, suffix=None) -> bytes:
    """从共享帧环取一帧，返回 JPEG 字节（不调用任何 capture 脚本）。

    after_id 非空时先等待帧号 > after_id 的新帧再取，用于「转向之后」的场景——
    否则可能取到转向前的旧帧（帧环默认保留最近几帧）。等不到新帧不报错，退回环内
    现有最新帧。

    resize=(宽, 高) 时返回缩放后的字节（默认 None＝原分辨率 1920×1080）；缩放在读取侧
    做，帧环里的原帧不受影响，别的调用方仍拿原尺寸。
    save=True 时由 frame_producer 把**返回的这份**落盘留档（默认不落盘，见模块头）。
    """
    if after_id is not None:
        frame_producer.wait_for_new_frame(after_id, timeout=_NEW_FRAME_TIMEOUT)

    jpeg = frame_producer.get_latest_frame(
        "jpeg", resize=resize, save=save, suffix=suffix
    )
    if jpeg is None:
        # 本进程还没有帧：拿一次取帧句柄再试。服务在跑 → start() 给的是它的客户端；
        # 服务不在（独立 CLI）→ 自拉生产者兜底，抢不到 FRAME_PRODUCER_LOCK_FILE 时
        # start() 返回 None（帧在别的进程的内存里，本进程拿不到，只能报错）。
        producer = frame_producer.start()
        if producer is not None and producer.wait_for_first_frame(timeout=_FIRST_FRAME_TIMEOUT):
            jpeg = frame_producer.get_latest_frame(
                "jpeg", resize=resize, save=save, suffix=suffix
            )

    if not jpeg:
        raise RuntimeError(_no_frame_reason())

    assert isinstance(jpeg, bytes)
    return jpeg


def grab_front_image(resize=None, save=False) -> bytes:
    """
    取前方图片（共享帧环最新一帧），返回 JPEG 字节。

    resize=(宽, 高) 见 grab_frame；默认 None 保持抓拍原分辨率。
    """
    return grab_frame(resize=resize, save=save, suffix="look")


def run_front(prompt, resize=None, save=False) -> str:
    """
    前方视角: 取帧 -> 视觉分析，返回识别结果文本。

    resize 透传给取帧：斗牛（bull_chase）按其约定传 (1280, 720)，其余调用方不传即
    维持原来的原分辨率行为。
    save=True 时留档这一帧（默认不留）。
    """

    jpeg = grab_front_image(resize=resize, save=save)

    return vision_local.request_vqa(
        image_base64=_to_base64(jpeg),
        prompt=prompt,
    )


def run_surround(prompt, save=False) -> str:
    """
    环绕视角: 原地转一圈、每 90° 取一帧（共享帧环）→ 内存拼四宫格 → 视觉搜索。
    返回服务端应答文本（调用方按它的 JSON 解析目标区域与水平位置）。

    拼图不落盘：图只在内存里拼、直接 base64 送模型（save=True 才留一张档）。
    拼图的列数/质量/最大宽度用 frame_producer.compose_grid 的默认值
    （2×2、q85、超 2560 宽等比缩——VLM 服务端最大支持 2560 宽）。
    """

    # 取帧句柄：取帧服务在跑 → 它的客户端（帧号经 socket 查）；服务不在（独立 CLI）→
    # 自拉的生产者实例；抢不到锁 → None（此时取帧必然失败，由 grab_frame 的报错说明）。
    producer = frame_producer.start()

    views = ("front", "right", "back", "left")
    images = []

    for _view in views:
        # 第一帧（正前方）直接取；其余帧在转完 90° 之后取，确保是新视角
        after_id = producer.latest_frame_id if (producer is not None and images) else None
        images.append(grab_frame(after_id=after_id))

        # [2026-09-15 注释] motion.sh 已改为「收到停止信号才返回」，返回即表示转向结束，
        # 这里再等机身停稳就是纯重复等待。
        # time.sleep(_TURN_SETTLE_SECONDS)
        run_cmd(["bash", str(MOTION_SCRIPT), *_TURN_ARGS])

    grid = frame_producer.compose_grid(
        images, cols=2, save=save, suffix="search_grid"
    )
    if not grid:
        raise RuntimeError("环绕搜索失败: 四视角拼图未生成（见上方取帧告警）")

    assert isinstance(grid, bytes)
    return vision_local.request_vqa(
        image_base64=_to_base64(grid),
        prompt=SURROUND_SEARCH_PROMPT.replace("{target}", prompt),
    )


def run_image(image, prompt):
    """
    单图模式：分析调用方给的图片路径（只读，不落盘）。
    """

    if not image:
        raise ValueError(
            "单图模式必须提供 --image"
        )

    return vision_local.request_vqa(
        image_path=image,
        prompt=prompt,
    )


def main():
    parser = argparse.ArgumentParser(
        description="机器人视觉任务调度"
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=[
            "front",
            "surround",
            "image"
        ],
        help="front=前方视角 surround=环绕视角 image=单图"
    )

    parser.add_argument(
        "--prompt",
        required=True,
        help="用户问题"
    )

    parser.add_argument(
        "--image",
        default=None,
        help="单图模式图片路径"
    )

    parser.add_argument(
        "--resize",
        nargs=2,
        type=int,
        metavar=("WIDTH", "HEIGHT"),
        default=None,
        help="front 模式取帧缩放到该尺寸（默认不缩放，保持抓拍原分辨率 1920x1080）"
    )

    parser.add_argument(
        "--save",
        action="store_true",
        help="把本轮用到的图落盘留档（默认不落盘；目录/命名/清理由 frame_producer 管）"
    )

    args = parser.parse_args()

    if args.mode == "front":
        run_front(
            args.prompt,
            resize=args.resize,
            save=args.save,
        )

    elif args.mode == "surround":
        run_surround(
            args.prompt,
            save=args.save,
        )

    elif args.mode == "image":
        run_image(
            args.image,
            args.prompt
        )

    else:
        raise RuntimeError(
            "未知mode"
        )


if __name__ == "__main__":
    main()
