#!/bin/bash
# lib-shell-paths.sh —— shell 侧读 configs/ 常量的取值器。
#
# 本仓的常量只有三处事实源：configs/paths.py（路径）、configs/configs.py（参数）、
# configs/agents.py（角色表）。shell 读不到 Python，就过桥取值：
#   cmcc_path <常量名>    —— 取 configs/paths.py 的常量（Path 或路径清单）
#   cmcc_conf <常量名>    —— 取 configs/configs.py 的常量（标量或清单）
#   cmcc_agents <常量名>  —— 取 configs/agents.py 的角色表（标量或清单）
#
# 为什么不各自写死：绝对路径写法（/root/cmcc_agent/...）一搬进部署树
# （/root/embody-harness/embody）就指向另一棵树，同一份技能两处跑必有一处是错的；
# 参数写死则改 configs 不生效，两边静默漂移。经 configs 取值则由 ROOT_DIR 自动定位，
# 两处各自正确。
#
# 用法（按相对层数 source，别写绝对路径）：
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/configs/lib-shell-paths.sh"
#   CAPTURE="$(cmcc_path CAMERA_CAPTURE_SCRIPT)" || exit 1
#   DOMAIN="$(cmcc_conf ROS_DOMAIN_ID)"
#   cmcc_require_setup "[TTS]" || exit 1        # 取运行中的机器人安装并 source
#
# 注意：每次调用起一个 python3。同一批值要在循环里反复用，先取到变量里。
# 取不到（常量名写错 / 仓库结构被挪）时返回非 0 且不打印——不打印是关键，
# 否则调用方会把空串当成有效路径继续跑下去。让调用方自己决定怎么报错。

CMCC_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

_cmcc_val() {
    (cd "$CMCC_REPO_ROOT" && python3 -c "
import contextlib, importlib, sys
# 被 import 的模块可能往 stdout 打东西（configs/paths.py 读设备配置失败就打印一行），
# 而 stdout 是本取值器的返回值本身 —— 不挡住，调用方拿到的就是「那行日志 + 换行 + 值」。
# 转到 stderr：不藏起来，只是不混进取到的值里。
with contextlib.redirect_stdout(sys.stderr):
    mod = importlib.import_module('$1')
if not hasattr(mod, '$2'):
    sys.exit(1)
v = getattr(mod, '$2')
# 清单按空格拼，好让 for x in \$VAR 直接用；标量（含 Path）原样打印
print(' '.join(str(x) for x in v) if isinstance(v, (list, tuple)) else v)")
}

cmcc_path() { _cmcc_val configs.paths "$1"; }
cmcc_conf() { _cmcc_val configs.configs "$1"; }
cmcc_agents() { _cmcc_val configs.agents "$1"; }

# 取「当前运行的机器人安装」的 setup.bash 并 source。
# 这是 .sh 里重复最多的一段，收敛在此；比各地写死安装目录安全（见 configs/paths.py
# 的 CMCC_ROBOT_INSTALL 注释：写错树是静默失效，不报错）。
# 参数是日志前缀（如 "[TTS]"）。source 在函数内执行会作用到调用方 shell，故取到的
# ROS 环境对调用方可见。失败返回非 0，调用方接 `|| exit 1`。
cmcc_require_setup() {
    local prefix="${1:-[cmcc]}" setup
    setup="$(cmcc_path CMCC_ROBOT_SETUP)" || {
        echo "$prefix ❌ 取不到 configs/paths.py 的 CMCC_ROBOT_SETUP" >&2
        return 1
    }
    source "$setup"
}
