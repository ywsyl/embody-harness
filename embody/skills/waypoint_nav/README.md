# Navigation Service

机器人导航 HTTP 能力服务。外部 LLM、Agent 或业务系统通过语义级 HTTP API 调用本服务，本服务再转换为 ROS2 Topic 消息。

## 运行

```bash
pip install -r requirements.txt
python main.py
```

也可以使用：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

如果当前机器未安装或未启动 ROS2，HTTP 服务仍会启动；导航/定位接口会返回：

```json
{
  "code": 1006,
  "message": "ROS2 navigation service is unavailable.",
  "data": null
}
```

## HTTP API

统一响应格式：

```json
{
  "code": 0,
  "message": "success",
  "data": {}
}
```

接口：

- `GET /api/v1/landmarks`
- `GET /api/v1/localization/status`
- `POST /api/v1/localization/relocalize`
- `POST /api/v1/navigation/go`
- `POST /api/v1/navigation/cancel`
- `GET /api/v1/navigation/status`

## ROS2 Topics

发布：

- `/localization/control`，`std_msgs/String(JSON)`
  - `action=2` 自动重定位
- `/navigation/control`，`std_msgs/String(JSON)`
  - `action=0` 导航
  - `action=1` 取消导航

订阅：

- `/localization/odom`，`nav_msgs/Odometry`
- `/task_status`，`std_msgs/String(JSON)`

## 地标文件

地标保存在 [app/data/landmarks.json](app/data/landmarks.json)。

格式：

```json
[
  {
    "name": "会议室",
    "description": "一楼会议室",
    "pose": {
      "x": 12.5,
      "y": 18.3,
      "yaw": 180
    }
  }
]
```

文件不存在时，服务会自动创建空数组文件。
