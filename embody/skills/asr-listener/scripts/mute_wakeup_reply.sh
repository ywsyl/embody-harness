#!/bin/bash
# 静默唤醒回应：把唤醒回应音频 wakeup04~10.wav 替换为静音
# 唤醒回应由 speech_core 用 AlsaHelper 直接播放（绕过 audio_player），
# 因此 stop_play 服务无法打断它，唯一可靠方式是替换音频文件为静音。
# 本脚本会先备份原文件，可用 restore_wakeup_reply.sh 恢复。
set -e

# 路径与静音文件名都取自 configs/paths.py（唯一事实源），不写绝对路径：
#   - 本仓内的备份目录 / 恢复脚本：本技能在开发仓与部署树下各自指向那棵树；
#   - RES：仓库外的**运行中机器人安装**，configs 里经 /usr/bin/cmcc_robot/install
#     符号链接取。别写死具体安装目录——指到 speech_core 不读的那棵树就是静默失效；
#   - SILENCE_WAV / WAKEUP_REPLY_FILES：静音文件与要替换的 wav 清单，与
#     restore_wakeup_reply.sh 共用同一份，保证「替换了什么就能恢复什么」。
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"
RES="$(cmcc_path WAKEUP_RES_DIR)" || {
    echo "[MUTE] ❌ 取不到 configs/paths.py 的 WAKEUP_RES_DIR" >&2; exit 1; }
BK="$(cmcc_path WAKEUP_BACKUP_DIR)" || {
    echo "[MUTE] ❌ 取不到 configs/paths.py 的 WAKEUP_BACKUP_DIR" >&2; exit 1; }
RESTORE_SCRIPT="$(cmcc_path RESTORE_WAKEUP_REPLY_SCRIPT)" || RESTORE_SCRIPT="restore_wakeup_reply.sh"
# 静音文件与「要替换哪些 wav」同样取自 configs：文件名清单必须与
# restore_wakeup_reply.sh 完全一致，各写一份时漏改一处就会「静默了恢复不回来」。
SILENCE="$(cmcc_path SILENCE_WAV)" || {
    echo "[MUTE] ❌ 取不到 configs/paths.py 的 SILENCE_WAV" >&2; exit 1; }
WAKEUP_FILES="$(cmcc_path WAKEUP_REPLY_FILES)" || {
    echo "[MUTE] ❌ 取不到 configs/paths.py 的 WAKEUP_REPLY_FILES" >&2; exit 1; }

# 1. 备份（仅在未备份过时）
mkdir -p "$BK"
if [ -z "$(ls -A "$BK" 2>/dev/null)" ]; then
    echo "[MUTE] 备份原始唤醒回应音频到 $BK"
    for f in $WAKEUP_FILES; do
        cp "$RES/$f" "$BK/$f"
    done
else
    echo "[MUTE] 检测到已有备份，跳过备份步骤"
fi

# 2. 生成静音 wav（16kHz mono s16le，0.3s）
if [ ! -f "$SILENCE" ]; then
    echo "[MUTE] 生成静音音频..."
    ffmpeg -f lavfi -i anullsrc=r=16000:cl=mono -t 0.3 -c:a pcm_s16le -y "$SILENCE" 2>/dev/null
fi

# 3. 替换为静音
echo "[MUTE] 替换唤醒回应为静音..."
for f in $WAKEUP_FILES; do
    cp "$SILENCE" "$RES/$f"
done

echo "[MUTE] ✅ 完成，唤醒回应已静默"
echo "[MUTE] 恢复方法: bash ${RESTORE_SCRIPT}"
