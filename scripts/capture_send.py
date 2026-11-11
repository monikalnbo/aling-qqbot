"""抓取 AstrBot 实际发往 QQ 的 send_msg 帧（用来验证"分条发送"效果）。

它自己扮演 NapCat（连 AstrBot 的反向 WS 端点），发一句用户消息，然后记录
所有 send_* 调用的内容与到达时间，最后按「第 1 条 / 第 2 条 …」打印。

用法：
    python3 scripts/capture_send.py \
        --text "今天好困啊" [--private|--group] [--wait 60]

退出码：0 = 至少收到 1 条发送；2 = 一条都没有（或连接失败）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

SELF_ID = 10000


def build_event(text: str, *, group: bool, user_id: int, nickname: str, mid: int) -> dict:
    sender = {"user_id": user_id, "nickname": nickname}
    if group:
        sender["card"] = nickname
    base = {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "message",
        "message_id": mid,
        "font": 0,
        "sender": sender,
        "to_me": True,
    }
    if group:
        base.update(
            {
                "sub_type": "normal",
                "group_id": 555111,
                "user_id": user_id,
                "message_type": "group",
                "message": [
                    {"type": "at", "data": {"qq": str(SELF_ID)}},
                    {"type": "text", "data": {"text": " " + text}},
                ],
                "raw_message": f"[CQ:at,qq={SELF_ID}] {text}",
            }
        )
    else:
        base.update(
            {
                "sub_type": "friend",
                "user_id": user_id,
                "message_type": "private",
                "message": [{"type": "text", "data": {"text": text}}],
                "raw_message": text,
            }
        )
    return base


def segments_text(params: dict) -> str:
    parts = []
    for seg in params.get("message", []):
        if not isinstance(seg, dict):
            continue
        st = seg.get("type")
        data = seg.get("data", {})
        if st == "text":
            parts.append(data.get("text", ""))
        elif st == "at":
            parts.append(f"[at:{data.get('qq')}]")
        elif st == "reply":
            parts.append(f"[reply:{data.get('id')}]")
        elif st == "face":
            parts.append(f"[face:{data.get('id')}]")
        elif st == "image":
            parts.append("[image]")
        else:
            parts.append(f"[{st}]")
    return "".join(parts)


async def run(url: str, text: str, *, group: bool, user_id: int, nickname: str, wait: float):
    import websockets

    frames: list[tuple[float, dict]] = []
    t0 = time.time()

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
                if action in ("send_msg", "send_private_msg", "send_group_msg"):
                    frames.append((time.time() - t0, data))
                result = {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": 700 + len(frames)},
                    "echo": data.get("echo"),
                }
                if action == "get_login_info":
                    result["data"] = {"user_id": SELF_ID, "nickname": "capture"}
                else:
                    result["data"] = {"user_id": user_id, "nickname": nickname}
                await ws.send(json.dumps(result, ensure_ascii=False))

        task = asyncio.create_task(reader())
        await asyncio.sleep(1.0)
        ev = build_event(
            text,
            group=group,
            user_id=user_id,
            nickname=nickname,
            mid=int(time.time() * 1000) % 900_000_000,
        )
        await ws.send(json.dumps(ev, ensure_ascii=False))
        print(f"sent: {'group@' if group else 'private'} {text}", flush=True)

        deadline = time.time() + wait
        last = 0
        quiet = 0.0
        # 等到「出现至少一条 + 之后静默 3 秒」或超时
        while time.time() < deadline:
            await asyncio.sleep(0.2)
            if len(frames) > last:
                last = len(frames)
                quiet = 0.0
            elif last:
                quiet += 0.2
                if quiet >= 3.0:
                    break
        task.cancel()

    print(f"--- captured {len(frames)} send_* frame(s) ---")
    prev = 0.0
    for idx, (ts, data) in enumerate(frames, 1):
        params = data.get("params", {})
        gap = ts - prev if idx > 1 else ts
        print(f"[{idx}] t={ts:6.2f}s (+{gap:4.2f}s) action={data.get('action')} "
              f"target={params.get('user_id') or params.get('group_id')} "
              f"text={segments_text(params)!r}")
        prev = ts
    if frames:
        print(f"total wall time to last send: {frames[-1][0]:.2f}s")
    return 0 if frames else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    ap.add_argument("--text", required=True)
    ap.add_argument("--group", action="store_true", help="群聊 @（默认私聊）")
    ap.add_argument("--user-id", type=int, default=123456)
    ap.add_argument("--nickname", default="测试")
    ap.add_argument("--wait", type=float, default=60.0)
    args = ap.parse_args()
    try:
        return asyncio.run(
            run(
                args.url,
                args.text,
                group=args.group,
                user_id=args.user_id,
                nickname=args.nickname,
                wait=args.wait,
            )
        )
    except Exception as exc:  # noqa: BLE001
        print("[capture] 失败:", type(exc).__name__, exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
