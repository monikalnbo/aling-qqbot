"""连贯性抽查：往已有长历史的群里再问几句，看它还能不能接上前文。

用法：python3 scripts/probe_coherence.py --group 880072 --out evidence/ctx_coherence.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

SELF_ID = 10000
UID, NAME = 222333, "小胖"


def ev(text: str, mid: int, gid: int) -> dict:
    return {
        "time": int(time.time()), "self_id": SELF_ID, "post_type": "message",
        "message_id": mid, "font": 0,
        "sender": {"user_id": UID, "nickname": NAME, "card": NAME},
        "to_me": True, "sub_type": "normal", "group_id": gid, "user_id": UID,
        "message_type": "group",
        "message": [{"type": "at", "data": {"qq": str(SELF_ID)}},
                    {"type": "text", "data": {"text": " " + text}}],
        "raw_message": f"[CQ:at,qq={SELF_ID}] {text}",
    }


ASK = [
    ("我下个月打算去哪儿玩来着？", ["成都"]),
    ("我养的那只猫怎么了？", ["掉毛", "毛"]),
    ("一开始我跟你说我工作的事，是怎么说的？", ["工作", "换"]),
]


async def main() -> int:
    import websockets

    ap = argparse.ArgumentParser()
    ap.add_argument("--group", type=int, default=880072)
    ap.add_argument("--out", default="evidence/ctx_coherence.log")
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    args = ap.parse_args()

    headers = {"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"}
    out: list[str] = []

    def emit(s: str = "") -> None:
        print(s, flush=True)
        out.append(s)

    async with websockets.connect(args.url, additional_headers=headers) as ws:
        replies: list[str] = []

        async def reader() -> None:
            while True:
                d = json.loads(await ws.recv())
                if "action" not in d:
                    continue
                if d.get("action") in ("send_msg", "send_group_msg"):
                    seg = d.get("params", {}).get("message", [])
                    txt = "".join(
                        s.get("data", {}).get("text", "")
                        for s in seg if isinstance(s, dict)
                    )
                    replies.append(txt or str(seg))
                await ws.send(json.dumps({"status": "ok", "retcode": 0,
                                          "data": {}, "echo": d.get("echo")}))

        task = asyncio.create_task(reader())
        await ws.send(json.dumps({"time": int(time.time()), "self_id": SELF_ID,
                                  "post_type": "meta_event",
                                  "meta_event_type": "lifecycle",
                                  "sub_type": "connect"}))
        await asyncio.sleep(1.0)
        emit(f"# 长对话后连贯性抽查  {time.strftime('%F %T')}  群={args.group}")
        ok = 0
        for i, (q, expect) in enumerate(ASK):
            n0 = len(replies)
            await ws.send(json.dumps(ev(q, 90000 + i, args.group), ensure_ascii=False))
            deadline = time.time() + 90
            t0 = time.time()
            while time.time() < deadline and len(replies) == n0:
                await asyncio.sleep(0.3)
            await asyncio.sleep(4)
            got = " / ".join(replies[n0:])
            hit = any(w in got for w in expect)
            ok += int(hit)
            emit(f"[问] {q}")
            emit(f"[答] {got.strip()[:300]}")
            emit(f"[判定] 命中 {expect} => {'PASS' if hit else 'FAIL'}  首条 {time.time() - t0:.1f}s")
            emit()
            await asyncio.sleep(1.0)
        task.cancel()

    emit(f"## 汇总 {ok}/{len(ASK)} PASS")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return 0 if ok == len(ASK) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
