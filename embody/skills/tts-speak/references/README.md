# TTS语音播报技能参考文档

## 相关服务

### TTS服务
- **服务名称**: `/homi_speech/helper_assistant_speech_text_service`
- **服务类型**: `homi_speech_interface/srv/AssistantSpeechText`
- **功能**: 文本转语音服务

### 音频播放服务
- **服务名称**: `/audio_player/play_wav`
- **服务类型**: `homi_speech_interface/srv/PlayWav`
- **功能**: 播放WAV音频文件

## 服务接口详情

### AssistantSpeechText服务
```yaml
请求:
  msg: string           # 要合成的文本消息
  tts_session: string   # TTS会话标识符
  nlu_session: string   # NLU会话标识符

响应:
  section_id: string    # 段落标识符
  error_code: int32     # 错误代码
```

### PlayWav服务
```yaml
请求:
  url: string                    # WAV文件的绝对路径
  interrupt_current: bool        # 是否打断正在播放的音频
  dedicat_player: bool           # 是否独占播放
  cannot_interrupted: bool       # 是否不可被后续播放打断
  play_id: int64                 # 播放预注册ID（可选）

响应:
  ret: int64                     # 播放状态码
```

## 错误代码说明

### PlayWav服务错误代码
- **非负数**: 播放成功，返回播放ID
- **-1**: 当前播放文件不接受打断
- **-2**: 不满足独占播放条件
- **-3**: 当前播放音频独占了设备
- **-4**: 设置的play_id已在播放列表
- **-5**: url路径不是一个文件
- **-6**: url路径对应的文件头读取失败
- **-7**: url路径对应的文件不是一个有效的wav文件
- **-8**: 音视频通话中不允许播放

## 使用示例

### 基本用法
```bash
bash skills/tts-speak/scripts/run_tts_speak.sh "您好，我是小狗"
```

### 完整参数
```bash
bash skills/tts-speak/scripts/run_tts_speak.sh "您好，我是小狗" true false false true
```

### 在技能中调用
```bash
# 调用TTS播报技能
bash skills/tts-speak/scripts/run_tts_speak.sh "任务已完成" "false" "false" "false" "false"
```

## 注意事项

1. **环境依赖**: 需要先加载ROS2环境
2. **服务可用性**: 需要确保TTS服务和音频播放服务正在运行
3. **文件路径**: TTS生成的文件通常位于`/tmp/tts_*.wav`
4. **错误处理**: 脚本会自动处理各种错误情况并返回结构化结果
5. **资源清理**: 脚本会自动清理临时文件