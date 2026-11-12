#!/usr/bin/env python3
"""用假 NapCat 走一遍「语音模式 → 真实生成语音 → 收回文字模式」。

它扮演 NapCat 连上 AstrBot 的反向 WS，按顺序做三件事：

1. 管理员发 `/语音模式`，确认回复仍是文字
2. 管理员发一句普通聊天，抓到 AstrBot 真正发出去的 record 帧，
   把 base64 音频落盘成 mp3，再用 ffmpeg 量音量/时长
3. 管理员发 `/文字模式` 收尾，保证不会把真实群的回复切成语音

真实调用百炼 TTS（消耗少量额度），不会向真实 QQ 账号发任何消息。

用法：
    python3 -I scripts/verify_voice_e2e.py [--url ws://127.0.0.1:6199/ws]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "evidence", "voice_e2e")
OUT_LOG = os.path.join(ROOT, "evidence", "voice_e2e.log")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))  # 你的管理员 QQ 号，跑验证前 export ADMIN_ID=...
SELF_ID = 10000
GROUP_ID = 555111

lines: list[str] = []


def log(s: str) -> None:
    print(s, flush=True)
    lines.append(s)


def build_event(text: str, mid: int) -> dict:
    return {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "message",
        "message_id": mid,
        "font": 0,
        "sub_type": "normal",
        "sender": {"user_id": ADMIN_ID, "nickname": "管理员", "card": "管理员"},
        "to_me": True,
        "group_id": GROUP_ID,
        "user_id": ADMIN_ID,
        "message_type": "group",
        "message": [
            {"type": "at", "data": {"qq": str(SELF_ID)}},
            {"type": "text", "data": {"text": " " + text}},
        ],
        "raw_message": f"[CQ:at,qq={SELF_ID}] {text}",
    }


def describe(params: dict) -> tuple[str, list[str]]:
    """返回（可读摘要, record 的 base64 数据列表）。"""
    parts, audios = [], []
    for seg in params.get("message", []):
        if not isinstance(seg, dict):
            continue
        st, data = seg.get("type"), seg.get("data", {}) or {}
        if st == "text":
            parts.append(data.get("text", ""))
        elif st == "record":
            f = data.get("file", "")
            parts.append(f"[record {len(f)}B]")
            if f.startswith("base64://"):
                audios.append(f[len("base64://"):])
            elif f.startswith(("http://", "https://")):
                parts.append("(url 形式)")
        elif st == "at":
            parts.append(f"[at:{data.get('qq')}]")
        elif st == "reply":
            parts.append("[reply]")
        else:
            parts.append(f"[{st}]")
    return "".join(parts), audios


async def run(url: str) -> int:
    import websockets

    frames: list[dict] = []

    async with websockets.connect(
        url,
        additional_headers={"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"},
        ping_interval=None,
        max_size=None,
    ) as ws:

        async def reader():
            while True:
                raw = await ws.recv()
                data = json.loads(raw)
                action = data.get("action")
                if action is None:
                    continue
                if action in ("send_msg", "send_group_msg", "send_private_msg"):
                    frames.append(data)
                result = {"status": "ok", "retcode": 0,
                          "data": {"message_id": 800 + len(frames)}, "echo": data.get("echo")}
                if action == "get_login_info":
                    result["data"] = {"user_id": SELF_ID, "nickname": "capture"}
                else:
                    result["data"] = {"user_id": ADMIN_ID, "nickname": "管理员"}
                await ws.send(json.dumps(result, ensure_ascii=False))

        task = asyncio.create_task(reader())
        await asyncio.sleep(1.0)

        async def send_and_collect(text: str, wait: float) -> list[dict]:
            start = len(frames)
            await ws.send(json.dumps(build_event(text, int(time.time() * 1000) % 900_000_000), ensure_ascii=False))
            deadline = time.time() + wait
            last = start
            quiet = 0.0
            while time.time() < deadline:
                await asyncio.sleep(0.2)
                if len(frames) > last:
                    last, quiet = len(frames), 0.0
                elif last > start:
                    quiet += 0.2
                    if quiet >= 2.0:
                        break
            return frames[start:]

        # 1. 开语音模式
        got = await send_and_collect("/语音模式", 15)
        log("§1 /语音模式 → " + " | ".join(describe(f.get("params", {}))[0] for f in got) or "(无回复)")

        # 2. 普通聊天，抓 record
        t0 = time.time()
        got = await send_and_collect("喂，说句话听听", 90)
        dt = time.time() - t0
        os.makedirs(OUT_DIR, exist_ok=True)
        saved = []
        for i, f in enumerate(got, 1):
            summary, audios = describe(f.get("params", {}))
            log(f"§2 第{i}条 action={f.get('action')} 内容={summary!r} 耗时={dt:.1f}s")
            for j, b64 in enumerate(audios):
                data = base64.b64decode(b64)
                # 百炼这边即使要 mp3 也可能回 wav，按文件头定后缀
                ext = ".wav" if data[:4] == b"RIFF" else ".mp3"
                p = os.path.join(OUT_DIR, f"voice_{i}_{j}{ext}")
                with open(p, "wb") as fh:
                    fh.write(data)
                saved.append((p, len(data)))

        # 3. 收尾：切回文字
        got = await send_and_collect("/文字模式", 15)
        log("§3 /文字模式 → " + " | ".join(describe(f.get("params", {}))[0] for f in got) or "(无回复)")
        task.cancel()

    ok = bool(saved)
    probe_dir = os.path.join(ROOT, "astrbot", "data", "astrbot", "temp", "voice_e2e")
    os.makedirs(probe_dir, exist_ok=True)
    for p, size in saved:
        # 机器人环境里看到的是 /AstrBot/data，把文件拷到挂载目录下再量
        import shutil
        probe_file = os.path.join(probe_dir, os.path.basename(p))
        shutil.copyfile(p, probe_file)
        mp = probe_file.replace(os.path.join(ROOT, "astrbot", "data", "astrbot"), "/AstrBot/data")
        probe = subprocess.run(
            ["docker", "exec", "astrbot", "ffprobe", "-v", "error",
             "-show_entries", "stream=codec_name,bit_rate:format=duration", "-of", "csv=p=0", mp],
            capture_output=True, text=True).stdout.strip()
        vd = subprocess.run(
            ["docker", "exec", "astrbot", "ffmpeg", "-hide_banner", "-i", mp, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True).stderr
        loud = [l.split("] ")[-1] for l in vd.splitlines() if "mean_volume" in l or "max_volume" in l]
        log(f"§2 落盘 {os.path.relpath(p, ROOT)} {size} 字节 probe={probe} {' | '.join(loud)}")
        if "mean_volume: -" in " ".join(loud):
            mean_db = float(next(l for l in loud if "mean_volume" in l).split("mean_volume: ")[1].split()[0])
            ok = ok and mean_db > -16.0

    log(f"RESULT {'PASS' if ok else 'FAIL'}")
    os.makedirs(os.path.dirname(OUT_LOG), exist_ok=True)
    with open(OUT_LOG, "w", encoding="utf-8") as f:
        f.write(f"语音模式端到端（假 NapCat，真实 TTS，未向真实 QQ 发送）\n时间：{time.strftime('%F %T')}\n\n")
        f.write("\n".join(lines) + "\n")
    log("evidence -> " + OUT_LOG)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args.url))
    except Exception as exc:  # noqa: BLE001
        log(f"[verify] 失败: {type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
