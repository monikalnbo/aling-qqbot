"""模拟 NapCat 的 OneBot v11 反向 WebSocket 客户端，用于离线验证 AstrBot。

用法：
    python scripts/mock_napcat.py                     # 跑全部场景，连 ws://127.0.0.1:6199/ws
    python scripts/mock_napcat.py --url ws://x:6199/ws
    python scripts/mock_napcat.py --token abc123      # 若 AstrBot 侧配了 token
    python scripts/mock_napcat.py --scenario private  # 只跑私聊

每个 API 请求都按 OneBot v11 规范回带 echo 的响应，因此 AstrBot 能正常收到
``send_msg`` / ``send_private_msg`` / ``get_login_info`` 等调用的结果。

退出码：0 = 所有场景符合预期；1 = 有场景不符合。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

SELF_ID = 10000  # 机器人 QQ 号（小号）
OWNER_ID = 123456  # 私聊用户
GROUP_ID = 555111  # 群号
GROUP_USER_ID = 222333  # 群里提问的人（默认）
GROUP_USER_ID_B = 333444  # 群里第二个人


def event(
    role: str,
    text: str,
    *,
    message_id: int,
    user_id: int = GROUP_USER_ID,
    nickname: str = "测试",
    card: str = "",
) -> dict:
    sender = {
        "user_id": user_id if role == "group" else OWNER_ID,
        "nickname": nickname,
    }
    # 空 card 会让 aiocqhttp 的事件解析失败，只有群名片非空时才带这个字段
    if card:
        sender["card"] = card
    base = {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "message",
        "message_id": message_id,
        "font": 0,
        "sender": sender,
        # 私聊一定是"对我说"；群聊只有被 @ 时才是（真实 NapCat 也是这样报的）
        "to_me": role == "private" or (role == "group" and "@" in text),
    }
    if role == "private":
        base.update(
            {
                "sub_type": "friend",
                "user_id": OWNER_ID,
                "message_type": "private",
                "message": [{"type": "text", "data": {"text": text}}],
                "raw_message": text,
            }
        )
    else:
        base.update(
            {
                "sub_type": "normal",
                "group_id": GROUP_ID,
                "user_id": user_id,
                "message_type": "group",
                "message": [{"type": "text", "data": {"text": text}}],
                "raw_message": text,
            }
        )
    return base


def fresh_id(offset: int = 0) -> int:
    """AstrBot 会按 message_id 去重，每次运行必须用新的 id。"""
    return int(time.time() * 1000) % 900_000_000 + offset


def group_at_event(
    text: str,
    message_id: int,
    *,
    user_id: int = GROUP_USER_ID,
    nickname: str = "测试",
    card: str = "",
) -> dict:
    ev = event(
        "group", text, message_id=message_id, user_id=user_id, nickname=nickname, card=card
    )
    ev["to_me"] = True
    ev["message"] = [
        {"type": "at", "data": {"qq": str(SELF_ID)}},
        {"type": "text", "data": {"text": " " + text}},
    ]
    ev["raw_message"] = f"[CQ:at,qq={SELF_ID}] {text}"
    return ev


class MockNapcat:
    def __init__(self, ws, token: str | None):
        self.ws = ws
        self.token = token
        self.replies: list[dict] = []

    async def _reader(self):
        while True:
            raw = await self.ws.recv()
            data = json.loads(raw)
            if "action" not in data:
                continue
            action = data["action"]
            echo = data.get("echo")
            if action in ("send_msg", "send_private_msg", "send_group_msg"):
                self.replies.append(data)
            result = {"status": "ok", "retcode": 0, "data": {"message_id": 900 + len(self.replies)}, "echo": echo}
            if action == "get_login_info":
                result["data"] = {"user_id": SELF_ID, "nickname": "大肥鱼测试号"}
            elif action == "get_group_info":
                result["data"] = {"group_id": GROUP_ID, "group_name": "测试群"}
            elif action in ("get_group_member_info", "get_stranger_info"):
                result["data"] = {"user_id": GROUP_USER_ID, "nickname": "测试"}
            await self.ws.send(json.dumps(result, ensure_ascii=False))

    async def wait_reply(self, timeout: float = 45.0) -> dict | None:
        before = len(self.replies)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.replies) > before:
                await asyncio.sleep(0.3)
                return self.replies[-1]
            await asyncio.sleep(0.2)
        return None


def reply_text(reply: dict) -> str:
    text = ""
    for seg in reply.get("params", {}).get("message", []):
        if isinstance(seg, dict) and seg.get("type") == "text":
            text += seg.get("data", {}).get("text", "")
    return text.strip()


async def run(url: str, token: str | None, scenarios: list[str]) -> int:
    import websockets

    headers = {"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with websockets.connect(url, additional_headers=headers, max_size=2**22) as ws:
        print(f"[mock-napcat] connected to {url}", flush=True)
        client = MockNapcat(ws, token)
        reader = asyncio.create_task(client._reader())
        # lifecycle 元事件：AstrBot 会据此打印“适配器已连接”
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
        await asyncio.sleep(1.0)

        results = []
        cases = {
            "private": (
                "私聊直接说话",
                event("private", "你好，我叫小明", message_id=fresh_id(1)),
                True,
            ),
            "private2": (
                "私聊第二句（看上下文）",
                event("private", "我刚才说我叫什么？", message_id=fresh_id(2)),
                True,
            ),
            "group_plain": (
                "群聊未 @ 且无触发",
                event("group", "今天天气不错", message_id=fresh_id(3)),
                False,
            ),
            "group_at": (
                "群聊 @ 机器人",
                group_at_event("在吗阿凌", fresh_id(4)),
                True,
            ),
            "group_identity": (
                "群聊认人：小胖说串串 / 阿哲问谁说的",
                None,
                True,
            ),
        }
        for offset, key in enumerate(scenarios):
            label, ev, expect = cases[key]
            offset = offset * 100
            if key == "group_identity":
                await ws.send(
                    json.dumps(
                        group_at_event(
                            "今晚的火锅我想吃串串",
                            fresh_id(5),
                            user_id=GROUP_USER_ID,
                            nickname="小胖",
                            card="小胖",
                        ),
                        ensure_ascii=False,
                    )
                )
                await client.wait_reply()
                await asyncio.sleep(1.0)
                ev = group_at_event(
                    "阿凌，刚才说想吃串串的是谁啊",
                    fresh_id(6),
                    user_id=GROUP_USER_ID_B,
                    nickname="阿哲",
                    card="阿哲",
                )
                await ws.send(json.dumps(ev, ensure_ascii=False))
            reply = await client.wait_reply(timeout=90.0)
            if reply is None and expect:
                # AstrBot 刚启动时首条事件可能还在排队，重发一次（换新 message_id 避免去重）
                ev = dict(ev)
                ev["message_id"] = fresh_id(offset + 500)
                await ws.send(json.dumps(ev, ensure_ascii=False))
                reply = await client.wait_reply(timeout=90.0)
            ok = bool(reply) == expect
            text = reply_text(reply) if reply else ""
            segs = [
                s.get("type")
                for s in (reply or {}).get("params", {}).get("message", [])
                if isinstance(s, dict)
            ]
            results.append(
                {
                    "scene": label,
                    "expect_reply": expect,
                    "ok": ok,
                    "reply": text,
                    "segments": segs,
                }
            )
            print(f"[mock-napcat] {label}: {'OK' if ok else 'FAIL'} -> {text or '(无回复)'}", flush=True)
            await asyncio.sleep(1.0)
        reader.cancel()
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(r["ok"] for r in results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    ap.add_argument("--token", default=None)
    ap.add_argument(
        "--scenario",
        default="all",
        choices=["all", "private", "private2", "group_plain", "group_at", "group_identity"],
    )
    args = ap.parse_args()
    scenarios = (
        ["private", "private2", "group_plain", "group_at", "group_identity"]
        if args.scenario == "all"
        else [args.scenario]
    )
    try:
        return asyncio.run(run(args.url, args.token, scenarios))
    except Exception as exc:  # noqa: BLE001
        print("[mock-napcat] 失败:", type(exc).__name__, exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
