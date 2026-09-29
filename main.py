# -*- coding: utf-8 -*-
"""图片清洗工具入口：端口探测、单实例、日志、自动开浏览器、启动 Flask。

日志走文件（logs/app.log, UTF-8），规避 Windows 控制台 GBK 乱码。
"""
import argparse
import json
import logging
import os
import socket
import sys
import threading
import urllib.request
import webbrowser

PORT_FILE = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "microvideo-labelwasher.port")
LOCK_FILE = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "microvideo-labelwasher.lock")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _instance_alive(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.6) as r:
            return r.status == 200
    except Exception:
        return False


def _setup_logging(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.FileHandler(os.path.join(log_dir, "app.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return root


def main():
    parser = argparse.ArgumentParser(description="图片清洗工具 microvideo-labelwasher")
    parser.add_argument("--port", type=int, default=0, help="监听端口（0=自动探测空闲端口）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--workspace-dir", default=None, help="工作区目录（默认 exe 同级 workspaces/）")
    args = parser.parse_args()

    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        ws_root = args.workspace_dir or os.path.join(base, "workspaces")
        log_dir = os.path.join(base, "logs")
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        ws_root = args.workspace_dir or os.path.join(here, "workspaces")
        log_dir = os.path.join(here, "logs")
    log = _setup_logging(log_dir)
    log.info("microvideo-labelwasher 启动 ws_root=%s", ws_root)

    # 单实例：已有实例存活则直接开浏览器复用
    existing_port = None
    if os.path.exists(PORT_FILE):
        try:
            with open(PORT_FILE, "r", encoding="utf-8") as f:
                existing_port = int(f.read().strip() or 0)
        except (ValueError, OSError):
            existing_port = None
        if existing_port and _instance_alive(existing_port):
            log.info("检测到已运行实例 %s，复用并打开浏览器", existing_port)
            webbrowser.open(f"http://127.0.0.1:{existing_port}/")
            return 0

    from server import create_app

    port = args.port or _free_port()
    app = create_app(ws_root=ws_root, port_file=PORT_FILE)

    with open(PORT_FILE, "w", encoding="utf-8") as f:
        f.write(str(port))

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
        log.info("浏览器将在 1.2 秒后打开 http://127.0.0.1:%s/", port)

    try:
        app.run(host=args.host, port=port, threaded=True, debug=False, use_reloader=False)
    finally:
        try:
            if os.path.exists(PORT_FILE):
                with open(PORT_FILE, "r", encoding="utf-8") as f:
                    cur = f.read().strip()
                if str(cur) == str(port):
                    os.unlink(PORT_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
