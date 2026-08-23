from __future__ import annotations

# Deprecated: prefer gui.py (tkinter GUI / single-exe entry). This WebUI remains for reference.

import argparse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import urlparse

from phigros_publisher import PublishPipeline, TaskState
from phigros_publisher.uploader import UPLOAD_SCOPES


DEMO_ROOT = Path(__file__).resolve().parent
WEB_ROOT = DEMO_ROOT / "web"
WORK_ROOT = DEMO_ROOT / "work"
STATE = TaskState()
PIPELINE = PublishPipeline(DEMO_ROOT, WORK_ROOT, STATE)


def pick_local_apk() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as error:
        raise RuntimeError("当前 Python 未提供 tkinter，无法打开文件选择框") from error

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilename(
            title="选择 Phigros APK",
            filetypes=[("Android APK", "*.apk"), ("All files", "*.*")],
        )
    finally:
        root.destroy()
    if not selected:
        raise ValueError("未选择 APK 文件")
    return selected


class RequestHandler(SimpleHTTPRequestHandler):
    server_version = "rRankerPhigrosDemo/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        if self.path != "/api/status":
            super().log_message(format, *args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(HTTPStatus.OK, STATE.to_dict())
            return
        if path == "/api/defaults":
            self._json(
                HTTPStatus.OK,
                {
                    "endpoint": "https://cn-nb1.rains3.com",
                    "bucket": "rranker-phigros-data",
                    "publicBase": "https://rranker-phigros-data.cn-nb1.rains3.com",
                    "uploadScopes": UPLOAD_SCOPES,
                },
            )
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/pick-apk":
            try:
                apk_path = pick_local_apk()
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            except RuntimeError as error:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
                return
            self._json(HTTPStatus.OK, {"apkPath": apk_path})
            return

        if path != "/api/start":
            self._json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        if STATE.is_running():
            self._json(HTTPStatus.CONFLICT, {"error": "已有任务正在运行"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("请求体大小无效")
            options = json.loads(self.rfile.read(length))
            if not isinstance(options, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (ValueError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        STATE.reset()

        def worker() -> None:
            try:
                result = PIPELINE.run(options)
                STATE.complete(result)
            except BaseException as error:
                STATE.fail(error)

        Thread(target=worker, daemon=True, name="phigros-publish-pipeline").start()
        self._json(HTTPStatus.ACCEPTED, {"status": "started"})


def main() -> None:
    parser = argparse.ArgumentParser(description="rRanker Phigros 资源发布 Demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    print(f"Phigros 资源发布 Demo：http://{args.host}:{args.port}")
    print("仅监听本机；关闭窗口或按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
