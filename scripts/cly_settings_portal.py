#!/usr/bin/env python3
"""cly 托管设置页（只监听 127.0.0.1，由 nginx 反代到随机路径）。

和两个扫码页同一套做法：URL 里的随机串就是访问密码
    https://<域名>/cly-set-<TOKEN>/
不带串 / 串不对一律 404。所有接口都在这个前缀下。

能改的 5 项（cly 主人自己填，我们只托管不自费）：
    deepseek_key    他的 DeepSeek Key（sk- 开头）
    deepseek_model  用哪个 DeepSeek 模型（默认 deepseek-chat）
    voice_key       他的语音 Key
    voice_base_url  语音接口地址（默认 https://maas.qianwenaiapi.com）
    voice_model     语音模型（默认 qwen-audio-3.0-tts-flash）

保存时：
  1. 写进 cly 自己的配置（cly/data/cli_settings/secret.json 等，与第一台无关）；
  2. 语音 key 有填就真发一次最小合成请求试一下，结果写进 voice_status.json；
  3. 重启 cly 的聊天程序让它读到新配置（本机 1 分钟内生效，页面会等到它起来）。

页面回传只给「填没填 + 末 4 位」，绝不回显完整 key。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cly", "tools"))
import cly_settings as cs  # noqa: E402

TOKEN_FILE = os.path.join(ROOT, ".cly_set_token")
BIND = os.environ.get("CLY_SET_BIND", "127.0.0.1")
PORT = int(os.environ.get("CLY_SET_PORT", "18125"))
RESTART_ON_SAVE = os.environ.get("CLY_SET_RESTART", "1") == "1"

_lock = threading.Lock()
_last_save = {"at": 0.0, "result": None}


def load_token() -> str:
    tok = os.environ.get("CLY_SET_TOKEN", "").strip()
    if tok:
        return tok
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return f.read().strip()


PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>cly · 托管设置</title>
<style>
 :root{--bg:#0b0f17;--card:#141b26;--line:#22304a;--txt:#e8eef7;--dim:#8b9bb4;--ok:#2ecc71;--warn:#ffb84d;--bad:#ff6b6b;--blue:#4da3ff}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;background:radial-gradient(1200px 600px at 50% -10%,#1b2740,#0b0f17 60%);
      color:var(--txt);font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
      display:flex;justify-content:center;padding:28px 16px}
 .card{width:100%;max-width:640px;background:var(--card);border:1px solid var(--line);border-radius:20px;
       padding:26px 24px;box-shadow:0 24px 60px rgba(0,0,0,.45)}
 h1{font-size:20px;margin:0 0 4px;font-weight:600}
 .sub{color:var(--dim);font-size:13px;margin-bottom:20px}
 .field{margin:0 0 18px;padding-bottom:16px;border-bottom:1px dashed rgba(255,255,255,.07)}
 .field:last-of-type{border-bottom:0}
 label{display:flex;align-items:center;justify-content:space-between;font-weight:600;font-size:14px;margin-bottom:6px}
 .state{font-size:12px;font-weight:500;color:var(--dim)}
 .state.ok{color:var(--ok)} .state.warn{color:var(--warn)} .state.bad{color:var(--bad)}
 input[type=text],input[type=password]{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
      background:#0e1420;color:var(--txt);font-size:14px;outline:none}
 input:focus{border-color:var(--blue)}
 .tip{color:var(--dim);font-size:12px;margin-top:6px;line-height:1.65}
 .row{display:flex;gap:10px;margin-top:18px;align-items:center;flex-wrap:wrap}
 button{border:0;border-radius:10px;padding:11px 22px;font-size:15px;font-weight:600;cursor:pointer}
 #save{background:linear-gradient(90deg,#4da3ff,#7d6bff);color:#fff}
 #save:disabled{opacity:.55;cursor:default}
 .ghost{background:#1d2839;color:var(--dim);font-size:12px;padding:6px 10px;font-weight:500}
 .banner{margin-top:18px;padding:12px 14px;border-radius:12px;font-size:13px;display:none;line-height:1.7;white-space:pre-wrap}
 .banner.ok{display:block;background:rgba(46,204,113,.12);border:1px solid rgba(46,204,113,.45);color:#7dffb0}
 .banner.bad{display:block;background:rgba(255,107,107,.12);border:1px solid rgba(255,107,107,.45);color:#ffb3b3}
 .banner.info{display:block;background:rgba(77,163,255,.10);border:1px solid rgba(77,163,255,.4);color:#b7d6ff}
 .foot{color:var(--dim);font-size:12px;margin-top:18px;line-height:1.8}
 code{background:#0e1420;padding:1px 5px;border-radius:5px;font-size:12px}
</style></head>
<body>
<div class="card">
  <h1>cly · 托管设置</h1>
  <div class="sub">这 5 项都是这台机器人自己的，填完点保存立刻生效。费用走你自己的账号，我们不代付。</div>

  <div class="field">
    <label>① DeepSeek Key <span class="state" id="s-deepseek_key"></span></label>
    <input type="password" id="deepseek_key" autocomplete="off" spellcheck="false">
    <div class="tip">在 DeepSeek 官网申请的 key（sk- 开头）。不填之前机器人只回一句「还没配置」，不会调用任何付费接口。</div>
  </div>

  <div class="field">
    <label>② DeepSeek 模型 <span class="state" id="s-deepseek_model"></span></label>
    <input type="text" id="deepseek_model" autocomplete="off" spellcheck="false" placeholder="deepseek-chat">
    <div class="tip">填模型名，留空默认 <code>deepseek-chat</code>（便宜够用）。</div>
  </div>

  <div class="field">
    <label>③ 语音 Key <span class="state" id="s-voice_key"></span></label>
    <input type="password" id="voice_key" autocomplete="off" spellcheck="false">
    <div class="tip">把文字读成语音用的 key。不填就只发文字，不会去调语音接口、也不会报错。</div>
  </div>

  <div class="field">
    <label>④ 语音接口地址 <span class="state" id="s-voice_base_url"></span></label>
    <input type="text" id="voice_base_url" autocomplete="off" spellcheck="false" placeholder="https://maas.qianwenaiapi.com">
    <div class="tip">语音服务的主机地址；带不带 <code>/api/v1</code> 都行。留空用默认地址。</div>
  </div>

  <div class="field">
    <label>⑤ 语音模型 <span class="state" id="s-voice_model"></span></label>
    <input type="text" id="voice_model" autocomplete="off" spellcheck="false" placeholder="qwen-audio-3.0-tts-flash">
    <div class="tip">语音合成的模型名，留空默认 <code>qwen-audio-3.0-tts-flash</code>。</div>
  </div>

  <div class="row">
    <button id="save">保存并生效</button>
    <button class="ghost" data-clear="deepseek_key">清空 DeepSeek Key</button>
    <button class="ghost" data-clear="voice_key">清空语音 Key</button>
  </div>
  <div class="banner" id="banner"></div>
  <div class="foot" id="foot"></div>
</div>
<script>
const BASE = __BASE__;
const KEYS = ['deepseek_key','deepseek_model','voice_key','voice_base_url','voice_model'];
const SECRET = new Set(['deepseek_key','voice_key']);
const DEFAULT_TEXT = {deepseek_model:'deepseek-chat', voice_base_url:'https://maas.qianwenaiapi.com', voice_model:'qwen-audio-3.0-tts-flash'};
const el = id => document.getElementById(id);
const banner = el('banner');

function setState(id, txt, cls){ const n = el('s-' + id); n.textContent = txt; n.className = 'state ' + (cls||''); }
function show(cls, text){ banner.className = 'banner ' + cls; banner.textContent = text; }

async function load(){
  const r = await fetch(BASE + '/status?t=' + Date.now(), {cache:'no-store'});
  const s = await r.json();
  if (!s.ok){ show('bad', '读不到当前配置：' + (s.error||'未知错误')); return; }
  for (const k of KEYS){
    const f = s.fields[k];
    setState(k, f.filled ? (SECRET.has(k) ? '已填 · 尾号 ' + f.tail : '已填') : '未填',
             f.filled ? 'ok' : 'warn');
    // 非密钥项直接把当前值填进去；密钥项留空 = 不改
    if (!SECRET.has(k)) el(k).value = f.value || '';
    el(k).placeholder = SECRET.has(k)
      ? (f.filled ? '已填写（尾号 ' + f.tail + '），留空表示不改动' : '还没填，粘贴到这里')
      : (DEFAULT_TEXT[k] || '');
  }
  el('foot').innerHTML = '语音检测：' + (s.voice_check.voice_ok ? '✅ 可用' : '⚠️ ' + (s.voice_check.msg || '未测'))
    + (s.voice_check.checked_at ? '（' + s.voice_check.checked_at + ' 测过）' : '')
    + '<br>保存后机器人会自动重启，约 10~30 秒；重启期间发消息它会稍晚回。';
}

async function save(clear){
  const body = {clear: clear ? [clear] : []};
  for (const k of KEYS){
    const v = el(k).value.trim();
    if (SECRET.has(k)){ if (v) body[k] = v; }      // 密钥：留空=不改
    else { body[k] = v; }
  }
  if (!clear && !body.deepseek_key && !body.voice_key){
    show('info', '密钥两栏没填就是「保持原样」。改别的项直接保存即可。');
  }
  el('save').disabled = true;
  show('info', '正在保存…' + (clear ? '' : '（会真发一次语音检测，约几秒）'));
  try{
    const r = await fetch(BASE + '/save', {method:'POST', cache:'no-store',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const s = await r.json();
    if (s.ok){
      show('ok', '✅ 已保存。\n' + (s.voice_msg ? '语音检测：' + s.voice_msg + '\n' : '')
        + (s.restarted ? '机器人已重启并读到新配置，现在生效。' : '配置已写入；机器人下次重启时生效。'));
    } else {
      show('bad', '保存失败：' + (s.error || '未知错误'));
    }
  }catch(e){ show('bad', '网络出错：' + e); }
  el('save').disabled = false;
  await load();
}

el('save').onclick = () => save(null);
document.querySelectorAll('[data-clear]').forEach(b => {
  b.onclick = () => {
    const k = b.dataset.clear;
    if (confirm('确定清空「' + (k === 'deepseek_key' ? 'DeepSeek Key' : '语音 Key') + '」吗？清空后对应功能会停用。')){
      el(k).value = '';
      save(k);
    }
  };
});
load();
</script>
</body></html>
"""


def status_payload() -> dict:
    vals = cs.load_secret()
    fields = {}
    for k, meta in cs.FIELDS.items():
        v = vals.get(k, "")
        fields[k] = {
            "label": meta["label"],
            "filled": bool(v),
            "tail": cs.tail(v) if meta["secret"] else "",
            "value": "" if meta["secret"] else v,
            "secret": bool(meta["secret"]),
        }
    return {"ok": True, "fields": fields, "voice_check": cs.load_voice_status()}


def do_save(body: dict) -> dict:
    """只接受白名单字段；密钥留空=不改；clear 列表里的清空。"""
    with _lock:
        vals = cs.load_secret()
        for k in body.get("clear") or []:
            if k in cs.FIELDS:
                vals[k] = ""
        for k in cs.FIELDS:
            v = body.get(k)
            if not isinstance(v, str):
                continue
            v = v.strip()
            if not v:
                continue
            if cs.FIELDS[k]["secret"]:
                vals[k] = v          # 密钥只在有输入时覆盖
            else:
                vals[k] = v
        cs.save_secret(vals)
        applied = cs.apply(vals)
        cs.fix_perms()

        restarted = False
        if RESTART_ON_SAVE:
            rc, _msg = cs.restart_container()
            restarted = rc == 0 and cs.wait_ready(90)

        _last_save["at"] = time.time()
        _last_save["result"] = applied
        return {
            "ok": True,
            "restarted": restarted,
            "voice_ok": applied.get("voice_ok"),
            "voice_msg": applied.get("voice_msg"),
        }


class Handler(BaseHTTPRequestHandler):
    prefix = ""

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        pre = self.prefix
        if path in (pre, pre + "/", pre + "/index.html"):
            html = PAGE.replace("__BASE__", json.dumps(pre))
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return
        if path == pre + "/status":
            try:
                self._json(200, status_payload())
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if path == pre + "/healthz":
            self._json(200, {"ok": True})
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != self.prefix + "/save":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 16384:
                self._json(400, {"ok": False, "error": "请求体为空或过大"})
                return
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("body 必须是对象")
            self._json(200, do_save(body))
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": str(exc)})


def main() -> None:
    token = load_token()
    Handler.prefix = "/cly-set-" + token
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    sys.stdout.write(
        "[cly-set] listening on http://%s:%d%s/\n" % (BIND, PORT, Handler.prefix)
    )
    sys.stdout.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
