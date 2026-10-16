/**
 * @file twist_controller_socket_server.cpp
 * @brief 机器狗速度控制 Socket 服务端
 *
 * 使用 Unix Domain Socket 进行进程间通信
 * 响应速度: ~10ms (比 ROS 2 服务快 5 倍)
 *
 * 回包时机（2026-09-15 改）：受理一条运动命令后**不再立刻回包**，而是等这次运动真正
 * 结束才回——结束 = 时长到达自动停止 / 收到 stop 命令 / 被新命令打断 / 排队被清空 /
 * 服务关闭。对应《机器人功能接口规格.md》里 Move 的「结束信号：需补充」。
 * 因此 socketLoop 改为每个连接一个线程：等待期间不能让别的客户端连不进来，否则
 * duration=0 的持续运动没有第二个连接来发 stop，会永远等下去。
 */

#include <rclcpp/rclcpp.hpp>
#include <geometry_msgs/msg/twist.hpp>
#include <poll.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <csignal>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

#include "motion_protocol.hpp"

using namespace std::chrono_literals;

class TwistControllerSocketServer : public rclcpp::Node
{
public:
    TwistControllerSocketServer()
        : Node("twist_controller_socket_server"),
          server_fd_(-1),
          running_(true),
          active_clients_(0),
          is_moving_(false),
          publish_count_(0),
          target_count_(0),
          elapsed_time_(0.0)
    {
        // 创建 Twist 发布者
        twist_publisher_ = this->create_publisher<geometry_msgs::msg::Twist>(
            "/catch_turtle/ctrl_instruct", 10);

        // 创建控制定时器（10Hz）
        control_timer_ = this->create_wall_timer(
            100ms,
            std::bind(&TwistControllerSocketServer::controlCallback, this));

        // 初始化 socket 服务器
        if (!initSocketServer()) {
            RCLCPP_ERROR(this->get_logger(), "❌ Socket 服务器初始化失败");
            rclcpp::shutdown();
            return;
        }

        // 启动 socket 监听线程
        socket_thread_ = std::thread(&TwistControllerSocketServer::socketLoop, this);

        RCLCPP_INFO(this->get_logger(), "🐕 机器狗速度控制 Socket 服务已启动");
        RCLCPP_INFO(this->get_logger(), "📡 Socket: %s", kMotionSocketPath);
        RCLCPP_INFO(this->get_logger(), "⚡ 响应速度: ~10ms");
        RCLCPP_INFO(this->get_logger(), "⏳ 回包时机: 运动结束（停止信号）后");
    }

    ~TwistControllerSocketServer()
    {
        running_ = false;

        // 先唤醒所有等待方，客户端线程才会从 waitForMotionEnd 里退出来
        {
            std::lock_guard<std::mutex> lock(mutex_);
            finishTicket(active_ticket_, "服务关闭");
            active_ticket_.reset();
            if (pending_.has_value()) {
                finishTicket(pending_->ticket, "服务关闭");
                pending_.reset();
            }
        }

        // socketLoop 用 poll(200ms) 轮询 running_，所以这里 join 最多 200ms 就返回
        if (socket_thread_.joinable()) {
            socket_thread_.join();
        }

        if (server_fd_ >= 0) {
            close(server_fd_);
            server_fd_ = -1;
        }

        // 等已接入的客户端线程自行退出（它们最多 200ms 醒一次；线程是 detach 的，
        // 这里只做有界等待，超时就让进程退出把它们带走）
        for (int i = 0; i < 20 && active_clients_.load() > 0; ++i) {
            std::this_thread::sleep_for(100ms);
        }

        unlink(kMotionSocketPath);
    }

private:
    /** 一条已入队、等待前一条运动结束才执行的命令 */
    struct QueuedCommand {
        MotionCommand cmd;
        MotionTicketPtr ticket;
    };

    bool initSocketServer()
    {
        // 删除旧的 socket 文件
        unlink(kMotionSocketPath);

        // 创建 socket
        server_fd_ = socket(AF_UNIX, SOCK_STREAM, 0);
        if (server_fd_ < 0) {
            RCLCPP_ERROR(this->get_logger(), "创建 socket 失败");
            return false;
        }

        // 设置地址
        struct sockaddr_un addr;
        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        copyCString(addr.sun_path, kMotionSocketPath);

        // 绑定
        if (bind(server_fd_, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
            RCLCPP_ERROR(this->get_logger(), "绑定 socket 失败");
            close(server_fd_);
            server_fd_ = -1;
            return false;
        }

        // 监听
        if (listen(server_fd_, 5) < 0) {
            RCLCPP_ERROR(this->get_logger(), "监听 socket 失败");
            close(server_fd_);
            server_fd_ = -1;
            return false;
        }

        return true;
    }

    void socketLoop()
    {
        // poll + 超时轮询 running_：Linux 上 close()/shutdown() 并不会唤醒阻塞在
        // accept() 的线程，靠关 fd 退出不可靠，所以这里自己带 200ms 心跳。
        struct pollfd pfd;
        pfd.fd = server_fd_;
        pfd.events = POLLIN;

        while (running_) {
            const int ready = poll(&pfd, 1, 200);
            if (ready <= 0) {
                continue;   // 超时或被打断：回到循环头检查 running_
            }

            // 接受连接
            int client_fd = accept(server_fd_, nullptr, nullptr);
            if (client_fd < 0) {
                if (running_ && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                    RCLCPP_WARN(this->get_logger(), "接受连接失败");
                }
                continue;
            }

            // 接收超时：半连接（连上不发命令 / 发一半就断）最多占住线程 5 秒，
            // 不会让客户端线程无限期挂着
            struct timeval tv;
            tv.tv_sec = 5;
            tv.tv_usec = 0;
            setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

            // 每连接一个线程并 detach：等待运动结束时不能让别的客户端连不进来——
            // 尤其 duration=0 的持续运动，只有另一个客户端发 stop 才能结束它。
            active_clients_.fetch_add(1);
            std::thread([this, client_fd]() {
                handleClient(client_fd);
                close(client_fd);
                active_clients_.fetch_sub(1);
            }).detach();
        }
    }

    void handleClient(int client_fd)
    {
        MotionCommand cmd;
        MotionResponse resp;
        resp.success = false;
        resp.message[0] = '\0';

        // 接收命令
        ssize_t n = recv(client_fd, &cmd, sizeof(cmd), 0);
        if (n != sizeof(cmd)) {
            sendResponse(client_fd, false, "接收命令失败");
            return;
        }
        // 命令名来自对端，可能是没带 '\0' 的畸形帧；后续 strcmp/打印都按 C 串用，
        // 这里兜底截断，避免越界读。
        cmd.command[kMotionCommandSize - 1] = '\0';

        MotionTicketPtr ticket;          // 非空 = 这条命令已受理，要等运动结束才回包
        std::string queued_note;         // 排队过的命令在最终回包里补一句说明

        {
            std::lock_guard<std::mutex> lock(mutex_);

            RCLCPP_INFO(this->get_logger(), "📥 收到命令: %s, 速度: %.2f, 时长: %.1f秒, 打断: %s",
                        cmd.command, cmd.speed, cmd.duration, cmd.interrupt ? "是" : "否");

            const bool is_stop = (strcmp(cmd.command, "stop") == 0);

            if (!is_stop && !cmd.interrupt && is_moving_) {
                // 队列模式：当前有运动在跑，本条入队
                if (pending_.has_value()) {
                    // 队列已满，拒绝新命令（立即回包，不进等待）
                    resp.success = false;
                    snprintf(resp.message, sizeof(resp.message),
                            "队列已满，请等待当前动作完成或使用打断模式");
                    RCLCPP_WARN(this->get_logger(), "⚠️  %s", resp.message);
                } else {
                    ticket = std::make_shared<MotionTicket>();
                    pending_ = QueuedCommand{cmd, ticket};
                    queued_note = "（本条曾排队）";
                    resp.success = true;
                    snprintf(resp.message, sizeof(resp.message), "命令已加入队列");
                    RCLCPP_INFO(this->get_logger(), "📋 %s，将在当前动作完成后执行", resp.message);
                }
            } else {
                // 打断模式 / 当前空闲 / stop：立即执行
                ticket = is_stop ? nullptr : std::make_shared<MotionTicket>();
                executeCommand(cmd, ticket, resp);
            }
        }

        // 未知命令 / 队列已满 / stop 命令：立即回包
        if (!resp.success || !ticket) {
            sendResponse(client_fd, resp.success, resp.message);
            return;
        }

        // 已受理的运动命令：阻塞到这次运动真正结束
        RCLCPP_INFO(this->get_logger(), "⏳ 等待运动结束（%s）...", resp.message);
        const std::string reason = waitForMotionEnd(ticket, client_fd);
        if (reason.empty()) {
            // 调用方已断开，没人收响应了，线程到此为止
            RCLCPP_INFO(this->get_logger(), "🔌 调用方已断开，放弃等待");
            return;
        }

        const std::string message = reason + queued_note;
        RCLCPP_INFO(this->get_logger(), "✅ 回包: %s", message.c_str());
        sendResponse(client_fd, true, message.c_str());
    }

    /**
     * 等待这次运动结束。返回结束原因；返回空串表示调用方已断开（无需回包）。
     *
     * 不持服务端状态锁等待：只等 ticket 自己的 cv，等待期间其它客户端照常连入、
     * 照常发 stop / 新命令。每 200ms 醒一次，顺带检查服务是否在关闭、调用方是否
     * 已经断开（断开的客户端没必要陪着运动跑完）。
     */
    std::string waitForMotionEnd(const MotionTicketPtr & ticket, int client_fd)
    {
        while (true) {
            std::unique_lock<std::mutex> lock(ticket->mutex);
            if (ticket->cv.wait_for(lock, 200ms, [&ticket] { return ticket->done; })) {
                return ticket->reason;
            }
            lock.unlock();

            if (!running_) {
                return "服务关闭";
            }
            if (peerClosed(client_fd)) {
                return "";
            }
        }
    }

    /** 调用方是否已关闭连接（非阻塞探测，不消费数据） */
    static bool peerClosed(int client_fd)
    {
        char buf;
        const ssize_t n = recv(client_fd, &buf, 1, MSG_PEEK | MSG_DONTWAIT);
        if (n == 0) {
            return true;
        }
        if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            return true;
        }
        return false;
    }

    void sendResponse(int client_fd, bool success, const char * message)
    {
        MotionResponse resp;
        resp.success = success;
        copyCString(resp.message, message);
        if (send(client_fd, &resp, sizeof(resp), MSG_NOSIGNAL) != sizeof(resp)) {
            RCLCPP_WARN(this->get_logger(), "⚠️  发送响应失败（调用方可能已断开）");
        }
    }

    /** 执行命令。必须持 mutex_ 调用。ticket 非空时应由本函数负责让它最终被 finish。 */
    void executeCommand(const MotionCommand & cmd, const MotionTicketPtr & ticket,
                        MotionResponse & resp)
    {
        // 解析命令
        if (!parseCommand(cmd.command, cmd.speed)) {
            resp.success = false;
            snprintf(resp.message, sizeof(resp.message), "未知命令: %s", cmd.command);
            RCLCPP_ERROR(this->get_logger(), "❌ %s", resp.message);
            finishTicket(ticket, "命令无效，未执行");   // 别让等待方永远等下去
            return;
        }

        // 更新状态
        copyCString(current_command_, cmd.command);
        current_speed_ = cmd.speed;
        duration_ = cmd.duration;
        elapsed_time_ = 0.0;
        publish_count_ = 0;

        if (strcmp(cmd.command, "stop") == 0) {
            // stop 本身就是停止信号：结束当前运动（唤醒它的等待方），并清空队列
            is_moving_ = false;
            target_count_ = 0;
            settleMotion("收到 stop 命令");
            if (pending_.has_value()) {
                RCLCPP_INFO(this->get_logger(), "📋 队列被 stop 清空");
                finishTicket(pending_->ticket, "队列被 stop 命令清空，未执行");
                pending_.reset();
            }
            resp.success = true;
            snprintf(resp.message, sizeof(resp.message), "已停止");
            return;
        }

        // 运动命令：先结掉正在跑的那条（它的等待方据「被新命令打断」返回），
        // 再把本次的 ticket 设为当前运动的凭据
        if (is_moving_) {
            settleMotion("被新命令打断");
        }

        is_moving_ = true;
        active_ticket_ = ticket;
        if (duration_ > 0) {
            target_count_ = static_cast<int>(duration_ * 10);
            resp.success = true;
            snprintf(resp.message, sizeof(resp.message),
                    "开始运动，持续 %.1f 秒", duration_);
        } else {
            target_count_ = 0;
            resp.success = true;
            snprintf(resp.message, sizeof(resp.message),
                    "开始运动，持续到下一个命令");
        }
    }

    bool parseCommand(const char* command, double speed)
    {
        twist_msg_.linear.x = 0.0;
        twist_msg_.linear.y = 0.0;
        twist_msg_.linear.z = 0.0;
        twist_msg_.angular.x = 0.0;
        twist_msg_.angular.y = 0.0;
        twist_msg_.angular.z = 0.0;

        if (strcmp(command, "forward") == 0) {
            twist_msg_.linear.x = speed;
        } else if (strcmp(command, "backward") == 0) {
            twist_msg_.linear.x = -speed;
        } else if (strcmp(command, "left") == 0) {
            twist_msg_.linear.y = speed;
        } else if (strcmp(command, "right") == 0) {
            twist_msg_.linear.y = -speed;
        } else if (strcmp(command, "turnleft") == 0) {
            twist_msg_.angular.z = speed;
        } else if (strcmp(command, "turnright") == 0) {
            twist_msg_.angular.z = -speed;
        } else if (strcmp(command, "stop") == 0) {
            // 全零速度
        } else {
            return false;
        }

        return true;
    }

    void controlCallback()
    {
        MotionTicketPtr queued_ticket;
        MotionCommand queued_cmd{};

        {
            std::lock_guard<std::mutex> lock(mutex_);

            if (!is_moving_) {
                return;
            }

            // 发布速度指令
            twist_publisher_->publish(twist_msg_);
            publish_count_++;
            elapsed_time_ = publish_count_ * 0.1;

            // 检查是否达到目标时间
            if (target_count_ > 0 && publish_count_ >= target_count_) {
                RCLCPP_INFO(this->get_logger(), "⏱️ 达到目标时间，自动停止");
                stopMovement();     // 内部会 finishTicket(active_ticket_, "时长到达…")

                // 取出待执行的命令，出锁后再执行（executeCommand 会取锁）
                if (pending_.has_value()) {
                    queued_cmd = pending_->cmd;
                    queued_ticket = pending_->ticket;
                    pending_.reset();
                }
            }
        }

        if (queued_ticket) {
            MotionResponse resp;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                RCLCPP_INFO(this->get_logger(), "📋 执行队列中的命令");
                executeCommand(queued_cmd, queued_ticket, resp);
                RCLCPP_INFO(this->get_logger(), "✅ 队列命令已执行: %s", resp.message);
            }
        }
    }

    /** 结束当前运动并唤醒它的等待方。必须持 mutex_ 调用。 */
    void settleMotion(const std::string & reason)
    {
        is_moving_ = false;
        if (active_ticket_) {
            finishTicket(active_ticket_, reason);
            active_ticket_.reset();
        }
    }

    /** 时长到达后自动停止。必须持 mutex_ 调用。 */
    void stopMovement()
    {
        // 发送停止指令
        geometry_msgs::msg::Twist stop_msg;
        twist_publisher_->publish(stop_msg);

        copyCString(current_command_, "stop");
        current_speed_ = 0.0;
        settleMotion("运动完成（时长到达，已自动停止）");
    }

    // 成员变量
    rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr twist_publisher_;
    rclcpp::TimerBase::SharedPtr control_timer_;

    geometry_msgs::msg::Twist twist_msg_;
    std::mutex mutex_;

    // Socket
    int server_fd_;
    std::thread socket_thread_;
    std::atomic<bool> running_;
    std::atomic<int> active_clients_;

    // 状态变量
    bool is_moving_;
    int publish_count_;
    int target_count_;
    double elapsed_time_;

    char current_command_[32];
    double current_speed_;
    double duration_;

    // 当前运动的完成凭据（等待方据此返回）
    MotionTicketPtr active_ticket_;

    // 命令队列（仅支持一个待执行命令）
    std::optional<QueuedCommand> pending_;
};

// 全局节点指针
std::shared_ptr<TwistControllerSocketServer> g_node = nullptr;

void signalHandler(int signum)
{
    if (g_node) {
        RCLCPP_WARN(g_node->get_logger(), "⚠️  收到中断信号，正在停止...");
    }
    rclcpp::shutdown();
    exit(signum);
}

int main(int argc, char** argv)
{
    rclcpp::init(argc, argv);

    signal(SIGINT, signalHandler);
    signal(SIGTERM, signalHandler);

    g_node = std::make_shared<TwistControllerSocketServer>();

    RCLCPP_INFO(g_node->get_logger(), "🚀 Socket 服务运行中，等待命令...");

    rclcpp::spin(g_node);

    rclcpp::shutdown();
    return 0;
}
