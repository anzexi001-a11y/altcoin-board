# -*- coding: utf-8 -*-
"""serve.py — 把榜单变成一个外部随时可读的网址

为什么需要它：
    Grok 之类的 AI 抓 URL 时不会执行网页里的 JavaScript，
    所以页面内的自动刷新对它们无效——它们只拿到服务器那一刻返回的 HTML。
    要"实时同步"，实时性必须放在服务端：后台线程定时重算，
    请求进来直接返回最新那一份。

用法:
    python serve.py                    本机 8765 端口，每 180 秒重算一次
    python serve.py --every 60         最快 1 分钟一次（请求量仍在交易所限流内）
    python serve.py --top 150          扫少一点，重算更快

让外网能读到（二选一，任装一个即可）:
    cloudflared tunnel --url http://127.0.0.1:8765
    ngrok http 8765
    然后把给出的 https 地址发给 Grok。

访问路径:
    /            最新榜单（HTML）
    /markdown    最新榜单（纯文字，适合直接粘贴）
    /status      当前状态（JSON：上次成功重算时间、扫描数、报错）
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD = os.path.join(HERE, "altcoin_board.py")
OUT = os.path.join(HERE, "output")

STATE = {
    "html": "<html><body style='background:#0b0e11;color:#eaecef;font-family:sans-serif;"
            "padding:40px'>正在生成第一份榜单，约 1 分钟…</body></html>".encode("utf-8"),
    "md": "正在生成…".encode("utf-8"),
    "at": 0.0,
    "ok": False,
    "err": "",
    "scanned": 0,
}
LOCK = threading.Lock()


def newest(ext):
    fs = sorted(glob.glob(os.path.join(OUT, "board_*." + ext)))
    return fs[-1] if fs else None


def regen(top, minvol):
    """调一次 board 脚本，把它产出的文件读进内存。"""
    try:
        cmd = [sys.executable, BOARD, "--top", str(top), "--minvol", str(minvol)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if r.returncode != 0:
            with LOCK:
                STATE["err"] = ((r.stderr or "") + (r.stdout or ""))[-600:]
                STATE["ok"] = False
            print("  [失败] 生成出错，保留上一版", flush=True)
            return False
        h, m = newest("html"), newest("md")
        if not h:
            with LOCK:
                STATE["err"] = "没有找到输出文件"
                STATE["ok"] = False
            return False
        with LOCK:
            STATE["html"] = open(h, "rb").read()
            STATE["md"] = open(m, "rb").read() if m else b""
            STATE["at"] = time.time()
            STATE["ok"] = True
            STATE["err"] = ""
            STATE["scanned"] = 0
        print("  [%s] 已重算 -> %s" % (datetime.now().strftime("%H:%M:%S"), os.path.basename(h)),
              flush=True)
        return True
    except Exception as e:
        with LOCK:
            STATE["err"] = str(e)
            STATE["ok"] = False
        print("  [异常] %s" % e, flush=True)
        return False


def loop(top, minvol, every):
    while True:
        regen(top, minvol)
        time.sleep(max(30, every))


class Handler(BaseHTTPRequestHandler):
    server_version = "BoardServer/1.0"

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        with LOCK:
            html_body = STATE["html"]
            md_body = STATE["md"]
            at, ok, err = STATE["at"], STATE["ok"], STATE["err"]
        if path in ("/", "/board", "/index.html"):
            self._send(html_body, "text/html; charset=utf-8")
        elif path in ("/markdown", "/board.md", "/md", "/text"):
            self._send(md_body, "text/plain; charset=utf-8")
        elif path == "/status":
            age = int(time.time() - at) if at else None
            payload = json.dumps({"ok": ok, "last_success_age_sec": age,
                                  "error": err,
                                  "last_success": (datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S")
                                                   if at else None)},
                                 ensure_ascii=False, indent=2)
            self._send(payload.encode("utf-8"), "application/json; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404  /  or  /markdown  or  /status")

    def log_message(self, fmt, *args):
        sys.stderr.write("  [http] %s - %s\n" % (self.address_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser(description="把榜单变成可被外部读取的网址")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", type=str, default="127.0.0.1",
                    help="默认只监听本机；要让局域网访问用 0.0.0.0（有暴露风险）")
    ap.add_argument("--every", type=int, default=180, help="重算间隔秒数（最小 30）")
    ap.add_argument("--top", type=int, default=400, help="扫描合约数")
    ap.add_argument("--minvol", type=float, default=1e6)
    args = ap.parse_args()

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    os.makedirs(OUT, exist_ok=True)
    print("=" * 68)
    print("  榜单服务  监听 http://%s:%d  每 %d 秒重算" % (args.host, args.port, args.every))
    print("=" * 68)

    t = threading.Thread(target=loop, args=(args.top, args.minvol, args.every), daemon=True)
    t.start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("  /            最新榜单 HTML")
    print("  /markdown    纯文字版")
    print("  /status      状态")
    print("  外网访问：另开终端跑  cloudflared tunnel --url http://127.0.0.1:%d" % args.port)
    print("  停止：Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
