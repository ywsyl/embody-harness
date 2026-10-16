# Vision Skill

## 1. 技能概述
Vision Skill 是机器人具备环境视觉理解能力的基础技能，用于根据任务需求获取视觉信息，并通过视觉模型完成场景理解、目标识别、环境分析和图片问答。

该技能统一封装机器人视觉能力入口，需要根据不同视觉任务选择对应处理模式，详细描述见【2. 模式能力范围说明】。
- 【单张图片分析】：当需要分析特定图片时使用
- 【前方视觉感知】：当需要观察当前、前方视角环境时使用
- 【原地环绕搜索】：当需要观察当前、360°视角环境时使用

### 图像落盘（三种模式默认都不留文件）
本技能取到的图**只在内存里流转**（帧 → base64 → 视觉服务），默认一个图像文件都不产生。
需要留档时加 `--save`（工具层不传，即不留档），落盘由 `skills/camera/frame_producer.py`
独占完成：目录 `runtime_files/photos/`、命名 `<时间戳>[_<用途后缀>].jpg` 都在它那儿——
**本技能不拼路径、不建目录**，要单独存一张图也调它。旧图的清理不归它管（落盘只写不清）：
监督器每轮按体积清一次（`configs.RUNTIME_PHOTOS_TRIGGER_MB` / `_TARGET_MB`）。

### 帧从哪来（不用管，但别误判）
front / surround 两模式取的是 `skills/camera/frame_producer.py` 的共享帧环，那个帧环在
**取帧服务进程**（由统一启动入口拉起的 `frame_producer.py --serve`）的内存里；本技能无论跑在
哪个进程，都由 frame_producer 自己经 `runtime_files/frame_producer.sock` 找服务取帧。
所以「本进程没有帧环」不是失败原因 —— 取不到帧时脚本会直接报错并说明是「取帧服务不可达」
还是「有人在抓拍但帧环里还没有帧」（相机共享内存没人推流）。



# 2. 模式能力范围说明


## 2.1 单张图片分析 `"mode"="image"`

### 能力描述
对指定图片文件进行视觉理解。

### 脚本调用方式
```python
python skills/vision/scripts/vision_dispatch.py --mode image --image {IMAGE_PATH} --prompt {PROMPT}
```
- 参数`{PROMPT}`为对视觉大模型的提示词，从视觉任务描述或用户指令中提取总结得到。
- 参数`{IMAGE_PATH}`为需要进行分析的图片的本地路径。
- 脚本输出为大模型生成的对`{PROMPT}`提问的自然回复

### 适用场景
- 判断机器人前方是否存在目标
- 识别人、物体、障碍物
- 分析当前场景


---



## 2.2 前方视角感知 `"mode"="front"`

### 能力描述
取机器人共享图像帧生产者（`skills/camera/frame_producer.py`）帧环中的**最新一帧**作为当前视野图像，
不触发额外拍照（帧由后台周期抓拍持续产生），并通过视觉模型分析当前环境。

### 适用场景
- 判断机器人前方是否存在目标
- 识别前方的人、物体、障碍物等
- 分析前方场景
- 前方视角足够覆盖的视觉任务

### 脚本调用方式
```python
python skills/vision/scripts/vision_dispatch.py --mode front --prompt {PROMPT}
```
- 参数`{PROMPT}`为对视觉大模型的提示词，从视觉任务描述或用户指令中提取总结得到。
- 脚本输出为大模型生成的对`{PROMPT}`提问的自然回复
- 这一帧默认不落盘（见【图像落盘】）；需要留下「当时看到什么」的证据时加 `--save`



---



## 2.3 原地环绕搜索 `"mode"="surround"`

### 能力描述
机器狗原地旋转一周，每转 90° 从共享帧生产者帧环取一帧（不调用拍照脚本），
将四个视角在**内存里**拼成四宫格（`frame_producer.compose_grid`，不再调 ImageMagick `montage`）
后交由视觉模型分析，完成对当前位置周边环境的环视扫描、图像采集、图像分析。
默认不落盘：四张视角图与拼图都不写文件，需要留档时加 `--save`。

### 适用场景
* 涉及“周围”“身边”“搜索”等指向“环绕视角”关键词的任务
* 前方视角不足够覆盖、需要在当前位置下进行360°搜索的视觉任务
* 上层技能需要当前位置四视角搜索结果
* 【前方视觉感知】模式下无法得到期望结果，需要进一步增加搜索范围的任务

### 脚本调用方式
```bash
python skills/vision/scripts/vision_dispatch.py --mode surround --prompt {PROMPT}
```
- 参数`{PROMPT}`为对搜索目标的详细外观描述，从视觉任务描述中推测或用户指令中提取总结得到。
- 该脚本控制机器狗旋转一周并拍摄四个视角的照片、将其拼成一张四宫格的图片发送给视觉大模型进行分析理解。该脚本输出以下格式信息：
```json  
    {{
        "found": true # or false,    ←是否找到疑似目标
        "targets": [
            {{
            "region": "xxx", ←目标出现的区域/视角
            "x_hirizon": xxx,  ←0-1之间的小数，表示目标在整张图像内部的中心点水平位置
            "visible_features": xxx, ←候选目标的可见特征
            "confidence": xxx  ←该目标为用户指定搜索对象的置信度
            }}
        ],
        "return_message": "(面向用户的简洁中文说明)"
        }}
```
其中`"targets"`中可能含有多个疑似目标，默认使用`"confidence"`最高的目标的参数。

### 下游功能：计算该目标的具体方位，可根据该方位转向目标
当需要得知该目标的具体方位，或是需要转向该目标以进行下一步交互时，调用以下脚本：
```bash
bash skills/vision/scripts/local-search/scripts/turn2subject.sh {region} {x_hirizon} {if_turn} 
```
- 脚本的输入参数
  - `{region}`：必填，来自上一步中vision_dispatch.py的输出
  - `{x_hirizon}`：必填，来自上一步中vision_dispatch.py的输出
  - `{if_turn}`：必填，布尔值，表示是否转向该目标

- 脚本的输出参数
  - `direction` 为 `turnleft` ，则目标在机器狗左侧； `direction` 为 `turnright` ，则目标在机器狗右侧
  - `angle` 为目标在机器狗左侧或右侧的角度
  - 这些方向是以机器狗当时朝向为原点，单位为度数。

- 脚本的适用情况
  - 只需显式得知该目标所在的具体方位，不需要转向该目标：填入{region}和{x_hirizon} ，{if_turn}=false
  - 需要转向该目标，并进行其它交互：填入{region}和{x_hirizon} ，{if_turn}=true
  

---

