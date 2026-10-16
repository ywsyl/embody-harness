#!/usr/bin/env bash
# rolebus-setup.sh —— 把“每角色独立 profile/进程”的配置文件分层物化到 DSH_HOME。
#
# 对每个角色：
#   1) 若 $DSH_HOME/profiles/<role> 尚不存在 → `dsh plugin --profile <role> add <bundle dir>`
#      （引擎会初始化该 profile，并把该本地 bundle pnpm link 进来）。本仓库 bundle 缺哪个
#      补哪个：@embody/dsh-rolebus（事件收发）、@embody/dsh-tools-bridge（自定义工具的注册方）
#      给每个角色；@embody/dsh-supervisor-loop（定时观察循环）只给 supervisor —— 它是本角色
#      的唤醒源，其余角色没有这一行（它们由 rolebus 事件唤醒，不对称是角色差异本身，不是漏给）；
#      @embody/dsh-task-context（每轮注入待做清单里归它执行、且已开始的条目）只给
#      orchestrator —— 清单是交互器写、它读的一份，它手里没有 task_list 工具
#      （configs/agents.py 的 ORCHESTRATOR_TOOLS）；
#      @embody/dsh-location-context（每轮注入【支持地点清单】）给 interactor 与 supervisor ——
#      这两个角色都要按清单判断（交互器判信息完备性、监督器判任务地点是否合法），而它们的工具
#      白名单里没有 waypoint_nav（清单的另一条路是那个工具自己的说明）；
#      @embody/dsh-speech-stream（把 speak/speak_listen 的 text 参数边生成边送 TTS）只给
#      interactor —— 出声的只有交互器，其余角色的工具面里根本没有这三个工具；
#      @embody/dsh-file-guard（文件围栏：只许读写 runtime_files、命令类工具一律拒）给每个
#      角色 —— 围栏不是某个角色的特征，而是这四个「能跟用户/云端说话的角色」共同的身份边界。
#      已废弃的旧注册方 @embody/dsh-embody-tools 连同它的悬空 link 一并摘掉——留着会让启动
#      解析不到模块。
#      装进来的 bundle 只是“行存在”：tools-bridge 的中性默认 role=unset 不注册任何工具、
#      不起 Python 桥，supervisor-loop / task-context 插的是 disabled: true —— 都由各自
#      profile 层赋予角色差异。
#   2) 把该角色的 profile 覆盖层覆盖为 profile 自身的 cordis.patch.yml（这是“profile 层”）。
#      事实源按角色定位（角色 → 目录的唯一映射在 scripts/lib-roles.sh）：
#      interactor → Interactor/、orchestrator → Orchestrator/、planner → Planner/、
#      supervisor → Supervisor/，都是 <Role>/cordis.patch.yml ——本角色唯一事实源，含人设、
#      rolebus 身份、自定义工具的角色身份、原生工具 disable 列表；supervisor 另有一份常驻
#      叠加层 Supervisor/resident.patch.yml 打开定时观察行（经 --patch 叠，不进 profile 层）。
#
#   3) 把仓库里的七个本地 bundle 编译出 lib/（`tsc -b`）。**这一步不能省**：bundle 的
#      package.json 指向 `lib/index.js`，而 lib/ 是 tsc 产物、已被 .gitignore 忽略 —— 换机器
#      或重新 clone 之后没有它，引擎在装载时报 `Cannot find module …/lib/index.js`，角色
#      全部启动即退出。src/ 比 lib/ 新时才重编，幂等。
#
# 幂等：profile 已存在且 bundles 齐全时跳过 add，只刷新 profile 层文件；lib/ 已是最新则跳过编译。
#
# 可覆盖环境变量：
#   ENGINE_DIR  引擎（默认 = 本仓库父目录，即 fork 根）
#   DSH_HOME    角色 profile 根（默认 $HOME/.dsh；隔离验证时传临时目录）
#   NODE_BIN_DIR / NODE22_BIN  node 运行时 bin（见 configs/scripts-env.sh）
# 运行配方见 configs/scripts-env.sh。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../configs/scripts-env.sh
source "$REPO_ROOT/configs/scripts-env.sh"   # EMBODY / ENGINE_DIR / NODE_BIN_DIR / CI 等
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
ROLEBUS_DIR="$REPO_ROOT/dsh-plugins/rolebus"
TOOLS_BRIDGE_DIR="$REPO_ROOT/dsh-plugins/tools-bridge"
SUPERVISOR_LOOP_DIR="$REPO_ROOT/dsh-plugins/supervisor-loop"
TASK_CONTEXT_DIR="$REPO_ROOT/dsh-plugins/task-context"
LOCATION_CONTEXT_DIR="$REPO_ROOT/dsh-plugins/location-context"
SPEECH_STREAM_DIR="$REPO_ROOT/dsh-plugins/speech-stream"
FILE_GUARD_DIR="$REPO_ROOT/dsh-plugins/file-guard"
# 旧的自定义工具注册方，已由 tools-bridge 取代。它的目录连同 link 都没了，profile 里
# 残留的依赖/bundles 条目会让启动解析不到模块。
RETIRED_TOOLS_BUNDLE='@embody/dsh-embody-tools'
ROLES="${ROLES:-interactor orchestrator planner supervisor}"

# 运行时前置到 PATH（pnpm 会按 PATH 解析 scripts 里的 node），并自检 import.meta.main ——
# 版本落在 [24.0.0, 24.2.0) 时 dsh 会静默空转，这里当场报出来。
embody_check_node rolebus-setup || exit 1
embody_apply_env

# shellcheck source=lib-roles.sh
source "$REPO_ROOT/scripts/lib-roles.sh"   # role_profile_patch：角色 → profile 层事实源

# 确保该 profile 的 bundles 含该本地 bundle（缺则 add）。$1=角色 $2=目录 $3=包名
ensure_bundle() {
  local role="$1" dir="$2" pkg="$3" profile_dir="$DSH_HOME/profiles/$role"
  if [ -f "$profile_dir/package.json" ] && grep -q "\"$pkg\"" "$profile_dir/package.json"; then
    echo "   [$pkg] 已就绪，跳过 add"
    return
  fi
  echo "   [$pkg] 缺失 → dsh plugin --profile $role add"
  (cd "$ENGINE_DIR" && pnpm dsh plugin --profile "$role" add "$dir")
}

# 编译本地 bundle 的 lib/（$1=目录 $2=包名）。跳过条件：lib/index.js 存在且比 src/ 下任何
# .ts 都新。tsconfig 用 `tsc -b`（含 references，会把引擎侧引用的 d.ts 一并构建）。
#
# 直接调引擎根那个 tsc，不走 npx：npx 就是 npm，而 embody_apply_env 导出的
# npm_config_verify_deps_before_run 是 **pnpm** 的设置项，npm 每见一次就打一行
# 「Unknown env config」警告（启动时每编译一个 bundle 一行，看着像出错）。dsh-plugins/*
# 自己没有 node_modules，`npx --no-install tsc` 找到的正是这个可执行文件。
build_bundle() {
  local dir="$1" pkg="$2"
  if [ -f "$dir/lib/index.js" ] && [ -z "$(find "$dir/src" -name '*.ts' -newer "$dir/lib/index.js" -print -quit)" ]; then
    echo "   [$pkg] lib/ 已是最新，跳过编译"
    return 0
  fi
  echo "   [$pkg] 缺 lib/ 或 src 更新 → tsc -b"
  (cd "$dir" && "$ENGINE_DIR/node_modules/.bin/tsc" -b) || {
    echo "rolebus-setup: 编译 $pkg 失败（$dir）—— 引擎装载时会报 Cannot find module lib/index.js" >&2
    echo "    依赖 tsc 在 $ENGINE_DIR/node_modules/.bin/tsc（引擎根装过依赖才有；见 scripts/manual/engine-build.sh）" >&2
    return 1
  }
}

# 摘掉已废弃 bundle 的残留（$1=角色 $2=包名）：`dsh plugin remove` 走 pnpm remove，并按
# 安装状态同步 dsh.profile.bundles 里的层条目。package.json 里没有它就什么都不做。
remove_bundle() {
  local role="$1" pkg="$2" profile_dir="$DSH_HOME/profiles/$role"
  [ -f "$profile_dir/package.json" ] || return 0
  grep -q "\"$pkg\"" "$profile_dir/package.json" || return 0
  echo "   [$pkg] 已废弃 → dsh plugin --profile $role remove"
  (cd "$ENGINE_DIR" && pnpm dsh plugin --profile "$role" remove "$pkg")
}

echo "ENGINE_DIR=$ENGINE_DIR"
echo "DSH_HOME=$DSH_HOME"
echo "ROLES=$ROLES"
echo

# 编译七个本地 bundle（与角色无关，做一次）：引擎装载 profile 时按 package.json 的 main 解析
# 到 lib/index.js，没有 lib/ 就角色全部起不来。放在起角色之前，失败即整体退出。
echo "== 编译本地 bundles（dsh-plugins/*，tsc -b）"
build_bundle "$ROLEBUS_DIR" '@embody/dsh-rolebus'
build_bundle "$TOOLS_BRIDGE_DIR" '@embody/dsh-tools-bridge'
build_bundle "$SUPERVISOR_LOOP_DIR" '@embody/dsh-supervisor-loop'
build_bundle "$TASK_CONTEXT_DIR" '@embody/dsh-task-context'
build_bundle "$LOCATION_CONTEXT_DIR" '@embody/dsh-location-context'
build_bundle "$SPEECH_STREAM_DIR" '@embody/dsh-speech-stream'
build_bundle "$FILE_GUARD_DIR" '@embody/dsh-file-guard'
echo

for role in $ROLES; do
  # 未登记的角色会让这里直接退出（lib-roles.sh 已把原因打到 stderr）：角色名拼错不该
  # 表现成「这个角色还没建置，跳过」。
  patch="$(role_profile_patch "$role")"
  if [ ! -f "$patch" ]; then
    echo "== [$role] 缺 profile 层事实源 $patch —— 跳过"
    continue
  fi

  echo "== [$role] 确保 profile 存在且 bundles 齐全 =="
  remove_bundle "$role" "$RETIRED_TOOLS_BUNDLE"
  ensure_bundle "$role" "$ROLEBUS_DIR" '@embody/dsh-rolebus'
  ensure_bundle "$role" "$TOOLS_BRIDGE_DIR" '@embody/dsh-tools-bridge'
  # 文件围栏给每个角色：它管的是这四个角色共同的一条规矩（不许拿命令行类工具读写项目根下的
  # 文件，只放行 runtime_files），不是某个角色的特征。bundle 插的是 disabled: true，四个
  # profile 都打开它（allowedRoot 是同一个值）。
  ensure_bundle "$role" "$FILE_GUARD_DIR" '@embody/dsh-file-guard'
  # 定时观察循环只装在 supervisor：那一行就是本角色的唤醒源，其余角色靠 rolebus 事件唤醒，
  # 装进来也没有任何 profile 会打开它（bundle 插的是 disabled: true）。
  if [ "$role" = supervisor ]; then
    ensure_bundle "$role" "$SUPERVISOR_LOOP_DIR" '@embody/dsh-supervisor-loop'
  fi
  # 待做清单的注入方只装在 orchestrator：清单是交互器写、它读的一份，它手里没有 task_list
  # 工具，靠这一行每轮把归它执行、且已开始的条目摆进模型输入。其余角色要么手里有工具
  # （interactor）、要么与这份清单没有往来，装进来也不会被任何 profile 打开（bundle 插的是
  # disabled: true）。
  if [ "$role" = orchestrator ]; then
    ensure_bundle "$role" "$TASK_CONTEXT_DIR" '@embody/dsh-task-context'
  fi
  # 支持地点清单的注入方装在 interactor 与 supervisor：这两个角色每轮都要按清单判断（交互器判
  # 信息完备性、监督器判任务地点是否合法），而它们的工具白名单里没有 waypoint_nav 那一行。
  # orchestrator / planner 不装——装进来也没人会打开它（bundle 插的是 disabled: true）。
  if [ "$role" = interactor ] || [ "$role" = supervisor ]; then
    ensure_bundle "$role" "$LOCATION_CONTEXT_DIR" '@embody/dsh-location-context'
  fi
  # 流式语音只装在 interactor：出声的只有交互器（speak / speak_listen / speak_input 三个
  # 工具只在它的白名单里），其余角色的 profile 也不会打开这一行（bundle 插的是 disabled: true）。
  if [ "$role" = interactor ]; then
    ensure_bundle "$role" "$SPEECH_STREAM_DIR" '@embody/dsh-speech-stream'
  fi

  echo "== [$role] 写入 profile 层覆盖（来自 ${patch#"$REPO_ROOT"/}）=="
  cp "$patch" "$DSH_HOME/profiles/$role/cordis.patch.yml"

  echo "-- [$role] 当前 bundles:"
  node -e "console.log(JSON.stringify(require('$DSH_HOME/profiles/$role/package.json').dsh.profile.bundles, null, 2))"
  echo
done

echo "OK: 角色 profiles 已就绪。"
