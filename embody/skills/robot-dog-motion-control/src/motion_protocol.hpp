#pragma once

#include <condition_variable>
#include <cstddef>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>

inline constexpr const char * kMotionSocketPath = "/tmp/robot_dog_motion.sock";
inline constexpr std::size_t kMotionCommandSize = 32;
inline constexpr std::size_t kMotionMessageSize = 128;

struct MotionCommand {
    char command[kMotionCommandSize];
    double speed;
    double duration;
    bool interrupt;
};

struct MotionResponse {
    bool success;
    char message[kMotionMessageSize];
};

/**
 * 一次运动的完成凭据。
 *
 * 服务端每受理一条运动命令就新建一张 ticket 交给发起它的客户端线程。运动真正结束时
 * （时长到达自动停止 / 收到 stop / 被新命令打断 / 排队被清空 / 服务关闭），由服务端
 * 写上结束原因并 notify；客户端线程据此才发送响应。
 *
 * 这就是「move 收到停止信号才返回」：客户端不再在命令下发后立刻拿到回包，而是阻塞到
 * 它所发起的那次运动结束。等待方等的是这张 ticket 自己的 cv，不持服务端状态锁，
 * 因此其它客户端仍能连进来发 stop / 新命令。
 *
 * 结构体只在同一进程的线程间传递（server 内部），不上 socket：
 * MotionCommand / MotionResponse 的线格式保持不变，避免新旧二进制互相误解。
 */
struct MotionTicket {
    std::mutex mutex;
    std::condition_variable cv;
    bool done = false;
    std::string reason;
};

using MotionTicketPtr = std::shared_ptr<MotionTicket>;

/** 结束一张 ticket 并唤醒等待方；重复调用只生效一次（首个原因胜出）。 */
inline void finishTicket(const MotionTicketPtr & ticket, const std::string & reason)
{
    if (!ticket) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(ticket->mutex);
        if (ticket->done) {
            return;
        }
        ticket->done = true;
        ticket->reason = reason;
    }
    ticket->cv.notify_all();
}

template <std::size_t N>
inline void copyCString(char (&target)[N], const char * source)
{
    if (source == nullptr) {
        target[0] = '\0';
        return;
    }

    std::strncpy(target, source, N - 1);
    target[N - 1] = '\0';
}
