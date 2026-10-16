#!/bin/bash
# 启动机器狗运动控制 Socket 服务节点

# 加载 ROS 2 环境
source /opt/ros/foxy/setup.bash
source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" priority="default" multicast="default" /></Interfaces><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'
export ROS_DOMAIN_ID=2
source /root/cmcc_robot/setup.bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀 启动机器狗运动控制服务"
echo "📡 Socket: /tmp/robot_dog_motion.sock"
echo "⚡ 响应速度: ~10ms"
echo ""
echo "💡 使用方法:"
echo "  ./motion.sh forward 0.5 10    # 前进"
echo "  ./motion.sh turnleft 0.3 5    # 左转"
echo "  ./motion.sh stop              # 停止"
echo ""

# 运行服务
#
# exec：用 server 替换掉本 shell，让 server 直接成为 systemd 的主进程。
# 不 exec 的话 bash 会 fork 出子进程，而 unit 里是 KillMode=process —— systemd 只把
# SIGTERM 发给主进程（bash），server 收不到，于是：
#   1) server 的优雅退出走不到（signalHandler / 析构里唤醒阻塞中的客户端那条路），
#      正阻塞在 `./motion.sh forward 0.5 0` 的调用方只能看到连接被断开；
#   2) systemd 不清理 cgroup 里剩下的 server，会留下孤儿进程占着
#      /tmp/robot_dog_motion.sock（若停服时正在运动，它还会继续发速度指令）；
#   3) server 崩溃但 bash 还活着时 unit 仍算 active，Restart=on-failure 不会触发。
exec "$SCRIPT_DIR/server"
