#!/bin/bash
# TTS语音播报技能执行脚本
# 用法: ./run_tts_speak.sh "<text>" [multi_wheel] [play_now] [wait_done]
#
# wait_done=true 时，等播报**真正播完**再返回（订阅状态话题等到 status=0 Idle，
# 判定在 TTS_Manager/speech_status.py，本目录 wait_tts_done.py 是它的薄壳）。
# 默认 false：服务返回即返回。
# play_tts 是异步流式播放，服务 1~3 秒就返回而音频还在放，所以不等的话
# 后续任何 ASR/Wakeup 都会掐断没放完的语音（实测 status=8 UserCancel）。
#
# play_now 默认 false：**不主动打断**在播的那条（要抢麦、立刻打断就传 true）。
# 注意设备没有队列：连续两次调用若不等前一次播完，后一次照样会停掉在播的那条
# （实测见 SKILL.md 的「排队」一节）。
#
# 本脚本是**回退路径与手动入口**：常驻服务（TTS_Manager/tts_service.py）在时，
# 工具侧走 socket 不再起这个进程 —— 每起一个进程的 bash + configs 桥 + ros2 CLI
# 就要 ~2.1s，正是句间空档的来源（2026-10-12 实测）。
#
# 输出约定：进度/诊断信息一律打到 stderr，成功时 stdout 保持为空。
# 调用方（tool_executor 等）用 subprocess 捕获 stdout 拼工具结果，
# 冗长的进度日志进 stderr 才能避免污染返回给 LLM 的 result。
set -e

# ROS 环境与 DDS 参数：与 wait_tts_done.sh 共用一份前导（缺环境时它会结束本脚本）。
source "$(dirname "${BASH_SOURCE[0]}")/lib-tts-env.sh"

TEXT="$1"
MULTI_WHEEL="${2:-false}"
PLAY_NOW="${3:-false}"
WAIT_DONE="${4:-false}"
WAIT_SEC="0.00"     # wait_done=true 时实际等了多久，仅用于诊断输出

# 调用来源：play_tts 的 AssistantSpeechText.srv 没有来源字段（实测字段只有
# msg/multi_wheel/play_now/tts_session/nlu_session），所以不传、也不做本地留痕。

if [[ -z "$TEXT" ]]; then
    echo '[TTS] 文本内容为空' >&2
    exit 1
fi

echo "[TTS] 正在合成语音: $TEXT" >&2

# 单引号按 YAML 单引号字符串规则转义（'' 表示字面单引号），否则 {msg: '...'} 会被拆坏导致服务调用失败
ESCAPED_TEXT=${TEXT//\'/\'\'}

TTS_SERVICE="$(cmcc_path ROS_TTS_SERVICE)" || {
    echo '[TTS] 取不到 configs/paths.py 的 ROS_TTS_SERVICE' >&2
    exit 1
}

# 用 if ! 捕获调用结果：命令在条件分支中不受 set -e 影响，失败时能保留真实报错并打印
if ! TTS_RESULT=$(ros2 service call "$TTS_SERVICE" homi_speech_interface/srv/AssistantSpeechText \
    "{msg: '$ESCAPED_TEXT', multi_wheel: $MULTI_WHEEL, play_now: $PLAY_NOW}" 2>&1); then
    echo "[TTS] 服务调用失败: $TTS_RESULT" >&2
    exit 1
fi

# 解析 Python repr 格式响应: section_id='xxx', error_code=0
# 错误码可为负数（如 -20 超时、-99 等待超时），字符类必须带上 '-'，
# 只写 [0-9] 会把负号留在原地、捕获到空串，导致错误码丢失（报错变成"错误码: "）。
ERROR_CODE=$(echo "$TTS_RESULT" | sed -n 's/.*error_code=\([-0-9]\{1,\}\).*/\1/p' | head -1)
SECTION_ID=$(echo "$TTS_RESULT" | sed -n "s/.*section_id='\([^']*\)'.*/\1/p" | head -1)

# 解析不出错误码说明响应格式不是预期样子（服务未起/返回体异常），单独报错便于排查
if [[ -z "$ERROR_CODE" ]]; then
    echo "[TTS] 无法从服务响应中解析 error_code: $TTS_RESULT" >&2
    exit 1
fi

if [[ "$ERROR_CODE" != "0" ]]; then
    echo "[TTS] 服务返回错误码: $ERROR_CODE" >&2
    exit 1
fi

# 等播报真正播完再返回（默认不等）。判定逻辑只有一份，在
# TTS_Manager/speech_status.py 的 DoneWatcher（status=0 Idle），由同目录
# wait_tts_done.py 起节点订阅话题喂给它。
# 该脚本恒退出 0（等不到就按兜底时限返回），故不会让播报本身算失败。
if [[ "$WAIT_DONE" == "true" ]]; then
    WAIT_START=$(python3 -c 'import time; print(time.time())')
    python3 "$(dirname "${BASH_SOURCE[0]}")/wait_tts_done.py" \
        --text "$TEXT" --section-id "$SECTION_ID" >&2 || true
    # 计时只为日志，取不到就退回 0.00 —— 不能因为计时失败让播报算失败
    # （set -e 下命令替换失败会直接退出脚本，故显式兜底）
    WAIT_SEC=$(python3 -c "import time; print('%.2f' % (time.time() - $WAIT_START))") || WAIT_SEC=""
    if [[ -z "$WAIT_SEC" ]]; then
        WAIT_SEC="0.00"
    fi
fi

echo "[TTS] 播报完成, section_id=$SECTION_ID (wait_done=$WAIT_DONE, 等待 ${WAIT_SEC}s): $TEXT" >&2
