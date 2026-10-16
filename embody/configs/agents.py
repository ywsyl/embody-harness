# =============================================================================
# Tools — 可用工具过滤器
# =============================================================================

INTERACTOR_TOOLS = (
    # ---- 交互器独有 ----
    # 语音类技能
    "speak",
    # "listen",
    # "speak_listen",
    # "audio_interact",  # 双工
    # "speak_input",

    # 状态相关技能
    "robot_state",
    "task_list", # 交互器按需读；编排器手里没有这个工具（清单每轮由系统注入，见下面那行）

    # 阻塞类工具的停止指令
    "bull_chase_stop",

    # ---- 与编排器共享 ---
    "weather",   
    "vision_tunables",
    "action",
    "gait",
    "move",
    "vision",
    "search_old",
    "waypoint_nav",
)

ORCHESTRATOR_TOOLS = (
    # ---- 编排器独有 ----
    # 阻塞类长工具
    "bull_chase",
    # "search_and_approach",

    # ---- 与交互器共享 ---
    "action",
    "move",
    "vision",
    "vision_tunables",
    "search_old",
    # "search_and_approach",
    "waypoint_nav",
    "weather",   
    # "vision_language_nav",
)

PLANNER_TOOLS = (
)

# 角色 → 工具白名单。键即 profile / 桥 `--role` 的取值，值是 available_tools.yaml
# 里的工具名。任何把工具交给模型的途径（dsh 注册、prompt 文本拼接）都经 role_tools()
# 取名单，不在本文件之外另写一份子集。
ROLE_TOOLS = {
    "interactor": INTERACTOR_TOOLS,
    "orchestrator": ORCHESTRATOR_TOOLS,
    "planner": PLANNER_TOOLS,
}


class UnknownRoleError(LookupError):
    """请求的角色不在对应的角色表里（ROLE_TOOLS / ROLE_THINKING）。

    取不到值的调用方必须失败：当成空集 / 空串处理会让该角色「一个工具都没有」或「开关没生效」
    却不报错。
    """


def role_tools(role: str) -> tuple:
    """取该角色的工具白名单。

    Args:
        role: 角色身份，取值同 profile / stdio 桥的 `--role`。

    Returns:
        该角色可见的工具名元组，顺序即白名单顺序。

    Raises:
        UnknownRoleError: 角色不在 ROLE_TOOLS 里。
    """
    try:
        return tuple(ROLE_TOOLS[role])
    except KeyError:
        raise UnknownRoleError(
            f"configs/agents.py 的 ROLE_TOOLS 无角色 {role!r}，已知: {sorted(ROLE_TOOLS)}"
        ) from None


# =============================================================================
# 各角色常驻 agent 的开关 — 逐角色
# =============================================================================

# 扩展思考开关：**True = 思考开着，False = 关掉**。
#
# 关掉不是给默认路由加个参数，而是**换一条路由**：走 configs/settings-model.yaml 里那条声明了
# 推理档位的镜像路由（见下面的 NO_THINK_MODEL_PROVIDER），并且构造模型选择时**不带
# reasoningEffort** —— 丢掉档位正是关思考那一动作（引擎的 installModelSelection 会消掉继承来的
# 档位，pi-ai 随之发出 thinking:{type:"disabled"}）。
# 不能改成给默认路由 robot-gateway 加推理元数据：那会把所有走它的角色一起关掉，所以那条镜像
# 路由必须与默认路由分开声明。
#
# 表里必须有 role_all() 的每一个角色：漏登记的角色在 role_model_provider() 处报错，而不是悄悄
# 退回默认路由 —— 那种失败是「这个开关根本没生效」，报出来才看得见（与 role_tools() 同一口径）。
ROLE_THINKING = {
    "interactor": False,
    "orchestrator": False,
    "planner": False,
    "supervisor": False,
}

# 关掉思考时改走的路由名（provider）。**必须与 configs/settings-model.yaml 里那条路由同名**
# —— 那里是路由的声明处，这里只是引用它；改名要两边一起改，否则请求会因找不到 provider 而失败。
NO_THINK_MODEL_PROVIDER = "robot-gateway-nothink"


def role_model_provider(role: str) -> str:
    """取该角色常驻 agent 的模型路由覆盖值（喂给 rolebus / supervisor-loop 的 `modelProvider`）。

    返回值是**路由覆盖**而不是布尔：空串表示不改路由（照走 agent-default-model，思考开着），
    非空表示改走那条镜像路由（关掉思考）。这一层由 `scripts/role-daemon.sh` 过桥成进程环境，
    各角色的 resident 叠加层用 `!!js process.env.ROLE_MODEL_PROVIDER` 读回去。

    Args:
        role: 角色身份，取值同 profile / stdio 桥的 `--role`。

    Returns:
        空串（思考开着）或 NO_THINK_MODEL_PROVIDER（关掉）。

    Raises:
        UnknownRoleError: 角色不在 ROLE_THINKING 里。
    """
    try:
        thinking = ROLE_THINKING[role]
    except KeyError:
        raise UnknownRoleError(
            f"configs/agents.py 的 ROLE_THINKING 无角色 {role!r}，已知: {sorted(ROLE_THINKING)}"
        ) from None
    return "" if thinking else NO_THINK_MODEL_PROVIDER


# 交互器流式播报（dsh-plugins/speech-stream）开关：True = 边生成边出声，False = 插件不装载 =
# 行为回到「整段生成完才出声」。它是**启动期**配置，改完要重起交互器（不是 vision_tunables 那种
# 运行时可调层）。同节的 SPEECH_MAX_CHARS_PER_CHUNK / SPEECH_ITEM_STALE_SEC /
# SPEECH_QUEUE_DRAIN_MAX_WAIT 是调参不是开关，留在 configs/configs.py。
SPEECH_STREAM_ENABLED = True


# =============================================================================
# 拉起前清空 — 哪些角色「重开一局」
# =============================================================================

# 表里的角色在**启动它的常驻进程之前**清两样东西：
#   · 历史会话：整份目录挪进 $DSH_HOME/session-archive/（留档，不删），本轮的会话 id 空出来
#     → 引擎建新会话，不续接上一轮；
#   · 收件箱里**还没被领走**的事件（.bad-/.misroute-/.pending- 残留与 archive/ 不动）。
# 表外的角色两样都留：会话续接上一轮（上下文还在），没领走的信留到下一轮。
#
# 两件事绑在同一张表里是刻意的 **一致性**要求，不是顺手合并：会话清了而信留着，角色会被一封
# 它已经不记得的任务的信唤醒（那封信多半指向一个上一轮的任务号）；信清了而会话留着，它记得
# 的任务却等不到后续那封信 —— 单清一样都会得到一个「记得一半」的角色。
#
# 消费者两处，各自取这份名单：`scripts/role-daemon.sh`（归档会话，在该角色拉起之前、
# 任何副作用之前）与 `starter.sh`（清收件箱，刻意排在后台组件之前 —— receiver 一上线就往
# spool 投事件，排在它后面清会把启动窗口里收到的信删掉）。
RESET_ON_START_ROLES = ("interactor", "orchestrator")


# =============================================================================
# 状态值参数 — 任务级（TASK_STATUS）
# =============================================================================
# value / description 之外还可以有 system_only：标了它的取值**模型写不了**（只有系统的初始化
# 会写），见 INTERRUPTED 那条。读法就一处：plugin_tools/task_board.py 按这个标记分出
# 「模型能写的状态」——别处不要再列一份子集。
TASK_STATUS = {
    "PENDING": {
        "value": "pending",
        "description": "该任务已创建但尚未开始执行",
    },
    "EXECUTING": {
        "value": "executing",
        "description": "任务尚未完成，当前任务仍可继续执行",
    },
    # system_only：**只有系统写得了这个取值**（starter.sh 拉起时的初始化，把上一轮停在
    # executing 的任务标成它，见 task_board.interrupt_executing）。模型发 items 改 status 时
    # 写它会被 task_board 拒掉——「谁被中断了」是系统的事实，不是模型能声明的。
    "INTERRUPTED": {
        "value": "interrupted",
        "description": "任务被系统中断：上一轮智能体退出（重启、掉电、被强杀）时还没做完，可以接着做",
        "system_only": True,
    },
    "SUCCEEDED": {
        "value": "succeeded",
        "description": "任务无需后续执行：目标达成或已明确完成",
    },
    "FAILED": {
        "value": "failed",
        "description": "任务被系统停止：用户要求的任务目标已无法合理继续（连续失败且没有合理重试方案）",
    },
    "STOPPED": {
        "value": "stopped",
        "description": "任务被用户停止：用户暂停/中止/触发急停状态等，还可能继续",
    },
    "CANCELLED": {
        "value": "cancelled",
        "description": "任务被用户取消：用户取消/终止等，明确不再需要继续执行",
    },
}

# 待做任务清单（plugin_tools/task_board.py）里 owner 字段的合法值：这条任务归谁做。
# 取值即角色名（同 ROLE_TOOLS 的键）。只有该角色应当去执行它 —— 这条由提示词约束，
# 不是机制强制：内置执行器看不到调用方角色，白名单只能管到「谁手里有这个工具」。
# （谁能**改** owner 是另一件事：owner 只在新建任务时给，之后谁都改不了，见
# plugin_tools/task_board.py 的 _reject_owner_field。）
TASK_OWNER = {
    "INTERACTOR": {
        "value": "interactor",
        "description": "交互器负责执行：与用户直接交互即可完成的任务",
    },
    "ORCHESTRATOR": {
        "value": "orchestrator",
        "description": "编排器负责执行：需要编排多步动作/工具才能完成的任务",
    },
}

# 待做任务清单的**按角色写入限制**：键是角色名（同 ROLE_TOOLS 的键），值是它不许做的事。
# 读法就一处：plugin_tools/task_board.py 的 write 按调用方角色（发出这次改动的角色，rolebus
# 事件的 source）查这张表，表里没有的角色没有额外限制——交互器能建任务，这条任务归谁做本来
# 就由它定（已有任务谁能改是另一回事，见 task_board._reject_owner_write）。
#   no_create：非空 = 本角色不能新建任务；这段字是人话理由，写侧直接把它拼进报错里给模型看。
# 模块自己记账的字段（task_id / create_time / last_update_time / 步骤的 attempts）对**所有**
# 调用方都不许写，不在这张表里（那张表是 task_board._TASK_RESERVED）。
ROLE_TASK_BOARD_LIMITS = {
    "orchestrator": {
        "no_create": "号只能抄云端或交互器带来的那个；没有号（云端没下发过）的活儿交给交互器建",
    },
}

