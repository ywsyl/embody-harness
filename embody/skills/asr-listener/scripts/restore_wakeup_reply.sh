#!/bin/bash
# 恢复唤醒回应：从备份恢复 wakeup04~10.wav 原始音频
set -e

# 两处路径都取自 configs/paths.py（唯一事实源），不写绝对路径：
#   - 本仓内的备份目录：本技能在开发仓与部署树下各自指向那棵树；
#   - RES：仓库外的**运行中机器人安装**，configs 里经 /usr/bin/cmcc_robot/install
#     符号链接取。别写死具体安装目录——恢复到 speech_core 不读的那棵树等于没恢复。
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"
RES="$(cmcc_path WAKEUP_RES_DIR)" || {
    echo "[RESTORE] ❌ 取不到 configs/paths.py 的 WAKEUP_RES_DIR" >&2; exit 1; }
BK="$(cmcc_path WAKEUP_BACKUP_DIR)" || {
    echo "[RESTORE] ❌ 取不到 configs/paths.py 的 WAKEUP_BACKUP_DIR" >&2; exit 1; }
# 文件名清单取自 configs（与 mute_wakeup_reply.sh 同一份）：两边不一致就会出现
# 「静默了一批、只恢复了一部分」。
WAKEUP_FILES="$(cmcc_path WAKEUP_REPLY_FILES)" || {
    echo "[RESTORE] ❌ 取不到 configs/paths.py 的 WAKEUP_REPLY_FILES" >&2; exit 1; }

if [ ! -d "$BK" ] || [ -z "$(ls -A "$BK" 2>/dev/null)" ]; then
    echo "[RESTORE] ❌ 未找到备份，无法恢复"
    exit 1
fi

echo "[RESTORE] 从 $BK 恢复唤醒回应音频..."
for f in $WAKEUP_FILES; do
    if [ -f "$BK/$f" ]; then
        cp "$BK/$f" "$RES/$f"
    fi
done

echo "[RESTORE] ✅ 完成，唤醒回应已恢复"
