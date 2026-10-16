#!/usr/bin/env python3
"""
等待一次 TTS 播报真正播完（供 run_tts_speak.sh 的 wait_done=true 调用）。

为什么要它：play_tts 是**异步流式播放** —— 服务在 1~3 秒内就返回，音频却还在
继续放几十秒。提前返回会让后续的 ASR/Wakeup 掐断还没放完的语音
（status=8 UserCancel）。

**本文件只是薄壳**：判定（订阅状态话题、什么时候算播完）在
TTS_Manager/speech_status.py 的 `DoneWatcher` 里，全仓只那一份。这里只负责
「起一个 rclpy 节点订阅话题 → 把事件喂给 DoneWatcher → 按上限收工 → 打日志」。

服务在的时候这条路不会跑（常驻服务自己判 drained，见 TTS_Manager/tts_service.py）；
它只服务回退路径：服务不可达时逐句调 run_tts_speak.sh，那时才需要这里自己订阅。

不知道 section_id 时靠 `GUARD_SEC` 保护期兜住「刚订阅就撞上上一轮残留的 Idle」。
等不到状态事件的唯一后果是**等满 --max-wait 上限**（默认取 configs.TTS_WAIT_DONE_MAX_WAIT）
—— 按字数估算时长的老方案已删（实测系统性偏短），宁可如实报超时。

输出约定与 run_tts_speak.sh 一致：stdout 保持为空，全部诊断走 stderr。
退出码恒为 0 —— 等待失败不应该让播报本身算失败。

用法:
    python3 wait_tts_done.py --text "<播报文本>" [--section-id <id>] \\
        [--max-wait 50] [--verbose]
"""

import argparse
import sys
import time
from pathlib import Path

# 确保仓库根在 sys.path（本文件 skills/tts-speak/scripts/ 上四级 = 仓库根），
# 以便 import configs 与 TTS_Manager。
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import TTS_WAIT_DONE_MAX_WAIT
from TTS_Manager.speech_status import STATUS_TOPIC, DoneWatcher

_VERBOSE = False


def _log(msg: str) -> None:
    print(f'[TTS] {msg}', file=sys.stderr, flush=True)


def _log_v(msg: str) -> None:
    if _VERBOSE:
        _log(msg)


def wait_done(section_id: str, max_wait: float) -> None:
    try:
        import rclpy
        from rclpy.node import Node
        from homi_speech_interface.msg import AssistantEvent
    except Exception as exc:
        # 没有 ROS 环境就无从判定播完。原先是退化成 sleep(估算时长)，
        # 那等于用一个已知偏短的值假装成功；现在直接说明情况返回 ——
        # 调用方（父进程）本来就会因为服务调用失败而知道这次播报没成。
        _log(f'⚠ 无法订阅状态话题（{exc}），无法判定播报结束，立即返回')
        return

    rclpy.init()
    node = Node('tts_wait_done')
    watcher = DoneWatcher(section_id)

    def on_event(m) -> None:
        _log_v(f'  status={m.status} section_id={m.section_id!r} '
               f'msg={(m.msg or "")[:40]!r} (t={watcher.elapsed():.2f}s)')
        if watcher.feed(m.status, m.section_id or ''):
            _log_v('  ← 这一条判定了播完')

    try:
        node.create_subscription(AssistantEvent, STATUS_TOPIC, on_event, 10)
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.1)
            if watcher.done:
                _log(f'播报结束（status=0 Idle），等待 {watcher.elapsed():.2f}s')
                return
            if watcher.elapsed() >= max_wait:
                _log(f'⚠ 等待 {max_wait:.1f}s 仍未收到 status=0 Idle，按上限返回')
                return
    except KeyboardInterrupt:
        _log('等待被中断')
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def main() -> int:
    global _VERBOSE
    ap = argparse.ArgumentParser(description='等待 TTS 播报播完（status=0 Idle）')
    ap.add_argument('--text', default='',
                    help='本次播报的文本，仅用于日志（判定已不依赖文本长度）')
    ap.add_argument('--section-id', default='',
                    help='play_tts 返回的 section_id，用于排除其它会话的 Idle 事件')
    ap.add_argument('--max-wait', type=float, default=float(TTS_WAIT_DONE_MAX_WAIT),
                    help=f'等待上限秒数，默认 {TTS_WAIT_DONE_MAX_WAIT}'
                         '（configs.TTS_WAIT_DONE_MAX_WAIT，须小于 TTS_TOOL_TIMEOUT）')
    ap.add_argument('--verbose', action='store_true', help='逐条打印收到的状态事件')
    args = ap.parse_args()

    _VERBOSE = args.verbose
    wait_done(args.section_id, args.max_wait)
    return 0


if __name__ == '__main__':
    sys.exit(main())
