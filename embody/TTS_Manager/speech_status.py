#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
语音状态话题的判定：**「播完了没有」这件事只在这里判一次**。

谁用：
  · `TTS_Manager/tts_service.py` —— 常驻服务。它订阅话题（**全仓唯一长期订阅者**），
    既拿最后一条原始码（`Observer/status_observer.probe_audio` 要的那份事实），又用
    `QueueDrain` 判「交出去的每一句都播完了」并广播 `drained`。
  · `skills/tts-speak/scripts/wait_tts_done.py` —— 回退路径上的一次性等待（服务不在时
    逐句播报那条老路），用 `DoneWatcher` 判自己那一次播报播完。它自己订阅话题：那时服务
    不在，「唯一订阅者」这条约定由服务承担，回退路径另起一次订阅与它并不冲突。

话题与判据的事实（2026-09-15 麦克风声学实测，见 skills/tts-speak/SKILL.md）：
  · `status=3 AsrTextout` 是「分句合成就提交播放」，全在播报开头 1~2 秒内出现，
     **不是**播完信号；
  · `status=0 Idle` 出现在**实际出声停止之后**，随文本长度线性增长，是可靠的播完信号；
  · `status=8` 是唤醒给的 UserCancel（掐断在播的 TTS），不是正常收尾。
原本按文本字数估算时长再 sleep 的方案已删除（实测系统性偏短）。
"""

import time
from typing import Optional

# 语音助手（/homi_speech 下的 speech_core）发的状态话题。全仓只在这里写一次话题名。
STATUS_TOPIC = '/homi_speech/speech_assistant_status_topic'

IDLE_STATUS = 0                      # Idle(空闲)：会话/播报结束
# Running / AsrResponsed / AsrTextout / AsrEnd / Wakeup ——
# 「这一轮真的动过」的证据，只服务保护期判定（见 DoneWatcher）
ACTIVE_STATUSES = (1, 2, 3, 4, 9)

# 最短保护期：刚订阅上时话题上可能还挂着上一轮会话残留的 Idle 事件，这时候直接判定
# 「播完」会提前返回。这段时间内的 status=0 一律忽略（除非 section_id 能明确对上，
# 那说明就是本次会话的收尾）。只对**刚订阅**的判定有意义，常驻服务的 QueueDrain 不用它
# （它长期订阅，保护期由「事件必须晚于最后一次交接」天然保证）。
GUARD_SEC = 1.5

# QueueDrain 的静默窗口：一次 Idle 之后要再静默这么久、期间没有 ACTIVE 事件，才算队列见底。
# 它量的是「设备在两句话之间回 Idle 的那一瞬」——2026-10-12 实测每句各有一条 Idle，紧接着
# 下一句 runInquiry，所以按单次 Idle 判会返回到两句中间（理由见 QueueDrain 的注释）。
# 值取「人耳听不出差别」量级：它只推迟 drained 的上报，不推迟出声。
DRAIN_SETTLE_SEC = 0.5


class DoneWatcher:
    """等**一次**播报播完：喂状态事件进去，问它 `done` 没。

    判据来自 wait_tts_done.py（2026-09-15 实测口径），供回退路径的一次性等待使用：
      · 收到 ACTIVE_STATUSES 之一 → 记「这一轮真的动过」（保护期的另一半）
      · 收到 IDLE_STATUS → `section_id` 两边都非空时须相等（对不上 = 别的会话的 Idle，
        忽略）；还没动过且距创建不满 `GUARD_SEC` → 上一轮残留，忽略；否则 done
    等不到时**不做任何估算兜底**（旧实现按字数估时长，实测偏短），由调用方按等待上限如实报超时。
    """

    def __init__(self, section_id: str = '', t0: Optional[float] = None) -> None:
        """@param section_id - 本次播报的 section_id（可为空串：调用方拿不到时只能靠保护期）。
        @param t0 - 计时起点（单调秒）；默认取当下。"""
        self._section_id = section_id
        self._t0 = time.monotonic() if t0 is None else t0
        self._active = False
        self._done = False

    @property
    def done(self) -> bool:
        """判定结果（喂过的事件里是否出现了「本次播报播完」）。"""
        return self._done

    def elapsed(self) -> float:
        """距计时起点过了多少秒（日志/超时判断用）。"""
        return time.monotonic() - self._t0

    def feed(self, status: int, section_id: str = '') -> bool:
        """喂一条状态事件，返回是否（已经）判定为播完。

        @param status - 事件里的原始状态码。
        @param section_id - 事件里的 section_id（可空）。
        @returns 判定为播完 → True。
        """
        if status in ACTIVE_STATUSES:
            self._active = True
            return self._done
        if status != IDLE_STATUS:
            return self._done
        # section_id 明确时优先按它认：对上 → 就是本次；对不上 → 是别的会话，忽略。
        # 状态事件里 section_id 可能为空串，那就不能用它排除，只能走下面的兜底判断。
        if self._section_id and section_id:
            if section_id != self._section_id:
                return self._done
            self._done = True
            return True
        if not self._active and self.elapsed() < GUARD_SEC:
            return False
        self._done = True
        return True


class QueueDrain:
    """逐句投递下「**交出去的每一句都不会再出声了**」的判定（常驻服务用）。

    与 DoneWatcher 的差别：那个等的是**某一条会话**播完（有 section_id 可归属、订阅是刚起的
    所以需要保护期）；这个等的是**队列见底** —— 服务长期订阅、自己记着最后一句是什么时候交
    出去的，所以「事件必须晚于最后一次交接」由调用方的时序天然保证，不需要 GUARD_SEC。

    「播完」这件事在这里是三件事同时成立（第三条由调用方 gate，见 poll 的注释）：
      1. 交出去但还没见底的句数 > 0（服务至少交过一句，否则没什么可等的）；
      2. 最后一次交接之后收到过 `status=0 Idle`（设备说「我没在放东西了」）；
      3. Idle 之后再静默 `settle_sec` —— 期间没有 ACTIVE 状态事件。

    第 3 条是**必须的**，不是保险：设备把几条请求排成一队逐条播时，「一条播完」与「下一条开始」
    之间它也会短暂回 Idle（2026-10-12 实测：每句各有一条 Idle，紧接着下一句 runInquiry）。
    少了它，`speak_listen` 会在自己第二句还在放的时候就开始收音 —— 那正是这套等待要防的事。
    代价是 drained 晚报 settle_sec（0.5s 量级），只影响「开始收音」的时刻，听不出差别。
    """

    def __init__(self, settle_sec: float = DRAIN_SETTLE_SEC) -> None:
        """@param settle_sec - Idle 之后要求静默多久才算队列见底（见 DRAIN_SETTLE_SEC）。"""
        self._settle_sec = settle_sec
        self._pending = 0                 # 交出去、还没见底的句数
        self._idle_at: Optional[float] = None   # 最后一次 Idle 的单调时刻（None = 还没见 Idle）

    @property
    def pending(self) -> int:
        """交出去、还没见底的句数（看门狗与诊断读它）。"""
        return self._pending

    def handoff(self) -> None:
        """记一次「一句已经交给设备了」（play_tts 调用已发出）。

        交出去的这一刻起，先前看到的 Idle 不再说明队列空了（它说的是交出它**之前**的事）。
        """
        self._pending += 1
        self._idle_at = None

    def cancel_handoff(self) -> None:
        """撤掉一次交接：play_tts 报错的那一句不会出声，不该继续压着「见底」不放。

        调用方保证只会撤销已 handoff 的句子（服务侧就是在应答回来报错时撤）。
        """
        if self._pending > 0:
            self._pending -= 1

    def reset(self) -> None:
        """清空记账（服务侧看门狗用：设备迟迟不回 Idle，按上限放行前先把账抹平）。"""
        self._pending = 0
        self._idle_at = None

    def on_event(self, status: int) -> None:
        """喂一条状态事件：Idle 记时刻（供静默计时），ACTIVE 作废先前那次 Idle。"""
        if status == IDLE_STATUS:
            self._idle_at = time.monotonic()
        elif status in ACTIVE_STATUSES:
            self._idle_at = None

    def poll(self, queue_empty: bool, now: Optional[float] = None) -> bool:
        """问一次「此刻见底了吗」；返回 True 时判定被消耗掉（下一次要重新攒）。

        @param queue_empty - 服务侧此刻**没有待发的句子、也没有在飞的 play_tts 调用**。
            这是必须由调用方给的：还有句子没交出去时，设备回 Idle 只说明「它手头这点放完了」，
            不代表接下来不会有声音。
        @param now - 时刻（单调秒）；默认取当下。
        @returns 见底 → True（并把记账清空）。
        """
        if not queue_empty or self._pending <= 0 or self._idle_at is None:
            return False
        now = time.monotonic() if now is None else now
        if now - self._idle_at < self._settle_sec:
            return False
        self._pending = 0
        self._idle_at = None
        return True
