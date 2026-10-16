# test_vqa 接口文档

## 概述

`POST /test_vqa` 是 visual_memory 的测试/调试接口，支持直接传入图片进行视觉问答，不依赖设备视频流。

## 接口地址

```
POST http://36.137.127.46:8070/test_vqa
Content-Type: application/json
```

## 请求参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `image_source` | str | **是** | `"local"` / `"oss"` / `"external"` |
| `image_path` | str | local 必填 | 本地图片文件路径 |
| `oss_key` | str | oss 必填 | OSS 对象 key |
| `image_base64` | str | external 必填 | base64 编码的图片字符串 |
| `prompt` | str | 否 | 对图片的提问（与 `query` 等价） |
| `query` | str | 否 | 对图片的提问（与 `prompt` 等价） |
| `messages` | list | 否 | OpenAI 格式多轮对话，`[{role, content}]` |
| `stream` | bool | 否 | 是否流式输出，默认 `false` |
| `session_id` | str | 否 | 会话 ID，不传自动生成 |

> 本仓库的调用方（`vision_local.request_vqa`、`Observer/vision_observer.py`）**不传时默认填本机设备 SN**（`configs/paths.py` 的 `DEVICE_SN`，来自设备配置文件，非硬编码）；SN 读不到时该字段整个不传，走上面的「自动生成」。

> **注意：** `prompt` 和 `messages` 至少传一个。

## 调用示例

### 1. 纯 base64 图片 + 自定义 prompt（非流式）

```bash
curl -X POST http://36.137.127.46:8070/test_vqa \
  -H 'Content-Type: application/json' \
  -d '{
    "image_source": "external",
    "image_base64": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA...",
    "prompt": "描述这张图片的内容"
  }'
```

返回：

```
这是一张室内场景图片，画面中有一张木制桌子，桌子上放着一台笔记本电脑和一杯咖啡...
```

### 2. 带 data URI 前缀的 base64（推荐，前端可直接传）

```bash
curl -X POST http://36.137.127.46:8070/test_vqa \
  -H 'Content-Type: application/json' \
  -d '{
    "image_source": "external",
    "image_base64": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA...",
    "prompt": "图中有什么物体？"
  }'
```

> 接口会自动去除 `data:image/xxx;base64,` 前缀，无需手动处理。

### 3. 流式输出

```bash
curl -X POST http://36.137.127.46:8070/test_vqa \
  -H 'Content-Type: application/json' \
  -d '{
    "image_source": "external",
    "image_base64": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA...",
    "prompt": "详细描述这个场景",
    "stream": true
  }'
```

返回 SSE 流式数据：

```
data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"这是"}}]}

data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"一个"}}]}

data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"办公室"}}]}

...

data: [DONE]
```

### 4. 使用 messages 格式（自定义 system prompt）

```bash
curl -X POST http://36.137.127.46:8070/test_vqa \
  -H 'Content-Type: application/json' \
  -d '{
    "image_source": "external",
    "image_base64": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA...",
    "messages": [
      {"role": "system", "content": "你是一个专业的安防巡检机器人，请重点关注画面中的安全隐患"},
      {"role": "user", "content": "分析当前画面"}
    ]
  }'
```

### 5. 使用 `query` 参数（与 `prompt` 等价）

```bash
curl -X POST http://36.137.127.46:8070/test_vqa \
  -H 'Content-Type: application/json' \
  -d '{
    "image_source": "external",
    "image_base64": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA...",
    "query": "这个人在做什么？"
  }'
```

## 前端 / Python 调用示例

### Python

```python
import requests
import base64

# 读取本地图片并编码为 base64
with open("/path/to/image.jpg", "rb") as f:
    image_base64 = base64.b64encode(f.read()).decode("utf-8")

response = requests.post(
    "http://36.137.127.46:8070/test_vqa",
    json={
        "image_source": "external",
        "image_base64": image_base64,
        "prompt": "描述这张图片"
    }
)

print(response.text)
```

### JavaScript (前端)

```javascript
// file 为 <input type="file"> 获取的 File 对象
const reader = new FileReader();
reader.onload = async (e) => {
  const imageBase64 = e.target.result; // data:image/jpeg;base64,xxx

  const response = await fetch("http://36.137.127.46:8070/test_vqa", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      image_source: "external",
      image_base64: imageBase64,
      prompt: "描述这张图片",
      stream: false
    })
  });

  const text = await response.text();
  console.log(text);
};
reader.readAsDataURL(file);
```

### JavaScript 流式调用

```javascript
const reader = new FileReader();
reader.onload = async (e) => {
  const response = await fetch("http://36.137.127.46:8070/test_vqa", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      image_source: "external",
      image_base64: e.target.result,
      prompt: "描述这张图片",
      stream: true
    })
  });

  const reader_stream = response.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader_stream.read();
    if (done) break;

    const chunk = decoder.decode(value);
    const lines = chunk.split("\n");
    for (const line of lines) {
      if (line.startsWith("data: ") && line !== "data: [DONE]") {
        const data = JSON.parse(line.slice(6));
        const content = data.choices?.[0]?.delta?.content || "";
        process.stdout.write(content);  // 逐字输出
      }
    }
  }
};
reader.readAsDataURL(file);
```

## 三种 image_source 对比

| image_source | 图片参数 | 适用场景 |
|---|---|---|
| `external` | `image_base64` | 外部系统直接传入图片数据，支持前端/API 调用 |
| `local` | `image_path` | 服务器本地文件调试 |
| `oss` | `oss_key` | 从 OSS 对象存储读取图片 |
