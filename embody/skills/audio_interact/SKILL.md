---
name: audio_interact
description: 一趟完整语音交互技能：TTS 播报（复用 tts-speak）→ 静默唤醒（复用 asr-listener）→ 订阅状态话题等这一轮收尾，返回设备侧 ASR 认定的用户回话。工具层的 audio_interact 工具由它落地。
---

# audio_interact 技能（播报 + 静默唤醒 + 等状态收尾）

一趟完整交互：**说我们这句话、把设备的语音会话拉起来、等这一轮走完**，顺便把设备侧 ASR
认出来的用户回话带回给模型。工具层的 `audio_interact` 工具由它落地。**播报必须在唤醒之前**
（理由见下节末），这是 2026-09-28 定序。

## 执行脚本

```bash
bash skills/audio_interact/audio_interact.sh "你好呀" [--max-wait 50] [--keep-reply] [--verbose]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `<text>`（位置参数） | 必填 | 要播报的文本（与 `run_tts_speak.sh` 同一个约定） |
| `--max-wait` | `configs.AUDIO_INTERACT_MAX_WAIT`（≈52s） | 整个工具的等待上限，含播报与唤醒那几秒 |
| `--keep-reply` | 关 | 唤醒时**保留**设备那句回应播报（默认临时静默，见下） |
| `--verbose` | 关 | 逐条打印收到的状态事件 |

**成功时 stdout 只有一行结果 JSON**，诊断一律走 stderr：

```json
{"answer": "关灯", "ended_by": 11, "waited_sec": 12.4, "statuses": [9, 1, 2, 3, 11]}
```

| 字段 | 含义 |
|---|---|
| `answer` | 用户这一轮的话（`status=2` 且 `finish=true` 的 `result.text` 里最后一条）；没人说话就是 `""` |
| `ended_by` | 触发「等用户回复」窗口的码：`11` 静默等待 / `0` 空闲 / `null` = 没达成，等满上限 |
| `waited_sec` | **全程**耗时（含唤醒、播报），不是等回复那一段 |
| `statuses` | 期间收到的状态码（相邻去重），诊断用 |

退出码：`0` 正常收工（**含等满上限**）；`1` 文本为空、播报失败、环境/导入问题。

## 三步流程（每一步都复用别人的代码，本技能只编排）

1. **语音播报** — 走 `TTS_Manager/tts_client.py` 的 `speak_and_wait`（播报通道的唯一入口
   —— 常驻 TTS 服务在时它就是**整段交一句 + 等队列见底**；服务不在时才退回
   `skills/tts-speak/scripts/run_tts_speak.sh` 的 `wait_done=true`），`multi_wheel` /
   `play_now` 都取默认 `false`，即**等这句真的播完再往下走**。为什么必须等：播报
   是异步流式播放，不等的话下一步的唤醒会给还在播的 TTS 发 `status=8 UserCancel`，把问句尾巴
   切掉（2026-10-09 实测）。
2. **静默唤醒** — 复用 `skills/asr-listener/scripts/asr_monitor.py` 的
   `ASRListener`：把唤醒回应音频（`wakeup04~10.wav`）临时替换为静音 → 发布 `Wakeup` 到
   `/audio_center/wakeup_event` → 等回应窗口（`configs.WAKEUP_REPLY_MUTE_WAIT`，2.5s）过去
   → 立即从备份恢复。**顺序不能反**，反了会漏出"在呢"。真实语音唤醒的播报不受影响。
   本技能直接 `import asr_monitor`（路径经 `configs.paths.ASR_MONITOR_SCRIPT` 取），
   不另抄一份唤醒/静音实现。发出 Wakeup 后立刻 `rearm_after_wakeup()`：状态判据从这一刻
   清零重算 —— 上面播报那段自己也会打 `3`、并留下收尾的 `11`/`0`，不算这一轮的证据。
3. **等状态收尾** — 订阅 `/homi_speech/speech_assistant_status_topic`，判据见下节。
   退出时复用 `ASRListener.abort_session()` 调 `/homi_speech/assistant_abort_service`：
   会话是本工具开的（不是用户自己喊起来的），统一收一次；**状态已回到 0 时它是空操作**，
   只有停在 11 或等满上限时才真正起作用。放在 `finally` 里，失败/异常路径也收。

### 为什么播报排在唤醒前面（2026-09-28 实测）

设备的播报服务（`AssistantSpeechText` = `/audio_center/play_tts`）开口前会**先停掉当前语音
会话**：实测 `stop notifyUserAbort: 0` → status `5` Aborting → `7` DisplayError(UserCancel)
→ `0`，然后**另起一个没有 `asrSession` 的** TTS 会话（域 `VOICE_INTERACTION_XF_SEPARATE_TTS`）
把这句话放出去，这期间**麦克风完全不上传**。所以"先唤醒再播报"= 自己把刚拉起来的收音会话
拆掉，用户说什么都收不到（2026-09-28 报的"收不到音"就是这个）；`play_now=false`、
`multi_wheel=true`、`tts_session=<当前会话>` 三种写法实测一样拆，`AssistantCtrl` 送文本也无效。
**本地这条 TTS 路不存在边播边听**；带 `/tmp/.fdx` 的双工
（`turnTakingMode=softFullDuplex`）体现在**唤醒之后**那个会话上：设备自己应答播报时用户能
打断，`/tmp/.fdx` 期间那条 ASR 照样是我们的 —— 这正是本工具能收到流式最终文本的路径。

## 退出判据（唯一自定义逻辑）

```
这一轮（rearm 之后）
   ├─ 收到最终 ASR（status=2 且 finish=true）→ 立刻收工（不等 11/0）
   └─ 没人说话：这一轮动过（1/2/3/9 任一）且首次收到 11 或 0 → 转入「等用户回复」窗口，
      静默满 REPLY_WAIT 才退出
```

- **收到最终 ASR 就收工，不等 `11`/`0`（2026-10-09 实测）**：收音会话开着时设备**一个
  `11`/`0` 都不发** —— 它会先耗完自己那 16s 多轮静默窗口，再播一句错误提示，最后才给 `0`。
  实测一条 `audio_interact` 因此白等 21s（回话在 +16.8s 就拿到了，工具到 +38.0s 才返回，
  用户还听到了那句错误提示）。收工时机现在与 `run_asr.sh` 完全一致，只是认哪一条 ASR
  不同（只认我们唤醒之后那一轮的）
- **"这一轮动过"是"会话真的起来了"的证据**（同一个码表 `ACTIVITY_STATUSES` 管两件事：证据 +
  窗口重置），用来挡掉残留的 11/0：一次都没出现过就不进窗口（那种情况等满上限并如实报超时）
- **11/0 不当退出信号**：实测设备**播报完会先给一个短暂的 `0`**，那会儿用户还没来得及开口，
  据此退出就永远收不到回复。它们只是"可以开始等回复了"的触发
- 窗口 = `configs.AUDIO_INTERACT_REPLY_WAIT`（3s）：**一直没人说话**时等这么久。窗口内每收到
  一次**对话动静**（`1` Running / `2` ASR / `3` 播报 / `9` Wakeup）就从**头**计时
- `11`/`0` 本身**不**重置窗口（否则设备若反复发就永远不退出）
- 整体上限 `configs.AUDIO_INTERACT_MAX_WAIT`（= 播报段 + 静默那 2.5s + 设备那一轮收音 +
  两个窗口，逐段相加），防你一句我一句把工具拖住；工具层子进程超时
  `AUDIO_INTERACT_TOOL_TIMEOUT` = 它 **+10s**（那 10s 是脚本在 MAX_WAIT 之外的固定开销：
  source 安装、起 rclpy、等发布者、退出时 abort）。两个上限都在 configs 里按公式算，别在此处抄数字

## 与相邻技能/工具的关系

| | 听什么 | 播完/收完怎么判 | 会话谁开 |
|---|---|---|---|
| `speak`（tts-speak） | 不收音 | 播完判定只有一份（`TTS_Manager/speech_status.py`）：服务在时等服务广播 `drained`，不在时 `wait_tts_done.py` 等 `status=0` | 别处 |
| `speak_listen` / `listen`（角色侧语音四件套） | 设备侧 ASR（`status=2` final） | 收音同 `run_asr.sh`：收到第一条最终结果就走 | **自己**（播报之后由 `run_asr.sh` 唤醒） |
| `run_asr.sh`（asr-listener） | 设备侧 ASR（`status=2` final） | 收到第一条最终结果就走 | **自己**（会 abort 收尾） |
| **`audio_interact`** | 设备侧 ASR（`status=2` final） | 播报段与 `speak` 同规则（`speak_and_wait`：服务在时等 `drained`，不在时 `wait_done=true` 的 `status=0`）；收尾段见上节 | **自己**（静默唤醒，退出时 abort） |

播报那一段借的就是上面 `speak` 那条规则（本次调用整段交一句、等服务说队列见底；服务不在才退回
脚本那条 `status=0`）；本技能只剩收尾那一条自定义判据。`speak` / `speak_listen` 那边的播完判定
不受影响。

## 跑之前要知道的副作用

这是**会真的动设备**的技能，验证时别拿它当靶子：

- 会**出声**（`play_tts`），会**真的唤醒设备**（`Wakeup` 发到 live 域）；**播报先出声、
  唤醒后收音**，两句之间不重叠（本地 TTS 路没有边播边听，见上文）
- 会**临时改写运行中安装里的唤醒回应音频**（那 2.5s 内所有人的唤醒回应都是静音的）
- 退出时**会调 abort**，掐掉当前会话（空闲时无副作用）
- 域与 DDS 实现**固定按 configs**，不接受环境变量覆盖（播报/唤醒子进程自己也按 configs 设域，
  允许覆盖就会造出"订阅在域 A、出声在域 B"的错配）。要旁听请在**另一个域**上用
  `test/watch_status.sh`（纯订阅、可换域；它也能看到 `9/1/2/3/11/0` 这一串，用来确认收音链路）
- 不流式：`dsh-plugins/speech-stream` 的 `SPEECH_TOOLS` 只含 `speak` / `speak_listen` /
  `speak_input`，**不含 `audio_interact`**，文本整段生成完才出声

## 工具层

`plugin_tools/available_tools.yaml` 的 `audio_interact`（在 `INTERACTOR_TOOLS` 白名单里）→
`plugin_tools/tool_executor.py` 的 `audio_interact()`：起本技能的脚本、解析 stdout 那行 JSON
（这两步与等待窗口在 `plugin_tools/audio_tools.py` 的 `run_audio_interact()`），结果渲染成
`[本地语音] 用户说：<文本>` 或 `[本地语音] 未收到用户语音（<结束原因>）`。

它对云端报两次「正在等待用户回答」：开口**前**置 `waiting_for_user=true`、收工（含超时与异常，
复位在 `finally`）后置 `false`，走 `Communicator.sender.report_waiting_state`。该工具 schema 里没有 `task_id`、
系统也不再注入 → 拿不到任务号就整体空转，不上报。

执行期间存在的 `/tmp/.fdx` 标记由**脚本**自己管（`audio_interact.sh` 头 touch、`trap ... EXIT`
清），工具层只在超时那条路上补一刀：子进程被 SIGKILL 时 trap 跑不到，得由调用方 `unlink`，
否则标记会留下来让外部消费方以为工具还在跑。
