#!/usr/bin/env python3
"""
audio_interact —— 一趟完整交互：语音播报 + 静默唤醒 + 等状态收尾。

三件事各自复用已有技能的代码，本文件只负责编排与那句状态判据：

  ① 语音播报：走 `TTS_Manager/tts_client.py` 的 `speak_and_wait`（服务连不上时它自己退回
     skills/tts-speak 的脚本，`multi_wheel` / `play_now` 都 false），即**等这句真的播完再往下走**。
     下一步的唤醒会给还在播的 TTS 发 status=8 UserCancel，所以不等就会把问句尾巴切掉
     （2026-10-09 曾按"不重复脚本默认值"改回只传文本，同日又按用户要求传回 true）。
  ② 静默唤醒：复用 skills/asr-listener 的 `asr_monitor.ASRListener` —— 发布 Wakeup 到
     `/audio_center/wakeup_event` 把会话拉起来，同时把唤醒回应音频临时替换为静音
     （否则设备紧接着又播一句"在呢"）。真实语音唤醒的播报不受影响。
  ③ 等状态收尾：订阅 `/homi_speech/speech_assistant_status_topic`，收到**唤醒之后这一轮**的
     最终 ASR 就收工；一直没人说话才走那条静默窗口（都见下面的"退出判据"）。
  ④ 收尾：复用 `ASRListener.abort_session()` 调 `/homi_speech/assistant_abort_service`
     —— 会话是本工具开的（不是用户自己喊起来的），退出时统一收一次；状态已回到 0 时该调用
     是空操作（asr-listener 实测），只有停在 11 或等满上限时才真正起作用。

**播报为什么排在唤醒前面**（2026-09-28 定序，别再换回去）：设备的播报服务
（`AssistantSpeechText` = `run_tts_speak.sh` 调的 `/audio_center/play_tts`）开口前会先停掉
当前语音会话 —— 实测 `stop notifyUserAbort: 0` → status 5 Aborting → 7 DisplayError(UserCancel)
→ 0，然后**另起一个没有 asrSession 的** TTS 会话（域 `VOICE_INTERACTION_XF_SEPARATE_TTS`）把
这句话放出去，这期间麦克风完全不上传。所以"先唤醒再播报"= 自己把刚拉起来的收音会话拆掉，
用户说什么都收不到（正是 2026-09-28 报的"收不到音"）；`play_now=false`、`multi_wheel=true`、
`tts_session=<当前会话>` 三种写法实测一样拆。先播完再唤醒，会话才活到用户开口。
注意这条只讲**本地** TTS：它不存在边播边听；`/tmp/.fdx` 的双工体现在唤醒之后的那个会话上
（`turnTakingMode=softFullDuplex`，设备自己应答播报时用户能打断，我们照样收到那条 ASR）。

退出判据（唯一的自定义逻辑）：**收到最终 ASR（status=2 且 finish=true）就立刻收工** —— 用户这一轮
的话拿到了，本工具要做的三件事就完了。没收到才走那条兜底窗口：唤醒之后**这一轮真的动过**
（1/2/3/9 任一）才算"会话起来了"，用来挡掉唤醒之前播报阶段残留的 11/0（所以
`rearm_after_wakeup()` 在这里把它们清零重算）；**11/0 不当退出信号**——实测设备播报完会先给一个
短暂的 0，那会儿用户还没来得及开口，据此退出就永远收不到回复；它们只是"可以从这一刻起等用户
开口"的触发（configs.AUDIO_INTERACT_REPLY_WAIT，窗口内每有一次对话动静 1/2/3/9 就从头计时，
静默满才收工）。整体另有 configs.AUDIO_INTERACT_MAX_WAIT 兜底。

**为什么"收下回话"就是收尾信号，不能挂在 11/0 上（2026-10-09 实测）**：收音会话开着的时候设备
**根本不发 11/0** —— 它一路等到自己那 16 秒多轮静默窗口超时（超时后还会播一句错误提示）才回到
0。那天的实测：用户的话 +16.8s 就拿到了，工具却等到 +38.0s 才返回，中间 21s 是拿着答案陪设备
等超时，用户还白听了一句超时提示。所以收工条件直接盯答案本身。

与 speak / speak_listen / listen 的关系：听的**同一路**声音（设备侧 ASR），收工时机现在也一致
（拿到第一条最终结果就走）；不同的只有"哪一条算数"—— 那两个走 `run_asr.sh`（`asr_monitor`
收下**任意一条**最终结果就退出，不管会话是谁开的），本工具只认**自己唤醒之后**这一轮的那条
（`rearm_after_wakeup()` 划的界），并在一直没人说话时另有一条静默窗口兜底（见上）。播完判定那
半段也一致（`wait_tts_done.py` 的 status=0 规则，即 ① 传的 `wait_done=true`）。

输出约定：成功时 stdout 打**一行 JSON**（供 plugin_tools/audio_tools.py 的 run_audio_interact 解析），诊断一律走 stderr。

用法:
    python3 audio_interact.py --text "<播报文本>" [--max-wait 50] [--keep-reply] [--verbose]
"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path


def _find_repo_root():
    """往上找带 configs/configs.py 的那一层：脚本挪到别的目录也不用改（不数固定层数）"""
    for parent in Path(__file__).resolve().parents:
        if (parent / 'configs' / 'configs.py').is_file():
            return parent
    return None


_ROOT = _find_repo_root()
if _ROOT is not None and str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from configs.configs import (AUDIO_INTERACT_MAX_WAIT, AUDIO_INTERACT_REPLY_WAIT,
                                 AUDIO_STATUS_NAMES, ROS_DOMAIN_ID, TTS_TOOL_TIMEOUT,
                                 WAKEUP_REPLY_MUTE_WAIT)
    from configs.paths import ASR_MONITOR_SCRIPT
    # 播报的唯一入口（常驻服务 socket；连不上它自己退回 skills/tts-speak 的脚本）。
    from TTS_Manager import tts_client
except ImportError as exc:
    print(f'[交互] ERROR: 无法从 configs 读取常量/脚本路径（{exc}）', file=sys.stderr)
    print('[交互] 提示: 脚本需放在仓库内（往上能找到 configs/configs.py 的那棵树下）运行',
          file=sys.stderr)
    sys.exit(1)

# 复用 asr-listener 的实现：唤醒怎么发、唤醒回应怎么临时静音、会话怎么收，全仓只有那一份。
# 目录经 configs.paths.ASR_MONITOR_SCRIPT 取，不在本文件写死（它同时给出技能所在位置）。
sys.path.insert(0, str(ASR_MONITOR_SCRIPT.parent))
try:
    import asr_monitor
except ImportError as exc:
    print(f'[交互] ERROR: 无法导入 skills/asr-listener 的 asr_monitor（{exc}）', file=sys.stderr)
    print('[交互] 提示: 需先 source ROS 2 环境（configs/paths.py 的 ROS_SETUP 与'
          ' CMCC_ROBOT_SETUP），且 asr-listener 技能在同一棵树里', file=sys.stderr)
    sys.exit(1)

STATUS_TOPIC = '/homi_speech/speech_assistant_status_topic'
STATUS_NAMES = AUDIO_STATUS_NAMES
REPLY_WAIT = AUDIO_INTERACT_REPLY_WAIT

# 等状态话题的发布者出现再动手：唤醒那一段的状态（9/3/11/0）来得很快，订阅晚了就看不到
PUBLISHER_WAIT_SEC = 3.0

ASR_STATUS = 2          # AsrResponsed：msg 里是 ASR 的 JSON（finish=true 才是最终结果）
SPEECH_STATUS = 3       # AsrTextout：转录/播报中
RUNNING_STATUS = 1      # Running：对话进行中
WAKEUP_STATUS = 9       # Wakeup：唤醒/开始收音（本工具发的唤醒就是它；用户自己再喊一次也发它）
SILENT_WAIT_STATUS = 11  # 静默等待：机器人回复完、用户没开启新一轮对话
IDLE_STATUS = 0         # 空闲：这一轮对话结束
END_STATUSES = (SILENT_WAIT_STATUS, IDLE_STATUS)
# 「这一轮真的动过」：1 对话中 / 2 ASR / 3 转录播报中 / 9 唤醒。一个码管两件事 ——
# 既是"会话真的起来了"的证据（没动过就收到 11/0 = 会话压根没起来，别认它），
# 也是「对话有动静」（收到就把等回复的窗口从头计时）。
# 播报那一段（本工具排在唤醒前面）自己也会打出 3、并留下收尾的 11/0，
# 所以这个判据只在 `rearm_after_wakeup()` 之后才算数。
ACTIVITY_STATUSES = (RUNNING_STATUS, ASR_STATUS, SPEECH_STATUS, WAKEUP_STATUS)

_VERBOSE = False


def _log(msg):
    print(f'[交互] {msg}', file=sys.stderr, flush=True)


def _log_v(msg):
    if _VERBOSE:
        _log(msg)


class AudioInteract(asr_monitor.ASRListener):
    """只替换父类的状态判据。

    父类那份不区分会话（播报那一段飘来的 ASR 也会被它当成结果）、也没有唤醒；本工具的判据是
    "唤醒之后这一轮（`rearm_after_wakeup()` 起算）的那条最终 ASR"，但**收工时机与父类一致**：
    拿到最终 ASR 就走；没拿到才走那条由 11/0 开出的静默窗口。
    """

    def __init__(self, verbose=False):
        super().__init__(verbose=verbose, trigger=True)
        self.t0 = time.monotonic()   # 整趟（含播报那段）的时钟：上限与耗时汇报都按它
        self.t_wakeup = None         # 发出 Wakeup 的时刻：唤醒回应静音窗口按它算
        self.session_live = False    # rearm 之后这一轮动过（ACTIVITY_STATUSES 里的码）
        self.ended_by = None         # 触发「等回复」窗口的码：11 / 0
        self.reply_deadline = None   # 窗口截止时刻；窗口内每有一次动静就往后推
        self.answer = None           # 最后一条最终 ASR 文本
        self.statuses = []           # 收到过的状态码（相邻去重），仅用于诊断

    def rearm_after_wakeup(self):
        """发出 Wakeup 之后立刻调用：判据清到"这一轮还没见过任何动静"，从这儿重新起算。

        播报那段（本工具排在唤醒前面）自己也会打 3、并留下收尾的 11/0，
        不清掉就会拿它们当"会话起来过"的证据，窗口也会提前打开。
        """
        self.t_wakeup = time.monotonic()
        self.session_live = False
        self.ended_by = None
        self.reply_deadline = None
        self.answer = None   # 答案必须是这一轮的：播报阶段若飘来一条旧 ASR 不算

    def listener_callback(self, msg):
        elapsed = time.monotonic() - self.t0
        name = STATUS_NAMES.get(msg.status, '?')
        detail = ''
        if msg.status == ASR_STATUS:
            text, finish = self._parse_asr(msg.msg)
            detail = f' text={text!r} finish={finish}' if text else ''
            # finish=false 是流式中间片段，不能当用户这一轮的话
            if text and finish:
                # 主循环看到 answer 就收工 —— 不等 11/0（收音会话开着时设备不发它，见模块 docstring）
                self.answer = text
                _log(f'已收下用户回话（{text!r}）@ {elapsed:.2f}s，收工')
        _log_v(f'  [{elapsed:6.2f}s] status={msg.status} {name}'
               f' section_id={msg.section_id!r}{detail}')

        if not self.statuses or self.statuses[-1] != msg.status:
            self.statuses.append(msg.status)

        # 这一轮动过了：既记成"会话起来过"的证据，也把等回复的窗口往后推
        now = time.monotonic()
        if msg.status in ACTIVITY_STATUSES:
            self.session_live = True
            if self.reply_deadline is not None:
                self.reply_deadline = now + REPLY_WAIT
                _log_v(f'  对话有动静（status={msg.status} {name}），窗口重新计时 {REPLY_WAIT}s')
        elif msg.status in END_STATUSES and self.session_live and self.reply_deadline is None:
            # 会话起来过且首次收到 11/0：不退出，转入等用户开口的静默窗口 ——
            # 设备播报完常先冒一个短暂的 0，此刻用户还没说话，直接退出就收不到回复。
            # 只开一次窗（此后 11/0 不重置，否则设备若反复发就永远不退出）
            self.ended_by = msg.status
            self.reply_deadline = now + REPLY_WAIT
            _log(f'会话已起来：这一轮动过（1/2/3/9）→ 收到 {msg.status} {name}'
                 f'（{elapsed:.2f}s）；转入等待用户回复，静默满 {REPLY_WAIT}s 才退出')


def speak(text, timeout):
    """发一次 TTS 播报并等它**真的播完**再返回（走 `tts_client.speak_and_wait`）。

    等播完在这里是**必须**的：播报是异步流式播放，服务 1~3 秒返回而音频还在放；不等就往下走，
    下一步的唤醒会给还在播的 TTS 发 status=8 UserCancel，把问句尾巴切掉（2026-10-09 实测）。

    `multi_wheel` / `play_now` 都 `false` —— 这里**不抢麦**：下面紧接着的唤醒本来就够掐断它，
    而 `play_now=true` 会在播报服务侧多一次主动 stop。

    等的是「队列见底」：常驻服务在时是它广播的 `drained`（这一句是本次调用自己交出去的，
    涵盖它），服务不在时退回脚本的 `wait_done=true`（`section_id` 是本次调用自己的，判定精确）。
    `timeout` 是等播完的上限，交给 tts_client（调用方给的是 TTS_TOOL_TIMEOUT，见 main）。
    """
    _log(f'播报: {text}')
    try:
        tts_client.speak_and_wait(text, timeout=timeout)
    except RuntimeError as exc:      # 服务拒绝/中途失败/回退脚本非 0 退出（ServiceError 也是它）
        return str(exc)
    return None


def _check_domain():
    """本进程按环境变量订阅，出声那一侧却无条件按 configs 设域 —— 常驻服务由 start_agent
    在 starter.sh 备好的环境里拉起、回退脚本（run_tts_speak.sh）自己按 configs 导，两者都
    不看本进程的 ROS_DOMAIN_ID。不一致就会「订阅在域 A、出声在域 B」：状态一条收不到、
    只能等满上限，而播报还真的打到了那边（2026-09-28 实测：订阅在隔离域 99，播报打到
    live 域 2 出了声）。换域验证请改 configs。"""
    env_domain = os.environ.get('ROS_DOMAIN_ID')
    if env_domain is not None and env_domain != str(ROS_DOMAIN_ID):
        _log(f'⚠ 本进程 ROS_DOMAIN_ID={env_domain} 与 configs 的 {ROS_DOMAIN_ID} 不一致：'
             '播报与唤醒都按 configs 走，订阅按环境走，两边会错配 —— 收不到状态事件，'
             '出声与唤醒还会落到 configs 那个域上。换域验证请改 configs。')


def main():
    global _VERBOSE
    parser = argparse.ArgumentParser(
        description='播报 + 静默唤醒 + 等状态收尾（收到最终 ASR 就收工；没收到时：唤醒后这一轮'
                    f'动过（1/2/3/9）又收到 11/0 起算，静默满 {AUDIO_INTERACT_REPLY_WAIT}s 收工）')
    parser.add_argument('--text', default='', help='要播报的文本')
    parser.add_argument('--max-wait', type=float, default=float(AUDIO_INTERACT_MAX_WAIT),
                        help=f'等待上限秒数，默认 {AUDIO_INTERACT_MAX_WAIT}'
                             '（configs.AUDIO_INTERACT_MAX_WAIT）')
    parser.add_argument('--keep-reply', action='store_true',
                        help='唤醒时保留设备那句回应播报（默认临时静默，免得和我们这句撞车）')
    parser.add_argument('--verbose', action='store_true', help='逐条打印收到的状态事件')
    args = parser.parse_args()
    _VERBOSE = args.verbose

    text = (args.text or '').strip()
    if not text:
        _log('ERROR: 文本内容为空')
        return 1

    _check_domain()

    rclpy = asr_monitor.rclpy          # 复用 asr-listener 已导入的 rclpy，不另起一份依赖
    rclpy.init()
    node = AudioInteract(verbose=args.verbose)
    threading.Thread(target=rclpy.spin, args=(node,), daemon=True).start()

    restored = False
    try:
        deadline = time.monotonic() + PUBLISHER_WAIT_SEC
        while node.count_publishers(STATUS_TOPIC) == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        if node.count_publishers(STATUS_TOPIC) == 0:
            _log(f'⚠ {PUBLISHER_WAIT_SEC:.1f}s 内没看到 {STATUS_TOPIC} 的发布者，'
                 '照样往下走，但可能拿不到状态事件')

        # ① 播报：必须先播完，再唤醒 —— 播报服务会拆掉当前语音会话（见模块 docstring）
        err = speak(text, TTS_TOOL_TIMEOUT)
        if err:
            _log(f'ERROR: 播报失败：{err}')
            return 1

        # ② 静默唤醒：先静音唤醒回应，再发 Wakeup（顺序不能反，反了会漏出"在呢"）
        if not args.keep_reply:
            node._ensure_backup()
            if not node.mute_wakeup_reply():
                # 静默没落盘 = 设备会照常播那句"在呢"，和我们这句撞车。不中断（识别照常），
                # 但必须在诊断里说清楚，否则只能靠人耳朵发现（2026-09-28 就是这么漏的）。
                _log('⚠ 唤醒回应未能静默：设备会先播一句回应，本次交互不是静默唤醒')
        node.trigger_wakeup()
        # 判据从这一刻重新起算：上面播报那段留下的 3 与 11/0 不算这一轮的证据
        node.rearm_after_wakeup()
        _log('已发起唤醒' + ('（唤醒回应已临时静默）' if not args.keep_reply else '（保留回应播报）'))

        # 唤醒回应窗口过去后立刻恢复音频；异常路径由 finally 兜底
        if not args.keep_reply:
            left = WAKEUP_REPLY_MUTE_WAIT - (time.monotonic() - node.t_wakeup)
            if left > 0:
                time.sleep(left)
            node.restore_wakeup_reply()
            restored = True

        # ③ 等状态收尾。轮询而不是一次性 wait：等回复的窗口会被新动静推后，截止时刻是活的
        hit_ceiling = False
        while True:
            now = time.monotonic()
            if now - node.t0 >= args.max_wait:
                hit_ceiling = True
                break
            if node.answer is not None:
                # 拿到用户这一轮的话就是成功出口：立刻收工，不等设备的 11/0（见模块 docstring）
                break
            if node.reply_deadline is not None and now >= node.reply_deadline:
                _log(f'等待回复窗口结束（静默 {REPLY_WAIT}s 无新动静），退出')
                break
            time.sleep(0.1)

        waited = time.monotonic() - node.t0
        # 告警只针对「既没回话、也没见到 11/0」：拿到回话的出口可能是它先到（ended_by 还空着），
        # 那不是异常
        if node.ended_by is None and node.answer is None:
            _log(f'⚠ 等待 {waited:.2f}s 仍未达成「这一轮动过（1/2/3/9）→ 收到 11/0」'
                 f'（这段时间收到的状态：{node.statuses or "无"}）')
        elif hit_ceiling:
            _log(f'⚠ 已到整体上限 {args.max_wait:.0f}s（等待回复期间反复有新动静，'
                 '窗口被一直推后）')

        print(json.dumps({
            'answer': node.answer or '',
            'ended_by': node.ended_by,
            'waited_sec': round(waited, 2),
            'statuses': node.statuses,
        }, ensure_ascii=False), flush=True)
    finally:
        if not args.keep_reply and not restored:
            node.restore_wakeup_reply()
        # 会话是本工具开的：退出时统一收一次（状态已是 0 时它是空操作）
        node.abort_session()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
