"""改前取证：在 mock 群里连续 @ 阿凌聊多轮，量每轮上下文体积（插件日志 ctx-size / usage）。

用法：python3 scripts/probe_ctx_before.py --rounds 14 --out evidence/ctx_before.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time

SELF_ID = 10000
UID, NAME = 222333, "小胖"

LINES = [
    "今天下班好晚，累死了",
    "我想吃你上次说的那个串串",
    "对了，我最近在学吉他，手指疼",
    "你说我该不该换个工作啊",
    "我妈又催我相亲了，烦",
    "周末我想去看电影，有什么推荐吗",
    "我养的那只猫最近老掉毛",
    "你觉得我这个人是不是太内向了",
    "刚才说到电影，我其实更喜欢科幻片",
    "我打算下个月去趟成都玩",
    "最近天气冷了，你也要多穿点",
    "我昨天梦到自己在飞，很奇怪吧",
    "算了不说这些了，陪我说会儿话",
    "你还记得我一开始跟你说过什么吗",
]


def ev(text: str, mid: int) -> dict:
    return {
        "time": int(time.time()), "self_id": SELF_ID, "post_type": "message",
        "message_id": mid, "font": 0,
        "sender": {"user_id": UID, "nickname": NAME, "card": NAME},
        "to_me": True, "sub_type": "normal", "group_id": 0, "user_id": UID,
        "message_type": "group",
        "message": [{"type": "at", "data": {"qq": str(SELF_ID)}},
                    {"type": "text", "data": {"text": " " + text}}],
        "raw_message": f"[CQ:at,qq={SELF_ID}] {text}",
    }


async def main() -> int:
    import websockets

    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=14)
    ap.add_argument("--group", type=int, default=0)
    ap.add_argument("--out", default="evidence/ctx_before.log")
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    args = ap.parse_args()
    gid = args.group or (880000 + int(time.time()) % 1000)

    headers = {"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"}
    started = int(time.time())
    out: list[str] = []

    def emit(s: str = "") -> None:
        print(s, flush=True)
        out.append(s)

    async with websockets.connect(args.url, additional_headers=headers) as ws:
        replies = 0

        async def reader() -> None:
            nonlocal replies
            while True:
                d = json.loads(await ws.recv())
                if "action" not in d:
                    continue
                if d["action"] in ("send_msg", "send_group_msg"):
                    replies += 1
                await ws.send(json.dumps({"status": "ok", "retcode": 0,
                                          "data": {}, "echo": d.get("echo")}))

        task = asyncio.create_task(reader())
        await ws.send(json.dumps({"time": int(time.time()), "self_id": SELF_ID,
                                  "post_type": "meta_event",
                                  "meta_event_type": "lifecycle",
                                  "sub_type": "connect"}))
        await asyncio.sleep(1.0)

        emit(f"# 改前上下文体积取证  {time.strftime('%F %T %Z')}  群={gid}")
        for i in range(args.rounds):
            e = ev(LINES[i % len(LINES)], 88000 + i)
            e["group_id"] = gid
            before = replies
            await ws.send(json.dumps(e, ensure_ascii=False))
            t0 = time.time()
            deadline = time.time() + 90
            first = None
            while time.time() < deadline:
                if replies > before:
                    first = first or time.time()
                    if time.time() - first > 5:
                        break
                await asyncio.sleep(0.3)
            emit(f"轮{i + 1:02d} {LINES[i % len(LINES)]}  -> 回复 {replies - before} 条"
                 f" 首条 {(first - t0) if first else -1:.1f}s")
            await asyncio.sleep(1.0)

        task.cancel()

    logs = subprocess.run(["docker", "logs", "astrbot", "--since", f"{int(time.time()) - started + 10}s"],
                          capture_output=True, text=True, timeout=60).stdout
    emit()
    emit("## 每轮上下文体积（插件日志）")
    for ln in logs.splitlines():
        if "ctx-size" in ln or "usage umo=" in ln or "short-term umo=" in ln:
            m = re.sub(r"^.*aling-memory\] ", "", ln).strip()
            emit("  " + m)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"[probe] written {args.out}")
    emit(f"[group] {gid}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
