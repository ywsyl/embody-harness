#!/usr/bin/env bash
# scripts-env.sh —— starter.sh 与 embody/scripts/*.sh 共用的**唯一** shell 侧
# 事实源：node 运行时、引擎根、节奏参数。
#
# 为什么单独一份：这些值原先各自抄在每个脚本里（`NODE22_BIN` 的默认值在
# role-daemon.sh / rolebus-setup.sh / settings-seed.sh 里各写了一遍），
# 换终端时漏改一处就表现为「某个角色起不来」。现在只在这里写一次，脚本一律 source 本文件。
#
# 用法（调用方先定好 REPO_ROOT = embody/，本文件不自己推）：
#   source "$REPO_ROOT/configs/scripts-env.sh"
#
# 本文件只设值、不 export（导出交给调用方决定）：它也可能被想要只读值的脚本 source。
# 但下面这些**必须**导出——子进程（pnpm → node、python 桥）要看得见：
#   NODE_BIN_DIR / ENGINE_DIR / EMBODY / EMBODY_CONFIGS_DIR / EMBODY_INSTALL_DIR / DSH_HOME（由调用方定）
#
# 每个值都可以用环境变量覆盖，覆盖的优先级高于本文件的默认值。

# -----------------------------------------------------------------------------
# 仓库内外路径
# -----------------------------------------------------------------------------

# embody/ 的绝对路径。调用方通常已设 REPO_ROOT；没设就用本文件位置反推（configs/ 的上一级）。
EMBODY="${EMBODY:-${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

# 引擎根（fork 根）= embody/ 的父目录。dsh 必须从**引擎根**启动：角色人设文件是 profile 层里
# 相对启动工作目录解析的 `!!js readFileSync('embody/<Role>/prompts/...')`
# （见 Interactor/cordis.patch.yml）。supervisor 的观测源 path 不在这条约定里：它们是 configs/
# paths.py 的绝对路径（经 role-daemon.sh 过桥）。
ENGINE_DIR="${ENGINE_DIR:-$(cd "$EMBODY/.." && pwd)}"
# 别名：role-daemon.sh 等脚本历史上用 ENGINE 这个名字，保留以免外部脚本失效。
ENGINE="${ENGINE:-$ENGINE_DIR}"

CONFIGS_DIR="${EMBODY_CONFIGS_DIR:-$EMBODY/configs}"
EMBODY_CONFIGS_DIR="$CONFIGS_DIR"

# install/ ：本机私有的部署值（模型服务端点 / 密钥，见 install/secret.env）。与 configs/ 的分工：
# configs/ 是随代码走的、可入库的事实源；install/secret.env 每台终端各一份，不入库
# （embody/.gitignore）。取值口径在 scripts/lib-local-env.sh。
INSTALL_DIR="${EMBODY_INSTALL_DIR:-$EMBODY/install}"
EMBODY_INSTALL_DIR="$INSTALL_DIR"

# -----------------------------------------------------------------------------
# node 运行时
# -----------------------------------------------------------------------------
# 引擎要求 node `^22.19.0 || >=24.0.0`（根 package.json 的 engines）。**只有一个下限不够**：
# apps/cli/src/bin.ts 与 packages/subprocess/subprocess-local/src/bin.ts 用 `import.meta.main`
# 决定要不要跑 main，而这个语法在 node v24.2.0 才落地。节点版本落在 [24.0.0, 24.2.0) 时
# `import.meta.main` 是 undefined，dsh 于是**什么都不做、以 0 退出**——没有报错、没有输出，
# 表现为「角色起了但什么都没发生」。选运行时因此要同时满足 engines 与 import.meta.main。
#
# 换终端就改这一行（或设环境变量 NODE_BIN_DIR 覆盖）。
NODE_BIN_DIR="${NODE_BIN_DIR:-${NODE22_BIN:-/root/.nvm/versions/node/v24.15.0/bin}}"
# 变量名带 22 是历史遗留（旧终端把它指向 harness/.runtime/node22）；现在不再特指 22，
# 但仍接受它作为覆盖值，免得旧脚本/旧文档里的 NODE22_BIN=… 静默失效。

# 运行时的**自检**：宁可在这里明确报错，也不要让 dsh 静默空转（见上）。$1 是调用方名字，
# 只用于报错前缀。本函数不 exit——是否中止由调用方决定（rolebus-setup 可以继续，角色启动不行）。
embody_check_node() {
  local who="${1:-embody}"
  local node="$NODE_BIN_DIR/node"
  if [ ! -x "$node" ]; then
    echo "$who: node 不在 $node（configs/scripts-env.sh 的 NODE_BIN_DIR，或用环境变量覆盖）" >&2
    return 1
  fi
  if [ "$("$node" --input-type=module -e 'console.log(String(import.meta.main))' 2>/dev/null)" != "true" ]; then
    echo "$who: $("$node" --version 2>/dev/null) 不支持 import.meta.main（需 node ≥22.19 或 ≥24.2）——" >&2
    echo "    dsh 会在这种运行时下静默空转（以 0 退出且什么都不做）。换一个 NODE_BIN_DIR。" >&2
    return 1
  fi
  return 0
}

# -----------------------------------------------------------------------------
# pnpm 环境
# -----------------------------------------------------------------------------
# CI=true：跳过 pnpm 每次跑前的 deps 状态检查与 lefthook 安装器（旧终端上 lefthook 会因
# git < 2.26 而失败，把 `pnpm dsh` 整个拖挂）。
# 注意 CI=true 也让 pnpm 默认 --frozen-lockfile：profile 目录（$DSH_HOME/profiles/<role>）
# 有自己的一份 pnpm-workspace.yaml，与引擎根的 lockfile 不是一套，不受引擎侧增减影响。
CI=true
# 这是 **pnpm** 的设置项（名字里的 npm_ 前缀只是它读配置的那条通道）。只给 pnpm 看：脚本里
# 别再拿 npm / npx 跑东西，npm 不认这个名字，每调用一次就打一行「Unknown env config」警告，
# 看着像出错（2026-10-09 踩到，见 scripts/rolebus-setup.sh 的 build_bundle）。
npm_config_verify_deps_before_run=false
DSH_TELEMETRY_DISABLED=1
NODE_OPTIONS="${NODE_OPTIONS:-}"

# -----------------------------------------------------------------------------
# 引擎构建产物
# -----------------------------------------------------------------------------
# 「引擎从源码跑」不等于「零构建」：有两类产物必须先在引擎根构建出来，而它们都是 gitignore
# 的（换终端/重新 clone 之后不会跟着代码过来）。缺了它们，症状是「角色进程活着、就绪块照
# 打，事件投进去却没有任何反应」——参见 scripts/manual/engine-build.sh 的抬头。
#
# 补齐它们的脚本（启动路径只**检查**、不自动构建：构建要几分钟）。
ENGINE_BUILD_SCRIPT="${ENGINE_BUILD_SCRIPT:-$EMBODY/scripts/manual/engine-build.sh}"

# 列出缺失的构建产物（每行一个，相对引擎根；全齐则无输出、返回 0）。
#
# 两处都**扫出来**而不是写死清单：
#   - typert 产物：扫 packages/<group>/<pkg>/package.json 里声明 exports["./typert"] 的包，
#     逐个核对那个 exports 指向的文件。写死的话引擎侧新增一个 typert 贡献方就会漏检。
#   - 原生插件：平台与 libc 由 node 自己报（process.platform / process.arch /
#     glibcVersionRuntime），不在 shell 里猜 uname 与 libc。
embody_engine_build_missing() {
  local engine="${ENGINE_DIR:-$ENGINE}"
  python3 - "$engine" "${NODE_BIN_DIR:-}" <<'PY'
import glob, json, os, subprocess, sys

engine, node_bin_dir = sys.argv[1], sys.argv[2]
missing = []

# ---- 1) typert 宿主产物（exports["./typert"] 指向的 lib/ 文件）
for manifest_path in sorted(glob.glob(os.path.join(engine, 'packages', '*', '*', 'package.json'))):
    try:
        with open(manifest_path, encoding='utf-8') as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        continue
    exports = manifest.get('exports')
    if not isinstance(exports, dict):
        continue
    entry = exports.get('./typert')
    # 两种写法都要认：短形式 "./lib/x.js" 与带条件的 {"default": "./lib/x.js"}。
    target = entry.get('default') if isinstance(entry, dict) else entry
    if not isinstance(target, str):
        continue
    path = os.path.join(os.path.dirname(manifest_path), target)
    if not os.path.exists(path):
        missing.append(os.path.relpath(path, engine))

# ---- 2) 平台原生插件（agent 跑一轮时装载）
# 平台与 libc 由 node 自己报，不在 shell 里猜 uname 与 libc。
node = os.path.join(node_bin_dir, 'node') if node_bin_dir else 'node'
try:
    probe = subprocess.run(
        [node, '--input-type=module', '-e',
         "const r=process.report.getReport();"
         "console.log(process.platform+'-'+process.arch+'/'"
         "+(r.header.glibcVersionRuntime?'glibc':'musl'))"],
        capture_output=True, text=True, timeout=60,
    )
except OSError as exc:
    sys.stderr.write('原生插件路径无法判定：%s\n' % exc)
    sys.exit(2)
if probe.returncode != 0:
    # 判不出原生插件的路径 = 这一项**没被核对过**，不能当成「齐全」放过去。
    sys.stderr.write('原生插件路径无法判定：' + (probe.stderr.strip() or 'node 退出码 %d' % probe.returncode) + '\n')
    sys.exit(2)
host, libc = probe.stdout.strip().split('/')
addon = os.path.join('native', 'system', 'packages', host, 'bin', libc, 'system.node')
if not os.path.exists(os.path.join(engine, addon)):
    missing.append(addon)

if missing:
    print('\n'.join(missing))
PY
}

# 检查引擎构建产物；缺失就报出来并给出补齐命令。$1 是调用方名字，只用于报错前缀。
# 需要「明知没构建也要跑」时，设 EMBODY_SKIP_ENGINE_CHECK=1 跳过。
embody_check_engine_build() {
  local who="${1:-embody}"
  if [ "${EMBODY_SKIP_ENGINE_CHECK:-0}" = 1 ]; then
    echo "$who: EMBODY_SKIP_ENGINE_CHECK=1，跳过引擎构建产物检查。" >&2
    return 0
  fi
  local missing
  # 取不到结果（python3 报错等）时**不能**当成「没有缺失」——那正是漏检的样子。
  if ! missing="$(embody_engine_build_missing)"; then
    echo "$who: 引擎构建产物检查没跑通（python3 或 node 报错，见上），无法确认产物齐全。" >&2
    return 1
  fi
  [ -n "$missing" ] || return 0
  echo "$who: 引擎构建产物缺失——dsh 会在装载或跑轮次时失败，角色表现为「活着但事件没人处理」：" >&2
  while IFS= read -r one; do
    [ -n "$one" ] && echo "    $one" >&2
  done <<< "$missing"
  echo "    补齐（在引擎根构建，几分钟）：$ENGINE_BUILD_SCRIPT" >&2
  echo "    这些产物是 gitignore 的，换终端/重新 clone 之后要重新构建。" >&2
  return 1
}

embody_apply_env() {
  # node 与 pnpm 是两个来源，别指望它们在同一个 bin 目录里：NODE_BIN_DIR 里只有
  # node/npm/npx（nvm 不装 pnpm），`pnpm` 实际来自另一个 nvm 版本目录（如 v24.0.1/bin），
  # 它是一个 `#!/usr/bin/env node` 的脚本。所以必须把 NODE_BIN_DIR 放在 PATH 最前面：
  # pnpm 自己由**最先命中的 node** 解释执行，这样 dsh 才跑在钉死的 node 上
  # （版本不对时 dsh 静默空转，见 embody_check_node）。
  export PATH="$NODE_BIN_DIR:$PATH"
  export CI npm_config_verify_deps_before_run DSH_TELEMETRY_DISABLED
  export EMBODY ENGINE ENGINE_DIR EMBODY_CONFIGS_DIR EMBODY_INSTALL_DIR
}

# -----------------------------------------------------------------------------
# 启动节奏（starter.sh 的角色侧启动流水）
# -----------------------------------------------------------------------------
# 角色之间隔几秒起：三次 profile 物化与 settings.yaml 的读改写不并发跑（settings-seed 是
# 读-改-写，非原子）。
START_STAGGER="${START_STAGGER:-2}"
# 启动健康检查窗口：窗口内进程没了就报出来并打日志尾巴。判据只有「进程还在不在」，与角色
# 这一窗口里跑没跑模型无关——交互器会先跑一轮开机自查（见 Interactor/resident.patch.yml 的
# startupWake），那是正常的一轮，不是启动失败；其余角色启动不跑模型，没有这一项。
START_CHECK_SECONDS="${START_CHECK_SECONDS:-5}"
# 收尾时给引擎的收尾时间，到点强杀剩下的进程。
KILL_GRACE_SECONDS="${KILL_GRACE_SECONDS:-5}"
# watcher 读会话日志的节奏；空值 = 用 role-watch.sh 自己的缺省（1s）。
WATCH_INTERVAL="${WATCH_INTERVAL:-}"
