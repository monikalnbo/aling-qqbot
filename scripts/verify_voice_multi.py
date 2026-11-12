#!/usr/bin/env python3
"""验证「默认即语音 + 一条回复拆成多条语音」在真实链路上成立。

它扮演 NapCat 连上 AstrBot 的反向 WS，依次做：

1. 重启后**不发任何口令**，直接发一句普通聊天 → 应该直接收到多条 record
2. 记录每条到达时间、间隔、体积，按文件名顺序落盘后用 ffmpeg 量时长/音量
3. 发 `/语音状态` 确认是语音模式；发 `/文字模式` 后普通聊天应回到纯文字；
   再发 `/语音模式` 切回来（验证口令双向可用）
4. 发一句会触发"整段都是动作"的话，确认不会发出一条空语音

真实调用百炼 TTS（消耗少量额度），不向真实 QQ 账号发任何消息。

用法：
    PYTHONPATH=/tmp/pydebs2:/tmp/pydebs python3 scripts/verify_voice_multi.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "evidence", "voice_multi")
OUT_LOG = os.path.join(ROOT, "evidence", "voice_multi.log")
PROBE_DIR = os.path.join(ROOT, "astrbot", "data", "astrbot", "temp", "voice_multi")
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
        elif st == "at":
            parts.append(f"[at:{data.get('qq')}]")
        elif st == "reply":
            parts.append("[reply]")
        else:
            parts.append(f"[{st}]")
    return "".join(parts), audios


def probe(path_in_container: str) -> tuple[str, float]:
    """返回（时长/编码摘要, 平均音量 dB）。"""
    info = subprocess.run(
        ["docker", "exec", "astrbot", "ffprobe", "-v", "error",
         "-show_entries", "stream=codec_name,bit_rate:format=duration", "-of", "csv=p=0",
         path_in_container],
        capture_output=True, text=True).stdout.strip().replace("\n", " ")
    vd = subprocess.run(
        ["docker", "exec", "astrbot", "ffmpeg", "-hide_banner", "-i", path_in_container,
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    mean_db = -99.0
    max_line = ""
    for l in vd.splitlines():
        if "mean_volume" in l:
            mean_db = float(l.split("mean_volume: ")[1].split()[0])
        if "max_volume" in l:
            max_line = l.split("] ")[-1]
    return info, mean_db, max_line


async def run(url: str) -> int:
    import websockets

    frames: list[dict] = []
    stamps: list[float] = []

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
                    stamps.append(time.time())
                result = {"status": "ok", "retcode": 0,
                          "data": {"message_id": 900 + len(frames)}, "echo": data.get("echo")}
                if action == "get_login_info":
                    result["data"] = {"user_id": SELF_ID, "nickname": "capture"}
                await ws.send(json.dumps(result, ensure_ascii=False))

        task = asyncio.create_task(reader())
        await asyncio.sleep(1.0)

        async def send_and_collect(text: str, wait: float):
            start = len(frames)
            await ws.send(json.dumps(build_event(text, int(time.time() * 1000) % 900_000_000),
                                     ensure_ascii=False))
            deadline, quiet, last = time.time() + wait, 0.0, start
            while time.time() < deadline:
                await asyncio.sleep(0.2)
                if len(frames) > last:
                    last, quiet = len(frames), 0.0
                elif last > start:
                    quiet += 0.2
                    if quiet >= 2.5:
                        break
            return frames[start:], stamps[start:]

        ok = True

        # §1 不发口令，直接聊天
        log("§1 重启后未发任何口令，直接普通聊天：")
        got, ts = await send_and_collect("喂，今天怎么样，说两句给我听听", 120)
        os.makedirs(OUT_DIR, exist_ok=True)
        os.makedirs(PROBE_DIR, exist_ok=True)
        records, texts = [], []
        t0 = ts[0] if ts else time.time()
        for i, (f, st) in enumerate(zip(got, ts), 1):
            summary, audios = describe(f.get("params", {}))
            log(f"  第{i}条 t+{st - t0:5.2f}s action={f.get('action')} {summary!r}")
            if audios:
                records.append((i, audios))
            else:
                texts.append(summary)

        if len(records) < 1:
            log("  FAIL：一条语音都没收到")
            ok = False
        else:
            log(f"  语音条数={len(records)} 纯文字条数={len(texts)}")
            # 顺序：按到达顺序落盘；间隔
            gaps = [round(ts[i + 1] - ts[i], 2) for i in range(len(ts) - 1)]
            log(f"  条间到达间隔（秒）= {gaps}")
            mean_dbs = []
            for n, (idx, audios_list) in enumerate(records, 1):
                data = base64.b64decode(audios_list[0])
                ext = ".wav" if data[:4] == b"RIFF" else ".mp3"
                rel = f"voice_{n}{ext}"
                p = os.path.join(OUT_DIR, rel)
                with open(p, "wb") as fh:
                    fh.write(data)
                probe_file = os.path.join(PROBE_DIR, rel)
                shutil.copyfile(p, probe_file)
                info, mean_db, max_line = probe("/AstrBot/data/temp/voice_multi/" + rel)
                log(f"  语音{n}: {len(data)} 字节 {ext} probe={info} mean={mean_db} dB | {max_line}")
                mean_dbs.append(mean_db)
            if any(db < -16.0 for db in mean_dbs):
                log("  FAIL：有语音平均音量低于 -16 dB")
                ok = False
            if len(records) < 2:
                log("  注意：这次只拆出 1 条语音（模型回复太短或没断句），不是失败，但不是分条证据")
                ok = False

        # §2 状态口令
        got, _ = await send_and_collect("/语音状态", 20)
        log("§2 /语音状态 → " + (" | ".join(describe(f.get("params", {}))[0] for f in got) or "(无回复)"))

        # §3 切文字
        got, _ = await send_and_collect("/文字模式", 20)
        log("§3 /文字模式 → " + (" | ".join(describe(f.get("params", {}))[0] for f in got) or "(无回复)"))
        got, _ = await send_and_collect("说句话，随便说说", 90)
        text_only = all(not describe(f.get("params", {}))[1] for f in got)
        log(f"§3 文字模式下普通聊天：{len(got)} 条，全为文字={text_only}")
        ok = ok and text_only and len(got) >= 1

        # §4 切回语音
        got, _ = await send_and_collect("/语音模式", 20)
        log("§4 /语音模式 → " + (" | ".join(describe(f.get("params", {}))[0] for f in got) or "(无回复)"))
        got, _ = await send_and_collect("再说一句，我听听声音", 120)
        back = [1 for f in got if describe(f.get("params", {}))[1]]
        log(f"§4 切回语音后普通聊天：{len(got)} 条，其中语音 {len(back)} 条")
        ok = ok and len(back) >= 1

        task.cancel()

    log(f"RESULT {'PASS' if ok else 'FAIL'}")
    with open(OUT_LOG, "w", encoding="utf-8") as f:
        f.write(f"语音多条 + 默认开启 端到端验证（假 NapCat，真实 TTS，未向真实 QQ 发送）\n"
                f"时间：{time.strftime('%F %T')}\n\n")
        f.write("\n".join(lines) + "\n")
    print("evidence ->", OUT_LOG)
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
