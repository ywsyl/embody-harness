#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source /opt/ros/foxy/setup.bash

export ROS_DOMAIN_ID=2
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH}:/usr/local/lib"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH}:/root/RobotAlgorithmGen1/install/map_server/lib/map_server"

source /root/RobotAlgorithmGen1/install/setup.bash

export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" priority="default" multicast="default" /></Interfaces><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'

source /home/unitree/map/map_id_for_nav.bash

exec python main.py
