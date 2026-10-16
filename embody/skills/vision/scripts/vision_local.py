import argparse
import base64
import json
import sys
from pathlib import Path

import requests

# 仓库根（本文件 skills/vision/scripts/ 上三级），以便 import configs —— 视觉服务地址的
# 唯一来源是 configs.VISION_LOCAL_VQA_URL。本文件也能被单独当脚本调用（CLI 见文件尾），
# 故自带引导而不只依赖调用方。
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import VISION_LOCAL_VQA_URL as DEFAULT_URL  # noqa: E402
from configs.paths import DEVICE_SN  # noqa: E402


def encode_image(image_path):
    """
    本地图片转base64
    """
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        return base64.b64encode(image_bytes).decode("utf-8")

    except Exception as e:
        raise RuntimeError(
            f"读取图片失败: {image_path}\n{e}"
        )

def request_vqa(
    image_path=None,
    image_base64=None,
    prompt=None,
    url=DEFAULT_URL,
    stream=False,
    session_id=None
):
    """
    调用test_vqa接口，返回识别结果文本。

    image_path 与 image_base64 二选一（都缺则 ValueError）：

    - 调用方手上有现成图片文件（如 LLM 给的单图路径）→ 传 image_path；
    - 图还在内存里、不该落盘（帧环里取出的一帧、拼好的宫格图）→ 直接传
      image_base64，省掉「写临时文件 → 读回来编码 → 删文件」这一圈，
      也让视觉链路默认不留任何图像文件（留档与否由 frame_producer 的 save 决定）。

    session_id：服务端会话 ID。不传（None）＝用本机设备 SN（configs/paths.py 的
    DEVICE_SN，由设备配置文件读出、非硬编码）；SN 读不到时它是空串，此时整个字段
    不传，交服务端自己生成。

    返回：非流式＝服务端原始应答文本（与原来子进程版 stdout 里的内容一致，调用方
    自己解析）；流式＝拼接后的模型输出文本。两种模式都仍 print 一份供 CLI 与人看。
    """

    if image_base64 is None:
        if image_path is None:
            raise ValueError("request_vqa 需要 image_path 或 image_base64 之一")
        image_base64 = encode_image(image_path)

    payload = {
        "image_source": "external",
        "image_base64": image_base64,
        "stream": stream
    }

    if prompt:
        payload["prompt"] = prompt
    else:
        payload["prompt"] = "描述这张图片"

    if session_id is None:
        session_id = DEVICE_SN
    if session_id:
        payload["session_id"] = session_id


    headers = {
        "Content-Type": "application/json"
    }


    response = requests.post(
        url,
        headers=headers,
        json=payload,
        stream=stream,
        timeout=120
    )


    if response.status_code != 200:
        raise RuntimeError(
            f"请求失败: {response.status_code}\n{response.text}"
        )


    if not stream:
        print(response.text)
        return response.text


    # SSE流式解析
    print("模型输出:")

    chunks = []

    for line in response.iter_lines():

        if not line:
            continue

        line = line.decode("utf-8")

        if not line.startswith("data:"):
            continue

        data = line[5:].strip()

        if data == "[DONE]":
            break

        try:
            obj = json.loads(data)

            content = (
                obj.get("choices", [{}])[0]
                .get("delta", {})
                .get("content", "")
            )

            if content:
                chunks.append(content)
                print(content, end="", flush=True)

        except Exception:
            pass

    print()

    return "".join(chunks)


def main():

    parser = argparse.ArgumentParser(
        description="test_vqa 图片问答客户端"
    )

    parser.add_argument(
        "--image",
        required=True,
        help="本地图片路径"
    )

    parser.add_argument(
        "--prompt",
        default=None,
        help="图片问题"
    )

    parser.add_argument(
        "--stream",
        action="store_true",
        help="开启流式输出"
    )

    parser.add_argument(
        "--session-id",
        default=None,
        help="服务端会话 ID；不传＝用本机设备 SN（设备配置文件里的 SN）"
    )


    args = parser.parse_args()


    request_vqa(
        image_path=args.image,
        prompt=args.prompt,
        url=DEFAULT_URL,
        stream=args.stream,
        session_id=args.session_id
    )


if __name__ == "__main__":
    main()