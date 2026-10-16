#!/usr/bin/env python3
"""
机器狗相机拍照工具 - 使用 GStreamer 直接复制 JPEG
支持单拍、连拍、定时拍照、图像处理等功能
"""

import sys
import time
import os
import subprocess
import argparse
from datetime import datetime
from pathlib import Path

def capture_gstreamer(socket_path, output_path, timeout=2, quiet=False):
    """使用 GStreamer 快速捕获 JPEG"""
    if not quiet:
        print(f"📷 拍照中...")
        print(f"  共享内存: {socket_path}")
        print(f"  输出: {output_path}")

    if not os.path.exists(socket_path):
        if not quiet:
            print(f"❌ 共享内存不存在: {socket_path}")
        return False

    start_time = time.time()

    # GStreamer pipeline：直接复制 JPEG
    # caps 只卡分辨率、**不卡 framerate**：上游 video_gst 相机节点的 shmsink caps 是
    # `image/jpeg, width=1920, height=1080`（无帧率），且相机实际帧率若不是 30 就与
    # 写死的 30/1 协商不上——那是「连得上、却一帧也拉不出来」的典型死法。
    # 不写帧率＝不关心，任何帧率的上游都能协商成功。
    #
    # 落盘元素不能用 `multifilesink max-files=1`（2026-09-23 修）：它每来一帧都要
    # 「删掉最旧的、再写新的」，而这里的 location 是**不带 %d 的固定文件名** ——
    # 新旧同名，它删的就是自己刚写的那个文件。上游 30fps 时输出文件会反复「出现又消失」
    # （实测 15 次里 13 次能观察到 1~4 次抖动），下面那个「查出文件存在且 >0」的轮询
    # 一旦落在空窗里就误判失败 → exit=1，而此刻上游其实帧流正常。换 filesink 后
    # 30/30 次无抖动。
    # `num-buffers=1`：抓满一帧就 EOS，管道自己干净退出 —— 既保证文件里只有一个完整
    # JPEG（否则 filesink 会不停往后追加帧），也让下面能等它自然收尾而不是中途打断。
    cmd = [
        'gst-launch-1.0', '-q',
        'shmsrc', f'socket-path={socket_path}',
        'is-live=true', 'do-timestamp=true', 'num-buffers=1', '!',
        'image/jpeg,width=1920,height=1080', '!',
        'filesink', f'location={output_path}'
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # 等待文件生成或超时
        waited = 0
        while waited < timeout * 1000:
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                # num-buffers=1 → 抓满一帧就 EOS，先等它自然收尾再返回：此刻文件是
                # 完整的（terminate 会打断正在落盘的那一帧，留下半张图交给调用方）。
                # 万一上游卡住不 EOS，1 秒后兜底打断，行为与原来一致。
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                break
            time.sleep(0.05)
            waited += 50
        else:
            proc.kill()
            proc.wait()

        elapsed = int((time.time() - start_time) * 1000)

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            size_kb = os.path.getsize(output_path) / 1024
            if not quiet:
                print(f"✅ 拍照成功!")
                print(f"  文件: {output_path}")
                print(f"  大小: {size_kb:.1f} KB")
                print(f"  耗时: {elapsed}ms")
            return True
        else:
            if not quiet:
                print("❌ 拍照失败")
            return False

    except Exception as e:
        if not quiet:
            print(f"❌ 错误: {e}")
        return False

def burst_capture(socket_path, output_dir, count, interval, prefix="photo", quiet=False):
    """连拍模式"""
    if not quiet:
        print(f"📸 连拍模式: {count} 张，间隔 {interval}s")

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    success_count = 0

    for i in range(count):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"{prefix}_{timestamp}.jpg"
        output_path = os.path.join(output_dir, filename)

        if not quiet:
            print(f"\n[{i+1}/{count}]", end=" ")

        if capture_gstreamer(socket_path, output_path, timeout=2, quiet=quiet):
            success_count += 1

        if i < count - 1:
            time.sleep(interval)

    if not quiet:
        print(f"\n✅ 连拍完成: {success_count}/{count} 张成功")
    return success_count

def timelapse_capture(socket_path, output_dir, duration, interval, prefix="timelapse", quiet=False):
    """定时拍照模式"""
    if not quiet:
        print(f"⏱️  定时拍照: 持续 {duration}s，间隔 {interval}s")

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    start_time = time.time()
    count = 0
    success_count = 0

    while time.time() - start_time < duration:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"{prefix}_{timestamp}.jpg"
        output_path = os.path.join(output_dir, filename)

        count += 1
        if not quiet:
            elapsed = int(time.time() - start_time)
            print(f"\n[{elapsed}s/{duration}s] 第 {count} 张", end=" ")

        if capture_gstreamer(socket_path, output_path, timeout=2, quiet=quiet):
            success_count += 1

        time.sleep(interval)

    if not quiet:
        print(f"\n✅ 定时拍照完成: {success_count}/{count} 张成功")
    return success_count

def resize_image(input_path, output_path, width, height):
    """调整图像大小"""
    try:
        from PIL import Image
        img = Image.open(input_path)
        img_resized = img.resize((width, height), Image.Resampling.LANCZOS)
        img_resized.save(output_path, quality=95)
        return True
    except ImportError:
        print("⚠️  需要安装 Pillow: pip install Pillow")
        return False
    except Exception as e:
        print(f"❌ 调整大小失败: {e}")
        return False

def add_timestamp_watermark(input_path, output_path):
    """添加时间戳水印"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(input_path)
        draw = ImageDraw.Draw(img)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 使用默认字体
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        except:
            font = ImageFont.load_default()

        # 在右下角添加时间戳
        text_bbox = draw.textbbox((0, 0), timestamp, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]

        x = img.width - text_width - 20
        y = img.height - text_height - 20

        # 添加黑色背景
        draw.rectangle([x-10, y-10, x+text_width+10, y+text_height+10], fill=(0, 0, 0, 180))
        draw.text((x, y), timestamp, fill=(255, 255, 255), font=font)

        img.save(output_path, quality=95)
        return True
    except ImportError:
        print("⚠️  需要安装 Pillow: pip install Pillow")
        return False
    except Exception as e:
        print(f"❌ 添加水印失败: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(
        description="机器狗相机拍照工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单张拍照
  %(prog)s -o photo.jpg

  # 连拍 5 张，间隔 0.5 秒
  %(prog)s --burst 5 --interval 0.5 -d /tmp/photos

  # 定时拍照 60 秒，每 5 秒一张
  %(prog)s --timelapse 60 --interval 5 -d /tmp/timelapse

  # 拍照并添加时间戳水印
  %(prog)s -o photo.jpg --watermark

  # 拍照并调整大小
  %(prog)s -o photo.jpg --resize 640 480
        """
    )

    # 基本参数
    parser.add_argument('-o', '--output', help='输出文件路径 (单拍模式)')
    parser.add_argument('-d', '--dir', default='/tmp', help='输出目录 (连拍/定时模式，默认: /tmp)')
    parser.add_argument('-s', '--socket', default='/tmp/neck_jpeg', help='共享内存路径 (默认: /tmp/neck_jpeg)')
    parser.add_argument('-t', '--timeout', type=int, default=2, help='超时时间(秒，默认: 2)')
    parser.add_argument('-q', '--quiet', action='store_true', help='静默模式')

    # 拍照模式
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('--burst', type=int, metavar='N', help='连拍模式: 拍摄 N 张照片')
    mode_group.add_argument('--timelapse', type=int, metavar='SEC', help='定时拍照模式: 持续 SEC 秒')

    parser.add_argument('--interval', type=float, default=1.0, help='连拍/定时拍照间隔(秒，默认: 1.0)')
    parser.add_argument('--prefix', default='photo', help='文件名前缀 (默认: photo)')

    # 图像处理
    parser.add_argument('--resize', nargs=2, type=int, metavar=('WIDTH', 'HEIGHT'), help='调整图像大小')
    parser.add_argument('--watermark', action='store_true', help='添加时间戳水印')

    args = parser.parse_args()

    # 连拍模式
    if args.burst:
        success = burst_capture(
            args.socket, args.dir, args.burst,
            args.interval, args.prefix, args.quiet
        )
        sys.exit(0 if success > 0 else 1)

    # 定时拍照模式
    elif args.timelapse:
        success = timelapse_capture(
            args.socket, args.dir, args.timelapse,
            args.interval, args.prefix, args.quiet
        )
        sys.exit(0 if success > 0 else 1)

    # 单拍模式
    else:
        if not args.output:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.makedirs('/tmp/image', exist_ok=True)
            args.output = f"/tmp/image/{timestamp}.jpg"

        # 如果需要图像处理，先拍到临时文件
        if args.resize or args.watermark:
            temp_path = f"{args.output}.tmp.jpg"
            success = capture_gstreamer(args.socket, temp_path, args.timeout, args.quiet)

            if success:
                current_path = temp_path

                # 调整大小
                if args.resize:
                    resized_path = f"{args.output}.resized.jpg"
                    if resize_image(current_path, resized_path, args.resize[0], args.resize[1]):
                        if current_path != temp_path:
                            os.remove(current_path)
                        current_path = resized_path
                    else:
                        success = False

                # 添加水印
                if success and args.watermark:
                    if add_timestamp_watermark(current_path, args.output):
                        os.remove(current_path)
                    else:
                        # 水印失败，保留原图
                        os.rename(current_path, args.output)
                else:
                    os.rename(current_path, args.output)

                # 清理临时文件
                if os.path.exists(temp_path):
                    os.remove(temp_path)
        else:
            success = capture_gstreamer(args.socket, args.output, args.timeout, args.quiet)

        sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()

