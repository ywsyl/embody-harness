#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_API_BASE_URL = os.environ.get("NAV_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DEFAULT_DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8001"))

API_BASE_URL = DEFAULT_API_BASE_URL

ROUTES = {
    "/proxy/health": ("GET", "/health"),
    "/proxy/landmarks": ("GET", "/api/v1/landmarks"),
    "/proxy/localization/status": ("GET", "/api/v1/localization/status"),
    "/proxy/localization/relocalize": ("POST", "/api/v1/localization/relocalize"),
    "/proxy/navigation/go": ("POST", "/api/v1/navigation/go"),
    "/proxy/navigation/cancel": ("POST", "/api/v1/navigation/cancel"),
    "/proxy/navigation/status": ("GET", "/api/v1/navigation/status"),
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "NavigationDashboard/1.0"

    def do_GET(self) -> None:
        if self.path in ("/", "/dashboard"):
            self._send_html()
            return
        self._handle_proxy("GET")

    def do_POST(self) -> None:
        self._handle_proxy("POST")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _send_html(self) -> None:
        body = HTML_PAGE.replace("__API_BASE_URL__", API_BASE_URL).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_proxy(self, expected_method: str) -> None:
        route = ROUTES.get(self.path)
        if route is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown route."})
            return

        upstream_method, upstream_path = route
        if upstream_method != expected_method:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Method not allowed."})
            return

        payload = self._read_json_body() if expected_method == "POST" else None
        status, response = proxy_request(upstream_method, upstream_path, payload)
        self._send_json(status, response)

    def _read_json_body(self) -> Optional[dict[str, Any]]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}

        raw_body = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def proxy_request(method: str, path: str, payload: Optional[dict[str, Any]]) -> tuple[int, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(f"{API_BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, parse_response(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, parse_response(exc.read().decode("utf-8"))
    except URLError as exc:
        return 502, {"error": "API request failed.", "detail": str(exc), "api_base_url": API_BASE_URL}


def parse_response(raw_body: str) -> Any:
    if not raw_body:
        return None
    try:
        return json.loads(raw_body)
    except json.JSONDecodeError:
        return {"raw": raw_body}


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Navigation Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --line: #d8dee4;
      --text: #17202a;
      --muted: #64748b;
      --primary: #1769aa;
      --primary-dark: #0f4f82;
      --danger: #b42318;
      --ok: #166534;
      --warn: #92400e;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }

    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }

    .header-inner {
      max-width: 1180px;
      margin: 0 auto;
      padding: 18px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }

    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 650;
    }

    .api-url {
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      word-break: break-all;
    }

    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      grid-template-columns: minmax(300px, 420px) 1fr;
      gap: 20px;
    }

    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }

    .section-head {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }

    h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 650;
    }

    .body {
      padding: 16px;
    }

    .stack {
      display: grid;
      gap: 12px;
    }

    .row {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    button {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #ffffff;
      color: var(--text);
      padding: 0 12px;
      font-weight: 600;
      cursor: pointer;
    }

    button.primary {
      border-color: var(--primary);
      background: var(--primary);
      color: #ffffff;
    }

    button.primary:hover {
      background: var(--primary-dark);
    }

    button.danger {
      border-color: var(--danger);
      color: var(--danger);
    }

    button:disabled {
      opacity: 0.55;
      cursor: wait;
    }

    select, input {
      height: 36px;
      min-width: 180px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: #ffffff;
      color: var(--text);
      font: inherit;
    }

    .status-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfd;
      min-height: 72px;
    }

    .label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 5px;
    }

    .value {
      font-size: 17px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }

    .ok {
      color: var(--ok);
    }

    .warn {
      color: var(--warn);
    }

    .bad {
      color: var(--danger);
    }

    .landmarks {
      display: grid;
      gap: 8px;
      max-height: 240px;
      overflow: auto;
    }

    .landmark {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: #fbfcfd;
    }

    .landmark strong {
      display: block;
      margin-bottom: 3px;
    }

    .landmark span {
      color: var(--muted);
      font-size: 12px;
    }

    pre {
      margin: 0;
      min-height: 460px;
      max-height: calc(100vh - 190px);
      overflow: auto;
      background: #101820;
      color: #e6edf3;
      border-radius: 0 0 8px 8px;
      padding: 16px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    @media (max-width: 860px) {
      .header-inner {
        align-items: flex-start;
        flex-direction: column;
      }

      main {
        grid-template-columns: 1fr;
        padding: 16px;
      }

      .status-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <h1>Navigation Dashboard</h1>
      <div class="api-url">API: __API_BASE_URL__</div>
    </div>
  </header>

  <main>
    <div class="stack">
      <section>
        <div class="section-head">
          <h2>状态</h2>
          <button id="refreshAll">刷新全部</button>
        </div>
        <div class="body stack">
          <div class="status-grid">
            <div class="metric">
              <div class="label">服务</div>
              <div id="serviceStatus" class="value">-</div>
            </div>
            <div class="metric">
              <div class="label">ROS2</div>
              <div id="rosStatus" class="value">-</div>
            </div>
            <div class="metric">
              <div class="label">定位</div>
              <div id="localizationStatus" class="value">-</div>
            </div>
            <div class="metric">
              <div class="label">导航</div>
              <div id="navigationStatus" class="value">-</div>
            </div>
          </div>
          <div class="row">
            <button id="healthBtn">健康检查</button>
            <button id="locStatusBtn">查询定位</button>
            <button id="navStatusBtn">查询导航</button>
          </div>
        </div>
      </section>

      <section>
        <div class="section-head">
          <h2>地标</h2>
          <button id="loadLandmarks">加载地标</button>
        </div>
        <div class="body stack">
          <div class="row">
            <select id="destination"></select>
            <input id="manualDestination" placeholder="手动输入地点名称">
          </div>
          <div id="landmarks" class="landmarks"></div>
        </div>
      </section>

      <section>
        <div class="section-head">
          <h2>操作</h2>
        </div>
        <div class="body stack">
          <div class="row">
            <button id="relocalizeBtn">自动重定位</button>
            <button id="goBtn" class="primary">导航到目标</button>
            <button id="cancelBtn" class="danger">取消导航</button>
          </div>
        </div>
      </section>
    </div>

    <section>
      <div class="section-head">
        <h2>响应</h2>
        <button id="clearLog">清空</button>
      </div>
      <pre id="log"></pre>
    </section>
  </main>

  <script>
    const logEl = document.getElementById("log");
    const buttons = Array.from(document.querySelectorAll("button"));
    const destinationSelect = document.getElementById("destination");
    const manualDestination = document.getElementById("manualDestination");

    function setBusy(isBusy) {
      buttons.forEach((button) => {
        button.disabled = isBusy;
      });
    }

    function writeLog(title, data) {
      const timestamp = new Date().toLocaleString();
      logEl.textContent = `[${timestamp}] ${title}\n${JSON.stringify(data, null, 2)}\n\n${logEl.textContent}`;
    }

    function valueClass(ok) {
      return ok ? "value ok" : "value bad";
    }

    async function api(method, path, body) {
      setBusy(true);
      try {
        const options = { method, headers: {} };
        if (body !== undefined) {
          options.headers["Content-Type"] = "application/json";
          options.body = JSON.stringify(body);
        }
        const response = await fetch(path, options);
        const text = await response.text();
        let data;
        try {
          data = text ? JSON.parse(text) : null;
        } catch {
          data = { raw: text };
        }
        writeLog(`${method} ${path} -> HTTP ${response.status}`, data);
        return { status: response.status, data };
      } finally {
        setBusy(false);
      }
    }

    async function refreshHealth() {
      const result = await api("GET", "/proxy/health");
      const data = result.data && result.data.data ? result.data.data : {};
      document.getElementById("serviceStatus").textContent = data.service || "-";
      document.getElementById("serviceStatus").className = valueClass(data.service === "ok");
      document.getElementById("rosStatus").textContent = data.ros_available === true ? "available" : "unavailable";
      document.getElementById("rosStatus").className = valueClass(data.ros_available === true);
    }

    async function refreshLocalization() {
      const result = await api("GET", "/proxy/localization/status");
      const data = result.data && result.data.data ? result.data.data : {};
      const localized = data.localized === true;
      document.getElementById("localizationStatus").textContent = localized ? "localized" : "not localized";
      document.getElementById("localizationStatus").className = localized ? "value ok" : "value warn";
    }

    async function refreshNavigation() {
      const result = await api("GET", "/proxy/navigation/status");
      const data = result.data && result.data.data ? result.data.data : {};
      const status = data.status || "-";
      const destination = data.destination ? ` -> ${data.destination}` : "";
      document.getElementById("navigationStatus").textContent = `${status}${destination}`;
      document.getElementById("navigationStatus").className =
        status === "FAILED" ? "value bad" : status === "RUNNING" ? "value warn" : "value ok";
    }

    async function loadLandmarks() {
      const result = await api("GET", "/proxy/landmarks");
      const items = result.data && Array.isArray(result.data.data) ? result.data.data : [];
      destinationSelect.innerHTML = "";
      document.getElementById("landmarks").innerHTML = "";

      items.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.name;
        option.textContent = item.name;
        destinationSelect.appendChild(option);

        const row = document.createElement("div");
        row.className = "landmark";
        row.innerHTML = `<div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.description || "")}</span></div><button data-name="${escapeHtml(item.name)}">选择</button>`;
        row.querySelector("button").addEventListener("click", () => {
          destinationSelect.value = item.name;
          manualDestination.value = "";
        });
        document.getElementById("landmarks").appendChild(row);
      });
    }

    async function relocalize() {
      await api("POST", "/proxy/localization/relocalize", {});
    }

    async function goNavigation() {
      const destination = manualDestination.value.trim() || destinationSelect.value;
      if (!destination) {
        writeLog("导航到目标", { error: "未选择或输入目标地点" });
        return;
      }
      await api("POST", "/proxy/navigation/go", { destination });
    }

    async function cancelNavigation() {
      await api("POST", "/proxy/navigation/cancel", {});
    }

    async function refreshAll() {
      await refreshHealth();
      await loadLandmarks();
      await refreshLocalization();
      await refreshNavigation();
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    document.getElementById("refreshAll").addEventListener("click", refreshAll);
    document.getElementById("healthBtn").addEventListener("click", refreshHealth);
    document.getElementById("locStatusBtn").addEventListener("click", refreshLocalization);
    document.getElementById("navStatusBtn").addEventListener("click", refreshNavigation);
    document.getElementById("loadLandmarks").addEventListener("click", loadLandmarks);
    document.getElementById("relocalizeBtn").addEventListener("click", relocalize);
    document.getElementById("goBtn").addEventListener("click", goNavigation);
    document.getElementById("cancelBtn").addEventListener("click", cancelNavigation);
    document.getElementById("clearLog").addEventListener("click", () => {
      logEl.textContent = "";
    });

    refreshAll();
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Navigation HTTP API dashboard.")
    parser.add_argument("--host", default=DEFAULT_DASHBOARD_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    return parser.parse_args()


def main() -> None:
    global API_BASE_URL

    args = parse_args()
    API_BASE_URL = args.api_base_url.rstrip("/")
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard: http://{args.host}:{args.port}")
    print(f"API base URL: {API_BASE_URL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
