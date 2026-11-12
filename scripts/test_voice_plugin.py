#!/usr/bin/env python3
"""在机器人运行环境里验证语音插件的两条关键路径（不进对话、不发 QQ 消息）。

跑法（在 Linux 服务器的机器人运行环境里）：
    python3 /AstrBot/data/plugins/astrbot_plugin_aling_voice/../../scripts/test_voice_plugin.py

做三件事：
 1. clean_for_speech：动作描写/表情/markdown 是否被清掉、正文是否保留
 2. _request_pcm：用真实密钥真调语音接口，拿到 PCM 字节数
 3. ffmpeg 转 mp3：落盘、检查体积与时长

输出 evidence/voice_unit.log。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time

# 在工作区里跑时用工作区路径；复制进机器人运行环境（/AstrBot/data/scripts/）后自动切到容器内路径。
_HERE = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists("/AstrBot/data/config/astrbot_plugin_aling_voice_config.json"):
    ROOT = os.path.dirname(_HERE)
    PLUGIN = os.path.join(ROOT, "plugins", "astrbot_plugin_aling_voice", "main.py")
    CONF = os.path.join(ROOT, "astrbot", "data", "astrbot", "config", "astrbot_plugin_aling_voice_config.json")
    OUT = os.path.join(ROOT, "evidence", "voice_unit.log")
    TMP = os.path.join(ROOT, "evidence", "voice_unit")
else:
    ROOT = "/AstrBot/data"
    PLUGIN = "/AstrBot/data/plugins/astrbot_plugin_aling_voice/main.py"
    CONF = "/AstrBot/data/config/astrbot_plugin_aling_voice_config.json"
    OUT = "/AstrBot/data/evidence/voice_unit.log"
    TMP = "/AstrBot/data/temp/voice_unit"

lines: list[str] = []


def log(s: str) -> None:
    print(s, flush=True)
    lines.append(s)


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("aling_voice_main", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aling_voice_main"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    os.makedirs(TMP, exist_ok=True)
    mod = load_plugin_module()
    log(f"插件模块已加载：{PLUGIN}")

    cases = [
        ("*揉着眼睛往被子里缩* 哈啊……吵死了；本小姐叫阿凌啊(￣^￣)", "吵死了；本小姐叫阿凌啊"),
        ("**啧**，又来？(￣^￣)😗 本小姐还没睡醒呢……", "啧，又来？本小姐还没睡醒呢"),
        ("| 阿凌 | 才不呢 |", "阿凌，才不呢"),
    ]
    ok = 0
    for src, expect in cases:
        got = mod.AlingVoicePlugin.clean_for_speech(src)
        hit = expect in got and "*" not in got and "（" not in got
        ok += 1 if hit else 0
        log(f"clean {'PASS' if hit else 'FAIL'} src={src!r} -> {got!r}")
    log(f"清洗用例 {ok}/{len(cases)}")

    with open(CONF, encoding="utf-8") as f:
        conf = json.load(f)
    log(f"配置文件读取成功 key_len={len(conf.get('api_key',''))} model={conf.get('model')} voice={conf.get('voice')}")

    obj = object.__new__(mod.AlingVoicePlugin)
    obj.cfg = {**mod.DEFAULT_CONFIG, **conf}
    from pathlib import Path

    obj.cache_dir = Path(TMP)

    text = "哈啊……杂鱼哥哥，你就不能让我多睡一会儿吗。"
    t0 = time.time()
    audio, ext = obj._request_audio(text, conf["api_key"])
    dt = time.time() - t0
    log(f"接口调用 音频={len(audio)} 字节 格式={ext}，耗时 {dt:.1f}s（model={conf.get('model')} voice={conf.get('voice')} volume={conf.get('volume')}）")
    mp3 = Path(TMP) / f"unit{ext}"
    mp3.write_bytes(audio)
    size = mp3.stat().st_size if mp3.exists() else 0
    log(f"mp3 落盘 {mp3} {size} 字节")

    vol = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(mp3), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    loud = [l.split("] ")[-1] for l in vol.stderr.splitlines() if "mean_volume" in l or "max_volume" in l]
    log("音量检测 " + " | ".join(loud))
    mean_db = next((float(l.split("mean_volume: ")[1].split()[0]) for l in vol.stderr.splitlines() if "mean_volume" in l), -99.0)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
        capture_output=True, text=True,
    )
    log(f"ffprobe 时长={probe.stdout.strip() or probe.stderr.strip()}")

    # 音量必须明显偏大：平均音量高于 -16 dB（volume=100 时实测约 -12 ~ -14 dB）
    passed = ok == len(cases) and len(audio) > 10000 and size > 1000 and mean_db > -16.0
    log(f"RESULT {'PASS' if passed else 'FAIL'}")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(f"语音插件单元验证（真实接口调用，未发 QQ 消息）\n时间：{time.strftime('%F %T')}\n\n")
        f.write("\n".join(lines) + "\n")
    print("evidence ->", OUT)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
