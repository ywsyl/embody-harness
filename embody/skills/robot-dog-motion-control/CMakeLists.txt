cmake_minimum_required(VERSION 3.8)
project(robot_dog_motion_control)

# 设置 C++17 标准
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 编译选项
if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# 查找依赖
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(geometry_msgs REQUIRED)

# 服务端（需要 ROS 2）
add_executable(server src/server.cpp)
ament_target_dependencies(server
  rclcpp
  geometry_msgs
)

# 客户端（纯 C++，不依赖 ROS）
add_executable(client src/client.cpp)

# 安装可执行文件
install(TARGETS
  server
  client
  DESTINATION lib/${PROJECT_NAME}
)

# 安装脚本
install(PROGRAMS
  scripts/start.sh
  scripts/motion.sh
  DESTINATION share/${PROJECT_NAME}/scripts
)

# 测试
if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
