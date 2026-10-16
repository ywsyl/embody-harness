/**
 * @file motion_client_socket.cpp
 * @brief 快速运动控制客户端 - Socket 实现
 *
 * 使用 Unix Domain Socket 通信
 * 响应速度: ~10ms (比 ROS 2 服务快 5 倍)
 */

#include <iostream>
#include <cstring>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "motion_protocol.hpp"

double parseDoubleArg(const char* value, const char* arg_name)
{
    try {
        size_t pos = 0;
        const std::string text(value);
        const double parsed = std::stod(text, &pos);
        if (pos != text.size()) {
            throw std::invalid_argument("contains trailing characters");
        }
        return parsed;
    } catch (const std::exception&) {
        std::cerr << "❌ 参数无效: " << arg_name << " = " << value << "\n";
        throw;
    }
}

bool parseInterruptArg(const char* value)
{
    const std::string interrupt_str(value);
    if (interrupt_str == "1" || interrupt_str == "true" || interrupt_str == "True") {
        return true;
    }
    if (interrupt_str == "0" || interrupt_str == "false" || interrupt_str == "False") {
        return false;
    }

    std::cerr << "❌ 打断模式参数无效: " << value << "，仅支持 1/0/true/false\n";
    throw std::invalid_argument("invalid interrupt arg");
}

/** 是否是会被服务端受理、需要等到运动结束才回包的运动命令。
 *
 *  stop 及未知命令是立即回包的，不能套用「阻塞等待」的提示语，否则 stop 会打印
 *  「持续运动中，等到收到停止信号才返回」——而它自己就是那个停止信号。
 */
bool isMotionCommand(const char* command)
{
    static const char* kMotionCommands[] = {
        "forward", "backward", "left", "right", "turnleft", "turnright"
    };
    for (const char* name : kMotionCommands) {
        if (strcmp(command, name) == 0) {
            return true;
        }
    }
    return false;
}

void showHelp(const char* program_name)
{
    std::cout << "用法: " << program_name << " <命令> [速度] [持续时间秒] [打断模式]\n\n";
    std::cout << "🚶 直线运动:\n";
    std::cout << "  forward [速度] [时间] [打断]   - 前进\n";
    std::cout << "  backward [速度] [时间] [打断]  - 后退\n";
    std::cout << "  left [速度] [时间] [打断]      - 左移\n";
    std::cout << "  right [速度] [时间] [打断]     - 右移\n\n";
    std::cout << "🔄 旋转:\n";
    std::cout << "  turnleft [速度] [时间] [打断]  - 左转\n";
    std::cout << "  turnright [速度] [时间] [打断] - 右转\n\n";
    std::cout << "⏹️ 控制:\n";
    std::cout << "  stop             - 停止\n\n";
    std::cout << "打断模式:\n";
    std::cout << "  1 或 true  - 立即打断当前动作（默认）\n";
    std::cout << "  0 或 false - 等待当前动作完成后执行\n\n";
    std::cout << "⏳ 返回时机:\n";
    std::cout << "  命令会阻塞到这次运动真正结束才返回——结束 = 时长到达自动停止 /\n";
    std::cout << "  收到 stop / 被新命令打断。时间给 0（持续运动）时会一直等到 stop。\n";
    std::cout << "  想在运动中途打断，请把本命令放到后台执行（末尾加 &）。\n\n";
    std::cout << "示例:\n";
    std::cout << "  " << program_name << " forward 0.5 10        # 前进 10 秒，10 秒后命令才返回\n";
    std::cout << "  " << program_name << " turnleft 0.3 5 0      # 左转 5 秒（排队模式）\n";
    std::cout << "  " << program_name << " forward 0.5 0 1 &     # 持续前进，后台执行\n";
    std::cout << "  " << program_name << " stop                  # 停止（并让上面那条命令返回）\n";
}

int main(int argc, char** argv)
{
    if (argc < 2) {
        showHelp(argv[0]);
        return 1;
    }

    MotionCommand cmd{};
    copyCString(cmd.command, argv[1]);

    try {
        cmd.speed = (argc >= 3) ? parseDoubleArg(argv[2], "速度") : 0.5;
        cmd.duration = (argc >= 4) ? parseDoubleArg(argv[3], "持续时间") : 0.0;
        cmd.interrupt = (argc >= 5) ? parseInterruptArg(argv[4]) : true;
    } catch (const std::exception&) {
        showHelp(argv[0]);
        return 1;
    }

    // 创建 socket
    int sock_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock_fd < 0) {
        std::cerr << "❌ 创建 socket 失败\n";
        return 1;
    }

    // 设置地址
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    copyCString(addr.sun_path, kMotionSocketPath);

    // 连接服务器
    if (connect(sock_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        std::cerr << "❌ 连接服务器失败\n";
        std::cerr << "💡 请先启动服务: ./start.sh\n";
        close(sock_fd);
        return 1;
    }

    std::cout << "📤 发送命令: " << cmd.command
              << ", 速度: " << cmd.speed
              << ", 时长: " << cmd.duration << "秒"
              << ", 打断: " << (cmd.interrupt ? "是" : "否") << "\n";

    // 发送命令
    if (send(sock_fd, &cmd, sizeof(cmd), 0) != sizeof(cmd)) {
        std::cerr << "❌ 发送命令失败\n";
        close(sock_fd);
        return 1;
    }

    // 接收响应。服务端受理运动命令后不再立刻回包，而是等这次运动真正结束
    // （时长到达 / 收到 stop / 被新命令打断）才回——所以这里会阻塞到运动结束。
    if (isMotionCommand(cmd.command)) {
        if (cmd.duration > 0) {
            std::cout << "⏳ 运动中，约 " << cmd.duration << " 秒后返回"
                      << "（提前收到停止信号则提前返回）...\n";
        } else {
            std::cout << "⏳ 持续运动中，等到收到停止信号才返回...\n";
        }
    }

    MotionResponse resp;
    if (recv(sock_fd, &resp, sizeof(resp), 0) != sizeof(resp)) {
        std::cerr << "❌ 接收响应失败\n";
        close(sock_fd);
        return 1;
    }

    // 显示结果
    if (resp.success) {
        std::cout << "✅ " << resp.message << "\n";
    } else {
        std::cerr << "❌ " << resp.message << "\n";
    }

    close(sock_fd);
    return resp.success ? 0 : 1;
}
