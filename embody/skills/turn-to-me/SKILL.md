---
name: turn-to-me
description: 跟随定位功能，目前只能转向声源为止。通过发布指令让机器狗转向声源位置。使用 /follow_me/start 和 /follow_me/end topic 接口。
---

# 跟随定位技能
控制机器狗转向声源位置，融合了视觉识别、UWB定位等多种传感器信息。

## 开启跟随定位
```bash
skills/turn-to-me/scripts/summon_start.sh
```
发送"start"到 /follow_me/start topic，触发机器狗转向声源

## 关闭跟随定位
```bash
skills/turn-to-me/scripts/summon_stop.sh
```

---

## 技术细节

- **开启Topic**: `/follow_me/start` (std_msgs/msg/String, data: "start")
- **关闭Topic**: `/follow_me/end` (std_msgs/msg/String, data: "stop")
- **订阅节点**: `/control_catch_turtle_v1`
- **ROS_DOMAIN_ID**: 2

## 使用场景

- "开启跟随定位"、"打开定向" → `summon_start.sh`
- "关闭跟随定位"、"停止定向" → `summon_stop.sh`
