#!/usr/bin/env python3
"""盘点 qwen-audio-3.0-tts-flash 的系统音色：逐个真调一次，记录成功/失败与音量。

用法（工作区）：
    python3 -I scripts/tts_voice_catalog.py

密钥从 astrbot/data/astrbot/config/astrbot_plugin_aling_voice_config.json 读，
只用于本次调用，不打印。输出：
    evidence/voice_audition/enum/<voice>.wav   每个音色的试听样例
    evidence/voice_enum_3.0.log              逐音色结果表
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONF = os.path.join(ROOT, "astrbot", "data", "astrbot", "config", "astrbot_plugin_aling_voice_config.json")
OUT_WAV = os.path.join(ROOT, "evidence", "voice_audition", "enum")
WORK_WAV = os.path.join(ROOT, "astrbot", "data", "astrbot", "temp", "audition", "enum")
OUT_LOG = os.path.join(ROOT, "evidence", "voice_enum_3.0.log")

URL = "https://maas.qianwenaiapi.com/api/v1/services/audio/tts/SpeechSynthesizer"
TEXT = "你好，我是阿凌。"
# 官方文档「Qwen-Audio-TTS 音色列表」中 qwen-audio-3.0-tts-flash 的系统音色
VOICES = [
    "longanfengyue", "longanyuanfei", "longanlingxi", "longanxiaoxin", "longanhuan_v3.6",
    "longjielidou_v3.6", "longpaopao_v3.6", "longhuohuo_v3.6", "longchuanshu_v3.6",
    "loongmary", "loongeva_v3.6", "loongjohn",
]
# 反例：属于别的模型 / 不存在的音色，用来证明音色是服务端校验的
NEGATIVE = ["longanlingxin", "longanhuan_v3.1", "Serena", "Cherry"]


def ffmpeg_mount_path(p: str) -> str:
    return p.replace(os.path.join(ROOT, "astrbot", "data", "astrbot"), "/AstrBot/data")


def probe(p: str) -> tuple[str, str, str]:
    """时长 / 平均音量 / 峰值音量，借助机器人环境里的 ffmpeg。"""
    mp = ffmpeg_mount_path(p)
    dur = subprocess.run(
        ["docker", "exec", "astrbot", "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp],
        capture_output=True, text=True,
    ).stdout.strip()
    vd = subprocess.run(
        ["docker", "exec", "astrbot", "ffmpeg", "-hide_banner", "-i", mp, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    mean = next((l.split("mean_volume: ")[1] for l in vd.splitlines() if "mean_volume" in l), "-")
    peak = next((l.split("max_volume: ")[1] for l in vd.splitlines() if "max_volume" in l), "-")
    return dur, mean, peak


def synth(key: str, voice: str, text: str = TEXT, volume: int = 100) -> bytes:
    body = {"model": "qwen-audio-3.0-tts-flash",
            "input": {"text": text, "voice": voice, "format": "wav", "sample_rate": 24000, "volume": volume}}
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST",
    )
    try:
        obj = json.loads(urllib.request.urlopen(req, timeout=90).read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:160]}") from None
    url = ((obj.get("output") or {}).get("audio") or {}).get("url")
    if not url:
        raise RuntimeError(json.dumps(obj, ensure_ascii=False)[:200])
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def main() -> int:
    with open(CONF, encoding="utf-8") as f:
        key = json.load(f)["api_key"]
    os.makedirs(OUT_WAV, exist_ok=True)
    os.makedirs(WORK_WAV, exist_ok=True)
    lines = ["qwen-audio-3.0-tts-flash 系统音色实测（每个音色真调一次，volume=100，24kHz wav）",
             "voice\t结果\t字节\t时长s\t平均音量\t峰值音量"]
    ok = 0
    for v in VOICES:
        p = os.path.join(WORK_WAV, f"{v}.wav")
        try:
            data = synth(key, v)
            with open(p, "wb") as f:
                f.write(data)
            with open(os.path.join(OUT_WAV, f"{v}.wav"), "wb") as f:
                f.write(data)
            dur, mean, peak = probe(p)
            ok += 1
            lines.append(f"{v}\tOK\t{len(data)}\t{dur}\t{mean}\t{peak}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{v}\tFAIL\t{exc}")
    lines.append("")
    lines.append("反例（属于其他模型 / 不存在，预期失败）：")
    for v in NEGATIVE:
        try:
            synth(key, v)
            lines.append(f"{v}\tOK（意外可用）")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{v}\tFAIL\t{exc}")
    lines.append("")
    lines.append(f"可用 {ok}/{len(VOICES)}")
    print("\n".join(lines))
    with open(OUT_LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok == len(VOICES) else 1


if __name__ == "__main__":
    sys.exit(main())
