#!/usr/bin/env python3
"""给「元气甜美、偏少女」挑音色：同一句文本、同一个 volume=100，真合成候选小样。

用法（工作区）：
    python3 -I scripts/voice_pick_candidates.py

密钥从插件配置读，只用于本次调用，不打印。输出：
    evidence/voice_pick/<voice>.mp3 / .wav
    evidence/voice_pick/measure.log
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONF = os.path.join(ROOT, "astrbot", "data", "astrbot", "config", "astrbot_plugin_aling_voice_config.json")
OUT_DIR = os.path.join(ROOT, "evidence", "voice_pick")
WORK_DIR = os.path.join(ROOT, "astrbot", "data", "astrbot", "temp", "voice_pick")
LOG = os.path.join(OUT_DIR, "measure.log")
URL = "https://maas.qianwenaiapi.com/api/v1/services/audio/tts/SpeechSynthesizer"

# 同一句：覆盖阿凌的短句 + 撒娇 + 疑问，方便听出甜度、气声和句尾上扬
TEXT = "哈啊，终于睡醒了。杂鱼哥哥今天也起这么早啊？快说，是不是想我了。"

CANDIDATES = [
    ("longanxiaoxin", "龙安小昕", "22 岁，官方标注「亲切活泼音」，音高 274Hz"),
    ("longanhuan_v3.6", "龙安欢", "25 岁，官方未标特质，音高 247Hz"),
    ("longpaopao_v3.6", "龙泡泡", "5 岁儿童音，官方「软糯可爱音」，音高 361Hz（对照组）"),
    ("longanlingxi", "龙安灵希", "现在在用的音色，25 岁「可爱甜美音」，音高 247Hz（对照组）"),
]


def synth(voice: str, key: str, text: str) -> tuple[bytes, float]:
    body = {
        "model": "qwen-audio-3.0-tts-flash",
        "input": {
            "text": text,
            "voice": voice,
            "format": "mp3",
            "sample_rate": 24000,
            "volume": 100,
        },
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        obj = json.loads(r.read() or b"{}")
    if obj.get("code") and obj.get("message"):
        raise RuntimeError(f"{obj['code']}: {obj['message']}")
    audio_url = (((obj.get("output") or {}).get("audio") or {}).get("url"))
    if not audio_url:
        raise RuntimeError("接口没返回音频地址")
    with urllib.request.urlopen(audio_url, timeout=60) as r:
        data = r.read()
    return data, time.time() - t0


def probe(name: str) -> tuple[str, str, str, str]:
    mp = "/AstrBot/data/temp/voice_pick/" + name
    info = subprocess.run(
        ["docker", "exec", "astrbot", "ffprobe", "-v", "error",
         "-show_entries", "stream=codec_name,sample_rate,channels:format=duration,bit_rate",
         "-of", "csv=p=0", mp],
        capture_output=True, text=True,
    ).stdout.strip().replace("\n", " ")
    vd = subprocess.run(
        ["docker", "exec", "astrbot", "ffmpeg", "-hide_banner", "-i", mp,
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    mean = next((l.split("mean_volume: ")[1].split()[0] for l in vd.splitlines() if "mean_volume" in l), "-")
    peak = next((l.split("max_volume: ")[1].split()[0] for l in vd.splitlines() if "max_volume" in l), "-")
    return info, mean, peak


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    with open(CONF, encoding="utf-8-sig") as f:
        key = json.load(f)["api_key"]

    lines = [f"音色候选小样（同一句、volume=100，真实调用）\n时间：{time.strftime('%F %T')}\n文本：{TEXT}\n"]
    ok = 0
    for voice, cn, note in CANDIDATES:
        try:
            data, dt = synth(voice, key, TEXT)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{voice:20s} {cn:6s} FAIL {type(exc).__name__}: {exc}")
            print(lines[-1], flush=True)
            continue
        ext = ".wav" if data[:4] == b"RIFF" else ".mp3"
        name = f"{voice}{ext}"
        p = os.path.join(OUT_DIR, name)
        with open(p, "wb") as fh:
            fh.write(data)
        shutil.copyfile(p, os.path.join(WORK_DIR, name))
        info, mean, peak = probe(name)
        lines.append(
            f"{voice:20s} {cn:6s} {len(data):7d} B {ext} 合成 {dt:.1f}s  probe={info}  "
            f"mean={mean} dB peak={peak} dB  | {note}"
        )
        print(lines[-1], flush=True)
        ok += 1

    lines.append(f"\n成功 {ok}/{len(CANDIDATES)}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("evidence ->", LOG)
    return 0 if ok == len(CANDIDATES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
