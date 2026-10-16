#!/bin/bash
# TTS 技能两个 shell 入口（run_tts_speak.sh / wait_tts_done.sh）共用的环境前导：
# source ROS setup → source 运行中的安装 → 导出 DDS 两件套。
#
# 用法：在入口脚本里 `source "<同目录>/lib-tts-env.sh"`（**不要执行**它：它只把环境
# 备好，不干别的）。缺 ROS_SETUP 或运行中的安装时它 **exit 1**，也就是把调用它的那个
# 入口脚本一起结束掉 —— 这正是两处入口想要的（环境不全就别往下走）。
#
# 事实源只有 configs/paths.py（路径）与 configs/configs.py（参数）：shell 读不到 Python，
# 经 configs/lib-shell-paths.sh 的 cmcc_path / cmcc_conf 过桥取值。
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"

ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
    echo "[TTS] ❌ 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
source "$ROS_SETUP"

# source 运行中的安装（configs 里经 /usr/bin/cmcc_robot/install 符号链接取）。
# 别写 /root/cmcc_robot/setup.bash：那是 8/3 的陈旧副本，接口定义与运行版不同。
cmcc_require_setup "[TTS]" || exit 1

export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"
export ROS_DOMAIN_ID="$(cmcc_conf ROS_DOMAIN_ID)"
