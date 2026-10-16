#!/bin/bash
# audio_interact 工具的环境壳：source 运行中的机器人安装后拉同目录的 audio_interact.py。
#
# 为什么要有这一层：本工具要发 Wakeup（Wakeup 消息定义）、调 /audio_center/play_tts、
# 订阅状态话题（AssistantEvent 定义）、调 /homi_speech/assistant_abort_service，
# 这些接口定义只有运行中的安装里是最新的；而工具是子进程起、环境不保证干净。
#
# 用法: ./audio_interact.sh "<播报文本>" [--max-wait 50] [--keep-reply] [--verbose]
#   文本是**位置参数**（与 run_tts_speak.sh 同一个约定，audio_tools 也是这么调的），
#   本脚本替它补上 --text 再交给 python。
# 输出约定：stdout 只有一行结果 JSON（audio_tools.run_audio_interact 解析它），诊断走 stderr。
#
# 想拿别的域做验证：**别用本脚本**（域固定 configs，见下），直接起
# `python3 audio_interact.py --text ...`（同样固定 configs 域，且会自检域是否对齐）。
set -e

# 本工具执行期间存在的标记文件（跨进程的固定路径，外部约定，故不登记在 configs）。
# 头 touch、尾清掉；清收口在 EXIT trap 而不是脚本最后一行 —— set -e 下任何一步失败都会
# 直接退出，写在尾部的 rm 就跑不到了，标记会留下来让消费方以为工具还在跑。
touch /tmp/.fdx
trap 'rm -f /tmp/.fdx' EXIT

# 路径与 ROS 参数都取自 configs/（唯一事实源）。仓库根按「往上找带 configs/lib-shell-paths.sh
# 的那一层」定，不数固定层数 —— 数层数的写法一挪目录就断。
_find_repo_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [ "$dir" != "/" ]; do
        [ -f "$dir/configs/lib-shell-paths.sh" ] && { printf '%s\n' "$dir"; return 0; }
        dir="$(dirname "$dir")"
    done
    return 1
}

REPO_ROOT="$(_find_repo_root)" || {
    echo "[交互] ❌ 往上找不到 configs/lib-shell-paths.sh，本脚本需放在仓库内运行" >&2
    exit 1
}
source "$REPO_ROOT/configs/lib-shell-paths.sh"

ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
    echo "[交互] ❌ 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
source "$ROS_SETUP"

# source 运行中的安装（configs 里经 /usr/bin/cmcc_robot/install 符号链接取）。
# 别写 /root/cmcc_robot/setup.bash：那是陈旧副本，接口定义与运行版不同。
cmcc_require_setup "[交互]" || exit 1

# 域与 DDS 实现**一律取 configs**，不接受环境覆盖：播报那半段走 run_tts_speak.sh，而它
# 是无条件按 configs 设域的（那边改不得，工具链路都靠它）。这里若允许覆盖，就会造出
# 「订阅在域 A、播报在域 B」的错配 —— 2026-09-28 实测踩到：订阅在 99，播报却打到 live 域 2
# 上真的出了声，而本脚本因为收不到状态事件只能等满上限。要换域只能改 configs。
export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"
export ROS_DOMAIN_ID="$(cmcc_conf ROS_DOMAIN_ID)"

TEXT="${1:-}"
if [ $# -gt 0 ]; then shift; fi   # 剩下的参数（--max-wait / --keep-reply / --verbose）原样透传

# stdout 保持干净给结果 JSON：这里不做重定向，python 侧的诊断本来就走 stderr
python3 "$(dirname "${BASH_SOURCE[0]}")/audio_interact.py" --text "$TEXT" "$@"
