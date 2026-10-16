#!/bin/bash
# 机器狗运动控制命令（响应速度 ~10ms）

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 运行客户端
"$SCRIPT_DIR/client" "$@"
