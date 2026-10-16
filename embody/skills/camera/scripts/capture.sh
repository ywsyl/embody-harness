#!/bin/bash
# 相机抓拍入口（对外稳定契约）：exec 同目录的 capture_direct.py。
#
# 只做转发，不在这里藏实现——外部调用方（harness 的 cmcc_camera 工具、兄弟部署的
# receiver）认的都是这一层，所以路径必须一直在，实现可以换。
# 实现就放在旁边、用 SCRIPT_DIR 相对定位，不写绝对路径：同一份代码在开发仓与部署树
# （/root/embody-harness/embody）下都能跑。
#
# 历史：这里曾是 capture_direct.py / capture.py 二选一。capture.py 读 GStreamer 共享
# 内存 socket（shmsrc socket-path=/tmp/neck_jpeg），本机那条 socket 是私有加密协议、
# 取不到帧，故只剩直采一条路。但它的**命令行契约**（-d 输出目录 / --burst / --timelapse /
# --interval / --prefix / --resize / --watermark）是对外约定，harness 的 cmcc_camera 按它
# 调用，已由 capture_direct.py 完整兑现——改参数别只改一边。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/capture_direct.py" "$@"
