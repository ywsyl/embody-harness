# vision_language_nav 视觉导航技能说明

**视觉导航（vision_language_nav）**：接收一段自然语言导航指令，利用视觉逐渐靠近目标，
运行 OmniNav 机器人导航栈直到收到 STOP，返回机器可读的结果。

> ⚠️ **与定点导航 waypoint_nav 的区别**：waypoint_nav 是预先建图并设置好地点（点位），
> 导航时输入**地点名称**前往对应坐标点；vision_language_nav 则是输入一段**自然语言指令**、
> 利用视觉自由规划路径、逐渐靠近目标（如"向前走并停在椅子旁"）。
>
> ⚠️ **输入要求**：自然语言指令必须翻译为**英文**再传入。

输入：

输入：

```json
{
  "instruction": "Walk forward and stop beside the chair."
}
```

该工具仅用于实时机器人导航，不适用于离线图像或视频推理接口。

## 机器人命令

在机器人上调用如下命令：

```bash
/root/omninav_stack_runs/dog_omninav_stack_isolated_20260701/scripts/run_vln_instruction.sh \
  "$INSTRUCTION"
```

同时支持通过标准输入传入纯文本：

```bash
printf '%s' "$INSTRUCTION" | \
  /root/omninav_stack_runs/dog_omninav_stack_isolated_20260701/scripts/run_vln_instruction.sh
```

该辅助脚本负责完整的导航生命周期：

1. 拒绝空指令。
2. 若 OAK 进程或机器人桥接（robot bridge）缺失，则启动 `start_stack.sh`。
3. 要求存在不超过五秒的新鲜机器人画面帧。
4. 禁用上一会话的命令发布。
5. 以全新的 OmniNav 上下文提交该指令。
6. 忽略来自更早指令序列的缓存结果。
7. 在第一条匹配轨迹到达后启用执行。
8. 由现有的机器人桥接和 Pure Pursuit 控制器执行服务器返回的每一条轨迹。
9. 持续执行，直到匹配的 OmniNav 结果发出 STOP。
10. 在收到 STOP、超时、网络故障或被中断后禁用控制。

辅助脚本不实现第二个控制器，也不自行发布速度。

## 进程契约

- 进度日志写入 stderr。
- stdout 恰好包含一个 JSON 对象。
- 退出码 `0` 表示 `status=ok`，且确实收到了显式的 OmniNav STOP。
- 非零退出码表示 `status=error`。
- 超时或传输故障绝不能被解释为 STOP。

成功结果：

```json
{
  "status": "ok",
  "instruction": "Walk forward and stop beside the chair.",
  "instruction_seq": 42,
  "stop": true,
  "episode_done": true,
  "arrive_pred": 1,
  "request_idx": 73,
  "model_infer_sec": 0.286,
  "path_length_m": 4.8,
  "robot_pose": {"x": 1.2, "y": -0.4, "yaw": 0.1},
  "raw_result": {}
}
```

错误结果：

```json
{
  "status": "error",
  "error": {
    "code": "no_live_frame",
    "message": "robot frame did not become fresh within 45s"
  }
}
```

## 技能边界

生成的 OpenClaw/Hermes 技能应当只做以下事情：

1. 校验 `instruction` 是否为非空字符串。
2. 以该指令作为唯一参数执行机器人命令。
3. 将进程超时设置得高于 `OMNINAV_RESULT_TIMEOUT_SEC`（默认 900 秒）。
4. 解析 stdout JSON，并保留 stderr 作为诊断日志。
5. 将解析后的 JSON 返回给上游 Agent。

它绝不能重新实现 HTTP 轮询、相机采集、里程计、轨迹转换、Pure Pursuit、
速度发布、模型部署或 Web Relay 等能力。
它绝不能与辅助脚本并行调用裸的会话控制接口。

同一时刻只能有一个来源向活动的 OmniNav 会话提交指令。
不要同时使用 Web Relay 和 OpenClaw 来替换正在执行的指令。

## 配置

机器人辅助脚本的默认配置如下：

```text
OMNINAV_URL=https://business.homibot.komect.com:9443/omninav
OMNINAV_NETRC=/root/.netrc
OMNINAV_RESULT_TIMEOUT_SEC=900
OMNINAV_STARTUP_TIMEOUT_SEC=45
OMNINAV_MAX_FRAME_AGE_SEC=5
OMNINAV_POLL_INTERVAL_SEC=0.2
```

凭据保存在机器人的密钥存储中或 `/root/.netrc` 中；绝不要将其放入技能提示词、
源代码、日志或返回的 JSON 中。

## 验收标准

1. 在机器人可能移动之前拒绝空输入。
2. 过期或缺失的画面帧会阻止任务提交。
3. 辅助脚本绝不接受来自更早指令的轨迹或 STOP。
4. 现有的 Pure Pursuit 控制器仍是唯一的轨迹执行器。
5. 新的 OmniNav STOP 返回 `status=ok`、`stop=true` 和退出码 `0`。
6. 网络、启动、控制与执行超时等错误均返回 `status=error`，绝不返回 STOP。
7. 中断工具会禁用系统控制发布。
