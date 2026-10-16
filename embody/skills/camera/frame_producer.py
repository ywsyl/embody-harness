#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
相机共享内存驱动的「图像帧生产者」—— 单一生产者 + 多消费者共存

为什么存在
----------
上一代是一个「纯读取端」（单条 GStreamer 管道读共享内存 /tmp/neck_jpeg 的 JPEG 帧流）。
它被本模块取代的原因是「单条管道 + 只想读最新帧」不够用（多消费者、追帧、拼接），
**不是**因为取帧方式不对 —— 本模块取的仍是同一条路：读共享内存。那份读取端代码
（skills/camera/latest_frame_stream.py）已随本模块上位一并删掉。

本模块用 **skills/camera/scripts/capture.py**（configs.paths.CAMERA_SHM_CAPTURE_SCRIPT）
作帧源：后台唯一线程按固定间隔抓拍一张 JPEG（每次 = 一条
`gst-launch shmsrc socket-path=<CAMERA_SHM_SOCKET> ! image/jpeg ! multifilesink`），
压入进程内「滚动帧环」，供 N 个消费者并发读取。**不碰 /dev/video*、不 fuser -k**，
只当共享内存的读者 —— 与本体原有视频流（video_gst 节点自身、以及别的消费者）并存。

为什么不直采设备
----------------
本模块此前走 skills/camera/scripts/capture.sh → capture_direct.py 直采设备。那条路
要求每次拍照先 `fuser -k` 杀掉独占相机的 video_gst 节点，而该节点未配 respawn，
杀掉不会自己回来 —— 于是**周期性抓拍等于把本体自己的视频流反复打死**
（capture_direct 里 [2026-09-02 停用] 那批「拍完恢复节点」逻辑，正是为收拾这个后果
而写、又被性能逼停的）。改用共享内存读取后，这条副作用整个消失。

「共享内存取不到帧」是**过时误判**：camera/scripts/capture_direct.py 文件头写
「video_gst 的共享内存 socket（/tmp/foo_fhd 等）是私有加密协议，无法直接解出 JPEG」
—— 2026-09-22 实测不成立。video_gst_neck_node 二进制里就是标准 GStreamer `shmsink`
（`strings` 可见 `shmsink_jpeg` / `socket-path` / `/tmp/neck_jpeg`，caps
`image/jpeg, width=1920, height=1080`），capture.py 的 shmsrc 管道对同款 shmsink
实测 53ms 拿到完整 1920×1080 JPEG。当初「拉不出帧」的真因更可能是那一刻根本没人在
推流（本机 2026-09-22 就一个 video_gst 节点都没跑），而不是协议解不开。

核心：1 个取帧管理者 + N 个消费者（全系统唯一，跨进程）
- 取帧权归**一个进程**：`frame_producer.py --serve`（取帧服务，由 start_agent.py 作
  组件子进程拉起）。它 `start()` 时先以 flock 独占 FRAME_PRODUCER_LOCK_FILE
  （configs.paths）——另一进程抢占失败即拒绝启动。进程内再靠模块级单例
  `_DEFAULT_PRODUCER` + start() 幂等保证不重复；任何消费者不要各自 new/start 生产者。
- 帧环是**服务进程的内存**，别的进程读不到 —— 所以消费者一律走本模块的模块级函数，
  由它们按三条规则分派（本进程在抓拍 → 取帧服务 RPC → 本地空环，见「模块级入口」一节）：
  **调用点不必知道帧在哪个进程里**，也不需要自己起生产者。跨进程那一段是 unix socket
  （configs.paths.FRAME_PRODUCER_SOCKET），服务端只把请求转给同名实例方法。
- 滚动帧环：保留最近 FRAME_PRODUCER_MAX_FRAMES 帧 (frame_id, wall_time, jpeg_bytes)，短暂滞后的消费者
  仍能读到 1–2 帧前的画面；落后超过环深自然快进到最新帧（丢帧但不阻塞）。
- 各消费者并发读同一共享缓存、各记各的进度；get_latest_frame / get_frames /
  get_frames_since / wait_for_new_frame / iter_latest_frames / get_stacked_frame
  均线程安全。跨进程时服务端 thread-per-connection，靠「读接口只拷引用、PIL 在锁外」
  保证并发不互相卡住（见 _FrameServiceServer 的说明）。
- 分辨率：抓拍原帧 1920×1080（定在 capture.py 的 GStreamer caps，
  本模块不传 --resize）。读接口一律带 resize=(宽, 高) 参数：
  默认 None＝原样透传、不重编码、不需要 Pillow；只有显式传参的消费者（如斗牛缩到
  1280×720）才在**读取侧**缩放——帧环里始终是原帧，各消费者各要各的尺寸互不影响，
  也不会为了某个消费者去改相机设置。

⚠️ 共享内存没人推流时抓拍会持续失败（video_gst 节点没起、或 socket 路径不对），
表现为帧环空、取帧一律 None，外加节流的失败告警；**但不会碰相机设备、也不会影响
本体视频流**，属「取不到」而非「搞坏」。socket 路径见 configs.paths.CAMERA_SHM_SOCKET。
模块被 import 不会启动抓拍（单例是惰性的）。

图像落盘：全仓唯一写手 + 唯一 save 开关
---------------------------------------
本模块是**全仓唯一**把图像写成文件的地方（`save_photo` / `photo_path`），落盘目录
只有 `RUNTIME_PHOTOS_DIR`（configs.paths，= runtime_files/photos/），命名
`<YYYYmmdd_HHMMSS_ffffff>[_<suffix>].jpg`。其它模块（视觉技能、观测器、工具层）
**不再自己拼路径**，只传 `save: bool = False`：

- `save=False`（默认）——**完全不落盘**。要交给大模型的图走内存 base64 直传
  （vision_local.request_vqa 的 image_base64），连临时文件都不产生。
- `save=True`，或 `save_photo()` 直接调用 —— 写受管目录；写失败只告警、返回 None，
  不影响取帧本身。

**落盘不自己清旧图**：写手只管写，"目录长胖了要删"是尺寸问题、不是落盘问题，收在
Observer/cleanup.py 一处（两档水位见 configs.RUNTIME_PHOTOS_TRIGGER_MB / _TARGET_MB），
由 dsh-plugins/supervisor-loop 每轮执行。本模块只提供「哪些文件名算我们落的」这条判据
（`is_managed_photo`，与 `photo_path` 的命名同源），别人放进目录的东西一律不碰。

读取侧不缩放时返回的是帧环里的原字节，落盘的是**要返回的那一份**（含 resize 之后
的），所以 `save=True` 存下来的就是消费者真正看到的那张。

抓拍临时帧：单帧仍是「写进 self.tmp_dir → 读完即 unlink」，但 tmp_dir 不再是
`tempfile.mkdtemp("/tmp/frame_producer_*")`，而是受管目录下的
`runtime_files/photos/scratch/pid_<pid>_<ns>/`——`stop()` 删自己的、`start()` 顺手
清理 pid 已不存在的残留（跨进程被 kill -9 也不会攒垃圾）。目录是**惰性建的**（真开始
抓拍才建，见 _ensure_tmp_dir）：消费者进程构造一个空帧环兜底时不留下任何目录。

依赖：bash（需 source ~/.bashrc）、skills/camera/scripts/capture.py
（依赖 gst-launch-1.0 及 shmsrc/multifilesink 插件）。机器人需已开机、
**video_gst 相机节点正在推流**（本模块只读它写的共享内存，自己不开相机）。
"""

from __future__ import annotations

import argparse
import atexit
import base64
import fcntl
import io
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import (
    Any,
    Deque,
    Dict,
    Generator,
    List,
    Literal,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

# 确保仓库根在 sys.path（本文件 skills/camera/ 上两级 = 仓库根），以便 import configs
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.paths import (  # noqa: E402
    CAMERA_SHM_CAPTURE_SCRIPT,
    CAMERA_SHM_SOCKET,
    FRAME_PRODUCER_LOCK_FILE,
    FRAME_PRODUCER_SOCKET,
    LABEL_FONT_PATH,
    RUNTIME_PHOTOS_DIR,
)
from configs.configs import (  # noqa: E402
    FRAME_PRODUCER_ATTACH_TIMEOUT,
    FRAME_PRODUCER_CAPTURE_TIMEOUT,
    FRAME_PRODUCER_INTERVAL,
    FRAME_PRODUCER_MAX_BLOB_BYTES,
    FRAME_PRODUCER_MAX_FRAMES,
    FRAME_PRODUCER_RPC_TIMEOUT,
    VISION_STACK_FRAMES,
)
# 视觉观测参数的**运行时可调**覆盖层（只含「每轮拼接帧数」这一项）。
# 直接从 Orchestrator/ 取，而不是 tools/：本模块是相机库，还被视觉技能当**子进程脚本**用
# （每次 vision 工具调用都起一个），不该把 tools/__init__ → tool_executor → Motion_Manager
# 那一串背进来。那个模块是纯叶子（只依赖 configs/、导入无副作用），落到 Orchestrator/ 里
# 与角色进程无关 —— 它服务的是观测器与相机，不是 cordis 那条链。
from Orchestrator import vision_tunables  # noqa: E402

FrameFormat = Literal["jpeg", "base64"]
FrameData = Union[bytes, str]

_LOG = logging.getLogger("frame_producer")


def _coalesce_jpeg(data: bytes) -> Optional[bytes]:
    """取 data 中最后一帧**完整** JPEG（最后一个 SOI..EOI 对），避免截断/拼接脏数据。

    EOI(0xFFD9) 必须一起校验：抓拍脚本只要**文件一有字节**就返回（capture.py 判据是
    size > 0），因此读到的有可能是**正在写入中的半截帧**——虽然 capture.py 自 2026-09-23
    起会等管道 EOS 落定再返回，但那是脚本侧的改进，本模块作为调用方不依赖它：抓拍脚本
    可替换（--capture-script），契约只有「成功时该路径上有个 JPEG」。
    只看到 SOI 就当帧用，读的时候 Pillow 会报 `OSError: image file is truncated
    (N bytes not processed)`，拼接时被当坏帧跳过。
    尾帧不完整就退而取它前面那一帧完整帧；一帧完整都没有（单帧被截断，这是最常见的情形
    ——capture.py 一个文件只落一帧）才返回 None，
    调用方丢弃本轮、按 interval 重抓。

    注：熵编码段里 0xFF 必被填充成 0xFF00，故 SOI/EOI 不会出现在帧内部
    （带 EXIF 缩略图的帧除外——那种情况外层 SOI 仍在最前、外层 EOI 仍在最后，仍取对）。
    """
    end = data.rfind(b"\xff\xd9")
    if end < 0:
        return None
    start = data.rfind(b"\xff\xd8", 0, end)
    if start < 0:
        return None
    return data[start:end + 2]


# 缩放输出的 JPEG 质量：与 camera/scripts 的 capture.py / capture_direct.py 的
# resize_image（quality=95）保持一致，避免同一条链路上两种重编码标准。
_RESIZE_JPEG_QUALITY = 95


def _normalize_resize(size: Optional[Sequence[int]]) -> Optional[Tuple[int, int]]:
    """把调用方给的 resize 归一成 (宽, 高) 正整数对；None 原样返回（= 不缩放）。"""
    if size is None:
        return None
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"resize 必须是 (宽, 高) 两个整数: {size!r}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"resize 必须为正整数: {size!r}")
    return (width, height)


def _resize_jpeg(jpeg: bytes, size: Tuple[int, int]) -> Optional[bytes]:
    """把一帧 JPEG 缩放到 size=(宽, 高)，返回重编码后的 JPEG；解析失败返回 None。

    仅在调用方显式传 resize 时触发——不传时帧原样透传，既不重编码也不引入 Pillow 依赖。
    依赖 Pillow（延迟 import，与 _stack_jpegs 同一路数）。
    """
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(jpeg))
        img.load()
    except Exception as exc:  # noqa: BLE001 —— 坏图不该抛给读帧的消费者
        _LOG.warning("缩放跳过无法解析的帧: %r", exc)
        return None

    buf = io.BytesIO()
    img.convert("RGB").resize(size, Image.Resampling.LANCZOS).save(
        buf, format="JPEG", quality=_RESIZE_JPEG_QUALITY
    )
    return buf.getvalue()


# 标签字体：优先 DejaVuSans-Bold（与 camera/scripts/capture.py 一致），
# 取不到时回退 Pillow 内置位图字体。路径唯一来源是 configs/paths.py 的
# LABEL_FONT_PATH —— 本文件与 camera/scripts/capture_direct.py 的水印共用，
# 原先两边各写一份字面量，改一处另一边会静默不生效。
_LABEL_FONT_PATH = str(LABEL_FONT_PATH)
_LABEL_MARGIN = 8       # 标签距子图左上角的最小边距（像素，随字号放大）
_LABEL_PAD = 6          # 文字黑底向外扩的最小边距（像素，随字号放大）


def _draw_label(draw: Any, text: str, tile_top: int, tile_height: int) -> None:
    """在子图左上角画黑底白字标签（字号取子图高度的 1/10，远看也能辨认）。"""
    from PIL import ImageFont

    size = max(20, min(96, tile_height // 10))
    margin = max(_LABEL_MARGIN, size // 2)
    pad = max(_LABEL_PAD, size // 4)
    try:
        font = ImageFont.truetype(_LABEL_FONT_PATH, size)
    except Exception:
        font = ImageFont.load_default()

    x = margin
    y = tile_top + margin
    bbox = draw.textbbox((x, y), text, font=font)
    draw.rectangle(
        [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad],
        fill=(0, 0, 0),
    )
    draw.text((x, y), text, fill=(255, 255, 255), font=font)


def _stack_jpegs(
    tiles: Sequence[Tuple[int, int, bytes]],
    *,
    quality: int,
    fmt: FrameFormat,
    resize: Optional[Tuple[int, int]] = None,
) -> Optional[FrameData]:
    """把 [(位置序号, 帧号, jpeg 字节)] 自上而下拼成一张图，左上角标注序号。

    - 标注格式 `frame 位置`：位置为拼接后的自上而下序号（1=最旧，数字越大越新）。
    - 尺寸不一致时不缩放：画布宽取各帧最大宽，各帧左对齐排布、右侧/底部空余补黑，
      保留原始像素便于前后帧对比。
    - resize 非空时**逐帧**缩到 (宽, 高) 再拼：拼图尺寸随之变成 宽 ×(帧数×高)，
      而不是把整张拼图压扁，子图之间的比例关系保持不变。
    - 单帧解析失败只记日志并跳过；全部失败返回 None（缩放失败同样按坏帧跳过）。
    - quality 为输出 JPEG 质量；fmt 决定返回 bytes（jpeg）还是 base64 str。

    依赖 Pillow（仅本功能需要，故在函数内延迟 import）。
    """
    from PIL import Image, ImageDraw

    images: List[Tuple[int, Any]] = []
    for pos, frame_id, jpeg in tiles:
        try:
            img = Image.open(io.BytesIO(jpeg))
            img.load()
            img = img.convert("RGB")
            if resize is not None:
                img = img.resize(resize, Image.Resampling.LANCZOS)
            images.append((pos, img))
        except Exception as exc:  # noqa: BLE001 —— 单帧坏图不该拖垮整张拼接
            _LOG.warning("拼接跳过无法解析的帧: pos=%s id=%s, %r", pos, frame_id, exc)
    if not images:
        return None

    canvas = Image.new(
        "RGB",
        (max(img.width for _, img in images), sum(img.height for _, img in images)),
        (0, 0, 0),
    )
    draw = ImageDraw.Draw(canvas)

    top = 0
    for pos, img in images:
        canvas.paste(img, (0, top))
        _draw_label(draw, f"frame {pos}", top, img.height)
        top += img.height

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=int(quality))
    return _convert(buf.getvalue(), fmt)


def _grid_jpegs(
    jpegs: Sequence[bytes],
    *,
    cols: int,
    quality: int,
    fmt: FrameFormat,
    max_width: Optional[int] = None,
) -> Optional[FrameData]:
    """把多张 jpeg 按 cols 列排成网格图（第 1 张在左上、逐行向右）。

    - 子图尺寸不一致时不缩放：格子按各图最大宽/最大高，图贴格子左上角，空余补黑。
    - max_width 非空且成图宽超过它时，整张按比例缩到该宽（沿用旧 local-search 的
      「宽 > 2560 则等比缩到 2560」——VLM 服务端最大支持 2560 宽）。
    - 不画标签（与旧 ImageMagick montage 的输出观感一致）。
    - 单帧解析失败只记日志并跳过；全部失败返回 None。
    """
    from PIL import Image

    images: List[Any] = []
    for jpeg in jpegs:
        try:
            img = Image.open(io.BytesIO(jpeg))
            img.load()
            images.append(img.convert("RGB"))
        except Exception as exc:  # noqa: BLE001 —— 单帧坏图不该拖垮整张网格图
            _LOG.warning("网格拼图跳过无法解析的帧: %r", exc)
    if not images:
        return None

    cols = max(1, int(cols))
    rows = (len(images) + cols - 1) // cols
    tile_w = max(img.width for img in images)
    tile_h = max(img.height for img in images)
    canvas = Image.new("RGB", (tile_w * cols, tile_h * rows), (0, 0, 0))
    for index, img in enumerate(images):
        canvas.paste(img, ((index % cols) * tile_w, (index // cols) * tile_h))

    if max_width and canvas.width > int(max_width):
        ratio = int(max_width) / canvas.width
        canvas = canvas.resize(
            (int(max_width), max(1, int(canvas.height * ratio))),
            Image.Resampling.LANCZOS,
        )

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=int(quality))
    return _convert(buf.getvalue(), fmt)


# 本模块落盘的图像文件名：`YYYYmmdd_HHMMSS_ffffff[_后缀].jpg`。
# 微秒时间戳天然不重名；后缀是可选的用途标记（look / search_front / grid / observe …），
# 由消费者在 save=True 时给出，字符集限制为 [A-Za-z0-9_-]。
_PHOTO_NAME_RE = re.compile(r"^\d{8}_\d{6}_\d{6}(?:_[A-Za-z0-9_-]+)?\.jpg$")
_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def photo_path(suffix: Optional[str] = None) -> Path:
    """本轮图像的落盘路径：RUNTIME_PHOTOS_DIR/<时间戳>[_<后缀>].jpg。

    这是**全仓图像落盘路径的唯一出处**：图像消费者不再自己拼路径、也不传路径进来，
    只传「是否保存」（save）与可选用途后缀。同名（微秒也撞上）时补 `_1`、`_2` 兜底。
    """
    now = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    base = f"{stamp}_{int(now * 1_000_000) % 1_000_000:06d}"
    if suffix:
        safe = str(suffix).strip()
        if not _SUFFIX_RE.match(safe):
            raise ValueError(f"图像后缀只允许字母数字下划线连字符: {suffix!r}")
        base = f"{base}_{safe}"
    path = RUNTIME_PHOTOS_DIR / f"{base}.jpg"
    serial = 1
    while path.exists():
        path = RUNTIME_PHOTOS_DIR / f"{base}_{serial}.jpg"
        serial += 1
    return path


def is_managed_photo(name: str) -> bool:
    """这个名字是不是本模块落盘时用的命名（`<时间戳>[_后缀].jpg`）。

    「哪些文件算本模块落的」这条判据只此一处，删旧图的执行者（Observer/cleanup.py 的
    体积清扫）拿它当筛选条件，别自己另写一份 glob：判据一旦分叉，就会出现要么越攒越多
    （写手认的这里不认）、要么误删（这里认的写手不认）的后果。传的是**文件名**不是路径，
    所以目录里其它人工放的图片、以及 `scratch/` 子目录里的临时帧都天然落选。
    """
    return _PHOTO_NAME_RE.match(name) is not None


def save_photo(data: FrameData, suffix: Optional[str] = None) -> Optional[Path]:
    """把一张图落盘到 RUNTIME_PHOTOS_DIR，返回路径；失败返回 None。

    **这是全仓唯一的图像落盘函数**（frame_producer 独占管理写入路径）：
    传 jpeg bytes 或 base64 str 均可（后者先解码回字节）。落盘是旁路——供事后回看、
    复现大模型看到的内容或任务留档，写盘失败只告警不抛，绝不拖垮视觉主链路。
    **只写不清**：目录的体积上限由 Observer/cleanup.py 的清扫管（见模块头）。
    """
    jpeg: bytes
    if isinstance(data, str):
        try:
            jpeg = base64.b64decode(data)
        except Exception as exc:  # noqa: BLE001 —— 畸形 base64 只告警
            _LOG.warning("图像落盘跳过：base64 解码失败 %r", exc)
            return None
    else:
        jpeg = data
    try:
        path = photo_path(suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg)
    except (OSError, ValueError) as exc:
        _LOG.warning("图像落盘失败(%s): %r", RUNTIME_PHOTOS_DIR, exc)
        return None
    return path


# 抓拍中间帧目录（RUNTIME_PHOTOS_DIR/scratch）里，实例子目录的命名：pid_<pid>_<纳秒>。
_SCRATCH_PREFIX = "pid_"


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（判据宽松：拿不准当活着，宁可不删别人的目录）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # 权限等其它错误 → 当作还在
    return True


def _convert(jpeg_bytes: bytes, fmt: FrameFormat) -> FrameData:
    fmt = fmt.lower()  # type: ignore[assignment]
    if fmt == "jpeg":
        return jpeg_bytes
    if fmt == "base64":
        return base64.b64encode(jpeg_bytes).decode("ascii")
    raise ValueError("fmt must be 'jpeg' or 'base64'")


def _resolve_stack_count(requested: Optional[int], max_frames: int) -> int:
    """把请求的拼接帧数夹到 [1, 帧环深度]。

    为什么夹取只写这一处：帧环里没有的帧拼不出来（上界＝本实例的 max_frames，注意它可能
    比 configs.FRAME_PRODUCER_MAX_FRAMES 小 —— 自检 CLI 的 --max-frames 能调小它），
    下界 1 是自明值（一帧＝只看当下一张，不再有「帧间差异」这个判据）。

    **不在这里为「成图尺寸」再设一道上限**：那是个物理事实（10 帧纵向拼起来是 1920×10800，
    PIL 解成 RGB 约 180MB、上传耗时可能顶到 VISION_TIMEOUT），已经写在
    configs.FRAME_PRODUCER_MAX_FRAMES 那一行的注释里 —— 它是给人看的警示，不是代码里的限制，
    因为「多大算太大」取决于调用方要不要 resize（本函数的调用方可以传 resize）。
    """
    want = int(VISION_STACK_FRAMES) if requested is None else int(requested)
    return max(1, min(want, int(max_frames)))


class LatestFrameProducer:
    """camera 技能周期性抓拍 → 滚动帧环缓存，供多消费者共享读取。

    帧元素统一为 (frame_id:int, wall_time:float, jpeg:bytes)，旧→新入环；
    deque(maxlen=max_frames) 自动滚出最旧帧。所有读接口在锁内拷贝引用、锁外转换。
    """

    def __init__(
        self,
        capture_script: str = str(CAMERA_SHM_CAPTURE_SCRIPT),
        socket_path: str = str(CAMERA_SHM_SOCKET),
        interval: Optional[float] = None,
        capture_timeout: Optional[float] = None,
        max_frames: Optional[int] = None,
        tmp_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.capture_script = str(capture_script)
        # 读哪条共享内存：capture.py 的 -s。**只读，不碰设备**——换相机就是换这个值
        # （颈部 /tmp/neck_jpeg、主相机 /tmp/foo_fhd）。
        self.socket_path = str(socket_path)
        # 抓拍节奏**只有这一个来源**（构造参数，缺省 = configs.FRAME_PRODUCER_INTERVAL）：
        # 生产者是全体消费者共享的常驻取帧服务，就该一直以高频率抓帧把帧环攒满，
        # 不受「视觉观测参数」那层运行时覆盖层影响（详见 configs.FRAME_PRODUCER_INTERVAL 那行）。
        self.interval = (
            float(FRAME_PRODUCER_INTERVAL) if interval is None else float(interval)
        )
        # 拼接帧数被夹取的告警去重（见 get_stacked_frame）：**不复用 _warn_throttled**，
        # 那个节流器与抓拍失败共用 _last_warn_at，一条每 3 秒重复的配置告警会把真正的
        # 抓拍失败日志压掉。
        self._last_stack_warn: Optional[int] = None
        self.capture_timeout = (
            float(FRAME_PRODUCER_CAPTURE_TIMEOUT)
            if capture_timeout is None
            else float(capture_timeout)
        )
        self.max_frames = int(
            FRAME_PRODUCER_MAX_FRAMES if max_frames is None else max_frames
        )
        if self.interval <= 0:
            raise ValueError("interval must be > 0")
        if self.capture_timeout <= 0:
            raise ValueError("capture_timeout must be > 0")
        if self.max_frames <= 0:
            raise ValueError("max_frames must be > 0")

        # 抓拍中间帧也要落在受管目录里（全仓图像写入只有 RUNTIME_PHOTOS_DIR 一个根）：
        # 每个实例一个带 pid 的子目录，stop() 时整目录回收，不再往 /tmp 里堆空目录。
        # **目录是惰性建的**（见 _ensure_tmp_dir）：构造实例只算路径、不碰文件系统 ——
        # 消费者进程（角色侧工具、独立 CLI）拿到的常常是个空帧环，不该因此留下空目录。
        self._owns_tmp_dir = tmp_dir is None
        self._tmp_fallback = False   # 受管目录不可用 → 回退系统临时目录（只回退一次）
        self.tmp_dir = (
            Path(tmp_dir)
            if tmp_dir is not None
            else (
                RUNTIME_PHOTOS_DIR
                / "scratch"
                / f"{_SCRATCH_PREFIX}{os.getpid()}_{time.time_ns()}"
            )
        )

        self._lock = threading.Lock()
        self._frame_ready = threading.Event()
        self._ring: Deque[Tuple[int, float, bytes]] = deque(maxlen=self.max_frames)
        self._latest_frame_id = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_warn_at = 0.0
        self._owner_fd: Optional[int] = None  # 跨进程单实例 flock fd；None=未持有

    # ------------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._running

    @property
    def latest_frame_id(self) -> int:
        """已成功发布的帧序号（等于环内最新帧 id）。"""
        with self._lock:
            return self._latest_frame_id

    @property
    def latest_frame_time(self) -> Optional[float]:
        """环内最新帧的 Unix 时间戳；尚无帧返回 None。"""
        with self._lock:
            if not self._ring:
                return None
            return self._ring[-1][1]

    @property
    def ring_frames(self) -> int:
        """环内当前帧数（诊断/自检用；不编码、不拷贝帧）。"""
        with self._lock:
            return len(self._ring)

    def _acquire_process_lock(self) -> bool:
        """跨进程单实例 flock（非阻塞独占 FRAME_PRODUCER_LOCK_FILE）。

        成功：持有 fd（_owner_fd），进程退出/close 自动释放 → 无陈旧锁问题。
        失败：另一进程（receiver 或独立 observer）已持锁 → 返回 False，不启动抓拍。
        锁文件打不开（如只读 FS）→ 告警并 fail-open 放行，保证生产链路不因锁文件
        异常而整体瘫痪（此时退化为「进程内唯一」，跨进程唯一性不保证）。
        """
        if self._owner_fd is not None:
            return True  # 本实例已持有
        path = FRAME_PRODUCER_LOCK_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            _LOG.warning(
                "frame_producer 单实例锁文件不可用(%s)，fail-open 放行，"
                "跨进程唯一性不保证: %r",
                path,
                exc,
            )
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner = "?"
            try:
                owner = os.read(fd, 64).decode(errors="replace").strip() or "?"
            except OSError:
                pass
            os.close(fd)
            _LOG.warning(
                "frame_producer 已被另一进程持有(pid=%s)，本进程不再启动抓拍；"
                "如需切换持有者，先停掉已运行的那个进程",
                owner,
            )
            return False
        self._owner_fd = fd
        try:  # 写入本进程 pid 供后续竞争方诊断
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            pass
        return True

    def _release_process_lock(self) -> None:
        """释放跨进程单实例锁（close fd 即解锁）。"""
        if self._owner_fd is not None:
            try:
                os.close(self._owner_fd)
            except OSError:
                pass
            self._owner_fd = None

    def start(self) -> bool:
        """拉起唯一后台抓拍线程。

        返回 True：本进程已持有 / 刚抢到跨进程单实例锁并启动抓拍（幂等）。
        返回 False：另一进程已持锁，本实例不抓拍。
        """
        if self._running:
            return True  # 幂等：进程内已运行，直接复用
        if not self._acquire_process_lock():
            return False
        self._cleanup_stale_scratch()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="robotdog-frame-producer"
        )
        self._thread.start()
        _LOG.info(
            "frame producer started(pid=%d, lock=%s): capture=%s socket=%s "
            "interval=%.1fs timeout=%.1fs ring=%d",
            os.getpid(),
            FRAME_PRODUCER_LOCK_FILE,
            self.capture_script,
            self.socket_path,
            self.interval,
            self.capture_timeout,
            self.max_frames,
        )
        return True

    def stop(self, join_timeout: float = 3.0) -> None:
        """停止抓拍线程、回收本实例的临时帧目录并释放跨进程单实例锁（幂等）。"""
        if self._running:
            self._running = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=join_timeout)
            self._thread = None
            _LOG.info("frame producer stopped")
        self._release_process_lock()
        self._cleanup_tmp_dir()

    def _ensure_tmp_dir(self) -> Path:
        """确保临时帧目录存在并返回它（惰性建，见 __init__ 那段）。

        受管目录不可用时回退系统临时目录，**只回退一次**：否则每次抓拍失败都新建一个
        /tmp 目录，攒垃圾而不自知。
        """
        if self.tmp_dir.exists():
            return self.tmp_dir
        try:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if not self._tmp_fallback:
                self._tmp_fallback = True
                _LOG.warning(
                    "受管临时帧目录不可用(%s)，回退系统临时目录: %r", self.tmp_dir, exc
                )
                self.tmp_dir = Path(tempfile.mkdtemp(prefix="frame_producer_"))
        return self.tmp_dir

    def _cleanup_tmp_dir(self) -> None:
        """删掉本实例自己创建的临时帧目录（外部传入 tmp_dir 的不动）。

        单帧在读完时就已 unlink，这里收掉空目录与任何残留；scratch 根目录空了也一并收掉。
        """
        if not self._owns_tmp_dir:
            return
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        try:
            (RUNTIME_PHOTOS_DIR / "scratch").rmdir()  # 只在空时成功，非空自然失败
        except OSError:
            pass

    def _cleanup_stale_scratch(self) -> None:
        """清理 scratch 下 pid 已不存在的残留子目录（上次异常退出留下的）。

        只在拿到跨进程单实例锁之后调用（此时没有别的生产者在跑，它的目录也已被本进程
        之外的规则排除：pid 存活的目录一律跳过）。
        """
        root = RUNTIME_PHOTOS_DIR / "scratch"
        try:
            names = os.listdir(root)
        except OSError:
            return
        for name in names:
            if not name.startswith(_SCRATCH_PREFIX):
                continue
            parts = name.split("_", 2)
            try:
                pid = int(parts[1])
            except (IndexError, ValueError):
                continue
            if pid == os.getpid() or _pid_alive(pid):
                continue
            shutil.rmtree(root / name, ignore_errors=True)

    def wait_for_first_frame(self, timeout: Optional[float] = None) -> bool:
        """等待至少一帧被缓存。"""
        return self._frame_ready.wait(timeout=timeout)

    # ------------------------------------------------------------------ 后台线程
    def _run(self) -> None:
        while self._running:
            # 节奏在构造时就定了，本轮内不再变：抓拍线程是常驻的取帧服务，
            # 不参与任何运行时调参（见 __init__ 里 self.interval 那段）。
            t0 = time.monotonic()
            try:
                self._capture_once()
            except Exception as exc:  # noqa: BLE001 —— 单帧失败绝不拖垮后台线程
                self._warn_throttled("抓拍异常: %r", exc)
            sleep_left = self.interval - (time.monotonic() - t0)
            if sleep_left > 0:
                time.sleep(sleep_left)

    def _capture_once(self) -> bool:
        """从共享内存抓一帧 JPEG 并压入帧环；失败保留旧缓存返回 False。"""
        out_path = self._ensure_tmp_dir() / f"frame_{time.time_ns()}.jpg"
        if not self._run_capture(out_path):
            self._warn_throttled(
                "共享内存抓帧失败(socket=%s)，保留旧帧缓存（latest_id=%d）",
                self.socket_path,
                self.latest_frame_id,
            )
            return False

        try:
            raw = out_path.read_bytes()
        finally:
            try:
                out_path.unlink()
            except OSError:
                pass
        jpeg = _coalesce_jpeg(raw)
        if not jpeg:
            self._warn_throttled("抓拍结果中无完整 JPEG，丢弃本帧")
            return False

        with self._lock:
            self._latest_frame_id += 1
            self._ring.append((self._latest_frame_id, time.time(), jpeg))
        self._frame_ready.set()
        return True

    def _run_capture(self, out_path: Path) -> bool:
        """执行 skills/camera/scripts/capture.py 从共享内存取一帧；镜像 vision_dispatch
        的成功姿势：bash -c "source ~/.bashrc && capture.py -o <tmp> -s <socket> -q -t <timeout>"。
        """
        cmd = (
            "source ~/.bashrc >/dev/null 2>&1 && exec "
            f"{shlex.quote(self.capture_script)} -o {shlex.quote(str(out_path))} "
            f"-s {shlex.quote(self.socket_path)} "
            f"-q -t {int(self.capture_timeout)}"
        )
        try:
            proc = subprocess.run(
                ["bash", "-c", cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=self.capture_timeout + 5.0,
            )
        except subprocess.TimeoutExpired:
            # 印的是**外层硬杀**这条上限（capture_timeout + 5），不是脚本自己的 -t：
            # 脚本内超时后还要 terminate/kill 收尾，那一步卡住时能超出 -t 一大截，
            # 只报 -t 会让人以为「1 秒就放弃了」（真机日志里就误读过一次）。
            self._warn_throttled(
                "共享内存抓帧超时(socket=%s, >%.0fs，脚本内 -t %.0fs 未兜住)",
                self.socket_path,
                self.capture_timeout + 5.0,
                self.capture_timeout,
            )
            return False

        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            self._warn_throttled(
                "capture.py 返回失败(socket=%s): %s",
                self.socket_path,
                (err or f"exit={proc.returncode}")[-300:],
            )
            return False
        return out_path.exists() and out_path.stat().st_size > 0

    # ------------------------------------------------------------------ 读接口
    @staticmethod
    def _encode(
        jpeg: bytes, fmt: FrameFormat, resize: Optional[Tuple[int, int]]
    ) -> Optional[FrameData]:
        """按需缩放后转为目标格式；resize 为空即原样透传（不重编码）。

        缩放失败（坏图）返回 None —— 与「无帧」同语义，调用方都按取帧失败处理。
        """
        if resize is not None:
            resized = _resize_jpeg(jpeg, resize)
            if resized is None:
                return None
            jpeg = resized
        return _convert(jpeg, fmt)

    @staticmethod
    def _maybe_save(data: FrameData, save: bool, suffix: Optional[str]) -> None:
        """save=True 时把这份数据落盘到 RUNTIME_PHOTOS_DIR（旁路，失败只告警）。"""
        if save:
            save_photo(data, suffix)

    def get_latest_frame(
        self,
        fmt: FrameFormat = "jpeg",
        resize: Optional[Sequence[int]] = None,
        *,
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> Optional[FrameData]:
        """返回环内最新一帧；尚无帧返回 None。

        resize=(宽, 高) 时把该帧缩到指定尺寸（默认 None＝原分辨率，1920×1080）；
        缩放失败同样返回 None。
        save=True 时把**返回的这份**（缩放后的）落盘留档，失败只告警；默认不落盘。
        图像消费者只用 save 这一个参数决定要不要留文件，不自己拼路径。
        """
        size = _normalize_resize(resize)
        with self._lock:
            jpeg = self._ring[-1][2] if self._ring else None
        if jpeg is None:
            return None
        data = self._encode(jpeg, fmt, size)
        if data is None:
            return None
        self._maybe_save(data, save, suffix)
        return data

    def get_frames(
        self,
        count: Optional[int] = None,
        fmt: FrameFormat = "jpeg",
        resize: Optional[Sequence[int]] = None,
        *,
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> List[FrameData]:
        """返回最近 count 帧（旧→新）；count 为空返回全部环内帧。

        resize 语义同 get_latest_frame；解析/缩放失败的帧被跳过（不占位）。
        save=True 时逐帧落盘留档（默认不落盘）。
        """
        size = _normalize_resize(resize)
        with self._lock:
            entries = list(self._ring)
        if count is not None:
            if count <= 0:
                return []
            entries = entries[-int(count):]
        frames = [self._encode(j[2], fmt, size) for j in entries]
        valid = [f for f in frames if f is not None]
        for frame in valid:
            self._maybe_save(frame, save, suffix)
        return valid

    def get_frames_since(
        self,
        after_id: int,
        fmt: FrameFormat = "jpeg",
        resize: Optional[Sequence[int]] = None,
        *,
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> List[FrameData]:
        """返回环内 id > after_id 的帧（旧→新），供消费者追帧；无则 []。

        resize 语义同 get_latest_frame；解析/缩放失败的帧被跳过（不占位）。
        save=True 时逐帧落盘留档（默认不落盘）。
        """
        size = _normalize_resize(resize)
        with self._lock:
            entries = [j for j in self._ring if j[0] > after_id]
        frames = [self._encode(j[2], fmt, size) for j in entries]
        valid = [f for f in frames if f is not None]
        for frame in valid:
            self._maybe_save(frame, save, suffix)
        return valid

    def wait_for_new_frame(
        self, after_id: int, timeout: Optional[float] = None
    ) -> Optional[int]:
        """阻塞到缓存出现 id > after_id 的帧，返回该最新 id；超时/停止返回 None。

        各消费者把自已的“已见 id”传给本函数即可错峰共享同一帧源。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if not self._running:
                return None
            with self._lock:
                if self._latest_frame_id > after_id:
                    return self._latest_frame_id
            if deadline is None:
                time.sleep(0.05)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.05, remaining))

    def get_stacked_frame(
        self,
        fmt: FrameFormat = "jpeg",
        quality: int = 85,
        resize: Optional[Sequence[int]] = None,
        *,
        count: Optional[int] = None,
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> Optional[FrameData]:
        """取环内（时间上）最新的 count 帧，自上而下拼成一张图并返回该图。

        - 选帧：帧环末尾连续 count 帧，即 id 最大的 count 帧（旧→新）。环内不足 count 帧
          时按可用帧拼；count 超过帧环深度时夹到环深（见 _resolve_stack_count）。
        - count=None（默认）＝按 configs.VISION_STACK_FRAMES 的出厂值（3），与历来的
          `[-3:]` 逐字同义。要按**运行时可调**的值拼，由**消费者**先调
          Orchestrator.vision_tunables.current() 再显式传进来 —— 本模块是帧环，只管机制
          （取几帧、怎么夹取），不该知道「谁在调参、为什么调」这件事，那是消费者的策略。
        - 标注：每张子图左上角标 `frame 位置`（frame 1 = 最旧，数字越大越新），位置为拼接后
          自上而下的序号；字号取子图高度 1/10，缩小也能辨认。
        - 尺寸不一致不缩放，按最大宽度左对齐、空余补黑（见 _stack_jpegs）。
        - resize=(宽, 高) 时**逐帧**缩到该尺寸再拼（默认 None＝原分辨率）：
          拼图尺寸为 宽 ×(count×高)，例如 3 帧 @ (1280, 720) → 1280×2160。
          ⚠️ 不传 resize 时拼图高度 = count × 1080，count 调大要留神成图尺寸与上传耗时
          （见 configs.FRAME_PRODUCER_MAX_FRAMES 那行的注释）。
        - 帧环为空、或所有帧都无法解析时返回 None（不阻塞、不触发抓拍，同其余读接口）。
        - fmt='jpeg' 返回 bytes，'base64' 返回 str；quality 为输出 JPEG 质量。
        - save=True 时把**返回的这张图**落盘到 RUNTIME_PHOTOS_DIR 留档（视觉观测每轮
          存一张，供事后回看大模型究竟看到了什么）；失败只告警，不影响返回值。
          默认不落盘 —— 留不留文件由消费者用 save 决定，路径不由消费者给。
          依赖 Pillow（延迟 import，模块其余功能不依赖它）。
        """
        want = _resolve_stack_count(count, self.max_frames)
        if count is not None and int(count) != want and self._last_stack_warn != int(count):
            # 夹取只在读侧做、且只提醒一次（同一个值重复告警没意义）。刻意不用
            # _warn_throttled：它的节流器与抓拍失败共用，会压掉真正的抓拍失败日志。
            self._last_stack_warn = int(count)
            _LOG.warning(
                "拼接帧数 %s 超出可用范围，已夹到 %d（帧环深度 %d）",
                count,
                want,
                self.max_frames,
            )
        size = _normalize_resize(resize)
        with self._lock:
            entries = list(self._ring)[-want:]  # 环内仍保持旧→新顺序
        if not entries:
            return None
        tiles = [(pos, entry[0], entry[2]) for pos, entry in enumerate(entries, start=1)]
        # 先按 jpeg 生成（不管调用方要 jpeg 还是 base64，存的都是同一张图）
        jpeg = _stack_jpegs(tiles, quality=quality, fmt="jpeg", resize=size)
        if jpeg is None:
            return None
        assert isinstance(jpeg, bytes)

        self._maybe_save(jpeg, save, suffix)
        return _convert(jpeg, fmt)

    def compose_grid(
        self,
        images: Sequence[bytes],
        *,
        cols: int = 2,
        quality: int = 85,
        max_width: Optional[int] = 2560,
        fmt: FrameFormat = "jpeg",
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> Optional[FrameData]:
        """把调用方给的多张 jpeg 排成 cols 列网格图并返回（环绕搜索的四宫格用它）。

        帧数据由调用方从帧环逐张取（各视角要取「转向之后」的新帧，只有调用方知道时机），
        拼图本身与落盘都在本模块做——消费者只说要不要留档，不碰路径。
        子图尺寸不一致不缩放、补黑；max_width 非空且成图超宽时整张等比缩到该宽
        （VLM 服务端最大支持 2560 宽）；不画标签，与旧 ImageMagick montage 观感一致。
        save=True 时把这**一张**成图落盘留档（子图不单独存），默认不落盘。
        """
        if not images:
            return None
        data = _grid_jpegs(
            images, cols=cols, quality=quality, fmt="jpeg", max_width=max_width
        )
        if data is None:
            return None
        assert isinstance(data, bytes)
        self._maybe_save(data, save, suffix)
        return _convert(data, fmt)

    def iter_latest_frames(
        self,
        interval_seconds: float,
        fmt: FrameFormat = "jpeg",
        *,
        wait_first_frame: bool = True,
        resize: Optional[Sequence[int]] = None,
    ) -> Generator[FrameData, None, None]:
        """按 interval_seconds 周期 yield 环内最新帧（deadline 对齐，无累积漂移）。

        不额外开相机/生产者，仅读共享帧环；多个生成器可并存互不影响。
        resize 语义同 get_latest_frame（默认 None＝原分辨率）。
        **没有 save 参数**（与其它读接口不同）：生成器是长期流，逐帧落盘等于把帧环
        内容持续灌进受管目录、把别处的留档挤掉。要留档就按帧直接调 save_photo()。
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        size = _normalize_resize(resize)
        if wait_first_frame:
            self._frame_ready.wait()
        next_deadline = time.monotonic()
        while self._running:
            now = time.monotonic()
            if now < next_deadline:
                time.sleep(next_deadline - now)
            frame = self.get_latest_frame(fmt, resize=size)
            if frame is not None:
                yield frame
            next_deadline += interval_seconds
            now = time.monotonic()
            if next_deadline <= now:
                missed = int((now - next_deadline) // interval_seconds) + 1
                next_deadline += missed * interval_seconds

    # ------------------------------------------------------------------ 其它
    def _warn_throttled(self, msg: str, *args: object) -> None:
        """失败日志节流（至少 10s 打一条），避免周期失败刷屏。"""
        now = time.time()
        if now - self._last_warn_at >= 10.0:
            self._last_warn_at = now
            _LOG.warning(msg, *args)


# =============================================================================
# 跨进程取帧：客户端 + 服务端（--serve）
#
# 帧环是「帧生产者所在进程」的内存，别的进程读不到 —— 取帧要跨进程只有一条路：
#   取帧服务进程（`frame_producer.py --serve`，持 FRAME_PRODUCER_LOCK_FILE）持有帧环，
#   其余进程经 unix socket（FRAME_PRODUCER_SOCKET，在 runtime_files/ 下）向它要帧。
# 服务端**不复制任何取帧逻辑**：每条请求原样转给 LatestFrameProducer 的同名方法。
#
# 线格式（请求与应答同构）:
#   u32(header_len) | header_json | blob…（每条 blob 前面另有 u32 长度）
#   请求头 = {"op": …, "args": {…}, "n_blobs": n}
#   应答头 = {"ok": true,  "result": …, "n_blobs": n}
#          | {"ok": false, "error": "TypeError: …"}   ← 服务端异常回给客户端，不吞
# 帧一律以**原始 JPEG 字节**走 blob（单帧约 400KB，不做 base64 膨胀）；服务端只认 jpeg，
# fmt='base64' 由客户端用 _convert 就地转 —— 于是协议只有一种载荷。
# resize 与 save 都在服务端：resize 要 Pillow（工具进程不碰它），save 必须守住
# 「全仓唯一图像写手」（save_photo）这条规矩。
#
# 为什么自己开一个 unix socket 服务（本仓第一例）：帧是二进制大块，unix socket 原生传
# 字节、不在 eth0 上开端口、权限由内核限到本机用户；waypoint_nav/dashboard.py 那套 HTTP
# 是给浏览器看的，帧路径不该为此引入 HTTP 客户端库。
# =============================================================================

# 定长整数一律 u32：长度字段。_MAX_* 是两端共用的 sanity 上限（防对端乱报长度吃内存）。
_U32 = struct.Struct("!I")
_MAX_HEADER_BYTES = 64 * 1024   # 头就是一小段 JSON（op + 参数）
_MAX_BLOBS = 64                 # 一条消息最多几条 blob（拼图 4 张、拼接 1 张）
_ATTACH_RETRY_SEC = 0.2         # 等取帧服务起来的重试间隔（秒）
_SERVICE_WARN_INTERVAL = 10.0   # 「服务调用失败」日志的节流间隔（秒）


class FrameServiceError(RuntimeError):
    """取帧服务不可达 / 通信协议出错。

    **不是**「帧环为空」—— 那是各读接口返回 None。这个异常表示取帧这条路本身断了
    （socket 连不上、连上后断开、服务端抛异常）。
    """


class FrameSourceState(NamedTuple):
    """describe_source() 的返回：本进程当前从哪儿取帧。"""

    backend: str   # local-owner / service / service-unreachable
    detail: str    # 给人看的说明（含 socket 路径或环内帧数）
    latest_frame_id: Optional[int]


def _send_message(
    sock: socket.socket, header: Dict[str, Any], blobs: Sequence[bytes] = ()
) -> None:
    """发一条消息：头（补上 n_blobs）+ 每条 blob（各带 u32 长度）。"""
    payload = json.dumps({**header, "n_blobs": len(blobs)}).encode("utf-8")
    chunks = [_U32.pack(len(payload)), payload]
    for blob in blobs:
        chunks.append(_U32.pack(len(blob)))
        chunks.append(blob)
    sock.sendall(b"".join(chunks))


def _recv_exact(sock: socket.socket, size: int) -> Optional[bytes]:
    """收满 size 字节；对端先关闭返回 None（调用方按「连接断开」处理）。"""
    buf = bytearray()
    while len(buf) < size:
        try:
            chunk = sock.recv(size - len(buf))
        except OSError as exc:
            raise FrameServiceError(f"读取取帧服务失败: {exc!r}") from exc
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _recv_message(sock: socket.socket) -> Tuple[Optional[Dict[str, Any]], List[bytes]]:
    """收一条消息；对端恰好停在消息边界关闭时返回 (None, [])。"""
    raw = _recv_exact(sock, _U32.size)
    if raw is None:
        return None, []
    header_len = _U32.unpack(raw)[0]
    if not 0 < header_len <= _MAX_HEADER_BYTES:
        raise FrameServiceError(f"取帧服务消息头长度非法: {header_len}")
    raw = _recv_exact(sock, header_len)
    if raw is None:
        return None, []
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameServiceError(f"取帧服务消息头不是合法 JSON: {exc!r}") from exc
    if not isinstance(header, dict):
        raise FrameServiceError("取帧服务消息头不是 JSON 对象")

    n_blobs = header.get("n_blobs") or 0
    if not isinstance(n_blobs, int) or not 0 <= n_blobs <= _MAX_BLOBS:
        raise FrameServiceError(f"取帧服务消息 blob 条数非法: {n_blobs!r}")
    blobs: List[bytes] = []
    for _ in range(n_blobs):
        raw = _recv_exact(sock, _U32.size)
        if raw is None:
            return None, []
        size = _U32.unpack(raw)[0]
        if size > FRAME_PRODUCER_MAX_BLOB_BYTES:
            raise FrameServiceError(f"取帧服务 blob 长度超上限: {size}")
        blob = _recv_exact(sock, size)
        if blob is None:
            return None, []
        blobs.append(blob)
    return header, blobs


def _bounded_wait(timeout: Optional[float]) -> float:
    """等待类调用的服务端等待上限（秒）。

    本地版 `timeout=None` 是「一直等到停止」，经 socket 就必须封顶 —— 否则服务端那条
    连接线程会一直挂着，客户端自己也无限期等下去。
    """
    return FRAME_PRODUCER_RPC_TIMEOUT if timeout is None else float(timeout)


def _frame_args(
    resize: Optional[Sequence[int]], save: bool, suffix: Optional[str]
) -> Dict[str, Any]:
    """取帧参数 → JSON 可传的 dict（resize 是元组，JSON 里走 list）。"""
    return {
        "resize": None if resize is None else [int(resize[0]), int(resize[1])],
        "save": bool(save),
        "suffix": suffix,
    }


def _opt_int(value: Optional[int]) -> Optional[int]:
    """None 透传，其余转 int（JSON 里 None 与 0 是两回事：count=None＝全部帧）。"""
    return None if value is None else int(value)


def _opt_float(value: Any) -> Optional[float]:
    """None 透传，其余转 float（wait 类调用的 timeout）。"""
    return None if value is None else float(value)


class FrameServiceClient:
    """取帧服务的只读客户端：名字与 LatestFrameProducer 的读取侧逐个对齐。

    消费者（vision_observer / vision_dispatch / bull_chase）因此不必知道帧在哪个进程里：
    拿到这个句柄就能照原样调用（见模块 start()）。

    **只读**：它不拥有生产者，`stop()` 只关本进程这条连接，绝不停服务 —— 服务由
    start_agent 的子进程管理负责停（见 start_agent._stop_children）。
    连接是长连接（一个客户端一条）；任何一次失败的调用都会作废它，所以服务重启最多丢
    一次调用，不会一直失败 —— 但「下一次取帧重新连」只对**模块级取帧函数**成立（它们的
    _service_client() 会重新拨号并换掉单例）。**本对象自己不会重连**：_close() 只把
    self._sock 置 None，谁把它存起来，谁在第一次失败之后就一直拿到「连接已关闭」。
    长活消费者请走模块级函数，别存句柄（见 _call）。

    **一条连接同时只跑一条请求**（_io_lock）：进程里可能有多个线程共用它 ——
    start_agent 启动期就是主线程（_wait_first_frame）与进程内视觉观测器线程同时在等首帧。
    socket 上的字节流没有消息边界，两个线程各自 recv 会把一条应答按到达顺序分掉，于是
    有人把一条应答的 JSON 头当成下一条的 4 字节长度（实测报「消息头长度非法: 2065854315」
    —— 那正是 `{"ok` 四个字节），两边一起失败、连接作废。代价：有人在做长等待
    （wait_for_first_frame / wait_for_new_frame）时，本进程的其它取帧调用要排队 ——
    常驻消费者只有观测器一条线，这点确定性比那点并发值钱。
    """

    def __init__(self, sock: socket.socket, sock_path: str) -> None:
        self.sock_path = sock_path
        self._sock: Optional[socket.socket] = sock
        self._io_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        """连接是否已作废（服务端断开或调用失败后为 True）。"""
        return self._sock is None

    @classmethod
    def try_connect(
        cls, *, timeout: Optional[float] = None
    ) -> Optional["FrameServiceClient"]:
        """连取帧服务并握手（发一条 status）；连不上返回 None（不抛）。

        timeout 非空＝在这个窗口内重试（给「服务刚被拉起」用，见 attach()）；缺省只试
        一次 —— 稳态取帧不该为一个不存在的服务等待。
        """
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            client = cls._connect_once()
            if client is not None:
                return client
            if deadline is None or time.monotonic() >= deadline:
                return None
            time.sleep(_ATTACH_RETRY_SEC)

    @classmethod
    def _connect_once(cls) -> Optional["FrameServiceClient"]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(FRAME_PRODUCER_RPC_TIMEOUT)
        try:
            sock.connect(str(FRAME_PRODUCER_SOCKET))
            client = cls(sock, str(FRAME_PRODUCER_SOCKET))
            client.status()  # 握手：确认对端说的是同一套协议
            return client
        except (OSError, FrameServiceError):
            try:
                sock.close()
            except OSError:
                pass
            return None

    # ------------------------------------------------------------- 状态与读接口
    def status(self) -> Dict[str, Any]:
        """服务当前状态：pid / running / latest_frame_id / ring_frames / interval / …"""
        result, _ = self._request("status")
        return dict(result or {})

    @property
    def running(self) -> bool:
        """服务端的抓拍线程是否在跑（服务活着时恒为 True）。"""
        return bool(self.status().get("running"))

    @property
    def latest_frame_id(self) -> int:
        """服务端帧环的最新帧号（每次读取都是一次 RPC）。"""
        return int(self.status().get("latest_frame_id") or 0)

    @property
    def latest_frame_time(self) -> Optional[float]:
        value = self.status().get("latest_frame_time")
        return None if value is None else float(value)

    @property
    def ring_frames(self) -> int:
        return int(self.status().get("ring_frames") or 0)

    @property
    def max_frames(self) -> int:
        return int(self.status().get("max_frames") or 0)

    @property
    def interval(self) -> float:
        return float(self.status().get("interval") or 0.0)

    def wait_for_first_frame(self, timeout: Optional[float] = None) -> bool:
        """等服务端出现首帧；超时返回 False。"""
        bound = _bounded_wait(timeout)
        result, _ = self._request(
            "wait_for_first_frame",
            {"timeout": bound},
            timeout=bound + FRAME_PRODUCER_RPC_TIMEOUT,
        )
        return bool(result)

    def wait_for_new_frame(
        self, after_id: int, timeout: Optional[float] = None
    ) -> Optional[int]:
        """等服务端出现 id > after_id 的帧，返回该帧号；超时返回 None。

        timeout=None 在这里是**有界**的（见 _bounded_wait）—— 远端调用不许无限期等。
        """
        bound = _bounded_wait(timeout)
        result, _ = self._request(
            "wait_for_new_frame",
            {"after_id": int(after_id), "timeout": bound},
            timeout=bound + FRAME_PRODUCER_RPC_TIMEOUT,
        )
        return None if result is None else int(result)

    def get_latest_frame(
        self,
        fmt: FrameFormat = "jpeg",
        resize: Optional[Sequence[int]] = None,
        *,
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> Optional[FrameData]:
        """取最新帧（缩放与落盘都在服务端做）；服务端无帧返回 None。"""
        _, blobs = self._request(
            "get_latest_frame", _frame_args(resize, save, suffix)
        )
        return _convert(blobs[0], fmt) if blobs else None

    def get_frames(
        self,
        count: Optional[int] = None,
        fmt: FrameFormat = "jpeg",
        resize: Optional[Sequence[int]] = None,
        *,
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> List[FrameData]:
        """取最近 count 帧（count=None＝全部环内帧），旧→新。"""
        _, blobs = self._request(
            "get_frames", {**_frame_args(resize, save, suffix), "count": _opt_int(count)}
        )
        return [_convert(blob, fmt) for blob in blobs]

    def get_frames_since(
        self,
        after_id: int,
        fmt: FrameFormat = "jpeg",
        resize: Optional[Sequence[int]] = None,
        *,
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> List[FrameData]:
        """取服务端环内 id > after_id 的帧（旧→新）。"""
        _, blobs = self._request(
            "get_frames_since",
            {**_frame_args(resize, save, suffix), "after_id": int(after_id)},
        )
        return [_convert(blob, fmt) for blob in blobs]

    def get_stacked_frame(
        self,
        fmt: FrameFormat = "jpeg",
        quality: int = 85,
        resize: Optional[Sequence[int]] = None,
        *,
        count: Optional[int] = None,
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> Optional[FrameData]:
        """取服务端最新的 count 帧拼成一张图（拼图与落盘都在服务端）。"""
        _, blobs = self._request(
            "get_stacked_frame",
            {
                **_frame_args(resize, save, suffix),
                "quality": int(quality),
                "count": _opt_int(count),
            },
        )
        return _convert(blobs[0], fmt) if blobs else None

    def compose_grid(
        self,
        images: Sequence[bytes],
        *,
        cols: int = 2,
        quality: int = 85,
        max_width: Optional[int] = 2560,
        fmt: FrameFormat = "jpeg",
        save: bool = False,
        suffix: Optional[str] = None,
    ) -> Optional[FrameData]:
        """把多张 jpeg 上送给服务端拼成网格图（拼图规则与落盘都留在服务端）。"""
        _, blobs = self._request(
            "compose_grid",
            {
                "cols": int(cols),
                "quality": int(quality),
                "max_width": _opt_int(max_width),
                "save": bool(save),
                "suffix": suffix,
            },
            blobs=[bytes(image) for image in images],
        )
        return _convert(blobs[0], fmt) if blobs else None

    def iter_latest_frames(
        self,
        interval_seconds: float,
        fmt: FrameFormat = "jpeg",
        *,
        wait_first_frame: bool = True,
        resize: Optional[Sequence[int]] = None,
    ) -> Generator[FrameData, None, None]:
        """按周期 yield 最新帧；客户端侧用「等首帧 + 取最新」实现同一条节奏。

        本地版是帧环阻塞等待，这里是一条 RPC/轮；节奏对齐与快进规则与本地版逐字一致。
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if wait_first_frame:
            self.wait_for_first_frame()
        next_deadline = time.monotonic()
        while not self.closed:
            delay = next_deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            frame = self.get_latest_frame(fmt, resize=resize)
            if frame is not None:
                yield frame
            next_deadline += interval_seconds
            now = time.monotonic()
            if next_deadline <= now:
                missed = int((now - next_deadline) // interval_seconds) + 1
                next_deadline += missed * interval_seconds

    def stop(self, join_timeout: float = 3.0) -> None:
        """只关本进程这条连接（**绝不停服务**）。

        签名与 LatestFrameProducer.stop() 对齐 —— 调用方（start_agent._teardown、
        独立 CLI）不必区分句柄是哪一种。join_timeout 对客户端没有意义，留作对齐。
        """
        self._close()

    # ------------------------------------------------------------------ 传输
    def _request(
        self,
        op: str,
        args: Optional[Dict[str, Any]] = None,
        blobs: Sequence[bytes] = (),
        *,
        timeout: Optional[float] = None,
    ) -> Tuple[Any, List[bytes]]:
        """发一条请求收一条应答。

        传输层任何问题（连不上/中途断开/协议错）→ FrameServiceError 并作废本连接；
        服务端**业务**异常也转成 FrameServiceError，但连接仍可用（不关）。
        **收发全程持 _io_lock**（一条连接同时只有一条请求在飞，理由见类注释）。
        """
        wait = FRAME_PRODUCER_RPC_TIMEOUT if timeout is None else float(timeout)
        with self._io_lock:
            sock = self._sock          # 锁内重取：等锁期间它可能已被别的线程作废
            if sock is None:
                raise FrameServiceError("取帧服务连接已关闭")
            try:
                sock.settimeout(wait)
                _send_message(sock, {"op": op, "args": args or {}}, blobs)
                header, out_blobs = _recv_message(sock)
            except (OSError, FrameServiceError) as exc:
                self._close()
                raise FrameServiceError(f"取帧服务调用 {op} 失败: {exc}") from exc
        if header is None:
            self._close()
            raise FrameServiceError(f"取帧服务在 {op} 应答前断开连接")
        if not header.get("ok"):
            raise FrameServiceError(f"取帧服务 {op} 出错: {header.get('error')}")
        return header.get("result"), out_blobs

    def _close(self) -> None:
        """作废本连接（**刻意不持 _io_lock**）。

        收摊（stop()）与失败处置都必须立刻生效，不能等在飞的那条请求把锁放回来 ——
        打断卡在 recv 上的那条调用正是这里要的。被打断的一方照常按传输错处置，锁归它释放。
        """
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class _FrameServiceHandler(socketserver.StreamRequestHandler):
    """取帧服务的单连接处理：一条请求 → 一条应答，循环到对端关闭。

    **不实现任何取帧/拼图/落盘逻辑**：每条请求原样转给 server.producer 的同名方法。
    刻意不设 idle 超时：客户端（观测器）可能几十秒才来一次，设了就会把它的长连接掐掉；
    卡住的客户端只占一个线程，而客户端个数是有限的（观测器 + 几个一次性工具子进程），
    且 daemon_threads 让这些线程不挡服务退出。
    """

    def handle(self) -> None:
        while True:
            try:
                header, blobs = _recv_message(self.request)
            except FrameServiceError as exc:
                _LOG.warning("取帧服务: 读请求失败 %s", exc)
                return
            if header is None:
                return
            try:
                result, out_blobs = self._dispatch(header, blobs)
            except Exception as exc:  # noqa: BLE001 —— 服务端任何错都要回给客户端
                _LOG.exception("取帧服务: %s 处理失败", header.get("op"))
                try:
                    _send_message(
                        self.request,
                        {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                    )
                except OSError:
                    return
                continue
            try:
                _send_message(self.request, {"ok": True, "result": result}, out_blobs)
            except OSError:
                return  # 客户端已走（比如 wait 类调用被它自己断开）

    def _dispatch(
        self, header: Dict[str, Any], blobs: List[bytes]
    ) -> Tuple[Any, List[bytes]]:
        """把一条请求转成 producer 的方法调用，返回 (result, blobs)。"""
        op = str(header.get("op") or "")
        args = header.get("args") or {}
        resize = args.get("resize")
        save = bool(args.get("save"))
        suffix = args.get("suffix")
        producer = self.server.producer

        if op == "status":
            return self._status(), []
        if op == "wait_for_first_frame":
            return producer.wait_for_first_frame(
                timeout=_opt_float(args.get("timeout"))
            ), []
        if op == "wait_for_new_frame":
            return producer.wait_for_new_frame(
                int(args["after_id"]), timeout=_opt_float(args.get("timeout"))
            ), []
        if op == "get_latest_frame":
            frame = producer.get_latest_frame(
                "jpeg", resize=resize, save=save, suffix=suffix
            )
            return None, ([] if frame is None else [frame])
        if op == "get_frames":
            frames = producer.get_frames(
                count=_opt_int(args.get("count")),
                fmt="jpeg",
                resize=resize,
                save=save,
                suffix=suffix,
            )
            return None, list(frames)
        if op == "get_frames_since":
            frames = producer.get_frames_since(
                int(args["after_id"]), fmt="jpeg", resize=resize, save=save, suffix=suffix
            )
            return None, list(frames)
        if op == "get_stacked_frame":
            frame = producer.get_stacked_frame(
                fmt="jpeg",
                quality=int(args.get("quality") or 85),
                resize=resize,
                count=_opt_int(args.get("count")),
                save=save,
                suffix=suffix,
            )
            return None, ([] if frame is None else [frame])
        if op == "compose_grid":
            grid = producer.compose_grid(
                list(blobs),
                cols=int(args.get("cols") or 2),
                quality=int(args.get("quality") or 85),
                max_width=_opt_int(args.get("max_width")),
                save=save,
                suffix=suffix,
            )
            return None, ([] if grid is None else [grid])
        raise ValueError(f"未知的取帧服务 op: {op!r}")

    def _status(self) -> Dict[str, Any]:
        producer = self.server.producer
        return {
            "pid": os.getpid(),
            "running": producer.running,
            "latest_frame_id": producer.latest_frame_id,
            "latest_frame_time": producer.latest_frame_time,
            "ring_frames": producer.ring_frames,
            "max_frames": producer.max_frames,
            "interval": producer.interval,
            "capture_script": producer.capture_script,
            "socket_path": producer.socket_path,
        }


class _FrameServiceServer(socketserver.ThreadingUnixStreamServer):
    """取帧服务的服务端：unix socket + 一条连接一个线程。

    并发读不互相卡住的前提在 LatestFrameProducer 那边：读接口在锁内只拷引用、PIL 与
    编码在锁外做，wait_for_new_frame 轮询时**不持锁** —— 于是 N 个客户端同时取帧安全。
    """

    daemon_threads = True    # 退出不必等卡在 recv 上的处理线程（server_close 不 join）
    request_queue_size = 16  # 并发客户端：观测器 + 若干一次性工具子进程

    def __init__(self, sock_path: str, producer: LatestFrameProducer) -> None:
        self.producer = producer
        super().__init__(sock_path, _FrameServiceHandler)


def _serve(args: argparse.Namespace) -> int:
    """`--serve`：前台长驻，把帧环以 unix socket 暴露给其它进程（取帧服务）。

    顺序有意如此：**先抢跨进程单实例锁、再 bind**。抢到独占锁即证明全系统没有别的
    生产者在跑，于是残留的 socket 文件（上次被 kill -9 留下）可以安全删掉重 bind ——
    陈 socket 因此不需要任何探测（探活反而会自己去抢一次锁，见 status_observer）。
    """
    producer = LatestFrameProducer(
        capture_script=args.capture_script,
        socket_path=args.socket,
        interval=args.interval,
        capture_timeout=args.timeout,
        max_frames=args.max_frames,
    )
    if not producer.start():
        print(
            "[frame_producer] 另一进程已持有单实例锁，取帧服务不启动",
            file=sys.stderr,
        )
        return 1
    # 服务进程里它就是「本地帧环」：本进程内的取帧调用走本地，不再绕一圈 socket。
    _adopt_default(producer)

    sock_path = Path(FRAME_PRODUCER_SOCKET)
    if len(os.fsencode(str(sock_path))) >= 108:  # unix sun_path 上限
        print(
            f"[frame_producer] socket 路径过长(>=108 字节)，无法监听: {sock_path}",
            file=sys.stderr,
        )
        producer.stop()
        return 1
    try:
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        server = _FrameServiceServer(str(sock_path), producer)
    except OSError as exc:
        producer.stop()
        print(f"[frame_producer] 取帧服务监听 {sock_path} 失败: {exc!r}", file=sys.stderr)
        return 1
    try:
        os.chmod(sock_path, 0o600)  # stdlib 不设权限，默认受 umask 影响
    except OSError as exc:
        _LOG.warning("取帧服务 socket 权限设置失败(%s): %r", sock_path, exc)

    def _on_signal(signum: int, _frame: object) -> None:
        # 信号处理器与 serve_forever 同在主线程 —— 直接 shutdown() 会死锁，另起一个
        # 线程去关它（socketserver 文档给的标准姿势）。默认 SIGTERM 不跑 atexit，
        # 所以收尾必须显式走 finally：unlink socket + 停抓拍子进程。
        _LOG.info("取帧服务收到信号 %s，停止中", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    print(
        f"[frame_producer] 取帧服务已监听 {sock_path} "
        f"(pid={os.getpid()} interval={producer.interval}s ring={producer.max_frames})",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        try:
            sock_path.unlink()
        except OSError:
            pass
        producer.stop()
    _LOG.info("取帧服务已停止")
    return 0


# =============================================================================
# 模块级入口：进程内单例 + 取帧服务客户端（消费者只认这几个函数）
#
# 分派规则（顺序固定，实现集中在 _call 一处）：
#   1. 本进程有在跑的生产者（服务进程自己 / 独立 CLI / 自拉兜底）→ 本地帧环；
#   2. 否则取帧服务可达 → RPC（服务由 start_agent 作组件子进程拉起）；
#   3. 否则 → 本地单例的空帧环，取帧返回 None —— 与「没有生产者」逐字同语义。
# 于是**调用点不必知道帧在哪个进程里**，也不必自己起生产者。
#
# 单例是**惰性**的：import 本模块不再建目录（客户端进程变多了，那个副作用会成片出现）。
# =============================================================================
_DEFAULT_PRODUCER: Optional[LatestFrameProducer] = None
_DEFAULT_LOCK = threading.Lock()
_SERVICE_CLIENT: Optional[FrameServiceClient] = None
_SERVICE_LOCK = threading.Lock()
_service_warn_at = 0.0


def _default() -> LatestFrameProducer:
    """取进程内默认生产者（惰性构造；不启动抓拍）。"""
    global _DEFAULT_PRODUCER
    if _DEFAULT_PRODUCER is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_PRODUCER is None:
                _DEFAULT_PRODUCER = LatestFrameProducer()
    return _DEFAULT_PRODUCER


def _adopt_default(producer: LatestFrameProducer) -> None:
    """把一个生产者认作本进程的默认实例（`--serve` 用）。

    服务进程里它既是「被服务的那个帧环」也是「本进程的帧环」，于是本进程内的取帧调用
    走本地而不绕一圈 socket（_local_running() 认的就是这个字段）。
    """
    global _DEFAULT_PRODUCER
    _DEFAULT_PRODUCER = producer


def _local_running() -> Optional[LatestFrameProducer]:
    """本进程内正在抓拍的生产者；没有则 None（**只认 running**，空壳单例不算）。"""
    producer = _DEFAULT_PRODUCER
    if producer is not None and producer.running:
        return producer
    return None


def _service_client(
    *, attach_timeout: Optional[float] = None
) -> Optional[FrameServiceClient]:
    """取帧服务的进程内客户端（长连接）；不可达返回 None。

    attach_timeout 非空时在这个窗口内重试（只给启动期用，见 attach()）；缺省只试一次。
    作废的连接（closed）在这里被清掉，下一次调用重连 —— 服务重启最多丢一次调用。
    """
    global _SERVICE_CLIENT
    with _SERVICE_LOCK:
        client = _SERVICE_CLIENT
        if client is not None and client.closed:
            client = _SERVICE_CLIENT = None
        if client is not None:
            return client
        client = FrameServiceClient.try_connect(timeout=attach_timeout)
        _SERVICE_CLIENT = client
        return client


def attach(timeout: float = FRAME_PRODUCER_ATTACH_TIMEOUT) -> Optional[FrameServiceClient]:
    """等取帧服务可达（最多 timeout 秒），返回客户端句柄；等不到返回 None。

    **只给启动期用**：服务是被刚拉起的子进程，socket 可能还没 bind（见
    start_agent._start_frame_service）。稳态取帧不走它 —— 模块级取帧函数只试连一次，
    绝不为一个不存在的服务等待。
    """
    return _service_client(attach_timeout=timeout)


def _service_status(*, attach_timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """读取取帧服务状态；不可达返回 None。

    传输断了会重连一次再试（服务刚重启是最常见的「连接作废但服务还在」情形）。
    """
    for attempt in (2, 1):
        client = _service_client(attach_timeout=attach_timeout if attempt == 2 else None)
        if client is None:
            return None
        try:
            return client.status()
        except FrameServiceError as exc:
            if attempt == 1:
                _warn_service_down("status", exc)
    return None


def _warn_service_down(op: str, exc: Exception) -> None:
    """「取帧服务调用失败」的告警节流（这几个函数历来不抛异常，失败只记一条日志）。"""
    global _service_warn_at
    now = time.time()
    if now - _service_warn_at >= _SERVICE_WARN_INTERVAL:
        _service_warn_at = now
        _LOG.warning(
            "取帧服务调用 %s 失败（本次按「无帧」处理，下次调用会重连）: %s", op, exc
        )


def _invoke(target: Any, op: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
    """在句柄上取一次读接口：方法就调用，属性（latest_frame_id）就取值。"""
    attr = getattr(target, op)
    return attr(*args, **kwargs) if callable(attr) else attr


def _call(op: str, *args: Any, empty: Any = None, **kwargs: Any) -> Any:
    """按三条分派规则调一次取帧接口。

    empty 是该接口「无帧」时的取值（None 或 []）：跨进程调用**失败不抛给消费者** ——
    这几个函数的契约历来是「没有帧就返回 None/[]」，抛异常会打死观测器那条循环。
    失败只节流记一条日志，由调用方按「无帧」处置。

    **长活消费者一律走这里，不要自己存 start()/attach() 返回的句柄**：一次失败就作废的
    是那个客户端**对象**（_close() 只置 self._sock = None，对象自己不会重连），重连只发生
    在本函数的 _service_client() 里。存句柄 = 一次抖动之后永远「连接已关闭」。
    """
    producer = _local_running()
    if producer is not None:
        return _invoke(producer, op, args, kwargs)
    client = _service_client()
    if client is not None:
        try:
            return _invoke(client, op, args, kwargs)
        except FrameServiceError as exc:
            _warn_service_down(op, exc)
            return empty
    return _invoke(_default(), op, args, kwargs)


def start() -> Optional[Union[LatestFrameProducer, FrameServiceClient]]:
    """保证本进程能取到帧，返回取帧句柄；实在拿不到返回 None。

    顺序：本进程已在抓拍 → 它自己；否则取帧服务可达 → **客户端句柄**；再否则自拉兜底
    （独立 CLI、以及 `--no-frame-producer` 的部署里消费者各自抓拍）；跨进程锁被别人
    持有时才 None（帧在别人的内存里，本进程拿不到）。

    返回的两种句柄读接口同名（latest_frame_id / wait_for_first_frame /
    wait_for_new_frame / get_stacked_frame / stop …），调用方不必区分。
    """
    producer = _local_running()
    if producer is not None:
        return producer
    client = _service_client()
    if client is not None:
        return client
    producer = _default()
    if producer.start():
        return producer
    return None


def stop() -> None:
    """停止本进程内的默认生产者，并关掉取帧服务连接（都不影响别的进程）。"""
    if _DEFAULT_PRODUCER is not None:
        _DEFAULT_PRODUCER.stop()
    client = _SERVICE_CLIENT
    if client is not None:
        client.stop()


def describe_source() -> FrameSourceState:
    """本进程当前从哪儿取帧（不抛异常；给报错文案与自检用）。

    backend 三种取值：
      local-owner         本进程自己在抓拍（取帧服务进程 / 独立 CLI / 自拉兜底）；
      service             取帧服务可达（帧在服务进程的内存里）；
      service-unreachable 连不上服务：它没在跑，或本进程拿不到那条 socket。
    不细分「服务在跑但锁被别人占了」：消费者能采取的行动与 unreachable 相同，而持有者
    pid 由自拉兜底那条路的日志给出（见 _acquire_process_lock）。
    """
    producer = _local_running()
    if producer is not None:
        return FrameSourceState(
            "local-owner",
            f"本进程在抓拍（环内 {producer.ring_frames} 帧）",
            producer.latest_frame_id,
        )
    status = _service_status()
    if status is not None:
        return FrameSourceState(
            "service",
            f"取帧服务 {FRAME_PRODUCER_SOCKET} (pid={status.get('pid')})",
            int(status.get("latest_frame_id") or 0),
        )
    return FrameSourceState(
        "service-unreachable",
        f"连不上取帧服务 {FRAME_PRODUCER_SOCKET}",
        None,
    )


def get_default() -> LatestFrameProducer:
    """取进程内默认生产者实例，**不启动抓拍**（与 start() 的区别）。

    给「只想拿到管理器对象、由别人负责启动」的场景用（例如 task_loop 需要把生产者对象
    挂到自己身上，而不是由它来决定起不起抓拍）。未 start 过时拿到的是空帧环，取帧返回
    None，不会报错。

    只返回**本地**实例，不给取帧服务的客户端句柄（那个要 start()）—— 客户端不是
    LatestFrameProducer，挂不上「本地生产者」那套字段。
    """
    return _default()


def get_latest_frame(
    fmt: FrameFormat = "jpeg",
    resize: Optional[Sequence[int]] = None,
    *,
    save: bool = False,
    suffix: Optional[str] = None,
) -> Optional[FrameData]:
    """取环内最新帧（jpeg bytes 或 base64 str）；resize=(宽, 高) 时按该尺寸缩放。

    save=True 时把返回的这份落盘到 RUNTIME_PHOTOS_DIR 留档（默认不落盘）。
    **帧在别的进程时自动走取帧服务**（见本节的模块级入口说明）。
    """
    return _call(
        "get_latest_frame", fmt, resize=resize, save=save, suffix=suffix, empty=None
    )


def get_frames(
    count: Optional[int] = None,
    fmt: FrameFormat = "jpeg",
    resize: Optional[Sequence[int]] = None,
    *,
    save: bool = False,
    suffix: Optional[str] = None,
) -> List[FrameData]:
    """取最近 count 帧（默认全部环内帧），旧→新；resize 语义同 get_latest_frame。"""
    return _call(
        "get_frames",
        count=count,
        fmt=fmt,
        resize=resize,
        save=save,
        suffix=suffix,
        empty=[],
    )


def get_frames_since(
    after_id: int,
    fmt: FrameFormat = "jpeg",
    resize: Optional[Sequence[int]] = None,
    *,
    save: bool = False,
    suffix: Optional[str] = None,
) -> List[FrameData]:
    """取环内 id > after_id 的帧（旧→新）；resize 语义同 get_latest_frame。"""
    return _call(
        "get_frames_since",
        after_id,
        fmt,
        resize=resize,
        save=save,
        suffix=suffix,
        empty=[],
    )


def latest_frame_id() -> int:
    """环内最新帧号；空环或取不到返回 0（跨进程时经取帧服务查）。"""
    return int(_call("latest_frame_id", empty=0) or 0)


def wait_for_first_frame(timeout: Optional[float] = None) -> bool:
    """等待环内出现首帧；超时或取不到返回 False（跨进程时经取帧服务等）。"""
    return bool(_call("wait_for_first_frame", timeout=timeout, empty=False))


def wait_for_new_frame(after_id: int, timeout: Optional[float] = None) -> Optional[int]:
    """等待出现新帧（id > after_id）；超时返回 None。"""
    return _call("wait_for_new_frame", after_id, timeout, empty=None)


def get_stacked_frame(
    fmt: FrameFormat = "jpeg",
    quality: int = 85,
    resize: Optional[Sequence[int]] = None,
    *,
    count: Optional[int] = None,
    save: bool = False,
    suffix: Optional[str] = None,
) -> Optional[FrameData]:
    """取环内最新 count 帧，自上而下拼成一张图（左上角标 `frame 1..count`）。

    count=None 时按 configs.VISION_STACK_FRAMES（=3）。返回 jpeg bytes
    （fmt='base64' 时返回 str）；帧环为空时返回 None。
    resize=(宽, 高) 时逐帧缩到该尺寸再拼（见类方法说明）。
    save=True 时把这张图落盘到 RUNTIME_PHOTOS_DIR 留档（默认不落盘）。
    """
    return _call(
        "get_stacked_frame",
        fmt=fmt,
        quality=quality,
        resize=resize,
        count=count,
        save=save,
        suffix=suffix,
        empty=None,
    )


def compose_grid(
    images: Sequence[bytes],
    *,
    cols: int = 2,
    quality: int = 85,
    max_width: Optional[int] = 2560,
    fmt: FrameFormat = "jpeg",
    save: bool = False,
    suffix: Optional[str] = None,
) -> Optional[FrameData]:
    """把多张 jpeg 排成 cols 列网格图（环绕搜索的四宫格用）；见类方法说明。"""
    return _call(
        "compose_grid",
        images,
        cols=cols,
        quality=quality,
        max_width=max_width,
        fmt=fmt,
        save=save,
        suffix=suffix,
        empty=None,
    )


def iter_latest_frames(
    interval_seconds: float,
    fmt: FrameFormat = "jpeg",
    *,
    resize: Optional[Sequence[int]] = None,
) -> Generator[FrameData, None, None]:
    """按周期 yield 最新帧（生成器形式）；resize 语义同 get_latest_frame。"""
    producer = _local_running()
    if producer is not None:
        yield from producer.iter_latest_frames(interval_seconds, fmt, resize=resize)
        return
    client = _service_client()
    if client is not None:
        try:
            yield from client.iter_latest_frames(interval_seconds, fmt, resize=resize)
        except FrameServiceError as exc:
            _warn_service_down("iter_latest_frames", exc)
        return
    yield from _default().iter_latest_frames(interval_seconds, fmt, resize=resize)


atexit.register(stop)


# =============================================================================
# 自检 / 演示 CLI
# =============================================================================
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="图像帧生产者自检/演示",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  # 用假拍摄脚本做无相机自检（假脚本把一张 JPEG 拷到 -o 指向路径）\n"
            "  python frame_producer.py --capture-script /tmp/fake_capture.sh "
            "--interval 0.5 --get jpeg\n"
            "  # 取一帧缩到 1280x720（斗牛用的尺寸）\n"
            "  python frame_producer.py --get jpeg --resize 1280 720 > /tmp/f.jpg\n"
            "  # 攒帧拼一张纵向拼接图，落盘到受管目录（唯一会写文件的自检）\n"
            "  python frame_producer.py --capture-script /tmp/fake_capture.sh --stacked\n"
            "  # 换一条共享内存（主相机）自检\n"
            "  python frame_producer.py --socket /tmp/foo_fhd --get jpeg > /tmp/f.jpg\n"
            "  # 真实周期抓拍演示（要求 video_gst 节点正在推流）\n"
            "  python frame_producer.py --interval 3\n"
            "  # 当取帧服务跑（start_agent 就这么拉起它；别的进程经 socket 找它取帧）\n"
            "  python frame_producer.py --serve\n"
            "  # 查现在从哪儿取帧、服务端状态（不起生产者）\n"
            "  python frame_producer.py --status\n"
        ),
    )
    p.add_argument("--capture-script", default=str(CAMERA_SHM_CAPTURE_SCRIPT),
                   help=f"共享内存取帧脚本（默认 {CAMERA_SHM_CAPTURE_SCRIPT}）")
    p.add_argument("--socket", default=str(CAMERA_SHM_SOCKET),
                   help=f"相机共享内存 socket（默认 {CAMERA_SHM_SOCKET}；主相机是 /tmp/foo_fhd）")
    p.add_argument("--interval", type=float, default=None,
                   help=f"周期抓拍间隔秒（默认 {FRAME_PRODUCER_INTERVAL}）。"
                        "这是**固定值**：生产者是常驻的取帧服务，抓拍节奏不受运行时"
                        "覆盖层影响（覆盖层只管观测器，见 Orchestrator/vision_tunables.py）")
    p.add_argument("--timeout", type=float, default=None,
                   help=f"单次拍照超时秒（默认 {FRAME_PRODUCER_CAPTURE_TIMEOUT}）")
    p.add_argument("--max-frames", type=int, default=None,
                   help=f"帧环保留最近帧数（默认 {FRAME_PRODUCER_MAX_FRAMES}）")
    p.add_argument("--run-seconds", type=float, default=None,
                   help="演示模式运行秒数（缺省持续到 Ctrl+C）")
    p.add_argument("--resize", nargs=2, type=int, metavar=("WIDTH", "HEIGHT"),
                   default=None,
                   help="把取到的帧缩放到该尺寸（默认不缩放，保持抓拍原分辨率 1920x1080）")
    p.add_argument("--get", choices=["jpeg", "base64"], default=None,
                   help="起生产者，抓一帧按该格式输出后退出（自检用）")
    p.add_argument("--get-timeout", type=float, default=10.0,
                   help="--get 等首帧超时秒（默认 10）")
    p.add_argument("--stacked", action="store_true",
                   help="起生产者，攒够若干帧后调 get_stacked_frame 把拼接图保存到 "
                        f"{RUNTIME_PHOTOS_DIR} 并退出（路径由本模块决定，不接受外部指定）")
    p.add_argument("--stacked-min-frames", type=int, default=None,
                   help="--stacked 先攒够几帧才开拼（默认 3，且不超过 --max-frames）。"
                        "注意这是「等几帧」，**不是**「拼几帧」—— 拼几帧取自运行时覆盖层"
                        "runtime_files/vision_tunables.yaml 的 stack_frames")
    p.add_argument("--stacked-timeout", type=float, default=None,
                   help="--stacked 等帧超时秒（默认按抓拍间隔估算，至少 15s）")
    p.add_argument("--stacked-quality", type=int, default=85,
                   help="--stacked 输出 JPEG 质量（默认 85）")
    p.add_argument("--serve", action="store_true",
                   help="前台长驻当**取帧服务**：把帧环以 unix socket "
                        f"({FRAME_PRODUCER_SOCKET}) 暴露给其它进程（start_agent 这样拉起它）。"
                        "与 --get / --stacked / --run-seconds 互斥")
    p.add_argument("--status", action="store_true",
                   help="报告本进程的取帧来源（本地抓拍 / 取帧服务 / 连不上）与"
                        "服务端状态后退出；不起生产者，可随时运行")
    return p


def _cmd_status() -> int:
    """`--status`：报告取帧来源与（服务可达时）服务端状态。

    返回码：有帧源（本地在抓拍，或取帧服务可达）0，否则 1 —— 便于脚本判「帧路径通不通」。
    """
    state = describe_source()
    print(f"[frame_producer] 取帧来源: {state.backend} —— {state.detail}")
    if state.backend == "service":
        status = _service_status()
        if status is not None:
            print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 0 if state.backend == "local-owner" else 1


def _demo_stacked(
    producer: LatestFrameProducer,
    *,
    min_frames: Optional[int],
    timeout: Optional[float],
    quality: int,
    resize: Optional[Sequence[int]] = None,
) -> int:
    """自检 get_stacked_frame：攒够帧 → 拼图 → 落盘到受管目录，便于肉眼看效果。

    等帧用的是公开接口（get_frames 的环内长度 + latest_frame_id），不碰内部字段。
    落盘走本模块的 save_photo —— 自检也不破「图像路径只由本模块决定」这条规矩。

    **拼几帧**取自运行时可调覆盖层（Orchestrator.vision_tunables），与观测器走同一条路 ——
    这样「用 CLI 改 stack_frames 再跑一次自检」就能肉眼验证改了到底生不生效
    （成图高度 = 帧数 × 1080）。注意 `--stacked-min-frames` 是「先攒够几帧再拼」，
    与拼几帧是两件事。
    """
    want = 3 if min_frames is None else int(min_frames)
    want = max(1, min(want, producer.max_frames))
    interval = producer.interval
    # 传**原值**给 get_stacked_frame（与观测器同一条路）：夹取与那条告警都在那里发生，
    # 自检才验得到「手改了一个越界值」这一路。这里只借同一个 helper 算出标注用的帧数。
    stack_raw = vision_tunables.current().stack_frames
    stack_count = _resolve_stack_count(stack_raw, producer.max_frames)
    if timeout is None:
        timeout = max(15.0, want * (interval + producer.capture_timeout) + 5.0)

    print(
        f"[frame_producer] 攒帧中：>= {want} 帧 / 最多等 {timeout:.0f}s "
        f"(interval={interval}s ring={producer.max_frames} 拼接={stack_count} 帧)"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ring_n = len(producer.get_frames())
        if ring_n >= want:
            break
        time.sleep(0.1)
    else:
        print(
            f"[frame_producer] 超时({timeout:.0f}s)仅攒到 "
            f"{len(producer.get_frames())} 帧，仍尝试拼接",
            file=sys.stderr,
        )

    ring_n = len(producer.get_frames())
    latest_id = producer.latest_frame_id
    got = producer.get_stacked_frame(
        fmt="jpeg", quality=quality, resize=resize, count=stack_raw
    )
    if got is None:
        producer.stop()
        print("[frame_producer] get_stacked_frame 返回 None（无可用帧）", file=sys.stderr)
        return 1
    assert isinstance(got, bytes)

    saved = save_photo(got, suffix="stacked")
    producer.stop()
    if saved is None:
        print("[frame_producer] 拼接图落盘失败（见上方告警）", file=sys.stderr)
        return 1
    stitched = min(stack_count, ring_n)
    print(
        f"[frame_producer] 已保存拼接图: {saved.resolve()} "
        f"({len(got) / 1024:.1f} KiB, quality={quality})\n"
        f"                 环内 {ring_n} 帧, latest_id={latest_id} —— 子图自上而下 "
        f"frame 1(最旧) … frame {stitched}(最新)"
    )
    return 0


def _main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_parser().parse_args()

    if args.status:
        return _cmd_status()
    if args.serve:
        for flag, given in (
            ("--get", args.get),
            ("--stacked", args.stacked),
            ("--run-seconds", args.run_seconds),
        ):
            if given:
                print(f"[frame_producer] --serve 与 {flag} 不能同时用", file=sys.stderr)
                return 2
        return _serve(args)

    producer = LatestFrameProducer(
        capture_script=args.capture_script,
        socket_path=args.socket,
        interval=args.interval,
        capture_timeout=args.timeout,
        max_frames=args.max_frames,
    )
    if not producer.start():
        print(
            "[frame_producer] 另一进程已持有单实例锁，本进程不启动抓拍",
            file=sys.stderr,
        )
        return 1

    if args.get is not None:
        deadline = time.monotonic() + args.get_timeout
        while time.monotonic() < deadline:
            value = producer.get_latest_frame(fmt=args.get, resize=args.resize)
            if value is not None:
                if args.get == "jpeg":
                    assert isinstance(value, bytes)
                    sys.stdout.buffer.write(value)
                    sys.stdout.buffer.flush()
                else:
                    print(value)
                producer.stop()
                return 0
            time.sleep(0.1)
        producer.stop()
        print(f"[frame_producer] 超时({args.get_timeout:.0f}s)未取到帧", file=sys.stderr)
        return 1

    if args.stacked:
        return _demo_stacked(
            producer,
            min_frames=args.stacked_min_frames,
            timeout=args.stacked_timeout,
            quality=args.stacked_quality,
            resize=args.resize,
        )

    print(
        f"[frame_producer] 开始周期抓拍 interval={producer.interval}s "
        f"ring={producer.max_frames} capture={producer.capture_script}"
    )
    t_start = time.monotonic()
    try:
        while True:
            if args.run_seconds and time.monotonic() - t_start >= args.run_seconds:
                break
            print(
                f"[heartbeat] latest_id={producer.latest_frame_id} "
                f"ring_frames={len(producer.get_frames())} "
                f"latest_bytes={len(producer.get_latest_frame('jpeg') or b'')}"
            )
            time.sleep(max(0.1, 2.0))
    except KeyboardInterrupt:
        print("\n[frame_producer] 手动停止")
    finally:
        producer.stop()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
