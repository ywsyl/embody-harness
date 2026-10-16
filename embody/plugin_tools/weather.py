"""天气查询工具 —— 问 open-meteo 取当前天气与逐日预报，译成中文摘要。

工具的唯一入口是 `query_weather(arguments)`（ToolExecutor 的内置执行器 `weather` 直接转发，
见 plugin_tools/tool_executor.py）。它把原先由模型自己走的三步收进来：

    task_policy.md 原文（改写前）：robot_state 读 location 段拿经纬度 → robot_state 读
    system.date 拿日期 → 自己拼一个长 URL 交给 web_fetch。

这三步每一环都可能出错且错了没人兜：URL 由模型手拼（经纬度/日期/字段名任一写错就是一个 400
或一段没用的 HTML）、`location` 段的值可能是 unknown 或很旧、`weather_code` 是 WMO 数字码，
模型每次都要自己解释成中文。收进工具后模型只说「查哪里、查哪天」。

参数（全部可选，省略即「机器狗所在地 + 当天」，由本模块补，模型不填）：
    location                    地名，走 geocoding 接口换坐标
    latitude / longitude        直接给坐标（必须成对），地名解析不出来时的退路
    start_date / end_date       YYYY-MM-DD；只给 start 即查一天

失败一律抛 RuntimeError：桥把它转成 {"error": ...}，模型据此换一条路（换个地名、改用坐标、
或去问用户）。**不返回「错误提示」形状的字符串** —— 那会让模型以为查到了。

网络走 `CURL_BIN` + `-4`（configs.paths / configs.configs），理由见 `_curl_json`。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

# 与 tool_executor/bridge 一致：把仓库根（embody/）放进搜索路径，
# 这样直接执行本文件与 `-m plugin_tools.weather` 都能 import configs.*。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.configs import (
    WEATHER_FORECAST_URL,
    WEATHER_GEOCODING_URL,
    WEATHER_REQUEST_TIMEOUT,
)
from configs.paths import CURL_BIN, RUNTIME_ROBOT_STATUS


# 预报接口要的字段集。**沿用 task_policy.md §2 原文那一份**（改写前模型自己拼的就是这些），
# 免得工具与提示词各说一套：current 五个性状 + daily 四项（天气码、最高/最低温、最大降水概率）。
_CURRENT_FIELDS = (
    "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
)
_DAILY_FIELDS = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
)

# curl 参数：`-s` 静音进度条（应答要走 stdout 解析）、`-4` 强制 IPv4。与
# Observer/status_observer.py 的 _CURL_ARGS 同款、同理由（本机没有 IPv6 默认路由）：
# 2026-09-28 在本机实测，python requests 直连 geocoding-api.open-meteo.com 三次里只成功一次
# （另两次 8s/11s 后 ConnectionError Errno 101 / ReadTimeout），同一地址 curl -4 六次全成功、各约 1s。
# **不带 --max-time / -f**：超时统一用 subprocess 的 timeout（只有一个数，见 configs 那条常量）；
# 不带 -f 是为了让 HTTP 400 的应答体照样拿得到 —— open-meteo 出错时正文是
# {"error": true, "reason": "..."}，透出 reason 比一句「接口失败」有用。
_CURL_ARGS = ("-s", "-4")

# WMO 4677 天气码 → 中文。码表是**外部规范**（open-meteo 的 weather_code 字段就是它），
# 只此一处消费者，故放本模块而不进 configs（那里的表要么多消费方、要么是部署可调项）。
# 未登记的码不猜：_wmo_text 回「未知天气码(N)」。
_WMO_TEXT = {
    0: "晴",
    1: "基本晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "毛毛雨",
    53: "中等毛毛雨",
    55: "浓毛毛雨",
    56: "冻毛毛雨",
    57: "浓冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中阵雨",
    82: "大阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    99: "雷阵雨伴大冰雹",
}

# 日期后缀：自己算而不是 strftime('%A')，那个给的是英文星期名（且在 C locale 下固定）
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _wmo_text(code: Any) -> str:
    """WMO 天气码 → 中文；没登记过的码原样报出来，不拿最像的那个凑。"""
    if isinstance(code, bool) or not isinstance(code, int):
        return f"未知天气码({code!r})"
    return _WMO_TEXT.get(code, f"未知天气码({code})")


def _text(value: Any) -> Optional[str]:
    """快照里的文本字段 → 非空字符串；`unknown` / null / 非字符串一律算取不到（None）。

    `unknown` 是快照自己的哨兵值（意思是「有实测通路、但此刻读不到」），不是地名，别当值用。
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and value != "unknown" else None


def _coord(value: Any) -> Optional[float]:
    """快照里的坐标 → float；取不到（`unknown` / null / 非数）→ None。

    坐标在快照里是**裸数字**（Observer/status_observer.py 的 _coord_text 保留 6 位小数），
    YAML 解析出来就是 float，所以这里不需要解析字符串。bool 是 int 的子类，得先挡掉。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _read_robot_location() -> Dict[str, Any]:
    """读 robot_status.yaml 的 location 段（机器人所在地）。

    这是全仓**第一处解析** robot_status.yaml 的代码（现有的读口 robot_state 都是原样透传给
    模型看）。照 Communicator/startup_cleanup.py 读 motion_status.yaml 的写法办：**读不到一律
    降级、不抛异常** —— 文件可能是观测器还没起、也可能写到一半，那些都是该由调用方处理的状态，
    不是本函数的异常。`yaml` 延迟到函数内 import：缺 PyYAML 只该让这个工具报「位置取不到」。

    返回的字典**总是有**，坐标取不到时 latitude / longitude 是 None；updated_at 仍可能有时刻
    —— 调用方要用它写进错误消息（「位置数据最后更新于 X」），所以不在这里吞掉。
    """
    empty = {"latitude": None, "longitude": None, "region": None, "city": None, "updated_at": None}
    try:
        import yaml

        data = yaml.safe_load(RUNTIME_ROBOT_STATUS.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — 文件缺失/坏掉/缺 PyYAML 都只降级成「取不到」
        return empty
    location = data.get("location") if isinstance(data, dict) else None
    if not isinstance(location, dict):
        return empty
    return {
        "latitude": _coord(location.get("latitude")),
        "longitude": _coord(location.get("longitude")),
        "region": _text(location.get("region")),
        "city": _text(location.get("city")),
        "updated_at": _text(location.get("updated_at")),
    }


def _curl_json(url: str) -> Dict[str, Any]:
    """curl 取一份 JSON 应答；任何失败都抛 RuntimeError（不猜、不返回半条）。

    失败判据取得宽：curl 起不来 / 被超时掐断 / 非零退出码 / 空应答 / 不是 JSON / 不是对象，
    都算请求失败。**这些与「接口说这次查不了」（error: true 的 reason）分开**：那种由调用方
    解析正文后连 reason 一起抛出，模型看到的是 open-meteo 自己的话。
    """
    try:
        proc = subprocess.run(
            [str(CURL_BIN), *_CURL_ARGS, url],
            capture_output=True, text=True, timeout=WEATHER_REQUEST_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # 命令不在 / 超时被杀 / 起不来
        raise RuntimeError(
            f"天气接口请求失败：curl 起不来或被超时掐断（超时 {WEATHER_REQUEST_TIMEOUT:g}s）：{exc}"
        ) from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"天气接口请求失败：curl 退出码 {proc.returncode}，{proc.stderr.strip() or '无 stderr'}"
        )
    body = proc.stdout.strip()
    if not body:
        raise RuntimeError("天气接口请求失败：空应答")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"天气接口应答不是 JSON：{body[:200]}") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"天气接口应答不是 JSON 对象：{body[:200]}")
    return data


def _geocode(place: str) -> Dict[str, Any]:
    """地名 → 坐标（open-meteo 的 geocoding 接口，与预报同一个厂家）。

    Returns:
        {"latitude", "longitude", "name", "admin1", "country"} —— name 是接口给的标准名
        （用户说「杭州西湖」时这里可能是「西湖」），回给模型时原样报出来，好让它核对解析对了没。
    """
    url = f"{WEATHER_GEOCODING_URL}?{urlencode({'name': place, 'count': 1, 'language': 'zh', 'format': 'json'})}"
    data = _curl_json(url)
    if data.get("error"):
        raise RuntimeError(f"地名解析失败：{data.get('reason') or data}")
    results = data.get("results")
    if not isinstance(results, list) or not results:
        raise RuntimeError(
            f"查不到地名 {place!r} 对应的坐标（open-meteo 地名库没有它）。"
            f"请换一个更常见的写法，或改用 latitude / longitude 直接给坐标"
        )
    first = results[0]
    if not isinstance(first, dict):
        raise RuntimeError(f"地名解析返回了意料之外的应答：{str(first)[:200]}")
    latitude = _coord(first.get("latitude"))
    longitude = _coord(first.get("longitude"))
    if latitude is None or longitude is None:
        raise RuntimeError(f"地名 {place!r} 解析出来没有坐标：{str(first)[:200]}")
    return {
        "latitude": latitude,
        "longitude": longitude,
        "name": _text(first.get("name")) or place,
        "admin1": _text(first.get("admin1")),
        "country": _text(first.get("country")),
    }


def _fetch_forecast(
    latitude: float, longitude: float, start_date: str, end_date: str
) -> Dict[str, Any]:
    """取当前天气 + 逐日预报（一项 open-meteo 请求同时给两段）。

    `timezone=auto`：按坐标所在地的时区返回，`daily.time` 就是当地日期 —— 这样「今天」与用户
    说的「今天」是同一个概念，不用本机时区去猜。超范围（预报只覆盖未来 16 天左右）时接口
    回 error+reason，这里原样透出，不在本地另设一个上限去猜。
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "current": _CURRENT_FIELDS,
        "daily": _DAILY_FIELDS,
        "timezone": "auto",
    }
    data = _curl_json(f"{WEATHER_FORECAST_URL}?{urlencode(params)}")
    if data.get("error"):
        raise RuntimeError(
            f"天气接口拒绝了这次查询（{start_date} ~ {end_date}）：{data.get('reason') or data}"
        )
    return data


def _num(value: Any, fmt: str = "{:g}", none: str = "—") -> str:
    """数值 → 文本；温度这类浮点默认按 %g 去掉多余的小数零（27.3 仍是 27.3、21.0 变 21）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return none
    return fmt.format(value)


def _coord_text(value: float) -> str:
    """坐标 → 文本，保留 6 位小数再去掉尾随零。

    **不能用 %g**：它只给 6 位有效数字，120.749677 会被截成 120.75（约 2km 的误差），
    30.752156 截成 30.7522 —— 快照那边专门为此不走 _scalar（见 Observer/status_observer.py
    的 _coord_text，同样的坑、同样的 6 位小数口径）。这里只是给模型看的标签，但报错地方比
    记对地方更糟：模型会照着这个坐标说给用户听。
    """
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _moment_text(value: Any) -> str:
    """ISO 时刻 → 可读文本（只把日期与时刻之间的 T 换成空格）。

    open-meteo 的 `current.time` 是 `2026-09-28T14:30`，中间那个 T 念出来是噪声；
    其余原样保留，不做时区换算（应答里的时刻已经是地点当地时刻）。
    """
    if not isinstance(value, str) or not value:
        return "—"
    return value.replace("T", " ", 1)


def _weekday_cn(iso_date: str) -> str:
    """YYYY-MM-DD → 周几；解析不了就回空串（少个后缀，不影响其余内容）。"""
    try:
        return _WEEKDAY_CN[datetime.strptime(iso_date, "%Y-%m-%d").weekday()]
    except ValueError:
        return ""


def _unit(units: Dict[str, Any], key: str, default: str = "") -> str:
    """取应答里的单位（如 °C / % / km/h）。

    **一律从应答取、不写死**：2026-09-28 实测 `wind_speed_10m` 默认给的是 km/h 而不是 m/s，
    写死就会把风速说错三倍多。
    """
    value = units.get(key) if isinstance(units, dict) else None
    return value if isinstance(value, str) and value else default


def _parse_date(value: Any, field: str) -> Optional[str]:
    """参数里的日期 → 规范化 YYYY-MM-DD；没给（None / 空串）返回 None，非法则抛错。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{field} 必须是 YYYY-MM-DD 格式的日期字符串，收到 {value!r}")
    text = value.strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise RuntimeError(f"{field} 不是合法日期（要 YYYY-MM-DD）：{value!r}") from None


def _resolve_place(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """定下这次查哪里：显式坐标 > 地名 > 机器狗所在地。

    三者都给时按上面这个优先级取（坐标最具体），但**给了 latitude 就必须给 longitude** ——
    只给一个多半是模型漏填，静默配上一个别处的值会查出一个看着正常、其实不是它要的结果。

    用机器狗所在地时把 `location` 段的时间戳一并带出来：那个值冻在最后一次反查成功的时刻，
    可能很旧，读方（模型/用户）要先知道它有多旧才能判断能不能采信。取不到就抛错，让模型去问
    用户要查哪个城市 —— 编一个位置或拿别处的数据顶上，用户问的是本地天气却拿到别处的，更糟。
    """
    latitude = arguments.get("latitude")
    longitude = arguments.get("longitude")
    place = arguments.get("location")

    if latitude is not None or longitude is not None:
        if latitude is None or longitude is None:
            raise RuntimeError(
                "latitude 与 longitude 必须成对给（只给了一个）："
                f"收到 latitude={latitude!r}、longitude={longitude!r}。"
                "也可以改用 location 传地名"
            )
        parsed_lat = _coord(latitude)
        parsed_lon = _coord(longitude)
        if parsed_lat is None or parsed_lon is None:
            raise RuntimeError(f"经纬度必须是数字：收到 latitude={latitude!r}、longitude={longitude!r}")
        return {
            "latitude": parsed_lat,
            "longitude": parsed_lon,
            "label": f"指定坐标（{_coord_text(parsed_lat)}, {_coord_text(parsed_lon)}）",
            "note": None,
        }

    if isinstance(place, str) and place.strip():
        geo = _geocode(place.strip())
        where = "，".join(part for part in (geo["name"], geo["admin1"], geo["country"]) if part)
        return {
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "label": f"{where}（{_coord_text(geo['latitude'])}, {_coord_text(geo['longitude'])}）",
            "note": None,
        }

    robot = _read_robot_location()
    if robot["latitude"] is None or robot["longitude"] is None:
        when = f"，该段最后更新于 {robot['updated_at']}" if robot["updated_at"] else ""
        raise RuntimeError(
            f"取不到机器人所在位置的经纬度（{RUNTIME_ROBOT_STATUS.name} 的 location 段没有可用坐标"
            f"{when}）——机器人的位置靠外网反查公网 IP 得到，反查没成功过或还没跑过就是这个状态。"
            "请向用户确认要查哪个城市，再用 location 参数重查一次"
        )
    where = "，".join(part for part in (robot["city"], robot["region"]) if part) or "经纬度见下"
    return {
        "latitude": robot["latitude"],
        "longitude": robot["longitude"],
        "label": f"机器人所在地：{where}（{_coord_text(robot['latitude'])}, {_coord_text(robot['longitude'])}）",
        "note": f"位置数据时刻：{robot['updated_at']}" if robot["updated_at"] else None,
    }


def _format(place: Dict[str, Any], data: Dict[str, Any]) -> str:
    """拼最终给模型的文本：中文摘要 + 末尾附关键原始数字。"""
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    daily = data.get("daily") if isinstance(data.get("daily"), dict) else {}
    current_units = data.get("current_units") if isinstance(data.get("current_units"), dict) else {}
    daily_units = data.get("daily_units") if isinstance(data.get("daily_units"), dict) else {}

    temp_unit = _unit(current_units, "temperature_2m", "°C")
    humidity_unit = _unit(current_units, "relative_humidity_2m", "%")
    wind_unit = _unit(current_units, "wind_speed_10m", "")
    rain_unit = _unit(daily_units, "precipitation_probability_max", "%")

    lines = [f"【天气查询】{place['label']}"]
    if place["note"]:
        lines.append(f"【位置时效】{place['note']}")

    offset = data.get("utc_offset_seconds")
    tz = data.get("timezone") if isinstance(data.get("timezone"), str) else "?"
    offset_text = f"｜GMT{offset // 3600:+d}" if isinstance(offset, int) and not isinstance(offset, bool) else ""
    lines.append(
        f"【数据来源】open-meteo｜时区 {tz}{offset_text}"
        f"｜数据时刻 {_moment_text(current.get('time'))}"
    )
    lines.append("")

    lines.append(
        "【现在】"
        f"{_wmo_text(current.get('weather_code'))}，"
        f"{_num(current.get('temperature_2m'))}{temp_unit}"
        f"（体感 {_num(current.get('apparent_temperature'))}{temp_unit}），"
        f"湿度 {_num(current.get('relative_humidity_2m'))}{humidity_unit}，"
        f"风速 {_num(current.get('wind_speed_10m'))}{(' ' + wind_unit) if wind_unit else ''}"
    )
    lines.append("")

    dates = daily.get("time") if isinstance(daily.get("time"), list) else []
    codes = daily.get("weather_code") if isinstance(daily.get("weather_code"), list) else []
    highs = daily.get("temperature_2m_max") if isinstance(daily.get("temperature_2m_max"), list) else []
    lows = daily.get("temperature_2m_min") if isinstance(daily.get("temperature_2m_min"), list) else []
    rains = (
        daily.get("precipitation_probability_max")
        if isinstance(daily.get("precipitation_probability_max"), list)
        else []
    )

    def at(seq: List[Any], index: int) -> Any:
        return seq[index] if index < len(seq) else None

    if not dates:
        lines.append("【逐日预报】接口这次没有返回逐日数据")
    for index, day in enumerate(dates):
        weekday = _weekday_cn(day) if isinstance(day, str) else ""
        rain = _num(at(rains, index))
        lines.append(
            f"【{day}{(' ' + weekday) if weekday else ''}】"
            f"{_wmo_text(at(codes, index))}，"
            f"{_num(at(lows, index))}~{_num(at(highs, index))}{temp_unit}，"
            f"最大降水概率 {rain}{rain_unit if rain != '—' else ''}"
        )

    lines.append("")
    lines.append(
        "【原始数据】"
        + json.dumps(
            {
                "current_units": current_units,
                "current": current,
                "daily_units": daily_units,
                "daily": daily,
            },
            ensure_ascii=False,
        )
    )
    return "\n".join(lines)


def query_weather(arguments: Dict[str, Any]) -> str:
    """天气查询工具入口。

    Args:
        arguments: 工具参数（可空字典）：location / latitude / longitude /
            start_date / end_date，全部可选，见模块抬头。

    Returns:
        中文摘要 + 关键原始数字（多行文本），可直接交给模型播报。

    Raises:
        RuntimeError: 位置取不到、地名解析不出来、参数非法、接口不通或拒绝查询。
            一律抛出去而不是返回错误文本 —— 桥转成 {"error": ...}，模型才知道这次没查成。
    """
    start = _parse_date(arguments.get("start_date"), "start_date")
    end = _parse_date(arguments.get("end_date"), "end_date")
    if start is None and end is None:
        # 缺省是**本机当天**，不去读 robot_status 的 system.date：本工具就跑在这台机器上，
        # 那个字段本身就是本机时钟现读的（见 configs.SNAPSHOT_DATE_FMT 的注释），直读少一个
        # 「观测器没在跑」的失败面。open-meteo 那边时区按地点取，daily.time 会是当地日期。
        start = end = date.today().strftime("%Y-%m-%d")
    elif start is None:
        start = end
    elif end is None:
        end = start
    if end < start:
        raise RuntimeError(f"结束日期早于开始日期：start_date={start}、end_date={end}")

    place = _resolve_place(arguments)
    data = _fetch_forecast(place["latitude"], place["longitude"], start, end)
    return _format(place, data)


if __name__ == "__main__":
    # 自检入口（与 nav_tools.py 末尾同款）：不带参数就是「机器狗所在地 + 当天」，
    # 要试别的分支就传一段 JSON，如 `python3 -m plugin_tools.weather '{"location":"北京"}'`。
    print(query_weather(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
