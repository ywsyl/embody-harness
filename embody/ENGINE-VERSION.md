# 引擎钉扎记录（Engine Pin）

> **修订记录**：2026-09-09 改写。引擎形态由「独立只读 checkout + Python 驱动打包可执行文件」改为 **fork 官方仓库 + 同仓库 pnpm workspace**；前版 pin `0.1.3-alpha.1`（checkout `d347e70390`）作废，旧独立 checkout 已删除（备份在 `/tmp/embody-harness-migration-backup-20260909/`）。

## 当前形态（2026-09-09 起）

- **引擎 = fork 官方 deepseek-harness 仓库，即本仓库根 `projects/embody-harness/`**。基线 = 官方 release **`0.1.5-alpha.1`**（引擎基线 commit `5dda764ed3`，即官方 release 合并点）。`origin → git@github.com:ywsyl/embody-harness.git`（用户 fork）；官方本体另作 `upstream` remote，用于 fetch + merge 跟随更新。
- **我们的开发目录 = `embody/`**（本文件所在），与引擎同仓并存。**不修改 fork 根引擎源码**（`apps/ packages/ docs/ python/ …`），使其与上游保持一致、上游 merge 干净；embody 提交与引擎路径不相交。
- 本仓库 `master` 首批 embody 提交：`d54eed50aa`（迁入骨架）、`ccad09e12b`（scripts 的 `ENGINE_DIR`/`NODE22_BIN` 改为 fork 根）。此后每次跟随官方发版，embody 提交与引擎更新叠在同一历史中。
- **依赖 / 运行**：同仓库 pnpm workspace —— fork 根 `pnpm install` 一次装齐，`pnpm dsh`（`apps/cli` 走 tsx 源码直跑，无需打包）。新增 dsh bundle/plugin 以本地目录被角色 profile 引用（`dsh plugin --profile <role> add <本地目录>` = pnpm link），**免发布、无 registry 版本语义**——版本由本文件对 fork HEAD 的钉扎表达，不存在 `^` / `~` 漂移问题。

## 本机配方（run / validate 前必读）

系统默认 Node 26 的头文件缺 `<source_location>`，本机 clang 无法为其编原生模块（fs-ext / node-pty / koffi）；且 pnpm 11 每次跑 bin 前会重跑 deps 检查、lefthook postinstall 会改 git hooks。因此一律：

- `PATH="<旧> harness/.runtime/node22/bin:$PATH"` —— node22 运行时仍在 `projects/harness/.runtime/node22/bin`（跨仓库绝对路径）。`embody/scripts/*.sh` 已默认解析此路径，可用 `NODE22_BIN` 覆盖。
- `export CI=true npm_config_verify_deps_before_run=false`（CI 使 lefthook 安装器直接 return，不改 git hooks）。
- **里程碑验证**：隔离 `DSH_HOME=$(mktemp -d)` → `embody/scripts/rolebus-setup.sh` → `embody/scripts/rolebus-verify.sh`（二者 `ENGINE_DIR` 默认即 fork 根）。里程碑 0（`--dump-config` 分层断言，免模型 key）已在本形态从零复跑 **ALL PASS**。

## 规则

1. **只读引擎源码**：fork 根属上游，不本地改动、不向它提交引擎文件；本项目唯一写入面是 `embody/`。
2. **跟随官方（升级）**：fork 仓库 `git fetch upstream` → merge 官方 release tag 进 `master` → 更新本文件钉扎记录 → 复跑里程碑验证。
3. **不发布 dsh 包**：插件/角色配置都以本地目录 + profile 覆盖层表达（见 `项目架构.md` §0 与 `dsh-plugins/`、`role-profiles/`）。
4. **行为漂移靠复跑捕捉**：里程碑 0 的 dump-config 分层断言是基线；里程碑 1 起叠加本仓 TS 插件 `typecheck` 与会话级回放/契约测试。

## 升级 Checklist（每次官方发版）

- [ ] `git fetch upstream`，确认官方 **release tag**（不是 master 漂移）
- [ ] merge tag 进 `master`（embody/ 与引擎路径不相交，通常干净；若有冲突仅在引擎侧，需人工判断取舍）
- [ ] 更新本文件：版本号 / 引擎基线 commit / 日期
- [ ] 隔离 `DSH_HOME` 复跑 `embody/scripts/rolebus-setup.sh` + `rolebus-verify.sh`（基线须 ALL PASS）
- [ ] 里程碑 1+：本仓 TS 插件 `typecheck`、会话回放/契约测试，捕捉行为级漂移
- [ ] 记录 API/行为变更（写入变更日志），标注支持版本
