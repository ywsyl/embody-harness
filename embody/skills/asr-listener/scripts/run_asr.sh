#!/bin/bash
# ASR 语音识别脚本：主动拉起收音并返回识别文本
# 用法:
#   ./run_asr.sh                    # 默认：主动触发 + configs.ASR_LISTEN_WINDOW 秒超时
#   ./run_asr.sh --timeout 15       # 显式指定超时（不传就用 configs 里的值）
#   ./run_asr.sh --no-trigger       # 不主动触发，仅监听（等语音唤醒）
#   ./run_asr.sh --wake-word 你好   # 自定义唤醒词
set -e

# 路径与 ROS 参数都取自 configs/（唯一事实源）。不写绝对路径：同一份技能在开发仓与
# 部署树（/root/embody-harness/embody）下各自指向那棵树；ROS 参数写死则改 configs 不生效。
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"

# 必须 source 正在运行的安装：脚本要调 /homi_speech/assistant_abort_service
# （srv/AssistantAbort），它的定义只在运行安装里是最新的。CMCC_ROBOT_SETUP 在
# configs/paths.py 里经 /usr/bin/cmcc_robot/install 符号链接取，不写死安装目录——
# /root/cmcc_robot 是 8/3 的陈旧副本，接口定义与运行版不同。
ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
    echo "run_asr: 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
source "$ROS_SETUP"
cmcc_require_setup "[ASR]" || exit 1

export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"
export ROS_DOMAIN_ID="$(cmcc_conf ROS_DOMAIN_ID)"

ASR_MONITOR="$(cmcc_path ASR_MONITOR_SCRIPT)" || {
    echo "run_asr: 取不到 configs/paths.py 的 ASR_MONITOR_SCRIPT" >&2; exit 1; }

# 透传所有参数给 Python 脚本
python3 "${ASR_MONITOR}" "$@"
