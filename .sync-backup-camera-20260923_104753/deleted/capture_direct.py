#!/usr/bin/env python3
'''
替代相机捕获脚本
直接从相机设备捕获（绕过共享内存）

说明：本机器人仅 /dev/video0（颈部相机）与 /dev/video2（主相机）是可用的
视频捕获设备，且正常运行时正被 ROS video_gst 相机节点独占；/dev/video1/3
是同相机的非捕获接口。video_gst 的共享内存 socket（/tmp/foo_fhd 等）是私有
加密协议，无法直接解出 JPEG。

因此拍照必须先短暂抢占相机设备（fuser -k 占用者）。

[2026-09-02 停用] 原实现会在捕获后按快照自动恢复被杀掉的 video_gst 节点（其未配
respawn、被杀即永久下线），但该恢复+等待持锁确认会拖慢每次视觉拍照 ~0.5-1s，
故暂时取消恢复逻辑（下方 _process_info/_snapshot_device_holders/_process_alive/
_restore_video_gst_nodes/_wait_video_gst_back 均已注释保留供恢复）。
'''

import os
import subprocess
import time
import argparse
import signal
import sys


def _held_by_video_gst(device):
    '''device 当前是否被 video_gst 相机节点占用。'''
    try:
        out = subprocess.run(
            ['fuser', '-v', device],
            capture_output=True, text=True,
        )
        # fuser -v 的进程表（含进程名）写到 stderr，stdout 只有裸 PID，须合并两路
        text = (out.stdout or '') + (out.stderr or '')
        return 'video_gst' in text
    except Exception:
        return False


def find_available_camera():
    '''找到可用的相机设备。

    优先选择被 video_gst 占用的设备（保证是真实捕获相机）；
    其次退化为第一个存在的设备（如颈部节点被杀后 video0 空闲，仍是真实相机）。
    '''
    for i in range(4):
        device = f'/dev/video{i}'
        if os.path.exists(device) and _held_by_video_gst(device):
            return device
    for i in range(4):
        device = f'/dev/video{i}'
        if os.path.exists(device):
            return device
    return None


# [2026-09-02 停用] 以下为「捕获后自动恢复 video_gst 节点」的实现，随 capture_direct()
# 内的恢复调用一并停用（视觉技能变慢）。恢复时取消本段注释。
#
# def _process_info(pid):
#     '''读取进程 cmdline 与完整环境；读取失败返回 None。'''
#     try:
#         with open(f'/proc/{pid}/cmdline', 'rb') as f:
#             cmd = [
#                 c.decode('utf-8', 'replace')
#                 for c in f.read().split(b'\x00') if c
#             ]
#         env = {}
#         with open(f'/proc/{pid}/environ', 'rb') as f:
#             for item in f.read().split(b'\x00'):
#                 if not item:
#                     continue
#                 key, _, val = item.partition(b'=')
#                 env[key.decode('utf-8', 'replace')] = val.decode('utf-8', 'replace')
#         if not cmd:
#             return None
#         return cmd, env
#     except Exception:
#         return None
#
#
# def _snapshot_device_holders(device):
#     '''记录占用 device 的进程（cmdline + 完整环境），用于捕获后恢复。'''
#     holders = {}
#     try:
#         out = subprocess.run(['fuser', device], capture_output=True, text=True)
#         for pid in (out.stdout or '').split():
#             pid = pid.strip()
#             if not pid.isdigit():
#                 continue
#             info = _process_info(int(pid))
#             if info:
#                 holders[int(pid)] = info
#     except Exception:
#         pass
#     return holders
#
#
# def _process_alive(pid):
#     '''进程是否仍存活（僵尸 Z / 已死 X 视为不存活）。'''
#     try:
#         with open(f'/proc/{pid}/stat', 'rb') as f:
#             data = f.read()
#         end = data.rfind(b')')
#         if end < 0:
#             return False
#         state = data[end + 2:end + 3]
#         return state not in (b'Z', b'X')
#     except Exception:
#         return False
#
#
# def _restore_video_gst_nodes(holders):
#     '''把 capture 期间被 fuser -k 杀掉的 video_gst 节点重新拉起。
#
#     相机设备被 video_gst 节点独占，直接捕获必须先 kill 它；捕获完成后
#     必须按原命令 + 原环境恢复，否则节点永久下线（video_gst 未配 respawn）。
#     返回成功发起恢复的节点数（发起 ≠ 节点已就绪，调用方需再等待持锁确认）。
#     '''
#     restored = 0
#     for pid, (cmd, env) in holders.items():
#         if not cmd or 'video_gst' not in (cmd[0] or ''):
#             continue  # 只恢复 video_gst 相机节点，不恢复残留 gst-launch 等
#         if _process_alive(pid):
#             continue  # 进程仍存活（可能已被别处恢复），避免重复拉起
#         try:
#             log = open('/tmp/camera_restore.log', 'a')
#             subprocess.Popen(
#                 cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
#                 start_new_session=True, cwd='/',
#             )
#             restored += 1
#             print(
#                 f'♻️ 已恢复 video_gst 节点 pid={pid}: {os.path.basename(cmd[0])}',
#                 file=sys.stderr,
#             )
#         except Exception as e:
#             print(f'⚠️ 恢复 video_gst 节点(pid={pid})失败: {e}', file=sys.stderr)
#     return restored
#
#
# def _wait_video_gst_back(device: str, max_wait: float = 5.0) -> bool:
#     '''轮询等待 device 重新被 video_gst 节点占用（restore 完成确认）。
#
#     节点重新 init 需 ~0.5-1s。若不等它重新持锁就返回，紧接的下一次抢拍
#     可能撞上节点 init 窗口：新节点还没开 fd（探测判"空闲"、快照为空），
#     同时 init 时 open 设备又被 gst-launch 抢占 → EBUSY 崩溃且无人再拉。
#     '''
#     deadline = time.monotonic() + max_wait
#     while time.monotonic() < deadline:
#         if _held_by_video_gst(device):
#             return True
#         time.sleep(0.1)
#     return False


def capture_direct(device, output_path, timeout=5):
    '''直接从相机设备捕获（会短暂抢占设备）。

    [2026-09-02 停用] 捕获后不再自动恢复被杀掉的 video_gst 节点——这是视觉技能
    变慢的主要来源（每次拍照后 _restore_video_gst_nodes + _wait_video_gst_back
    等节点重新 init/持锁 ~0.5-1s）。副作用：fuser -k 杀掉占设备的 video_gst
    相机节点后不会自动拉起（其未配 respawn），需人工/外部恢复。
    '''
    print(f"📷 直接从 {device} 捕获...", file=sys.stderr)

    # 记录当前占用设备的 video_gst 节点（随后 fuser -k 会被杀，捕获后需恢复）——[2026-09-02 停用]
    # holders = _snapshot_device_holders(device)

    try:
        # 首先尝试停止占用设备的进程
        try:
            subprocess.run(['fuser', '-k', device], capture_output=True)
            # time.sleep(0.1)
        except:
            pass

        # 使用GStreamer直接捕获
        cmd = [
            'gst-launch-1.0', '-q',
            'v4l2src', f'device={device}', '!',
            'image/jpeg,width=1920,height=1080,framerate=30/1', '!',
            'jpegparse', '!',
            'filesink', f'location={output_path}'
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # 等待文件生成或超时
            start_time = time.time()
            while time.time() - start_time < timeout:
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                    proc.terminate()
                    try:
                        _, err = proc.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        _, err = proc.communicate()
                    return True
                time.sleep(0.1)

            # 超时，终止进程（收集 gst-launch 的真实报错）
            proc.terminate()
            try:
                _, err = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, err = proc.communicate()
            print(f"❌ 相机捕获超时({timeout}s): {output_path}", file=sys.stderr)
            err_text = err.decode("utf-8", "replace").strip() if err else ""
            if err_text:
                print(err_text, file=sys.stderr)
            return False

        except Exception as e:
            print(f"❌ 捕获失败: {e}", file=sys.stderr)
            return False
    finally:
        # [2026-09-02 停用] 恢复被杀掉的 video_gst 节点（含等待重新持锁确认）——
        # 视觉技能变慢主因，暂时取消。恢复时取消下列注释：
        # if _restore_video_gst_nodes(holders) > 0:
        #     # 确认节点重新持锁再返回，避免下一次抢拍撞上节点 init 窗口
        #     if not _wait_video_gst_back(device):
        #         print(
        #             f'⚠️ 等待 video_gst 重新占用 {device} 超时(>5s)，'
        #             f'请检查节点状态：tail /tmp/camera_restore.log',
        #             file=sys.stderr,
        #         )
        pass


def main():
    parser = argparse.ArgumentParser(description='替代相机捕获工具')
    parser.add_argument('-o', '--output', required=True, help='输出文件路径')
    parser.add_argument('-d', '--device', help='指定相机设备')
    parser.add_argument('-s', '--socket', help='兼容性参数（已忽略）')
    parser.add_argument('-t', '--timeout', type=int, default=5, help='超时时间(秒)')
    parser.add_argument('-q', '--quiet', action='store_true', help='静默模式')
    args = parser.parse_args()

    # 找到可用的相机设备
    if args.device:
        device = args.device
    else:
        device = find_available_camera()
        if not device:
            print("❌ 没有找到可用的相机设备", file=sys.stderr)
            sys.exit(1)

    if not args.quiet:
        print(f"使用相机设备: {device}")

    # 捕获图像
    if capture_direct(device, args.output, args.timeout):
        size = os.path.getsize(args.output)
        if not args.quiet:
            print(f"✅ 捕获成功: {args.output} ({size} bytes)")

            # 验证文件
            result = subprocess.run(['file', args.output], capture_output=True, text=True)
            if 'JPEG' in result.stdout:
                print("✅ 文件格式正确")
            else:
                print("⚠️  文件格式可能不正确")
    else:
        if not args.quiet:
            print("❌ 捕获失败")
        sys.exit(1)

if __name__ == "__main__":
    main()
