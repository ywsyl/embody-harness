#!/usr/bin/env python3
"""
ASR 语音识别监听脚本

主动拉起收音 + 提取 ASR 识别文本：
  1. 发布 Wakeup 消息到 /audio_center/wakeup_event 触发拾音
  2. 订阅 /homi_speech/speech_assistant_status_topic
  3. 在 status=2 (AsrResponsed) 且 result.finish=true 时取 result.text
  4. 调 /homi_speech/assistant_abort_service 结束会话（主动触发时）

真实接口（已在机器狗上实测验证）：
  - 触发拾音: Topic pub /audio_center/wakeup_event (Wakeup)
  - 状态话题: Topic sub /homi_speech/speech_assistant_status_topic (AssistantEvent)

⚠️ 唤醒回应播报（"在呢"）由 speech_core 用 AlsaHelper 直接 ALSA 播放，
   绕过 audio_player，因此 stop_play 服务无法打断。
   主动触发时本脚本会把唤醒回应音频临时替换为静音（触发后立即恢复），
   因此只有主动触发这一下是静默的；真实语音唤醒播报不受影响。
"""

import os
import sys
import json
import time
import wave
import shutil
import threading
import argparse
from pathlib import Path

# 确保仓库根在 sys.path（本文件 skills/asr-listener/scripts/ 上四级 = 仓库根），
# 以便 import configs —— 收音窗口的唯一来源是 configs.ASR_LISTEN_WINDOW
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import rclpy
    from rclpy.node import Node
    from homi_speech_interface.msg import AssistantEvent, Wakeup
except ImportError:
    print('[ASR] ERROR: 无法导入 rclpy / homi_speech_interface，请先 source ROS 2 环境')
    print('[ASR] 提示: 先 source ROS 2 环境（路径见 configs/paths.py 的 ROS_SETUP'
          ' 与 CMCC_ROBOT_SETUP，别写死安装目录）')
    sys.exit(1)

try:
    from configs.configs import AUDIO_STATUS_NAMES, ASR_LISTEN_WINDOW, WAKEUP_REPLY_MUTE_WAIT
    from configs.paths import (
        SILENCE_WAV,
        WAKEUP_BACKUP_DIR,
        WAKEUP_REPLY_FILES,
        WAKEUP_RES_DIR,
    )
except ImportError as exc:
    # 不设字面量兜底：收音窗口只允许有一个来源，宁可在这里明确报错，
    # 也不要再造一个会和 configs 漂移的"默认值"。备份目录同理，而且它一旦写成
    # /root/... 绝对路径，本技能就被钉死在某一棵树上（开发仓 vs 部署树）。
    # WAKEUP_RES_DIR 更险：它指的不是本仓文件而是运行中的机器人安装，写死具体
    # 安装目录就可能落到 speech_core 根本不读的那棵树上，静默失效。
    # WAKEUP_REPLY_FILES 必须与 mute/restore 两个脚本同源：两边不一致就会出现
    # "静默了一批、只恢复了一部分"。AUDIO_STATUS_NAMES 与 tests/ 的两个观察脚本同源。
    # WAKEUP_REPLY_MUTE_WAIT 与 skills/audio_interact 同源：那边也静默唤醒回应。
    print(f'[ASR] ERROR: 无法从 configs 读取 ASR_LISTEN_WINDOW / WAKEUP_REPLY_MUTE_WAIT /'
          f' WAKEUP_BACKUP_DIR / WAKEUP_RES_DIR / WAKEUP_REPLY_FILES / SILENCE_WAV（{exc}）')
    print('[ASR] 提示: 脚本需位于 <仓库根>/skills/asr-listener/scripts/ 下运行')
    sys.exit(1)

DEFAULT_TIMEOUT = ASR_LISTEN_WINDOW

# 状态码→名称的全码表在 configs（与 tests/asr_test.py、tests/evt_watch.py 共用一份）
STATUS_NAMES = AUDIO_STATUS_NAMES

# 会话已被中止的状态码。出现这两个说明这次收音不会有结果了，立即结束等待，
# 不必再干等到超时。
#   5 Aborting ：会话被拆掉（abort 服务 / stop 打断播报 / 某轮无有效输入收尾）
#   8 UserAbort：用户中止，实测由 Wakeup 掐断正在播放的 TTS 时发出
ABORTED_STATUSES = (5, 8)

# 语音会话收尾服务（由 /homi_speech/helper 提供，硬停止当前会话）
ABORT_SERVICE = '/homi_speech/assistant_abort_service'

# 唤醒回应音频路径（speech_core 用 AlsaHelper 直接 ALSA 播放，绕过 audio_player，
# 无法用 stop_play 打断；它每次播放时读文件，因此替换文件内容可立即生效）
# WAKEUP_RES_DIR / WAKEUP_BACKUP_DIR / WAKEUP_REPLY_FILES / SILENCE_WAV 全部从 configs
# 导入（见文件顶部），不在本文件写死 —— WAKEUP_RES_DIR 必须落在运行中的安装上
# （见 configs/paths.py 的说明），文件名清单必须与 mute/restore 脚本同源。
WAKEUP_FILES = WAKEUP_REPLY_FILES


class ASRListener(Node):
    def __init__(self, timeout=DEFAULT_TIMEOUT, verbose=False, wake_word='小智小智',
                 trigger=True, trigger_wait=1.0):
        super().__init__('asr_listener')
        self.timeout = timeout
        self.verbose = verbose
        self.wake_word = wake_word
        self.trigger = trigger
        self.trigger_wait = trigger_wait
        # 调用来源标识，默认 local-agent，环境变量 ASR_CALLER 可覆盖
        self.call_source = os.environ.get('ASR_CALLER', '').strip() or 'local-agent'
        self.asr_text = None
        self.done = threading.Event()
        self.aborted = False
        self.start_time = time.time()
        self._last_partial = ''

        self.subscription = self.create_subscription(
            AssistantEvent,
            '/homi_speech/speech_assistant_status_topic',
            self.listener_callback,
            10,
        )
        self.wake_pub = self.create_publisher(Wakeup, '/audio_center/wakeup_event', 10)
        if self.verbose:
            self.get_logger().info('订阅 /homi_speech/speech_assistant_status_topic')

    def trigger_wakeup(self):
        """发布 Wakeup 消息主动触发拾音"""
        w = Wakeup()
        w.ivw_word = self.wake_word
        w.angle = 0
        # 调用来源标识：Wakeup 没有专门的来源字段，用既有的通用扩展字段 extra_info
        # 承载（全仓库无消费方，恒为空串）。默认 local-agent，ASR_CALLER 可覆盖。
        w.extra_info = json.dumps({"source": self.call_source}, ensure_ascii=False)
        self.wake_pub.publish(w)
        if self.verbose:
            print(f'[ASR] 🎤 已发送唤醒触发 (ivw_word="{self.wake_word}")，正在收音...')

    # ---------- 条件性静默唤醒回应 ----------
    # 需求：真实语音唤醒时保留"在呢"等播报；主动触发 ASR 时无播报。
    # 实现：发布 Wakeup 前把 wakeup04~10.wav 临时替换为静音，等回应窗口过去后
    #       立即恢复。speech_core 每次播放时读文件，因此替换立即生效。

    def _silence_bytes(self):
        """生成（或读取）0.3s 16kHz mono s16le 静音 wav，返回字节"""
        if not os.path.exists(SILENCE_WAV):
            # wave.open 只认 str：传 Path 时它把 Path 当"已打开的文件对象"，
            # 直到 writeframes 才炸（'PosixPath' object has no attribute 'write'）。
            # 本机 python3.8 实测如此 —— 且炸点被 mute_wakeup_reply 的 except 吞掉，
            # 结果是"静默唤醒"整个不生效（设备照常播"在呢"）。
            with wave.open(str(SILENCE_WAV), 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b'\x00\x00' * int(16000 * 0.3))
        with open(SILENCE_WAV, 'rb') as fh:
            return fh.read()

    def _ensure_backup(self):
        """确保备份目录里有原始音频（不覆盖已存在备份；跳过已是静音的）"""
        silence = None
        for f in WAKEUP_FILES:
            bk = os.path.join(WAKEUP_BACKUP_DIR, f)
            src = os.path.join(WAKEUP_RES_DIR, f)
            if os.path.exists(bk) or not os.path.exists(src):
                continue
            try:
                if silence is None:
                    silence = self._silence_bytes()
                with open(src, 'rb') as fh:
                    if fh.read() == silence:
                        continue  # 当前已是静音，不能当原始音频备份
                shutil.copy2(src, bk)
            except Exception as e:
                print(f'[ASR] ⚠️ 备份 {f} 失败: {e}')

    def mute_wakeup_reply(self):
        """主动触发前：把唤醒回应音频临时替换为静音。

        返回是否真的写成功：静默失败时设备会照常播那句回应（不影响识别），调用方
        （skills/audio_interact）据此报一条明确的告警 —— 2026-09-28 实测踩到过一次
        静默失效：生成静音 wav 的 wave.open 吃了 Path 直接抛异常，被下面的 except
        吞成一条无声的警告，"静默唤醒"整个没生效，只能靠人耳朵发现。
        """
        try:
            self._ensure_backup()
            silence = self._silence_bytes()
            for f in WAKEUP_FILES:
                dst = os.path.join(WAKEUP_RES_DIR, f)
                if os.path.exists(dst):
                    with open(dst, 'wb') as fh:
                        fh.write(silence)
            if self.verbose:
                print('[ASR] 🔇 唤醒回应已临时静默（仅本次主动触发）')
            return True
        except Exception as e:
            print(f'[ASR] ⚠️ 静默唤醒回应失败（不影响识别）: {e}')
            return False

    def restore_wakeup_reply(self):
        """主动触发后：从备份恢复原始唤醒回应音频"""
        try:
            for f in WAKEUP_FILES:
                bk = os.path.join(WAKEUP_BACKUP_DIR, f)
                dst = os.path.join(WAKEUP_RES_DIR, f)
                if os.path.exists(bk):
                    shutil.copy2(bk, dst)
            if self.verbose:
                print('[ASR] 🔊 唤醒回应音频已恢复，真实语音唤醒播报正常')
        except Exception as e:
            print(f'[ASR] ⚠️ 恢复唤醒回应音频失败: {e}')

    # ---------- 会话收尾 ----------
    # 实测：Wakeup 之后助手会进入多轮对话（每轮 16s 静音窗口），不主动结束就会
    # 一直收音下去，与「单次收音」的技能语义不符。abort 由 /homi_speech/helper
    # 提供，会硬停止当前会话（端上表现为 5 Aborting → 7 DisplayError(UserCancel)
    # → 0 Idle）。空闲时调用是空操作，所以可以无条件调。
    #
    # 只在「主动触发」（node.trigger）时收尾：--no-trigger 是「只听正在进行的
    # 真实语音对话」，那种场景用户的会话不该被我们掐断。

    def abort_session(self):
        try:
            from homi_speech_interface.srv import AssistantAbort
        except ImportError as e:
            print(f'[ASR] ⚠️ 无法导入 AssistantAbort，跳过会话收尾: {e}')
            return

        try:
            client = self.create_client(AssistantAbort, ABORT_SERVICE)
            if not client.wait_for_service(timeout_sec=2.0):
                print(f'[ASR] ⚠️ {ABORT_SERVICE} 不可用，跳过会话收尾')
                return

            future = client.call_async(AssistantAbort.Request())
            # spin 在独立线程里跑，这里轮询等响应即可
            deadline = time.time() + 2.0
            while not future.done() and time.time() < deadline:
                time.sleep(0.05)

            if not future.done():
                print('[ASR] ⚠️ 会话收尾调用超时（不影响识别结果）')
                return

            error_code = future.result().error_code
            if error_code == 0:
                if self.verbose:
                    print('[ASR] ⏹ 会话已中止（assistant_abort_service）')
            else:
                print(f'[ASR] ⚠️ 会话收尾返回错误码: {error_code}')
        except Exception as e:
            # 收尾失败不该影响识别结果与退出码，只告警
            print(f'[ASR] ⚠️ 会话收尾失败（不影响识别结果）: {e}')

    def listener_callback(self, msg):
        status = msg.status
        raw_msg = msg.msg.strip() if msg.msg else ''

        if self.verbose:
            print(f'[ASR] status={status} ({STATUS_NAMES.get(status, "?")}) '
                  f'msg={raw_msg[:120]}')

        if status == 9 and self.verbose:
            print('[ASR] ✅ 已唤醒，正在收音...')

        if status == 2 and raw_msg:
            text, finish = self._parse_asr(raw_msg)
            if text is not None:
                if finish:
                    self.asr_text = text
                    if self.verbose:
                        print(f'[ASR] 🎯 最终识别结果: {text}')
                    self.done.set()
                elif self.verbose and text != self._last_partial:
                    self._last_partial = text
                    print(f'[ASR] 流式识别中: {text}')

        # 会话已被中止：不会有识别结果了，立即结束等待
        if status in ABORTED_STATUSES:
            self.aborted = True
            self.done.set()
            return

        if time.time() - self.start_time > self.timeout:
            if self.verbose:
                print(f'[ASR] ⏰ 超时 ({self.timeout}s)')
            self.done.set()

    @staticmethod
    def _parse_asr(raw_msg):
        """解析 ASR msg JSON，返回 (text, finish)"""
        try:
            data = json.loads(raw_msg)
        except (json.JSONDecodeError, TypeError):
            return (raw_msg or None), True

        result = data.get('result') if isinstance(data, dict) else None
        if isinstance(result, dict):
            text = result.get('text', '')
            finish = bool(result.get('finish', True))
            return (str(text).strip() or None), finish
        return None, True

    def wait_for_result(self):
        self.done.wait(timeout=self.timeout)
        return self.asr_text


def main():
    parser = argparse.ArgumentParser(description='ASR 语音识别监听脚本')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT,
                        help=f'收音超时秒数，默认 {DEFAULT_TIMEOUT}'
                             '（configs.ASR_LISTEN_WINDOW；工具层不传此参数时要改就改那边）')
    parser.add_argument('--verbose', '-v', action='store_true', help='打印所有状态事件')
    parser.add_argument('--wake-word', default='小智小智', help='唤醒词，默认"小智小智"')
    parser.add_argument('--no-trigger', action='store_true',
                        help='不主动触发，仅监听（用于机器狗已被语音唤醒的场景）')
    parser.add_argument('--trigger-wait', type=float, default=1.0,
                        help='发布唤醒前等待订阅建立秒数，默认1.0')
    parser.add_argument('--keep-reply', action='store_true',
                        help='主动触发时保留唤醒回应播报（默认主动触发静默）')
    parser.add_argument('--mute-reply-wait', type=float, default=WAKEUP_REPLY_MUTE_WAIT,
                        help=f'主动触发后等待秒数再恢复唤醒回应，需盖过最长回应音频(1.5s)，'
                             f'默认 {WAKEUP_REPLY_MUTE_WAIT}（configs.WAKEUP_REPLY_MUTE_WAIT）')
    args = parser.parse_args()

    rclpy.init(args=None)
    node = ASRListener(
        timeout=args.timeout,
        verbose=args.verbose,
        wake_word=args.wake_word,
        trigger=not args.no_trigger,
        trigger_wait=args.trigger_wait,
    )

    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start()

    # 等订阅建立
    time.sleep(args.trigger_wait)

    restored = False
    try:
        if node.trigger:
            # 主动触发：先临时静默唤醒回应（只静默本次触发），再发布 Wakeup，
            # 等回应窗口过去后立即恢复，保证真实语音唤醒播报不受影响。
            if not args.keep_reply:
                node._ensure_backup()
                node.mute_wakeup_reply()
            node.trigger_wakeup()
            if not args.keep_reply:
                time.sleep(args.mute_reply_wait)
                node.restore_wakeup_reply()
                restored = True
        if args.verbose:
            print(f'[ASR] 等待识别结果（超时 {args.timeout}s）...')
        result = node.wait_for_result()
    finally:
        # 异常/中断时兜底恢复，避免唤醒回应音频停留在静音状态
        if node.trigger and not args.keep_reply and not restored:
            node.restore_wakeup_reply()
        # 主动触发必须收尾（成功/超时/中断都走这里）：否则助手会继续多轮收音。
        # 放在 finally 里是为了异常路径也收尾，避免会话一直挂着。
        if node.trigger:
            node.abort_session()

    node.destroy_node()
    rclpy.shutdown()

    if result:
        print(f'[ASR] {result}')
        sys.exit(0)
    else:
        print('[ASR] 未识别到语音内容')
        sys.exit(1)


if __name__ == '__main__':
    main()
