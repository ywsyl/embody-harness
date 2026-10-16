#!/bin/bash
# 语义寻物（search_and_approach）包装脚本：source ROS 环境后调用端侧 findobj 客户端。
# 用法（透传所有参数）：
#   ./run_findobj_search.sh --prompt "red cup"                    # 默认 SN=设备 devSn
#   ./run_findobj_search.sh --prompt "sofa" --sn <机器狗编号> --timeout 120
# 说明：--dry-run 可做自检（不初始化 ROS）。
set -e

# ROS 环境与仓库其它节点一致：foxy + cyclonedds + domain 2（值取自 configs/，唯一事实源）
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"
ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
    echo "[FINDOBJ] ❌ 取不到 configs/paths.py 的 ROS_SETUP" >&2; exit 1; }
source "$ROS_SETUP"

# source 运行中的安装（经 /usr/bin/cmcc_robot/install 符号链接取）；
# 别写 /root/cmcc_robot/setup.bash，那是 8/3 的陈旧副本。
cmcc_require_setup "[FINDOBJ]" || exit 1

export RMW_IMPLEMENTATION="$(cmcc_conf RMW_IMPLEMENTATION)"
export ROS_DOMAIN_ID="$(cmcc_conf ROS_DOMAIN_ID)"

# 按脚本所在目录定位，不依赖调用时 CWD
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/findobj_search.py" "$@"
