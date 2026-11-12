#!/usr/bin/env python3
"""一条命令切换阿凌的音色（只改插件配置里的 voice，然后提示重启）。

用法：
    python3 scripts/set_voice.py                 # 看当前音色和可选项
    python3 scripts/set_voice.py longanxiaoxin   # 切换并自动重启机器人

改的是 astrbot/data/astrbot/config/astrbot_plugin_aling_voice_config.json
（权限保持 600），不会打印密钥。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONF = os.path.join(ROOT, "astrbot", "data", "astrbot", "config", "astrbot_plugin_aling_voice_config.json")

VOICES = {
    "longanxiaoxin": "龙安小昕 · 22 岁 · 亲切活泼音（默认，元气少女）",
    "longanhuan_v3.6": "龙安欢 · 25 岁 · 明亮俏皮",
    "longanlingxi": "龙安灵希 · 25 岁 · 可爱甜美音（旧默认，偏成熟）",
    "longanfengyue": "龙安风悦 · 30 岁 · 自然亲切",
    "longanyuanfei": "龙安元妃 · 30 岁 · 高傲妃子音",
    "longpaopao_v3.6": "龙泡泡 · 5 岁 · 软糯儿童音",
    "longhuohuo_v3.6": "龙火火 · 8 岁 · 顽皮少年音",
    "longjielidou_v3.6": "龙杰力豆 · 5 岁 · 天真男童",
    "longchuanshu_v3.6": "龙川叔 · 40 岁 · 川普大叔音",
    "loongmary": "loongmary · 英文专用",
    "loongeva_v3.6": "loongeva · 英文专用",
    "loongjohn": "loongjohn · 英文专用",
}


def main() -> int:
    with open(CONF, encoding="utf-8-sig") as f:
        cfg = json.load(f)

    if len(sys.argv) < 2:
        print(f"当前音色：{cfg.get('voice')}（{VOICES.get(cfg.get('voice'), '未知')}）")
        print("\n可切换：")
        for k, v in VOICES.items():
            print(f"  {k:20s} {v}")
        print("\n切换示例：python3 scripts/set_voice.py longanxiaoxin")
        return 0

    voice = sys.argv[1].strip()
    if voice not in VOICES:
        print(f"不认识的音色：{voice}\n可用：{', '.join(VOICES)}")
        return 2

    old = cfg.get("voice")
    cfg["voice"] = voice
    with open(CONF, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(CONF, 0o600)
    print(f"音色已改：{old} → {voice}（{VOICES[voice]}）")

    if "--no-restart" not in sys.argv:
        print("重启机器人让它生效…")
        r = subprocess.run(["docker", "restart", "astrbot"], capture_output=True, text=True)
        if r.returncode != 0:
            print("重启失败，请手动执行：docker restart astrbot")
            print(r.stderr.strip())
            return 1
        print("已重启。可以发「语音状态」看看是否仍是语音模式。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
