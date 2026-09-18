#!/usr/bin/env bash
# lib-roles.sh —— 角色 → 本仓库内事实源目录的唯一映射，供 scripts/*.sh 共用。
#
# 约定：每个角色的 profile 事实源放在以角色命名的目录下（<Role>/cordis.patch.yml、
# <Role>/resident.patch.yml、<Role>/prompts/；原名 role-profiles/<role>/ 的形态已随
# interactor、orchestrator 两个角色迁走而废弃）。这份 case 表是「哪个角色在哪个目录」
# 的唯一副本——setup / daemon / watch 三个脚本都从这里取，免得各自写一份后漂移
# （此前 interactor 的路径就散在三处）。新角色先在表里登记，再建目录。
#
# supervisor 的目录里还有一份 robot_status.example.yaml（本体状态观测的形状样例，
# 运行时文件在 runtime_files/，那里整目录 gitignore）。
#
# 依赖调用方先设好 REPO_ROOT（= embody/）。
# 用法：
#   source "$REPO_ROOT/scripts/lib-roles.sh"
#   patch="$(role_profile_patch orchestrator)"

# role_dir <role> —— 该角色的 profile 事实源目录。
# 未登记的角色报错返回 1，而不是猜一个路径：猜出来的路径不存在时，调用方只会看到
# 「缺 profile 层事实源」这种像是没建置的提示，掩盖了「角色名拼错 / 没登记」。
role_dir() {
  case "$1" in
    interactor)   printf '%s\n' "$REPO_ROOT/Interactor" ;;
    orchestrator) printf '%s\n' "$REPO_ROOT/Orchestrator" ;;
    planner)      printf '%s\n' "$REPO_ROOT/Planner" ;;
    supervisor)   printf '%s\n' "$REPO_ROOT/Supervisor" ;;
    *)
      echo "lib-roles: 未知角色 $1 —— 先在 scripts/lib-roles.sh 的 role_dir 里登记它的目录" >&2
      return 1
      ;;
  esac
}

# role_all —— 已登记角色，空格分隔。新角色先加进 role_dir 的 case，再加到这里；两份清单
# 一起改（本文件里没有更松的写法可用：bash 3.2 无关联数组，从 case 里反推角色名不可靠）。
role_all() {
  printf '%s\n' 'interactor orchestrator planner supervisor'
}

# role_profile_patch <role> —— 该角色的 profile 层覆盖层（“profile 层”，位于 bundle 层之后）。
# 两个 *_patch 都要显式接住 role_dir 的失败：直接写进 printf 的参数位置时，命令替换失败
# 不会让 printf 失败，函数照样返回 0 并打印出 "/cordis.patch.yml" 这种半截路径。
role_profile_patch() {
  local dir
  dir="$(role_dir "$1")" || return 1
  printf '%s\n' "$dir/cordis.patch.yml"
}

# role_resident_patch <role> —— 该角色的常驻待机叠加层（只经 --patch 叠加，不进 profile 层）。
role_resident_patch() {
  local dir
  dir="$(role_dir "$1")" || return 1
  printf '%s\n' "$dir/resident.patch.yml"
}

# role_session_id <role> —— 该角色常驻进程持有的会话 id：会话日志的目录名、以及「同一
# DSH_HOME 同时只能有一个常驻进程」那条写租约的键都是它（role-watch 靠它定位日志）。
# 事实源分两处，本函数不自己造第三个：
#   - 事件型角色（interactor / orchestrator / planner）：rolebus 的常驻 agent 固定用
#     rolebus-<role>（dsh-plugins/rolebus/src/resident.ts 的 openResident，不在配置里）；
#   - 定时观察型（supervisor）：会话 id 写在 <Role>/resident.patch.yml 的唤醒源行里，
#     这里直接从那份叠加层读——抄一份字面量就会漂。
role_session_id() {
  local patch id=""
  patch="$(role_resident_patch "$1" 2>/dev/null || true)"
  if [ -n "$patch" ] && [ -f "$patch" ]; then
    # 常驻叠加层里带 sessionId 的只有唤醒源那一行；真出现多处时以第一处为准。
    id="$(sed -n 's/^[[:space:]]*sessionId:[[:space:]]*//p' "$patch" | head -1)"
  fi
  [ -n "$id" ] || id="rolebus-$1"
  printf '%s\n' "$id"
}

# role_default_dsh_home <role> —— 本地联调缺省的隔离 DSH_HOME（仓库内、已 gitignore）。
role_default_dsh_home() {
  printf '%s\n' "$REPO_ROOT/.dsh-dev/$1"
}
