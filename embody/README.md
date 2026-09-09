# embody-harness

一套基于 [deepseek-harness（dsh）](https://github.com/deepseek-ai)（引擎 = **本仓库父目录的 fork**，基线 release `0.1.5-alpha.1`，见 [ENGINE-VERSION.md](ENGINE-VERSION.md)）的**具身 agent 编排系统**：多个角色（agent / code）各自独立 dsh profile/进程，通过共享事件词汇表解耦协作。

架构顶层见 [reference/项目架构.md](reference/项目架构.md)（§0 讲仓库形态：**开发点 = 本目录 `embody/`，引擎 = 父目录 fork 根**）；角色间消息契约（谁→谁、何时发、payload）唯一事实源是 [reference/events.md](reference/events.md)；引擎钉扎与升级纪律见 [ENGINE-VERSION.md](ENGINE-VERSION.md)。

## 目录

| 路径 | 内容 |
|---|---|
| `reference/` | 架构总述、事件契约 |
| `Interactor/` `Orchestrator/` `Supervisor/` | 三个角色 agent 的实现设计文档 |
| `dsh-plugins/rolebus/` | 跨角色消息总线的 **dsh bundle**（里程碑 0：仅配置层） |
| `role-profiles/<role>/` | 每角色独立 profile 的 **profile 覆盖层**（本仓库即事实源） |
| `scripts/` | 物化角色 profiles + 验证配置分层 |

## 里程碑 0：每角色独立 profile/进程 —— 只验证配置分层（免模型 key）

目标不是跑通任务，而是先实证 dsh 的配置分层能把「每个角色 = 独立 dsh profile/进程」表达对：
**同一份 base bundle + 同一份 rolebus bundle，经各自 profile 层改写后，产出不同且正确的角色构成。**

各角色 `dsh --profile <role>` 的构成按 dsh 引擎分层叠成（后层按行整块胜出）：

```text
bundles（@deepseek-ai/dsh-base → @embody/dsh-rolebus）
  └─ profile 层：$DSH_HOME/profiles/<role>/cordis.patch.yml
       （改写 base 的 system-prompt.persona；改写 rolebus 的 config.role / peers）
       ← 本仓库 role-profiles/<role>/cordis.patch.yml 是其事实源
```

### 验证方法（本机已实测 exit 0）

```sh
# 0) 环境：node22 前置 PATH + CI=true（见 ENGINE-VERSION.md 配方）。脚本自带处理。
# 1) 物化角色 profiles 到 DSH_HOME（默认 ~/.dsh；隔离验证可传 DSH_HOME=$(mktemp -d)）
scripts/rolebus-setup.sh

# 2) 免 boot、免模型 key：对每角色 dump 合成后的配置树并断言分层正确
scripts/rolebus-verify.sh
```

验证断言每条 profile：

1. base 行确实被「本角色 profile 层」覆盖（dump 里有 provenance 注释
   `# == @deepseek-ai/dsh-base, patched by .../profiles/<role>/cordis.patch.yml`）；
2. `system-prompt.persona` 是本角色自己的（`You are the Interactor/Orchestrator/...`），不再是空 persona；
3. `rolebus.config.role == <role>`（bundle 插入的行被 profile 层整块改写）。

`--dump-config` 只做行合成，不解析插件代码、不需要网络与模型 key，因此这套验证可在
CI / 无凭据环境稳定运行。参考实现（引擎 fork 根下）：`../apps/cli/src/dump-config.ts`、`../packages/boot/app-boot/src/profile.ts`。

## 下一步（里程碑 1+）

- rolebus 插件从占位 `apply` 长成真实跨进程事件总线（见 `reference/events.md` 的
  `user_task_*` / `interaction_request` / `execution_*` / `replan_required` 等 event_type），
  各角色用 `rolebus` 收发；executor（Programmatic Runtime）作为 code 参与方先以本地
  观察 + 钩子接入。
- 角色 agent 真身：各角色独立 dsh 进程（SDK/headless 类 profile 承载 session），配各自的
  preset/工具白名单；Program DSL 与 validator、监督器 L1/L2/L3 升级各自落地为插件。
