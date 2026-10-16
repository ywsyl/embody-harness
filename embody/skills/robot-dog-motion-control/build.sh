#!/usr/bin/env bash
# 编译 robot-dog-motion-control，并同步产物到 scripts

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_ROOT="$SCRIPT_DIR"
PACKAGE_NAME="robot_dog_motion_control"
BUILD_DIR="$SKILL_ROOT/build"
INSTALL_DIR="$SKILL_ROOT/install"
LOG_DIR="$SKILL_ROOT/log"
BIN_DIR="$INSTALL_DIR/$PACKAGE_NAME/lib/$PACKAGE_NAME"
LEGACY_BIN_DIR="$INSTALL_DIR/lib/$PACKAGE_NAME"
TARGET_SCRIPTS_DIR="$SKILL_ROOT/scripts"

printf "==> 加载 ROS 2 环境\n"
# 路径与参数取自 configs/（唯一事实源）。取值必须在这里做——下面第 28 行的
# `set -u` 是特意推迟到 source 完 ROS 之后的（ROS 的 setup.bash 会引用未定义变量），
# 而 cmcc_path/cmcc_conf 要在那之前调用。
source "$(cd "$SCRIPT_DIR/../.." && pwd)/configs/lib-shell-paths.sh"

ROS_SETUP="$(cmcc_path ROS_SETUP)" || {
  printf "错误: 取不到 configs/paths.py 的 ROS_SETUP\n" >&2; exit 1; }
# 保留 -f 守卫：这里的报错比 source 一个不存在的文件清楚
if [ -f "$ROS_SETUP" ]; then
  . "$ROS_SETUP"
else
  printf "错误: 未找到 %s\n" "$ROS_SETUP" >&2
  exit 1
fi

UNITREE_SETUP="$(cmcc_path UNITREE_ROS_SETUP)" || {
  printf "错误: 取不到 configs/paths.py 的 UNITREE_ROS_SETUP\n" >&2; exit 1; }
if [ -f "$UNITREE_SETUP" ]; then
  . "$UNITREE_SETUP"
fi

RMW="$(cmcc_conf RMW_IMPLEMENTATION)" || {
  printf "错误: 取不到 configs/configs.py 的 RMW_IMPLEMENTATION\n" >&2; exit 1; }

set -u

export RMW_IMPLEMENTATION="$RMW"

if [ ! -f "$SKILL_ROOT/CMakeLists.txt" ] || [ ! -f "$SKILL_ROOT/package.xml" ]; then
  printf "错误: skill 目录缺少 CMakeLists.txt 或 package.xml: %s\n" "$SKILL_ROOT" >&2
  exit 1
fi

if [ "$#" -gt 0 ] && [ "$1" = "clean" ]; then
  printf "==> 清理本地构建产物\n"
  rm -rf "$BUILD_DIR" "$INSTALL_DIR" "$LOG_DIR"
  rm -f "$TARGET_SCRIPTS_DIR/server" "$TARGET_SCRIPTS_DIR/client"
fi

printf "==> 编译目录: %s\n" "$SKILL_ROOT"
printf "==> 编译包: %s\n" "$PACKAGE_NAME"
colcon --log-base "$LOG_DIR" build \
  --base-paths "$SKILL_ROOT" \
  --packages-select "$PACKAGE_NAME" \
  --build-base "$BUILD_DIR" \
  --install-base "$INSTALL_DIR"

if [ -x "$BIN_DIR/server" ] && [ -x "$BIN_DIR/client" ]; then
  :
elif [ -x "$LEGACY_BIN_DIR/server" ] && [ -x "$LEGACY_BIN_DIR/client" ]; then
  BIN_DIR="$LEGACY_BIN_DIR"
else
  printf "错误: 未找到编译产物: %s 或 %s\n" "$BIN_DIR" "$LEGACY_BIN_DIR" >&2
  exit 1
fi

printf "==> 同步可执行文件到 scripts\n"
# 先写临时文件再 mv（rename），不能用 cp 直接覆盖：
# 服务正跑着 scripts/server 时，cp 会报 "Text file busy" 而失败（set -e 直接把整个
# 构建脚本带崩，client 也一起同步不了）。rename 只替换目录项，运行中的进程继续持有
# 旧 inode 照常跑，新二进制在下次重启服务时生效。
sync_bin() {
  local src="$1" dst="$2" tmp="$2.tmp.$$"
  cp "$src" "$tmp"
  chmod +x "$tmp"
  mv -f "$tmp" "$dst"
}
sync_bin "$BIN_DIR/server" "$TARGET_SCRIPTS_DIR/server"
sync_bin "$BIN_DIR/client" "$TARGET_SCRIPTS_DIR/client"

printf "\n✅ 编译成功\n"
printf "产物位置:\n"
printf "  %s/server\n" "$TARGET_SCRIPTS_DIR"
printf "  %s/client\n" "$TARGET_SCRIPTS_DIR"
printf "后续可执行:\n"
printf "  cd \"%s\" && ./start.sh\n" "$TARGET_SCRIPTS_DIR"
printf "  cd \"%s\" && ./motion.sh forward 0.5 10\n" "$TARGET_SCRIPTS_DIR"
