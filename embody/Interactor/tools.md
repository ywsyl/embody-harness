interactor可见工具/插件

原生：
工具名	行 id	插件包
bash	tool-bash	dsh-tool-bash（win32 禁用）
pwsh	tool-pwsh	dsh-tool-pwsh（非 win32 禁用）
read write edit read_image	tool-fs	dsh-tool-fs
glob grep	tool-fs-search	dsh-tool-fs-search
job_list job_output job_kill	tool-jobs	dsh-tool-jobs
skill	tool-skill	dsh-tool-skill
web_search web_fetch	tool-web	dsh-tool-web（fetch: true）
create_goal get_goal update_goal	tool-goal	dsh-tool-goal
exit_plan_mode	plan-mode	dsh-plan-mode（内含 plan 模式提示词节）

自定义：
1、speak 语音播报(string,填写播报内容用户能收到的唯一回复途径，语音播报的内容，要语气符合人设和口语习惯,同时简短不宜播报过久)
2、speak_listen 语音播报+收听

rolebus（写给其它角色的事件，工具名与参数由事件目录生成，见 ../dsh-plugins/rolebus/src/event-catalog.ts）：
rolebus_send_task_content_update  把任务交给编排器（人设【本智能体职责】里「多步的具身任务」走它）
rolebus_send_interaction_result   把用户的回答带回给编排器

────────────────────────────────────────────────────────────────

上表如何落实（机制说明；命令与门禁见 ../README.md「里程碑 2」）：

原生工具：由本目录 cordis.patch.yml 实现——清单内的行保持 base 默认不动，清单外的行按
`- id: <row>` + `disabled: true` 显式禁用。当前被禁用的是 user-questions（具身智能体只有语音
通道，提问改走 speak_listen）、tool-subagent / tool-subagent-fork / tool-subagent-control /
tool-subagent-list-agents、tool-workflow、tool-ralph，以及 tool-todo（任务跟踪的归属是
[../plugin_tools/task_board.py](../plugin_tools/task_board.py) 那份待做清单，本角色读、编排器写，
不走会话内的 todo_write；编排器的 profile 同样禁用它）。

自定义工具：定义在 ../plugin_tools/available_tools.yaml，本角色能看到哪些由
../configs/agent_plugin.py 的 INTERACTOR_TOOLS 白名单决定；白名单是唯一闸口，dsh 注册、
ToolParser 的 prompt 文本、tools.llm_tools.format_tools_for_prompt 都从 role_tools(role)
取名单，没有「不过滤」的入口。

**注册方是 @embody/dsh-tools-bridge**（[../dsh-plugins/tools-bridge/](../dsh-plugins/tools-bridge/)）：
装载本 profile 时按 tools-bridge 行的 `role: interactor` 取上面这份白名单（经
`python -m plugin_tools.bridge --role interactor list`），逐条 `ctx.tools.register`；模型每次
调用再经 `call <tool>` 转发给 Python 侧实现。上面两个工具因此直接在工具表里，工具名不加
前缀。取不到清单（角色不在白名单表、清单为空、解释器起不来）即启动失败，不会少挂几个还
装作正常。原注册方 @embody/dsh-embody-tools 已删除，它的悬空 link 由安装脚本摘掉。

rolebus 工具：由 `@embody/dsh-rolebus` 按本 profile 的 `rolebus.role` 与 peers 生成，
不走本文件的白名单（信封形状与工具名都归事件目录）。因此 Python 侧不再有发件工具：
原 `embody_execute` 因与 `rolebus_send_task_content_update` 重复而被摘除。
