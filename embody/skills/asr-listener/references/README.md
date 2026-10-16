# ASR 技能参考文档（已在机器狗上实测验证）

> 本文件记录的是实测可用的接口，与《对外能力接口文档》章节号对应，
> 但 topic/service 名以运行中的机器狗为准。

## 实测可用接口（来源：2026-08-17 在真实机器狗上 `ros2 service list` / `topic list` 探测）

### 触发拾音（主动拉起收音）⭐

- **类型**: Topic（发布）
- **路径**: `/audio_center/wakeup_event`
- **消息类型**: `homi_speech_interface/msg/Wakeup`

```
string ivw_word     唤醒词，如 "小智小智"
int32  angle        声源角度，填 0
string extra_info   附加信息，留空
```

### 监听语音对话状态（核心）

- **类型**: Topic（订阅）
- **路径**: `/homi_speech/speech_assistant_status_topic`
- **消息类型**: `homi_speech_interface/msg/AssistantEvent`

```
int32  status        状态码
string description   状态描述
string msg           JSON，status=2/3 时含有效内容
string section_id    会话ID
```

### status 状态码（int32）

| status | 含义 | msg 内容 |
|---|---|---|
| 0 | Idle 待机 | - |
| 1 | Running 对话进行中 | - |
| 2 | AsrResponsed 收听中 | ASR 结果 JSON |
| 3 | AsrTextout 转录/播报中 | 回复文本 |
| 7 | DisplayError 显示错误 | UserCancel 等 |
| 9 | Wakeup 唤醒 | - |
| 11 | 静默等待 | - |

### ASR 文本提取（status=2 时，msg 是 JSON）

```json
{"code":0,"result":{"finish":false,"index":1,"text":"这"},"voiceId":"iat000dae90@..."}
{"code":0,"result":{"finish":true,"index":8,"text":"最终文本"},"voiceId":"iat000dae90@..."}
```

- `finish=false` → 流式中间片段（忽略）
- `finish=true` → **最终结果**，取 `result.text`

## 静默唤醒回应 ⭐⭐

### 问题

唤醒后机器狗会播报一句回应（"在呢"/"怎么了"）。实测发现该回应由 speech_core 用
**AlsaHelper 直接 ALSA 播放**（二进制内有 `AlsaHelper.h`、`snd_pcm_format_set_silence`、
`alsa helper is preempted` 等字符串），**绕过 audio_player 节点**。

证据：唤醒时 `/audio_player/play_end` 和 `/audio_player/player_state` 均无消息，
而手动调用 `/audio_player/play_wav` 播放 wav 时这两个话题正常发布事件。

因此 `/audio_center/stop_play`（StopWav）**无法**打断唤醒回应：

| play_id | 语义 | 效果 |
|---|---|---|
| -1 | 停止 speech_core | 取消整个对话（UserCancel） |
| -2 | 停止 audio_player 离线音频 | 停不到唤醒回应（它不走 audio_player） |
| -3 | 停止两者 | 同样无效 |

### 解决方案：条件性替换唤醒回应音频为静音

唤醒回应音频真实路径（一律经 `/usr/bin/cmcc_robot/install` 这个「运行中安装」的符号链接
取，别写具体安装目录——指到 speech_core 不读的那棵树就是静默失效）：

```
/usr/bin/cmcc_robot/install/homi_speech/share/homi_speech/launch/res/wakeup04.wav ~ wakeup10.wav
```

格式：PCM S16LE / 16kHz / mono。

speech_core 每次播放时读文件（lsof 显示它只保持打开最近播放的一个文件，非启动时全部加载），
因此替换文件内容后立即生效，无需重启。

**条件性静默（默认）：** `asr_monitor.py` 主动触发前把 7 个 wav 临时替换为静音，
发布 Wakeup 后等待 `--mute-reply-wait`（默认 2.5s，盖过最长回应 wakeup04=1.519s），
再从 `backup_wakeup/` 恢复。效果：

| 触发方式 | 唤醒回应播报 |
|---|---|
| 用户语音唤醒 | 保留（"在呢"等正常播报） |
| 脚本主动触发（默认） | 静默 |
| 脚本主动触发 + `--keep-reply` | 保留 |

时序要点：
- speech_core 在**播放时**读取 wav 文件，所以发布前换成静音 → 主动触发不播报；
  恢复后再唤醒 → 播报正常。
- 静默窗口只覆盖主动触发后 ~2.5s，之后文件即恢复，真实语音唤醒不受影响。
- 备份逻辑：`_ensure_backup()` 仅在备份缺失且当前文件**非静音**时备份
  （避免把静音当原始音频），已存在备份不覆盖；异常/中断由 `finally` 兜底恢复。

脚本：
- `asr_monitor.py` — 内置条件性静默（主动触发默认静默、触发后自动恢复）
- `mute_wakeup_reply.sh` — 旧方案，永久静默所有唤醒回应（备份 + 替换 0.3s 静音 wav）
- `restore_wakeup_reply.sh` — 从备份恢复原始唤醒回应

### 试过但不行：用 AssistantQuiet `mode=2` 静音（2026-09-15 实测否掉）

`AssistantQuiet` 的 `mode`（0 关闭 / 1 ASR+NLP / 2 仅 ASR）看着能"只收音不回复"，
但**静音不了唤醒回应**：

- `mode=2` 下 `wakeup05.wav` **仍被 speech_core 打开播放**
  （`lsof -p <speech_core pid> | grep res/wakeup\*.wav`，播放期间保持打开）
- `mode=2` 只是不做 ASR 之后的 NLP/TTS 后续（识别完立刻 `status=7+0` 收尾）；
  唤醒回应在 ASR **之前**就播了，不受它管
- 另外这条路径上必须用**运行中的安装** `/usr/bin/cmcc_robot/install/setup.bash` 发服务，
  技能脚本 source 的 `/root/cmcc_robot` 是陈旧副本，那边 `AssistantQuiet` 还是旧的
  `bool enable`，字段名不同（见本文开头关于两个安装目录的说明）

**结论：上面的静音文件方案继续用，不要改成 `mode=2`。**

## 实测完整状态流转

```
发布 Wakeup 到 /audio_center/wakeup_event
   ↓
status=1 (Running)
   ↓
status=9 (Wakeup)
   ↓
status=2 (AsrResponsed) × N  (finish=false 流式中间)
   ↓
status=2 (AsrResponsed) finish=true  → 取 result.text（ASR 最终结果）
   ↓
status=3 (AsrTextout)  → 对话/NLU 回复文本
```

## 关于 AssistantCtrl 服务（实测不触发拾音）

`/homi_speech/speech_assistant_ctrl_service`（`AssistantCtrl`）虽然存在，且 `start=true` 返回 `error_code=0`，
但**实测未触发拾音**（无 status 事件、event_id 为空）。主动拉起收音请用 Wakeup 话题方式。

## 其他相关 service（探测到，供参考）

| 服务 | 类型 | 用途 |
|---|---|---|
| `/homi_speech/assistant_enable_service` | AssistantQuiet | 开关语音助手 (enable) |
| `/homi_speech/assistant_abort_service` | AssistantAbort | 强制结束对话 |
| `/homi_speech/assistant_speech_text_service` | AssistantSpeechText | TTS 播报 |
| `/audio_center/set_wake_event_service` | SetWakeEvent | 开关语音唤醒 |
| `/audio_recorder/diy_wakeup_service` | SetDiyWord | 自定义唤醒词 |

## 唤醒回应播报配置来源

`/homi_speech/speech_core` 节点的 `speech_resourse_file_config` 参数，定义在：

- 源: `/usr/bin/cmcc_robot/install/homi_speech/share/homi_speech/launch/config/speech_res.yaml`

`Wakeup` 数组指向 `wakeup04.wav` ~ `wakeup10.wav`。

## 麦克风原始流（可选，绕过上层 ASR）

| Topic | 说明 |
|---|---|
| `/audio_recorder/pcm_stream` | 降噪后音频 |
| `/audio_recorder/pcm_origin` | 原始音频 |

格式：PCM S16LE 16kHz 1channel

## 对比 TTS

| | TTS | ASR |
|---|---|---|
| 接口类型 | Service | Topic pub + Topic sub |
| 路径 | `/audio_center/play_tts` | `/audio_center/wakeup_event` + `/homi_speech/speech_assistant_status_topic` |
| 通信模式 | 同步请求-响应 | 异步发布 + 订阅 |
| 触发方式 | 直接调用 | 发布 Wakeup 消息 |
| 结果位置 | 响应 section_id | msg JSON 的 result.text |
