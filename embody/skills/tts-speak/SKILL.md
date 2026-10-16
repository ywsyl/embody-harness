---
name: tts-speak
description: 使用TTS服务通过扬声器播报语音的技能。支持文本转语音并播放，可设置播放参数。
---

# TTS语音播报技能

使用TTS服务通过扬声器播报语音的技能。

## 执行脚本
```bash
bash skills/tts-speak/scripts/run_tts_speak.sh "<text>" [multi_wheel] [play_now] [wait_done]
```

只等一次**已经在放**的播报播完（播报不是这个脚本发出去的，拿不到 `section_id`）：
```bash
bash skills/tts-speak/scripts/wait_tts_done.sh "<text>"
```

**这两条脚本是回退路径与手动入口**：正常情况下播报走**常驻 TTS 服务**
（`TTS_Manager/tts_service.py --serve`，由 `start_agent.py` 拉起，unix socket
`runtime_files/tts_speak.sock`；全体工具的入口是 `TTS_Manager/tts_client.py`，
连不上才自己退回这两个脚本）。为什么必须常驻：逐句各起一个进程时，每句要付 bash 前导 +
configs 桥 + `ros2 service call` 的 CLI 启动与 DDS 发现，2026-10-12 实测合计约 **2.1 秒**，
而设备从收到请求到第一个音频包只要 **0.463 秒** —— 那 2.1 秒就是用户听到的句间空档
（`/root/log/speechcore.20261012.log`：第一句 10:18:42.795 播完 → 第二句 10:18:45.311 才
`runInquiry`）。两条 shell 入口共用一份环境前导 `scripts/lib-tts-env.sh`。

`wait_tts_done.sh` 的判据是「保护期后话题上出现一次 `status=0 Idle`」—— 判定代码全仓只有
一份，在 `TTS_Manager/speech_status.py`，本脚本的 `wait_tts_done.py` 只是它的薄壳。
**只在服务不在时用到**（服务没起 / `--no-tts-service` / 插件没装载）。话题上的一次 Idle
之所以不能直接当「整段播完」：设备把几条请求排成一队逐条播时，**一条播完与下一条开始之间
它也会短暂回 Idle**（2026-10-12 实测每句各有一条 Idle，紧接着下一句 `runInquiry`）。
常驻服务等的是「队列见底」= 待发为空 ∧ 无在飞调用 ∧ 最后一次交接之后收到过 Idle 且静默
`DRAIN_SETTLE_SEC`（`drained` 事件）；插件在时工具层等的是记录文件里的 `drained_at`
（同前一条事实，见 `plugin_tools/audio_tools.py` 的 `_wait_streamed_done`）。

## 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `text` | string | 必填 | 要播报的文本内容 |
| `multi_wheel` | bool | `false` | 是否多轮播报（透传给服务的 `multi_wheel`） |
| `play_now` | bool | `false` | `false`（默认）= 不抢麦（但**挡不住**下一条播报停掉它，见下节）；`true` = 打断在播的那条立刻播（要抢麦时用） |
| `wait_done` | bool | `false` | 是否等播报**真正播完**再返回。`true` 时等状态话题出现 `status=0 Idle`（见下）后才退出 |

> 工具层（`speak` / `speak_listen` / `speak_input`）的默认值不完全相同：`speak` 的
> `wait_done` 默认 **false**（发出请求即返回，不阻塞），`speak_listen` / `speak_input` 的
> `wait_done` 默认 **true**（必须等播完再开始收音/读终端，否则会把没播完的语音掐断）。
> `play_now` 三处都按本表的默认 **false**：只有 `speak` 在 schema 里暴露这个开关，
> `speak_listen` / `speak_input` 写死 false；`audio_interact` 的位置参数也填 `false`
> （2026-10-09 起不再抢麦 —— 它后面紧跟的唤醒本来就够掐断）。

## 播报结束怎么判定

`play_tts` 是**异步流式播放**：服务 1~3 秒就返回，音频却还在继续放几十秒。
所以"服务返回"不等于"播报结束"。判据全仓只有一份：`TTS_Manager/speech_status.py`
（订阅 `/homi_speech/speech_assistant_status_topic`，等到 **`status=0 (Idle)`**）。
常驻服务用它判「队列见底」并广播 `drained`；回退路径上由 `scripts/wait_tts_done.py`
起节点订阅、把事件喂给同一个对象（`DoneWatcher`）。

实测（2026-09-15，麦克风声学对照）：

| 状态码 | 含义 | 实测位置 |
|--------|------|----------|
| `3` AsrTextout | 分句**合成就提交播放** | 全在播报开头 1~2 秒内出现，**不是**播完信号 |
| `7` DisplayError(msg=NoError) | 会话收尾 | 实际出声停止之后 |
| `0` Idle | 会话/播报结束 | 同上，随文本长度线性增长：3字→4.57s，38字→8.92s，143字→29.30s |

原方案是按文本字数估算时长再 `sleep`（`configs.configs` 的 `TTS_RATE` /
`TTS_BASE` / `TTS_FLOOR`），实测偏短 —— 143 字估 28.6s、实测 29.30s；38 字估
7.6s、实测 8.92s。提前返回会让随后的 ASR `Wakeup` 给正在播放的 TTS 下发
UserCancel（`status=8`），把没播完的语音掐断。

现已**完全改用状态码判定**：那三个估算参数连同估算代码一并删除，不再有任何
"猜时长"的分支。等不到 `status=0` 时不再退化成估算值，而是等满等待上限
（`configs.configs` 的 `TTS_WAIT_DONE_MAX_WAIT`，默认 30s，须小于
`TTS_TOOL_TIMEOUT`）后如实报超时。宁可多等，也不提前返回把播报掐断。

## audio_interact 工具

不在本技能里，已独立成 `skills/audio_interact/`（它还要发唤醒、还要等状态收尾，不是 TTS 的
附属）。它**经 `TTS_Manager/tts_client.py` 的 `speak_and_wait` 发播报**（服务不在时那边退回
本技能的 `run_tts_speak.sh`），而且**先播报、后唤醒**：本技能的播报
服务开口前会停掉当前语音会话、另起一个不收音的 TTS 会话（见
`skills/audio_interact/SKILL.md` 的实测记录），先唤醒就收不到音。播报那一段它是**等播完**
才往下走（`speak_and_wait`；服务在时等的是 `drained`，不在时是脚本 `wait_done=true` 那份
`status=0 Idle`），`multi_wheel` / `play_now` 都取默认 `false`（不抢麦）；**收尾那一段**才是
它自己的判据（「唤醒后这一轮动过（1/2/3/9）→ 11/0 → 再等用户回复」），**两者刻意不合并**。

## 排队：客户端等播完再送下一句（设备**没有**队列）

客户端那套队列早就不在了（原先的「自建 deque + 常驻 worker 线程 + 按字数估算的
`threading.Timer`」整体删除），但**设备也没有队列**：

- 每来一条新 `play_tts` 请求，`speech_core` 都先 `speech app stop` 停掉在播的那条，再
  `runInquiry` 说新的一句。2026-10-12 15:37~15:39 三次实测：前一句分别在 **75ms / 96ms /
  185ms** 处被后一句的请求掐断（`speech app stop called notifyUserAbort: 0` → `UserAbort_e`
  → `displayError UserCancel`），用户听到的就是「只播了后半句」。
- ⇒ 连播只能由**客户端**保证：这一句播完了再送下一句。常驻 TTS 服务与交互器插件就是这么
  做的（`configs.SPEECH_DELIVERY_MODE="serial"`：插件等服务广播的 `drained` 再交下一句）。
  代价是句间多出约 0.8s（判定静默 0.5s + 设备 stop/runInquiry 约 0.3s），仍远好于逐句起
  进程时的 2~3s。
- 把整段话**一次**交给设备仍是句间最紧的路 —— 设备自己逐句连播、几乎没有空档，
  `audio_interact` 与 `speak` 的整段兜底走的就是它。

> 2026-10-12 上午曾据厂商库那句 `play_now: True=打断当前立即播；False=排队`
> （`/usr/bin/cmcc_robot/install/claw_client/lib/python3.8/site-packages/claw_client/
> hermes_bridge.py:2402`）判定「排队发生在设备侧、客户端可以不等播完就交下一句」，据此把默认
> 改成 `pipeline`，连 2026-10-09「设备不排队」那条结论也一并作废了。当天实测打回：
> `play_now=false` 只表示**我们不发** stop，挡不住设备自己那条 stop。`pipeline` 只有在设备
> 改成真排队之后才成立。

调用方语义对应关系：

| 想要的效果 | 传参 |
|---|---|
| 接着播、不切在播的那条（自己等它播完再发） | `play_now=false`（默认） |
| 打断在播的、立刻播 | `play_now=true` |
| 等这条播完再往下走 | `wait_done=true`（服务在时等的是 `drained`，不在时是 `status=0 Idle`） |

## 技术细节

- **TTS服务**: `/audio_center/play_tts`（`homi_speech_interface/srv/AssistantSpeechText`）
- **ROS_DOMAIN_ID**: 2
- **服务字段**: `msg` 为播报文本，`multi_wheel` / `play_now` 为播放参数
- **错误码**: 可为负数（如 `-20` 超时、`-99` 等待超时）；非 0 一律按失败处理

### 调用来源

`AssistantSpeechText.srv` **没有「调用来源」字段**（实测字段只有 `msg` / `multi_wheel` / `play_now` / `tts_session` / `nlu_session`，端上二进制也只解析 `tts_session` 里的 `speed` / `voiceId`）。接口不支持，因此**不传入来源，也不做任何本地留痕**。

## 返回值

成功时 **stdout 为空**（退出码 0），进度/诊断日志（`[TTS] 正在合成语音`、`[TTS] 播报完成, section_id=...` 等）打到 **stderr**，供终端与调试使用。
调用方（`TTS_Manager/tts_client.py` 的回退路径）通过退出码判断成败，不依赖 stdout 内容。
