#!/usr/bin/env bash
# interactor-daemon.sh —— `scripts/role-daemon.sh --role interactor` 的固定角色入口。
#
# 常驻形态的完整说明（不挂 headless 的原因、进程为什么不退出、单实例约束、speak 要真终端）
# 都在 role-daemon.sh 的文件头。本文件只把角色写死，让已有的命令继续可用
# ——下面两条完全等价：
#   scripts/interactor-daemon.sh
#   scripts/role-daemon.sh --role interactor
#
# 可覆盖环境变量：EMBODY / ENGINE / NODE22_BIN / DSH_HOME（透传）。

set -euo pipefail

# -h 用本文件的头（说清这是固定角色入口）；其余参数原样转给 role-daemon.sh，它才是
# 常驻形态说明与实现所在。
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/role-daemon.sh" --role interactor "$@"
