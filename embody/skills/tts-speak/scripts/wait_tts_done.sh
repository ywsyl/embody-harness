#!/bin/bash
# 只等一次**已经在放**的播报播完 —— 播报不是本脚本发出去的，拿不到 section_id。
#
# 谁用它：plugin_tools/audio_tools.py 的播报实现（入口是 tool_executor.py 的播报工具）。交互器的 dsh 插件
# （dsh-plugins/speech-stream）会把 speak / speak_listen / speak_input 的 text 参数
# 边生成边送进 TTS，于是这些工具被派发时那段话可能已经在放了；工具不再重发，但
# wait_done=true 的语义必须保住 —— 尤其 speak_listen：不等播完就收音，会把机器自己的
# 声音收进去（ASR 的 Wakeup 还会给在播的 TTS 下发 UserCancel）。
#
# 环境引导与 run_tts_speak.sh 同源（同一份 configs 事实源 + 同一个运行中的安装），
# 判定逻辑只有一份：TTS_Manager/speech_status.py 的 DoneWatcher（status=0 Idle；
# 没有 section_id 时在保护期后接受 Idle），本目录的 wait_tts_done.py 是它的薄壳。
#
# 用法: ./wait_tts_done.sh "<text>"     # text 只用于日志，判定不依赖文本长度
# 输出约定与 run_tts_speak.sh 一致：诊断走 stderr，成功时 stdout 保持为空。
set -e

# ROS 环境与 DDS 参数：与 run_tts_speak.sh 共用一份前导（缺环境时它会结束本脚本）。
source "$(dirname "${BASH_SOURCE[0]}")/lib-tts-env.sh"

TEXT="${1:-}"

# 恒退出 0（等不到就按上限返回，见 wait_tts_done.py）：等待失败不该让播报算失败。
python3 "$(dirname "${BASH_SOURCE[0]}")/wait_tts_done.py" --text "$TEXT" >&2 || true
