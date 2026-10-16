#!/bin/bash

region="$1"
x_hirizon="$2"
if_turn="$3"

# motion.sh 取自 configs/paths.py（唯一事实源），不写绝对路径：本技能在开发仓与部署树
# （/root/embody-harness/embody）下各自指向那棵树。
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)/configs/lib-shell-paths.sh"
MOTION_SCRIPT="$(cmcc_path MOTION_SCRIPT)" || {
    echo "[ERROR] 取不到 configs/paths.py 的 MOTION_SCRIPT" >&2; exit 1; }

eval "$(python3 - "$region" "$x_hirizon" <<'PY'
import sys
import shlex

region = sys.argv[1]
x_hirizon = sys.argv[2]

try:
    x_hirizon = float(x_hirizon)
except ValueError:
    print(f'echo "[ERROR] x_hirizon 必须是数字，当前值: {shlex.quote(str(x_hirizon))}" >&2')
    print("exit 1")
    sys.exit(0)

if not 0 <= x_hirizon <= 1:
    print(f'echo "[ERROR] x_hirizon 必须在 [0, 1] 范围内，当前值: {x_hirizon}" >&2')
    print("exit 1")
    sys.exit(0)

region_config = {
    "left_top": {
        "center_angle": 0,
        "x_center": 0.25,
    },
    "right_top": {
        "center_angle": 90,
        "x_center": 0.75,
    },
    "left_bottom": {
        "center_angle": 180,
        "x_center": 0.25,
    },
    "right_bottom": {
        "center_angle": 270,
        "x_center": 0.75,
    },
}

if region not in region_config:
    print(f'echo "[ERROR] region 参数非法: {shlex.quote(region)}" >&2')
    print("exit 1")
    sys.exit(0)

cfg = region_config[region]
center_angle = cfg["center_angle"]
x_center = cfg["x_center"]

result = center_angle + (x_hirizon - x_center) * 180

if 0 <= result <= 180:
    direction = "turnright"
    i_direction = "turnleft"
    angle = result
elif result < 0:
    direction = "turnleft"
    i_direction = "turnright"
    angle = -result
else:
    direction = "turnleft"
    i_direction = "turnright"
    angle = 360 - result

result = round(result, 2)
angle = round(angle, 2)

# 1.0速度下：每转 90 度约需 1.8 秒
speed = 1.0
duration = round(angle / 90 * 1.8, 2)
sleep_duration = round(duration*0.7, 2)

if angle > 0:
    duration = max(duration, 0.2)

print(f"direction={shlex.quote(direction)}")
print(f"i_direction={shlex.quote(i_direction)}")
print(f"angle={shlex.quote(str(angle))}")
print(f"duration={shlex.quote(str(duration))}")
print(f"sleep_duration={shlex.quote(str(sleep_duration))}")
print(f"speed={shlex.quote(str(speed))}")

PY
)"
echo "[INFO] region=$region"
echo "[INFO] x_hirizon=$x_hirizon"
echo "[INFO] if_turn=$if_turn"
echo "[INFO] direction=$direction"
echo "[INFO] angle=$angle"

_turn_flag=$(echo "$if_turn" | tr '[:upper:]' '[:lower:]')
if [ "$_turn_flag" = "true" ] || [ "$_turn_flag" = "1" ] || [ "$_turn_flag" = "yes" ] || [ "$_turn_flag" = "on" ]; then
    bash "${MOTION_SCRIPT}" "$direction" "$speed" "$duration"
    # [2026-09-15 注释] motion.sh 已改为「收到停止信号才返回」，转向结束才返回，
    # 这里再等就是纯重复等待（$sleep_duration 的计算保留，便于回退时直接放开）。
    # sleep "$sleep_duration"
    echo "[INFO] 已完成转向"
else
    echo "[INFO] if_turn=$if_turn，已跳过转向"
fi
