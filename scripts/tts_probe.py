#!/usr/bin/env python3
"""探测用户提供的阿里云 MaaS（OpenAI 兼容）语音合成接口到底怎么调。

做法：把几种常见调用方式各打一次，如实记录 HTTP 状态与返回体片段，
选出一条真正能拿到音频的路径，并把音频样例存到 evidence/。

输出：
  evidence/tts_probe.log        探测矩阵（状态码、返回片段、结论）
  evidence/tts_sample.<ext>     最终样例音频（PCM 流按需再转 wav/mp3）

密钥只从环境变量 MaaS_TTS_API_KEY 读取，不写进本文件。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EV = os.path.join(ROOT, "evidence")
BASE = os.environ.get("MAAS_BASE", "https://maas.qianwenaiapi.com/compatible-mode/v1").rstrip("/")
KEY = os.environ.get("MAAS_TTS_API_KEY", "").strip()
TEXT = "测试一下，听得见吗？"

LOG: list[str] = []


def log(line: str) -> None:
    print(line, flush=True)
    LOG.append(line)


def post(path: str, body: dict, raw: bool = False, timeout: int = 90):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return r.status, data, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:  # noqa: BLE001
        return -1, str(e).encode(), {}


def short(b: bytes, n: int = 260) -> str:
    s = b[:n].decode("utf-8", "replace")
    s = re.sub(r"[A-Za-z0-9+/]{120,}", "<base64>", s)
    return s.replace("\n", " ")


def main() -> int:
    if not KEY:
        log("缺少环境变量 MAAS_TTS_API_KEY")
        return 2
    os.makedirs(EV, exist_ok=True)
    log(f"base={BASE} text={TEXT!r} at={time.strftime('%F %T')}")
    log("")

    # 1) 标准 OpenAI TTS 端点
    log("== 1. POST /audio/speech（OpenAI 标准 TTS 端点）")
    for model in ("qwen-audio-3.0-tts-flash", "qwen3-tts-flash"):
        st, b, h = post("/audio/speech", {"model": model, "input": TEXT, "voice": "Ethan",
                                          "response_format": "mp3"})
        log(f"   model={model} status={st} ctype={h.get('Content-Type','')} body={short(b)}")

    # 2) 用户给的模型走 chat/completions（非流式）
    log("")
    log("== 2. POST /chat/completions  model=qwen-audio-3.0-tts-flash（非流式）")
    st, b, h = post("/chat/completions", {
        "model": "qwen-audio-3.0-tts-flash",
        "messages": [{"role": "user", "content": TEXT}],
        "modalities": ["text", "audio"],
        "audio": {"voice": "Ethan", "format": "wav"},
    })
    log(f"   status={st} ctype={h.get('Content-Type','')} body={short(b)}")

    # 3) 同模型 + stream
    log("")
    log("== 3. POST /chat/completions  model=qwen-audio-3.0-tts-flash（stream=true）")
    st, b, h = post("/chat/completions", {
        "model": "qwen-audio-3.0-tts-flash",
        "messages": [{"role": "user", "content": TEXT}],
        "modalities": ["text", "audio"],
        "audio": {"voice": "Ethan", "format": "wav"},
        "stream": True,
    })
    log(f"   status={st} ctype={h.get('Content-Type','')} body={short(b)}")

    # 4) 可用的 omni 模型 + stream（这是实测能出音频的路径）
    log("")
    log("== 4. POST /chat/completions  model=qwen3.5-omni-flash（stream=true + audio 输出）")
    st, b, h = post("/chat/completions", {
        "model": "qwen3.5-omni-flash",
        "messages": [{"role": "user", "content": TEXT}],
        "modalities": ["text", "audio"],
        "audio": {"voice": "Ethan", "format": "wav"},
        "stream": True,
    })
    log(f"   status={st} ctype={h.get('Content-Type','')}")

    pcm = bytearray()
    text_out = ""
    audio_meta = ""
    if st == 200:
        for line in b.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    text_out += str(d["content"])
                au = d.get("audio") or {}
                if au.get("data"):
                    pcm += base64.b64decode(au["data"])
                    audio_meta = f"id={au.get('id','')} expires_at={au.get('expires_at','')}"
    log(f"   text={text_out!r}")
    log(f"   pcm_bytes={len(pcm)} {audio_meta}")

    if len(pcm) < 1000:
        log("   结论：没拿到音频（该模型/该参数组合不可用）")
        with open(os.path.join(EV, "tts_probe.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(LOG) + "\n")
        return 1

    raw_path = os.path.join(EV, "tts_sample.pcm")
    with open(raw_path, "wb") as f:
        f.write(bytes(pcm))
    dur = len(pcm) / (24000 * 2)
    log(f"   结论：可用。PCM16 24kHz 单声道，约 {dur:.1f}s -> {raw_path}")
    log("")
    log("可复现的 curl：")
    log(f"""curl -sS {BASE}/chat/completions \\
  -H "Authorization: Bearer $MAAS_TTS_API_KEY" -H "Content-Type: application/json" \\
  -d '{{"model":"qwen3.5-omni-flash","modalities":["text","audio"],"audio":{{"voice":"Ethan","format":"wav"}},"stream":true,"messages":[{{"role":"user","content":"{TEXT}"}}]}}'""")

    with open(os.path.join(EV, "tts_probe.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")
    print("evidence ->", os.path.join(EV, "tts_probe.log"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
