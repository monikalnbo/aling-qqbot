"""用假 NapCat（OneBot v11 反向 WS）跟 cly 聊一句，看它到底回了什么。

不连真 QQ、不带真 key，纯离线验证：

    python3 cly/tools/mock_napcat_guard.py --url ws://127.0.0.1:6191/ws
    python3 cly/tools/mock_napcat_guard.py --url ws://127.0.0.1:6190/ws --text "在吗"

输出 JSON：回了几条、每条的文字、有没有语音段、从发出到收到第一条用了多久。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

SELF_ID = 10000
USER_ID = 123456


def _event(text: str) -> dict:
    return {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": int(time.time() * 1000) % 900_000_000,
        "user_id": USER_ID,
        "sender": {"user_id": USER_ID, "nickname": "测试用户"},
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "font": 0,
    }


async def run(url: str, text: str, wait: float) -> int:
    import websockets

    headers = {"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"}
    replies: list[dict] = []
    async with websockets.connect(url, additional_headers=headers, max_size=2**22) as ws:
        async def reader() -> None:
            while True:
                raw = await ws.recv()
                data = json.loads(raw)
                if data.get("action") in ("send_msg", "send_private_msg", "send_group_msg"):
                    replies.append(data)
                    await ws.send(
                        json.dumps(
                            {
                                "status": "ok",
                                "retcode": 0,
                                "data": {"message_id": 900 + len(replies)},
                                "echo": data.get("echo"),
                            }
                        )
                    )
                elif "action" in data:
                    await ws.send(
                        json.dumps(
                            {
                                "status": "ok",
                                "retcode": 0,
                                "data": {"user_id": SELF_ID, "nickname": "cly测试号"},
                                "echo": data.get("echo"),
                            }
                        )
                    )

        task = asyncio.create_task(reader())
        await ws.send(
            json.dumps(
                {
                    "time": int(time.time()),
                    "self_id": SELF_ID,
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                }
            )
        )
        await asyncio.sleep(2.0)  # 等 AstrBot 认出适配器
        t0 = time.time()
        await ws.send(json.dumps(_event(text), ensure_ascii=False))

        deadline = time.time() + wait
        while time.time() < deadline:
            await asyncio.sleep(0.3)
            if replies:
                await asyncio.sleep(1.5)  # 等可能的第二条
                break
        elapsed = round(time.time() - t0, 2)
        task.cancel()

    out = []
    for r in replies:
        segs = r.get("params", {}).get("message", [])
        texts = [
            s.get("data", {}).get("text", "")
            for s in segs
            if isinstance(s, dict) and s.get("type") == "text"
        ]
        kinds = [s.get("type") for s in segs if isinstance(s, dict)]
        out.append({"text": "".join(texts), "segments": kinds, "action": r.get("action")})
    print(
        json.dumps(
            {"url": url, "sent": text, "count": len(out), "elapsed_s": elapsed, "replies": out},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if out else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6191/ws")
    ap.add_argument("--text", default="你好，在吗")
    ap.add_argument("--wait", type=float, default=30.0)
    a = ap.parse_args()
    return asyncio.run(run(a.url, a.text, a.wait))


if __name__ == "__main__":
    sys.exit(main())
