#!/usr/bin/env python3
"""QQ 扫码登录实时看板 —— 极简本地 HTTP 服务（只监听 127.0.0.1，由 nginx 反代）。

页面地址形如 https://<域名>/qqqr-<TOKEN>/
所有接口都在这个前缀下，不知道 token 的人打不开，也拿不到二维码。

数据来源：
  - 登录状态：NapCat WebUI /api/QQLogin/CheckLoginStatus（isLogin / qrLoginAccepted / qrcodeurl）
  - 二维码： 优先按 CheckLoginStatus 返回的 qrcodeurl 实时生成 PNG（缓存按 URL 区分），
             生成失败时退回已有的 evidence/qq_login_qr.png

不写任何配置、不改 NapCat/AstrBot，只读接口。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE_FALLBACK = os.path.join(ROOT, "evidence", "qq_login_qr.png")
TOKEN_FILE = os.path.join(ROOT, ".qqqr_token")

BIND = os.environ.get("QRPORTAL_BIND", "127.0.0.1")
PORT = int(os.environ.get("QRPORTAL_PORT", "18123"))
NAPCAT_URL = os.environ.get("NAPCAT_URL", "http://127.0.0.1:6099")
NAPCAT_CONTAINER = os.environ.get("NAPCAT_CONTAINER", "napcat")
# 码寿命：实测约 2 分钟，留一点余量
CODE_TTL = int(os.environ.get("QRPORTAL_TTL", "120"))

sys.path.insert(0, "/tmp/pyqr")  # fetch_qr.sh 之前把 qrcode 装在这里

try:
    from PIL import Image  # noqa: F401  仅用于“图片有效”自检
    _HAS_PIL = True
except Exception:  # pragma: no cover
    _HAS_PIL = False


def load_token() -> str:
    tok = os.environ.get("QRPORTAL_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        tok = ""
    if not tok:
        raise SystemExit("缺少访问 token：设置 QRPORTAL_TOKEN 或写入 .qqqr_token")
    return tok


# ---------------- NapCat ----------------

_cred_cache: dict[str, object] = {"value": "", "exp": 0.0}


def napcat_token() -> str:
    tok = os.environ.get("NAPCAT_TOKEN", "").strip()
    if tok:
        return tok
    proc = subprocess.run(
        ["docker", "logs", NAPCAT_CONTAINER],
        capture_output=True, text=True, timeout=20,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    best = ""
    for line in out.splitlines():
        marker = "WebUi Token: "
        if marker in line:
            best = line.split(marker, 1)[1].strip().split()[0]
    return best


def credential() -> str:
    now = time.time()
    if _cred_cache["value"] and float(_cred_cache["exp"]) > now:  # type: ignore[arg-type]
        return str(_cred_cache["value"])
    tok = napcat_token()
    if not tok:
        raise RuntimeError("拿不到 NapCat WebUI token")
    h = hashlib.sha256((tok + ".napcat").encode()).hexdigest()
    body = json.dumps({"hash": h}).encode()
    req = urllib.request.Request(
        NAPCAT_URL.rstrip("/") + "/api/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode() or "{}")
    cred = ((data.get("data") or {}).get("Credential") or "")
    if not cred:
        raise RuntimeError(f"NapCat 鉴权失败：{data}")
    _cred_cache["value"] = cred
    _cred_cache["exp"] = now + 600
    return cred


def napcat_api(path: str) -> dict:
    req = urllib.request.Request(
        NAPCAT_URL.rstrip("/") + "/api/" + path, data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + credential(),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode() or "{}")


# 记录每张码第一次出现的时间，用来到期倒计时
_seen: dict[str, float] = {}
_last_err = ""


def status_snapshot() -> dict:
    global _last_err
    out: dict = {
        "ok": False,
        "phase": "unknown",
        "label": "读取状态中…",
        "is_login": False,
        "accepted": False,
        "uin": "",
        "qr_url": "",
        "age": 0,
        "ttl": CODE_TTL,
        "remain": 0,
        "error": "",
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        raw = napcat_api("QQLogin/CheckLoginStatus")
        d = raw.get("data") or {}
        out["ok"] = True
        out["is_login"] = bool(d.get("isLogin"))
        out["accepted"] = bool(d.get("qrLoginAccepted"))
        out["uin"] = str(d.get("uin") or "")
        url = str(d.get("qrcodeurl") or d.get("qrcode") or "")
        if url:
            first = _seen.setdefault(url, time.time())
            if len(_seen) > 20:  # 只留最近 20 张，避免无限增长
                for k in sorted(_seen, key=_seen.get)[: len(_seen) - 20]:  # type: ignore[arg-type]
                    _seen.pop(k, None)
            out["qr_url"] = url
            out["age"] = int(time.time() - first)
            out["remain"] = max(0, CODE_TTL - out["age"])

        if out["is_login"]:
            out["phase"] = "logged_in"
            out["label"] = "已登录成功，可以关掉这个页面了"
        elif out["accepted"]:
            out["phase"] = "pending_confirm"
            out["label"] = "已扫码，请在手机上点「确认登录」"
        elif not url:
            out["phase"] = "refreshing"
            out["label"] = "换码中…马上出新的"
        elif out["remain"] <= 0:
            out["phase"] = "refreshing"
            out["label"] = "这张码已到期，正在换新的…"
        else:
            out["phase"] = "waiting"
            out["label"] = "等待扫码"
        out["error"] = ""
        _last_err = ""
    except Exception as exc:  # noqa: BLE001
        out["phase"] = "unknown"
        out["label"] = "暂时读不到登录状态，页面会自动重试"
        out["error"] = str(exc) or exc.__class__.__name__
        _last_err = out["error"]
    return out


# ---------------- 二维码图片 ----------------

_png_cache: dict[str, bytes] = {}


def png_for(url: str) -> bytes:
    if url in _png_cache:
        return _png_cache[url]
    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        if _HAS_PIL:
            Image.open(io.BytesIO(data)).verify()
        if len(_png_cache) > 20:
            _png_cache.clear()
        _png_cache[url] = data
        return data
    except Exception:  # noqa: BLE001
        # 退路：用监控脚本写好的固定图片
        with open(EVIDENCE_FALLBACK, "rb") as f:
            return f.read()


# ---------------- 页面 ----------------

PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>QQ 登录二维码</title>
<style>
 :root{--bg:#0b0f17;--card:#141b26;--txt:#e8eef7;--dim:#8b9bb4;--ok:#2ecc71;--warn:#ffb84d;--bad:#ff6b6b}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;background:radial-gradient(1200px 600px at 50% -10%,#1b2740,#0b0f17 60%);
      color:var(--txt);font:16px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
      display:flex;align-items:center;justify-content:center;padding:24px}
 .card{width:100%;max-width:460px;background:var(--card);border:1px solid #22304a;border-radius:20px;
       padding:26px 24px 22px;box-shadow:0 24px 60px rgba(0,0,0,.45);text-align:center}
 h1{font-size:19px;margin:0 0 4px;font-weight:600}
 .sub{color:var(--dim);font-size:13px;margin-bottom:18px}
 .qrbox{position:relative;background:#fff;border-radius:14px;padding:12px;width:min(78vw,320px);
        margin:0 auto;aspect-ratio:1/1;display:flex;align-items:center;justify-content:center}
 .qrbox img{width:100%;height:100%;object-fit:contain;display:block;image-rendering:pixelated}
 .qrbox.dead::after{content:"换码中…";position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
        background:rgba(255,255,255,.92);color:#333;font-size:18px;font-weight:600;border-radius:14px}
 .badge{display:inline-flex;align-items:center;gap:8px;margin:18px 0 6px;padding:8px 16px;border-radius:999px;
        font-size:15px;font-weight:600;background:#1d2839;border:1px solid #2b3a55}
 .dot{width:9px;height:9px;border-radius:50%;background:var(--warn);animation:pulse 1.4s infinite}
 .waiting .dot{background:var(--warn)} .pending .dot{background:#4da3ff}
 .ok .dot{background:var(--ok)} .bad .dot{background:var(--bad)}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
 .meta{color:var(--dim);font-size:13px;margin-top:10px;min-height:40px}
 .bar{height:6px;border-radius:99px;background:#1d2839;overflow:hidden;margin:12px auto 0;max-width:320px}
 .bar>i{display:block;height:100%;width:100%;background:linear-gradient(90deg,#4da3ff,#7d6bff);transition:width .9s linear}
 .big{display:none;margin-top:14px;padding:18px;border-radius:14px;background:rgba(46,204,113,.12);
      border:1px solid rgba(46,204,113,.45);color:#7dffb0;font-size:22px;font-weight:700;line-height:1.45}
 body.done .qrbox,body.done .bar,body.done .meta{display:none}
 body.done .big{display:block}
 body.done .badge{display:none}
 .tip{color:var(--dim);font-size:12px;margin-top:16px;line-height:1.7;text-align:left}
 code{background:#0e1420;padding:1px 5px;border-radius:5px;font-size:12px}
</style></head>
<body>
<div class="card">
  <h1>阿凌 · QQ 登录</h1>
  <div class="sub">用机器人小号扫码，页面会自动换成最新的码</div>
  <div class="qrbox" id="qrbox"><img id="qr" alt="登录二维码"></div>
  <div class="bar"><i id="bar"></i></div>
  <div class="badge waiting" id="badge"><span class="dot"></span><span id="label">加载中…</span></div>
  <div class="meta" id="meta"></div>
  <div class="big">✅ 已登录成功，可以关掉这个页面了</div>
  <div class="tip">
    扫码后手机上若有「登录风险 / 安全验证」提示，按提示完成滑块或短信验证即可；<br>
    这张码约 <b>2 分钟</b>有效，过期后面会自动换新的，不用手动刷新。
  </div>
</div>
<script>
const BASE = __BASE__;
const qr = document.getElementById('qr');
const box = document.getElementById('qrbox');
const badge = document.getElementById('badge');
const label = document.getElementById('label');
const meta = document.getElementById('meta');
const bar = document.getElementById('bar');
let shownUrl = '';

async function tick(){
  try{
    const r = await fetch(BASE + '/state?t=' + Date.now(), {cache:'no-store'});
    const s = await r.json();
    document.body.className = s.phase === 'logged_in' ? 'done' : '';
    badge.className = 'badge ' + (s.phase === 'logged_in' ? 'ok'
                      : s.phase === 'pending_confirm' ? 'pending'
                      : s.phase === 'unknown' ? 'bad' : 'waiting');
    label.textContent = s.label;
    if (s.phase === 'logged_in'){
      meta.textContent = '机器人已上线' + (s.uin ? '：' + s.uin : '');
      return;
    }
    if (s.qr_url && s.qr_url !== shownUrl){
      shownUrl = s.qr_url;
      qr.src = BASE + '/qr.png?u=' + encodeURIComponent(s.qr_url) + '&t=' + Date.now();
    }
    box.classList.toggle('dead', s.phase === 'refreshing' || !s.qr_url);
    const remain = Math.max(0, s.remain|0);
    bar.style.width = Math.min(100, (remain / (s.ttl||120)) * 100) + '%';
    let txt = '这张码生成于 ' + s.age + ' 秒前';
    txt += remain > 0 ? '，约 ' + remain + ' 秒后自动换新' : '，正在换新';
    if (s.error) txt += '<br>（' + s.error + '）';
    meta.innerHTML = txt;
  }catch(e){
    meta.textContent = '网络不通，正在重试…';
  }
}
tick(); setInterval(tick, 3000);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    prefix = ""

    def log_message(self, fmt: str, *args) -> None:  # 安静点：只记到 stdout 一行
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        pre = self.prefix
        if path in (pre, pre + "/"):
            html = PAGE.replace("__BASE__", json.dumps(pre))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == pre + "/state":
            self._send(200, json.dumps(status_snapshot(), ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if path == pre + "/qr.png":
            q = urlparse(self.path).query
            url = ""
            for part in q.split("&"):
                if part.startswith("u="):
                    from urllib.parse import unquote

                    url = unquote(part[2:])
            if not url:
                snap = status_snapshot()
                url = snap.get("qr_url", "")
            if not url:
                self._send(404, b"no qr", "text/plain")
                return
            try:
                data = png_for(url)
            except Exception:  # noqa: BLE001
                self._send(500, b"qr error", "text/plain")
                return
            self._send(200, data, "image/png")
            return
        if path == pre + "/healthz":
            self._send(200, b"ok", "text/plain")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


def main() -> None:
    token = load_token()
    Handler.prefix = "/qqqr-" + token
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print(f"[qr-portal] listening http://{BIND}:{PORT}{Handler.prefix}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
