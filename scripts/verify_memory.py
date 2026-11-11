"""真实链路验证：身份锚点 + 三层记忆（模拟 NapCat，真实 DeepSeek 调用）。

场景
----
1. attribution：两个不同 QQ 号在群里各说一句，问「刚才谁说的」→ 应答对昵称+QQ号
2. cross_session：A 说一个事实 → 换一个会话（新群号）再问 → 仍记得
3. isolation：同一会话里 B 问同一件事 → 不应取到 A 的记忆
4. window：超出短期窗口后，旧信息以摘要形式保留 → 仍能答对
5. cmd_status：/mem status 返回插件状态

用法：
    python3 scripts/verify_memory.py \
        --url ws://127.0.0.1:6199/ws --out evidence/memory_verify.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

SELF_ID = 10000
BOT_NAME = "大肥鱼测试号"
ADMIN_ID = <你的管理员QQ号>  # 2026-11-11 加入 admins_id 的 QQ 号


def fresh_id(offset: int = 0) -> int:
    return int(time.time() * 1000) % 10_000_000 * 10 + offset % 10


def at_event(
    text: str,
    *,
    message_id: int,
    group_id: int,
    user_id: int,
    nickname: str,
    card: str = "",
) -> dict:
    sender = {"user_id": user_id, "nickname": nickname}
    if card:
        sender["card"] = card
    return {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "message",
        "message_id": message_id,
        "font": 0,
        "sender": sender,
        "to_me": True,
        "sub_type": "normal",
        "group_id": group_id,
        "user_id": user_id,
        "message_type": "group",
        "message": [
            {"type": "at", "data": {"qq": str(SELF_ID)}},
            {"type": "text", "data": {"text": " " + text}},
        ],
        "raw_message": f"[CQ:at,qq={SELF_ID}] {text}",
    }


def plain_event(
    text: str, *, message_id: int, group_id: int, user_id: int, nickname: str
) -> dict:
    ev = at_event(
        text, message_id=message_id, group_id=group_id, user_id=user_id, nickname=nickname
    )
    ev["to_me"] = False
    ev["message"] = [{"type": "text", "data": {"text": text}}]
    ev["raw_message"] = text
    return ev


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.replies: list[dict] = []

    async def reader(self):
        while True:
            raw = await self.ws.recv()
            data = json.loads(raw)
            if "action" not in data:
                continue
            if data["action"] in ("send_msg", "send_private_msg", "send_group_msg"):
                self.replies.append(data)
            result = {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 900 + len(self.replies)},
                "echo": data.get("echo"),
            }
            if data["action"] == "get_login_info":
                result["data"] = {"user_id": SELF_ID, "nickname": BOT_NAME}
            await self.ws.send(json.dumps(result, ensure_ascii=False))

    async def say(self, ev: dict, timeout: float = 90.0) -> str | None:
        before = len(self.replies)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.replies) > before:
                await asyncio.sleep(0.4)
                return text_of(self.replies[-1])
            await asyncio.sleep(0.2)
        return None


def text_of(reply: dict) -> str:
    out = ""
    for seg in reply.get("params", {}).get("message", []):
        if isinstance(seg, dict) and seg.get("type") == "text":
            out += seg.get("data", {}).get("text", "")
    return out.strip()


async def run(url: str, token: str | None) -> int:
    import websockets

    headers = {"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    results: list[dict] = []
    checks: list[tuple[str, bool, str]] = []

    async with websockets.connect(url, additional_headers=headers, max_size=2**22) as ws:
        print(f"[verify] connected {url}", flush=True)
        cli = Client(ws)
        task = asyncio.create_task(cli.reader())
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
        await asyncio.sleep(1.2)

        base_gid = 660000 + int(time.time()) % 1000
        gA = base_gid + 1  # 场景 1/2 的群
        gB = base_gid + 2  # A 的「新会话」
        gW = base_gid + 3  # 窗口场景群

        # ---------- 场景 1：两个 QQ 号，谁说的 ----------
        r1 = await cli.say(
            at_event(
                "我叫小胖，今天穿的是红色外套",
                message_id=fresh_id(1),
                group_id=gA,
                user_id=222333,
                nickname="小胖",
                card="小胖",
            )
        )
        print(f"[1a] bot -> {r1}", flush=True)
        await asyncio.sleep(1.5)
        q1 = await cli.say(
            at_event(
                "刚才说穿红色外套的人是谁？把他的 QQ 号也说出来",
                message_id=fresh_id(2),
                group_id=gA,
                user_id=333444,
                nickname="阿哲",
                card="阿哲",
            )
        )
        print(f"[1b] bot -> {q1}", flush=True)
        ok1 = bool(q1) and "小胖" in q1 and "222333" in q1
        checks.append(("1 认人：答对小胖+222333", ok1, q1 or ""))
        results.append({"scene": "attribution", "reply": q1, "ok": ok1})

        # ---------- 场景 2/3：事实跨会话 + 用户隔离 ----------
        await asyncio.sleep(1.2)
        await cli.say(
            at_event(
                "记住，我家的猫叫年糕，是只橘猫",
                message_id=fresh_id(3),
                group_id=gA,
                user_id=222333,
                nickname="小胖",
                card="小胖",
            )
        )
        await asyncio.sleep(2.0)
        a2 = await cli.say(
            at_event(
                "我家的猫叫什么名字？",
                message_id=fresh_id(4),
                group_id=gB,
                user_id=222333,
                nickname="小胖",
                card="小胖",
            )
        )
        print(f"[2] 新会话里 A 问 -> {a2}", flush=True)
        ok2 = bool(a2) and "年糕" in a2
        checks.append(("2 跨会话：A 在新会话仍记得年糕", ok2, a2 or ""))
        results.append({"scene": "cross_session", "reply": a2, "ok": ok2})

        await asyncio.sleep(1.2)
        b2 = await cli.say(
            at_event(
                "我家的猫叫什么名字？",
                message_id=fresh_id(5),
                group_id=gB,
                user_id=333444,
                nickname="阿哲",
                card="阿哲",
            )
        )
        print(f"[3] 同一会话里 B 问 -> {b2}", flush=True)
        ok3 = bool(b2) and "年糕" not in b2
        checks.append(("3 隔离：B 问不到 A 的年糕", ok3, b2 or ""))
        results.append({"scene": "isolation", "reply": b2, "ok": ok3})

        # ---------- 场景 4：超窗摘要 ----------
        await asyncio.sleep(1.2)
        marker = "我昨天去了动物园，看到一只叫土豆的企鹅"
        await cli.say(
            at_event(
                marker,
                message_id=fresh_id(6),
                group_id=gW,
                user_id=222333,
                nickname="小胖",
                card="小胖",
            )
        )
        fillers = [
            ("阿哲", 333444, "今天外面好热啊"),
            ("小胖", 222333, "是啊，空调开到十六度"),
            ("阿哲", 333444, "你晚饭吃什么"),
            ("小胖", 222333, "还没想好，可能点外卖"),
            ("阿哲", 333444, "外卖太贵了"),
            ("小胖", 222333, "那就泡面吧"),
            ("阿哲", 333444, "泡面也不便宜"),
            ("小胖", 222333, "烦死了别算了"),
            ("阿哲", 333444, "哈哈你急什么"),
            ("小胖", 222333, "我没急"),
            ("阿哲", 333444, "好好好你没急"),
            ("小胖", 222333, "下班再说吧"),
        ]
        for i, (nick, uid, text) in enumerate(fillers):
            await asyncio.sleep(1.0)
            await cli.say(
                at_event(
                    text,
                    message_id=fresh_id(10 + i),
                    group_id=gW,
                    user_id=uid,
                    nickname=nick,
                    card=nick,
                )
            )
        await asyncio.sleep(1.5)
        w = await cli.say(
            at_event(
                "我昨天在动物园看到了什么？那只企鹅叫什么？",
                message_id=fresh_id(40),
                group_id=gW,
                user_id=222333,
                nickname="小胖",
                card="小胖",
            )
        )
        print(f"[4] 超窗后问 -> {w}", flush=True)
        ok4 = bool(w) and "土豆" in w
        checks.append(("4 超窗：摘要保留「土豆」", ok4, w or ""))
        results.append({"scene": "window_summary", "reply": w, "ok": ok4})

        # ---------- 场景 5：/mem status（管理员可用，普通成员被拒） ----------
        await asyncio.sleep(1.0)
        s = await cli.say(
            plain_event(
                "/mem status",
                message_id=fresh_id(50),
                group_id=gW,
                user_id=ADMIN_ID,
                nickname="管理员",
            )
        )
        print(f"[5] 管理员 /mem status -> {s}", flush=True)
        ok5 = bool(s) and "长期条目总数" in s
        checks.append(("5 管理员 /mem status 可用", ok5, s or ""))
        results.append({"scene": "cmd_status", "reply": s, "ok": ok5})

        await asyncio.sleep(1.0)
        s2 = await cli.say(
            plain_event(
                "/mem status",
                message_id=fresh_id(52),
                group_id=gW,
                user_id=222333,
                nickname="小胖",
            )
        )
        print(f"[5a] 普通成员 /mem status -> {s2}", flush=True)
        ok5a = bool(s2) and "记忆总览只有管理员能看" in s2
        checks.append(("5a 普通成员 /mem status 被拒", ok5a, s2 or ""))
        results.append({"scene": "cmd_status_denied", "reply": s2, "ok": ok5a})

        # B 在窗口群里问 A 的动物园 -> 不应串
        await asyncio.sleep(1.0)
        b5 = await cli.say(
            at_event(
                "我昨天在动物园看到了什么？",
                message_id=fresh_id(51),
                group_id=gW,
                user_id=333444,
                nickname="阿哲",
                card="阿哲",
            )
        )
        print(f"[5b] 窗口群里 B 问 -> {b5}", flush=True)
        ok5b = bool(b5) and "土豆" not in b5
        checks.append(("5b 隔离：B 问不到 A 的动物园记忆", ok5b, b5 or ""))
        results.append({"scene": "isolation_in_window", "reply": b5, "ok": ok5b})

        task.cancel()

    print("\n=== 汇总 ===", flush=True)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      -> {detail[:180]}", flush=True)
    all_ok = all(ok for _, ok, _ in checks)
    print(f"groups={gA},{gB},{gW}", flush=True)
    print(f"exit={0 if all_ok else 1}", flush=True)
    return 0 if all_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    ap.add_argument("--token", default=None)
    args = ap.parse_args()
    return asyncio.run(run(args.url, args.token))


if __name__ == "__main__":
    sys.exit(main())
