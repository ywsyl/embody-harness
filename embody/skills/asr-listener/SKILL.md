---
name: asr-listener
description: 语音拾音与ASR识别技能。发布Wakeup消息到/audio_center/wakeup_event主动拉起收音，订阅/homi_speech/speech_assistant_status_topic提取status=2(AsrResponsed)且finish=true时的result.text。主动触发时临时静默唤醒回应（真实语音唤醒播报不受影响）。已在机器狗上实测验证。
---

# ASR 语音识别技能

主动拉起收音并提取 ASR 识别文本。

## 执行脚本

**一键识别（主动触发 + 返回结果）**
```bash
bash skills/asr-listener/scripts/run_asr.sh
```

**常用参数**
```bash
# 15秒超时
bash skills/asr-listener/scripts/run_asr.sh --timeout 15

# 仅监听不触发（机器狗已用语音唤醒时）
bash skills/asr-listener/scripts/run_asr.sh --no-trigger

# 打印完整状态流调试
python3 skills/asr-listener/scripts/asr_monitor.py --verbose
```

## 状态监视（实时监督语音助手状态变化）

只订阅、不发任何消息，用来实时看「唤醒 → 收音 → ASR → 播报 → 收尾」整条链路。
两个脚本放在 **`test/`**（本地目录，见 `embody/.gitignore`，不入库）：

```bash
bash test/watch_status.sh              # 一直监视，Ctrl-C 结束
bash test/watch_status.sh -t 30        # 只监视 30 秒
bash test/watch_status.sh --section-id abc123   # 只盯某一轮会话（话题是共享的）
ROS_DOMAIN_ID=99 bash test/watch_status.sh      # 换域（默认 configs.ROS_DOMAIN_ID）
```

- `test/watch_status.sh` 只是启动壳：source `configs.paths.ROS_SETUP` 与运行中的机器人安装
  （`AssistantEvent` 的消息定义只有那儿是最新的），再按 configs 设 `ROS_DOMAIN_ID` /
  `RMW_IMPLEMENTATION`，最后拉 `test/status_watch.py`。**已 source 过 ROS 的 shell 里可以跳过它**
  直接 `python3 test/status_watch.py`
- 两个脚本都按「往上找 `configs/`」定位仓库根，不数固定层数，换目录/换 cwd 都不影响

每条事件打印本机时刻、距本会话上一条的 Δt、status 码与名称、`section_id`、`msg`：

```
10:26:43.005  Δ=   0.351s  status= 2 AsrResponsed(收听中)   sec=test-1  msg='你好'(流式)  ↳1→2
10:26:44.406  Δ=   0.351s  status= 4 AsrEnd(收听结束)       sec=test-1    ↳3→4
              ✦ 会话 section_id='test-1' 结束：历时 2.45s，事件 8 条，ASR 最终结果='你好，请讲'，播报分句 2 条
```

- `status=2` 的 msg 是 JSON，脚本解开成 `result.text` 并标 **(流式)/(最终)**；`status=3` 的 msg 是回复文本
- **话题是全局共享的**：云端 NLP、离线指令等别处发起的会话也发在这里，区分依据只有
  `section_id`（协议没有"调用来源"字段）。同名会话首次出现时打一行「新会话」分隔线
- 状态码名称取自 `configs.AUDIO_STATUS_NAMES`（全仓唯一事实源），脚本不另抄一份；
  `status=3`/`status=0` 首次出现时补一句实测语义（3 不是播完信号、0 才是）
- 退出时打印小结：状态码分布 + 每个 session 的历时/事件数/ASR 结果；`--no-summary` 可关
- `--heartbeat 30`（默认）在完全没有状态变化的周期里报一声「监视中」，0 = 关；
  参数见 `--help`

**超时值的归属**：收音窗口的**唯一来源是 `configs.ASR_LISTEN_WINDOW`** ——
`asr_monitor.py` 的 `--timeout` 默认值直接从那里 import（脚本内不再硬编码秒数）。
要改收音窗口就改这一处。工具层另有一个子进程封顶 `configs.ASR_TOOL_TIMEOUT`
（= `ASR_LISTEN_WINDOW + 5`，读者是 `plugin_tools/audio_tools.py` 的 `_run_asr`）：
它只封子进程总时长、**不传 `--timeout`**，所以收音窗口仍然只有上面那一个来源，两处不会漂移。

## 唤醒回应播报控制（条件性静默）

机器狗唤醒后默认会播报一句回应（"在呢"/"怎么了"）。该回应由 speech_core 用
**AlsaHelper 直接 ALSA 播放**，绕过 audio_player，因此 stop_play 服务无法打断。

**默认行为（推荐，符合"真实唤醒留播报、主动触发静默"）：**
- 用户用语音唤醒 → **保留**"在呢"等语音回复
- 本技能主动触发 ASR → **无**语音播报

实现方式：主动触发前把 wakeup04~10.wav 临时替换为静音，等回应窗口（最长 1.5s）
过去后立即从备份恢复。speech_core 每次播放时读文件，替换立即生效。

```bash
# 默认：主动触发静默（无需任何参数）
bash skills/asr-listener/scripts/run_asr.sh

# 若某次主动触发想保留回应播报
bash skills/asr-listener/scripts/run_asr.sh --keep-reply

# 调整静默窗口（默认 2.5s，需盖过最长回应音频 1.5s）
bash skills/asr-listener/scripts/run_asr.sh --mute-reply-wait 3.0
```

> 主动触发前若备份缺失，脚本会自动把当前非静音音频备份到
> `backup_wakeup/`（已存在则不覆盖）。异常/中断也会兜底恢复，不会停留在静音态。

> **父进程侧的额外保险**：脚本被 SIGKILL 时它自己的 `finally` 来不及跑，恢复就得由
> **调用方**补做 —— 杀掉整个进程组（避免留下占用 ASR 会话的孙进程），再调一次
> `restore_wakeup_reply.sh`（幂等，重跑无副作用）。
>
> 工具层就是这么做的：`plugin_tools/audio_tools.py` 的 `_run_asr`（listen / speak_listen
> 用它收音）超时后 `killpg` 整个进程组、有上限收尸、再补调恢复脚本。自己手工起本脚本时
> （下面的用法）要自己承担这条兜底。

### 为什么不用 AssistantQuiet `mode=2` 来静音（实测否掉了）

`AssistantQuiet` 的 `mode` 有 0 关闭 / 1 ASR+NLP / 2 仅 ASR 三档，看起来
`mode=2` 能"只收音不回复"，但它**静音不了唤醒回应**：

- 实测 `mode=2` 下唤醒回应 `wakeup05.wav` **仍被 speech_core 打开播放**
  （`lsof -p <speech_core pid> | grep res/wakeup\*.wav` 可证，播放期间保持打开）
- `mode=2` 实际只是**不做 ASR 之后的 NLP/TTS 后续**（识别完立刻 `status=7+0` 收尾），
  而唤醒回应是在 ASR 之前就播的，不受它管
- 另外 `mode` 只在**运行中的安装** `/usr/bin/cmcc_robot/install` 里是 `int32 mode`，
  技能脚本 source 的 `/root/cmcc_robot` 是陈旧副本，那边还是旧的 `bool enable`

**结论：静音文件方案（上一节）必须保留，不要用 `mode=2` 替代。**

如确需**永久**静默所有唤醒回应，可用旧脚本（可逆，已备份）：

```bash
bash skills/asr-listener/scripts/mute_wakeup_reply.sh   # 永久静默
bash skills/asr-listener/scripts/restore_wakeup_reply.sh  # 恢复
```

## 工作流程

```
发布 Wakeup 到 /audio_center/wakeup_event   ← 主动拉起收音
   ↓
status=1 (Running) → status=9 (Wakeup) → status=2 (AsrResponsed)
   ↓
finish=true 时取 msg JSON 里的 result.text   ← 最终识别文本
   ↓
调 assistant_abort_service 结束会话          ← 不再进入下一轮收音
```

## 技术细节

### ROS 接口（实测可用）

| 操作 | 类型 | 路径 | 消息类型 |
|---|---|---|---|
| 触发拾音 | Topic (pub) | `/audio_center/wakeup_event` | `homi_speech_interface/msg/Wakeup` |
| 监听状态 | Topic (sub) | `/homi_speech/speech_assistant_status_topic` | `homi_speech_interface/msg/AssistantEvent` |
| 会话收尾 | Service (client) | `/homi_speech/assistant_abort_service` | `homi_speech_interface/srv/AssistantAbort` |

### 会话收尾（为什么必须调 abort）

**实测：Wakeup 之后助手不会停在"播完一句"就结束，而是进入多轮对话**——每轮给 16 秒
静音窗口（`sppech_round1_waitMs` / `sppech_roundn_waitMs`），不说话就进入下一轮，
终止条件是云端判定结束、每轮静音超时、或外部 stop/abort。对本技能这种「单次收音」
语义来说，不收尾就会一直挂着收音。

因此**主动触发时**在收尾阶段调 `/homi_speech/assistant_abort_service`：

- 该服务由 `/homi_speech/helper` 提供（不是 speech_core），请求为空、响应只有
  `int32 error_code`
- 作用是**硬停止当前会话**：端上表现为 `5 Aborting → 7 DisplayError(UserCancel)
  → 0 Idle`，正在播的 TTS 会被掐断
- **空闲时调用是空操作**，所以可以无条件调
- 放在 `finally` 里，**识别成功 / 超时 / 异常都会收尾**；调用失败只告警，
  不影响识别结果与退出码
- `--no-trigger`（只听正在进行的真实语音对话）**不调**，避免掐断用户自己的会话
- 另：`status=5 (Aborting)` / `status=8 (UserAbort)` 出现即视为会话已中止，
  脚本立即结束等待，不再干等到超时

### AssistantEvent status 状态码（int32）

| status | 含义 |
|---|---|
| 0 | Idle 待机（会话结束、**播报真正播完**都看它） |
| 1 | Running 对话进行中 |
| 2 | AsrResponsed 收听中（msg 是 ASR 结果 JSON） |
| 3 | AsrTextout 转录/播报中（分句**合成就提交播放**，不是播完信号，全在开头 1~2 秒内出现） |
| 4 | AsrEnd ASR 结束 |
| 5 | Aborting 对话中止 |
| 6 | OfflineInstructionMatched 离线指令 |
| 7 | DisplayError 显示错误（会话收尾时 msg 为 `NoError`） |
| 8 | UserAbort 用户中止（ASR Wakeup 会掐断正在播放的 TTS，即此码） |
| 9 | Wakeup 唤醒/开始收音 |
| 11 | 静默等待（机器人回复完、用户没开启新一轮对话；再等 x 秒仍无指令，端上判这轮结束 → 0） |

码表的唯一事实源是 `configs/configs.py` 的 `AUDIO_STATUS_NAMES`（本表与它同源，改一处即可）。

关于 `status=0` 与 `status=8` 的实测细节，见
`skills/tts-speak/SKILL.md` 的「播报结束怎么判定」一节；`audio_interact` 工具
（`skills/audio_interact/`）复用本技能的 `asr_monitor.ASRListener` 发唤醒与收尾，
但它自己的状态判据是「唤醒后这一轮动过（1/2/3/9）→ 收到 11/0 → 再等用户回复」，
且**先播报、后唤醒**（本地播报服务会拆掉当前语音会话，先唤醒就收不到音），见那份 SKILL.md。

### ASR 文本提取

`status=2` 时 `msg` 是 JSON：

```json
{"code":0,"result":{"finish":true,"index":8,"text":"识别文本"},"voiceId":"..."}
```

- `finish=false` → 流式中间片段（忽略）
- `finish=true` → **最终结果**，取 `result.text`

### 调用来源标识

`Wakeup` 消息没有专门的「调用来源」字段（只有 `ivw_word` / `angle` / `extra_info`），
因此用既有的通用扩展字段 `extra_info` 承载，发布时填一段 JSON：

```json
{"source": "local-agent"}
```

- `source` 默认 `local-agent`，**调用方可用环境变量 `ASR_CALLER` 覆盖**
- `extra_info` 在仓库内无消费方（实测全仓库无读取处），承载来源不影响收音流程
- 仅**主动触发**（`run_asr.sh` 默认行为）会填；`--no-trigger` 纯监听模式不发布 Wakeup，
  也就没有来源可言

## 环境变量

- `ROS_DOMAIN_ID=2`
- `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`
- `ASR_CALLER`：覆盖 Wakeup `extra_info` 里的调用来源，默认 `local-agent`

## 返回值

- 识别成功：输出 `[ASR] <识别文本>`
- 超时/静音：输出 `[ASR] 未识别到语音内容`
