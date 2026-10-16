#!/usr/bin/env bash
# engine-build.sh —— 补齐 dsh 引擎的**构建产物**（在引擎根跑，不是在 embody/ 里跑）。
#
# 为什么需要这一步：dsh 的包从源码经 tsx 装载，但并不是零构建。两类产物必须先落盘，
# 缺了它们的表现是「角色进程活着、就绪块照打，事件投进去却没有一点反应」——因为
# 引擎把一轮里的失败咽在自己的 driver 边界（见下「症状」）：
#   1) 各包的 lib/：typert-loader 按 package.json 的 exports["./typert"] 直接 import
#      lib/typert.host.js。这条路径走的是 Node 的 exports 解析，**不经过 tsconfig paths**，
#      所以源码树里没有它就是 Cannot find module（引擎装载直接失败，dsh 以 1 退出）。
#   2) 平台原生插件：agent 跑一轮时要装载
#      native/system/packages/<platform>-<arch>/bin/<libc>/system.node。缺它时引擎把这一
#      轮的异常咽掉，进程不退出、日志不响，只有「事件投进去没反应」。
#
# 为什么不做成每次启动都跑：产物齐全时它仍要几分钟（tsdown 是所有包一起重新打包）。
# 启动路径改为**检查**（configs/scripts-env.sh 的 embody_check_engine_build），只在本脚本
# 被显式调用时才真的构建。
#
# 用法：
#   embody/scripts/manual/engine-build.sh          # 补齐两类产物（已是最新的部分会被跳过）
#
# 只跑两条引擎自带的脚本，不自己拼编译命令：
#   pnpm run build:native-system   → tsx native/system/scripts/build.ts --host-addon-only
#   pnpm run build:lib:host        → tsc -b tsconfig.host.json && tsdown --env.DSH_BUILD_FACE host
# 刻意不跑 `pnpm run build`（整仓构建）：它还会带 build:web 与 client 面，常驻角色用不到。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=configs/scripts-env.sh
source "$REPO_ROOT/configs/scripts-env.sh"

embody_check_node engine-build || exit 1
embody_apply_env

echo "== 引擎根：$ENGINE_DIR"
cd "$ENGINE_DIR"

echo "== 1/2 原生插件（pnpm run build:native-system）"
pnpm run build:native-system

echo "== 2/2 各包 lib/（pnpm run build:lib:host）"
pnpm run build:lib:host

echo "== 复查：还缺什么"
if embody_check_engine_build engine-build; then
  echo "engine-build: 构建产物齐全。"
else
  echo "engine-build: 上面的产物仍然缺失——构建没成功，先看它自己的报错。" >&2
  exit 1
fi
