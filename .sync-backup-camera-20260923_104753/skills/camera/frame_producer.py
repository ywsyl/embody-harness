#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
robot-dog-camera 驱动的「图像帧生产者」—— 单一生产者 + 多消费者共存

为什么存在
----------
skills/camera/latest_frame_stream.py 是纯读取端：单条 GStreamer 管道读共享内存
/tmp/neck_jpeg 的 JPEG 帧流，依赖上游持续写该 socket 的生产者。实测该 socket
被 video_gst_neck 监听但不吐帧（connect_ex 成功、gst-launch 拉不出任何帧），
因此 latest_frame_stream 当前拿不到可用帧源。

本模块改用 **robot-dog-camera 拍摄脚本**（capture.sh；存在 capture_direct.py 时
走设备直拍）作为真实帧源：后台唯一线程按固定间隔周期抓拍一张 JPEG，压入进程内
「滚动帧环」，供 N 个消费者并发读取。不读 /tmp/neck_jpeg、不依赖 shm 吐帧。

核心：1 个生产者 + N 个消费者（跨进程唯一）
- 任意时刻全系统只有一条抓拍线程：LatestFrameProducer.start() 先以 flock 独占
  FRAME_PRODUCER_LOCK_FILE（configs.paths）——另一进程（如同时跑独立 observer /
  另一个 receiver）抢占失败即拒绝启动。进程内再靠模块级单例 _DEFAULT_PRODUCER
  + start() 幂等保证不重复；任何消费者不要各自 new/start 生产者。
- 滚动帧环：保留最近 FRAME_PRODUCER_MAX_FRAMES 帧 (frame_id, wall_time, jpeg_bytes)，短暂滞后的消费者
  仍能读到 1–2 帧前的画面；落后超过环深自然快进到最新帧（丢帧但不阻塞）。
- 各消费者并发读同一共享缓存、各记各的进度；get_latest_frame / get_frames /
  get_frames_since / wait_for_new_frame / iter_latest_frames / get_stacked_frame
  均线程安全。
- 分辨率：抓拍原帧 1920×1080（定在 skills/camera/scripts/capture_direct.py 的
  GStreamer caps，本模块不传 --resize）。读接口一律带 resize=(宽, 高) 参数：
  默认 None＝原样透传、不重编码、不需要 Pillow；只有显式传参的消费者（如斗牛缩到
  1280×720）才在**读取侧**缩放——帧环里始终是原帧，各消费者各要各的尺寸互不影响，
  也不会为了某个消费者去改相机设置。

⚠️ 副作用：robot-dog-camera capture.sh → capture_direct 每次抓拍会 fuser -k 杀掉
占用相机的 video_gst 节点，且按 [2026-09-02 停用] 捕获后不再自动恢复（其未配
respawn）。周期抓拍会反复触发该抢占——仅当机器人允许抢占相机、且需要持续帧源时
才调用 start()。模块被 import 不会启动抓拍（单例是惰性的）。

图像落盘：全仓唯一写手 + 唯一 save 开关
---------------------------------------
本模块是**全仓唯一**把图像写成文件的地方（`save_photo` / `photo_path`），落盘目录
只有 `RUNTIME_PHOTOS_DIR`（configs.paths，= runtime_files/photos/），命名
`<YYYYmmdd_HHMMSS_ffffff>[_<suffix>].jpg`。其它模块（视觉技能、观测器、工具层）
**不再自己拼路径**，只传 `save: bool = False`：

- `save=False`（默认）——**完全不落盘**。要交给大模型的图走内存 base64 直传
  （vision_local.request_vqa 的 image_base64），连临时文件都不产生。
- `save=True`，或 `save_photo()` 直接调用 —— 写受管目录，并按
  `configs.FRAME_PRODUCER_PHOTOS_KEEP` 滚动删旧（只删本模块命名格式的文件，
  别人放进来的东西不动）；写失败只告警、返回 None，不影响取帧本身。

读取侧不缩放时返回的是帧环里的原字节，落盘的是**要返回的那一份**（含 resize 之后
的），所以 `save=True` 存下来的就是消费者真正看到的那张。

抓拍临时帧：单帧仍是「写进 self.tmp_dir → 读完即 unlink」，但 tmp_dir 不再是
`tempfile.mkdtemp("/tmp/frame_producer_*")`，而是受管目录下的
`runtime_files/photos/scratch/pid_<pid>_<ns>/`——`stop()` 删自己的、`start()` 顺手
清理 pid 已不存在的残留（跨进程被 kill -9 也不会攒垃圾）。

依赖：bash（需 source ~/.bashrc）、robot-dog-camera scripts/capture.sh
（capture_direct 依赖 gst-launch-1.0 / fuser）。机器人需已开机、相机可抢占。
"""

from __future__ import annotations

import argparse
import atexit
import base64
import fcntl
import io
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Generator, List, Literal, Optional, Sequence, Tuple, Union

# 确保仓库根在 sys.path（本文件 skills/camera/ 上两级 = 仓库根），以便 import configs
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.paths import (  # noqa: E402
    CAMERA_CAPTURE_SCRIPT,
    FRAME_PRODUCER_LOCK_FILE,
    LABEL_FONT_PATH,
    RUNTIME_PHOTOS_DIR,
)
from configs.configs import (  # noqa: E402
    FRAME_PRODUCER_INTERVAL,
    FRAME_PRODUCER_CAPTURE_TIMEOUT,
    FRAME_PRODUCER_MAX_FRAMES,
    FRAME_PRODUCER_PHOTOS_KEEP,
    VISION_STACK_FRAMES,
)
# 视觉观测参数的**运行时可调**覆盖层（只含「每轮拼接帧数」这一项）。
# 经 configs 这一层取，而不是 tools/：本模块是相机库，还被视觉技能当**子进程脚本**用
# （每次 vision 工具调用都起一个），不该把 tools/__init__ → tool_executor → Motion_Manager
# 那一串背进来。configs/ 没有 __init__.py、零导入副作用，是所有模块都已 import 的叶子层。
from configs import vision_tunables  # noqa: E402

FrameFormat = Literal["jpeg", "base64"]
FrameData = Union[bytes, str]

_LOG = logging.getLogger("frame_producer")


def _coalesce_jpeg(data: bytes) -> Optional[bytes]:
    """取 data 中最后一帧**完整** JPEG（最后一个 SOI..EOI 对），避免截断/拼接脏数据。

    EOI(0xFFD9) 必须一起校验：抓拍是「文件长到 >1000 字节就 terminate gst-launch」
    （capture_direct），文件尾巴上常留着**下一帧刚写了半截**的字节，而
    `gst-launch v4l2src ! image/jpeg ! filesink` 是连续追加、一个文件里可以有好几帧
    （实测 -t 1 拿到 1~3 帧、每帧 ~232KB）。只看到 SOI 就返回，等于把那半截当成一帧
    压进帧环，读的时候 Pillow 报 `OSError: image file is truncated (N bytes not
    processed)`，拼接时被当坏帧跳过。
    尾帧不完整就退而取它前面那一帧完整帧（同一次抓拍的上一帧），一帧完整都没有才返回 None。

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


# 缩放输出的 JPEG 质量：与 robot-dog-camera capture.py / capture_direct.py 的
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


# 标签字体：优先 DejaVuSans-Bold（与 robot-dog-camera/scripts/capture.py 一致），
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


def prune_photos(
    directory: Path, keep: int, *, dry_run: bool = False
) -> List[Path]:
    """滚动清理目录内本模块生成的图像，只保留最新 keep 张（字典序=时间序）。

    - 只删匹配 _PHOTO_NAME_RE 的文件：目录里其它人工放的图片一律不碰。
      「哪些文件算本模块的」这条判据只此一处——外部清理工具（Observer/cleanup.py）
      也走这个函数，别自己另写一份 glob。
    - keep 非正数 = 不清理（与 vision_observer._trim_observations 的约定一致）。
    - 返回**删掉的**路径（旧→新）；dry_run=True 时只算不删，返回「本来会删的」那些。
    """
    if keep <= 0:
        return []
    try:
        names = sorted(n for n in os.listdir(directory) if _PHOTO_NAME_RE.match(n))
    except OSError:
        return []
    doomed = [directory / n for n in names[: max(0, len(names) - int(keep))]]
    if dry_run:
        return doomed
    removed: List[Path] = []
    for path in doomed:
        try:
            os.unlink(path)
        except OSError:
            continue
        removed.append(path)
    return removed


def save_photo(data: FrameData, suffix: Optional[str] = None) -> Optional[Path]:
    """把一张图落盘到 RUNTIME_PHOTOS_DIR 并滚动清理，返回路径；失败返回 None。

    **这是全仓唯一的图像落盘函数**（frame_producer 独占管理写入路径）：
    传 jpeg bytes 或 base64 str 均可（后者先解码回字节）。落盘是旁路——供事后回看、
    复现大模型看到的内容或任务留档，写盘失败只告警不抛，绝不拖垮视觉主链路。
    保留张数见 configs.FRAME_PRODUCER_PHOTOS_KEEP，整个目录共用一份额度。
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
    prune_photos(RUNTIME_PHOTOS_DIR, FRAME_PRODUCER_PHOTOS_KEEP)
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
    """robot-dog-camera 周期性抓拍 → 滚动帧环缓存，供多消费者共享读取。

    帧元素统一为 (frame_id:int, wall_time:float, jpeg:bytes)，旧→新入环；
    deque(maxlen=max_frames) 自动滚出最旧帧。所有读接口在锁内拷贝引用、锁外转换。
    """

    def __init__(
        self,
        capture_script: str = str(CAMERA_CAPTURE_SCRIPT),
        interval: Optional[float] = None,
        capture_timeout: Optional[float] = None,
        max_frames: Optional[int] = None,
        tmp_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.capture_script = str(capture_script)
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
        self._owns_tmp_dir = tmp_dir is None
        if tmp_dir is None:
            self.tmp_dir = (
                RUNTIME_PHOTOS_DIR
                / "scratch"
                / f"{_SCRATCH_PREFIX}{os.getpid()}_{time.time_ns()}"
            )
            try:
                self.tmp_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _LOG.warning(
                    "受管临时帧目录不可用(%s)，回退系统临时目录: %r", self.tmp_dir, exc
                )
                self.tmp_dir = Path(tempfile.mkdtemp(prefix="frame_producer_"))
        else:
            self.tmp_dir = Path(tmp_dir)
            self.tmp_dir.mkdir(parents=True, exist_ok=True)

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
            "frame producer started(pid=%d, lock=%s): capture=%s interval=%.1fs "
            "timeout=%.1fs ring=%d",
            os.getpid(),
            FRAME_PRODUCER_LOCK_FILE,
            self.capture_script,
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
        """调 robot-dog-camera 抓一帧 JPEG 并压入帧环；失败保留旧缓存返回 False。"""
        out_path = self.tmp_dir / f"frame_{time.time_ns()}.jpg"
        if not self._run_capture(out_path):
            self._warn_throttled(
                "robot-dog-camera 抓拍失败，保留旧帧缓存（latest_id=%d）",
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
        """执行 robot-dog-camera 拍照；镜像 vision_dispatch 的成功姿势：
        bash -c "source ~/.bashrc && capture.sh -o <tmp> -q -t <timeout>"。
        """
        cmd = (
            "source ~/.bashrc >/dev/null 2>&1 && exec "
            f"{shlex.quote(self.capture_script)} -o {shlex.quote(str(out_path))} "
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
                "robot-dog-camera 抓拍超时(>%.0fs，脚本内 -t %.0fs 未兜住)",
                self.capture_timeout + 5.0,
                self.capture_timeout,
            )
            return False

        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            self._warn_throttled(
                "robot-dog-camera 返回失败: %s",
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
          configs.vision_tunables.current() 再显式传进来 —— 本模块是帧环，只管机制
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
# 模块级单例 + 外部函数（唯一生产者入口；import 本模块不会启动抓拍）。
# 进程内：_DEFAULT_PRODUCER 单例 + start() 幂等。
# 跨进程：LatestFrameProducer.start() 抢 FRAME_PRODUCER_LOCK_FILE 独占锁，
#        抢不到（receiver/独立 observer 已在别的进程跑）→ 模块 start() 返回 None。
# =============================================================================
_DEFAULT_PRODUCER = LatestFrameProducer()


def start() -> Optional[LatestFrameProducer]:
    """启动默认生产者并返回管理器；另一进程已持有单实例锁时返回 None。"""
    if _DEFAULT_PRODUCER.start():
        return _DEFAULT_PRODUCER
    return None


def stop() -> None:
    """停止默认生产者。"""
    _DEFAULT_PRODUCER.stop()


def get_default() -> LatestFrameProducer:
    """取进程内默认生产者实例，**不启动抓拍**（与 start() 的区别）。

    给「只想拿到管理器对象、由别人负责启动」的场景用（例如 task_loop 需要把
    生产者对象挂到自己身上，而不是由它来决定起不起抓拍）。未 start 过时拿到的是
    空帧环，取帧返回 None，不会报错。
    """
    return _DEFAULT_PRODUCER


def get_latest_frame(
    fmt: FrameFormat = "jpeg",
    resize: Optional[Sequence[int]] = None,
    *,
    save: bool = False,
    suffix: Optional[str] = None,
) -> Optional[FrameData]:
    """取环内最新帧（jpeg bytes 或 base64 str）；resize=(宽, 高) 时按该尺寸缩放。

    save=True 时把返回的这份落盘到 RUNTIME_PHOTOS_DIR 留档（默认不落盘）。
    """
    return _DEFAULT_PRODUCER.get_latest_frame(fmt, resize=resize, save=save, suffix=suffix)


def get_frames(
    count: Optional[int] = None,
    fmt: FrameFormat = "jpeg",
    resize: Optional[Sequence[int]] = None,
    *,
    save: bool = False,
    suffix: Optional[str] = None,
) -> List[FrameData]:
    """取最近 count 帧（默认全部环内帧），旧→新；resize 语义同 get_latest_frame。"""
    return _DEFAULT_PRODUCER.get_frames(
        count=count, fmt=fmt, resize=resize, save=save, suffix=suffix
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
    return _DEFAULT_PRODUCER.get_frames_since(
        after_id, fmt, resize=resize, save=save, suffix=suffix
    )


def wait_for_new_frame(after_id: int, timeout: Optional[float] = None) -> Optional[int]:
    """等待默认生产者出现新帧（id > after_id）。"""
    return _DEFAULT_PRODUCER.wait_for_new_frame(after_id, timeout)


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
    return _DEFAULT_PRODUCER.get_stacked_frame(
        fmt=fmt, quality=quality, resize=resize, count=count, save=save, suffix=suffix
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
    return _DEFAULT_PRODUCER.compose_grid(
        images,
        cols=cols,
        quality=quality,
        max_width=max_width,
        fmt=fmt,
        save=save,
        suffix=suffix,
    )


def iter_latest_frames(
    interval_seconds: float,
    fmt: FrameFormat = "jpeg",
    *,
    resize: Optional[Sequence[int]] = None,
) -> Generator[FrameData, None, None]:
    """按周期 yield 最新帧（生成器形式）；resize 语义同 get_latest_frame。"""
    yield from _DEFAULT_PRODUCER.iter_latest_frames(interval_seconds, fmt, resize=resize)


atexit.register(stop)


# =============================================================================
# 自检 / 演示 CLI
# =============================================================================
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="robot-dog-camera 图像帧生产者自检/演示",
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
            "  # 真实周期抓拍演示\n"
            "  python frame_producer.py --interval 3\n"
        ),
    )
    p.add_argument("--capture-script", default=str(CAMERA_CAPTURE_SCRIPT),
                   help=f"robot-dog-camera 拍摄脚本（默认 {CAMERA_CAPTURE_SCRIPT}）")
    p.add_argument("--interval", type=float, default=None,
                   help=f"周期抓拍间隔秒（默认 {FRAME_PRODUCER_INTERVAL}）。"
                        "这是**固定值**：生产者是常驻的取帧服务，抓拍节奏不受运行时"
                        "覆盖层影响（覆盖层只管观测器，见 configs/vision_tunables.py）")
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
    return p


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

    **拼几帧**取自运行时可调覆盖层（configs.vision_tunables），与观测器走同一条路 ——
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
    producer = LatestFrameProducer(
        capture_script=args.capture_script,
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
