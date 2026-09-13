#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""油羊毛（oil-coupon）面板服务

把 data/coupons.json 里的加油优惠数据用网页动态展示，
支持按品牌（中国石油 / 中国石化）、油号（92/95/98）、地区（全国/湖北/陕西）、状态筛选，
每行附「活动入口二维码」（服务端实时生成 SVG，无需外网）。

用法：
    python oil_server.py                # 自动选端口启动，不开浏览器
    python oil_server.py --open         # 启动并在浏览器打开面板
    python oil_server.py --port 8790    # 指定端口
    python oil_server.py --no-browser   # 只启动

接口：
    GET /               面板页面（web/index.html）
    GET /api/health     健康检查（用于单实例探测）
    GET /api/coupons    全部优惠数据（前端自行筛选）
    GET /api/reload     重新读取 coupons.json
    GET /api/qr?text=   生成二维码（SVG）
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.normpath(os.path.join(HERE, ".."))
WEB_DIR = os.path.join(SKILL_ROOT, "web")
DATA_FILE = os.path.join(SKILL_ROOT, "data", "coupons.json")

OWNER = "oil-coupon"
DEFAULT_PORT = int(os.environ.get("OIL_PORT", "8890"))
PORT_TRIES = 12

_LOCK = threading.Lock()
_CACHE = {"ts": 0.0, "payload": None}


# --------------------------------------------------------------------------
# 数据
# --------------------------------------------------------------------------
def load_data(force: bool = False) -> dict:
    with _LOCK:
        now = time.time()
        if (not force) and _CACHE["payload"] is not None and now - _CACHE["ts"] < 2.0:
            return _CACHE["payload"]
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "读取数据失败：%s" % e,
                "items": [], "updated_at": "", "brands": [], "regions": [], "grades": []}
    if not isinstance(data, dict):
        return {"ok": False, "error": "coupons.json 结构异常", "items": []}
    data["ok"] = True
    data["total"] = len(data.get("items") or [])
    data["data_file"] = DATA_FILE
    try:
        mt = os.path.getmtime(DATA_FILE)
        data["data_mtime"] = mt
        data["data_mtime_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mt))
    except OSError:
        data["data_mtime"] = 0
        data["data_mtime_iso"] = ""
    with _LOCK:
        _CACHE["ts"] = time.time()
        _CACHE["payload"] = data
    return data


# --------------------------------------------------------------------------
# 二维码（SVG，无第三方依赖时降级为错误提示）
# --------------------------------------------------------------------------
def qr_svg(text: str, border: int = 2) -> bytes:
    """生成 SVG 二维码；尺寸由前端 CSS 控制（img 宽高），无需位图依赖。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")
    import io

    import qrcode  # 延迟导入，缺库时报错更清晰
    import qrcode.image.svg as svg_mod

    qr = qrcode.QRCode(version=None,
                       error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=1, border=border,
                       image_factory=svg_mod.SvgPathImage)
    qr.add_data(text)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "OilCoupon/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默
        pass

    # -- helpers -----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _file(self, path: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = "text/html; charset=utf-8"
        if path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif path.endswith(".svg"):
            ctype = "image/svg+xml"
        self._send(200, body, ctype)

    # -- routes ------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._file(os.path.join(WEB_DIR, "index.html"))
        if path == "/api/health":
            return self._json({"ok": True, "owner": OWNER, "port": self.server.server_address[1]})
        if path == "/api/coupons":
            return self._json(load_data())
        if path == "/api/reload":
            return self._json(load_data(force=True))
        if path == "/api/qr":
            text = (qs.get("text") or [""])[0]
            try:
                svg = qr_svg(text)
            except Exception as e:  # noqa: BLE001
                return self._send(500, ("二维码生成失败：%s" % e).encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._send(200, svg, "image/svg+xml")
        return self._send(404, b"not found", "text/plain; charset=utf-8")


# --------------------------------------------------------------------------
# 端口 / 单实例
# --------------------------------------------------------------------------
def _probe(port: int, timeout: float = 0.6) -> str | None:
    """返回 'mine' / 'other' / None"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=timeout) as r:
            obj = json.loads(r.read().decode("utf-8"))
        return "mine" if obj.get("owner") == OWNER else "other"
    except Exception:
        return None


def _occupied(port: int) -> bool:
    """端口是否已被占用。

    注意：Windows 下 SO_REUSEADDR 允许重复绑定同一端口（会造成两个服务互相遮蔽），
    因此这里先用「连接探测」，再用「不带 SO_REUSEADDR 的 bind 探测」双重判断。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


class PanelServer(ThreadingHTTPServer):
    """禁用地址复用，避免与其它 skill 的面板抢占同一端口。"""
    allow_reuse_address = False
    daemon_threads = True


def run(host: str, port: int, open_browser: bool):
    # 已有本 skill 的面板在跑 → 直接复用
    for p in range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES):
        if _probe(p) == "mine":
            url = "http://127.0.0.1:%d/" % p
            print("[油羊毛] 面板已在运行：%s" % url)
            if open_browser:
                webbrowser.open(url)
            return 0

    httpd = None
    chosen = None
    for p in range(port, port + PORT_TRIES):
        if _occupied(p):
            continue
        try:
            httpd = PanelServer((host, p), Handler)
        except OSError:
            continue
        chosen = p
        break
    if httpd is None:
        print("[油羊毛] 未找到可用端口（%d-%d 均被占用）" % (port, port + PORT_TRIES))
        return 1

    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, chosen)
    print("[油羊毛] 面板已启动：%s" % url)
    print("[油羊毛] 数据文件：%s" % DATA_FILE)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[油羊毛] 已停止")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="油羊毛 · 加油优惠面板服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true", help="自检：打印数据统计与依赖状态")
    args = ap.parse_args(argv)

    if args.check:
        data = load_data(force=True)
        print("数据文件：%s" % DATA_FILE)
        print("更新时间：%s" % data.get("updated_at"))
        items = data.get("items") or []
        print("优惠条数：%d" % len(items))
        brand_names = {"petrochina": "中国石油", "sinopec": "中国石化",
                       "both": "两桶油均可", "channel": "第三方渠道"}
        for k, name in brand_names.items():
            n = sum(1 for it in items if it.get("brand") == k)
            print("  - %s：%d 条" % (name, n))
        for r in ("全国", "湖北", "陕西"):
            n = sum(1 for it in items if it.get("region") == r)
            print("  - %s：%d 条" % (r, n))
        try:
            qr_svg("https://www.cnpc.com.cn")
            print("二维码依赖：qrcode 可用")
        except Exception as e:  # noqa: BLE001
            print("二维码依赖：不可用（%s）→ 请执行 pip install qrcode pillow" % e)
        err = data.get("error")
        if err:
            print("数据错误：%s" % err)
        return 0

    return run(args.host, args.port, args.open and not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())

