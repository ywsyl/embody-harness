# 引擎钉扎记录（Engine Pin）

> **迁移待改版（2026-09-09）**：本文随项目迁入 `projects/embody-harness/embody/`。引擎形态已改为 **fork 官方仓库**（本地根目录 `projects/embody-harness/`，remote `origin → git@github.com:ywsyl/embody-harness.git`，当前 HEAD = release `0.1.5-alpha.1`），与正文旧表述（独立只读 checkout、pin `0.1.3-alpha.1`）不符。升级策略与依赖解析改为「同仓库 pnpm workspace + 跟随 fork 上游」，本文待据此重写。

上游引擎：`deepseek-harness`（DeepSeek Harness，`@deepseek-ai/dsh-*`）
本地 checkout：`/Users/sylviej/Documents/projects/harness/deepseek-harness`（旧，待删除）

**当前钉扎版本：`0.1.3-alpha.1`**（HEAD = `d347e70390`，即 release 合并点）

## 规则

1. 引擎 checkout **只读**：永不修改其源码、永不向它提交。
2. 本项目（embody-harness）全部代码与配置位于引擎树之外。
3. 依赖 `@deepseek-ai/dsh-*` 一律**精确锁版本**（alpha 期禁用 `^` / `~`）。
4. 升级 = `git -C deepseek-harness fetch origin` → `checkout <新 release tag>` → 更新本文件 → 重编译本仓库插件 → 跑契约/回放测试。

## 升级 Checklist（每次官方发版）

- [ ] fetch 官方新 tag 并切换（确认是 release tag，不是 master 漂移）
- [ ] 更新本文件版本号与 commit
- [ ] 本仓库 TS 插件 `typecheck`，以编译错误为升级地图逐项适配
- [ ] 跑会话级回放/契约测试，捕捉行为级漂移
- [ ] 记录 API 变更（写入本仓库变更日志），标记支持版本
