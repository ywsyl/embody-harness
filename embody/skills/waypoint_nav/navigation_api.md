# Navigation Service API V1

## 1. 简介

Navigation Service 为智能体（Agent）、大语言模型（LLM）提供机器人定位和导航能力。

该 API 面向语义级机器人控制。

机器人内部已经完成：

- 地图创建；
- 地图加载；
- 地标配置。


---

# 2. 通用数据结构


## 2.1 Task


部分耗时操作采用异步任务。


|字段|类型|说明|
|-|-|-|
|task_id|string|任务ID|


说明：

task_id 仅用于机器人内部任务跟踪。

Agent 通常无需保存或查询 task_id。


## 2.2 Pose

机器人或地标在地图坐标系下的位姿与坐标。

|字段|类型|说明|
|-|-|-|
|x|float|地图 X 轴坐标（单位：米）|
|y|float|地图 Y 轴坐标（单位：米）|
|yaw|float|偏航角 / 朝向角（单位：度，范围通常为 -180 ~ 180）|

说明：

Pose 表示机器人在当前加载地图中的几何坐标与朝向。


---

# 3. Landmark 地标能力


## 3.1 获取可导航地点


HTTP:

```

GET /api/v1/landmarks

````


功能：

获取机器人支持导航的地点列表。


Response:


```json
{
    "code":0,
    "message":"success",
    "data":[
        {
            "name":"大厅",
            "description":"入口大厅"
        },
        {
            "name":"会议室",
            "description":"一楼会议室"
        }
    ]
}
````

字段说明：

| 字段          | 类型     | 说明   |
| ----------- | ------ | ---- |
| name        | string | 地点名称 |
| description | string | 地点描述 |

说明：

该接口用于：

* LLM理解机器人可到达的位置；
* Agent选择导航目标。

---

# 4. Localization 定位能力

## 4.1 查询定位状态

HTTP:

```
GET /api/v1/localization/status
```

功能：

查询机器人当前是否已经完成定位。

Response:

```json
{
    "code":0,
    "message":"success",
    "data":{
        "localized":true
    }
}
```

字段：

| 字段        | 类型   | 说明     |
| --------- | ---- | ------ |
| localized | bool | 是否完成定位 |

---

## 4.2 自动重定位

HTTP:

```
POST /api/v1/localization/relocalize
```

功能：

启动自动重定位。

Request:

```json
{}
```

Response:

```json
{
    "code":0,
    "message":"success",
    "data":{
        "task_id":"loc_001"
    }
}
```

说明：

机器人根据当前环境自动在地图上完成初始重定位。

执行完成后：

```
GET /api/v1/localization/status
```

查询定位结果。

---

## 4.3 查询当前坐标与位姿

HTTP:

```
GET /api/v1/localization/pose
```

功能：

查询机器人在当前地图中的实时坐标与位姿（朝向）。

Request:

无（GET 请求，无需请求体）

Response:

```json
{
    "code":0,
    "message":"success",
    "data":{
        "x":0.570,
        "y":-0.109,
        "yaw":11.47
    }
}
```

字段说明：

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| x | float | 地图坐标系 X 轴坐标（单位：米） |
| y | float | 地图坐标系 Y 轴坐标（单位：米） |
| yaw | float | 偏航角 / 朝向角（单位：度，范围通常为 -180 ~ 180） |

说明：

* 机器人已预先完成重定位，在地图中处于已知位姿状态；
* 调用此接口可直接获取机器人在当前地图坐标系下的实时坐标 `(x, y)` 及朝向偏航角 `yaw`。

---

# 5. Navigation 导航能力

## 5.1 导航到指定地点

HTTP:

```
POST /api/v1/navigation/go
```

功能：

导航机器人到指定地标。

Request:

```json
{
    "destination":"会议室"
}
```

参数说明：

| 参数          | 类型     | 说明     |
| ----------- | ------ | ------ |
| destination | string | 目标地点名称 |

Response:

```json
{
    "code":0,
    "message":"success",
    "data":{
        "task_id":"nav_001"
    }
}
```

内部处理：

```
地点名称

↓

查询内部Landmark

↓

获取地图坐标

↓

调用ROS2导航

↓

执行导航
```

---

## 5.2 导航到指定坐标

HTTP:

```
POST /api/v1/navigation/go_pose
```

功能：

直接通过地图坐标与朝向角导航机器人到目标位置。

Request:

```json
{
    "x": 0.0,
    "y": 0.0,
    "yaw": 0.0
}
```

参数说明：

| 参数 | 类型 | 是否必填 | 说明 |
| ---- | ---- | -------- | ---- |
| x | float | 是 | 目标点在地图坐标系下的 X 轴坐标（单位：米） |
| y | float | 是 | 目标点在地图坐标系下的 Y 轴坐标（单位：米） |
| yaw | float | 否 | 目标点偏航朝向角（单位：度，范围通常为 -180 ~ 180，默认 0.0） |

Response:

```json
{
    "code":0,
    "message":"success",
    "data":{
        "task_id":"nav_001"
    }
}
```

内部处理：

```
目标坐标 (x, y, yaw)

↓

组装 ROS2 导航目标点

↓

调用 ROS2 导航控制

↓

执行导航
```

---

## 5.3 取消导航

HTTP:

```
POST /api/v1/navigation/cancel
```

功能：

取消当前导航任务。

Request:

```json
{}
```

Response:

```json
{
    "code":0,
    "message":"success"
}
```

---

## 5.4 查询当前导航状态

HTTP:

```
GET /api/v1/navigation/status
```

功能：

查询当前导航执行状态。

Response:

```json
{
    "code":0,
    "message":"success",
    "data":{
        "status":"RUNNING",
        "destination":"会议室"
    }
}
```

字段说明：

| 字段          | 类型     | 说明   |
| ----------- | ------ | ---- |
| status      | string | 导航状态 |
| destination | string | 目标地点 |

导航状态：

| 状态        | 说明      |
| --------- | ------- |
| IDLE      | 当前无导航任务 |
| RUNNING   | 导航执行中   |
| SUCCESS   | 导航完成    |
| FAILED    | 导航失败    |
| CANCELLED | 导航取消    |

---

# 6. Accompany 跟随能力

## 6.1 开启跟随

HTTP:

```
POST /api/v1/accompany/start
```

功能：

启动机器人跟随模式（跟随前方目标/人员移动）。

Request:

```json
{}
```

Response:

```json
{
    "code":0,
    "message":"success",
    "data":{
        "task_id":"acc_001"
    }
}
```

内部处理：

```
HTTP 请求

↓

发布 ROS2 话题：/accompany/start
消息类型：std_msgs/msg/String
消息内容：{"data": "start"}

↓

机器人启动跟随控制
```

---

## 6.2 停止跟随

HTTP:

```
POST /api/v1/accompany/stop
```

*(说明：也可兼容调用 `POST /api/v1/accompany/end`)*

功能：

停止/退出机器人跟随模式。

Request:

```json
{}
```

Response:

```json
{
    "code":0,
    "message":"success"
}
```

内部处理：

```
HTTP 请求

↓

发布 ROS2 话题：/accompany/end
消息类型：std_msgs/msg/String
消息内容：{"data": "end"}

↓

机器人停止跟随动作并驻留
```

---

## 6.3 查询跟随状态

HTTP:

```
GET /api/v1/accompany/status
```

功能：

查询机器人当前是否处于跟随运行状态。

Response:

```json
{
    "code":0,
    "message":"success",
    "data":{
        "status":"RUNNING"
    }
}
```

字段说明：

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| status | string | 跟随状态（IDLE: 空闲未跟随 / RUNNING: 跟随进行中） |

---

# 7. 错误码

| code | 说明      |
| ---- | ------- |
| 0    | 成功      |
| 1001 | 机器人未定位  |
| 1002 | 目标地点不存在 |
| 1003 | 导航任务执行中 |
| 1004 | 目标不可达   |
| 1005 | 自动重定位失败 |
| 1006 | 导航失败    |
| 1007 | 任务取消    |
| 1008 | 跟随服务异常  |
| 1099 | 系统错误    |

---

# 8. Agent典型调用流程

## 8.1 流程一：导航到指定地点

### 用户：

> 去会议室

---

## Step 1：确认目标地点

调用：

```
GET /api/v1/landmarks
```

确认存在：

```
会议室
```

---

## Step 2：确认机器人定位状态

调用：

```
GET /api/v1/localization/status
```

如果返回：

```json
{
    "localized":false
}
```

执行：

```
POST /api/v1/localization/relocalize
```

等待定位完成。

再次查询：

```
GET /api/v1/localization/status
```

直到：

```json
{
    "localized":true
}
```

---

## Step 3：开始导航

调用：

```
POST /api/v1/navigation/go
```

请求：

```json
{
    "destination":"会议室"
}
```

---

## Step 4：等待导航完成

周期查询：

```
GET /api/v1/navigation/status
```

直到：

```json
{
    "status":"SUCCESS"
}
```

---

## 8.2 流程二：查询当前位置坐标

### 用户：

> 你当前在地图的哪个位置？/ 汇报当前坐标

---

### Step 1：查询当前坐标与位姿

调用：

```
GET /api/v1/localization/pose
```

返回：

```json
{
    "code":0,
    "message":"success",
    "data":{
        "x":0.570,
        "y":-0.109,
        "yaw":11.47
    }
}
```

Agent 获取到实时坐标后，可直接向用户汇报当前坐标数值，或结合已知地标列表计算当前距离最近的地标。

---

## 8.3 流程三：直接导航到指定坐标

### 用户：

> 请移动到坐标 x=0, y=0, yaw=0

---

### Step 1：发起坐标导航

调用：

```
POST /api/v1/navigation/go_pose
```

请求：

```json
{
    "x": 0.0,
    "y": 0.0,
    "yaw": 0.0
}
```

返回：

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "task_id": "nav_002"
    }
}
```

---

### Step 2：等待导航完成

周期查询：

```
GET /api/v1/navigation/status
```

直到：

```json
{
    "status": "SUCCESS"
}
```

---

## 8.4 流程四：开启与停止人员跟随

### 用户：

> 跟着我走 / 开启跟随

---

### Step 1：启动跟随模式

调用：

```
POST /api/v1/accompany/start
```

请求：

```json
{}
```

返回：

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "task_id": "acc_001"
    }
}
```

Agent 语音向用户播报提示：“已开启跟随模式，我将跟随您移动。”

---

### 用户：

> 停下来 / 结束跟随

---

### Step 2：停止跟随模式

调用：

```
POST /api/v1/accompany/stop
```

请求：

```json
{}
```

返回：

```json
{
    "code": 0,
    "message": "success"
}
```

Agent 语音向用户播报提示：“已退出跟随模式。”




