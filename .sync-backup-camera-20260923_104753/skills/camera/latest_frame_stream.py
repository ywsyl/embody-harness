#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【遗留副本 · 未启用】本模块已被 frame_producer 取代，请勿据此判断取帧行为
=========================================================================
现状（2026-09-20 审计确认）：
  - 全仓无 import 方。唯一提到它的 configs/paths.py 的 LATEST_FRAME_STREAM_MODULE
    也只有定义、零消费者；Observer/vision_observer.py 里那句 "与旧
    latest_frame_stream 同名" 就是在说它已被换掉。
  - 活的那份是 skills/camera/frame_producer.py（同为「单一生产者 + 多消费者」，
    但改走 capture.sh 抓拍，不依赖 shm 吐帧）。
  - 之所以被换掉：它默认读的 /tmp/neck_jpeg 实测**不吐帧**——该 socket 被
    video_gst_neck 监听、connect_ex 成功，但 gst-launch 拉不出任何帧，因此本模块
    当前拿不到可用帧源。这一点 frame_producer.py 的模块 docstring 有完整记录。

故此处保留原样的 "/tmp/neck_jpeg" 字面量，configs/paths.py **也没有**为它登记常量：
给死代码登记常量，等于往唯一事实源里掺零消费者的条目，正是本轮要治的病。若将来
真要复活本模块，请连同 socket 路径一起登记到 configs/paths.py 再引用，不要再写字面量。

注意：以下原始 docstring 描述的是**当初的**设计，不代表现在跑的行为。
=========================================================================

Latest-frame stream reader based on GStreamer shared-memory JPEG stream.

Default source follows the supplied C++ JpegFactor implementation:
    shmsrc socket-path=/tmp/neck_jpeg ! jpegparse ! appsink

Design
------
1. A single GStreamer pipeline owns the stream connection.
2. The callback samples the incoming stream at ``fps`` (default: 3 FPS).
3. Only the latest complete JPEG frame is kept in memory.
4. Two public read APIs are provided:
   - get_latest_frame(fmt="jpeg")
       Return the latest frame immediately.
   - iter_latest_frames(interval_seconds, fmt="jpeg")
       Yield the latest frame every x seconds.

Return formats
--------------
- fmt="jpeg"   -> bytes containing a complete JPEG image.
- fmt="base64" -> ASCII str containing Base64-encoded JPEG data.

No image is written to disk.
"""

from __future__ import annotations

import atexit
import base64
import logging
import threading
import time
from typing import Generator, Literal, Optional, Union

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


FrameFormat = Literal["jpeg", "base64"]
FrameData = Union[bytes, str]

_LOG = logging.getLogger("latest_frame_stream")


class LatestFrameStream:
    """Keep the newest JPEG frame from one GStreamer stream in memory."""

    def __init__(
        self,
        socket_path: str = "/tmp/neck_jpeg",
        fps: float = 3.0,
        pipeline: Optional[str] = None,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be > 0")

        self.socket_path = socket_path
        self.fps = float(fps)
        self.sample_interval = 1.0 / self.fps

        # Keep JPEG encoded in the pipeline. Compared with the supplied C++
        # implementation, this avoids decode -> raw bytes -> re-encode when
        # callers ultimately need JPEG/Base64 JPEG.
        self.pipeline_desc = pipeline or (
            f'shmsrc socket-path="{self.socket_path}" is-live=true do-timestamp=true '
            "! jpegparse "
            "! appsink name=msink emit-signals=true sync=false max-buffers=1 drop=true"
        )

        self._lock = threading.Lock()
        self._frame_ready = threading.Event()
        self._latest_jpeg: Optional[bytes] = None
        self._latest_frame_time: Optional[float] = None
        self._latest_frame_id = 0
        self._last_sample_monotonic = 0.0

        self._pipeline: Optional[Gst.Element] = None
        self._appsink: Optional[Gst.Element] = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def latest_frame_id(self) -> int:
        with self._lock:
            return self._latest_frame_id

    @property
    def latest_frame_time(self) -> Optional[float]:
        """Unix timestamp of the newest cached frame."""
        with self._lock:
            return self._latest_frame_time

    def start(self) -> None:
        """Start the single GStreamer pipeline. Safe to call repeatedly."""
        if self._running:
            return

        Gst.init(None)

        try:
            pipeline = Gst.parse_launch(self.pipeline_desc)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to create GStreamer pipeline: {self.pipeline_desc}"
            ) from exc

        appsink = pipeline.get_by_name("msink")
        if appsink is None:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("appsink 'msink' not found in GStreamer pipeline")

        appsink.connect("new-sample", self._on_new_sample)
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("Unable to set GStreamer pipeline to PLAYING")

        self._pipeline = pipeline
        self._appsink = appsink
        self._running = True
        _LOG.info(
            "Frame stream started: socket=%s, sample_fps=%.3f",
            self.socket_path,
            self.fps,
        )

    def stop(self) -> None:
        """Stop the GStreamer pipeline. Safe to call repeatedly."""
        if not self._running:
            return

        pipeline = self._pipeline
        self._running = False
        self._pipeline = None
        self._appsink = None

        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)

        _LOG.info("Frame stream stopped")

    def wait_for_first_frame(self, timeout: Optional[float] = None) -> bool:
        """Wait until at least one sampled frame has been cached."""
        return self._frame_ready.wait(timeout=timeout)

    def _on_new_sample(self, appsink: Gst.Element) -> Gst.FlowReturn:
        """GStreamer streaming-thread callback."""
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR

        # The source can run faster than 3 FPS. We still drain appsink, but only
        # promote at most `fps` frames per second into the shared latest-frame
        # cache. This keeps the consumer-side frame update frequency controlled
        # without opening another camera/stream connection.
        now_mono = time.monotonic()
        if now_mono - self._last_sample_monotonic < self.sample_interval:
            return Gst.FlowReturn.OK

        buffer = sample.get_buffer()
        if buffer is None:
            return Gst.FlowReturn.ERROR

        ok, map_info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR

        try:
            # bytes() performs a deep copy before GstBuffer is unmapped.
            jpeg_bytes = bytes(map_info.data)
        finally:
            buffer.unmap(map_info)

        if not jpeg_bytes:
            return Gst.FlowReturn.OK

        now_wall = time.time()
        with self._lock:
            self._latest_jpeg = jpeg_bytes
            self._latest_frame_time = now_wall
            self._latest_frame_id += 1
            self._last_sample_monotonic = now_mono

        self._frame_ready.set()
        return Gst.FlowReturn.OK

    @staticmethod
    def _convert(jpeg_bytes: bytes, fmt: FrameFormat) -> FrameData:
        fmt = fmt.lower()  # type: ignore[assignment]
        if fmt == "jpeg":
            return jpeg_bytes
        if fmt == "base64":
            return base64.b64encode(jpeg_bytes).decode("ascii")
        raise ValueError("fmt must be 'jpeg' or 'base64'")

    # External function 1 (instance form)
    def get_latest_frame(self, fmt: FrameFormat = "jpeg") -> Optional[FrameData]:
        """
        Return the latest cached frame immediately.

        Returns None before the first frame arrives. The lock is held only long
        enough to copy the Python bytes reference; Base64 conversion happens
        outside the lock.
        """
        with self._lock:
            jpeg_bytes = self._latest_jpeg

        if jpeg_bytes is None:
            return None
        return self._convert(jpeg_bytes, fmt)

    # External function 2 (instance form)
    def iter_latest_frames(
        self,
        interval_seconds: float,
        fmt: FrameFormat = "jpeg",
        *,
        wait_first_frame: bool = True,
    ) -> Generator[FrameData, None, None]:
        """
        Yield the newest cached frame every ``interval_seconds`` seconds.

        This does NOT create another capture pipeline. It only reads the shared
        latest-frame cache at the requested interval.

        Example:
            for image in stream.iter_latest_frames(10, "base64"):
                handle(image)
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")

        if wait_first_frame:
            self._frame_ready.wait()

        next_deadline = time.monotonic()
        while self._running:
            now = time.monotonic()
            if now < next_deadline:
                time.sleep(next_deadline - now)

            frame = self.get_latest_frame(fmt)
            if frame is not None:
                yield frame

            # Deadline-based scheduling avoids accumulating processing drift.
            next_deadline += interval_seconds
            now = time.monotonic()
            if next_deadline <= now:
                missed = int((now - next_deadline) // interval_seconds) + 1
                next_deadline += missed * interval_seconds

    def __enter__(self) -> "LatestFrameStream":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Module-level singleton and the two external functions requested by the user.
# Importing this module does NOT open a second pipeline if start() is repeated.
# ---------------------------------------------------------------------------
_DEFAULT_STREAM = LatestFrameStream()


def start() -> LatestFrameStream:
    """Start the default 3-FPS latest-frame stream and return its manager."""
    _DEFAULT_STREAM.start()
    return _DEFAULT_STREAM


def stop() -> None:
    """Stop the default stream."""
    _DEFAULT_STREAM.stop()


def get_latest_frame(fmt: FrameFormat = "jpeg") -> Optional[FrameData]:
    """External function 1: return the latest JPEG bytes or Base64 string."""
    return _DEFAULT_STREAM.get_latest_frame(fmt)


def iter_latest_frames(
    interval_seconds: float,
    fmt: FrameFormat = "jpeg",
) -> Generator[FrameData, None, None]:
    """External function 2: yield the latest frame every x seconds."""
    yield from _DEFAULT_STREAM.iter_latest_frames(interval_seconds, fmt)


atexit.register(stop)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    start()
    try:
        if not _DEFAULT_STREAM.wait_for_first_frame(timeout=5.0):
            raise RuntimeError("No frame received within 5 seconds")

        # Demo: print one sample every 2 seconds without writing any image file.
        for frame in iter_latest_frames(2.0, "jpeg"):
            print(
                f"frame_id={_DEFAULT_STREAM.latest_frame_id}, "
                f"jpeg_bytes={len(frame)}"
            )
    except KeyboardInterrupt:
        pass
    finally:
        stop()
