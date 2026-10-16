---
name: robot-dog-camera
description: 机器狗相机拍照工具，基于当前 scripts 下的 capture.sh / capture.py。适用于用户说“拍照”“拍张照片”“看看前面”“连拍”“定时拍照”“加水印”“缩放图片”时。支持单张拍照、连拍、定时拍照、时间戳水印、图片缩放和静默输出。
---

# 机器狗相机拍照

机器狗相机拍照工具，当前以 `scripts/capture.sh` 和 `scripts/capture.py` 为准。

## 何时使用此技能

**✅ 适用场景：**
- 用户说“拍照”“拍一张”“拍张照片”
- 用户说“连拍 5 张”“每隔 1 秒拍一张”
- 用户说“定时拍照”“持续拍 60 秒”
- 用户说“拍完加时间水印”
- 用户说“拍完缩放到 640x480”
- 需要脚本静默执行，只关心成功或失败

**❌ 不适用场景：**
- 障碍物检测、避障判断
- 视频流分析
- 导航定位

## 核心能力

1. **单张拍照** - 直接从共享内存抓取 JPEG
2. **连拍模式** - 按指定次数和间隔连续拍照
3. **定时拍照** - 在指定持续时间内周期拍照
4. **图像后处理** - 支持缩放和时间戳水印
5. **静默模式** - 适合脚本或自动化调用

## 快速开始

```bash
cd scripts/

# 单张拍照
./capture.sh -o photo.jpg

# 自动生成文件名拍照
./capture.sh

# 连拍 5 张，间隔 0.5 秒
./capture.sh --burst 5 --interval 0.5 -d /tmp/photos

# 定时拍照 60 秒，每 5 秒一张
./capture.sh --timelapse 60 --interval 5 -d /tmp/timelapse

# 拍照并添加时间戳水印
./capture.sh -o photo.jpg --watermark

# 拍照并缩放到 640x480
./capture.sh -o photo.jpg --resize 640 480
```

## 命令说明

### 拍照脚本

- `capture.sh`：Shell 入口，透传参数给 `capture.py`
- `capture.py`：实际拍照与图像处理实现

### 命令语法

```bash
./capture.sh [options]
```

### 基本参数

- `-o, --output FILE` - 输出文件路径，单拍模式使用
- `-d, --dir DIR` - 输出目录，连拍/定时模式使用（`capture.py` 默认 `/tmp`；`capture_direct.py` 无此参数）
- `-s, --socket PATH` - 共享内存路径（`capture.py` 默认 `/tmp/neck_jpeg`，见下方「拍照实现」；`capture_direct.py` 无此参数）
- `-t, --timeout SEC` - 拍照超时时间，默认 `2`（`capture_direct.py` 默认 `5`）
- `-q, --quiet` - 静默模式

### 拍照模式

- `--burst N` - 连拍 N 张
- `--timelapse SEC` - 持续拍照 SEC 秒
- `--interval SEC` - 连拍或定时拍照间隔，默认 `1.0`
- `--prefix NAME` - 文件名前缀，默认 `photo`

### 图像处理

- `--resize WIDTH HEIGHT` - 调整图片尺寸
- `--watermark` - 添加时间戳水印

## 执行规则

1. **默认单拍**
   - 未指定 `--burst` 或 `--timelapse` 时，执行单张拍照

2. **模式互斥**
   - `--burst` 与 `--timelapse` 不能同时使用

3. **有后处理时先拍临时文件**
   - 如果指定 `--resize` 或 `--watermark`
   - 先拍到临时文件，再做后处理，再输出到目标文件

4. **未指定输出文件时自动命名**
   - 默认输出：`{输出目录}/robot_photo_{timestamp}.jpg`（输出目录即 `--dir`，未给时用 `capture.py` 的 `/tmp`）

5. **批量模式自动创建目录**
   - 连拍和定时拍照会自动创建输出目录

## 使用示例

```bash
cd scripts/

# 单张拍照
./capture.sh
./capture.sh -o /tmp/photo.jpg
./capture.sh -o /tmp/photo.jpg -q

# 连拍模式
./capture.sh --burst 5 --interval 0.5 -d /tmp/photos
./capture.sh --burst 10 --interval 1 --prefix dog -d /tmp/photos

# 定时拍照
./capture.sh --timelapse 60 --interval 5 -d /tmp/timelapse
./capture.sh --timelapse 300 --interval 10 --prefix time -d /tmp/timelapse

# 图像处理
./capture.sh -o /tmp/photo.jpg --watermark
./capture.sh -o /tmp/photo.jpg --resize 640 480
./capture.sh -o /tmp/photo.jpg --watermark --resize 800 600

# 自定义共享内存路径
./capture.sh -s /tmp/neck_jpeg -o /tmp/photo.jpg

# 延长超时
./capture.sh -o /tmp/photo.jpg -t 5
```

## 模型执行指令模板

当用户提出相机相关请求时，按以下规则处理：

1. **先识别目标模式**
   - “拍一张” → 单拍
   - “连拍 N 张” → `--burst N`
   - “持续拍 X 秒” → `--timelapse X`

2. **再补充附加参数**
   - 有间隔要求 → `--interval`
   - 有输出位置要求 → `-o` 或 `-d`
   - 有文件名前缀要求 → `--prefix`
   - 有静默要求 → `-q`

3. **图片处理按需附加**
   - “加时间水印” → `--watermark`
   - “缩放到 WxH” → `--resize W H`

4. **输出命令应可直接执行**
   - 优先给出 `./capture.sh ...`
   - 参数顺序清晰完整
   - 不要虚构不存在的脚本或参数

5. **不要把此技能用于障碍物检测**
   - 当前 scripts 下只有拍照工具，没有障碍物检测脚本

## 技术细节

### 当前脚本
- `skills/robot-dog-camera/scripts/capture.sh:1`
- `skills/robot-dog-camera/scripts/capture.py:15`

### 拍照实现
- 共享内存：`/tmp/neck_jpeg`
- 格式：JPEG
- 分辨率：`1920x1080`
- 帧率：`30fps`
- GStreamer pipeline：`shmsrc -> image/jpeg -> multifilesink`

### 功能特性
- ✅ 单张拍照
- ✅ 连拍
- ✅ 定时拍照
- ✅ 自动时间戳命名
- ✅ 图片缩放
- ✅ 时间戳水印
- ✅ 静默模式
- ✅ Shell / Python 双入口

### 实现说明
- 连拍和定时拍照由 Python 循环控制
- 单拍成功判断依据：输出文件存在且大小大于 0
- 图像处理依赖 Pillow
- Shell 脚本仅做参数透传，不包含业务逻辑

## 输出规则

**默认输出路径：**
- 单拍：`/tmp/robot_photo_{timestamp}.jpg`
- 连拍/定时：`{dir}/{prefix}_{timestamp}.jpg`

**文件名时间格式：**
- `YYYYMMDD_HHMMSS_mmm`
- 示例：`photo_20260324_143025_123.jpg`

## 运行要求

- 机器狗已开机
- 相机共享内存可用
- 已安装 `gst-launch-1.0`
- Python 3 可用
- 可选：Pillow（用于缩放和水印）

## 安装

```bash
# 安装 Pillow（可选）
pip3 install Pillow

# 查看帮助
cd scripts/
./capture.sh --help
```

## 故障排查

**共享内存不存在：**
```bash
ls -l /tmp/neck_jpeg
```

**拍照超时：**
```bash
./capture.sh -o /tmp/photo.jpg -t 5
```

**图像处理不可用：**
```bash
pip3 install Pillow
```

**Shell 入口无法调用：**
- 检查 `capture.sh` 是否能找到同目录下的 `capture.py`

## 相关文件

- `scripts/capture.sh`
- `scripts/capture.py`
